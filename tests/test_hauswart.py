"""Tests für den Hauswart-Blueprint (Review-Queue + Personen-Ansicht).

Strategie wie ``test_absences.py``: frisches In-Memory-SQLite, Schema via
``db.metadata.create_all`` nur für die benötigten Tabellen (AuditLog-JSONB
würde unter SQLite scheitern). Login wird über die Flask-Login-Session direkt
gesetzt, der OAuth-Flow also umgangen.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app import create_app
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
from app.extensions import db
from app.models.karma import KarmaEvent
from app.models.task import (
    TaskAssignment,
    TaskDefinition,
    TaskOccurrence,
)
from app.models.user import User, UserRole

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(monkeypatch: pytest.MonkeyPatch) -> Flask:
    from app.config import DevConfig

    monkeypatch.setattr(DevConfig, "SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
    application = create_app("dev")
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with application.app_context():
        tables = [
            User.__table__,
            UserRole.__table__,
            TaskDefinition.__table__,
            TaskOccurrence.__table__,
            TaskAssignment.__table__,
            KarmaEvent.__table__,
        ]
        db.metadata.create_all(bind=db.engine, tables=tables)
        yield application
        db.session.remove()
        db.metadata.drop_all(bind=db.engine, tables=tables)


@pytest.fixture()
def client(app: Flask) -> FlaskClient:
    return app.test_client()


def _mk_user(name: str, *, roles: list[Role] | None = None) -> User:
    suffix = uuid.uuid4().hex[:10]
    user = User(
        username=f"u-{suffix}",
        email=f"{suffix}@test.local",
        name=name,
        status=UserStatus.APPROVED,
        must_change_password=False,
    )
    db.session.add(user)
    db.session.flush()
    for role in roles or []:
        db.session.add(UserRole(user_id=user.id, role=role))
    db.session.commit()
    return user


def _login(client: FlaskClient, user: User) -> None:
    from flask import g

    with client.session_transaction() as sess:
        sess.clear()
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    g.pop("_login_user", None)


def _mk_assignment(
    user: User,
    *,
    kind: TaskKind = TaskKind.DIENST,
    period_start: date,
    period_end: date,
    status: AssignmentStatus = AssignmentStatus.OPEN,
    difficulty_points: int = 4,
    points_earned: int = 0,
    review_status: ReviewStatus = ReviewStatus.PENDING,
    recurrence: Recurrence = Recurrence.NONE,
    required_assignees: int = 1,
    title: str | None = None,
) -> TaskAssignment:
    definition = TaskDefinition(
        title=title or f"Task-{uuid.uuid4().hex[:6]}",
        kind=kind,
        difficulty_points=difficulty_points,
        recurrence=recurrence,
        required_assignees=required_assignees,
    )
    db.session.add(definition)
    db.session.flush()

    occurrence = TaskOccurrence(
        task_definition_id=definition.id,
        period_start=period_start,
        period_end=period_end,
        due_date=period_start,
        status=TaskStatus.DONE if status == AssignmentStatus.DONE else TaskStatus.OPEN,
    )
    db.session.add(occurrence)
    db.session.flush()

    assignment = TaskAssignment(
        occurrence_id=occurrence.id,
        user_id=user.id,
        status=status,
        points_earned=points_earned,
        completed_at=datetime.now(UTC) if status == AssignmentStatus.DONE else None,
        review_status=review_status,
    )
    db.session.add(assignment)
    db.session.commit()
    return assignment


# ---------------------------------------------------------------------------
# Role-Gate
# ---------------------------------------------------------------------------


def test_hausbewohner_gets_403_on_index(app: Flask, client: FlaskClient) -> None:
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    _login(client, resident)
    assert client.get("/hauswart/").status_code == 403


def test_hausbewohner_gets_403_on_user_detail(app: Flask, client: FlaskClient) -> None:
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    other = _mk_user("Otto", roles=[Role.HAUSBEWOHNER])
    _login(client, resident)
    assert client.get(f"/hauswart/user/{other.id}").status_code == 403


def test_hauswart_gets_200_on_index(app: Flask, client: FlaskClient) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSBEWOHNER, Role.HAUSWART])
    _login(client, hw)
    assert client.get("/hauswart/").status_code == 200


def test_review_queue_count_returns_pending(app: Flask, client: FlaskClient) -> None:
    from app.services.scheduling import review_queue_count

    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    # DIENST, Periode vorbei, PENDING -> zählt.
    _mk_assignment(
        resident,
        kind=TaskKind.DIENST,
        period_start=date.today() - timedelta(days=8),
        period_end=date.today() - timedelta(days=1),
        status=AssignmentStatus.DONE,
        review_status=ReviewStatus.PENDING,
        points_earned=4,
    )
    # AUFGABE überfällig OPEN -> zählt.
    _mk_assignment(
        resident,
        kind=TaskKind.AUFGABE,
        period_start=date.today() - timedelta(days=3),
        period_end=date.today() - timedelta(days=2),
        status=AssignmentStatus.OPEN,
        review_status=ReviewStatus.PENDING,
    )
    # Schon APPROVED -> zählt NICHT.
    _mk_assignment(
        resident,
        kind=TaskKind.DIENST,
        period_start=date.today() - timedelta(days=8),
        period_end=date.today() - timedelta(days=1),
        status=AssignmentStatus.DONE,
        review_status=ReviewStatus.APPROVED,
        points_earned=4,
    )
    count = review_queue_count(db.session)
    assert count == 2


def test_pending_review_count_in_context(app: Flask, client: FlaskClient) -> None:
    """Context-Processor injiziert pending_review_count im Hauswart-Login."""
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    _mk_assignment(
        resident,
        kind=TaskKind.DIENST,
        period_start=date.today() - timedelta(days=8),
        period_end=date.today() - timedelta(days=1),
        status=AssignmentStatus.DONE,
        review_status=ReviewStatus.PENDING,
        points_earned=4,
    )
    _login(client, hw)
    resp = client.get("/hauswart/")
    assert resp.status_code == 200
    # Badge im Nav sollte gerendert sein.
    assert b"Verwaltung" in resp.data


def test_score_full_points_acts_like_approve(app: Flask, client: FlaskClient) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    assignment = _mk_assignment(
        resident,
        period_start=date.today() - timedelta(days=5),
        period_end=date.today() - timedelta(days=1),
        status=AssignmentStatus.DONE,
        difficulty_points=4,
    )
    _login(client, hw)
    resp = client.post(
        f"/hauswart/{assignment.id}/score",
        data={"points_earned": "4"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    refreshed = db.session.get(TaskAssignment, assignment.id)
    assert refreshed.review_status == ReviewStatus.APPROVED
    assert refreshed.points_earned == 4
    # Keine PENALTY-Karma.
    penalties = (
        db.session.query(KarmaEvent)
        .filter_by(user_id=resident.id, kind=KarmaKind.PENALTY)
        .count()
    )
    assert penalties == 0


def test_score_zero_points_acts_like_reject(app: Flask, client: FlaskClient) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    assignment = _mk_assignment(
        resident,
        period_start=date.today() - timedelta(days=5),
        period_end=date.today() - timedelta(days=1),
        status=AssignmentStatus.DONE,
        difficulty_points=4,
        points_earned=4,
    )
    _login(client, hw)
    resp = client.post(
        f"/hauswart/{assignment.id}/score",
        data={"points_earned": "0", "note": "war Dreck"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    refreshed = db.session.get(TaskAssignment, assignment.id)
    assert refreshed.review_status == ReviewStatus.REJECTED
    assert refreshed.points_earned == 0
    assert refreshed.review_note == "war Dreck"
    # PENALTY in voller Höhe.
    penalty = (
        db.session.query(KarmaEvent)
        .filter_by(user_id=resident.id, kind=KarmaKind.PENALTY)
        .first()
    )
    assert penalty is not None
    assert penalty.points == 4


def test_score_partial_points_creates_gap_penalty(app: Flask, client: FlaskClient) -> None:
    """Teilpunkte: 2 von 4 → 2 Punkte gutgeschrieben + 2 Strafe."""
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    assignment = _mk_assignment(
        resident,
        period_start=date.today() - timedelta(days=5),
        period_end=date.today() - timedelta(days=1),
        status=AssignmentStatus.DONE,
        difficulty_points=4,
        points_earned=4,
    )
    _login(client, hw)
    resp = client.post(
        f"/hauswart/{assignment.id}/score",
        data={"points_earned": "2", "note": "nur halb"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    refreshed = db.session.get(TaskAssignment, assignment.id)
    assert refreshed.review_status == ReviewStatus.APPROVED
    assert refreshed.points_earned == 2
    # PENALTY = 4 - 2 = 2.
    penalty = (
        db.session.query(KarmaEvent)
        .filter_by(user_id=resident.id, kind=KarmaKind.PENALTY)
        .first()
    )
    assert penalty is not None
    assert penalty.points == 2


def test_score_rescoring_removes_old_penalty(app: Flask, client: FlaskClient) -> None:
    """Mehrfache Bewertung: alte PENALTY wird wegräumt, neue gebucht."""
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    assignment = _mk_assignment(
        resident,
        period_start=date.today() - timedelta(days=5),
        period_end=date.today() - timedelta(days=1),
        status=AssignmentStatus.DONE,
        difficulty_points=4,
        points_earned=4,
    )
    _login(client, hw)
    # Erst: 0 Punkte → PENALTY 4.
    client.post(
        f"/hauswart/{assignment.id}/score",
        data={"points_earned": "0"},
        headers={"HX-Request": "true"},
    )
    # Dann: 3 Punkte → PENALTY 1.
    client.post(
        f"/hauswart/{assignment.id}/score",
        data={"points_earned": "3"},
        headers={"HX-Request": "true"},
    )
    penalties = (
        db.session.query(KarmaEvent)
        .filter_by(user_id=resident.id, kind=KarmaKind.PENALTY)
        .all()
    )
    assert len(penalties) == 1
    assert penalties[0].points == 1


def test_archive_route_lists_reviewed_items(app: Flask, client: FlaskClient) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    # Ein APPROVED-Item, ein REJECTED-Item, ein PENDING (sollte NICHT erscheinen).
    a_approved = _mk_assignment(
        resident,
        kind=TaskKind.DIENST,
        period_start=date.today() - timedelta(days=8),
        period_end=date.today() - timedelta(days=1),
        status=AssignmentStatus.DONE,
        review_status=ReviewStatus.APPROVED,
        points_earned=4,
    )
    a_approved.reviewed_at = datetime.now(UTC) - timedelta(days=1)
    a_rejected = _mk_assignment(
        resident,
        kind=TaskKind.DIENST,
        period_start=date.today() - timedelta(days=10),
        period_end=date.today() - timedelta(days=3),
        status=AssignmentStatus.DONE,
        review_status=ReviewStatus.REJECTED,
        points_earned=0,
    )
    a_rejected.reviewed_at = datetime.now(UTC) - timedelta(days=2)
    _mk_assignment(
        resident,
        kind=TaskKind.DIENST,
        period_start=date.today() - timedelta(days=5),
        period_end=date.today() - timedelta(days=1),
        status=AssignmentStatus.DONE,
        review_status=ReviewStatus.PENDING,
        points_earned=4,
    )
    db.session.commit()

    _login(client, hw)
    resp = client.get("/hauswart/archiv")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", "replace")
    # Beide bewerteten Items sind im Archiv.
    assert "genehmigt" in body or "Genehmigt" in body
    assert "abgelehnt" in body or "Abgelehnt" in body


def test_archive_filters_by_status(app: Flask, client: FlaskClient) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    a_approved = _mk_assignment(
        resident,
        kind=TaskKind.DIENST,
        period_start=date.today() - timedelta(days=8),
        period_end=date.today() - timedelta(days=1),
        status=AssignmentStatus.DONE,
        review_status=ReviewStatus.APPROVED,
        points_earned=4,
        title="DienstA",
    )
    a_approved.reviewed_at = datetime.now(UTC) - timedelta(days=1)
    a_rejected = _mk_assignment(
        resident,
        kind=TaskKind.DIENST,
        period_start=date.today() - timedelta(days=10),
        period_end=date.today() - timedelta(days=3),
        status=AssignmentStatus.DONE,
        review_status=ReviewStatus.REJECTED,
        points_earned=0,
        title="DienstR",
    )
    a_rejected.reviewed_at = datetime.now(UTC) - timedelta(days=2)
    db.session.commit()

    _login(client, hw)
    resp = client.get("/hauswart/archiv?status=APPROVED")
    body = resp.data.decode("utf-8", "replace")
    assert "DienstA" in body
    assert "DienstR" not in body


def test_review_queue_count_excludes_excused_and_old_window(
    app: Flask, client: FlaskClient
) -> None:
    """Excused-Items und außerhalb des 7-Tage-Fensters zählen nicht."""
    from app.services.scheduling import review_queue_count

    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    # EXCUSED -> nicht gezählt.
    _mk_assignment(
        resident,
        kind=TaskKind.DIENST,
        period_start=date.today() - timedelta(days=5),
        period_end=date.today() - timedelta(days=1),
        status=AssignmentStatus.DONE,
        review_status=ReviewStatus.EXCUSED,
        points_earned=0,
    )
    # Außerhalb des 7-Tage-Fensters -> nicht gezählt.
    _mk_assignment(
        resident,
        kind=TaskKind.DIENST,
        period_start=date.today() - timedelta(days=30),
        period_end=date.today() - timedelta(days=20),
        status=AssignmentStatus.DONE,
        review_status=ReviewStatus.PENDING,
        points_earned=4,
    )
    count = review_queue_count(db.session)
    assert count == 0


def test_admin_gets_200_on_user_detail(app: Flask, client: FlaskClient) -> None:
    admin = _mk_user("Adi", roles=[Role.ADMIN])
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    _login(client, admin)
    assert client.get(f"/hauswart/user/{resident.id}").status_code == 200


# ---------------------------------------------------------------------------
# Mutationen
# ---------------------------------------------------------------------------


def test_approve_sets_review_status_approved(app: Flask, client: FlaskClient) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    assignment = _mk_assignment(
        resident,
        period_start=date.today() - timedelta(days=14),
        period_end=date.today() - timedelta(days=1),
        status=AssignmentStatus.DONE,
        points_earned=4,
    )
    _login(client, hw)

    resp = client.post(
        f"/hauswart/{assignment.id}/approve",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    refreshed = db.session.get(TaskAssignment, assignment.id)
    assert refreshed.review_status == ReviewStatus.APPROVED
    assert refreshed.reviewed_by_id == hw.id
    assert refreshed.points_earned == 4


def test_reject_sets_rejected_stores_note_and_zeroes_points(
    app: Flask, client: FlaskClient
) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    assignment = _mk_assignment(
        resident,
        period_start=date.today() - timedelta(days=14),
        period_end=date.today() - timedelta(days=1),
        status=AssignmentStatus.DONE,
        points_earned=4,
    )
    _login(client, hw)

    resp = client.post(
        f"/hauswart/{assignment.id}/reject",
        data={"note": "Bad nicht geputzt"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    refreshed = db.session.get(TaskAssignment, assignment.id)
    assert refreshed.review_status == ReviewStatus.REJECTED
    assert refreshed.review_note == "Bad nicht geputzt"
    assert refreshed.points_earned == 0
    assert refreshed.reviewed_by_id == hw.id


def test_mark_done_sets_done_and_approved(app: Flask, client: FlaskClient) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    assignment = _mk_assignment(
        resident,
        period_start=date.today() - timedelta(days=14),
        period_end=date.today() - timedelta(days=1),
        status=AssignmentStatus.OPEN,
        difficulty_points=6,
    )
    _login(client, hw)

    resp = client.post(
        f"/hauswart/{assignment.id}/mark-done",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    refreshed = db.session.get(TaskAssignment, assignment.id)
    assert refreshed.status == AssignmentStatus.DONE
    assert refreshed.review_status == ReviewStatus.APPROVED
    assert refreshed.points_earned == 6
    assert refreshed.completed_at is not None


def test_excuse_done_sets_excused_zeroes_points(
    app: Flask, client: FlaskClient
) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    assignment = _mk_assignment(
        resident,
        period_start=date.today() - timedelta(days=14),
        period_end=date.today() - timedelta(days=1),
        status=AssignmentStatus.DONE,
        points_earned=4,
    )
    _login(client, hw)

    resp = client.post(
        f"/hauswart/{assignment.id}/excuse",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    refreshed = db.session.get(TaskAssignment, assignment.id)
    assert refreshed.review_status == ReviewStatus.EXCUSED
    assert refreshed.points_earned == 0
    assert refreshed.reviewed_by_id == hw.id


def test_excuse_open_overdue_sets_skipped(app: Flask, client: FlaskClient) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    assignment = _mk_assignment(
        resident,
        period_start=date.today() - timedelta(days=14),
        period_end=date.today() - timedelta(days=1),
        status=AssignmentStatus.OPEN,
    )
    _login(client, hw)

    resp = client.post(
        f"/hauswart/{assignment.id}/excuse",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    refreshed = db.session.get(TaskAssignment, assignment.id)
    assert refreshed.status == AssignmentStatus.SKIPPED
    assert refreshed.review_status == ReviewStatus.EXCUSED
    # Occurrence ohne andere Assignments → vollständig SKIPPED.
    assert refreshed.occurrence.status == TaskStatus.SKIPPED


def test_excuse_stores_note(app: Flask, client: FlaskClient) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    assignment = _mk_assignment(
        resident,
        period_start=date.today() - timedelta(days=14),
        period_end=date.today() - timedelta(days=1),
        status=AssignmentStatus.OPEN,
    )
    _login(client, hw)

    resp = client.post(
        f"/hauswart/{assignment.id}/excuse",
        data={"note": "krank"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    refreshed = db.session.get(TaskAssignment, assignment.id)
    assert refreshed.review_note == "krank"


def test_excuse_creates_no_penalty(app: Flask, client: FlaskClient) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    assignment = _mk_assignment(
        resident,
        period_start=date.today() - timedelta(days=14),
        period_end=date.today() - timedelta(days=1),
        status=AssignmentStatus.OPEN,
    )
    _login(client, hw)

    client.post(
        f"/hauswart/{assignment.id}/excuse",
        headers={"HX-Request": "true"},
    )
    penalties = (
        db.session.query(KarmaEvent)
        .filter(
            KarmaEvent.user_id == resident.id,
            KarmaEvent.kind == KarmaKind.PENALTY,
        )
        .all()
    )
    assert penalties == []


def test_excuse_removes_existing_penalty(app: Flask, client: FlaskClient) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    assignment = _mk_assignment(
        resident,
        period_start=date.today() - timedelta(days=14),
        period_end=date.today() - timedelta(days=1),
        status=AssignmentStatus.SKIPPED,
    )
    # Cron-Simulation: der Überfällig-Job hat bereits eine Strafe gebucht.
    db.session.add(
        KarmaEvent(
            user_id=resident.id,
            kind=KarmaKind.PENALTY,
            points=4,
            note="Überfällig – nicht erledigt",
            occurrence_id=assignment.occurrence_id,
        )
    )
    db.session.commit()
    _login(client, hw)

    resp = client.post(
        f"/hauswart/{assignment.id}/excuse",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    refreshed = db.session.get(TaskAssignment, assignment.id)
    assert refreshed.review_status == ReviewStatus.EXCUSED
    penalties = (
        db.session.query(KarmaEvent)
        .filter(
            KarmaEvent.user_id == resident.id,
            KarmaEvent.kind == KarmaKind.PENALTY,
        )
        .all()
    )
    assert penalties == []


def test_non_htmx_mutation_redirects(app: Flask, client: FlaskClient) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    assignment = _mk_assignment(
        resident,
        period_start=date.today() - timedelta(days=14),
        period_end=date.today() - timedelta(days=1),
        status=AssignmentStatus.DONE,
        points_earned=4,
    )
    _login(client, hw)

    resp = client.post(f"/hauswart/{assignment.id}/approve")
    assert resp.status_code in (302, 303)
    assert "/hauswart" in resp.headers["Location"]


def test_mutation_404_for_unknown_assignment(app: Flask, client: FlaskClient) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    _login(client, hw)
    resp = client.post(f"/hauswart/{uuid.uuid4()}/approve")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Queue-Inhalt
# ---------------------------------------------------------------------------


def test_index_shows_past_dienst_not_plain_done_aufgabe(
    app: Flask, client: FlaskClient
) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])

    # Ein beendeter Dienst (PENDING) → muss in der Queue erscheinen.
    dienst = _mk_assignment(
        resident,
        kind=TaskKind.DIENST,
        period_start=date.today() - timedelta(days=14),
        period_end=date.today() - timedelta(days=1),
        status=AssignmentStatus.DONE,
        points_earned=4,
    )
    dienst_def = db.session.get(TaskAssignment, dienst.id).occurrence.task_definition
    dienst_def.title = "Muelldienst"
    db.session.commit()

    # Eine simple abgehakte Aufgabe → darf NICHT in der Queue erscheinen.
    aufgabe = _mk_assignment(
        resident,
        kind=TaskKind.AUFGABE,
        period_start=date.today() - timedelta(days=3),
        period_end=date.today() - timedelta(days=1),
        status=AssignmentStatus.DONE,
        points_earned=2,
    )
    aufgabe_def = db.session.get(TaskAssignment, aufgabe.id).occurrence.task_definition
    aufgabe_def.title = "Schnellaufgabe"
    db.session.commit()

    _login(client, hw)
    body = client.get("/hauswart/").get_data(as_text=True)
    assert "Muelldienst" in body
    assert "Schnellaufgabe" not in body


def test_index_shows_overdue_unclaimed_aufgabe(app: Flask, client: FlaskClient) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])

    overdue = _mk_assignment(
        resident,
        kind=TaskKind.AUFGABE,
        period_start=date.today() - timedelta(days=5),
        period_end=date.today() - timedelta(days=2),
        status=AssignmentStatus.OPEN,
    )
    overdue_def = db.session.get(TaskAssignment, overdue.id).occurrence.task_definition
    overdue_def.title = "Vergessene Aufgabe"
    db.session.commit()

    _login(client, hw)
    body = client.get("/hauswart/").get_data(as_text=True)
    assert "Vergessene Aufgabe" in body
    assert "nicht beansprucht" in body


# ---------------------------------------------------------------------------
# Filter & Sort
# ---------------------------------------------------------------------------


def test_index_filter_by_user_shows_only_that_user(
    app: Flask, client: FlaskClient
) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    jonas = _mk_user("Jonas", roles=[Role.HAUSBEWOHNER])
    michael = _mk_user("Michael", roles=[Role.HAUSBEWOHNER])

    jonas_assignment = _mk_assignment(
        jonas,
        kind=TaskKind.DIENST,
        period_start=date.today() - timedelta(days=10),
        period_end=date.today() - timedelta(days=3),
        status=AssignmentStatus.DONE,
        points_earned=4,
        title="Jonas-Muelldienst",
    )
    michael_assignment = _mk_assignment(
        michael,
        kind=TaskKind.DIENST,
        period_start=date.today() - timedelta(days=10),
        period_end=date.today() - timedelta(days=3),
        status=AssignmentStatus.DONE,
        points_earned=4,
        title="Michael-Putzdienst",
    )
    # Touch variables to avoid unused warnings.
    assert jonas_assignment.id and michael_assignment.id

    _login(client, hw)
    body = client.get(f"/hauswart/?user_id={jonas.id}").get_data(as_text=True)
    assert "Jonas-Muelldienst" in body
    assert "Michael-Putzdienst" not in body


def test_index_filter_invalid_user_id_is_ignored(
    app: Flask, client: FlaskClient
) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    _login(client, hw)
    resp = client.get("/hauswart/?user_id=not-a-uuid")
    assert resp.status_code == 200


def test_index_sort_by_type_puts_dienst_before_wiederholend(
    app: Flask, client: FlaskClient
) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])

    # Wiederholende Aufgabe (OPEN, ueberfaellig) — vom Service nach period_end
    # asc sortiert: aelteste zuerst. Wir machen sie ABSICHTLICH aelter als den
    # Dienst (innerhalb des 7-Tage-Fensters), damit die Default-Sortierung sie
    # zuerst zeigt.
    weekly = _mk_assignment(
        resident,
        kind=TaskKind.AUFGABE,
        recurrence=Recurrence.WEEKLY,
        period_start=date.today() - timedelta(days=10),
        period_end=date.today() - timedelta(days=5),
        status=AssignmentStatus.OPEN,
        title="Wiederholende-Aufgabe",
    )
    dienst = _mk_assignment(
        resident,
        kind=TaskKind.DIENST,
        period_start=date.today() - timedelta(days=6),
        period_end=date.today() - timedelta(days=2),
        status=AssignmentStatus.DONE,
        points_earned=4,
        title="Mein-Dienst",
    )
    assert weekly.id and dienst.id

    _login(client, hw)
    body = client.get("/hauswart/?sort=type").get_data(as_text=True)
    assert "Mein-Dienst" in body
    assert "Wiederholende-Aufgabe" in body
    # Sort=type: Dienst → Event → Wiederholend → Einmalig.
    assert body.index("Mein-Dienst") < body.index("Wiederholende-Aufgabe")


def test_review_row_renders_dienst_border_class(
    app: Flask, client: FlaskClient
) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    _mk_assignment(
        resident,
        kind=TaskKind.DIENST,
        period_start=date.today() - timedelta(days=10),
        period_end=date.today() - timedelta(days=3),
        status=AssignmentStatus.DONE,
        points_earned=4,
        title="Dienst-Border-Test",
    )
    _login(client, hw)
    body = client.get("/hauswart/").get_data(as_text=True)
    assert "Dienst-Border-Test" in body
    assert "border-brand-400" in body


def test_review_row_renders_wiederholend_border_class(
    app: Flask, client: FlaskClient
) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    _mk_assignment(
        resident,
        kind=TaskKind.AUFGABE,
        recurrence=Recurrence.WEEKLY,
        period_start=date.today() - timedelta(days=6),
        period_end=date.today() - timedelta(days=2),
        status=AssignmentStatus.OPEN,
        title="Wiederholend-Border-Test",
    )
    _login(client, hw)
    body = client.get("/hauswart/").get_data(as_text=True)
    assert "Wiederholend-Border-Test" in body
    assert "border-sky-400" in body


def test_review_row_renders_einmalig_border_class(
    app: Flask, client: FlaskClient
) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    _mk_assignment(
        resident,
        kind=TaskKind.AUFGABE,
        recurrence=Recurrence.NONE,
        required_assignees=1,
        period_start=date.today() - timedelta(days=6),
        period_end=date.today() - timedelta(days=2),
        status=AssignmentStatus.OPEN,
        title="Einmalig-Border-Test",
    )
    _login(client, hw)
    body = client.get("/hauswart/").get_data(as_text=True)
    assert "Einmalig-Border-Test" in body
    assert "border-accent-400" in body


def test_review_row_renders_event_border_class(
    app: Flask, client: FlaskClient
) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    resident = _mk_user("Resi", roles=[Role.HAUSBEWOHNER])
    _mk_assignment(
        resident,
        kind=TaskKind.AUFGABE,
        recurrence=Recurrence.NONE,
        required_assignees=2,
        period_start=date.today() - timedelta(days=6),
        period_end=date.today() - timedelta(days=2),
        status=AssignmentStatus.OPEN,
        title="Event-Border-Test",
    )
    _login(client, hw)
    body = client.get("/hauswart/").get_data(as_text=True)
    assert "Event-Border-Test" in body
    assert "border-rose-400" in body


def test_index_empty_state_when_filter_user_has_nothing(
    app: Flask, client: FlaskClient
) -> None:
    hw = _mk_user("Heinz", roles=[Role.HAUSWART])
    quiet = _mk_user("Stille", roles=[Role.HAUSBEWOHNER])
    _login(client, hw)
    body = client.get(f"/hauswart/?user_id={quiet.id}").get_data(as_text=True)
    assert "Nichts zu prüfen" in body
