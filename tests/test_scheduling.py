"""Unit-Tests für den Fairness-Algorithmus in ``app.services.scheduling``.

Wir nutzen eine In-Memory-SQLite statt der Remote-Postgres, damit die Tests
ohne Netzwerk laufen. Das funktioniert, weil:

* SQLAlchemy 2.0s ``postgresql.UUID``-Typ auf SQLite einen CHAR-Fallback hat.
* Die hier getesteten Code-Pfade in ``scheduling.py`` keine JSONB-Spalten
  anfassen — die einzige JSONB-Spalte (``AuditLog.payload``) wird in
  ``CREATE TABLE`` zwar erzeugt, aber wir schreiben sie nie.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from flask import Flask
from sqlalchemy.orm import Session

from app import create_app
from app.domain.enums import (
    AssignmentStatus,
    Recurrence,
    ReviewStatus,
    Role,
    TaskKind,
    TaskStatus,
    UserStatus,
)
from app.extensions import db
from app.models.absence import Absence
from app.models.task import (
    TaskAssignment,
    TaskDefinition,
    TaskOccurrence,
)
from app.models.user import User, UserRole
from app.services import scheduling


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(monkeypatch) -> Flask:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    application = create_app("dev")
    application.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    application.config["SQLALCHEMY_ENGINE_OPTIONS"] = {}
    application.config["TESTING"] = True
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def session(app: Flask) -> Session:
    return db.session


def _make_user(
    session: Session,
    name: str,
    *,
    joined_days_ago: int = 365,
    last_assigned_days_ago: int | None = None,
    role: Role = Role.HAUSBEWOHNER,
    status: UserStatus = UserStatus.APPROVED,
) -> User:
    now = datetime.now(timezone.utc)
    import uuid as _uuid
    suffix = _uuid.uuid4().hex[:6]
    user = User(
        username=f"{name.lower().replace(' ', '.').replace('.', '-')}-{suffix}",
        email=f"{name.lower().replace(' ', '.')}-{suffix}@example.com",
        name=name,
        status=status,
        joined_at=now - timedelta(days=joined_days_ago),
        last_assigned_at=(
            now - timedelta(days=last_assigned_days_ago)
            if last_assigned_days_ago is not None
            else None
        ),
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    session.add(UserRole(user_id=user.id, role=role))
    session.flush()
    return user


def _make_definition(
    session: Session,
    title: str = "Mülldienst",
    *,
    recurrence: Recurrence = Recurrence.WEEKLY,
    required: int = 1,
    difficulty: int = 4,
    kind: TaskKind = TaskKind.AUFGABE,
    anchor_weekday: int | None = None,
    anchor_day_of_month: int | None = None,
    interval_days: int | None = None,
) -> TaskDefinition:
    definition = TaskDefinition(
        title=title,
        difficulty_points=difficulty,
        recurrence=recurrence,
        kind=kind,
        anchor_weekday=anchor_weekday,
        anchor_day_of_month=anchor_day_of_month,
        recurrence_interval_days=interval_days,
        required_assignees=required,
        is_active=True,
    )
    session.add(definition)
    session.flush()
    return definition


def _make_occurrence(
    session: Session,
    definition: TaskDefinition,
    *,
    period_start: date,
    period_length: int = 7,
) -> TaskOccurrence:
    period_end = period_start + timedelta(days=period_length - 1)
    occurrence = TaskOccurrence(
        task_definition_id=definition.id,
        period_start=period_start,
        period_end=period_end,
        due_date=period_end,
        status=TaskStatus.OPEN,
    )
    session.add(occurrence)
    session.flush()
    occurrence.task_definition = definition
    return occurrence


_SEED_OFFSET = {"value": 0}


def _grant_points(
    session: Session,
    user: User,
    definition: TaskDefinition,
    points: int,
    *,
    completed_days_ago: int = 10,
) -> TaskAssignment:
    """Hilfsfunktion: legt eine bereits erledigte Assignment an, um Score zu seeden.

    Jeder Aufruf bekommt ein eigenes ``period_start`` (Offset über einen
    Modul-Counter), damit das ``UNIQUE(task_definition_id, period_start)``
    von TaskOccurrence nicht verletzt wird.
    """

    now = datetime.now(timezone.utc)
    _SEED_OFFSET["value"] += 1
    occurrence_start = (
        now - timedelta(days=completed_days_ago + 1 + _SEED_OFFSET["value"])
    ).date()
    occurrence = _make_occurrence(
        session, definition, period_start=occurrence_start, period_length=1
    )
    assignment = TaskAssignment(
        occurrence_id=occurrence.id,
        user_id=user.id,
        status=AssignmentStatus.DONE,
        completed_at=now - timedelta(days=completed_days_ago),
        points_earned=points,
    )
    session.add(assignment)
    session.flush()
    return assignment


# ---------------------------------------------------------------------------
# effective_score
# ---------------------------------------------------------------------------


def test_effective_score_new_resident_normalises_against_90_days(session):
    """Neuer Bewohner (< 90 Tage) wird gegen 90 normalisiert, nicht gegen Tenure.

    Formel: ``sum_points / max(days_active, 90) * 90``. Für den Frischling
    (10 Tage) wirkt der 90-Tage-Floor und wir bekommen ``9/90*90 = 9``. Der
    Veteran (400 Tage) wird durch seine echte Tenure geteilt: ``9/400*90 = 2.025``.
    """

    fresh = _make_user(session, "Frischling", joined_days_ago=10)
    veteran = _make_user(session, "Veteran", joined_days_ago=400)
    seed_def = _make_definition(session, title="Seed", difficulty=10)

    _grant_points(session, fresh, seed_def, points=9)
    _grant_points(session, veteran, seed_def, points=9)

    fresh_score = scheduling.effective_score(session, fresh)
    vet_score = scheduling.effective_score(session, veteran)

    # Frischling: 90-Tage-Floor greift -> voller Score.
    assert fresh_score == pytest.approx(9.0)
    # Veteran: echte Tenure -> Score deutlich kleiner.
    assert vet_score == pytest.approx(9.0 / 400 * 90)
    assert fresh_score > vet_score


def test_effective_score_zero_when_no_assignments(session):
    user = _make_user(session, "Untaetig", joined_days_ago=200)
    assert scheduling.effective_score(session, user) == 0.0


def test_effective_score_ignores_old_assignments(session):
    user = _make_user(session, "Alterling", joined_days_ago=400)
    seed_def = _make_definition(session, title="Seed", difficulty=10)
    _grant_points(session, user, seed_def, points=5, completed_days_ago=200)

    assert scheduling.effective_score(session, user) == 0.0


# ---------------------------------------------------------------------------
# eligible_candidates + Sortierung
# ---------------------------------------------------------------------------


def test_assignment_sort_score_then_last_assigned_then_id(session):
    """Sortierung: Score asc, last_assigned_at asc, user_id asc."""

    # Drei User mit klarem Tiebreaker-Verlauf:
    a = _make_user(session, "Alex", joined_days_ago=200, last_assigned_days_ago=30)
    b = _make_user(session, "Bea", joined_days_ago=200, last_assigned_days_ago=10)
    c = _make_user(session, "Cem", joined_days_ago=200, last_assigned_days_ago=None)

    seed_def = _make_definition(session, title="Seed", difficulty=10)
    # Alex und Bea bekommen gleich viele Punkte -> Tiebreaker last_assigned_at.
    _grant_points(session, a, seed_def, points=3)
    _grant_points(session, b, seed_def, points=3)
    # Cem hat den höchsten Score und wird nicht gepickt.
    _grant_points(session, c, seed_def, points=10)

    definition = _make_definition(session, title="Putzdienst", required=2)
    occurrence = _make_occurrence(
        session, definition, period_start=date.today() + timedelta(days=1)
    )

    assignments = scheduling.assign_occurrence(session, occurrence)

    # Erwartet: Alex (älter last_assigned) zuerst, dann Bea — Cem nicht.
    picked_names = [
        session.get(User, a.user_id).name for a in assignments
    ]
    assert picked_names == ["Alex", "Bea"]


def test_absent_user_is_excluded(session):
    away = _make_user(session, "Weg", joined_days_ago=200)
    here = _make_user(session, "Da", joined_days_ago=200)

    period_start = date.today() + timedelta(days=2)
    period_end = period_start + timedelta(days=6)

    session.add(
        Absence(
            user_id=away.id,
            start_date=period_start,
            end_date=period_end,
        )
    )
    session.flush()

    definition = _make_definition(session, required=1)
    occurrence = _make_occurrence(
        session, definition, period_start=period_start
    )
    assignments = scheduling.assign_occurrence(session, occurrence)

    assert len(assignments) == 1
    assert assignments[0].user_id == here.id
    assert assignments[0].assigned_during_absence is False


def test_all_absent_fallback_picks_lowest_score_with_flag(session):
    """Wenn alle abwesend sind, wird der mit niedrigstem Score genommen, Flag gesetzt."""

    period_start = date.today() + timedelta(days=2)
    period_end = period_start + timedelta(days=6)

    seed_def = _make_definition(session, title="Seed", difficulty=10)

    lo = _make_user(session, "LowScore", joined_days_ago=200, last_assigned_days_ago=20)
    hi = _make_user(session, "HighScore", joined_days_ago=200, last_assigned_days_ago=20)
    _grant_points(session, hi, seed_def, points=8)

    for u in (lo, hi):
        session.add(
            Absence(user_id=u.id, start_date=period_start, end_date=period_end)
        )
    session.flush()

    definition = _make_definition(session, required=1)
    occurrence = _make_occurrence(
        session, definition, period_start=period_start
    )
    assignments = scheduling.assign_occurrence(session, occurrence)

    assert len(assignments) == 1
    assert assignments[0].user_id == lo.id
    assert assignments[0].assigned_during_absence is True


# ---------------------------------------------------------------------------
# generate_occurrences
# ---------------------------------------------------------------------------


def test_generate_occurrences_is_idempotent(session):
    _make_user(session, "Eins", joined_days_ago=200)
    _make_user(session, "Zwei", joined_days_ago=200)

    _make_definition(
        session, title="Putzen", recurrence=Recurrence.WEEKLY, required=1
    )

    first = scheduling.generate_occurrences(session, lookahead_periods=2)
    second = scheduling.generate_occurrences(session, lookahead_periods=2)

    assert first == 2
    assert second == 0  # zweiter Aufruf erzeugt nichts Neues

    total_occurrences = session.query(TaskOccurrence).count()
    assert total_occurrences == 2


def test_generate_occurrences_skips_none_recurrence(session):
    _make_user(session, "Solo", joined_days_ago=200)
    _make_definition(session, title="Einmalig", recurrence=Recurrence.NONE)

    created = scheduling.generate_occurrences(session, lookahead_periods=2)
    assert created == 0


# ---------------------------------------------------------------------------
# _iter_periods / Anker (Wochentag, Tag-im-Monat, CUSTOM)
# ---------------------------------------------------------------------------


def test_weekly_anchor_lands_on_chosen_weekday(session):
    """WEEKLY mit anchor_weekday=1 (Dienstag) -> jeder period_start ist ein Di."""

    _make_user(session, "Eins", joined_days_ago=200)
    definition = _make_definition(
        session,
        title="Müll",
        recurrence=Recurrence.WEEKLY,
        anchor_weekday=1,  # Dienstag
    )

    scheduling.generate_occurrences(session, lookahead_periods=4)

    starts = sorted(
        o.period_start
        for o in session.query(TaskOccurrence)
        .filter(TaskOccurrence.task_definition_id == definition.id)
        .all()
    )
    assert len(starts) == 4
    # 1 == Dienstag (Mon=0).
    assert all(s.weekday() == 1 for s in starts)
    # Aufeinanderfolgende Starts sind genau 7 Tage auseinander.
    for prev, nxt in zip(starts, starts[1:]):
        assert (nxt - prev).days == 7


def test_biweekly_anchor_steps_14_days(session):
    _make_user(session, "Eins", joined_days_ago=200)
    definition = _make_definition(
        session,
        title="Großputz",
        recurrence=Recurrence.BIWEEKLY,
        anchor_weekday=3,  # Donnerstag
    )

    scheduling.generate_occurrences(session, lookahead_periods=3)

    starts = sorted(
        o.period_start
        for o in session.query(TaskOccurrence)
        .filter(TaskOccurrence.task_definition_id == definition.id)
        .all()
    )
    assert len(starts) == 3
    assert all(s.weekday() == 3 for s in starts)
    for prev, nxt in zip(starts, starts[1:]):
        assert (nxt - prev).days == 14


def test_monthly_anchor_lands_on_chosen_day_of_month(session):
    """MONTHLY mit anchor_day_of_month=15 -> jeder period_start hat day == 15."""

    _make_user(session, "Eins", joined_days_ago=200)
    definition = _make_definition(
        session,
        title="Heizung",
        recurrence=Recurrence.MONTHLY,
        anchor_day_of_month=15,
    )

    scheduling.generate_occurrences(session, lookahead_periods=3)

    starts = sorted(
        o.period_start
        for o in session.query(TaskOccurrence)
        .filter(TaskOccurrence.task_definition_id == definition.id)
        .all()
    )
    assert len(starts) == 3
    assert all(s.day == 15 for s in starts)


def test_custom_interval_three_days_apart(session):
    """CUSTOM mit interval=3 -> aufeinanderfolgende period_starts 3 Tage apart."""

    _make_user(session, "Eins", joined_days_ago=200)
    definition = _make_definition(
        session,
        title="Pflanzen gießen",
        recurrence=Recurrence.CUSTOM,
        interval_days=3,
    )

    scheduling.generate_occurrences(session, lookahead_periods=4)

    starts = sorted(
        o.period_start
        for o in session.query(TaskOccurrence)
        .filter(TaskOccurrence.task_definition_id == definition.id)
        .all()
    )
    assert len(starts) == 4
    for prev, nxt in zip(starts, starts[1:]):
        assert (nxt - prev).days == 3


def test_custom_without_interval_generates_nothing(session):
    """CUSTOM ohne (gültiges) Intervall kann nicht generieren -> 0."""

    _make_user(session, "Eins", joined_days_ago=200)
    _make_definition(
        session,
        title="Unklar",
        recurrence=Recurrence.CUSTOM,
        interval_days=None,
    )

    created = scheduling.generate_occurrences(session, lookahead_periods=3)
    assert created == 0


def test_generated_occurrence_due_date_equals_period_start(session):
    """due_date wird auf period_start gesetzt (nicht period_end)."""

    _make_user(session, "Eins", joined_days_ago=200)
    definition = _make_definition(
        session, title="Müll", recurrence=Recurrence.WEEKLY, anchor_weekday=1
    )

    scheduling.generate_occurrences(session, lookahead_periods=2)

    occurrences = (
        session.query(TaskOccurrence)
        .filter(TaskOccurrence.task_definition_id == definition.id)
        .all()
    )
    assert occurrences
    assert all(o.due_date == o.period_start for o in occurrences)


# ---------------------------------------------------------------------------
# effective_scores_for — Batch-Parität mit effective_score
# ---------------------------------------------------------------------------


def test_effective_scores_for_matches_single_user_score(session):
    """Batch-Score liefert exakt dieselben Werte wie der Per-User-Score."""

    fresh = _make_user(session, "Frischling", joined_days_ago=10)
    veteran = _make_user(session, "Veteran", joined_days_ago=400)
    idle = _make_user(session, "Untaetig", joined_days_ago=200)

    seed_def = _make_definition(session, title="Seed", difficulty=10)
    _grant_points(session, fresh, seed_def, points=9)
    _grant_points(session, veteran, seed_def, points=9)
    # idle bekommt keine Punkte -> erwartet 0.0.

    users = [fresh, veteran, idle]
    batch = scheduling.effective_scores_for(session, users)

    for u in users:
        assert batch[u.id] == pytest.approx(scheduling.effective_score(session, u))

    # Konkrete Erwartungswerte (vgl. den Per-User-Score-Test).
    assert batch[fresh.id] == pytest.approx(9.0)
    assert batch[veteran.id] == pytest.approx(9.0 / 400 * 90)
    assert batch[idle.id] == pytest.approx(0.0)


def test_effective_scores_for_empty_returns_empty(session):
    assert scheduling.effective_scores_for(session, []) == {}


# ---------------------------------------------------------------------------
# reassign_open_overlap
# ---------------------------------------------------------------------------


def test_reassign_open_overlap_moves_to_other_user(session):
    a = _make_user(session, "Alpha", joined_days_ago=200, last_assigned_days_ago=5)
    b = _make_user(session, "Beta", joined_days_ago=200, last_assigned_days_ago=50)

    seed_def = _make_definition(session, title="Seed", difficulty=10)
    # Sorgen dafür, dass Alpha den niedrigeren Score hat und damit gepickt wird.
    _grant_points(session, b, seed_def, points=8)

    period_start = date.today() + timedelta(days=3)
    period_end = period_start + timedelta(days=6)

    definition = _make_definition(session, title="Müll", required=1)
    occurrence = _make_occurrence(
        session, definition, period_start=period_start
    )
    initial = scheduling.assign_occurrence(session, occurrence)
    assert len(initial) == 1
    assert initial[0].user_id == a.id

    # Alpha wird abwesend → soll re-zugewiesen werden.
    session.add(
        Absence(user_id=a.id, start_date=period_start, end_date=period_end)
    )
    session.flush()

    moved = scheduling.reassign_open_overlap(session, a, period_start, period_end)
    assert moved == 1

    session.refresh(occurrence)
    remaining = [
        a for a in occurrence.assignments if a.status == AssignmentStatus.OPEN
    ]
    assert len(remaining) == 1
    assert remaining[0].user_id == b.id


# ---------------------------------------------------------------------------
# mark_done
# ---------------------------------------------------------------------------


def test_mark_done_writes_points_and_closes_occurrence(session):
    user = _make_user(session, "Solo", joined_days_ago=200)

    definition = _make_definition(session, title="Bad putzen", difficulty=6, required=1)
    occurrence = _make_occurrence(
        session, definition, period_start=date.today() + timedelta(days=1)
    )
    [assignment] = scheduling.assign_occurrence(session, occurrence)

    scheduling.mark_done(session, assignment, user)

    assert assignment.status == AssignmentStatus.DONE
    assert assignment.points_earned == 6
    assert occurrence.status == TaskStatus.DONE


def test_mark_done_splits_points_for_multi_assignees(session):
    a = _make_user(session, "Eins", joined_days_ago=200)
    b = _make_user(session, "Zwei", joined_days_ago=200)

    definition = _make_definition(
        session, title="Großputz", difficulty=10, required=2
    )
    occurrence = _make_occurrence(
        session, definition, period_start=date.today() + timedelta(days=1)
    )
    assignments = scheduling.assign_occurrence(session, occurrence)
    assert len(assignments) == 2

    scheduling.mark_done(session, assignments[0], a)

    assert assignments[0].points_earned == 5  # 10 / 2
    # Solange nicht alle DONE sind, bleibt die Occurrence OPEN.
    assert occurrence.status == TaskStatus.OPEN

    scheduling.mark_done(session, assignments[1], b)
    assert occurrence.status == TaskStatus.DONE


# ---------------------------------------------------------------------------
# review_assignment / hauswart_mark_done
# ---------------------------------------------------------------------------


def _make_assignment(
    session: Session,
    occurrence: TaskOccurrence,
    user: User,
    *,
    status: AssignmentStatus = AssignmentStatus.OPEN,
    points: int = 0,
    completed_days_ago: int | None = None,
    review_status: ReviewStatus = ReviewStatus.PENDING,
) -> TaskAssignment:
    """Direkt eine TaskAssignment auf einer Occurrence anlegen (für Reviews)."""

    now = datetime.now(timezone.utc)
    assignment = TaskAssignment(
        occurrence_id=occurrence.id,
        user_id=user.id,
        status=status,
        points_earned=points,
        completed_at=(
            now - timedelta(days=completed_days_ago)
            if completed_days_ago is not None
            else None
        ),
        review_status=review_status,
    )
    session.add(assignment)
    session.flush()
    return assignment


def test_review_assignment_approved_keeps_points(session):
    reviewer = _make_user(session, "Hauswart", role=Role.HAUSWART)
    user = _make_user(session, "Bewohner", joined_days_ago=200)

    definition = _make_definition(session, title="Bad", difficulty=6, required=1)
    occurrence = _make_occurrence(
        session, definition, period_start=date.today() + timedelta(days=1)
    )
    assignment = _make_assignment(
        session,
        occurrence,
        user,
        status=AssignmentStatus.DONE,
        points=6,
        completed_days_ago=1,
    )

    scheduling.review_assignment(session, assignment, reviewer, approved=True)

    assert assignment.review_status == ReviewStatus.APPROVED
    assert assignment.reviewed_by_id == reviewer.id
    assert assignment.reviewed_at is not None
    assert assignment.points_earned == 6  # Punkte bleiben.


def test_review_assignment_rejected_zeroes_points_and_drops_score(session):
    """REJECTED entzieht Punkte UND bucht Negativ-Karma -> Score wird negativ."""

    reviewer = _make_user(session, "Hauswart", role=Role.HAUSWART)
    user = _make_user(session, "Bewohner", joined_days_ago=200)

    definition = _make_definition(session, title="Bad", difficulty=6, required=1)
    # Occurrence-Periode + completed_at frisch genug fürs Score-Fenster.
    occurrence = _make_occurrence(
        session, definition, period_start=date.today() - timedelta(days=2)
    )
    assignment = _make_assignment(
        session,
        occurrence,
        user,
        status=AssignmentStatus.DONE,
        points=6,
        completed_days_ago=1,
    )

    score_before = scheduling.effective_score(session, user)
    assert score_before > 0  # 6 Punkte im Fenster.

    scheduling.review_assignment(
        session, assignment, reviewer, approved=False, note="Schlecht geputzt"
    )

    assert assignment.review_status == ReviewStatus.REJECTED
    assert assignment.review_note == "Schlecht geputzt"
    assert assignment.points_earned == 0  # Punkte entzogen.

    # Punkte weg (0) UND ein PENALTY in Höhe des Anteils (6/1 = 6) -> Score
    # rutscht ins Minus: -6 / max(200, 90) * 90.
    score_after = scheduling.effective_score(session, user)
    assert score_after == pytest.approx(-6 / 200 * 90)


def test_hauswart_mark_done_sets_done_approved_and_points(session):
    """Hauswart trägt für vergesslichen Bewohner nach: DONE + APPROVED + Punkte."""

    reviewer = _make_user(session, "Hauswart", role=Role.HAUSWART)
    user = _make_user(session, "Bewohner", joined_days_ago=200)

    definition = _make_definition(session, title="Müll", difficulty=8, required=1)
    occurrence = _make_occurrence(
        session, definition, period_start=date.today() - timedelta(days=1)
    )
    assignment = _make_assignment(
        session, occurrence, user, status=AssignmentStatus.OPEN
    )

    scheduling.hauswart_mark_done(session, assignment, reviewer)

    assert assignment.status == AssignmentStatus.DONE
    assert assignment.review_status == ReviewStatus.APPROVED
    assert assignment.points_earned == 8  # difficulty / 1 assignee.
    assert assignment.reviewed_by_id == reviewer.id
    assert assignment.completed_at is not None
    assert occurrence.status == TaskStatus.DONE


# ---------------------------------------------------------------------------
# review_queue
# ---------------------------------------------------------------------------


def test_review_queue_includes_dienst_overdue_open_and_done_aufgabe(session):
    """Neue Semantik: jede DONE+PENDING-Zuweisung muss in der Queue sein.

    AUFGABE soll nicht mehr ungeprueft durchwinkten — Hauswart entscheidet
    bewusst ueber jede Erledigung (Teilpunkte / voll / ablehnen).
    """
    user = _make_user(session, "Bewohner", joined_days_ago=200)
    today = date.today()

    # 1) Beendeter DIENST, PENDING -> in der Queue.
    dienst_def = _make_definition(
        session, title="Mülldienst", kind=TaskKind.DIENST, difficulty=6
    )
    dienst_occ = _make_occurrence(
        session, dienst_def, period_start=today - timedelta(days=8), period_length=7
    )  # period_end = gestern.
    dienst_a = _make_assignment(
        session,
        dienst_occ,
        user,
        status=AssignmentStatus.DONE,
        points=6,
        completed_days_ago=1,
        review_status=ReviewStatus.PENDING,
    )

    # 2) Überfällige, nicht-abgehakte AUFGABE (OPEN) -> in der Queue.
    overdue_def = _make_definition(session, title="Spülen", kind=TaskKind.AUFGABE)
    overdue_occ = _make_occurrence(
        session, overdue_def, period_start=today - timedelta(days=3), period_length=1
    )  # period_end vor heute.
    overdue_a = _make_assignment(
        session, overdue_occ, user, status=AssignmentStatus.OPEN
    )

    # 3) Abgehakte AUFGABE (DONE+PENDING) -> JETZT auch in der Queue,
    # damit der Hauswart sie bewerten kann.
    done_def = _make_definition(session, title="Kehren", kind=TaskKind.AUFGABE)
    done_occ = _make_occurrence(
        session, done_def, period_start=today, period_length=1
    )
    done_a = _make_assignment(
        session,
        done_occ,
        user,
        status=AssignmentStatus.DONE,
        points=4,
        completed_days_ago=0,
        review_status=ReviewStatus.PENDING,
    )

    queue = scheduling.review_queue(session, days=14)
    queue_ids = {a.id for a in queue}

    assert dienst_a.id in queue_ids
    assert overdue_a.id in queue_ids
    assert done_a.id in queue_ids


# ---------------------------------------------------------------------------
# user_task_stats
# ---------------------------------------------------------------------------


def test_user_task_stats_counts_completed_missed_points_reliability(session):
    user = _make_user(session, "Bewohner", joined_days_ago=200)
    today = date.today()

    definition = _make_definition(session, title="Putzen", difficulty=4)

    # 2x done + approved (zählt als completed, Punkte zählen).
    for i in range(2):
        occ = _make_occurrence(
            session, definition, period_start=today - timedelta(days=20 + i)
        )
        _make_assignment(
            session,
            occ,
            user,
            status=AssignmentStatus.DONE,
            points=3,
            completed_days_ago=10,
            review_status=ReviewStatus.APPROVED,
        )

    # 1x rejected (zählt als missed, Punkte=0).
    occ_rej = _make_occurrence(
        session, definition, period_start=today - timedelta(days=40)
    )
    _make_assignment(
        session,
        occ_rej,
        user,
        status=AssignmentStatus.DONE,
        points=0,
        completed_days_ago=30,
        review_status=ReviewStatus.REJECTED,
    )

    # 1x überfällig-unbeansprucht (OPEN, period_end < today) -> missed.
    occ_overdue = _make_occurrence(
        session, definition, period_start=today - timedelta(days=5), period_length=1
    )
    _make_assignment(session, occ_overdue, user, status=AssignmentStatus.OPEN)

    stats = scheduling.user_task_stats(session, user)

    assert stats["completed"] == 2
    assert stats["missed"] == 2  # 1 rejected + 1 overdue-open.
    assert stats["points"] == 6  # 3 + 3 (rejected hat 0).
    assert stats["reliability"] == pytest.approx(0.5)  # 2 / (2 + 2).


def test_user_task_stats_reliability_none_when_nothing(session):
    user = _make_user(session, "Neuling", joined_days_ago=5)
    stats = scheduling.user_task_stats(session, user)
    assert stats["completed"] == 0
    assert stats["missed"] == 0
    assert stats["points"] == 0
    assert stats["reliability"] is None


# ---------------------------------------------------------------------------
# current_duties_for
# ---------------------------------------------------------------------------


def test_current_duties_for_returns_only_active_dienst_of_user(session):
    """Liefert nur DIENST-Occurrences, in deren Periode ``today`` liegt
    und denen der gegebene User zugewiesen ist."""

    today = date.today()
    me = _make_user(session, "Ich", joined_days_ago=100)
    other = _make_user(session, "Andere", joined_days_ago=100)

    # 1) Laufender Dienst des Users -> soll auftauchen.
    duty_def = _make_definition(
        session, title="Müll", kind=TaskKind.DIENST, required=1
    )
    duty_occ = _make_occurrence(
        session, duty_def, period_start=today - timedelta(days=2), period_length=7
    )
    _make_assignment(session, duty_occ, me, status=AssignmentStatus.OPEN)

    # 2) Gleicher Dienst, aber Periode liegt in der Zukunft -> nicht dabei.
    future_occ = _make_occurrence(
        session, duty_def, period_start=today + timedelta(days=10), period_length=7
    )
    _make_assignment(session, future_occ, me, status=AssignmentStatus.OPEN)

    # 3) Laufender Dienst, aber dem anderen User zugewiesen -> nicht dabei.
    other_duty_def = _make_definition(
        session, title="Bad", kind=TaskKind.DIENST, required=1
    )
    other_occ = _make_occurrence(
        session, other_duty_def, period_start=today - timedelta(days=1), period_length=7
    )
    _make_assignment(session, other_occ, other, status=AssignmentStatus.OPEN)

    # 4) Laufende Aufgabe (kind=AUFGABE) des Users -> nicht dabei.
    aufgabe_def = _make_definition(
        session, title="Staubsaugen", kind=TaskKind.AUFGABE, required=1
    )
    aufgabe_occ = _make_occurrence(
        session, aufgabe_def, period_start=today, period_length=1
    )
    _make_assignment(session, aufgabe_occ, me, status=AssignmentStatus.OPEN)

    result = scheduling.current_duties_for(session, me, today)

    assert [o.id for o in result] == [duty_occ.id]


# ---------------------------------------------------------------------------
# reassign_all_open_for (Entfernen eines Bewohners)
# ---------------------------------------------------------------------------


def test_reassign_all_open_for_moves_open_assignments_to_others(session):
    """Verlässt ein Bewohner die WG, fallen seine offenen Aufgaben an andere."""

    a = _make_user(session, "Weg", joined_days_ago=200, last_assigned_days_ago=5)
    b = _make_user(session, "Bleibt", joined_days_ago=200, last_assigned_days_ago=50)

    seed_def = _make_definition(session, title="Seed", difficulty=10)
    # b hat den höheren Score -> a wird initial gepickt.
    _grant_points(session, b, seed_def, points=8)

    definition = _make_definition(session, title="Müll", required=1)
    occurrence = _make_occurrence(
        session, definition, period_start=date.today() + timedelta(days=3)
    )
    initial = scheduling.assign_occurrence(session, occurrence)
    assert initial[0].user_id == a.id

    moved = scheduling.reassign_all_open_for(session, a)
    assert moved == 1

    session.refresh(occurrence)
    remaining = [x for x in occurrence.assignments if x.status == AssignmentStatus.OPEN]
    assert len(remaining) == 1
    assert remaining[0].user_id == b.id


# ---------------------------------------------------------------------------
# Fallback-Abwesenheit verzerrt last_assigned_at nicht
# ---------------------------------------------------------------------------


def test_fallback_during_absence_does_not_update_last_assigned(session):
    """Notnagel-Zuweisung an Abwesende lässt last_assigned_at unangetastet."""

    period_start = date.today() + timedelta(days=2)
    period_end = period_start + timedelta(days=6)

    u = _make_user(session, "Allein", joined_days_ago=200, last_assigned_days_ago=20)
    before = u.last_assigned_at
    session.add(Absence(user_id=u.id, start_date=period_start, end_date=period_end))
    session.flush()

    definition = _make_definition(session, required=1)
    occurrence = _make_occurrence(session, definition, period_start=period_start)
    assignments = scheduling.assign_occurrence(session, occurrence)

    assert len(assignments) == 1
    assert assignments[0].assigned_during_absence is True
    assert u.last_assigned_at == before  # NICHT aktualisiert


def test_present_assignment_does_update_last_assigned(session):
    """Gegenprobe: eine reguläre (anwesende) Zuweisung setzt last_assigned_at."""

    u = _make_user(session, "Anwesend", joined_days_ago=200, last_assigned_days_ago=20)
    before = u.last_assigned_at

    definition = _make_definition(session, required=1)
    occurrence = _make_occurrence(
        session, definition, period_start=date.today() + timedelta(days=1)
    )
    assignments = scheduling.assign_occurrence(session, occurrence)

    assert assignments[0].assigned_during_absence is False
    assert u.last_assigned_at != before
    assert u.last_assigned_at > before


# ---------------------------------------------------------------------------
# Hard-Delete eines Users mit Assignment crasht nicht (passive_deletes)
# ---------------------------------------------------------------------------


def test_hard_delete_user_with_assignment_does_not_crash(session):
    """session.delete(user) mit offener Zuweisung darf nicht mit IntegrityError
    crashen (passive_deletes überlässt das Aufräumen der DB)."""

    u = _make_user(session, "HardDel", joined_days_ago=200)
    definition = _make_definition(session, required=1)
    occurrence = _make_occurrence(
        session, definition, period_start=date.today() + timedelta(days=1)
    )
    scheduling.assign_occurrence(session, occurrence)

    session.delete(u)
    session.flush()  # vor dem Fix: IntegrityError (user_id NOT NULL)

    assert session.get(User, u.id) is None


def test_reassign_all_open_for_leaves_occurrence_empty_when_no_replacement(session):
    """Ist der entfernte Bewohner der einzige Kandidat, bleibt die Occurrence
    unbesetzt — sie fällt NICHT an die entfernte Person zurück."""

    solo = _make_user(session, "Einzig", joined_days_ago=200)
    definition = _make_definition(session, required=1)
    occurrence = _make_occurrence(
        session, definition, period_start=date.today() + timedelta(days=2)
    )
    [initial] = scheduling.assign_occurrence(session, occurrence)
    assert initial.user_id == solo.id

    moved = scheduling.reassign_all_open_for(session, solo)
    assert moved == 1

    session.refresh(occurrence)
    open_assignments = [
        x for x in occurrence.assignments if x.status == AssignmentStatus.OPEN
    ]
    assert open_assignments == []  # niemand sonst -> leer, nicht zurück an solo


def test_reassign_fallback_during_absence_does_not_update_last_assigned(session):
    """Wie der assign_occurrence-Fall, aber über reassign_open_overlap: weicht der
    Re-Pick auf einen Abwesenden aus, bleibt dessen last_assigned_at unangetastet."""

    period_start = date.today() + timedelta(days=2)
    period_end = period_start + timedelta(days=6)

    a = _make_user(session, "Initial", joined_days_ago=200, last_assigned_days_ago=5)
    b = _make_user(session, "FallbackAbw", joined_days_ago=200, last_assigned_days_ago=30)
    seed_def = _make_definition(session, title="Seed", difficulty=10)
    _grant_points(session, b, seed_def, points=8)  # b höherer Score -> a initial gepickt

    definition = _make_definition(session, title="Müll", required=1)
    occurrence = _make_occurrence(session, definition, period_start=period_start)
    [initial] = scheduling.assign_occurrence(session, occurrence)
    assert initial.user_id == a.id

    # Sowohl a (zugewiesen) als auch der einzige Ersatz b werden abwesend ->
    # reassign muss auf den abwesenden b ausweichen (Fallback).
    session.add(Absence(user_id=a.id, start_date=period_start, end_date=period_end))
    session.add(Absence(user_id=b.id, start_date=period_start, end_date=period_end))
    session.flush()
    b_before = b.last_assigned_at

    moved = scheduling.reassign_open_overlap(session, a, period_start, period_end)
    assert moved == 1

    session.refresh(occurrence)
    open_assignments = [
        x for x in occurrence.assignments if x.status == AssignmentStatus.OPEN
    ]
    assert len(open_assignments) == 1
    assert open_assignments[0].user_id == b.id
    assert open_assignments[0].assigned_during_absence is True
    assert b.last_assigned_at == b_before  # Fallback -> NICHT aktualisiert


# ---------------------------------------------------------------------------
# Dienst-Ausnahme bei Abwesenheit (skip_dienst)
# ---------------------------------------------------------------------------


def test_reassign_skips_dienst_on_absence(session):
    """Abwesenheit: AUFGABE wird umverteilt, DIENST bleibt bei der Person."""

    a = _make_user(session, "Weg", joined_days_ago=200)
    b = _make_user(session, "Da", joined_days_ago=200)

    period_start = date.today() + timedelta(days=2)
    period_end = period_start + timedelta(days=6)

    aufgabe_def = _make_definition(session, title="Spülen", kind=TaskKind.AUFGABE, required=1)
    dienst_def = _make_definition(session, title="Mülldienst", kind=TaskKind.DIENST, required=1)
    aufgabe_occ = _make_occurrence(session, aufgabe_def, period_start=period_start)
    dienst_occ = _make_occurrence(session, dienst_def, period_start=period_start)
    _make_assignment(session, aufgabe_occ, a, status=AssignmentStatus.OPEN)
    _make_assignment(session, dienst_occ, a, status=AssignmentStatus.OPEN)

    session.add(Absence(user_id=a.id, start_date=period_start, end_date=period_end))
    session.flush()

    moved = scheduling.reassign_open_overlap(
        session, a, period_start, period_end, skip_dienst=True
    )

    assert moved == 1  # nur die AUFGABE wurde angefasst
    session.refresh(aufgabe_occ)
    session.refresh(dienst_occ)
    aufgabe_owners = [
        x.user_id for x in aufgabe_occ.assignments if x.status == AssignmentStatus.OPEN
    ]
    dienst_owners = [
        x.user_id for x in dienst_occ.assignments if x.status == AssignmentStatus.OPEN
    ]
    assert aufgabe_owners == [b.id]  # AUFGABE wanderte zu B
    assert dienst_owners == [a.id]  # DIENST blieb bei A


def test_removal_reassigns_dienst_too(session):
    """Entfernen (dauerhaft) verteilt auch einen DIENST um — anders als Abwesenheit."""

    a = _make_user(session, "Geht", joined_days_ago=200)
    b = _make_user(session, "Bleibt", joined_days_ago=200)

    dienst_def = _make_definition(session, title="Mülldienst", kind=TaskKind.DIENST, required=1)
    dienst_occ = _make_occurrence(
        session, dienst_def, period_start=date.today() + timedelta(days=2)
    )
    _make_assignment(session, dienst_occ, a, status=AssignmentStatus.OPEN)

    moved = scheduling.reassign_all_open_for(session, a)

    assert moved == 1
    session.refresh(dienst_occ)
    owners = [x.user_id for x in dienst_occ.assignments if x.status == AssignmentStatus.OPEN]
    assert owners == [b.id]  # Dienst wanderte zu B (Entfernen ≠ Abwesenheit)


# ---------------------------------------------------------------------------
# Vorlauf-Garantie: mindestens 1 Woche im Voraus (MIN_NOTICE_DAYS)
# ---------------------------------------------------------------------------


def test_daily_materializes_at_least_one_week(session):
    """DAILY mit Default-Args deckt lückenlos [today, today+7] ab (8 Termine)."""

    _make_user(session, "Eins", joined_days_ago=200)
    definition = _make_definition(
        session, title="Küche", recurrence=Recurrence.DAILY, required=1
    )

    scheduling.generate_occurrences(session)  # defaults: lookahead 2, min_notice 7

    starts = {
        o.period_start
        for o in session.query(TaskOccurrence)
        .filter(TaskOccurrence.task_definition_id == definition.id)
        .all()
    }
    today = date.today()
    assert starts == {today + timedelta(days=i) for i in range(8)}


def test_weekly_default_lookahead_unchanged(session):
    """WEEKLY mit Default → weiterhin genau 2 Perioden; idempotent (Regression)."""

    _make_user(session, "Eins", joined_days_ago=200)
    _make_definition(
        session, title="Müll", recurrence=Recurrence.WEEKLY, anchor_weekday=0
    )

    first = scheduling.generate_occurrences(session)
    second = scheduling.generate_occurrences(session)

    assert first == 2
    assert second == 0


def test_custom_short_interval_covers_week(session):
    """CUSTOM interval=2: der Horizont deckt die Woche ab (bis ≥ today+6)."""

    _make_user(session, "Eins", joined_days_ago=200)
    definition = _make_definition(
        session, title="Gießen", recurrence=Recurrence.CUSTOM, interval_days=2
    )

    scheduling.generate_occurrences(session)

    starts = sorted(
        o.period_start
        for o in session.query(TaskOccurrence)
        .filter(TaskOccurrence.task_definition_id == definition.id)
        .all()
    )
    today = date.today()
    assert today in starts
    assert max(starts) >= today + timedelta(days=6)


def test_long_custom_interval_keeps_period_floor(session):
    """CUSTOM interval=30: der Perioden-Floor (lookahead=2) hält 2 Termine,
    der 7-Tage-Horizont erzeugt keine Phantom-Periode."""

    _make_user(session, "Eins", joined_days_ago=200)
    definition = _make_definition(
        session, title="Großputz", recurrence=Recurrence.CUSTOM, interval_days=30
    )

    created = scheduling.generate_occurrences(session)

    assert created == 2
    starts = sorted(
        o.period_start
        for o in session.query(TaskOccurrence)
        .filter(TaskOccurrence.task_definition_id == definition.id)
        .all()
    )
    today = date.today()
    assert starts == [today, today + timedelta(days=30)]
