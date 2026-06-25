"""Tests für rebalance_open_assignments und die Admin-Hooks.

Spiegelt das Live-Szenario „neuer Bewohner kommt rein → bestehende OPEN-Future-
Zuweisungen werden umverteilt" sowie die Idempotenz beim Cron-Catch-up.
"""

from __future__ import annotations

import uuid as _uuid
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
from app.services import scheduling


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


def _make_user(name: str, *, roles: list[Role]) -> User:
    suffix = _uuid.uuid4().hex[:6]
    user = User(
        username=f"{name.lower()}-{suffix}",
        email=f"{name.lower()}-{suffix}@example.com",
        name=name,
        status=UserStatus.APPROVED,
        joined_at=datetime.now(timezone.utc) - timedelta(days=200),
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


def _add_definition(title: str, *, required: int = 1) -> TaskDefinition:
    d = TaskDefinition(
        title=title,
        difficulty_points=2,
        required_assignees=required,
        recurrence=Recurrence.WEEKLY,
        is_active=True,
    )
    db.session.add(d)
    db.session.flush()
    return d


def _add_occurrence(d: TaskDefinition, *, days_ahead: int, assignees: list[User]) -> TaskOccurrence:
    start = date.today() + timedelta(days=days_ahead)
    occ = TaskOccurrence(
        task_definition_id=d.id,
        period_start=start,
        period_end=start + timedelta(days=6),
        due_date=start,
        status=TaskStatus.OPEN,
    )
    db.session.add(occ)
    db.session.flush()
    for u in assignees:
        db.session.add(
            TaskAssignment(occurrence_id=occ.id, user_id=u.id, status=AssignmentStatus.OPEN)
        )
    db.session.flush()
    return occ


# ---------------------------------------------------------------------------
# Service-Funktion direkt
# ---------------------------------------------------------------------------


def test_rebalance_distributes_load_to_new_resident(app):
    anna = _make_user("Anna", roles=[Role.HAUSBEWOHNER])
    _ = _make_user("Bea", roles=[Role.HAUSBEWOHNER])  # Mit-Bewohnerin im Pool
    d = _add_definition("Mülldienst")
    # 6 offene Future-Occurrences ausschließlich Anna zugewiesen.
    for i in range(6):
        _add_occurrence(d, days_ahead=3 + i * 7, assignees=[anna])
    db.session.commit()

    # Jonas zieht ein.
    jonas = _make_user("Jonas", roles=[Role.HAUSBEWOHNER])

    swaps = scheduling.rebalance_open_assignments(db.session)
    db.session.commit()

    counts: dict = {}
    for a in db.session.query(TaskAssignment).filter_by(status=AssignmentStatus.OPEN).all():
        counts[a.user_id] = counts.get(a.user_id, 0) + 1

    # Jonas hat jetzt mindestens 1 Aufgabe (war 0).
    assert counts.get(jonas.id, 0) >= 1
    # Bea sollte auch was abbekommen haben oder Anna entlastet sein.
    assert counts.get(anna.id, 0) <= 3  # vorher 6, jetzt ~2
    assert swaps >= 3  # mindestens drei Swaps für 6→{2,2,2}


def test_rebalance_is_idempotent(app):
    anna = _make_user("Anna", roles=[Role.HAUSBEWOHNER])
    _ = _make_user("Bea", roles=[Role.HAUSBEWOHNER])  # Mit-Bewohnerin im Pool
    d = _add_definition("Mülldienst")
    for i in range(4):
        _add_occurrence(d, days_ahead=3 + i * 7, assignees=[anna])
    db.session.commit()

    first = scheduling.rebalance_open_assignments(db.session)
    db.session.commit()
    second = scheduling.rebalance_open_assignments(db.session)
    db.session.commit()

    assert first > 0
    assert second == 0  # schon ausgewogen


def test_rebalance_respects_existing_co_assignee(app):
    """Kein Swap auf eine Occurrence, in der die Zielperson schon assigned ist."""
    anna = _make_user("Anna", roles=[Role.HAUSBEWOHNER])
    bea = _make_user("Bea", roles=[Role.HAUSBEWOHNER])
    d = _add_definition("Putzdienst", required=2)
    # Beide Slots gehören Anna+Bea bereits — nichts zu swappen.
    _add_occurrence(d, days_ahead=3, assignees=[anna, bea])
    _add_occurrence(d, days_ahead=10, assignees=[anna, bea])
    db.session.commit()

    swaps = scheduling.rebalance_open_assignments(db.session)
    assert swaps == 0


def test_rebalance_skips_past_occurrences(app):
    """Vergangene Occurrences werden nicht angefasst."""
    anna = _make_user("Anna", roles=[Role.HAUSBEWOHNER])
    _ = _make_user("Bea", roles=[Role.HAUSBEWOHNER])  # Mit-Bewohnerin im Pool
    d = _add_definition("Mülldienst")
    # Vergangene Occurrence — period_end < today.
    past_start = date.today() - timedelta(days=10)
    past = TaskOccurrence(
        task_definition_id=d.id,
        period_start=past_start,
        period_end=past_start + timedelta(days=6),
        due_date=past_start,
        status=TaskStatus.OPEN,
    )
    db.session.add(past)
    db.session.flush()
    db.session.add(
        TaskAssignment(occurrence_id=past.id, user_id=anna.id, status=AssignmentStatus.OPEN)
    )
    db.session.commit()

    swaps = scheduling.rebalance_open_assignments(db.session)
    assert swaps == 0
    # past assignment unverändert bei Anna.
    a = db.session.query(TaskAssignment).filter_by(occurrence_id=past.id).first()
    assert a.user_id == anna.id


# ---------------------------------------------------------------------------
# Admin-Route-Integration
# ---------------------------------------------------------------------------


def test_admin_approve_triggers_rebalance(app):
    """Approve eines PENDING-Users löst Rebalance aus."""
    admin = _make_user("Admin", roles=[Role.ADMIN, Role.HAUSBEWOHNER])
    anna = _make_user("Anna", roles=[Role.HAUSBEWOHNER])
    d = _add_definition("Mülldienst")
    for i in range(4):
        _add_occurrence(d, days_ahead=3 + i * 7, assignees=[anna])
    db.session.commit()

    # PENDING-User Jonas.
    jonas = User(
        username="jonas-x",
        email="jonas-x@example.com",
        name="Jonas",
        status=UserStatus.PENDING,
        must_change_password=False,
    )
    db.session.add(jonas)
    db.session.commit()

    client = app.test_client()
    _login(client, admin)
    response = client.post(f"/admin/users/{jonas.id}/approve")
    assert response.status_code == 302

    db.session.refresh(jonas)
    assert jonas.status == UserStatus.APPROVED

    counts: dict = {}
    for a in db.session.query(TaskAssignment).filter_by(status=AssignmentStatus.OPEN).all():
        counts[a.user_id] = counts.get(a.user_id, 0) + 1
    # Jonas hat jetzt mindestens 1 der vier Mülldienste.
    assert counts.get(jonas.id, 0) >= 1


def test_admin_grant_hausbewohner_triggers_rebalance(app):
    """toggle_role(add HAUSBEWOHNER) löst Rebalance aus."""
    admin = _make_user("Admin", roles=[Role.ADMIN, Role.HAUSBEWOHNER])
    anna = _make_user("Anna", roles=[Role.HAUSBEWOHNER])
    d = _add_definition("Mülldienst")
    for i in range(4):
        _add_occurrence(d, days_ahead=3 + i * 7, assignees=[anna])
    db.session.commit()

    # User ohne HAUSBEWOHNER-Rolle, wird jetzt zugewiesen.
    gast = _make_user("Gast", roles=[])

    client = app.test_client()
    _login(client, admin)
    response = client.post(
        f"/admin/users/{gast.id}/roles",
        data={"role": "HAUSBEWOHNER", "action": "add"},
    )
    assert response.status_code == 302

    counts: dict = {}
    for a in db.session.query(TaskAssignment).filter_by(status=AssignmentStatus.OPEN).all():
        counts[a.user_id] = counts.get(a.user_id, 0) + 1
    assert counts.get(gast.id, 0) >= 1


def test_admin_create_user_triggers_rebalance(app):
    """new_user_create legt User an und rebalanced."""
    admin = _make_user("Admin", roles=[Role.ADMIN, Role.HAUSBEWOHNER])
    anna = _make_user("Anna", roles=[Role.HAUSBEWOHNER])
    d = _add_definition("Mülldienst")
    for i in range(4):
        _add_occurrence(d, days_ahead=3 + i * 7, assignees=[anna])
    db.session.commit()

    client = app.test_client()
    _login(client, admin)
    response = client.post(
        "/admin/users/new",
        data={"name": "Jonas", "username": "jonas", "roles": ["HAUSBEWOHNER"]},
    )
    # 200, weil Success-Karte gerendert wird (kein Redirect).
    assert response.status_code == 200

    jonas = db.session.query(User).filter_by(username="jonas").first()
    assert jonas is not None

    counts: dict = {}
    for a in db.session.query(TaskAssignment).filter_by(status=AssignmentStatus.OPEN).all():
        counts[a.user_id] = counts.get(a.user_id, 0) + 1
    assert counts.get(jonas.id, 0) >= 1
