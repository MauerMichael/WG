"""Admin-Blueprint: User-Approval, Rollen-Toggle.

Zugriff nur für User mit Rolle ``ADMIN`` oder ``HAUSWART``. Alle State-Changes
schreiben einen Eintrag in ``AuditLog``. HTMX-Endpunkte rendern Teil-Templates
für Inline-Updates; ohne HTMX wird auf die volle Listen-Seite zurückgeleitet.
"""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import date, datetime, timezone

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import case, func
from werkzeug.security import generate_password_hash

from app.blueprints.auth import (
    ensure_joined_at,
    grant_role,
    require_admin_or_hauswart,
    revoke_role,
    user_has_role,
)
from app.domain.enums import Role, UserStatus
from app.extensions import db
from app.models.audit import AuditLog
from app.models.karma import KarmaEvent
from app.models.task import TaskOccurrence
from app.models.user import User, UserRole
from app.services import scheduling

# a-z, 0-9, Bindestrich, Underscore — bewusst eng, damit Usernames URL-safe sind.
_USERNAME_RE = re.compile(r"^[a-z0-9_-]{2,64}$")

bp = Blueprint("admin", __name__, template_folder="../../templates/admin")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _get_user_or_404(user_id: str) -> User:
    try:
        uid = uuid.UUID(user_id)
    except (ValueError, TypeError):
        abort(404)
    user = db.session.get(User, uid)
    if user is None:
        abort(404)
    return user


def _audit(action: str, target: User, payload: dict | None = None) -> None:
    log = AuditLog(
        user_id=current_user.id,
        action=action,
        entity_type="user",
        entity_id=target.id,
        payload=payload or {},
    )
    db.session.add(log)


def _row_response(user: User):
    """Antwort für HTMX-Mutationen: Zeile als Partial, sonst Vollreload."""
    if _is_htmx():
        return render_template(
            "admin/_user_row.html",
            user=user,
            can_grant_admin=user_has_role(current_user, Role.ADMIN),
        )
    return redirect(url_for("admin.users"))


def _load_users_sorted() -> list[User]:
    status_order = case(
        (User.status == UserStatus.PENDING, 0),
        (User.status == UserStatus.APPROVED, 1),
        (User.status == UserStatus.REJECTED, 2),
        else_=3,
    )
    return (
        db.session.query(User)
        .order_by(status_order, User.created_at.asc())
        .all()
    )


# ---------------------------------------------------------------------------
# Routen
# ---------------------------------------------------------------------------


@bp.route("/")
@login_required
def index():
    require_admin_or_hauswart()
    return redirect(url_for("admin.users"))


@bp.route("/users", methods=["GET"])
@login_required
def users():
    require_admin_or_hauswart()
    user_list = _load_users_sorted()
    return render_template(
        "admin/users.html",
        users=user_list,
        Role=Role,
        UserStatus=UserStatus,
        can_grant_admin=user_has_role(current_user, Role.ADMIN),
    )


@bp.route("/users/<user_id>", methods=["GET"])
@login_required
def user_detail(user_id: str):
    require_admin_or_hauswart()
    user = _get_user_or_404(user_id)
    return render_template(
        "admin/user_detail.html",
        user=user,
        Role=Role,
        UserStatus=UserStatus,
        can_grant_admin=user_has_role(current_user, Role.ADMIN),
    )


@bp.route("/users/<user_id>/approve", methods=["POST"])
@login_required
def approve(user_id: str):
    require_admin_or_hauswart()
    user = _get_user_or_404(user_id)

    previous_status = user.status
    user.status = UserStatus.APPROVED
    ensure_joined_at(user)
    role_added = grant_role(user, Role.HAUSBEWOHNER)
    # Frischer Bewohner kommt in den Pool: bestehende OPEN-Future-Zuweisungen
    # auf den neuen Stand bringen (sonst kriegt er erst beim nächsten Cron was).
    db.session.flush()
    rebalanced = scheduling.rebalance_open_assignments(db.session)
    _audit(
        "user.approve",
        user,
        {
            "previous_status": str(previous_status),
            "role_added_default": role_added,
            "rebalanced_swaps": rebalanced,
        },
    )
    db.session.commit()
    if not _is_htmx():
        msg = f"{user.name} wurde freigeschaltet."
        if rebalanced:
            msg += f" {rebalanced} Aufgabe(n) neu verteilt."
        flash(msg, "success")
    return _row_response(user)


@bp.route("/users/<user_id>/reject", methods=["POST"])
@login_required
def reject(user_id: str):
    require_admin_or_hauswart()
    user = _get_user_or_404(user_id)

    previous_status = user.status
    user.status = UserStatus.REJECTED
    _audit("user.reject", user, {"previous_status": str(previous_status)})
    db.session.commit()
    if not _is_htmx():
        flash(f"{user.name} wurde abgelehnt.", "success")
    return _row_response(user)


@bp.route("/users/<user_id>/roles", methods=["POST"])
@login_required
def toggle_role(user_id: str):
    require_admin_or_hauswart()
    user = _get_user_or_404(user_id)

    raw_role = (request.form.get("role") or "").strip().upper()
    action = (request.form.get("action") or "").strip().lower()
    if raw_role not in Role.__members__ or action not in {"add", "remove"}:
        abort(400)
    role = Role[raw_role]

    # Nur ADMIN darf ADMIN vergeben/entziehen.
    if role == Role.ADMIN and not user_has_role(current_user, Role.ADMIN):
        abort(403)

    changed = False
    reassigned = 0
    rebalanced = 0
    if action == "add":
        changed = grant_role(user, role)
        # HAUSBEWOHNER-Rolle frisch vergeben: in den Verteilungs-Pool integrieren.
        if changed and role == Role.HAUSBEWOHNER:
            db.session.flush()
            rebalanced = scheduling.rebalance_open_assignments(db.session)
    else:
        # Verhindern, dass sich Admin selbst die letzte Admin-Rolle entzieht
        # und niemand mehr Admin ist.
        if (
            role == Role.ADMIN
            and user.id == current_user.id
            and sum(1 for r in user.roles if r.role == Role.ADMIN) <= 1
        ):
            # Anzahl Admins gesamt prüfen
            total_admins = (
                db.session.query(User)
                .join(User.roles)
                .filter(User.id != user.id)
                .filter(User.status == UserStatus.APPROVED)
            )
            from app.models.user import UserRole as _UR

            admin_count = (
                db.session.query(_UR)
                .filter(_UR.role == Role.ADMIN, _UR.user_id != user.id)
                .count()
            )
            if admin_count == 0:
                if _is_htmx():
                    return (
                        "Letzter Admin kann sich selbst nicht entziehen.",
                        409,
                    )
                flash("Letzter Admin kann sich selbst nicht entziehen.", "error")
                return redirect(url_for("admin.users"))
            _ = total_admins  # noqa: F841
        changed = revoke_role(user, role)
        # Auszug aus der WG (HAUSBEWOHNER entzogen): Account/Status bleiben, aber
        # die Person fällt aus dem Zuweisungs-Pool. Offene Aufgaben an die übrigen
        # Bewohner zurückgeben, statt sie verwaist hängen zu lassen.
        if changed and role == Role.HAUSBEWOHNER:
            db.session.flush()
            reassigned = scheduling.reassign_all_open_for(db.session, user)

    _audit(
        "user.role." + action,
        user,
        {
            "role": str(role),
            "changed": changed,
            "reassigned_occurrences": reassigned,
            "rebalanced_swaps": rebalanced,
        },
    )
    db.session.commit()
    if not _is_htmx() and (reassigned or rebalanced):
        msg_parts = []
        if reassigned:
            msg_parts.append(f"{reassigned} offene Aufgabe(n) neu verteilt")
        if rebalanced:
            msg_parts.append(f"{rebalanced} Verteilungs-Swap(s)")
        flash(", ".join(msg_parts) + ".", "success")
    return _row_response(user)


@bp.route("/users/<user_id>/delete", methods=["POST"])
@login_required
def delete(user_id: str):
    """Soft-Delete: Status=REJECTED + alle Rollen entfernen."""
    require_admin_or_hauswart()
    user = _get_user_or_404(user_id)

    if user.id == current_user.id:
        if _is_htmx():
            return ("Eigenen Account kann man nicht löschen.", 409)
        flash("Eigenen Account kann man nicht löschen.", "error")
        return redirect(url_for("admin.users"))

    removed_roles = [str(r.role) for r in list(user.roles)]
    for r in list(user.roles):
        db.session.delete(r)
        user.roles.remove(r)
    previous_status = user.status
    user.status = UserStatus.REJECTED
    # Status/Rollen-Entzug sichtbar machen, bevor neu verteilt wird, damit die
    # entfernte Person nicht erneut als Kandidat auftaucht.
    db.session.flush()

    # Offene Aufgaben der entfernten Person an die übrigen Bewohner zurückgeben,
    # statt sie verwaist hängen zu lassen.
    reassigned = scheduling.reassign_all_open_for(db.session, user)

    _audit(
        "user.delete",
        user,
        {
            "previous_status": str(previous_status),
            "removed_roles": removed_roles,
            "reassigned_occurrences": reassigned,
        },
    )
    db.session.commit()
    if not _is_htmx():
        msg = f"{user.name} wurde entfernt."
        if reassigned:
            msg += f" {reassigned} offene Aufgabe(n) neu verteilt."
        flash(msg, "success")
    return _row_response(user)


# ---------------------------------------------------------------------------
# User-Anlage + Passwort-Reset (Username/Passwort statt Google-OAuth)
# ---------------------------------------------------------------------------


def _generate_temp_password() -> str:
    """Erzeugt ein einmaliges temporaeres Passwort (URL-safe, ~11 Zeichen).

    Wird genau einmal in der Success-Karte angezeigt; muss vom Empfaenger
    sofort weitergeleitet werden, da kein Speicher.
    """
    return secrets.token_urlsafe(8)


def _username_taken(username: str) -> bool:
    return (
        db.session.query(User)
        .filter(func.lower(User.username) == username.lower())
        .first()
        is not None
    )


@bp.route("/users/new", methods=["GET"])
@login_required
def new_user_form():
    """Formular zum Anlegen eines neuen Accounts (Username + temp Passwort)."""
    require_admin_or_hauswart()
    return render_template(
        "admin/new_user.html",
        Role=Role,
        can_grant_admin=user_has_role(current_user, Role.ADMIN),
        form={},
        errors=[],
        created=None,
    )


@bp.route("/users/new", methods=["POST"])
@login_required
def new_user_create():
    """Legt einen Account mit temporaerem Passwort an."""
    require_admin_or_hauswart()

    name = (request.form.get("name") or "").strip()
    username = (request.form.get("username") or "").strip().lower()
    raw_roles = request.form.getlist("roles") or []
    can_grant_admin = user_has_role(current_user, Role.ADMIN)

    errors: list[str] = []
    if not name:
        errors.append("Name darf nicht leer sein.")
    if not username:
        errors.append("Benutzername darf nicht leer sein.")
    elif not _USERNAME_RE.match(username):
        errors.append(
            "Benutzername muss 2–64 Zeichen aus a–z, 0–9, _ und - enthalten."
        )
    elif _username_taken(username):
        errors.append("Benutzername ist bereits vergeben.")

    # Rollen validieren: HAUSBEWOHNER wird immer gesetzt (Standard-Mitglied).
    roles_to_grant: set[Role] = {Role.HAUSBEWOHNER}
    for r in raw_roles:
        if r == Role.HAUSWART.name:
            roles_to_grant.add(Role.HAUSWART)
        elif r == Role.ADMIN.name:
            if not can_grant_admin:
                errors.append("Nur Admins koennen die Admin-Rolle vergeben.")
            else:
                roles_to_grant.add(Role.ADMIN)
        elif r == Role.HAUSBEWOHNER.name:
            roles_to_grant.add(Role.HAUSBEWOHNER)

    if errors:
        return render_template(
            "admin/new_user.html",
            Role=Role,
            can_grant_admin=can_grant_admin,
            form={"name": name, "username": username, "roles": raw_roles},
            errors=errors,
            created=None,
        ), 400

    temp_password = _generate_temp_password()
    now = datetime.now(timezone.utc)
    user = User(
        username=username,
        name=name,
        status=UserStatus.APPROVED,
        joined_at=now,
        password_hash=generate_password_hash(temp_password),
        must_change_password=True,
    )
    db.session.add(user)
    db.session.flush()
    for role in roles_to_grant:
        db.session.add(UserRole(user_id=user.id, role=role))

    # Neuer Bewohner: bestehende OPEN-Future-Zuweisungen rebalancen, sodass
    # er sofort eingebunden ist (nicht erst beim nächsten Cron).
    db.session.flush()
    rebalanced = scheduling.rebalance_open_assignments(db.session)

    _audit(
        "user.create",
        user,
        {
            "username": username,
            "name": name,
            "roles": sorted(r.name for r in roles_to_grant),
            "rebalanced_swaps": rebalanced,
        },
    )
    db.session.commit()

    return render_template(
        "admin/new_user.html",
        Role=Role,
        can_grant_admin=can_grant_admin,
        form={},
        errors=[],
        created={
            "user": user,
            "username": username,
            "temp_password": temp_password,
        },
    )


@bp.route("/users/<user_id>/reset-password", methods=["POST"])
@login_required
def reset_password(user_id: str):
    """Setzt das Passwort des Users auf ein neues temporaeres zurueck."""
    require_admin_or_hauswart()
    user = _get_user_or_404(user_id)

    temp_password = _generate_temp_password()
    user.password_hash = generate_password_hash(temp_password)
    user.must_change_password = True
    _audit("user.reset_password", user, {"username": user.username})
    db.session.commit()

    if _is_htmx():
        return render_template(
            "admin/_password_reset_card.html",
            user=user,
            temp_password=temp_password,
        )
    flash(
        f"Neues temporaeres Passwort fuer {user.name}: {temp_password}",
        "success",
    )
    return redirect(url_for("admin.users"))


# ---------------------------------------------------------------------------
# Wartungs-Aktionen
# ---------------------------------------------------------------------------


@bp.route("/maintenance/rebalance", methods=["POST"])
@login_required
def maintenance_rebalance():
    """Triggert manuell rebalance_open_assignments fuer die ganze WG."""
    require_admin_or_hauswart()
    swaps = scheduling.rebalance_open_assignments(db.session)
    _audit(
        "maintenance.rebalance",
        current_user,
        {"swaps": swaps},
    )
    db.session.commit()
    if swaps:
        flash(f"{swaps} offene Aufgabe(n) neu verteilt.", "success")
    else:
        flash("Verteilung war schon ausgewogen — keine Aenderungen.", "info")
    return redirect(url_for("admin.users"))


@bp.route("/maintenance/reset-stats", methods=["POST"])
@login_required
def maintenance_reset_stats():
    """Setzt Statistiken + Historie zurueck (destruktiv).

    Loescht:
    - Alle KarmaEvents (HONOR + PENALTY)
    - Alle vergangenen Occurrences inkl. Assignments (period_end < heute)
    - User.last_assigned_at = NULL (Tiebreak-Reset)

    Behaelt:
    - TaskDefinitions + ihre aktive Schedule
    - Aktuelle + zukuenftige Occurrences (laufende Verteilung bleibt heile)
    - Bewohner, Rollen, Absences, Einkauf

    Anschliessend wird ein Rebalance-Lauf gestartet, damit der Score-Reset
    sofort sichtbar wird.
    """
    require_admin_or_hauswart()
    if not user_has_role(current_user, Role.ADMIN):
        abort(403)  # Reset ist nur fuer Admins, nicht reine Hauswarte.

    today = date.today()

    karma_deleted = db.session.query(KarmaEvent).delete(synchronize_session=False)

    # Vergangene Occurrences cascadet ueber FK auf TaskAssignments + ihre
    # StepCompletions, KarmaEvents wurden eh schon entsorgt.
    past_occ_ids = [
        oid for (oid,) in db.session.query(TaskOccurrence.id).filter(
            TaskOccurrence.period_end < today
        ).all()
    ]
    past_occ_deleted = 0
    if past_occ_ids:
        past_occ_deleted = (
            db.session.query(TaskOccurrence)
            .filter(TaskOccurrence.id.in_(past_occ_ids))
            .delete(synchronize_session=False)
        )

    # last_assigned_at fuer alle User leeren.
    last_assigned_reset = (
        db.session.query(User)
        .update({User.last_assigned_at: None}, synchronize_session=False)
    )

    db.session.flush()
    # Direkt rebalancen, damit alles fair startet.
    swaps = scheduling.rebalance_open_assignments(db.session)

    _audit(
        "maintenance.reset_stats",
        current_user,
        {
            "karma_deleted": int(karma_deleted or 0),
            "past_occurrences_deleted": int(past_occ_deleted or 0),
            "users_last_assigned_reset": int(last_assigned_reset or 0),
            "rebalance_swaps": swaps,
        },
    )
    db.session.commit()
    flash(
        (
            f"Reset durchgefuehrt: {karma_deleted} Karma-Events + "
            f"{past_occ_deleted} vergangene Termine geloescht. "
            f"{swaps} Aufgabe(n) neu verteilt."
        ),
        "success",
    )
    return redirect(url_for("admin.users"))


# Use `datetime` to silence unused-import warning when ensure_joined_at is patched.
_ = datetime  # noqa: F841
_ = timezone  # noqa: F841
