"""Tests für die WG-Mitgliedschaft im Admin.

Auszug = HAUSBEWOHNER-Rolle entziehen: Account/Status (APPROVED) bleiben, die
Person fällt aber aus dem Zuweisungs-Pool, und ihre offenen zukünftigen Dienste
werden an die übrigen Bewohner zurückgegeben (statt verwaist hängen zu bleiben).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from flask import Flask

from app import create_app
from app.domain.enums import (
    AssignmentStatus,
    Recurrence,
    Role,
    TaskStatus,
    UserStatus,
)
from app.extensions import db
from app.models.task import TaskAssignment, TaskDefinition, TaskOccurrence
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


def _make_user(name: str, *, roles: list[Role], status: UserStatus = UserStatus.APPROVED) -> User:
    import uuid as _uuid
    now = datetime.now(timezone.utc)
    suffix = _uuid.uuid4().hex[:6]
    user = User(
        username=f"{name.lower()}-{suffix}",
        email=f"{name.lower()}-{suffix}@example.com",
        name=name,
        status=status,
        joined_at=now - timedelta(days=200),
        must_change_password=False,
    )
    db.session.add(user)
    db.session.flush()
    for role in roles:
        db.session.add(UserRole(user_id=user.id, role=role))
    db.session.commit()
    return user


def _login(client, user: User) -> None:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def test_move_out_revokes_role_keeps_account_and_reassigns(app):
    admin = _make_user("Admin", roles=[Role.ADMIN])
    anna = _make_user("Anna", roles=[Role.HAUSBEWOHNER])
    bea = _make_user("Bea", roles=[Role.HAUSBEWOHNER])
    client = app.test_client()
    _login(client, admin)

    # Zukünftiger Dienst, zunächst Anna allein zugewiesen.
    definition = TaskDefinition(
        title="Mülldienst",
        difficulty_points=2,
        required_assignees=1,
        recurrence=Recurrence.WEEKLY,
        is_active=True,
    )
    db.session.add(definition)
    db.session.flush()
    occ = TaskOccurrence(
        task_definition_id=definition.id,
        period_start=date.today() + timedelta(days=3),
        period_end=date.today() + timedelta(days=9),
        due_date=date.today() + timedelta(days=3),
        status=TaskStatus.OPEN,
    )
    db.session.add(occ)
    db.session.flush()
    db.session.add(
        TaskAssignment(
            occurrence_id=occ.id, user_id=anna.id, status=AssignmentStatus.OPEN
        )
    )
    db.session.commit()

    # Anna zieht aus → HAUSBEWOHNER entziehen.
    response = client.post(
        f"/admin/users/{anna.id}/roles",
        data={"role": "HAUSBEWOHNER", "action": "remove"},
    )
    assert response.status_code == 302  # ohne HX-Header: Redirect auf die Liste

    db.session.refresh(anna)
    # Account bleibt erhalten, nur raus aus der Rotation.
    assert anna.status == UserStatus.APPROVED
    assert all(r.role != Role.HAUSBEWOHNER for r in anna.roles)

    # Offener Dienst ist neu verteilt: nicht mehr Anna, sondern Bea.
    open_assignments = (
        db.session.query(TaskAssignment)
        .filter_by(occurrence_id=occ.id, status=AssignmentStatus.OPEN)
        .all()
    )
    assignee_ids = {a.user_id for a in open_assignments}
    assert anna.id not in assignee_ids
    assert bea.id in assignee_ids


def test_move_in_grants_role(app):
    admin = _make_user("Admin", roles=[Role.ADMIN])
    # Freigeschalteter Account, aber (noch) kein Bewohner.
    gast = _make_user("Gast", roles=[])
    client = app.test_client()
    _login(client, admin)

    response = client.post(
        f"/admin/users/{gast.id}/roles",
        data={"role": "HAUSBEWOHNER", "action": "add"},
    )
    assert response.status_code == 302

    db.session.refresh(gast)
    assert any(r.role == Role.HAUSBEWOHNER for r in gast.roles)
