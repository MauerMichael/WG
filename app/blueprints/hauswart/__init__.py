"""Hauswart-Blueprint: Review-Queue + Personen-Ansicht.

Zugriff nur für User mit Rolle ``ADMIN`` oder ``HAUSWART`` (mirror Admin via
:func:`require_admin_or_hauswart`). Der Hauswart prüft erledigte Dienste und
überfällig-unbeanspruchte Aufgaben: **Genehmigen** / **Ablehnen (mit Notiz)** /
**Für ihn abhaken**. Die State-Changes delegieren komplett an
``app.services.scheduling`` — hier wird nichts reimplementiert.

HTMX-Endpunkte rendern die Zeile als Partial (``_review_row.html``); ohne HTMX
wird auf die Queue zurückgeleitet.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict

from flask import (
    Blueprint,
    abort,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from app.blueprints.auth import require_admin_or_hauswart, user_has_any_role
from app.domain.enums import Recurrence, ReviewStatus, Role, TaskKind, UserStatus
from app.extensions import db
from app.models.task import TaskAssignment, TaskDefinition
from app.models.user import User, UserRole
from app.services.scheduling import (
    effective_scores_for,
    excuse_assignment,
    hauswart_mark_done,
    review_archive,
    review_assignment,
    review_queue,
    score_assignment,
    user_review_items,
    user_task_stats,
)

bp = Blueprint("hauswart", __name__, template_folder="../../templates/hauswart")


# Macht ``Role`` und ``user_has_any_role`` in JEDEM Template verfügbar — die
# Nav-Leiste in ``base.html`` blendet damit die Hauswart-/Admin-Tabs nur für
# berechtigte Rollen ein. (App-weiter Processor, vom Blueprint registriert.)
@bp.app_context_processor
def _inject_role_helpers() -> dict:
    return {"Role": Role, "user_has_any_role": user_has_any_role}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _get_assignment_or_404(assignment_id) -> TaskAssignment:
    assignment = db.session.get(TaskAssignment, assignment_id)
    if assignment is None:
        abort(404)
    return assignment


def _approved_residents() -> list[User]:
    """Alle freigeschalteten HAUSBEWOHNER (für die Personen-Links)."""
    return (
        db.session.query(User)
        .join(UserRole, UserRole.user_id == User.id)
        .filter(
            User.status == UserStatus.APPROVED,
            UserRole.role == Role.HAUSBEWOHNER,
        )
        .order_by(User.name.asc())
        .distinct()
        .all()
    )


# Server-seitige Spiegelung von ``components/ui.html::task_type_key``. Wird nur
# für die Sortierreihenfolge gebraucht — die Templates rendern weiter über das
# Makro. Bitte bei Änderungen am Makro hier mitziehen.
_TYPE_ORDER = {"dienst": 0, "event": 1, "wiederholend": 2, "einmalig": 3}


def _task_type_key(definition: TaskDefinition) -> str:
    """Klassifiziert eine ``TaskDefinition`` in eine der 4 Typ-Klassen."""
    if definition.kind == TaskKind.DIENST:
        return "dienst"
    if definition.recurrence == Recurrence.NONE:
        if (definition.required_assignees or 1) > 1:
            return "event"
        return "einmalig"
    return "wiederholend"


def _row_response(assignment: TaskAssignment):
    """Antwort für HTMX-Mutationen: Zeile als Partial, sonst zurück zur Queue."""
    if _is_htmx():
        return render_template("hauswart/_review_row.html", assignment=assignment)
    return redirect(url_for("hauswart.index"))


def _parse_user_id(raw: str | None) -> uuid.UUID | None:
    """Tolerantes Parsen des Filter-Param: ungültige UUIDs werden ignoriert."""
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Routen
# ---------------------------------------------------------------------------


@bp.route("/")
@login_required
def index():
    require_admin_or_hauswart()
    items = review_queue(db.session)

    # Filter: nur Items eines bestimmten Bewohners (falls Param gültig).
    selected_user_id = _parse_user_id(request.args.get("user_id"))
    if selected_user_id is not None:
        items = [item for item in items if item.user_id == selected_user_id]

    # Sort: Default 'date' (Service-Reihenfolge), 'type' = nach Typ-Klasse.
    selected_sort = request.args.get("sort", "date")
    if selected_sort not in ("date", "type"):
        selected_sort = "date"
    if selected_sort == "type":
        # Service liefert bereits period_end asc — stable sort erhält das als Tiebreak.
        items = sorted(
            items,
            key=lambda a: _TYPE_ORDER.get(
                _task_type_key(a.occurrence.task_definition), 99
            ),
        )

    # Gruppieren pro Bewohner; gewählte Reihenfolge bewahren.
    groups: OrderedDict[User, list[TaskAssignment]] = OrderedDict()
    for item in items:
        groups.setdefault(item.user, []).append(item)

    # Zuverlässigkeits-Badge je angezeigtem Bewohner.
    stats = {user.id: user_task_stats(db.session, user) for user in groups}

    residents = _approved_residents()

    # Verteil-Score-Tabelle: batched Score je Bewohner (eine GROUP-BY-Query
    # statt N Einzel-Queries) + all-time-Zuverlässigkeit. Höchster Score zuerst
    # (Leaderboard), Tiebreak Name. Spiegelt direkt die Fairness-Verteilung.
    score_map = effective_scores_for(db.session, residents)
    score_rows = [
        {
            "user": resident,
            "score": score_map.get(resident.id, 0.0),
            "stats": user_task_stats(db.session, resident),
        }
        for resident in residents
    ]
    score_rows.sort(key=lambda row: (-row["score"], (row["user"].name or "").lower()))
    avg_score = (
        sum(row["score"] for row in score_rows) / len(score_rows)
        if score_rows
        else None
    )

    return render_template(
        "hauswart/index.html",
        groups=groups,
        stats=stats,
        residents=residents,
        score_rows=score_rows,
        avg_score=avg_score,
        selected_user_id=selected_user_id,
        selected_sort=selected_sort,
    )


@bp.route("/archiv")
@login_required
def archive():
    """Archiv der bewerteten Aufgaben (APPROVED / REJECTED / EXCUSED).

    Filter: Bewohner, Status, Datums-Range. Wenn keine Filter gesetzt,
    Default-Fenster letzte 90 Tage.
    """
    require_admin_or_hauswart()

    from datetime import date as _date

    selected_user_id = _parse_user_id(request.args.get("user_id"))
    raw_status = (request.args.get("status") or "").strip().upper()
    selected_status = None
    if raw_status in {"APPROVED", "REJECTED", "EXCUSED"}:
        selected_status = ReviewStatus[raw_status]

    def _parse_date(raw):
        try:
            return _date.fromisoformat(raw) if raw else None
        except (TypeError, ValueError):
            return None

    from_date = _parse_date(request.args.get("from"))
    to_date = _parse_date(request.args.get("to"))

    items = review_archive(
        db.session,
        user_id=selected_user_id,
        from_date=from_date,
        to_date=to_date,
        status=selected_status,
    )

    residents = _approved_residents()

    return render_template(
        "hauswart/archive.html",
        items=items,
        residents=residents,
        selected_user_id=selected_user_id,
        selected_status=raw_status,
        from_date=from_date.isoformat() if from_date else "",
        to_date=to_date.isoformat() if to_date else "",
    )


@bp.route("/user/<uuid:user_id>")
@login_required
def user_detail(user_id):
    require_admin_or_hauswart()
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)

    items = user_review_items(db.session, user)
    stats = user_task_stats(db.session, user)

    return render_template(
        "hauswart/user_detail.html",
        user=user,
        items=items,
        stats=stats,
    )


@bp.route("/<uuid:assignment_id>/approve", methods=["POST"])
@login_required
def approve(assignment_id):
    require_admin_or_hauswart()
    assignment = _get_assignment_or_404(assignment_id)
    review_assignment(db.session, assignment, current_user, approved=True)
    db.session.commit()
    return _row_response(assignment)


@bp.route("/<uuid:assignment_id>/score", methods=["POST"])
@login_required
def score(assignment_id):
    """Hauswart vergibt Teilpunkte (0..max). Auto-Strafe = Differenz."""
    require_admin_or_hauswart()
    assignment = _get_assignment_or_404(assignment_id)
    try:
        points = int(request.form.get("points_earned", "").strip())
    except (TypeError, ValueError):
        abort(400)
    note = (request.form.get("note") or "").strip() or None
    score_assignment(
        db.session, assignment, current_user, points_earned=points, note=note
    )
    db.session.commit()
    return _row_response(assignment)


@bp.route("/<uuid:assignment_id>/reject", methods=["POST"])
@login_required
def reject(assignment_id):
    require_admin_or_hauswart()
    assignment = _get_assignment_or_404(assignment_id)
    note = (request.form.get("note") or "").strip() or None
    review_assignment(db.session, assignment, current_user, approved=False, note=note)
    db.session.commit()
    return _row_response(assignment)


@bp.route("/<uuid:assignment_id>/mark-done", methods=["POST"])
@login_required
def mark_done(assignment_id):
    require_admin_or_hauswart()
    assignment = _get_assignment_or_404(assignment_id)
    hauswart_mark_done(db.session, assignment, current_user)
    db.session.commit()
    return _row_response(assignment)


@bp.route("/<uuid:assignment_id>/excuse", methods=["POST"])
@login_required
def excuse(assignment_id):
    require_admin_or_hauswart()
    assignment = _get_assignment_or_404(assignment_id)
    note = (request.form.get("note") or "").strip() or None
    excuse_assignment(db.session, assignment, current_user, note=note)
    db.session.commit()
    return _row_response(assignment)
