"""Tests für die Karma-Erweiterung (Ehrenpunkte + Negativ-Karma + Cap).

Eigene Datei, damit ``test_scheduling.py`` (aktiv in Bearbeitung) unangetastet
bleibt. Läuft wie die übrigen Tests gegen In-Memory-SQLite.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from flask import Flask
from sqlalchemy.orm import Session

from app import create_app
from app.domain.enums import (
    AssignmentStatus,
    KarmaKind,
    Recurrence,
    Role,
    TaskStatus,
    UserStatus,
)
from app.domain.points import (
    MAX_OPEN_ASSIGNMENTS_PER_USER,
    SOFT_CAP_OPEN_ASSIGNMENTS,
)
from app.extensions import db
from app.models.karma import KarmaEvent
from app.models.task import TaskAssignment, TaskDefinition, TaskOccurrence
from app.models.user import User, UserRole
from app.services import scheduling

# ---------------------------------------------------------------------------
# Fixtures + Helpers
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


_PERIOD_OFFSET = {"value": 0}


def _make_user(
    session: Session,
    name: str,
    *,
    joined_days_ago: int = 200,
    last_assigned_days_ago: int | None = None,
    role: Role = Role.HAUSBEWOHNER,
) -> User:
    now = datetime.now(UTC)
    user = User(
        email=f"{name.lower()}@example.com",
        name=name,
        status=UserStatus.APPROVED,
        joined_at=now - timedelta(days=joined_days_ago),
        last_assigned_at=(
            now - timedelta(days=last_assigned_days_ago)
            if last_assigned_days_ago is not None
            else None
        ),
    )
    session.add(user)
    session.flush()
    session.add(UserRole(user_id=user.id, role=role))
    session.flush()
    return user


def _make_definition(
    session: Session, *, difficulty: int = 4, required: int = 1
) -> TaskDefinition:
    definition = TaskDefinition(
        title="Putzen",
        difficulty_points=difficulty,
        recurrence=Recurrence.WEEKLY,
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
    period_start: date | None = None,
    period_length: int = 7,
) -> TaskOccurrence:
    if period_start is None:
        _PERIOD_OFFSET["value"] += 1
        period_start = date.today() + timedelta(days=_PERIOD_OFFSET["value"])
    period_end = period_start + timedelta(days=period_length - 1)
    occurrence = TaskOccurrence(
        task_definition_id=definition.id,
        period_start=period_start,
        period_end=period_end,
        due_date=period_start,
        status=TaskStatus.OPEN,
    )
    session.add(occurrence)
    session.flush()
    occurrence.task_definition = definition
    return occurrence


def _open_assignment(
    session: Session, definition: TaskDefinition, user: User
) -> TaskAssignment:
    occurrence = _make_occurrence(session, definition)
    assignment = TaskAssignment(
        occurrence_id=occurrence.id,
        user_id=user.id,
        status=AssignmentStatus.OPEN,
    )
    session.add(assignment)
    session.flush()
    return assignment


# ---------------------------------------------------------------------------
# Ehrenpunkte / Strafen -> Score
# ---------------------------------------------------------------------------


def test_award_honor_raises_score(session):
    user = _make_user(session, "Held", joined_days_ago=200)
    scheduling.award_honor(session, user, 10, by_user=None, note="Keller entrümpelt")

    # 10 / max(200, 90) * 90
    assert scheduling.effective_score(session, user) == pytest.approx(10 / 200 * 90)


def test_record_penalty_makes_score_negative(session):
    user = _make_user(session, "Schlampe", joined_days_ago=200)
    scheduling.record_penalty(session, user, 10, note="Müll vergessen")

    assert scheduling.effective_score(session, user) == pytest.approx(-10 / 200 * 90)


def test_honor_offsets_penalty(session):
    user = _make_user(session, "Gemischt", joined_days_ago=200)
    scheduling.award_honor(session, user, 10)
    scheduling.record_penalty(session, user, 4)

    # raw = 10 - 4 = 6
    assert scheduling.effective_score(session, user) == pytest.approx(6 / 200 * 90)


def test_penalty_decays_after_40_days(session):
    user = _make_user(session, "Altschuld", joined_days_ago=200)
    old = datetime.now(UTC) - timedelta(days=41)
    session.add(
        KarmaEvent(user_id=user.id, kind=KarmaKind.PENALTY, points=10, occurred_at=old)
    )
    session.flush()

    assert scheduling.effective_score(session, user) == pytest.approx(0.0)


def test_penalty_within_40_days_still_counts(session):
    user = _make_user(session, "Frischschuld", joined_days_ago=200)
    recent = datetime.now(UTC) - timedelta(days=39)
    session.add(
        KarmaEvent(user_id=user.id, kind=KarmaKind.PENALTY, points=10, occurred_at=recent)
    )
    session.flush()

    assert scheduling.effective_score(session, user) == pytest.approx(-10 / 200 * 90)


def test_honor_decays_after_80_days(session):
    user = _make_user(session, "Altruhm", joined_days_ago=200)
    old = datetime.now(UTC) - timedelta(days=81)
    session.add(
        KarmaEvent(user_id=user.id, kind=KarmaKind.HONOR, points=10, occurred_at=old)
    )
    session.flush()

    assert scheduling.effective_score(session, user) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# REJECTED-Review bucht Penalty
# ---------------------------------------------------------------------------


def test_review_rejected_books_penalty_event(session):
    reviewer = _make_user(session, "Hauswart", role=Role.HAUSWART)
    user = _make_user(session, "Bewohner", joined_days_ago=200)

    definition = _make_definition(session, difficulty=6, required=1)
    occurrence = _make_occurrence(session, definition)
    assignment = TaskAssignment(
        occurrence_id=occurrence.id,
        user_id=user.id,
        status=AssignmentStatus.DONE,
        points_earned=6,
        completed_at=datetime.now(UTC) - timedelta(days=1),
    )
    session.add(assignment)
    session.flush()

    scheduling.review_assignment(session, assignment, reviewer, approved=False, note="Pfusch")

    penalties = (
        session.query(KarmaEvent)
        .filter(KarmaEvent.user_id == user.id, KarmaEvent.kind == KarmaKind.PENALTY)
        .all()
    )
    assert len(penalties) == 1
    assert penalties[0].points == 6  # difficulty 6 / 1 assignee
    assert penalties[0].created_by_id == reviewer.id


def test_review_rejected_twice_does_not_double_penalty(session):
    reviewer = _make_user(session, "Hauswart", role=Role.HAUSWART)
    user = _make_user(session, "Bewohner", joined_days_ago=200)

    definition = _make_definition(session, difficulty=6, required=1)
    occurrence = _make_occurrence(session, definition)
    assignment = TaskAssignment(
        occurrence_id=occurrence.id,
        user_id=user.id,
        status=AssignmentStatus.DONE,
        points_earned=6,
        completed_at=datetime.now(UTC) - timedelta(days=1),
    )
    session.add(assignment)
    session.flush()

    scheduling.review_assignment(session, assignment, reviewer, approved=False)
    scheduling.review_assignment(session, assignment, reviewer, approved=False)

    penalties = (
        session.query(KarmaEvent)
        .filter(KarmaEvent.user_id == user.id, KarmaEvent.kind == KarmaKind.PENALTY)
        .count()
    )
    assert penalties == 1


# ---------------------------------------------------------------------------
# EXCUSED-Review: neutral, keine Strafe, entfernt vorhandene
# ---------------------------------------------------------------------------


def test_excuse_books_no_penalty(session):
    reviewer = _make_user(session, "Hauswart", role=Role.HAUSWART)
    user = _make_user(session, "Bewohner", joined_days_ago=200)

    definition = _make_definition(session, difficulty=6, required=1)
    occurrence = _make_occurrence(session, definition)
    assignment = TaskAssignment(
        occurrence_id=occurrence.id,
        user_id=user.id,
        status=AssignmentStatus.OPEN,
    )
    session.add(assignment)
    session.flush()

    scheduling.excuse_assignment(session, assignment, reviewer, note="krank")

    assert assignment.status == AssignmentStatus.SKIPPED
    assert assignment.points_earned == 0
    penalties = (
        session.query(KarmaEvent)
        .filter(KarmaEvent.user_id == user.id, KarmaEvent.kind == KarmaKind.PENALTY)
        .count()
    )
    assert penalties == 0


def test_excuse_removes_existing_penalty(session):
    reviewer = _make_user(session, "Hauswart", role=Role.HAUSWART)
    user = _make_user(session, "Bewohner", joined_days_ago=200)

    definition = _make_definition(session, difficulty=6, required=1)
    occurrence = _make_occurrence(session, definition)
    assignment = TaskAssignment(
        occurrence_id=occurrence.id,
        user_id=user.id,
        status=AssignmentStatus.OPEN,
    )
    session.add(assignment)
    session.flush()

    # Cron hat bereits zugeschlagen: SKIPPED + Strafe.
    assignment.status = AssignmentStatus.SKIPPED
    scheduling.record_penalty(
        session, user, 6, note="Überfällig – nicht erledigt", occurrence=occurrence
    )
    assert (
        session.query(KarmaEvent)
        .filter(KarmaEvent.user_id == user.id, KarmaEvent.kind == KarmaKind.PENALTY)
        .count()
        == 1
    )

    scheduling.excuse_assignment(session, assignment, reviewer, note="war krank")

    assert (
        session.query(KarmaEvent)
        .filter(KarmaEvent.user_id == user.id, KarmaEvent.kind == KarmaKind.PENALTY)
        .count()
        == 0
    )


# ---------------------------------------------------------------------------
# Assignment-Cap
# ---------------------------------------------------------------------------


def test_cap_deprioritizes_user_at_limit(session):
    """Wer am OPEN-Limit ist, wird trotz besserer Fairness ans Ende gereiht."""

    # A wäre ohne Cap zuerst dran (älteres last_assigned_at, gleicher Score 0).
    a = _make_user(session, "Voll", joined_days_ago=200, last_assigned_days_ago=100)
    b = _make_user(session, "Frei", joined_days_ago=200, last_assigned_days_ago=1)

    filler = _make_definition(session, difficulty=1, required=1)
    for _ in range(MAX_OPEN_ASSIGNMENTS_PER_USER):
        _open_assignment(session, filler, a)

    definition = _make_definition(session, difficulty=4, required=1)
    occurrence = _make_occurrence(session, definition)
    [assignment] = scheduling.assign_occurrence(session, occurrence)

    assert assignment.user_id == b.id  # A ist am Cap -> B wird gewählt.


def test_cap_relaxes_when_everyone_at_limit(session):
    """Sind alle Anwesenden am Limit, wird trotzdem (statt gar nicht) zugewiesen."""

    a = _make_user(session, "Voll", joined_days_ago=200, last_assigned_days_ago=10)

    filler = _make_definition(session, difficulty=1, required=1)
    for _ in range(MAX_OPEN_ASSIGNMENTS_PER_USER):
        _open_assignment(session, filler, a)

    definition = _make_definition(session, difficulty=4, required=1)
    occurrence = _make_occurrence(session, definition)
    assignments = scheduling.assign_occurrence(session, occurrence)

    assert len(assignments) == 1
    assert assignments[0].user_id == a.id


# ---------------------------------------------------------------------------
# Überfälligkeits-Penalty
# ---------------------------------------------------------------------------


def test_apply_overdue_penalties_skips_and_penalizes(session):
    user = _make_user(session, "Vergesslich", joined_days_ago=200)
    definition = _make_definition(session, difficulty=4, required=1)

    occurrence = _make_occurrence(
        session,
        definition,
        period_start=date.today() - timedelta(days=3),
        period_length=1,
    )  # period_end = vorgestern -> überfällig.
    assignment = TaskAssignment(
        occurrence_id=occurrence.id,
        user_id=user.id,
        status=AssignmentStatus.OPEN,
    )
    session.add(assignment)
    session.flush()

    count = scheduling.apply_overdue_penalties(session)

    assert count == 1
    assert assignment.status == AssignmentStatus.SKIPPED
    assert occurrence.status == TaskStatus.SKIPPED

    penalties = (
        session.query(KarmaEvent)
        .filter(KarmaEvent.user_id == user.id, KarmaEvent.kind == KarmaKind.PENALTY)
        .all()
    )
    assert len(penalties) == 1
    assert penalties[0].points == 4

    # Idempotent: zweiter Lauf bestraft nicht erneut.
    assert scheduling.apply_overdue_penalties(session) == 0
    assert (
        session.query(KarmaEvent)
        .filter(KarmaEvent.kind == KarmaKind.PENALTY)
        .count()
        == 1
    )


def test_overdue_absence_fallback_skipped_without_penalty(session):
    """Notnagel während Abwesenheit: Occurrence wird SKIPPED, aber KEINE Strafe."""

    user = _make_user(session, "ImUrlaub", joined_days_ago=200)
    definition = _make_definition(session, difficulty=4, required=1)

    occurrence = _make_occurrence(
        session,
        definition,
        period_start=date.today() - timedelta(days=3),
        period_length=1,
    )  # überfällig
    assignment = TaskAssignment(
        occurrence_id=occurrence.id,
        user_id=user.id,
        status=AssignmentStatus.OPEN,
        assigned_during_absence=True,
    )
    session.add(assignment)
    session.flush()

    count = scheduling.apply_overdue_penalties(session)

    assert count == 0  # keine Strafe gebucht
    assert assignment.status == AssignmentStatus.SKIPPED
    assert occurrence.status == TaskStatus.SKIPPED
    assert (
        session.query(KarmaEvent)
        .filter(KarmaEvent.user_id == user.id, KarmaEvent.kind == KarmaKind.PENALTY)
        .count()
        == 0
    )


# ---------------------------------------------------------------------------
# Soft-Cap (weicher Burst-Schutz)
# ---------------------------------------------------------------------------


def test_soft_cap_deprioritizes_user_with_burst(session):
    """Wer >= SOFT_CAP OPEN hält (noch unter dem Hard-Cap), kommt hinter einen
    unbelasteten Kandidaten — selbst wenn er nach Fairness zuerst dran wäre."""

    assert SOFT_CAP_OPEN_ASSIGNMENTS < MAX_OPEN_ASSIGNMENTS_PER_USER

    # a wäre ohne Cap zuerst dran (älteres last_assigned_at, gleicher Score 0).
    a = _make_user(session, "Burst", joined_days_ago=200, last_assigned_days_ago=100)
    b = _make_user(session, "Frei", joined_days_ago=200, last_assigned_days_ago=1)

    filler = _make_definition(session, difficulty=1, required=1)
    for _ in range(SOFT_CAP_OPEN_ASSIGNMENTS):
        _open_assignment(session, filler, a)

    definition = _make_definition(session, difficulty=4, required=1)
    occurrence = _make_occurrence(session, definition)
    [assignment] = scheduling.assign_occurrence(session, occurrence)

    assert assignment.user_id == b.id  # a ist über dem Soft-Cap -> b gewählt.
