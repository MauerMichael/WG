"""Tests fuer den Auth-Blueprint (Username + Passwort).

Nutzt die in-memory-SQLite-DB ueber ``tests/conftest.py``. Der Auth-Flow wird
direkt im Request-Context simuliert (``_login_as``) oder per echtem
``POST /auth/login`` durchgespielt.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from flask_login import login_user
from werkzeug.security import generate_password_hash

from app.domain.enums import Role, UserStatus
from app.extensions import db
from app.models.audit import AuditLog
from app.models.user import User, UserRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(
    *,
    status: UserStatus = UserStatus.APPROVED,
    roles: tuple[Role, ...] = (Role.HAUSBEWOHNER,),
    name: str | None = None,
    username: str | None = None,
    password: str = "test123",
    must_change_password: bool = False,
) -> User:
    suffix = uuid.uuid4().hex[:10]
    user = User(
        username=username or f"u-{suffix}",
        email=f"test+{suffix}@example.com",
        name=name or f"Test User {suffix}",
        status=status,
        password_hash=generate_password_hash(password),
        must_change_password=must_change_password,
    )
    db.session.add(user)
    db.session.flush()  # User-ID materialisieren.
    for role in roles:
        db.session.add(UserRole(user_id=user.id, role=role))
    db.session.commit()
    return user


def _delete_user(user_id: uuid.UUID) -> None:
    """Raeumt User + Rollen + Audit-Logs fuer diesen User auf."""
    db.session.query(UserRole).filter(UserRole.user_id == user_id).delete()
    db.session.query(AuditLog).filter(
        (AuditLog.user_id == user_id) | (AuditLog.entity_id == user_id)
    ).delete()
    db.session.query(User).filter(User.id == user_id).delete()
    db.session.commit()


@pytest.fixture()
def created_user_ids(app: Flask) -> Iterator[list[uuid.UUID]]:
    """Sammelt User-IDs fuer automatisches Cleanup nach jedem Test."""
    created: list[uuid.UUID] = []
    with app.app_context():
        yield created
        for uid in created:
            try:
                _delete_user(uid)
            except Exception:  # noqa: BLE001
                db.session.rollback()


def _login_as(client: FlaskClient, user: User) -> None:
    """Loggt einen User in den Test-Client ein (umgeht Passwort-Check)."""
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


# ---------------------------------------------------------------------------
# Tests: Status / Guard
# ---------------------------------------------------------------------------


def test_anonymous_root_redirects_to_login(client: FlaskClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (301, 302, 303, 307, 308)
    assert "/auth/login" in response.headers.get("Location", "")


def test_login_page_returns_200(client: FlaskClient) -> None:
    response = client.get("/auth/login")
    assert response.status_code == 200
    assert "Benutzername".encode("utf-8") in response.data
    assert b'name="username"' in response.data
    # Kein Google-Button mehr.
    assert "Mit Google anmelden".encode("utf-8") not in response.data


def test_pending_user_sees_pending_page_not_dashboard(
    app: Flask,
    client: FlaskClient,
    created_user_ids: list[uuid.UUID],
) -> None:
    with app.app_context():
        user = _make_user(status=UserStatus.PENDING, roles=())
        created_user_ids.append(user.id)

    _login_as(client, user)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert "Wartet auf Freischaltung".encode("utf-8") in response.data
    assert "Willkommen".encode("utf-8") not in response.data


def test_approved_user_sees_dashboard(
    app: Flask,
    client: FlaskClient,
    created_user_ids: list[uuid.UUID],
) -> None:
    with app.app_context():
        user = _make_user(status=UserStatus.APPROVED, roles=(Role.HAUSBEWOHNER,))
        created_user_ids.append(user.id)

    _login_as(client, user)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert "Wartet auf Freischaltung".encode("utf-8") not in response.data
    assert "Zugang abgelehnt".encode("utf-8") not in response.data


def test_hausbewohner_cannot_access_admin(
    app: Flask,
    client: FlaskClient,
    created_user_ids: list[uuid.UUID],
) -> None:
    with app.app_context():
        user = _make_user(status=UserStatus.APPROVED, roles=(Role.HAUSBEWOHNER,))
        created_user_ids.append(user.id)

    _login_as(client, user)
    response = client.get("/admin/users", follow_redirects=False)
    assert response.status_code == 403


def test_admin_can_access_admin_users(
    app: Flask,
    client: FlaskClient,
    created_user_ids: list[uuid.UUID],
) -> None:
    with app.app_context():
        user = _make_user(
            status=UserStatus.APPROVED,
            roles=(Role.HAUSBEWOHNER, Role.ADMIN),
        )
        created_user_ids.append(user.id)

    _login_as(client, user)
    response = client.get("/admin/users", follow_redirects=False)
    assert response.status_code == 200
    assert "Benutzer verwalten".encode("utf-8") in response.data


def test_hauswart_can_approve_pending_user(
    app: Flask,
    client: FlaskClient,
    created_user_ids: list[uuid.UUID],
) -> None:
    with app.app_context():
        hauswart = _make_user(
            status=UserStatus.APPROVED,
            roles=(Role.HAUSBEWOHNER, Role.HAUSWART),
        )
        pending = _make_user(status=UserStatus.PENDING, roles=())
        created_user_ids.extend([hauswart.id, pending.id])
        pending_id = pending.id

    _login_as(client, hauswart)
    response = client.post(
        f"/admin/users/{pending_id}/approve",
        follow_redirects=False,
    )
    assert response.status_code in (200, 302, 303)

    with app.app_context():
        refreshed = db.session.get(User, pending_id)
        assert refreshed is not None
        assert refreshed.status == UserStatus.APPROVED
        assert refreshed.joined_at is not None
        assert any(r.role == Role.HAUSBEWOHNER for r in refreshed.roles)


def test_admin_approve_htmx_returns_user_row_fragment(
    app: Flask,
    client: FlaskClient,
    created_user_ids: list[uuid.UUID],
) -> None:
    """HTMX-Mutation liefert das neu gestaltete (div-basierte) Karten-Fragment."""
    with app.app_context():
        admin = _make_user(
            status=UserStatus.APPROVED,
            roles=(Role.HAUSBEWOHNER, Role.ADMIN),
        )
        pending = _make_user(status=UserStatus.PENDING, roles=())
        created_user_ids.extend([admin.id, pending.id])
        pending_id = pending.id

    _login_as(client, admin)
    response = client.post(
        f"/admin/users/{pending_id}/approve",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert f'id="user-row-{pending_id}"' in body
    assert "<div" in body
    assert "Hauswart vergeben" in body


def test_logout_redirects_to_login(
    app: Flask,
    client: FlaskClient,
    created_user_ids: list[uuid.UUID],
) -> None:
    with app.app_context():
        user = _make_user(status=UserStatus.APPROVED, roles=(Role.HAUSBEWOHNER,))
        created_user_ids.append(user.id)

    _login_as(client, user)
    response = client.get("/auth/logout", follow_redirects=False)
    assert response.status_code in (301, 302, 303, 307, 308)
    assert "/auth/login" in response.headers.get("Location", "")


# Hilfs-Smoke: zeigt, dass ``login_user`` direkt nutzbar ist.
def test_login_user_directly_in_request_context(app: Flask) -> None:
    with app.test_request_context():
        with app.app_context():
            user = _make_user(status=UserStatus.APPROVED, roles=(Role.HAUSBEWOHNER,))
            try:
                assert login_user(user) is True
            finally:
                _delete_user(user.id)


# ---------------------------------------------------------------------------
# Tests: Login per Username + Passwort
# ---------------------------------------------------------------------------


def test_login_with_correct_password_logs_in(
    app: Flask,
    client: FlaskClient,
    created_user_ids: list[uuid.UUID],
) -> None:
    with app.app_context():
        user = _make_user(
            status=UserStatus.APPROVED,
            roles=(Role.HAUSBEWOHNER,),
            username="alice",
            password="hunter2x",
        )
        created_user_ids.append(user.id)

    response = client.post(
        "/auth/login",
        data={"username": "alice", "password": "hunter2x"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers.get("Location", "").rstrip("/").endswith("") is True
    # Folge-Request mit Session sieht das Dashboard.
    followed = client.get("/", follow_redirects=False)
    assert followed.status_code == 200
    assert "Wartet auf Freischaltung".encode("utf-8") not in followed.data


def test_login_with_wrong_password_shows_error(
    app: Flask,
    client: FlaskClient,
    created_user_ids: list[uuid.UUID],
) -> None:
    with app.app_context():
        user = _make_user(
            status=UserStatus.APPROVED,
            roles=(Role.HAUSBEWOHNER,),
            username="bob",
            password="correct",
        )
        created_user_ids.append(user.id)

    response = client.post(
        "/auth/login",
        data={"username": "bob", "password": "wrong"},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert "Benutzername oder Passwort falsch".encode("utf-8") in response.data
    # Nicht eingeloggt — Dashboard erzwingt erneut Login-Redirect.
    follow = client.get("/", follow_redirects=False)
    assert follow.status_code in (301, 302, 303, 307, 308)
    assert "/auth/login" in follow.headers.get("Location", "")


def test_login_with_must_change_password_redirects_to_change_password(
    app: Flask,
    client: FlaskClient,
    created_user_ids: list[uuid.UUID],
) -> None:
    with app.app_context():
        user = _make_user(
            status=UserStatus.APPROVED,
            roles=(Role.HAUSBEWOHNER,),
            username="carol",
            password="tempXYZ",
            must_change_password=True,
        )
        created_user_ids.append(user.id)

    response = client.post(
        "/auth/login",
        data={"username": "carol", "password": "tempXYZ"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert "/auth/change-password" in response.headers.get("Location", "")

    # Auch beim Versuch, das Dashboard direkt aufzurufen, leitet der Guard um.
    again = client.get("/", follow_redirects=False)
    assert again.status_code in (301, 302, 303, 307, 308)
    assert "/auth/change-password" in again.headers.get("Location", "")


# ---------------------------------------------------------------------------
# Tests: Change-Password
# ---------------------------------------------------------------------------


def test_change_password_updates_hash_and_clears_flag(
    app: Flask,
    client: FlaskClient,
    created_user_ids: list[uuid.UUID],
) -> None:
    with app.app_context():
        user = _make_user(
            status=UserStatus.APPROVED,
            roles=(Role.HAUSBEWOHNER,),
            username="dora",
            password="oldpass1",
            must_change_password=True,
        )
        created_user_ids.append(user.id)
        user_id = user.id

    _login_as(client, user)
    response = client.post(
        "/auth/change-password",
        data={
            "current_password": "oldpass1",
            "new_password": "newpass1234",
            "confirm_password": "newpass1234",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    with app.app_context():
        refreshed = db.session.get(User, user_id)
        assert refreshed is not None
        assert refreshed.must_change_password is False
        # Neuer Hash ist anders als alter.
        from werkzeug.security import check_password_hash

        assert check_password_hash(refreshed.password_hash, "newpass1234")
        assert not check_password_hash(refreshed.password_hash, "oldpass1")


def test_change_password_rejects_wrong_old_password(
    app: Flask,
    client: FlaskClient,
    created_user_ids: list[uuid.UUID],
) -> None:
    with app.app_context():
        user = _make_user(
            status=UserStatus.APPROVED,
            roles=(Role.HAUSBEWOHNER,),
            username="eve",
            password="correct1",
            must_change_password=True,
        )
        created_user_ids.append(user.id)
        user_id = user.id

    _login_as(client, user)
    response = client.post(
        "/auth/change-password",
        data={
            "current_password": "WRONG",
            "new_password": "newpass1234",
            "confirm_password": "newpass1234",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "Aktuelles Passwort".encode("utf-8") in response.data

    with app.app_context():
        refreshed = db.session.get(User, user_id)
        assert refreshed is not None
        # Flag bleibt gesetzt, altes Passwort gilt weiter.
        assert refreshed.must_change_password is True
        from werkzeug.security import check_password_hash

        assert check_password_hash(refreshed.password_hash, "correct1")


def test_anonymous_change_password_redirects_to_login(
    client: FlaskClient,
) -> None:
    response = client.get("/auth/change-password", follow_redirects=False)
    assert response.status_code in (301, 302, 303, 307, 308)
    assert "/auth/login" in response.headers.get("Location", "")
