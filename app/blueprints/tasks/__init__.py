"""Tasks-Blueprint: CRUD + Fairness-Zuweisung.

Routen:

* ``GET  /tasks``                — „Meine"-Ansicht (Heute-fokussiert, eigene
  Zuweisungen, 7-Tages-Streifen, Sektionen Heute/Morgen/…).
* ``GET  /tasks/alle``           — „WG"-Ansicht (Bewohner-Strip oben +
  Mo–So-Kalender).
* ``GET  /tasks/new``            — Formular für neue TaskDefinition.
* ``POST /tasks``                — Neue Definition + ggf. Occurrence anlegen.
* ``GET  /tasks/<def_id>``       — Detail-View einer Definition.
* ``POST /tasks/<occ_id>/done``  — Eigene Zuweisung erledigen (HTMX-friendly).
* ``GET  /tasks/verwalten``      — Definitions-Verwaltung (Entwürfe/Aktive).
* ``POST /tasks/<def_id>/activate`` — Entwurf aktivieren + Occurrences erzeugen.
* ``POST /tasks/aktivieren-alle`` — Alle Entwürfe auf einmal aktivieren.
* ``POST /tasks/<def_id>/deactivate`` — Definition deaktivieren.

Alle Routen erfordern einen approved User. HAUSWART/ADMIN dürfen anlegen,
aktivieren und deaktivieren; Bewohner dürfen nur Eigenes als erledigt markieren.

Aufgaben können beim Anlegen als **Entwurf** gespeichert werden
(``is_active=False``): sie erzeugen keine Occurrences und damit keine
Zuweisungen, bis sie unter „Verwalten" aktiviert werden — gedacht fürs
Vorbereiten der WG, bevor genug Bewohner da sind.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from functools import wraps

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
from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload

from app.domain.enums import (
    AssignmentStatus,
    Recurrence,
    Role,
    TaskKind,
    TaskStatus,
    UserStatus,
)
from app.domain.points import (
    DEFAULT_DIFFICULTY_POINTS,
    MAX_DIFFICULTY_POINTS,
    MIN_DIFFICULTY_POINTS,
)
from app.extensions import db
from app.models.handover import TaskHandoverOffer
from app.models.task import (
    TaskAssignment,
    TaskDefinition,
    TaskDefinitionEligibleUser,
    TaskOccurrence,
    TaskStep,
    TaskStepCompletion,
)
from app.models.user import User, UserRole
from app.services import handovers, scheduling

bp = Blueprint("tasks", __name__, template_folder="../../templates/tasks")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


RECURRENCE_LABELS: dict[Recurrence, str] = {
    Recurrence.NONE: "Einmalig",
    Recurrence.DAILY: "Täglich",
    Recurrence.WEEKLY: "Wöchentlich",
    Recurrence.BIWEEKLY: "Alle 2 Wochen",
    Recurrence.MONTHLY: "Monatlich",
    Recurrence.CUSTOM: "Eigene",
}

# Typ-Auswahl im Formular: AUFGABE (abhakbar) vs. DIENST (Zeitraum-Verantwortung).
KIND_LABELS: dict[TaskKind, str] = {
    TaskKind.AUFGABE: "Aufgabe (abhakbar)",
    TaskKind.DIENST: "Dienst (Zeitraum-Verantwortung)",
}

# 0=Montag … 6=Sonntag (passt zu date.weekday() und anchor_weekday).
WEEKDAY_LABELS: list[str] = [
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
]
WEEKDAY_LABELS_SHORT: list[str] = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _has_role(user: User, *roles: Role) -> bool:
    user_roles = {ur.role for ur in (user.roles or [])}
    return any(r in user_roles for r in roles)


def _require_approved(view):
    """User muss eingeloggt + APPROVED sein."""

    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not getattr(current_user, "is_authenticated", False):
            abort(401)
        if getattr(current_user, "status", None) != UserStatus.APPROVED:
            abort(403)
        return view(*args, **kwargs)

    return wrapper


def _require_hauswart(view):
    @wraps(view)
    @_require_approved
    def wrapper(*args, **kwargs):
        if not _has_role(current_user, Role.HAUSWART, Role.ADMIN):
            abort(403)
        return view(*args, **kwargs)

    return wrapper


def _approved_hausbewohner() -> list[User]:
    stmt = (
        select(User)
        .where(User.status == UserStatus.APPROVED)
        .join(UserRole, UserRole.user_id == User.id)
        .where(UserRole.role == Role.HAUSBEWOHNER)
        .distinct()
        .order_by(User.name)
    )
    return list(db.session.scalars(stmt))


def _parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        abort(404)


def _monday_of(d: date) -> date:
    """Montag der Woche, in der ``d`` liegt."""

    return d - timedelta(days=d.weekday())


def _week_range_label(monday: date) -> str:
    """„KW 22 · 26.05.–01.06.2026" für die Kalender-Kopfzeile."""

    sunday = monday + timedelta(days=6)
    iso = monday.isocalendar()
    return f"KW {iso.week} · {monday.strftime('%d.%m.')}–{sunday.strftime('%d.%m.%Y')}"


def _group_by_week(occurrences: list[TaskOccurrence]) -> list[dict]:
    """Occurrences nach Kalenderwoche (Montag des due_date) gruppieren."""

    buckets: dict[date, list[TaskOccurrence]] = {}
    for occ in occurrences:
        buckets.setdefault(_monday_of(occ.due_date), []).append(occ)
    return [
        {"label": _week_range_label(monday), "occurrences": buckets[monday]}
        for monday in sorted(buckets)
    ]


# ---------------------------------------------------------------------------
# Routen
# ---------------------------------------------------------------------------


def _parse_date_arg(name: str, default: date) -> date:
    raw = (request.args.get(name) or "").strip()
    if not raw:
        return default
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return default


# Reihenfolge der Sektionen in der „Meine"-Ansicht.
_SECTION_ORDER = ["today", "tomorrow", "dayafter", "thisweek", "later", "past"]


def _section_for(anchor_day: date, focus: date, monday: date, sunday: date) -> tuple[str, str]:
    """Mappt einen Periode-Anker-Tag auf eine Sektions-Bucket-Bezeichnung."""
    if anchor_day == focus:
        return ("today", "Heute")
    if anchor_day == focus + timedelta(days=1):
        return ("tomorrow", "Morgen")
    if anchor_day == focus + timedelta(days=2):
        return ("dayafter", "Übermorgen")
    if monday <= anchor_day <= sunday and anchor_day > focus + timedelta(days=2):
        return ("thisweek", "Später diese Woche")
    if anchor_day > sunday:
        return ("later", "Kommende Tage")
    return ("past", "Letzte Tage")


@bp.route("/", methods=["GET"])
@_require_approved
def index() -> str:
    """„Meine"-Ansicht: Heute-fokussierte persönliche Aufgabenliste.

    Query-Param ``?day=YYYY-MM-DD`` (default heute) wählt den Fokus-Tag.
    Oben: laufender + nächster Dienst. Mitte: 7-Tages-Streifen.
    Darunter: Sektionen Heute / Morgen / Übermorgen / Diese Woche / Kommende
    Tage / Letzte Tage, nur eigene Zuweisungen.
    """

    today = date.today()
    day = _parse_date_arg("day", today)
    monday = _monday_of(day)
    sunday = monday + timedelta(days=6)

    # Window: letzte Woche bis ~zwei Wochen voraus.
    window_start = day - timedelta(days=7)
    window_end = day + timedelta(days=13)

    stmt = (
        select(TaskOccurrence)
        .join(TaskAssignment, TaskAssignment.occurrence_id == TaskOccurrence.id)
        .where(
            TaskAssignment.user_id == current_user.id,
            TaskOccurrence.period_start <= window_end,
            TaskOccurrence.period_end >= window_start,
        )
        .options(
            selectinload(TaskOccurrence.assignments).selectinload(
                TaskAssignment.user
            ),
            selectinload(TaskOccurrence.assignments).selectinload(
                TaskAssignment.handover_offers
            ),
            joinedload(TaskOccurrence.task_definition),
        )
        .order_by(TaskOccurrence.period_start.asc())
    )
    own_occurrences = list(db.session.scalars(stmt).unique())

    buckets: dict[str, list[TaskOccurrence]] = {k: [] for k in _SECTION_ORDER}
    labels: dict[str, str] = {}
    for occ in own_occurrences:
        key, label = _section_for(occ.period_start, day, monday, sunday)
        labels[key] = label
        buckets[key].append(occ)
    sections = [
        {"key": k, "label": labels[k], "occurrences": buckets[k]}
        for k in _SECTION_ORDER
        if buckets[k]
    ]

    # Laufender + nächster Dienst.
    current_duties = scheduling.current_duties_for(db.session, current_user, today)
    next_duty = db.session.scalars(
        select(TaskOccurrence)
        .join(TaskAssignment, TaskAssignment.occurrence_id == TaskOccurrence.id)
        .join(
            TaskDefinition, TaskDefinition.id == TaskOccurrence.task_definition_id
        )
        .where(
            TaskAssignment.user_id == current_user.id,
            TaskDefinition.kind == TaskKind.DIENST,
            TaskOccurrence.period_start > today,
        )
        .options(joinedload(TaskOccurrence.task_definition))
        .order_by(TaskOccurrence.period_start.asc())
        .limit(1)
    ).first()

    # 7-Tages-Streifen rund um den Fokus-Tag.
    day_strip = [
        {
            "date": day + timedelta(days=o),
            "iso": (day + timedelta(days=o)).strftime("%Y-%m-%d"),
            "is_today": (day + timedelta(days=o)) == today,
            "is_focus": o == 0,
            "label": WEEKDAY_LABELS_SHORT[(day + timedelta(days=o)).weekday()],
            "num": (day + timedelta(days=o)).strftime("%d.%m."),
        }
        for o in range(-3, 4)
    ]

    return render_template(
        "tasks/mine.html",
        day=day,
        today=today,
        day_strip=day_strip,
        prev_day=(day - timedelta(days=7)).strftime("%Y-%m-%d"),
        next_day=(day + timedelta(days=7)).strftime("%Y-%m-%d"),
        today_str=today.strftime("%Y-%m-%d"),
        sections=sections,
        current_duties=current_duties,
        next_duty=next_duty,
        current_user_id=current_user.id,
        can_create=_has_role(current_user, Role.HAUSWART, Role.ADMIN),
    )


@bp.route("/alle", methods=["GET"])
@_require_approved
def view_all() -> str:
    """„WG"-Ansicht: Bewohner-Strip oben + Mo–So-Kalender als Timeline.

    Query-Param ``?week=YYYY-MM-DD`` (default heute).
    """

    today = date.today()
    anchor = _parse_date_arg("week", today)
    monday = _monday_of(anchor)
    sunday = monday + timedelta(days=6)

    # Occurrences laden, deren [period_start, period_end] die Woche überlappt;
    # OPEN und DONE (damit „erledigt von wem" sichtbar ist). Eager-Loading
    # kollabiert die N+1-Queries auf wenige.
    stmt = (
        select(TaskOccurrence)
        .where(
            TaskOccurrence.period_start <= sunday,
            TaskOccurrence.period_end >= monday,
        )
        .options(
            selectinload(TaskOccurrence.assignments).selectinload(
                TaskAssignment.user
            ),
            selectinload(TaskOccurrence.assignments).selectinload(
                TaskAssignment.handover_offers
            ),
            joinedload(TaskOccurrence.task_definition),
        )
        .order_by(TaskOccurrence.period_start.asc())
    )
    occurrences = list(db.session.scalars(stmt).unique())

    # 7 Tagesspalten; jede Occurrence in der Spalte ihres period_start, in die
    # sichtbare Woche geclamped (beginnt die Periode vor Montag, zeigt sie
    # unter Montag mit „läuft"-Hinweis).
    days: list[dict] = []
    for offset in range(7):
        day = monday + timedelta(days=offset)
        days.append(
            {
                "date": day,
                "weekday_label": WEEKDAY_LABELS[offset],
                "weekday_short": WEEKDAY_LABELS_SHORT[offset],
                "is_today": day == today,
                "entries": [],
            }
        )

    for occ in occurrences:
        column_day = occ.period_start
        starts_before = occ.period_start < monday
        if starts_before:
            column_day = monday
        elif occ.period_start > sunday:
            # Überlappt zwar (period_end >= monday wäre dann unmöglich), aber
            # zur Sicherheit clampen.
            column_day = sunday

        col_index = (column_day - monday).days
        col_index = max(0, min(6, col_index))

        multi_day = occ.period_end > occ.period_start
        days[col_index]["entries"].append(
            {
                "occurrence": occ,
                "starts_before": starts_before,
                "multi_day": multi_day,
            }
        )

    prev_week = (monday - timedelta(days=7)).strftime("%Y-%m-%d")
    next_week = (monday + timedelta(days=7)).strftime("%Y-%m-%d")

    # Bewohner-Strip: wer hat IN DER SICHTBAREN WOCHE welchen Dienst.
    # Wir filtern die bereits geladenen Wochen-Occurrences (keine Extra-Query)
    # auf DIENST-Typ und ordnen sie pro Bewohner zu.
    duties_by_user: dict = {}
    for occ in occurrences:
        if occ.task_definition.kind != TaskKind.DIENST:
            continue
        for a in occ.assignments:
            duties_by_user.setdefault(a.user_id, []).append(occ)
    resident_strip = []
    for u in _approved_hausbewohner():
        duties = sorted(
            duties_by_user.get(u.id, []), key=lambda o: o.period_start
        )
        resident_strip.append({"user": u, "duties": duties})

    return render_template(
        "tasks/all.html",
        days=days,
        monday=monday,
        sunday=sunday,
        week_label=_week_range_label(monday),
        prev_week=prev_week,
        next_week=next_week,
        this_week=today.strftime("%Y-%m-%d"),
        resident_strip=resident_strip,
        today=today,
        current_user_id=current_user.id,
        can_create=_has_role(current_user, Role.HAUSWART, Role.ADMIN),
    )


@bp.route("/new", methods=["GET"])
@_require_hauswart
def new() -> str:
    return render_template(
        "tasks/form.html",
        recurrence_labels=RECURRENCE_LABELS,
        kind_labels=KIND_LABELS,
        weekday_labels=WEEKDAY_LABELS,
        hausbewohner=_approved_hausbewohner(),
        form={
            "title": "",
            "description": "",
            "kind": TaskKind.AUFGABE.value,
            "difficulty_points": DEFAULT_DIFFICULTY_POINTS,
            "recurrence": Recurrence.WEEKLY.value,
            "recurrence_interval_days": "",
            "anchor_weekday": "0",
            "anchor_day_of_month": "1",
            "required_assignees": 1,
            "due_date": "",
            "due_time": "",
            "eligible_user_ids": [],
            "save_as_draft": False,
            "steps": [],
        },
        errors={},
        min_points=MIN_DIFFICULTY_POINTS,
        max_points=MAX_DIFFICULTY_POINTS,
        max_task_steps=MAX_TASK_STEPS,
    )


def _parse_int(value: str | None, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_due_time(value: str | None) -> time | None:
    """Parsed HH:MM oder HH:MM:SS in ein ``time``; sonst None."""
    if not value:
        return None
    raw = value.strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).time()
        except (TypeError, ValueError):
            continue
    return None


# Maximal so viele Schritte unterstützen wir pro Aufgabe; verhindert Form-DoS.
MAX_TASK_STEPS = 5


def _parse_steps(form) -> list[dict]:
    """Liest aus dem Form-Multipart die Schritt-Liste (Name + Tag + Uhrzeit).

    Erwartet Felder ``step_name_0..N``, ``step_day_offset_0..N``,
    ``step_time_0..N``. Schritte mit leerem Namen werden ignoriert (so kann der
    Form-Renderer einfach leere Slots zeigen).
    """
    out: list[dict] = []
    for i in range(MAX_TASK_STEPS):
        name = (form.get(f"step_name_{i}") or "").strip()
        if not name:
            continue
        day_offset = _parse_int(form.get(f"step_day_offset_{i}"), 0) or 0
        time_of_day = _parse_due_time(form.get(f"step_time_{i}"))
        out.append(
            {"name": name, "day_offset": day_offset, "time_of_day": time_of_day}
        )
    return out


def _steps_to_form(steps: list[TaskStep]) -> list[dict]:
    """Serialisiert die DB-Schritte für den Form-Renderer."""
    return [
        {
            "name": s.name,
            "day_offset": s.day_offset,
            "time": s.time_of_day.strftime("%H:%M") if s.time_of_day else "",
        }
        for s in sorted(steps, key=lambda s: s.step_order)
    ]


@bp.route("/", methods=["POST"])
@_require_hauswart
def create():
    form = request.form
    title = (form.get("title") or "").strip()
    description = (form.get("description") or "").strip() or None
    kind_raw = form.get("kind") or TaskKind.AUFGABE.value
    try:
        kind = TaskKind(kind_raw)
    except ValueError:
        kind = TaskKind.AUFGABE
    difficulty = _parse_int(form.get("difficulty_points"), DEFAULT_DIFFICULTY_POINTS)
    recurrence_raw = form.get("recurrence") or Recurrence.NONE.value
    interval = _parse_int(form.get("recurrence_interval_days"))
    anchor_weekday_raw = form.get("anchor_weekday")
    anchor_dom_raw = form.get("anchor_day_of_month")
    required = _parse_int(form.get("required_assignees"), 1) or 1
    eligible_ids = form.getlist("eligible_user_ids")
    due_date_raw = (form.get("due_date") or "").strip()
    due_time_raw = (form.get("due_time") or "").strip()
    due_time = _parse_due_time(due_time_raw)
    steps = _parse_steps(form)
    save_as_draft = bool(form.get("save_as_draft"))

    errors: dict[str, str] = {}
    if not title:
        errors["title"] = "Titel ist erforderlich."
    if difficulty is None or difficulty < MIN_DIFFICULTY_POINTS or difficulty > MAX_DIFFICULTY_POINTS:
        errors["difficulty_points"] = (
            f"Punkte müssen zwischen {MIN_DIFFICULTY_POINTS} und {MAX_DIFFICULTY_POINTS} liegen."
        )
    try:
        recurrence = Recurrence(recurrence_raw)
    except ValueError:
        recurrence = Recurrence.NONE
        errors["recurrence"] = "Unbekannte Wiederholung."

    if required < 1:
        errors["required_assignees"] = "Mindestens 1 Bewohner."

    # Anker-Felder nur für die jeweils relevante Recurrence parsen + validieren.
    anchor_weekday: int | None = None
    anchor_day_of_month: int | None = None
    if recurrence in (Recurrence.WEEKLY, Recurrence.BIWEEKLY):
        anchor_weekday = _parse_int(anchor_weekday_raw, 0)
        if anchor_weekday is None or anchor_weekday < 0 or anchor_weekday > 6:
            errors["anchor_weekday"] = "Wochentag muss zwischen Montag und Sonntag liegen."
    elif recurrence == Recurrence.MONTHLY:
        anchor_day_of_month = _parse_int(anchor_dom_raw, 1)
        if anchor_day_of_month is None or anchor_day_of_month < 1 or anchor_day_of_month > 28:
            errors["anchor_day_of_month"] = "Tag im Monat muss zwischen 1 und 28 liegen."
    elif recurrence == Recurrence.CUSTOM:
        if interval is None or interval < 1:
            errors["recurrence_interval_days"] = "Eigenes Intervall muss mindestens 1 Tag sein."

    due_date = None
    if recurrence == Recurrence.NONE:
        if not due_date_raw:
            errors["due_date"] = "Fälligkeitsdatum ist für einmalige Aufgaben Pflicht."
        else:
            try:
                due_date = datetime.strptime(due_date_raw, "%Y-%m-%d").date()
            except ValueError:
                errors["due_date"] = "Datum muss im Format YYYY-MM-DD sein."

    if errors:
        return (
            render_template(
                "tasks/form.html",
                recurrence_labels=RECURRENCE_LABELS,
                kind_labels=KIND_LABELS,
                weekday_labels=WEEKDAY_LABELS,
                hausbewohner=_approved_hausbewohner(),
                form={
                    "title": title,
                    "description": description or "",
                    "kind": kind.value,
                    "difficulty_points": difficulty or DEFAULT_DIFFICULTY_POINTS,
                    "recurrence": recurrence_raw,
                    "recurrence_interval_days": form.get("recurrence_interval_days") or "",
                    "anchor_weekday": anchor_weekday_raw if anchor_weekday_raw not in (None, "") else "0",
                    "anchor_day_of_month": anchor_dom_raw if anchor_dom_raw not in (None, "") else "1",
                    "required_assignees": required,
                    "due_date": due_date_raw,
                    "due_time": due_time_raw,
                    "eligible_user_ids": eligible_ids,
                    "save_as_draft": save_as_draft,
                    "steps": [
                        {
                            "name": s["name"],
                            "day_offset": s["day_offset"],
                            "time": s["time_of_day"].strftime("%H:%M") if s["time_of_day"] else "",
                        }
                        for s in steps
                    ],
                },
                errors=errors,
                min_points=MIN_DIFFICULTY_POINTS,
                max_points=MAX_DIFFICULTY_POINTS,
                max_task_steps=MAX_TASK_STEPS,
            ),
            400,
        )

    # Entwurf nur für wiederkehrende Aufgaben: der NONE-Pfad legt die Occurrence
    # direkt an (nicht über generate_occurrences) und bräuchte ein gespeichertes
    # due_date zum späteren Aktivieren — daher dort kein Entwurf.
    is_draft = save_as_draft and recurrence != Recurrence.NONE

    definition = TaskDefinition(
        title=title,
        description=description,
        kind=kind,
        difficulty_points=difficulty,
        recurrence=recurrence,
        recurrence_interval_days=interval if recurrence == Recurrence.CUSTOM else None,
        anchor_weekday=anchor_weekday,
        anchor_day_of_month=anchor_day_of_month,
        required_assignees=required,
        is_active=not is_draft,
        default_due_time=due_time,
        created_by_id=current_user.id,
    )
    db.session.add(definition)
    db.session.flush()

    for raw_id in eligible_ids:
        try:
            uid = uuid.UUID(raw_id)
        except (ValueError, TypeError):
            continue
        db.session.add(
            TaskDefinitionEligibleUser(task_definition_id=definition.id, user_id=uid)
        )
    # Schritte anlegen.
    for idx, s in enumerate(steps):
        db.session.add(
            TaskStep(
                task_definition_id=definition.id,
                step_order=idx,
                name=s["name"],
                day_offset=s["day_offset"],
                time_of_day=s["time_of_day"],
            )
        )
    db.session.flush()

    if recurrence == Recurrence.NONE:
        assert due_date is not None
        occurrence = TaskOccurrence(
            task_definition_id=definition.id,
            period_start=due_date,
            period_end=due_date,
            due_date=due_date,
            due_time=due_time,
            status=TaskStatus.OPEN,
        )
        db.session.add(occurrence)
        db.session.flush()
        occurrence.task_definition = definition
        scheduling.assign_occurrence(db.session, occurrence)
    elif not is_draft:
        # Entwürfe (is_active=False) werden von generate_occurrences ohnehin
        # übersprungen — wir sparen uns den Lauf und erzeugen erst beim Aktivieren.
        scheduling.generate_occurrences(db.session, lookahead_periods=2)

    db.session.commit()
    if is_draft:
        flash(
            "Als Entwurf gespeichert (noch nicht aktiv). "
            "Aktiviere ihn später unter „Verwalten“, wenn die WG bereit ist.",
            "info",
        )
        return redirect(url_for("tasks.manage"))
    flash(
        "Dienst angelegt." if kind == TaskKind.DIENST else "Aufgabe angelegt.",
        "success",
    )
    return redirect(url_for("tasks.detail", definition_id=str(definition.id)))


@bp.route("/<definition_id>", methods=["GET"])
@_require_approved
def detail(definition_id: str) -> str:
    def_uuid = _parse_uuid(definition_id)
    definition = db.session.get(TaskDefinition, def_uuid)
    if definition is None:
        abort(404)

    occurrences = sorted(
        definition.occurrences, key=lambda o: o.period_start, reverse=True
    )

    return render_template(
        "tasks/detail.html",
        definition=definition,
        occurrences=occurrences,
        recurrence_labels=RECURRENCE_LABELS,
        current_user_id=current_user.id,
        can_manage=_has_role(current_user, Role.HAUSWART, Role.ADMIN),
    )


def _load_occurrence_for_card(occ_uuid: uuid.UUID) -> TaskOccurrence | None:
    """Occurrence inkl. Assignments + Usern + Abgabe-Angeboten eager laden.

    Nach einem commit würde das Rendern des Karten-Partials sonst Lazy-Loads auf
    eine ggf. expirte Instanz auslösen (u. a. ``assignment.handover_offers``).
    """

    return db.session.scalars(
        select(TaskOccurrence)
        .where(TaskOccurrence.id == occ_uuid)
        .options(
            selectinload(TaskOccurrence.assignments).selectinload(
                TaskAssignment.user
            ),
            selectinload(TaskOccurrence.assignments).selectinload(
                TaskAssignment.handover_offers
            ),
            joinedload(TaskOccurrence.task_definition),
        )
    ).first()


def _render_done_swap(occurrence: TaskOccurrence):
    """HTMX-Swap-Partial nach mark_done (Wochen-Kalender vs. occurrence_card).

    ``today`` wird mitgegeben, damit der „Erledigt"-Button für noch nicht
    begonnene Perioden gesperrt bleibt.
    """

    today = date.today()
    if request.values.get("view") == "week":
        return render_template(
            "tasks/_components/calendar_entry.html",
            occurrence=occurrence,
            current_user_id=current_user.id,
            starts_before=occurrence.period_start < _monday_of(today),
            multi_day=occurrence.period_end > occurrence.period_start,
            today=today,
        )
    return render_template(
        "tasks/_components/occurrence_card.html",
        occurrence=occurrence,
        current_user_id=current_user.id,
        today=today,
    )


@bp.route("/<occurrence_id>/done", methods=["POST"])
@_require_approved
def mark_done(occurrence_id: str):
    occ_uuid = _parse_uuid(occurrence_id)
    occurrence = db.session.get(TaskOccurrence, occ_uuid)
    if occurrence is None:
        abort(404)

    assignment = next(
        (
            a
            for a in occurrence.assignments
            if a.user_id == current_user.id and a.status == AssignmentStatus.OPEN
        ),
        None,
    )
    if assignment is None:
        abort(403)

    # Zukünftige Perioden können nicht im Voraus abgehakt werden — erst sobald
    # die Periode begonnen hat (period_start <= heute).
    if occurrence.period_start > date.today():
        if request.headers.get("HX-Request"):
            return _render_done_swap(_load_occurrence_for_card(occ_uuid))
        flash("Diese Aufgabe ist noch nicht dran.", "info")
        return redirect(request.referrer or url_for("tasks.index"))

    scheduling.mark_done(db.session, assignment, current_user)
    # Erledigt der Anbieter die Aufgabe selbst, fällt eine offene Abgabe weg.
    handovers.close_open_offer_for(db.session, assignment, "mark_done")
    db.session.commit()

    if request.headers.get("HX-Request"):
        return _render_done_swap(_load_occurrence_for_card(occ_uuid))
    return redirect(request.referrer or url_for("tasks.index"))


@bp.route("/<occurrence_id>/step/<step_id>/done", methods=["POST"])
@_require_approved
def step_done(occurrence_id: str, step_id: str):
    """Bewohner markiert einen Schritt einer mehrteiligen Aufgabe als erledigt.

    Wenn nach diesem Tick **alle** Schritte × **alle** Assignees fertig sind,
    wird die Occurrence als Ganzes auf DONE gesetzt (Punkte werden verteilt).
    """
    occ_uuid = _parse_uuid(occurrence_id)
    step_uuid = _parse_uuid(step_id)
    occurrence = db.session.get(TaskOccurrence, occ_uuid)
    if occurrence is None:
        abort(404)
    step = db.session.get(TaskStep, step_uuid)
    if step is None or step.task_definition_id != occurrence.task_definition_id:
        abort(404)

    assignment = next(
        (
            a
            for a in occurrence.assignments
            if a.user_id == current_user.id and a.status == AssignmentStatus.OPEN
        ),
        None,
    )
    if assignment is None:
        abort(403)
    if occurrence.period_start > date.today():
        if request.headers.get("HX-Request"):
            return _render_done_swap(_load_occurrence_for_card(occ_uuid))
        flash("Diese Aufgabe ist noch nicht dran.", "info")
        return redirect(request.referrer or url_for("tasks.index"))

    already = (
        db.session.query(TaskStepCompletion)
        .filter_by(assignment_id=assignment.id, step_id=step.id)
        .first()
    )
    if already is None:
        db.session.add(
            TaskStepCompletion(assignment_id=assignment.id, step_id=step.id)
        )
        db.session.flush()

    # Alle Schritte aller Assignees fertig? → ganze Occurrence DONE.
    db.session.refresh(occurrence)
    if scheduling.is_occurrence_step_complete(occurrence):
        for a in occurrence.assignments:
            if a.status == AssignmentStatus.OPEN:
                scheduling.mark_done(db.session, a, current_user)
        handovers.close_open_offer_for(db.session, assignment, "step_done")

    db.session.commit()
    if request.headers.get("HX-Request"):
        return _render_done_swap(_load_occurrence_for_card(occ_uuid))
    return redirect(request.referrer or url_for("tasks.index"))


@bp.route("/<occurrence_id>/step/<step_id>/undo", methods=["POST"])
@_require_approved
def step_undo(occurrence_id: str, step_id: str):
    """Bewohner hebt das Abhaken eines Schritts wieder auf (nur eigener)."""
    occ_uuid = _parse_uuid(occurrence_id)
    step_uuid = _parse_uuid(step_id)
    occurrence = db.session.get(TaskOccurrence, occ_uuid)
    if occurrence is None:
        abort(404)
    step = db.session.get(TaskStep, step_uuid)
    if step is None or step.task_definition_id != occurrence.task_definition_id:
        abort(404)

    assignment = next(
        (
            a
            for a in occurrence.assignments
            if a.user_id == current_user.id
        ),
        None,
    )
    if assignment is None:
        abort(403)

    completion = (
        db.session.query(TaskStepCompletion)
        .filter_by(assignment_id=assignment.id, step_id=step.id)
        .first()
    )
    if completion is not None:
        db.session.delete(completion)
        db.session.flush()

    db.session.commit()
    if request.headers.get("HX-Request"):
        return _render_done_swap(_load_occurrence_for_card(occ_uuid))
    return redirect(request.referrer or url_for("tasks.index"))


# ---------------------------------------------------------------------------
# Aufgaben-Börse: Abgeben → Übernehmen
# ---------------------------------------------------------------------------


@bp.route("/boerse", methods=["GET"])
@_require_approved
def boerse() -> str:
    """„Börse"-Ansicht: alle offenen Abgaben, von jedem übernehmbar."""

    offers = handovers.open_offers(db.session)
    return render_template(
        "tasks/boerse.html",
        offers=offers,
        today=date.today(),
        current_user_id=current_user.id,
        can_create=_has_role(current_user, Role.HAUSWART, Role.ADMIN),
    )


@bp.route("/<occurrence_id>/abgeben", methods=["POST"])
@_require_approved
def offer(occurrence_id: str):
    """Eigene offene Zuweisung an die Börse abgeben (HTMX-friendly)."""

    occ_uuid = _parse_uuid(occurrence_id)
    occurrence = db.session.get(TaskOccurrence, occ_uuid)
    if occurrence is None:
        abort(404)

    assignment = next(
        (
            a
            for a in occurrence.assignments
            if a.user_id == current_user.id and a.status == AssignmentStatus.OPEN
        ),
        None,
    )
    if assignment is None:
        abort(403)

    try:
        handovers.offer_assignment(
            db.session, assignment, current_user, note=request.form.get("note")
        )
        db.session.commit()
    except handovers.HandoverError as exc:
        db.session.rollback()
        flash(str(exc), "warning")

    if request.headers.get("HX-Request"):
        return _render_done_swap(_load_occurrence_for_card(occ_uuid))
    return redirect(request.referrer or url_for("tasks.index"))


@bp.route("/abgaben/<offer_id>/zurueckziehen", methods=["POST"])
@_require_approved
def withdraw_offer(offer_id: str):
    """Anbieter zieht eine offene Abgabe zurück (aus Karte oder Börse)."""

    off_uuid = _parse_uuid(offer_id)
    offer_row = db.session.get(TaskHandoverOffer, off_uuid)
    if offer_row is None:
        abort(404)
    occ_uuid = offer_row.assignment.occurrence_id

    try:
        handovers.cancel_offer(db.session, offer_row, current_user)
        db.session.commit()
    except handovers.HandoverError as exc:
        db.session.rollback()
        flash(str(exc), "warning")

    if request.headers.get("HX-Request"):
        if request.values.get("from") == "board":
            return render_template(
                "tasks/_components/_boerse_offer.html",
                offer=offer_row,
                today=date.today(),
                current_user_id=current_user.id,
                removed=True,
            )
        return _render_done_swap(_load_occurrence_for_card(occ_uuid))
    return redirect(request.referrer or url_for("tasks.boerse"))


@bp.route("/abgaben/<offer_id>/uebernehmen", methods=["POST"])
@_require_approved
def claim_offer(offer_id: str):
    """Ein anderer Bewohner übernimmt eine Abgabe von der Börse."""

    off_uuid = _parse_uuid(offer_id)
    # FOR UPDATE sperrt die Zeile unter Postgres gegen parallele Übernahmen;
    # SQLite ignoriert es harmlos.
    offer_row = db.session.get(TaskHandoverOffer, off_uuid, with_for_update=True)
    if offer_row is None:
        abort(404)

    claimed = False
    try:
        handovers.claim_offer(db.session, offer_row, current_user)
        db.session.commit()
        claimed = True
    except handovers.HandoverError as exc:
        db.session.rollback()
        flash(str(exc), "warning")

    if request.headers.get("HX-Request"):
        return render_template(
            "tasks/_components/_boerse_offer.html",
            offer=offer_row,
            today=date.today(),
            current_user_id=current_user.id,
            claimed=claimed,
        )
    if claimed:
        flash("Aufgabe übernommen.", "success")
    return redirect(url_for("tasks.boerse"))


@bp.route("/<definition_id>/deactivate", methods=["POST"])
@_require_hauswart
def deactivate(definition_id: str):
    def_uuid = _parse_uuid(definition_id)
    definition = db.session.get(TaskDefinition, def_uuid)
    if definition is None:
        abort(404)
    definition.is_active = False
    db.session.commit()
    flash("Aufgabe deaktiviert.", "info")
    return redirect(request.referrer or url_for("tasks.manage"))


@bp.route("/<definition_id>/edit", methods=["GET"])
@_require_hauswart
def edit(definition_id: str):
    """Bearbeitungs-Formular: vorbefüllt mit den aktuellen Werten."""
    def_uuid = _parse_uuid(definition_id)
    definition = db.session.get(TaskDefinition, def_uuid)
    if definition is None:
        abort(404)

    eligible_ids = [str(e.user_id) for e in (definition.eligible_users or [])]
    return render_template(
        "tasks/form.html",
        recurrence_labels=RECURRENCE_LABELS,
        kind_labels=KIND_LABELS,
        weekday_labels=WEEKDAY_LABELS,
        hausbewohner=_approved_hausbewohner(),
        form={
            "title": definition.title,
            "description": definition.description or "",
            "kind": definition.kind.value,
            "difficulty_points": definition.difficulty_points,
            "recurrence": definition.recurrence.value,
            "recurrence_interval_days": definition.recurrence_interval_days or "",
            "anchor_weekday": str(definition.anchor_weekday) if definition.anchor_weekday is not None else "0",
            "anchor_day_of_month": str(definition.anchor_day_of_month) if definition.anchor_day_of_month is not None else "1",
            "required_assignees": definition.required_assignees,
            "due_date": (
                definition.occurrences[0].due_date.strftime("%Y-%m-%d")
                if (definition.recurrence == Recurrence.NONE and definition.occurrences)
                else ""
            ),
            "due_time": (
                definition.default_due_time.strftime("%H:%M")
                if definition.default_due_time
                else ""
            ),
            "eligible_user_ids": eligible_ids,
            "save_as_draft": False,
            "steps": _steps_to_form(definition.steps or []),
        },
        errors={},
        min_points=MIN_DIFFICULTY_POINTS,
        max_points=MAX_DIFFICULTY_POINTS,
        max_task_steps=MAX_TASK_STEPS,
        form_action=url_for("tasks.update", definition_id=str(definition.id)),
        page_title=f"„{definition.title}" + "“ bearbeiten",
        page_subtitle="Änderungen wirken sich auf alle künftigen Perioden aus.",
        sub_nav_active="manage",
    )


@bp.route("/<definition_id>/edit", methods=["POST"])
@_require_hauswart
def update(definition_id: str):
    """Aktualisiert eine TaskDefinition.

    Spielregel: bei Änderung der Wiederholungs-Logik (Recurrence-Art, Anker,
    Custom-Intervall, Required-Assignees) werden ZUKÜNFTIGE OPEN-Occurrences
    weggeworfen und mit den neuen Werten neu generiert. Vergangenheit + bereits
    erledigte Termine bleiben unangetastet.
    """
    def_uuid = _parse_uuid(definition_id)
    definition = db.session.get(TaskDefinition, def_uuid)
    if definition is None:
        abort(404)

    form = request.form
    title = (form.get("title") or "").strip()
    description = (form.get("description") or "").strip() or None
    kind_raw = form.get("kind") or TaskKind.AUFGABE.value
    try:
        kind = TaskKind(kind_raw)
    except ValueError:
        kind = TaskKind.AUFGABE
    difficulty = _parse_int(form.get("difficulty_points"), DEFAULT_DIFFICULTY_POINTS)
    recurrence_raw = form.get("recurrence") or Recurrence.NONE.value
    interval = _parse_int(form.get("recurrence_interval_days"))
    anchor_weekday_raw = form.get("anchor_weekday")
    anchor_dom_raw = form.get("anchor_day_of_month")
    required = _parse_int(form.get("required_assignees"), 1) or 1
    eligible_ids = form.getlist("eligible_user_ids")
    due_time_raw = (form.get("due_time") or "").strip()
    due_time = _parse_due_time(due_time_raw)
    steps = _parse_steps(form)

    errors: dict[str, str] = {}
    if not title:
        errors["title"] = "Titel ist erforderlich."
    if difficulty is None or difficulty < MIN_DIFFICULTY_POINTS or difficulty > MAX_DIFFICULTY_POINTS:
        errors["difficulty_points"] = (
            f"Punkte müssen zwischen {MIN_DIFFICULTY_POINTS} und {MAX_DIFFICULTY_POINTS} liegen."
        )
    try:
        recurrence = Recurrence(recurrence_raw)
    except ValueError:
        recurrence = Recurrence.NONE
        errors["recurrence"] = "Unbekannte Wiederholung."
    if required < 1:
        errors["required_assignees"] = "Mindestens 1 Bewohner."

    anchor_weekday: int | None = None
    anchor_day_of_month: int | None = None
    if recurrence in (Recurrence.WEEKLY, Recurrence.BIWEEKLY):
        anchor_weekday = _parse_int(anchor_weekday_raw, 0)
        if anchor_weekday is None or anchor_weekday < 0 or anchor_weekday > 6:
            errors["anchor_weekday"] = "Wochentag muss zwischen Montag und Sonntag liegen."
    elif recurrence == Recurrence.MONTHLY:
        anchor_day_of_month = _parse_int(anchor_dom_raw, 1)
        if anchor_day_of_month is None or anchor_day_of_month < 1 or anchor_day_of_month > 28:
            errors["anchor_day_of_month"] = "Tag im Monat muss zwischen 1 und 28 liegen."
    elif recurrence == Recurrence.CUSTOM:
        if interval is None or interval < 1:
            errors["recurrence_interval_days"] = "Eigenes Intervall muss mindestens 1 Tag sein."

    # NONE (einmalig) macht beim Edit keinen Sinn — wir blockieren das Wechseln
    # zwischen NONE und wiederkehrend bewusst, weil die Occurrence-Logik dort
    # ganz anders ist.
    if recurrence != definition.recurrence and (
        recurrence == Recurrence.NONE or definition.recurrence == Recurrence.NONE
    ):
        errors["recurrence"] = (
            "Wechsel zwischen einmaliger und wiederkehrender Aufgabe nicht "
            "möglich — bitte neue Aufgabe anlegen."
        )

    if errors:
        return (
            render_template(
                "tasks/form.html",
                recurrence_labels=RECURRENCE_LABELS,
                kind_labels=KIND_LABELS,
                weekday_labels=WEEKDAY_LABELS,
                hausbewohner=_approved_hausbewohner(),
                form={
                    "title": title,
                    "description": description or "",
                    "kind": kind.value,
                    "difficulty_points": difficulty or DEFAULT_DIFFICULTY_POINTS,
                    "recurrence": recurrence_raw,
                    "recurrence_interval_days": form.get("recurrence_interval_days") or "",
                    "anchor_weekday": anchor_weekday_raw if anchor_weekday_raw not in (None, "") else "0",
                    "anchor_day_of_month": anchor_dom_raw if anchor_dom_raw not in (None, "") else "1",
                    "required_assignees": required,
                    "due_date": "",
                    "due_time": due_time_raw,
                    "eligible_user_ids": eligible_ids,
                    "save_as_draft": False,
                    "steps": [
                        {
                            "name": s["name"],
                            "day_offset": s["day_offset"],
                            "time": s["time_of_day"].strftime("%H:%M") if s["time_of_day"] else "",
                        }
                        for s in steps
                    ],
                },
                errors=errors,
                min_points=MIN_DIFFICULTY_POINTS,
                max_points=MAX_DIFFICULTY_POINTS,
                max_task_steps=MAX_TASK_STEPS,
                form_action=url_for("tasks.update", definition_id=str(definition.id)),
                page_title=f"„{definition.title}" + "“ bearbeiten",
                page_subtitle="Änderungen wirken sich auf alle künftigen Perioden aus.",
                sub_nav_active="manage",
            ),
            400,
        )

    # Schauen ob sich die Verteilungs-Logik geändert hat — dann regenerieren.
    schedule_changed = (
        recurrence != definition.recurrence
        or anchor_weekday != definition.anchor_weekday
        or anchor_day_of_month != definition.anchor_day_of_month
        or (interval if recurrence == Recurrence.CUSTOM else None)
            != definition.recurrence_interval_days
        or required != definition.required_assignees
    )

    # Felder aktualisieren.
    due_time_changed = due_time != definition.default_due_time
    definition.title = title
    definition.description = description
    definition.kind = kind
    definition.difficulty_points = difficulty
    definition.recurrence = recurrence
    definition.recurrence_interval_days = (
        interval if recurrence == Recurrence.CUSTOM else None
    )
    definition.anchor_weekday = anchor_weekday
    definition.anchor_day_of_month = anchor_day_of_month
    definition.required_assignees = required
    definition.default_due_time = due_time

    # Eligibility-Liste neu setzen.
    db.session.query(TaskDefinitionEligibleUser).filter_by(
        task_definition_id=definition.id
    ).delete()
    for raw_id in eligible_ids:
        try:
            uid = uuid.UUID(raw_id)
        except (ValueError, TypeError):
            continue
        db.session.add(
            TaskDefinitionEligibleUser(task_definition_id=definition.id, user_id=uid)
        )

    # Steps komplett neu setzen (Cascade räumt alte + ihre Completions auf).
    for old_step in list(definition.steps or []):
        db.session.delete(old_step)
    db.session.flush()
    for idx, s in enumerate(steps):
        db.session.add(
            TaskStep(
                task_definition_id=definition.id,
                step_order=idx,
                name=s["name"],
                day_offset=s["day_offset"],
                time_of_day=s["time_of_day"],
            )
        )

    # Default-Uhrzeit-Änderung an künftige OPEN-Occurrences durchreichen,
    # damit der Bewohner auf seiner Karte gleich die neue Zeit sieht.
    if due_time_changed:
        today_d = date.today()
        for occ in definition.occurrences:
            if occ.status == TaskStatus.OPEN and occ.period_start >= today_d:
                occ.due_time = due_time

    # Schedule-Änderung -> alle OPEN-Occurrences in der Zukunft wegwerfen,
    # dann neu generieren. Vergangene + DONE bleiben in Ruhe.
    if schedule_changed and recurrence != Recurrence.NONE:
        today = date.today()
        future_open = [
            occ for occ in definition.occurrences
            if occ.status == TaskStatus.OPEN and occ.period_start > today
        ]
        for occ in future_open:
            db.session.delete(occ)
        db.session.flush()
        scheduling.generate_occurrences(db.session, lookahead_periods=2)

    db.session.commit()
    flash("Aufgabe aktualisiert.", "success")
    return redirect(url_for("tasks.detail", definition_id=str(definition.id)))


@bp.route("/<definition_id>/delete", methods=["POST"])
@_require_hauswart
def delete(definition_id: str):
    """Hard-Delete einer Aufgabe inkl. ihrer Occurrences/Assignments.

    KarmaEvents bleiben erhalten (occurrence_id wird per ON DELETE SET NULL
    auf NULL gesetzt), damit der Score nicht plötzlich umspringt.
    """
    def_uuid = _parse_uuid(definition_id)
    definition = db.session.get(TaskDefinition, def_uuid)
    if definition is None:
        abort(404)

    title = definition.title
    # Cascade in den Modellen entfernt Occurrences -> Assignments -> Handover-Offers
    # zuverlässig in der richtigen Reihenfolge.
    db.session.delete(definition)
    db.session.commit()
    flash(f"„{title}“ gelöscht.", "info")
    return redirect(url_for("tasks.manage"))


@bp.route("/verwalten", methods=["GET"])
@_require_hauswart
def manage() -> str:
    """Definitions-Verwaltung: Entwürfe aktivieren / Aktive deaktivieren.

    Entwürfe (``is_active=False``) haben keine Occurrences und tauchen daher in
    Kalender-/„Meine"-Ansicht nicht auf — diese Seite ist ihr Zuhause.
    """

    definitions = list(
        db.session.scalars(
            select(TaskDefinition)
            .options(selectinload(TaskDefinition.occurrences))
            .order_by(TaskDefinition.title.asc())
        )
    )
    return render_template(
        "tasks/manage.html",
        drafts=[d for d in definitions if not d.is_active],
        active=[d for d in definitions if d.is_active],
        recurrence_labels=RECURRENCE_LABELS,
        can_create=True,
    )


@bp.route("/<definition_id>/activate", methods=["POST"])
@_require_hauswart
def activate(definition_id: str):
    def_uuid = _parse_uuid(definition_id)
    definition = db.session.get(TaskDefinition, def_uuid)
    if definition is None:
        abort(404)
    definition.is_active = True
    db.session.flush()
    # Erzeugt + verteilt die Occurrences dieser Definition auf die JETZT
    # vorhandenen Bewohner (idempotent: bestehende Perioden bleiben unberührt).
    scheduling.generate_occurrences(db.session, lookahead_periods=2)
    db.session.commit()
    flash(f"„{definition.title}“ aktiviert und verteilt.", "success")
    return redirect(url_for("tasks.manage"))


@bp.route("/aktivieren-alle", methods=["POST"])
@_require_hauswart
def activate_all():
    drafts = list(
        db.session.scalars(
            select(TaskDefinition).where(TaskDefinition.is_active.is_(False))
        )
    )
    for definition in drafts:
        definition.is_active = True
    db.session.flush()
    if drafts:
        scheduling.generate_occurrences(db.session, lookahead_periods=2)
    db.session.commit()
    flash(f"{len(drafts)} Aufgabe(n) aktiviert und verteilt.", "success")
    return redirect(url_for("tasks.manage"))
