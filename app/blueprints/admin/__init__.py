"""Admin-Blueprint: User-Approval, Rollen-Toggle.

Zugriff nur für User mit Rolle ``ADMIN`` oder ``HAUSWART``. Alle State-Changes
schreiben einen Eintrag in ``AuditLog``. HTMX-Endpunkte rendern Teil-Templates
für Inline-Updates; ohne HTMX wird auf die volle Listen-Seite zurückgeleitet.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

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
from sqlalchemy import case

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
from app.models.user import User
from app.services import scheduling

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
    _audit(
        "user.approve",
        user,
        {
            "previous_status": str(previous_status),
            "role_added_default": role_added,
        },
    )
    db.session.commit()
    if not _is_htmx():
        flash(f"{user.name} wurde freigeschaltet.", "success")
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
    if action == "add":
        changed = grant_role(user, role)
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
        },
    )
    db.session.commit()
    if not _is_htmx() and reassigned:
        flash(f"{reassigned} offene Aufgabe(n) neu verteilt.", "success")
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


# Use `datetime` to silence unused-import warning when ensure_joined_at is patched.
_ = datetime  # noqa: F841
_ = timezone  # noqa: F841
