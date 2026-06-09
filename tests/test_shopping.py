"""Tests für den Shopping-Blueprint.

Wie ``test_absences.py``: eigene In-Memory-SQLite-App, damit parallele Agenten
auf der remote DB uns nicht ins Knie schießen.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app import create_app
from app.domain.enums import Role, UserStatus
from app.extensions import db
from app.models.shopping import ShoppingItem
from app.models.user import User, UserRole


# ---------------------------------------------------------------------------
# Lokale Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(monkeypatch: pytest.MonkeyPatch) -> Flask:
    """In-Memory-SQLite, damit parallele DB-Operationen anderer Agenten uns
    nicht stören. Die Config wird VOR ``create_app`` gepatcht."""
    from app.config import DevConfig

    monkeypatch.setattr(DevConfig, "SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
    application = create_app("dev")
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with application.app_context():
        tables = [
            User.__table__,
            UserRole.__table__,
            ShoppingItem.__table__,
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
    """Setzt die Flask-Login Session direkt — umgeht den OAuth-Flow.

    Wichtig: Wir poppen einen ggf. gecachten ``g._login_user`` aus dem
    AppContext, weil die Test-Fixture diesen AppContext über mehrere
    Requests offen hält und Flask-Login sonst den User der vorherigen
    Request weiterverwendet.
    """
    from flask import g

    with client.session_transaction() as sess:
        sess.clear()
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    g.pop("_login_user", None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_add_item_appears_in_offen(app: Flask, client: FlaskClient) -> None:
    user = _mk_user("Alice", roles=[Role.HAUSBEWOHNER])
    _login(client, user)

    resp = client.post(
        "/shopping/",
        data={"title": "Milch", "quantity": "2x"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "Milch".encode("utf-8") in resp.data

    found = db.session.query(ShoppingItem).filter_by(title="Milch").one_or_none()
    assert found is not None
    assert found.bought_at is None
    assert found.added_by_id == user.id
    assert found.quantity == "2x"


def test_check_item_moves_to_done(app: Flask, client: FlaskClient) -> None:
    user = _mk_user("Alice", roles=[Role.HAUSBEWOHNER])
    item = ShoppingItem(title="Brot", added_by_id=user.id)
    db.session.add(item)
    db.session.commit()
    _login(client, user)

    resp = client.post(f"/shopping/{item.id}/check")
    assert resp.status_code in (200, 302, 303)

    db.session.expire_all()
    refreshed = db.session.get(ShoppingItem, item.id)
    assert refreshed.bought_at is not None
    assert refreshed.bought_by_id == user.id

    resp = client.get("/shopping/")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "Brot" in body
    assert "Kürzlich gekauft" in body


def test_uncheck_restores_item(app: Flask, client: FlaskClient) -> None:
    user = _mk_user("Alice", roles=[Role.HAUSBEWOHNER])
    item = ShoppingItem(
        title="Käse",
        added_by_id=user.id,
        bought_at=datetime.now(timezone.utc),
        bought_by_id=user.id,
    )
    db.session.add(item)
    db.session.commit()
    _login(client, user)

    resp = client.post(f"/shopping/{item.id}/uncheck")
    assert resp.status_code in (200, 302, 303)

    db.session.expire_all()
    refreshed = db.session.get(ShoppingItem, item.id)
    assert refreshed.bought_at is None
    assert refreshed.bought_by_id is None


def test_delete_only_by_adder_or_admin(app: Flask) -> None:
    # Pro Login einen frischen Test-Client → keine Cookie/Login-Carry-Over.
    alice = _mk_user("Alice", roles=[Role.HAUSBEWOHNER])
    bob = _mk_user("Bob", roles=[Role.HAUSBEWOHNER])
    admin = _mk_user("Admin", roles=[Role.HAUSBEWOHNER, Role.ADMIN])

    item = ShoppingItem(title="Salat", added_by_id=alice.id)
    db.session.add(item)
    db.session.commit()

    # Bob (kein Adder, kein Admin) → 403
    bob_client = app.test_client()
    _login(bob_client, bob)
    resp = bob_client.post(f"/shopping/{item.id}/delete")
    assert resp.status_code == 403
    assert db.session.get(ShoppingItem, item.id) is not None

    # Adder darf löschen.
    alice_client = app.test_client()
    _login(alice_client, alice)
    resp = alice_client.post(f"/shopping/{item.id}/delete")
    assert resp.status_code in (200, 302, 303)
    assert db.session.get(ShoppingItem, item.id) is None

    # Admin darf auch ein Nicht-Adder-Item löschen.
    item2 = ShoppingItem(title="Joghurt", added_by_id=alice.id)
    db.session.add(item2)
    db.session.commit()
    admin_client = app.test_client()
    _login(admin_client, admin)
    resp = admin_client.post(f"/shopping/{item2.id}/delete")
    assert resp.status_code in (200, 302, 303)
    assert db.session.get(ShoppingItem, item2.id) is None


def test_unauthenticated_redirected(app: Flask, client: FlaskClient) -> None:
    resp = client.get("/shopping/")
    assert resp.status_code in (302, 303, 401)
