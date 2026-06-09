"""Tests für Extra-Leistungen + Ehrenpunkte (Service + Routen).

Eigene Datei, In-Memory-SQLite mit vollem ``create_all`` (braucht
``extra_contributions`` + ``karma_events``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy.orm import Session

from app import create_app
from app.domain.enums import KarmaKind, ReviewStatus, Role, UserStatus
from app.extensions import db
from app.models.extra import ExtraContribution
from app.models.karma import KarmaEvent
from app.models.user import User, UserRole
from app.services import contributions, scheduling

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


def _make_user(
    name: str, *, role: Role = Role.HAUSBEWOHNER, joined_days_ago: int = 200
) -> User:
    suffix = uuid.uuid4().hex[:6]
    user = User(
        username=f"{name.lower().replace(' ', '')}-{suffix}",
        email=f"{name.lower()}-{suffix}@example.com",
        name=name,
        status=UserStatus.APPROVED,
        joined_at=datetime.now(UTC) - timedelta(days=joined_days_ago),
        must_change_password=False,
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


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def test_submit_creates_pending(session):
    user = _make_user("Bewohner")
    c = contributions.submit_contribution(session, user, "  Keller entrümpelt  ")

    assert c.status == ReviewStatus.PENDING
    assert c.description == "Keller entrümpelt"  # getrimmt
    assert c.honor_points is None


def test_approve_awards_honor_and_raises_score(session):
    reviewer = _make_user("Hauswart", role=Role.HAUSWART)
    user = _make_user("Bewohner", joined_days_ago=200)
    c = contributions.submit_contribution(session, user, "Waschküche geputzt")

    contributions.approve_contribution(session, c, reviewer, 5, note="Top")

    assert c.status == ReviewStatus.APPROVED
    assert c.honor_points == 5
    assert c.awarded_by_id == reviewer.id
    assert c.awarded_at is not None

    events = (
        session.query(KarmaEvent)
        .filter(KarmaEvent.user_id == user.id, KarmaEvent.kind == KarmaKind.HONOR)
        .all()
    )
    assert len(events) == 1
    assert events[0].points == 5

    # 5 / max(200, 90) * 90
    assert scheduling.effective_score(session, user) == pytest.approx(5 / 200 * 90)


def test_approve_is_idempotent(session):
    reviewer = _make_user("Hauswart", role=Role.HAUSWART)
    user = _make_user("Bewohner")
    c = contributions.submit_contribution(session, user, "Etwas Gutes")

    contributions.approve_contribution(session, c, reviewer, 5)
    contributions.approve_contribution(session, c, reviewer, 99)  # zweiter Aufruf

    events = (
        session.query(KarmaEvent)
        .filter(KarmaEvent.kind == KarmaKind.HONOR)
        .all()
    )
    assert len(events) == 1
    assert events[0].points == 5  # erster Wert bleibt, kein zweites Event


def test_reject_sets_status_and_grants_no_honor(session):
    reviewer = _make_user("Hauswart", role=Role.HAUSWART)
    user = _make_user("Bewohner")
    c = contributions.submit_contribution(session, user, "Strittig")

    contributions.reject_contribution(session, c, reviewer, note="Zählt nicht")

    assert c.status == ReviewStatus.REJECTED
    assert c.honor_points is None
    assert c.review_note == "Zählt nicht"
    assert session.query(KarmaEvent).count() == 0
    assert scheduling.effective_score(session, user) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Routen
# ---------------------------------------------------------------------------


def test_resident_can_submit(client, session):
    resident = _make_user("Resi")
    _login(client, resident)

    resp = client.post("/extras/", data={"description": "Fenster geputzt"})
    assert resp.status_code == 302

    rows = session.query(ExtraContribution).all()
    assert len(rows) == 1
    assert rows[0].status == ReviewStatus.PENDING


def test_index_renders_for_resident(client):
    resident = _make_user("Resi")
    _login(client, resident)
    resp = client.get("/extras/")
    assert resp.status_code == 200


def test_hauswart_can_approve_via_route(client, session):
    reviewer = _make_user("Hauswart", role=Role.HAUSWART)
    user = _make_user("Bewohner")
    c = contributions.submit_contribution(session, user, "Etwas Extra")
    db.session.commit()

    _login(client, reviewer)
    resp = client.post(
        f"/extras/{c.id}/approve", data={"honor_points": "4", "note": "Danke"}
    )
    assert resp.status_code in (200, 302)

    db.session.refresh(c)
    assert c.status == ReviewStatus.APPROVED
    assert c.honor_points == 4
    assert (
        session.query(KarmaEvent)
        .filter(KarmaEvent.kind == KarmaKind.HONOR)
        .count()
        == 1
    )


def test_review_section_renders_for_hauswart(client, session):
    """Hauswart-Index rendert die Prüf-Sektion inkl. _review_row.html-Partial."""
    reviewer = _make_user("Hauswart", role=Role.HAUSWART)
    user = _make_user("Bewohner")
    contributions.submit_contribution(session, user, "Etwas Extra")
    db.session.commit()

    _login(client, reviewer)
    resp = client.get("/extras/")
    assert resp.status_code == 200
    assert b"Genehmigen" in resp.data  # Approve-Button aus _review_row.html


def test_resident_cannot_approve(client, session):
    reviewer_target = _make_user("Andere")
    c = contributions.submit_contribution(session, reviewer_target, "Fremd")
    db.session.commit()

    resident = _make_user("Resi")
    _login(client, resident)
    resp = client.post(f"/extras/{c.id}/approve", data={"honor_points": "4"})
    assert resp.status_code == 403

    db.session.refresh(c)
    assert c.status == ReviewStatus.PENDING  # unverändert
