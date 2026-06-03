"""Auth-Blueprint: Google-OAuth + Approval-Flow.

Routen:
- ``GET  /auth/login``           — Login-Seite mit Google-Button.
- ``GET  /auth/google``          — Redirect zu Google.
- ``GET  /auth/google/callback`` — OAuth-Rückkanal, User anlegen/aktualisieren.
- ``GET  /auth/logout``          — Session beenden.

Außerdem registriert :func:`register_auth_guard` einen ``before_request``-Hook
auf der App, der nicht-eingeloggte Requests zu ``/auth/login`` umleitet und
nicht-approved User auf die jeweilige Status-Seite zwingt.
"""

from __future__ import annotations

from datetime import date

from flask import (
    Blueprint,
    Flask,
    abort,
    current_app,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user

from app.domain.enums import Role, UserStatus
from app.extensions import db, oauth
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


# ---------------------------------------------------------------------------
# Routen
# ---------------------------------------------------------------------------


@bp.route("/login")
def login() -> str:
    """Login-Seite mit 'Mit Google anmelden'-Button."""
    if current_user.is_authenticated and current_user.status == UserStatus.APPROVED:
        return redirect(url_for("dashboard.index"))
    return render_template("auth/login.html")


@bp.route("/google")
def google():
    """Startet OAuth-Flow Richtung Google."""
    redirect_uri = url_for("auth.google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@bp.route("/google/callback")
def google_callback():
    """OAuth-Callback: User finden/anlegen + Session setzen."""
    try:
        token = oauth.google.authorize_access_token()
    except Exception:  # noqa: BLE001
        current_app.logger.exception("Google OAuth callback fehlgeschlagen")
        return render_template("auth/login.html", error="oauth_failed"), 400

    userinfo = token.get("userinfo")
    if not userinfo:
        # Fallback: aktiv abrufen.
        try:
            userinfo = oauth.google.parse_id_token(token, nonce=session.get("oauth_nonce"))
        except Exception:  # noqa: BLE001
            userinfo = None
    if not userinfo:
        return render_template("auth/login.html", error="oauth_no_userinfo"), 400

    google_sub = userinfo.get("sub")
    email = userinfo.get("email")
    name = userinfo.get("name") or email
    avatar_url = userinfo.get("picture")

    if not google_sub or not email:
        return render_template("auth/login.html", error="oauth_no_identity"), 400

    user = db.session.query(User).filter_by(google_sub=google_sub).first()
    if user is None:
        # Eventuell existiert der User via E-Mail bereits (manuell angelegt).
        user = db.session.query(User).filter_by(email=email).first()

    if user is None:
        user = User(
            google_sub=google_sub,
            email=email,
            name=name,
            avatar_url=avatar_url,
            status=UserStatus.PENDING,
        )
        db.session.add(user)
    else:
        # Profil-Sync für Returning User.
        user.google_sub = google_sub
        user.name = name
        user.avatar_url = avatar_url

    db.session.commit()

    login_user(user)

    if user.status == UserStatus.APPROVED:
        return redirect(url_for("dashboard.index"))
    if user.status == UserStatus.PENDING:
        return render_template("auth/pending.html", user=user)
    # REJECTED:
    logout_user()
    return render_template("auth/rejected.html", user=user), 403


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------------------
# Dev-Login (ohne Google) — nur aktiv, wenn DEV_LOGIN_ENABLED gesetzt ist.
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
    login_user(user)
    if user.status == UserStatus.APPROVED:
        return redirect(url_for("dashboard.index"))
    return _render_status_page(user) or redirect(url_for("auth.login"))


# ---------------------------------------------------------------------------
# Globaler Auth-Guard
# ---------------------------------------------------------------------------


# Endpunkte, die auch ohne Login erreichbar sein müssen.
_PUBLIC_ENDPOINTS = frozenset(
    {
        "static",
        "auth.login",
        "auth.google",
        "auth.google_callback",
        "auth.logout",
        "auth.dev_login",
        "auth.dev_login_as",
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

        # Eingeloggt, aber Status prüfen.
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
    """Fügt eine Rolle hinzu, falls noch nicht vorhanden. Gibt True bei Änderung zurück."""
    if user_has_role(user, role):
        return False
    user.roles.append(UserRole(user_id=user.id, role=role))
    return True


def revoke_role(user: User, role: Role) -> bool:
    """Entfernt eine Rolle, falls vorhanden. Gibt True bei Änderung zurück."""
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
