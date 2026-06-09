"""Tests fuer User-Anlage + Passwort-Reset durch Admin/Hauswart."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask
from flask.testing import FlaskClient
from werkzeug.security import check_password_hash, generate_password_hash

from app import create_app
from app.domain.enums import Role, UserStatus
from app.extensions import db
from app.models.user import User, UserRole


@pytest.fixture()
def app() -> Flask:
    application = create_app("dev")
    application.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SQLALCHEMY_ENGINE_OPTIONS={},
        TESTING=True,
        WTF_CSRF_ENABLED=False,
    )
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app: Flask) -> FlaskClient:
    return app.test_client()


def _make_user(
    name: str,
    *,
    roles: list[Role],
    username: str | None = None,
    password: str = "test123",
    status: UserStatus = UserStatus.APPROVED,
) -> User:
    suffix = uuid.uuid4().hex[:6]
    now = datetime.now(timezone.utc)
    user = User(
        username=username or f"{name.lower()}-{suffix}",
        email=f"{name.lower()}-{suffix}@example.com",
        name=name,
        status=status,
        joined_at=now - timedelta(days=200),
        password_hash=generate_password_hash(password),
        must_change_password=False,
    )
    db.session.add(user)
    db.session.flush()
    for role in roles:
        db.session.add(UserRole(user_id=user.id, role=role))
    db.session.commit()
    return user


def _login(client: FlaskClient, user: User) -> None:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_admin_can_create_user_with_temp_password(client: FlaskClient) -> None:
    admin = _make_user("Admin", roles=[Role.ADMIN, Role.HAUSBEWOHNER])
    _login(client, admin)

    response = client.post(
        "/admin/users/new",
        data={
            "name": "Neue Person",
            "username": "neueperson",
            "roles": ["HAUSBEWOHNER"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    # Erfolgs-Karte mit Username + temp Passwort.
    assert "Neue Person" in body
    assert "neueperson" in body
    assert "Temporäres Passwort" in body

    created = (
        db.session.query(User).filter(User.username == "neueperson").first()
    )
    assert created is not None
    assert created.status == UserStatus.APPROVED
    assert created.must_change_password is True
    assert created.password_hash is not None
    assert any(r.role == Role.HAUSBEWOHNER for r in created.roles)


def test_hauswart_can_create_user_with_temp_password(
    client: FlaskClient,
) -> None:
    hauswart = _make_user(
        "Hauswart", roles=[Role.HAUSWART, Role.HAUSBEWOHNER]
    )
    _login(client, hauswart)

    response = client.post(
        "/admin/users/new",
        data={
            "name": "Gast",
            "username": "gast",
            "roles": ["HAUSBEWOHNER"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert (
        db.session.query(User).filter(User.username == "gast").first()
        is not None
    )


def test_resident_cannot_access_create_user_form(client: FlaskClient) -> None:
    bewohner = _make_user("Bewohner", roles=[Role.HAUSBEWOHNER])
    _login(client, bewohner)

    response = client.get("/admin/users/new", follow_redirects=False)
    assert response.status_code == 403

    response = client.post(
        "/admin/users/new",
        data={
            "name": "X",
            "username": "x123",
            "roles": ["HAUSBEWOHNER"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_create_user_duplicate_username_shows_error(
    client: FlaskClient,
) -> None:
    admin = _make_user(
        "Admin", roles=[Role.ADMIN, Role.HAUSBEWOHNER], username="adminbob"
    )
    _make_user(
        "Schon Da", roles=[Role.HAUSBEWOHNER], username="schonda"
    )
    _login(client, admin)

    response = client.post(
        "/admin/users/new",
        data={
            "name": "Doppelter",
            "username": "schonda",
            "roles": ["HAUSBEWOHNER"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    body = response.get_data(as_text=True)
    assert "bereits vergeben" in body
    # Nur ein User mit dem Username existiert.
    count = (
        db.session.query(User).filter(User.username == "schonda").count()
    )
    assert count == 1


def test_reset_password_generates_new_temp_password(
    client: FlaskClient,
) -> None:
    admin = _make_user("Admin", roles=[Role.ADMIN, Role.HAUSBEWOHNER])
    target = _make_user(
        "Ziel",
        roles=[Role.HAUSBEWOHNER],
        username="ziel",
        password="originalpw",
    )
    _login(client, admin)

    response = client.post(
        f"/admin/users/{target.id}/reset-password",
        follow_redirects=False,
    )
    # Redirect (non-HTMX) zur Liste.
    assert response.status_code in (302, 303)

    refreshed = db.session.get(User, target.id)
    assert refreshed is not None
    assert refreshed.must_change_password is True
    # Altes Passwort funktioniert nicht mehr.
    assert not check_password_hash(refreshed.password_hash, "originalpw")


def test_reset_password_htmx_returns_card_with_password(
    client: FlaskClient,
) -> None:
    admin = _make_user("Admin", roles=[Role.ADMIN, Role.HAUSBEWOHNER])
    target = _make_user(
        "Ziel2",
        roles=[Role.HAUSBEWOHNER],
        username="ziel2",
        password="originalpw",
    )
    _login(client, admin)

    response = client.post(
        f"/admin/users/{target.id}/reset-password",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    # HTMX-Card enthaelt Username + Swap-Ziel-ID.
    assert f'id="user-row-{target.id}"' in body
    assert "ziel2" in body
    assert "Passwort" in body
