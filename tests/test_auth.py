"""Tests für Auth- und Admin-Blueprints.

Diese Tests nutzen die echte (remote) Postgres-DB, sorgen aber für Cleanup,
indem sie alle in einem Test angelegten User-IDs am Ende wieder löschen.
Der Auth-Flow wird umgangen: ``flask_login.login_user`` wird direkt im
Request-Context aufgerufen.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from flask_login import login_user

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
) -> User:
    suffix = uuid.uuid4().hex[:10]
    user = User(
        google_sub=f"test-sub-{suffix}",
        email=f"test+{suffix}@example.com",
        name=name or f"Test User {suffix}",
        status=status,
    )
    db.session.add(user)
    db.session.flush()  # User-ID materialisieren.
    for role in roles:
        db.session.add(UserRole(user_id=user.id, role=role))
    db.session.commit()
    return user


def _delete_user(user_id: uuid.UUID) -> None:
    """Räumt User + Rollen + Audit-Logs für diesen User auf."""
    db.session.query(UserRole).filter(UserRole.user_id == user_id).delete()
    db.session.query(AuditLog).filter(
        (AuditLog.user_id == user_id) | (AuditLog.entity_id == user_id)
    ).delete()
    db.session.query(User).filter(User.id == user_id).delete()
    db.session.commit()


@pytest.fixture()
def created_user_ids(app: Flask) -> Iterator[list[uuid.UUID]]:
    """Sammelt User-IDs für automatisches Cleanup nach jedem Test."""
    created: list[uuid.UUID] = []
    with app.app_context():
        yield created
        for uid in created:
            try:
                _delete_user(uid)
            except Exception:  # noqa: BLE001
                db.session.rollback()


def _login_as(client: FlaskClient, user: User) -> None:
    """Loggt einen User in den Test-Client ein (umgeht Google)."""
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_anonymous_root_redirects_to_login(client: FlaskClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (301, 302, 303, 307, 308)
    assert "/auth/login" in response.headers.get("Location", "")


def test_login_page_returns_200(client: FlaskClient) -> None:
    response = client.get("/auth/login")
    assert response.status_code == 200
    assert "Mit Google anmelden".encode("utf-8") in response.data


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
    # Pending-/Rejected-Marker dürfen *nicht* auftauchen — das hieße, der Guard
    # hätte die Dashboard-Seite ersetzt.
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
    """HTMX-Mutation liefert das neu gestaltete (div-basierte) Karten-Fragment
    mit stabilem Swap-Ziel und den Rollen-Aktionen zurück."""
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
    # Swap-Ziel bleibt stabil; Root ist jetzt ein <div> (Karte) statt <tr>.
    assert f'id="user-row-{pending_id}"' in body
    assert "<div" in body
    # Frisch freigeschaltet → Rollen-Aktion sichtbar.
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


# Hilfs-Smoke: zeigt, dass `login_user` direkt nutzbar ist (für andere Agents).
def test_login_user_directly_in_request_context(app: Flask) -> None:
    with app.test_request_context():
        with app.app_context():
            user = _make_user(status=UserStatus.APPROVED, roles=(Role.HAUSBEWOHNER,))
            try:
                assert login_user(user) is True
            finally:
                _delete_user(user.id)
