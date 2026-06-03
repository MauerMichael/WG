"""Tests für die Aufgaben-Börse (``app.services.handovers`` + Routen).

Eigene Datei, In-Memory-SQLite mit vollem ``create_all`` (braucht
``task_handover_offers`` + ``karma_events``). Stil wie ``test_extras.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy.orm import Session

from app import create_app
from app.domain.enums import (
    AssignmentStatus,
    HandoverStatus,
    KarmaKind,
    Recurrence,
    Role,
    TaskKind,
    TaskStatus,
    UserStatus,
)
from app.extensions import db
from app.models.handover import TaskHandoverOffer
from app.models.karma import KarmaEvent
from app.models.task import (
    TaskAssignment,
    TaskDefinition,
    TaskOccurrence,
)
from app.models.user import User, UserRole
from app.services import handovers, scheduling

# ---------------------------------------------------------------------------
# Fixtures + Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(monkeypatch) -> Flask:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    application = create_app("dev")
    application.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    application.config["SQLALCHEMY_ENGINE_OPTIONS"] = {}
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def session(app: Flask) -> Session:
    return db.session


@pytest.fixture()
def client(app: Flask) -> FlaskClient:
    return app.test_client()


def _make_user(name: str, *, role: Role = Role.HAUSBEWOHNER) -> User:
    user = User(
        email=f"{name.lower()}-{uuid.uuid4().hex[:6]}@example.com",
        name=name,
        status=UserStatus.APPROVED,
        joined_at=datetime.now(UTC) - timedelta(days=200),
    )
    db.session.add(user)
    db.session.flush()
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


_OFFSET = {"v": 0}


def _make_definition(*, required: int = 1, difficulty: int = 4) -> TaskDefinition:
    definition = TaskDefinition(
        title="Küche wischen",
        difficulty_points=difficulty,
        recurrence=Recurrence.WEEKLY,
        kind=TaskKind.AUFGABE,
        required_assignees=required,
        is_active=True,
    )
    db.session.add(definition)
    db.session.flush()
    return definition


def _make_occurrence(
    definition: TaskDefinition, *, start: date | None = None, length: int = 1
) -> TaskOccurrence:
    _OFFSET["v"] += 1
    if start is None:
        start = date.today() + timedelta(days=_OFFSET["v"])
    occ = TaskOccurrence(
        task_definition_id=definition.id,
        period_start=start,
        period_end=start + timedelta(days=length - 1),
        due_date=start + timedelta(days=length - 1),
        status=TaskStatus.OPEN,
    )
    db.session.add(occ)
    db.session.flush()
    occ.task_definition = definition
    return occ


def _assign(occ: TaskOccurrence, user: User) -> TaskAssignment:
    a = TaskAssignment(
        occurrence_id=occ.id,
        user_id=user.id,
        status=AssignmentStatus.OPEN,
        points_earned=0,
    )
    db.session.add(a)
    db.session.flush()
    return a


def _open_assignment(user: User, **occ_kw) -> TaskAssignment:
    definition = _make_definition()
    occ = _make_occurrence(definition, **occ_kw)
    return _assign(occ, user)


# ---------------------------------------------------------------------------
# Service: Abgeben
# ---------------------------------------------------------------------------


def test_offer_creates_open_and_keeps_hauptmann(session):
    user = _make_user("Anbieter")
    assignment = _open_assignment(user)

    offer = handovers.offer_assignment(session, assignment, user, note="  bin weg ")

    assert offer.status == HandoverStatus.OPEN
    assert offer.note == "bin weg"
    # Hauptmann bleibt: Zuweisung unangetastet.
    assert assignment.user_id == user.id
    assert assignment.status == AssignmentStatus.OPEN


def test_offer_rejected_if_not_owner(session):
    owner = _make_user("Owner")
    other = _make_user("Other")
    assignment = _open_assignment(owner)

    with pytest.raises(handovers.HandoverError):
        handovers.offer_assignment(session, assignment, other)


def test_offer_rejected_if_assignment_not_open(session):
    user = _make_user("Anbieter")
    assignment = _open_assignment(user)
    assignment.status = AssignmentStatus.DONE
    session.flush()

    with pytest.raises(handovers.HandoverError):
        handovers.offer_assignment(session, assignment, user)


def test_offer_rejected_if_already_offered(session):
    user = _make_user("Anbieter")
    assignment = _open_assignment(user)
    handovers.offer_assignment(session, assignment, user)

    with pytest.raises(handovers.HandoverError):
        handovers.offer_assignment(session, assignment, user)


# ---------------------------------------------------------------------------
# Service: Board-Query
# ---------------------------------------------------------------------------


def test_open_offers_lists_only_valid(session):
    user = _make_user("Anbieter")

    a_open = _open_assignment(user)
    handovers.offer_assignment(session, a_open, user)

    a_done = _open_assignment(user)
    o_done = handovers.offer_assignment(session, a_done, user)
    a_done.status = AssignmentStatus.DONE  # Assignment erledigt -> raus
    session.flush()

    a_cancel = _open_assignment(user)
    o_cancel = handovers.offer_assignment(session, a_cancel, user)
    handovers.cancel_offer(session, o_cancel, user)  # zurückgezogen -> raus

    listed = handovers.open_offers(session)
    ids = {o.id for o in listed}
    assert a_open.handover_offers[0].id in ids
    assert o_done.id not in ids
    assert o_cancel.id not in ids
    assert handovers.open_offer_count(session) == 1


# ---------------------------------------------------------------------------
# Service: Übernehmen
# ---------------------------------------------------------------------------


def test_claim_transfers_assignment(session):
    owner = _make_user("Owner")
    taker = _make_user("Taker")
    assignment = _open_assignment(owner)
    offer = handovers.offer_assignment(session, assignment, owner)

    handovers.claim_offer(session, offer, taker)

    assert assignment.user_id == taker.id
    assert assignment.assigned_during_absence is False
    assert taker.last_assigned_at is not None
    assert offer.status == HandoverStatus.CLAIMED
    assert offer.claimed_by_id == taker.id
    assert offer.claimed_at is not None
    # vom Brett verschwunden
    assert offer.id not in {o.id for o in handovers.open_offers(session)}


def test_claim_by_offerer_rejected(session):
    owner = _make_user("Owner")
    assignment = _open_assignment(owner)
    offer = handovers.offer_assignment(session, assignment, owner)

    with pytest.raises(handovers.HandoverError):
        handovers.claim_offer(session, offer, owner)
    assert assignment.user_id == owner.id


def test_claim_rejected_if_taker_already_assigned(session):
    owner = _make_user("Owner")
    co = _make_user("CoAssignee")
    definition = _make_definition(required=2)
    occ = _make_occurrence(definition)
    a_owner = _assign(occ, owner)
    _assign(occ, co)  # co ist bereits auf der Occurrence
    offer = handovers.offer_assignment(session, a_owner, owner)

    with pytest.raises(handovers.HandoverError):
        handovers.claim_offer(session, offer, co)


def test_double_claim_race_rejected(session):
    owner = _make_user("Owner")
    t1 = _make_user("Taker1")
    t2 = _make_user("Taker2")
    assignment = _open_assignment(owner)
    offer = handovers.offer_assignment(session, assignment, owner)

    handovers.claim_offer(session, offer, t1)
    with pytest.raises(handovers.HandoverError):
        handovers.claim_offer(session, offer, t2)
    assert assignment.user_id == t1.id


# ---------------------------------------------------------------------------
# Service: Zurückziehen / Auto-Close
# ---------------------------------------------------------------------------


def test_withdraw_cancels_offer(session):
    owner = _make_user("Owner")
    assignment = _open_assignment(owner)
    offer = handovers.offer_assignment(session, assignment, owner)

    handovers.cancel_offer(session, offer, owner)

    assert offer.status == HandoverStatus.CANCELLED
    assert assignment.user_id == owner.id
    assert handovers.open_offer_count(session) == 0


def test_withdraw_rejected_if_not_owner(session):
    owner = _make_user("Owner")
    other = _make_user("Other")
    assignment = _open_assignment(owner)
    offer = handovers.offer_assignment(session, assignment, owner)

    with pytest.raises(handovers.HandoverError):
        handovers.cancel_offer(session, offer, other)


def test_mark_done_closes_open_offer(session):
    owner = _make_user("Owner")
    assignment = _open_assignment(owner, start=date.today())
    offer = handovers.offer_assignment(session, assignment, owner)

    scheduling.mark_done(session, assignment, owner)
    handovers.close_open_offer_for(session, assignment, "mark_done")

    assert offer.status == HandoverStatus.CANCELLED
    assert assignment.status == AssignmentStatus.DONE
    assert handovers.open_offer_count(session) == 0


# ---------------------------------------------------------------------------
# Service: Hauptmann bleibt / Cascade
# ---------------------------------------------------------------------------


def test_offerer_stays_hauptmann_until_claimed_overdue_penalty(session):
    owner = _make_user("Owner")
    assignment = _open_assignment(
        owner, start=date.today() - timedelta(days=5)
    )  # Periode vorbei
    offer = handovers.offer_assignment(session, assignment, owner)

    scheduling.apply_overdue_penalties(session)

    # Penalty trifft den Anbieter, nicht irgendwen.
    penalties = (
        session.query(KarmaEvent)
        .filter(
            KarmaEvent.user_id == owner.id, KarmaEvent.kind == KarmaKind.PENALTY
        )
        .all()
    )
    assert len(penalties) == 1
    assert assignment.status == AssignmentStatus.SKIPPED
    # Stale Offer ist vom Brett gefiltert.
    assert offer.id not in {o.id for o in handovers.open_offers(session)}


def test_absence_reassign_cascade_deletes_offer(session):
    owner = _make_user("Owner")
    _make_user("Ersatz")  # zweiter eligible Kandidat für die Neuverteilung
    start = date.today() + timedelta(days=2)
    assignment = _open_assignment(owner, start=start, length=3)
    offer = handovers.offer_assignment(session, assignment, owner)
    offer_id = offer.id

    # Anbieter wird abwesend -> seine OPEN-Zuweisung wird gelöscht + neu verteilt.
    scheduling.reassign_open_overlap(
        session, owner, start, start + timedelta(days=2)
    )

    assert session.get(TaskHandoverOffer, offer_id) is None


# ---------------------------------------------------------------------------
# Routen
# ---------------------------------------------------------------------------


def test_boerse_renders_empty(client, session):
    resident = _make_user("Resi")
    _login(client, resident)
    resp = client.get("/tasks/boerse")
    assert resp.status_code == 200
    assert "Keine offenen Abgaben." in resp.get_data(as_text=True)


def test_boerse_lists_offer(client, session):
    owner = _make_user("Owner")
    assignment = _open_assignment(owner)
    handovers.offer_assignment(session, assignment, owner)
    db.session.commit()

    taker = _make_user("Taker")
    _login(client, taker)
    resp = client.get("/tasks/boerse")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Küche wischen" in body
    assert "Übernehmen" in body


def test_boerse_shows_own_offer_with_withdraw(client, session):
    """Eigentümer-Ansicht rendert „deine Abgabe" + Zurückziehen (from=board)."""
    owner = _make_user("Owner")
    assignment = _open_assignment(owner)
    handovers.offer_assignment(session, assignment, owner)
    db.session.commit()

    _login(client, owner)
    resp = client.get("/tasks/boerse")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "deine Abgabe" in body
    assert "Zurückziehen" in body
    assert "from=board" in body


def test_post_abgeben_creates_offer(client, session):
    owner = _make_user("Owner")
    assignment = _open_assignment(owner, start=date.today())
    occ_id = assignment.occurrence_id
    db.session.commit()

    _login(client, owner)
    resp = client.post(
        f"/tasks/{occ_id}/abgeben",
        data={"note": "keine Zeit"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "Zurückziehen" in resp.get_data(as_text=True)

    offers = session.query(TaskHandoverOffer).all()
    assert len(offers) == 1
    assert offers[0].status == HandoverStatus.OPEN


def test_post_uebernehmen_transfers(client, session):
    owner = _make_user("Owner")
    taker = _make_user("Taker")
    assignment = _open_assignment(owner)
    offer = handovers.offer_assignment(session, assignment, owner)
    db.session.commit()

    _login(client, taker)
    resp = client.post(
        f"/tasks/abgaben/{offer.id}/uebernehmen",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "Übernommen" in resp.get_data(as_text=True)

    db.session.refresh(assignment)
    assert assignment.user_id == taker.id


def test_post_zurueckziehen_restores_card(client, session):
    owner = _make_user("Owner")
    assignment = _open_assignment(owner, start=date.today())
    offer = handovers.offer_assignment(session, assignment, owner)
    db.session.commit()

    _login(client, owner)
    resp = client.post(
        f"/tasks/abgaben/{offer.id}/zurueckziehen",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    # Karte zeigt wieder den Abgeben-Knopf.
    assert "Abgeben" in resp.get_data(as_text=True)

    db.session.refresh(offer)
    assert offer.status == HandoverStatus.CANCELLED


def test_future_period_offer_allowed(client, session):
    owner = _make_user("Owner")
    assignment = _open_assignment(
        owner, start=date.today() + timedelta(days=10)
    )
    occ_id = assignment.occurrence_id
    db.session.commit()

    _login(client, owner)
    resp = client.post(f"/tasks/{occ_id}/abgeben", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert session.query(TaskHandoverOffer).count() == 1
