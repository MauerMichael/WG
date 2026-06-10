"""Auth-Blueprint: Username + Passwort + Approval-Flow.

Routen:
- ``GET  /auth/login``           — Login-Form (Username + Passwort).
- ``POST /auth/login``           — Login pruefen, ``login_user`` + Status/Redirect.
- ``GET  /auth/change-password`` — Passwort-Wechsel-Form (login-required).
- ``POST /auth/change-password`` — Passwort wechseln.
- ``GET  /auth/logout``          — Session beenden.
- ``GET  /auth/dev``             — Dev-Login (nur wenn ``DEV_LOGIN_ENABLED``).
- ``POST /auth/dev/<uuid>``      — Dev-Login als bestimmter User.

Google-OAuth ist absichtlich entfernt (HTTPS-Zwang/Redirect-URI-Pflicht zu
sperrig). ``google_sub`` bleibt als Spalte erhalten (additiv, ungenutzt).

Ausserdem registriert :func:`register_auth_guard` einen ``before_request``-Hook
auf der App, der nicht-eingeloggte Requests zu ``/auth/login`` umleitet,
nicht-approved User auf die jeweilige Status-Seite zwingt und Accounts mit
``must_change_password=True`` zwingend zu ``/auth/change-password`` schickt.
"""

from __future__ import annotations

from datetime import date

from flask import (
    Blueprint,
    Flask,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import func
from werkzeug.security import check_password_hash, generate_password_hash

from app.domain.enums import Role, UserStatus
from app.extensions import db
from app.models.user import User, UserRole

bp = Blueprint("auth", __name__, template_folder="../../templates/auth")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_status_page(user: User):
    """Rendert pending/rejected-Seiten je nach User-Status."""
    if user.status == UserStatus.PENDING:
        return render_template("auth/pending.html", user=user)
    if user.status == UserStatus.REJECTED:
        # Rejected: Session sofort beenden und Hinweisseite zeigen.
        logout_user()
        return render_template("auth/rejected.html", user=user), 403
    return None


def _safe_next(target: str | None) -> str | None:
    """Verhindert Open-Redirect: erlaubt nur relative Pfade."""
    if not target:
        return None
    if not target.startswith("/") or target.startswith("//"):
        return None
    return target


def _post_login_redirect(user: User, next_url: str | None):
    """Default-Redirect-Reihenfolge nach erfolgreichem Login."""
    if user.must_change_password:
        return redirect(url_for("auth.change_password"))
    if user.status != UserStatus.APPROVED:
        page = _render_status_page(user)
        if page is not None:
            return page
    safe = _safe_next(next_url)
    if safe:
        return redirect(safe)
    return redirect(url_for("dashboard.index"))


# ---------------------------------------------------------------------------
# Routen
# ---------------------------------------------------------------------------


@bp.route("/login", methods=["GET"])
def login():
    """Login-Seite (Username + Passwort)."""
    if current_user.is_authenticated and current_user.status == UserStatus.APPROVED:
        if current_user.must_change_password:
            return redirect(url_for("auth.change_password"))
        return redirect(url_for("dashboard.index"))
    next_url = request.args.get("next")
    return render_template("auth/login.html", next_url=next_url)


@bp.route("/login", methods=["POST"])
def login_submit():
    """Login pruefen: Username + Passwort -> Session."""
    next_url = request.form.get("next") or request.args.get("next")
    username = (request.form.get("username") or "").strip().lower()
    password = request.form.get("password") or ""

    if not username or not password:
        return render_template(
            "auth/login.html",
            error="missing_fields",
            username=username,
            next_url=next_url,
        ), 400

    user = (
        db.session.query(User)
        .filter(func.lower(User.username) == username)
        .first()
    )
    if user is None or not user.password_hash or not check_password_hash(
        user.password_hash, password
    ):
        return render_template(
            "auth/login.html",
            error="invalid_credentials",
            username=username,
            next_url=next_url,
        ), 401

    # Session + Remember-Cookie permanent machen, sonst geht beim Tab-Schliessen
    # alles verloren (Default = Browser-Session-Cookie).
    session.permanent = True
    login_user(user, remember=True)
    return _post_login_redirect(user, next_url)


@bp.route("/change-password", methods=["GET"])
@login_required
def change_password():
    """Passwort-Wechsel-Form."""
    return render_template("auth/change_password.html")


@bp.route("/change-password", methods=["POST"])
@login_required
def change_password_submit():
    """Passwort wechseln: altes validieren, neues hashen."""
    current_pw = request.form.get("current_password") or ""
    new_pw = request.form.get("new_password") or ""
    confirm_pw = request.form.get("confirm_password") or ""

    errors: list[str] = []
    if not current_user.password_hash or not check_password_hash(
        current_user.password_hash, current_pw
    ):
        errors.append("Aktuelles Passwort stimmt nicht.")
    if len(new_pw) < 8:
        errors.append("Neues Passwort muss mindestens 8 Zeichen lang sein.")
    if new_pw != confirm_pw:
        errors.append("Neues Passwort und Bestaetigung stimmen nicht ueberein.")

    if errors:
        return render_template(
            "auth/change_password.html",
            errors=errors,
        ), 400

    current_user.password_hash = generate_password_hash(new_pw)
    current_user.must_change_password = False
    db.session.commit()
    flash("Passwort erfolgreich geaendert.", "success")
    return redirect(url_for("dashboard.index"))


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------------------
# Dev-Login (ohne Passwort) — nur aktiv, wenn DEV_LOGIN_ENABLED gesetzt ist.
# ---------------------------------------------------------------------------


@bp.route("/dev")
def dev_login():
    """Listet alle User auf und erlaubt Einloggen per Klick (nur Dev)."""
    if not current_app.config.get("DEV_LOGIN_ENABLED"):
        abort(404)
    users = db.session.query(User).order_by(User.name).all()
    return render_template("auth/dev_login.html", users=users)


@bp.route("/dev/<uuid:user_id>", methods=["POST"])
def dev_login_as(user_id):
    """Loggt als angegebener User ein (nur Dev)."""
    if not current_app.config.get("DEV_LOGIN_ENABLED"):
        abort(404)
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    session.permanent = True
    login_user(user, remember=True)
    # Dev-Login umgeht must_change_password absichtlich (lokales Testen).
    if user.status == UserStatus.APPROVED:
        return redirect(url_for("dashboard.index"))
    return _render_status_page(user) or redirect(url_for("auth.login"))


# ---------------------------------------------------------------------------
# Globaler Auth-Guard
# ---------------------------------------------------------------------------


# Endpunkte, die auch ohne Login erreichbar sein muessen.
_PUBLIC_ENDPOINTS = frozenset(
    {
        "static",
        "auth.login",
        "auth.login_submit",
        "auth.logout",
        "auth.dev_login",
        "auth.dev_login_as",
        # PWA: Browser holt Manifest/Service-Worker anonym (auch auf der
        # Login-Seite); Offline-Seite muss ebenfalls ohne Session laden.
        "pwa.manifest",
        "pwa.service_worker",
        "pwa.offline",
    }
)

# Endpunkte, die ein eingeloggter User mit must_change_password=True
# erreichen darf (sonst Endlos-Redirect auf die Wechsel-Form).
_PASSWORD_EXEMPT_ENDPOINTS = frozenset(
    {
        "auth.change_password",
        "auth.change_password_submit",
        "auth.logout",
        "static",
    }
)


def register_auth_guard(app: Flask) -> None:
    """Registriert die globale ``before_request``-Auth-Logik auf der App."""

    @app.before_request
    def _enforce_login():
        endpoint = request.endpoint
        if endpoint is None:
            return None
        if endpoint in _PUBLIC_ENDPOINTS:
            return None
        # Static-Dateien aus Blueprints durchlassen.
        if endpoint.endswith(".static"):
            return None
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login", next=request.path))

        # Erzwungener Passwort-Wechsel hat Vorrang vor Status-Check, damit
        # frisch angelegte Accounts (immer APPROVED) ihr Passwort setzen.
        if (
            getattr(current_user, "must_change_password", False)
            and endpoint not in _PASSWORD_EXEMPT_ENDPOINTS
        ):
            return redirect(url_for("auth.change_password"))

        # Eingeloggt, aber Status pruefen.
        if current_user.status == UserStatus.APPROVED:
            return None
        return _render_status_page(current_user)


# ---------------------------------------------------------------------------
# Rolle-Helpers (auch von Admin-Blueprint genutzt)
# ---------------------------------------------------------------------------


def user_has_role(user: User, role: Role) -> bool:
    """True, wenn ``user`` ``role`` besitzt."""
    if not user or not user.is_authenticated:
        return False
    return any(r.role == role for r in user.roles)


def user_has_any_role(user: User, *roles: Role) -> bool:
    if not user or not user.is_authenticated:
        return False
    role_set = {r.role for r in user.roles}
    return any(r in role_set for r in roles)


def require_admin_or_hauswart():
    """Bricht den Request mit 403 ab, wenn der User weder Admin noch Hauswart ist."""
    if not current_user.is_authenticated:
        abort(401)
    if current_user.status != UserStatus.APPROVED:
        abort(403)
    if not user_has_any_role(current_user, Role.ADMIN, Role.HAUSWART):
        abort(403)


def grant_role(user: User, role: Role) -> bool:
    """Fuegt eine Rolle hinzu, falls noch nicht vorhanden. Gibt True bei Aenderung zurueck."""
    if user_has_role(user, role):
        return False
    user.roles.append(UserRole(user_id=user.id, role=role))
    return True


def revoke_role(user: User, role: Role) -> bool:
    """Entfernt eine Rolle, falls vorhanden. Gibt True bei Aenderung zurueck."""
    to_remove = [r for r in user.roles if r.role == role]
    if not to_remove:
        return False
    for r in to_remove:
        db.session.delete(r)
        user.roles.remove(r)
    return True


def ensure_joined_at(user: User) -> None:
    """Setzt ``joined_at`` auf heute, falls noch leer."""
    if user.joined_at is None:
        from datetime import datetime, timezone

        user.joined_at = datetime.combine(
            date.today(),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
