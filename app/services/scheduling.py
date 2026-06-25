"""Fairness-Algorithmus und Occurrence-Generierung.

Reine Funktionen, kein Flask-Request-Kontext. Jede Funktion nimmt eine
SQLAlchemy-Session als ersten Parameter — so sind die Funktionen sowohl im
Web-Request, im Cron-Skript als auch im Test direkt nutzbar.

Hauptaufgaben:

* ``effective_score`` — tenure-normalisierter 90-Tage-Punkte-Score.
* ``eligible_candidates`` — filtert Kandidaten nach Rolle/Eligibility/Absence.
* ``assign_occurrence`` — wählt Top-N Kandidaten und erzeugt die
  ``TaskAssignment``-Zeilen.
* ``generate_occurrences`` — materialisiert für jede aktive Definition die
  nächsten N Perioden (idempotent über ``UNIQUE(task_definition_id,
  period_start)``).
* ``reassign_open_overlap`` — wird vom Absences-Blueprint aufgerufen, wenn
  eine neue Abwesenheit OPEN-Occurrences überlappt.
* ``mark_done`` — schreibt Punkte gut und markiert Occurrence ggf. als fertig.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import uuid

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.orm import joinedload, selectinload

from app.domain.enums import (
    AssignmentStatus,
    KarmaKind,
    Recurrence,
    ReviewStatus,
    Role,
    TaskKind,
    TaskStatus,
    UserStatus,
)
from app.domain.points import (
    DEFAULT_LOOKAHEAD_PERIODS,
    HONOR_LIFESPAN_DAYS,
    MAX_OPEN_ASSIGNMENTS_PER_USER,
    MIN_NOTICE_DAYS,
    PENALTY_LIFESPAN_DAYS,
    SCORE_WINDOW_DAYS,
    SOFT_CAP_OPEN_ASSIGNMENTS,
)
from app.models.absence import Absence
from app.models.karma import KarmaEvent
from app.models.task import (
    TaskAssignment,
    TaskDefinition,
    TaskDefinitionEligibleUser,
    TaskOccurrence,
)
from app.models.user import User, UserRole

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """Wrapper, damit Tests die Zeit monkeypatchen können."""

    return datetime.now(timezone.utc)


def _as_aware(dt: datetime) -> datetime:
    """SQLAlchemy liefert je nach Backend naive datetimes — vereinheitlichen."""

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _normalize_score(total: float, joined_at: datetime | None, today: date) -> float:
    """Tenure-Normalisierung: ``total / max(days_active, 90) * 90``."""

    if joined_at is None:
        days_active = 0
    else:
        days_active = (today - _as_aware(joined_at).date()).days
    days_active = max(days_active, SCORE_WINDOW_DAYS)
    return (total / days_active) * SCORE_WINDOW_DAYS


def effective_scores_for(
    session: "Session", users: list[User]
) -> dict[uuid.UUID, float]:
    """Batch-Variante von ``effective_score``: roher Score je User in 2 Queries.

    Der rohe Score ist:

        Task-Punkte (DONE, 90-Tage-Fenster)
        + HONOR-Karma (≤ 80 Tage alt)
        − PENALTY-Karma (≤ 40 Tage alt)

    Query 1 summiert die DONE-``points_earned`` (wie bisher). Query 2 summiert
    pro User die HONOR- und PENALTY-Events je in ihrem eigenen Decay-Fenster
    (ein ``GROUP BY`` mit zwei bedingten Summen). Die Tenure-Normalisierung
    (Floor/Faktor ``SCORE_WINDOW_DAYS``) passiert danach in Python.

    Liefert für jeden übergebenen User einen Eintrag (0.0, falls nichts zählt).
    Ein negativer Score (Penalty überwiegt) ist erlaubt und schiebt die Person
    in der Sortierung nach vorne (= bekommt öfter Aufgaben).
    """

    if not users:
        return {}

    now = _utcnow()
    today = now.date()
    task_window_start = now - timedelta(days=SCORE_WINDOW_DAYS)
    honor_window_start = now - timedelta(days=HONOR_LIFESPAN_DAYS)
    penalty_window_start = now - timedelta(days=PENALTY_LIFESPAN_DAYS)

    user_ids = [u.id for u in users]

    task_stmt = (
        select(
            TaskAssignment.user_id,
            func.sum(TaskAssignment.points_earned),
        )
        .where(
            TaskAssignment.status == AssignmentStatus.DONE,
            TaskAssignment.completed_at.is_not(None),
            TaskAssignment.completed_at >= task_window_start,
            TaskAssignment.user_id.in_(user_ids),
        )
        .group_by(TaskAssignment.user_id)
    )
    totals: dict[uuid.UUID, float] = {
        user_id: float(total or 0) for user_id, total in session.execute(task_stmt)
    }

    karma_stmt = (
        select(
            KarmaEvent.user_id,
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                KarmaEvent.kind == KarmaKind.HONOR,
                                KarmaEvent.occurred_at >= honor_window_start,
                            ),
                            KarmaEvent.points,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                KarmaEvent.kind == KarmaKind.PENALTY,
                                KarmaEvent.occurred_at >= penalty_window_start,
                            ),
                            KarmaEvent.points,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
        )
        .where(KarmaEvent.user_id.in_(user_ids))
        .group_by(KarmaEvent.user_id)
    )
    for user_id, honor, penalty in session.execute(karma_stmt):
        totals[user_id] = totals.get(user_id, 0.0) + float(honor or 0) - float(penalty or 0)

    return {
        u.id: _normalize_score(totals.get(u.id, 0.0), u.joined_at, today)
        for u in users
    }


def effective_score(session: "Session", user: User) -> float:
    """Tenure-normalisierter Score über das rollende Fenster.

    ``sum(points_earned) / max(days_active, 90) * 90``

    Ein neuer Bewohner (``joined_at`` heute) bekommt also seinen Score
    geteilt durch 90 — nicht durch 1 — und wird damit nicht ungewollt nach
    oben katapultiert, falls er an Tag 1 eine Aufgabe erledigt.

    Backward-compat-Wrapper: die Logik lebt jetzt in ``effective_scores_for``.
    """

    return effective_scores_for(session, [user]).get(user.id, 0.0)


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def _approved_hausbewohner(session: "Session") -> list[User]:
    """Alle approved User mit Rolle HAUSBEWOHNER."""

    stmt = (
        select(User)
        .where(User.status == UserStatus.APPROVED)
        .where(
            exists().where(
                and_(UserRole.user_id == User.id, UserRole.role == Role.HAUSBEWOHNER)
            )
        )
    )
    return list(session.scalars(stmt))


def _absent_user_ids(
    session: "Session", period_start: date, period_end: date
) -> set:
    """User-IDs, die im Zeitraum eine Absence haben (inkl. End-Tag)."""

    stmt = select(Absence.user_id).where(
        Absence.start_date <= period_end,
        Absence.end_date >= period_start,
    )
    return {row for row in session.scalars(stmt)}


def eligible_candidates(
    session: "Session",
    task_definition: TaskDefinition,
    period_start: date,
    period_end: date,
) -> list[User]:
    """Approved HAUSBEWOHNER, gefiltert nach Eligibility und Absence.

    * Wenn ``task_definition.eligible_users`` nicht-leer ist, dann nur diese
      User.
    * User mit einer Absence, die ``[period_start, period_end]`` überlappt,
      werden ausgefiltert.
    """

    base = _approved_hausbewohner(session)

    eligible_ids = {
        link.user_id for link in (task_definition.eligible_users or [])
    }
    if eligible_ids:
        base = [u for u in base if u.id in eligible_ids]

    absent = _absent_user_ids(session, period_start, period_end)
    return [u for u in base if u.id not in absent]


def _absent_eligible_candidates(
    session: "Session",
    task_definition: TaskDefinition,
    period_start: date,
    period_end: date,
) -> list[User]:
    """Wie ``eligible_candidates``, aber NUR die abwesenden Kandidaten.

    Fallback, wenn nicht genug anwesende User für die Pflicht-N existieren.
    """

    base = _approved_hausbewohner(session)
    eligible_ids = {
        link.user_id for link in (task_definition.eligible_users or [])
    }
    if eligible_ids:
        base = [u for u in base if u.id in eligible_ids]

    absent = _absent_user_ids(session, period_start, period_end)
    return [u for u in base if u.id in absent]


def _sort_key(user: User, score: float) -> tuple:
    """Sortierung: Score asc, last_assigned_at asc (nulls first), user_id asc.

    ``score`` wird vom Aufrufer EINMAL vorberechnet (``effective_scores_for``)
    und hier durchgereicht — kein Per-Vergleich-Query mehr.
    """

    last = user.last_assigned_at
    # "nulls first" = noch nie zugewiesen kommt zuerst -> sortier-Wert kleiner
    last_key = (1, _as_aware(last)) if last is not None else (0, datetime.min.replace(tzinfo=timezone.utc))
    return (score, last_key, str(user.id))


def _sorted_by_fairness(session: "Session", users: list[User]) -> list[User]:
    """Sortiert ``users`` nach Fairness; Score-Map einmalig vorberechnet."""

    scores = effective_scores_for(session, users)
    return sorted(users, key=lambda u: _sort_key(u, scores.get(u.id, 0.0)))


def _open_assignment_counts(
    session: "Session", user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Anzahl aktuell OPEN Zuweisungen je User (für das Assignment-Cap)."""

    if not user_ids:
        return {}
    stmt = (
        select(TaskAssignment.user_id, func.count())
        .where(
            TaskAssignment.user_id.in_(user_ids),
            TaskAssignment.status == AssignmentStatus.OPEN,
        )
        .group_by(TaskAssignment.user_id)
    )
    return {user_id: int(count) for user_id, count in session.execute(stmt)}


def _order_by_cap(session: "Session", candidates: list[User]) -> list[User]:
    """Reiht bereits fairness-sortierte ``candidates`` nach dem OPEN-Cap um.

    Drei Stufen (innerhalb jeder bleibt die Fairness-Reihenfolge dank stabilem
    Sort erhalten):

    * **0 — unbelastet** (< ``SOFT_CAP_OPEN_ASSIGNMENTS``): kommen zuerst.
    * **1 — weicher Burst** (≥ Soft-Cap, < Hard-Cap): kommen dahinter. So
      sammelt eine Person (Newbie mit Score 0, Negativ-Karma) nicht in einer
      einzigen Verteil-Runde alles auf einmal auf, bekommt aber weiterhin
      Vorrang vor Personen am harten Limit.
    * **2 — hartes Limit** (≥ ``MAX_OPEN_ASSIGNMENTS_PER_USER``): ganz zuletzt.

    Kein hartes Entfernen — so fällt eine Occurrence nie an Abwesende, solange
    Anwesende existieren, selbst wenn alle am Limit sind.
    """

    counts = _open_assignment_counts(session, [c.id for c in candidates])

    def _tier(candidate: User) -> int:
        open_count = counts.get(candidate.id, 0)
        if open_count >= MAX_OPEN_ASSIGNMENTS_PER_USER:
            return 2
        if open_count >= SOFT_CAP_OPEN_ASSIGNMENTS:
            return 1
        return 0

    # sorted() ist stabil: gleiche Stufe behält die übergebene Fairness-Ordnung.
    return sorted(candidates, key=_tier)


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


def assign_occurrence(
    session: "Session", occurrence: TaskOccurrence
) -> list[TaskAssignment]:
    """Erzeugt ``TaskAssignment``-Zeilen für eine frisch erzeugte Occurrence.

    Existierende Assignments werden hier nicht angetastet — Aufrufer ist dafür
    verantwortlich, das vorher zu cleanen, falls eine Reassignment-Runde läuft.
    """

    definition: TaskDefinition = occurrence.task_definition
    n = max(int(definition.required_assignees or 1), 1)

    candidates = eligible_candidates(
        session, definition, occurrence.period_start, occurrence.period_end
    )
    candidates = _sorted_by_fairness(session, candidates)
    candidates = _order_by_cap(session, candidates)

    picked: list[tuple[User, bool]] = [(u, False) for u in candidates[:n]]

    if len(picked) < n:
        # Nicht genug Anwesende -> mit Abwesenden auffüllen.
        already = {u.id for u, _ in picked}
        fallback = _absent_eligible_candidates(
            session, definition, occurrence.period_start, occurrence.period_end
        )
        fallback = [u for u in fallback if u.id not in already]
        fallback = _sorted_by_fairness(session, fallback)
        for u in fallback:
            if len(picked) >= n:
                break
            picked.append((u, True))

    now = _utcnow()
    created: list[TaskAssignment] = []
    for user, during_absence in picked:
        assignment = TaskAssignment(
            occurrence_id=occurrence.id,
            user_id=user.id,
            status=AssignmentStatus.OPEN,
            points_earned=0,
            assigned_during_absence=during_absence,
        )
        session.add(assignment)
        # Notnagel-Zuweisung während eigener Abwesenheit zählt fairness-technisch
        # nicht als „war dran" — sonst rutscht die abwesende Person beim Tiebreak
        # (last_assigned_at) unverdient nach hinten.
        if not during_absence:
            user.last_assigned_at = now
        created.append(assignment)

    session.flush()
    return created


# ---------------------------------------------------------------------------
# Occurrence-Generierung
# ---------------------------------------------------------------------------


def _add_months(d: date, n: int) -> date:
    """Addiert ``n`` Monate auf ``d`` (Jahres-Rollover inklusive).

    Da der Tag-im-Monat-Anker auf 1..28 geklemmt ist, gibt es keine
    Monatslängen-Probleme (kein 30./31./29.02.).
    """

    month_index = d.month - 1 + n
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, d.day)


def _iter_periods(
    definition: TaskDefinition, today: date, count: int
) -> list[tuple[date, date]]:
    """Liefert die nächsten ``count`` Perioden als ``(start, end)``-Tupel.

    ``end`` ist inklusiv. Der ``start`` ist der "Tag, an dem die Aufgabe dran
    ist" — er wird später als ``due_date`` der Occurrence verwendet, damit die
    Kalender-Ansicht intuitiv ist.

    Recurrence-spezifisch:

    * ``DAILY``   — eine Periode pro Tag.
    * ``WEEKLY`` / ``BIWEEKLY`` — auf ``anchor_weekday`` (0=Mo … 6=So,
      Default 0) gesnappt; Schritt 7 bzw. 14 Tage.
    * ``MONTHLY`` — auf ``anchor_day_of_month`` (1..28, Default 1) gesnappt;
      Schritt ein Monat.
    * ``CUSTOM``  — Schritt ``recurrence_interval_days`` (positiver int, sonst
      leer).
    * ``NONE``    — leer (einmalige Aufgaben legt das Formular von Hand an).
    """

    recurrence = definition.recurrence

    if recurrence == Recurrence.DAILY:
        return [(today + timedelta(days=i), today + timedelta(days=i)) for i in range(count)]

    if recurrence in (Recurrence.WEEKLY, Recurrence.BIWEEKLY):
        step = 7 if recurrence == Recurrence.WEEKLY else 14
        anchor = definition.anchor_weekday
        if anchor is None:
            anchor = 0
        base = today - timedelta(days=(today.weekday() - anchor) % 7)
        periods: list[tuple[date, date]] = []
        for i in range(count):
            start = base + timedelta(days=i * step)
            end = start + timedelta(days=step - 1)
            periods.append((start, end))
        return periods

    if recurrence == Recurrence.MONTHLY:
        dom = definition.anchor_day_of_month or 1
        dom = max(1, min(28, dom))
        # Letzter dom-Tag <= heute (sonst Vormonat).
        if today.day >= dom:
            base = date(today.year, today.month, dom)
        else:
            base = _add_months(date(today.year, today.month, dom), -1)
        periods = []
        for i in range(count):
            start = _add_months(base, i)
            end = _add_months(base, i + 1) - timedelta(days=1)
            periods.append((start, end))
        return periods

    if recurrence == Recurrence.CUSTOM:
        step = definition.recurrence_interval_days
        if not isinstance(step, int) or step <= 0:
            return []
        periods = []
        for i in range(count):
            start = today + timedelta(days=i * step)
            end = start + timedelta(days=step - 1)
            periods.append((start, end))
        return periods

    # Recurrence.NONE (und alles Unbekannte).
    return []


def _period_start_exists(
    session: "Session", definition_id, period_start: date
) -> bool:
    stmt = select(TaskOccurrence.id).where(
        TaskOccurrence.task_definition_id == definition_id,
        TaskOccurrence.period_start == period_start,
    )
    return session.scalars(stmt).first() is not None


def generate_occurrences(
    session: "Session",
    lookahead_periods: int = DEFAULT_LOOKAHEAD_PERIODS,
    min_notice_days: int = MIN_NOTICE_DAYS,
) -> int:
    """Materialisiert je Definition genug Perioden im Voraus + weist sie zu.

    Pro Definition werden so viele Perioden erzeugt, dass BEIDE Mindestgrenzen
    erfüllt sind:

    * mindestens ``lookahead_periods`` Perioden (Perioden-Floor — großzügiger
      Vorlauf bei langen Intervallen wie MONTHLY/CUSTOM-30), UND
    * mindestens der ``min_notice_days``-Tage-Horizont: jede Periode, die
      innerhalb der nächsten ``min_notice_days`` Tage *startet*, wird
      materialisiert. So ist auch eine tägliche Aufgabe garantiert ≥ 1 Woche
      im Voraus zugewiesen.

    Idempotent: bereits existierende ``(task_definition_id, period_start)``-
    Kombinationen werden übersprungen. Für jede *neu* erzeugte Occurrence
    wird ``assign_occurrence`` aufgerufen.

    Gibt die Anzahl neu erzeugter Occurrences zurück.
    """

    today = _utcnow().date()
    horizon_date = today + timedelta(days=min_notice_days)
    created_total = 0

    definitions = list(
        session.scalars(
            select(TaskDefinition).where(TaskDefinition.is_active.is_(True))
        )
    )

    for definition in definitions:
        # Periodengrenzen je nach Recurrence (Wochentag-/Monats-Anker).
        # NONE / CUSTOM ohne gültiges Intervall -> leere Liste, nichts wird
        # automatisch generiert.
        #
        # Großzügig generieren (Tages-Horizont als Obergrenze), dann filtern auf:
        # die ersten `lookahead_periods` (Perioden-Floor) ODER jede Periode, die
        # im `min_notice_days`-Tage-Horizont startet (Zeit-Garantie).
        max_count = max(lookahead_periods, min_notice_days + 1)
        candidate_periods = _iter_periods(definition, today, max_count)
        periods = [
            (start, end)
            for index, (start, end) in enumerate(candidate_periods)
            if index < lookahead_periods or start <= horizon_date
        ]

        # Idempotent: bereits existierende (definition_id, period_start)-
        # Kombinationen werden übersprungen.
        for period_start_value, period_end in periods:
            if _period_start_exists(session, definition.id, period_start_value):
                continue

            occurrence = TaskOccurrence(
                task_definition_id=definition.id,
                period_start=period_start_value,
                period_end=period_end,
                # due_date = period_start: der Tag, an dem es dran ist (die
                # Kalender-Ansicht zeigt Tasks auf ihrem Start-Tag).
                due_date=period_start_value,
                # Optionale Default-Uhrzeit aus der Definition übernehmen.
                due_time=definition.default_due_time,
                status=TaskStatus.OPEN,
            )
            session.add(occurrence)
            session.flush()
            # Beziehung lazy-load für assign_occurrence.
            occurrence.task_definition = definition
            assign_occurrence(session, occurrence)
            created_total += 1

    return created_total


# ---------------------------------------------------------------------------
# Reassignment bei Absences
# ---------------------------------------------------------------------------


def reassign_open_overlap(
    session: "Session",
    user: User,
    start_date: date,
    end_date: date,
    *,
    skip_dienst: bool = False,
) -> int:
    """Wird vom Absences-Blueprint nach `Absence`-Erstellung gerufen.

    Entfernt die Assignments dieses Users auf OPEN Occurrences, deren Periode
    sich mit ``[start_date, end_date]`` überschneidet, und weist neu zu.
    Co-Assignees auf derselben Occurrence bleiben unangetastet.

    ``skip_dienst`` (Abwesenheits-Pfad): DIENST-Occurrences werden NICHT
    umverteilt — der Dienst bleibt bei der abwesenden Person (normale
    Behandlung, ggf. Hauswart-Review am Periodenende). Beim *Entfernen* eines
    Users (``reassign_all_open_for``) bleibt ``skip_dienst=False`` → auch
    Dienste wandern, weil die Person dauerhaft weg ist.

    Gibt die Anzahl re-zugewiesener Occurrences zurück.
    """

    stmt = (
        select(TaskOccurrence)
        .join(TaskAssignment, TaskAssignment.occurrence_id == TaskOccurrence.id)
        .where(
            TaskAssignment.user_id == user.id,
            TaskAssignment.status == AssignmentStatus.OPEN,
            TaskOccurrence.status == TaskStatus.OPEN,
            TaskOccurrence.period_start <= end_date,
            TaskOccurrence.period_end >= start_date,
        )
        .distinct()
    )
    if skip_dienst:
        stmt = stmt.join(
            TaskDefinition, TaskDefinition.id == TaskOccurrence.task_definition_id
        ).where(TaskDefinition.kind != TaskKind.DIENST)
    occurrences = list(session.scalars(stmt))

    reassigned = 0
    for occurrence in occurrences:
        # Wie viele Slots wir gleich neu füllen müssen.
        own_assignments = [
            a for a in occurrence.assignments if a.user_id == user.id
        ]
        if not own_assignments:
            continue

        # Co-Assignees behalten, die User-Assignments dieses Users löschen.
        kept_user_ids = {
            a.user_id for a in occurrence.assignments if a.user_id != user.id
        }
        for a in own_assignments:
            session.delete(a)
        session.flush()

        # Pure Re-pick: definition + period unverändert. Wir setzen ein
        # temporäres required_assignees-Override über einen Loop, der die
        # bereits behaltenen Plätze respektiert.
        definition: TaskDefinition = occurrence.task_definition
        slots_to_fill = max(
            int(definition.required_assignees or 1) - len(kept_user_ids), 0
        )
        if slots_to_fill <= 0:
            reassigned += 1
            continue

        candidates = eligible_candidates(
            session, definition, occurrence.period_start, occurrence.period_end
        )
        candidates = [c for c in candidates if c.id not in kept_user_ids and c.id != user.id]
        candidates = _sorted_by_fairness(session, candidates)
        candidates = _order_by_cap(session, candidates)

        picked: list[tuple[User, bool]] = [(u, False) for u in candidates[:slots_to_fill]]

        if len(picked) < slots_to_fill:
            already = {u.id for u, _ in picked}
            fallback = _absent_eligible_candidates(
                session, definition, occurrence.period_start, occurrence.period_end
            )
            fallback = [
                c
                for c in fallback
                if c.id not in kept_user_ids
                and c.id != user.id
                and c.id not in already
            ]
            fallback = _sorted_by_fairness(session, fallback)
            for u in fallback:
                if len(picked) >= slots_to_fill:
                    break
                picked.append((u, True))

        now = _utcnow()
        for u, during_absence in picked:
            session.add(
                TaskAssignment(
                    occurrence_id=occurrence.id,
                    user_id=u.id,
                    status=AssignmentStatus.OPEN,
                    points_earned=0,
                    assigned_during_absence=during_absence,
                )
            )
            # Siehe assign_occurrence: Fallback-Zuweisung an Abwesende verzerrt
            # den last_assigned_at-Tiebreak nicht.
            if not during_absence:
                u.last_assigned_at = now
        session.flush()
        reassigned += 1

    return reassigned


def reassign_all_open_for(session: "Session", user: User) -> int:
    """Verteilt ALLE offenen Zuweisungen eines Users neu.

    Gedacht für den Fall „Bewohner verlässt die WG / wird entfernt": die noch
    offenen Aufgaben sollen an die übrigen Bewohner zurückfallen, statt an der
    entfernten Person hängen zu bleiben. Dünner Wrapper um
    ``reassign_open_overlap`` über den maximalen Datumsbereich (greift damit
    jede OPEN-Occurrence des Users). ``reassign_open_overlap`` schließt den User
    selbst als Kandidat aus, eine zuvor entzogene HAUSBEWOHNER-Rolle ebenso.

    Gibt die Anzahl re-zugewiesener Occurrences zurück.
    """

    return reassign_open_overlap(session, user, date.min, date.max)


def rebalance_open_assignments(
    session: "Session", residents: list[User] | None = None
) -> int:
    """Verteilt OPEN-Future-Assignments fairer um, idempotent.

    Wird bei Mitglieder-Wechsel (neuer Bewohner aufgenommen, Rolle gewechselt)
    und im täglichen Cron-Catch-up aufgerufen. Swap-basiert: solange ``max-min
    > 1`` zwischen den Bewohnern, schiebe eine Zuweisung vom Überlasteten zum
    Unterlasteten — falls die Zielperson zur Occurrence eligibel ist und nicht
    im Zeitraum absent.

    Im Gegensatz zu ``generate_occurrences`` (das nur NEUE Perioden erzeugt)
    greift diese Funktion in EXISTIERENDE OPEN-Occurrences ein. Dadurch wird
    z.B. ein frisch aufgenommener Bewohner mit 0 Zuweisungen sofort in die
    bestehende Verteilung integriert.

    Schreibt nichts wenn schon ausgewogen. Idempotent: deterministische
    Reihenfolge über sortierte User-IDs.

    Gibt die Anzahl Swaps zurück.
    """

    today = _utcnow().date()
    if residents is None:
        residents = _approved_hausbewohner(session)
    if not residents:
        return 0

    user_by_id = {u.id: u for u in residents}
    user_ids = sorted(user_by_id.keys(), key=str)

    stmt = (
        select(TaskAssignment)
        .join(TaskOccurrence, TaskAssignment.occurrence_id == TaskOccurrence.id)
        .where(
            TaskAssignment.status == AssignmentStatus.OPEN,
            TaskOccurrence.period_end >= today,
        )
        .options(
            selectinload(TaskAssignment.occurrence)
            .selectinload(TaskOccurrence.task_definition)
            .selectinload(TaskDefinition.eligible_users),
            selectinload(TaskAssignment.occurrence).selectinload(
                TaskOccurrence.assignments
            ),
        )
    )
    open_assigns = [
        a for a in session.scalars(stmt) if a.user_id in user_by_id
    ]
    if not open_assigns:
        return 0

    absence_cache: dict[tuple[date, date], set] = {}

    def absent_ids_for(occ: TaskOccurrence) -> set:
        key = (occ.period_start, occ.period_end)
        if key not in absence_cache:
            absence_cache[key] = _absent_user_ids(
                session, occ.period_start, occ.period_end
            )
        return absence_cache[key]

    def is_eligible(user_id, occ: TaskOccurrence) -> bool:
        definition = occ.task_definition
        eligible = {link.user_id for link in (definition.eligible_users or [])}
        if eligible and user_id not in eligible:
            return False
        if user_id in absent_ids_for(occ):
            return False
        return True

    def counts() -> dict:
        c = {uid: 0 for uid in user_ids}
        for a in open_assigns:
            c[a.user_id] = c.get(a.user_id, 0) + 1
        return c

    swaps = 0
    for _ in range(500):  # Safety-Bound: realistisch endet's nach <20 Swaps.
        c = counts()
        # Deterministischer Tiebreak: bei gleicher Count die kleinere/größere
        # User-ID gewinnt — sodass mehrfache Aufrufe identisch enden.
        max_uid = max(c, key=lambda k: (c[k], str(k)))
        min_uid = min(c, key=lambda k: (c[k], str(k)))
        if c[max_uid] - c[min_uid] <= 1:
            break
        moved = False
        for a in open_assigns:
            if a.user_id != max_uid:
                continue
            if any(other.user_id == min_uid for other in a.occurrence.assignments):
                continue
            if not is_eligible(min_uid, a.occurrence):
                continue
            a.user_id = min_uid
            moved = True
            swaps += 1
            break
        if not moved:
            break

    if swaps:
        session.flush()
    return swaps


# ---------------------------------------------------------------------------
# Erledigung
# ---------------------------------------------------------------------------


def is_occurrence_step_complete(occurrence: TaskOccurrence) -> bool:
    """Für mehrteilige Aufgaben: sind ALLE Schritte × ALLE Assignees fertig?

    Wird in den Step-Done-Routen geprüft, um nach dem letzten Schritt
    automatisch ``mark_done`` auf alle Assignments auszuführen. Bei einteiligen
    Aufgaben (keine Steps) liefert die Funktion ``False`` — der reguläre
    Erledigt-Flow greift dort wie gehabt.
    """

    definition: TaskDefinition = occurrence.task_definition
    steps = definition.steps or []
    if not steps:
        return False
    assignments = occurrence.assignments
    if not assignments:
        return False
    for assignment in assignments:
        done_step_ids = {c.step_id for c in (assignment.step_completions or [])}
        for step in steps:
            if step.id not in done_step_ids:
                return False
    return True


def mark_done(
    session: "Session", assignment: TaskAssignment, by_user: User
) -> None:
    """Markiert eine Zuweisung als erledigt und schreibt Punkte gut.

    Wenn alle Zuweisungen auf der Occurrence DONE sind, wird auch die
    Occurrence auf DONE gesetzt. ``by_user`` ist mit-übergeben, falls eine
    spätere Erweiterung (z.B. "Hauswart hat für Bewohner abgehakt") darauf
    aufsetzen will — aktuell wird nur die Zugehörigkeit geprüft.
    """

    if assignment.status == AssignmentStatus.DONE:
        return

    occurrence: TaskOccurrence = assignment.occurrence
    definition: TaskDefinition = occurrence.task_definition

    n = max(len(occurrence.assignments), 1)
    points = int(round(int(definition.difficulty_points or 0) / n))

    assignment.status = AssignmentStatus.DONE
    assignment.completed_at = _utcnow()
    assignment.points_earned = points

    _rederive_occurrence_status(occurrence)

    session.flush()


def _rederive_occurrence_status(occurrence: TaskOccurrence) -> None:
    """Setzt den Occurrence-Status aus den Assignment-Status neu ab.

    Alle Assignments DONE -> Occurrence DONE; alle SKIPPED -> SKIPPED; sonst
    OPEN. Wird von ``mark_done``, ``review_assignment`` und ``excuse_assignment``
    genutzt, damit ein abgelehntes (= nicht mehr DONE-zählendes) Review die
    Occurrence wieder öffnen bzw. ein vollständig entschuldigter/übersprungener
    Dienst die Occurrence schließen kann. Der SKIPPED-Zweig greift nur, wenn
    *alle* Assignments SKIPPED sind — in mark_done/review_assignment kommt das
    nie vor, daher rückwärtskompatibel.
    """

    assignments = occurrence.assignments
    if assignments and all(
        a.status == AssignmentStatus.DONE for a in assignments
    ):
        occurrence.status = TaskStatus.DONE
    elif assignments and all(
        a.status == AssignmentStatus.SKIPPED for a in assignments
    ):
        occurrence.status = TaskStatus.SKIPPED
    else:
        occurrence.status = TaskStatus.OPEN


# ---------------------------------------------------------------------------
# Karma (Ehrenpunkte / Strafen)
# ---------------------------------------------------------------------------


def _assignment_point_share(occurrence: TaskOccurrence) -> int:
    """Punkte-Anteil pro Assignee einer Occurrence (mind. 1).

    Spiegelt die Vergabe in ``mark_done``: ``difficulty / anzahl_assignees``.
    Wird sowohl für die Penalty-Höhe (entgangene Punkte) als auch andernorts
    genutzt.
    """

    definition: TaskDefinition = occurrence.task_definition
    n = max(len(occurrence.assignments), 1)
    return max(int(round(int(definition.difficulty_points or 0) / n)), 1)


def award_honor(
    session: "Session",
    user: User,
    points: int,
    by_user: User | None = None,
    note: str | None = None,
    occurrence: TaskOccurrence | None = None,
) -> KarmaEvent:
    """Schreibt einen Ehrenpunkt-Event (positiv) gut.

    Hebt den Fairness-Score (seltener dran) und verrechnet bestehendes
    Negativ-Karma. ``points`` ist die positive Magnitude (mind. 1).
    """

    event = KarmaEvent(
        user_id=user.id,
        kind=KarmaKind.HONOR,
        points=max(int(points), 1),
        occurred_at=_utcnow(),
        created_by_id=by_user.id if by_user is not None else None,
        note=note,
        occurrence_id=occurrence.id if occurrence is not None else None,
    )
    session.add(event)
    session.flush()
    return event


def record_penalty(
    session: "Session",
    user: User,
    points: int,
    by_user: User | None = None,
    note: str | None = None,
    occurrence: TaskOccurrence | None = None,
) -> KarmaEvent:
    """Schreibt einen Negativ-Karma-Event (Strafe).

    Senkt den Fairness-Score (öfter dran). ``points`` ist die positive
    Magnitude (mind. 1); das Vorzeichen liefert ``KarmaKind.PENALTY``.
    """

    event = KarmaEvent(
        user_id=user.id,
        kind=KarmaKind.PENALTY,
        points=max(int(points), 1),
        occurred_at=_utcnow(),
        created_by_id=by_user.id if by_user is not None else None,
        note=note,
        occurrence_id=occurrence.id if occurrence is not None else None,
    )
    session.add(event)
    session.flush()
    return event


def _remove_penalties_for(
    session: "Session", user: User, occurrence: TaskOccurrence
) -> int:
    """Entfernt alle PENALTY-Karma-Events eines Users für eine Occurrence.

    Wird beim Entschuldigen genutzt, falls der nächtliche
    ``apply_overdue_penalties``-Job schon eine Strafe gebucht hatte, bevor der
    Hauswart von der Verhinderung (z. B. Krankheit) erfuhr. HONOR-Events bleiben
    unangetastet. Gibt die Anzahl entfernter Events zurück.
    """

    removed = 0
    for event in session.scalars(
        select(KarmaEvent).where(
            KarmaEvent.user_id == user.id,
            KarmaEvent.occurrence_id == occurrence.id,
            KarmaEvent.kind == KarmaKind.PENALTY,
        )
    ):
        session.delete(event)
        removed += 1
    return removed


# ---------------------------------------------------------------------------
# Hauswart-Review
# ---------------------------------------------------------------------------


def score_assignment(
    session: "Session",
    assignment: TaskAssignment,
    reviewer: User,
    points_earned: int,
    note: str | None = None,
) -> None:
    """Hauswart bewertet eine Zuweisung mit Punkten von 0 bis voll.

    Drei Fälle abhängig von ``points_earned`` (geklemmt auf
    ``[0, _assignment_point_share(occurrence)]``):

    * **voll**  → ``review_status = APPROVED``, full Punkte gutgeschrieben,
      keine Strafe.
    * **null**  → ``review_status = REJECTED``, 0 Punkte, PENALTY in voller
      Höhe (= klassische Ablehnung wie vorher).
    * **teilweise**  → ``review_status = APPROVED`` mit reduzierten Punkten,
      **Auto-Strafe** in Höhe der Differenz ``full - points_earned`` als
      PENALTY-Karma. So bedeutet „2 von 3 Punkte" zugleich 1 Strafe.

    **Idempotenz beim Re-Score**: vor dem Buchen einer neuen PENALTY werden
    alle bisherigen PENALTYs für diese Occurrence + diesen User entfernt,
    damit mehrfache Anpassungen sauber bleiben.
    """

    full = _assignment_point_share(assignment.occurrence)
    points_earned = max(0, min(int(points_earned), full))
    gap = full - points_earned

    assignment.reviewed_by_id = reviewer.id
    assignment.reviewed_at = _utcnow()
    assignment.review_note = note
    assignment.points_earned = points_earned

    if points_earned == 0:
        assignment.review_status = ReviewStatus.REJECTED
    else:
        assignment.review_status = ReviewStatus.APPROVED

    # Vorherige Strafe (falls vorhanden) wegräumen — verhindert Doppel-Strafe
    # bei Re-Reviews.
    _remove_penalties_for(session, assignment.user, assignment.occurrence)

    if gap > 0:
        record_penalty(
            session,
            assignment.user,
            gap,
            by_user=reviewer,
            note=note,
            occurrence=assignment.occurrence,
        )

    _rederive_occurrence_status(assignment.occurrence)
    session.flush()


def review_assignment(
    session: "Session",
    assignment: TaskAssignment,
    reviewer: User,
    approved: bool,
    note: str | None = None,
) -> None:
    """Backward-compat-Wrapper um ``score_assignment``.

    ``approved=True`` ≙ volle Punkte; ``approved=False`` ≙ 0 Punkte.
    Wird intern noch von Tests und den Shortcut-Routen approve/reject
    aufgerufen.
    """

    full = _assignment_point_share(assignment.occurrence)
    score_assignment(
        session,
        assignment,
        reviewer,
        points_earned=full if approved else 0,
        note=note,
    )


def hauswart_mark_done(
    session: "Session", assignment: TaskAssignment, reviewer: User
) -> None:
    """„Bewohner hat vergessen einzutragen" — Hauswart trägt nach + bestätigt.

    Verhält sich wie ``mark_done`` (Status DONE, Punkte, ``completed_at``) und
    setzt sofort ``review_status=APPROVED`` inkl. Reviewer-Daten.
    """

    mark_done(session, assignment, reviewer)
    assignment.review_status = ReviewStatus.APPROVED
    assignment.reviewed_by_id = reviewer.id
    assignment.reviewed_at = _utcnow()
    session.flush()


def excuse_assignment(
    session: "Session",
    assignment: TaskAssignment,
    reviewer: User,
    note: str | None = None,
) -> None:
    """Hauswart entschuldigt eine Zuweisung (z. B. krank / berechtigt verhindert).

    Neutraler dritter Review-Ausgang: weder Punkte noch Strafe.

    * ``review_status=EXCUSED`` + Reviewer-Daten + optionaler Grund (``note``).
    * ``points_earned=0`` — kein positiver Score.
    * Noch nicht erledigte (OPEN) Einträge werden ``SKIPPED``: damit verlassen
      sie die Review-Queue und der ``apply_overdue_penalties``-Job (nur OPEN)
      bestraft sie nicht nachträglich. Ein bereits abgehakter (DONE) Eintrag
      behält seinen Status.
    * Eine evtl. schon gebuchte PENALTY für diese Occurrence wird entfernt —
      kein negativer Score.
    """

    assignment.review_status = ReviewStatus.EXCUSED
    assignment.reviewed_by_id = reviewer.id
    assignment.reviewed_at = _utcnow()
    assignment.review_note = note
    assignment.points_earned = 0

    if assignment.status != AssignmentStatus.DONE:
        assignment.status = AssignmentStatus.SKIPPED

    _remove_penalties_for(session, assignment.user, assignment.occurrence)
    _rederive_occurrence_status(assignment.occurrence)
    session.flush()


def review_queue(session: "Session", days: int = 7) -> list[TaskAssignment]:
    """Assignments, die ein Hauswart-Review brauchen.

    Schlanke Queue (vgl. Plan): nur
    * Dienste, deren Periode beendet ist und noch PENDING sind, ODER
    * überfällig-unbeanspruchte Einträge (Periode vorbei, Assignment OPEN).

    Einfache abgehakte Aufgaben (DONE) gelten automatisch als ok und tauchen
    hier NICHT auf. Begrenzt auf das ``days``-Fenster
    (``period_end >= today - days``), damit die Queue beschränkt bleibt.
    Eager-load occurrence -> definition sowie user. Sortierung:
    ``period_end`` asc, dann ``user_id``.
    """

    today = _utcnow().date()
    window_start = today - timedelta(days=days)

    needs_review = or_(
        and_(
            TaskDefinition.kind == TaskKind.DIENST,
            TaskOccurrence.period_end <= today,
            TaskAssignment.review_status == ReviewStatus.PENDING,
        ),
        and_(
            TaskOccurrence.period_end < today,
            TaskAssignment.status == AssignmentStatus.OPEN,
        ),
    )

    stmt = (
        select(TaskAssignment)
        .join(TaskOccurrence, TaskAssignment.occurrence_id == TaskOccurrence.id)
        .join(
            TaskDefinition,
            TaskOccurrence.task_definition_id == TaskDefinition.id,
        )
        .where(
            TaskOccurrence.period_end >= window_start,
            needs_review,
        )
        .options(
            selectinload(TaskAssignment.occurrence).selectinload(
                TaskOccurrence.task_definition
            ),
            selectinload(TaskAssignment.user),
        )
        .order_by(TaskOccurrence.period_end, TaskAssignment.user_id)
    )
    return list(session.scalars(stmt))


def review_archive(
    session: "Session",
    *,
    user_id: uuid.UUID | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    status: ReviewStatus | None = None,
    days: int = 90,
) -> list[TaskAssignment]:
    """Archiv aller bewerteten Assignments (APPROVED / REJECTED / EXCUSED).

    Im Gegensatz zu ``review_queue`` zeigt das Archiv die Items NACH der
    Hauswart-Entscheidung. Default-Fenster: letzte ``days`` Tage über
    ``reviewed_at``. Optional filterbar nach Bewohner / Datums-Range / Status.

    Sortiert nach ``reviewed_at`` desc (neueste zuerst).
    """

    now = _utcnow()
    default_from = now - timedelta(days=days)

    stmt = (
        select(TaskAssignment)
        .join(TaskOccurrence, TaskAssignment.occurrence_id == TaskOccurrence.id)
        .join(
            TaskDefinition,
            TaskOccurrence.task_definition_id == TaskDefinition.id,
        )
        .where(
            TaskAssignment.review_status.in_(
                [ReviewStatus.APPROVED, ReviewStatus.REJECTED, ReviewStatus.EXCUSED]
            ),
            TaskAssignment.reviewed_at.is_not(None),
        )
        .options(
            selectinload(TaskAssignment.occurrence).selectinload(
                TaskOccurrence.task_definition
            ),
            selectinload(TaskAssignment.user),
        )
        .order_by(TaskAssignment.reviewed_at.desc())
    )

    if user_id is not None:
        stmt = stmt.where(TaskAssignment.user_id == user_id)
    if status is not None:
        stmt = stmt.where(TaskAssignment.review_status == status)
    if from_date is not None:
        stmt = stmt.where(
            TaskAssignment.reviewed_at >= datetime.combine(from_date, datetime.min.time(), tzinfo=timezone.utc)
        )
    elif from_date is None and to_date is None and user_id is None and status is None:
        # Default-Fenster nur greift, wenn KEINE Filter gesetzt sind.
        stmt = stmt.where(TaskAssignment.reviewed_at >= default_from)
    if to_date is not None:
        stmt = stmt.where(
            TaskAssignment.reviewed_at <= datetime.combine(to_date, datetime.max.time(), tzinfo=timezone.utc)
        )

    return list(session.scalars(stmt))


def review_queue_count(session: "Session", days: int = 7) -> int:
    """COUNT-only-Variante von ``review_queue`` für das Nav-Badge.

    Eine leichtgewichtige Aggregat-Query (kein Object-Loading, kein
    eager-Load), damit der Context-Processor in jedem Request quasi-frei
    aufrufbar ist.
    """

    today = _utcnow().date()
    window_start = today - timedelta(days=days)

    needs_review = or_(
        and_(
            TaskDefinition.kind == TaskKind.DIENST,
            TaskOccurrence.period_end <= today,
            TaskAssignment.review_status == ReviewStatus.PENDING,
        ),
        and_(
            TaskOccurrence.period_end < today,
            TaskAssignment.status == AssignmentStatus.OPEN,
        ),
    )

    stmt = (
        select(func.count())
        .select_from(TaskAssignment)
        .join(TaskOccurrence, TaskAssignment.occurrence_id == TaskOccurrence.id)
        .join(
            TaskDefinition,
            TaskOccurrence.task_definition_id == TaskDefinition.id,
        )
        .where(
            TaskOccurrence.period_end >= window_start,
            needs_review,
        )
    )
    return int(session.scalar(stmt) or 0)


def user_review_items(
    session: "Session", user: User, days: int = 7
) -> list[TaskAssignment]:
    """Alle Assignments eines Bewohners im ``days``-Fenster (Personen-Ansicht).

    Enthält auch auto-ok abgehakte Aufgaben, damit der Hauswart sie
    überschreiben kann. Fenster über die Occurrence-Periode
    (``period_end >= today - days``), neueste zuerst.
    """

    today = _utcnow().date()
    window_start = today - timedelta(days=days)

    stmt = (
        select(TaskAssignment)
        .join(TaskOccurrence, TaskAssignment.occurrence_id == TaskOccurrence.id)
        .where(
            TaskAssignment.user_id == user.id,
            TaskOccurrence.period_end >= window_start,
        )
        .options(
            selectinload(TaskAssignment.occurrence).selectinload(
                TaskOccurrence.task_definition
            ),
            selectinload(TaskAssignment.user),
        )
        .order_by(TaskOccurrence.period_end.desc(), TaskOccurrence.period_start.desc())
    )
    return list(session.scalars(stmt))


def user_task_stats(session: "Session", user: User) -> dict:
    """All-time-Statistiken eines Bewohners für die Übersicht.

    Liefert:
    * ``completed`` — Anzahl DONE-Assignments mit ``review_status != REJECTED``.
    * ``points``    — Summe aller ``points_earned``.
    * ``missed``    — Anzahl REJECTED + überfällig-unbeanspruchte (OPEN,
      ``period_end < today``).
    * ``reliability`` — ``completed / (completed + missed)``; ``None`` falls
      ``completed + missed == 0`` (noch keine bewertbaren Einträge).

    Zwei Aggregat-Queries (kein N+1).
    """

    today = _utcnow().date()

    # Aggregat 1: über die Assignments selbst (kein Occurrence-Join nötig).
    completed = session.scalar(
        select(func.count())
        .select_from(TaskAssignment)
        .where(
            TaskAssignment.user_id == user.id,
            TaskAssignment.status == AssignmentStatus.DONE,
            TaskAssignment.review_status != ReviewStatus.REJECTED,
        )
    ) or 0

    points = session.scalar(
        select(func.coalesce(func.sum(TaskAssignment.points_earned), 0)).where(
            TaskAssignment.user_id == user.id
        )
    ) or 0

    rejected = session.scalar(
        select(func.count())
        .select_from(TaskAssignment)
        .where(
            TaskAssignment.user_id == user.id,
            TaskAssignment.review_status == ReviewStatus.REJECTED,
        )
    ) or 0

    # Aggregat 2: überfällig-unbeansprucht (Occurrence-Join für period_end).
    overdue_open = session.scalar(
        select(func.count())
        .select_from(TaskAssignment)
        .join(TaskOccurrence, TaskAssignment.occurrence_id == TaskOccurrence.id)
        .where(
            TaskAssignment.user_id == user.id,
            TaskAssignment.status == AssignmentStatus.OPEN,
            TaskOccurrence.period_end < today,
        )
    ) or 0

    completed = int(completed)
    points = int(points)
    missed = int(rejected) + int(overdue_open)

    denominator = completed + missed
    reliability = completed / denominator if denominator > 0 else None

    return {
        "completed": completed,
        "points": points,
        "missed": missed,
        "reliability": reliability,
    }


# ---------------------------------------------------------------------------
# Laufende Dienste eines Bewohners
# ---------------------------------------------------------------------------


def current_duties_for(
    session: "Session", user: User, today: date
) -> list[TaskOccurrence]:
    """Laufende DIENST-Occurrences eines Bewohners.

    Ein Dienst läuft, wenn ``period_start <= today <= period_end`` und die
    Definition ``kind == DIENST`` ist und ``user`` zugewiesen ist. Eager-Load
    von Definition + Assignments (inkl. deren User) vermeidet N+1 beim Rendern.

    Wird vom Dashboard (laufender Dienst oben) und von der „Meine"-Aufgaben-
    Ansicht (laufender Dienst prominent oben) wiederverwendet.
    """

    stmt = (
        select(TaskOccurrence)
        .join(TaskDefinition, TaskOccurrence.task_definition_id == TaskDefinition.id)
        .join(TaskAssignment, TaskAssignment.occurrence_id == TaskOccurrence.id)
        .where(
            TaskDefinition.kind == TaskKind.DIENST,
            TaskAssignment.user_id == user.id,
            TaskOccurrence.period_start <= today,
            TaskOccurrence.period_end >= today,
        )
        .options(
            joinedload(TaskOccurrence.task_definition),
            selectinload(TaskOccurrence.assignments).selectinload(TaskAssignment.user),
        )
        .order_by(TaskOccurrence.period_end.asc())
        .distinct()
    )
    return list(session.scalars(stmt).unique())


# ---------------------------------------------------------------------------
# Automatischer Penalty bei Überfälligkeit
# ---------------------------------------------------------------------------


def apply_overdue_penalties(session: "Session") -> int:
    """Bestraft überfällig-unbeanspruchte Zuweisungen (Cron-Job).

    Findet alle ``OPEN``-Assignments auf Occurrences, deren Periode vorbei ist
    (``period_end < heute``), setzt sie auf ``SKIPPED`` und bucht je ein
    PENALTY-Karma-Event in Höhe der entgangenen Punkte. Idempotent: bereits
    auf SKIPPED gesetzte Zeilen werden nicht erneut bestraft. Eine Occurrence,
    deren Assignments danach alle SKIPPED sind, wird selbst SKIPPED.

    Gibt die Anzahl bestrafter Assignments zurück.
    """

    today = _utcnow().date()
    stmt = (
        select(TaskAssignment)
        .join(TaskOccurrence, TaskAssignment.occurrence_id == TaskOccurrence.id)
        .where(
            TaskAssignment.status == AssignmentStatus.OPEN,
            TaskOccurrence.period_end < today,
        )
        .options(
            selectinload(TaskAssignment.occurrence).selectinload(
                TaskOccurrence.task_definition
            ),
            selectinload(TaskAssignment.occurrence).selectinload(
                TaskOccurrence.assignments
            ),
            selectinload(TaskAssignment.user),
        )
    )
    overdue = list(session.scalars(stmt))

    penalized = 0
    for assignment in overdue:
        occurrence = assignment.occurrence
        assignment.status = AssignmentStatus.SKIPPED
        # Wer nur als Notnagel WÄHREND der eigenen Abwesenheit zugewiesen wurde,
        # konnte die Aufgabe gar nicht erledigen — die Occurrence wird zwar
        # SKIPPED (sonst hängt sie ewig in der Review-Queue), aber es gibt KEINE
        # Strafe. Ein regulär zugewiesener Verweigerer wird wie bisher bestraft.
        if not assignment.assigned_during_absence:
            record_penalty(
                session,
                assignment.user,
                _assignment_point_share(occurrence),
                by_user=None,
                note="Überfällig – nicht erledigt",
                occurrence=occurrence,
            )
            penalized += 1
        if all(
            a.status == AssignmentStatus.SKIPPED for a in occurrence.assignments
        ):
            occurrence.status = TaskStatus.SKIPPED

    session.flush()
    return penalized
