"""Routen-Tests für das Tasks-Blueprint.

Wir loggen den Test-User über die Flask-Session manuell ein (statt durch den
OAuth-Flow), damit die Tests unabhängig vom Auth-Agent laufen.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from flask import Flask

from app import create_app
from app.domain.enums import Recurrence, Role, TaskKind, UserStatus
from app.extensions import db
from app.models.task import TaskDefinition, TaskOccurrence
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


def _make_user(name: str, *, role: Role, status: UserStatus = UserStatus.APPROVED) -> User:
    now = datetime.now(timezone.utc)
    import uuid as _uuid
    suffix = _uuid.uuid4().hex[:6]
    user = User(
        username=f"{name.lower().replace(' ', '')}-{suffix}",
        email=f"{name.lower()}-{suffix}@example.com",
        name=name,
        status=status,
        joined_at=now - timedelta(days=200),
        must_change_password=False,
    )
    db.session.add(user)
    db.session.flush()
    db.session.add(UserRole(user_id=user.id, role=role))
    db.session.commit()
    return user


def _login(client, user: User) -> None:
    """Manuelles Flask-Login: setzt die Session-Keys, die Flask-Login erwartet."""

    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def test_index_requires_login(app):
    client = app.test_client()
    response = client.get("/tasks/")
    assert response.status_code in (302, 401)


def test_index_renders_mine_view_for_approved_resident(app):
    user = _make_user("Lina", role=Role.HAUSBEWOHNER)
    client = app.test_client()
    _login(client, user)

    response = client.get("/tasks/")
    assert response.status_code == 200
    # „Meine"-Ansicht: persönlicher Hero + Tab-Nav „Meine/WG".
    assert "Meine Aufgaben".encode("utf-8") in response.data
    assert b">Meine<" in response.data
    assert b">WG<" in response.data


def test_view_all_renders_wg_calendar(app):
    user = _make_user("Lina", role=Role.HAUSBEWOHNER)
    client = app.test_client()
    _login(client, user)

    response = client.get("/tasks/alle")
    assert response.status_code == 200
    # WG-Ansicht: Bewohner-Strip (Heading enthält "Dienste") + Wochen-Kalender.
    assert b"Dienste" in response.data
    assert b"KW " in response.data
    for short in ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"):
        assert short.encode("utf-8") in response.data


def test_view_all_accepts_week_param(app):
    user = _make_user("Lina", role=Role.HAUSBEWOHNER)
    client = app.test_client()
    _login(client, user)

    response = client.get("/tasks/alle?week=2026-05-28")
    assert response.status_code == 200
    # KW 22 enthält den 28.05.2026 (ein Donnerstag).
    assert "KW 22".encode("utf-8") in response.data


def test_view_all_invalid_week_param_falls_back(app):
    user = _make_user("Lina", role=Role.HAUSBEWOHNER)
    client = app.test_client()
    _login(client, user)

    response = client.get("/tasks/alle?week=not-a-date")
    assert response.status_code == 200


def test_index_accepts_day_param(app):
    user = _make_user("Lina", role=Role.HAUSBEWOHNER)
    client = app.test_client()
    _login(client, user)

    response = client.get("/tasks/?day=2026-05-28")
    assert response.status_code == 200


def test_calendar_shows_task_on_its_weekday(app):
    hauswart = _make_user("Hannes", role=Role.HAUSWART)
    _make_user("Bewohner", role=Role.HAUSBEWOHNER)
    client = app.test_client()
    _login(client, hauswart)

    # WEEKLY mit anchor_weekday = 1 (Dienstag) → erscheint im Kalender.
    response = client.post(
        "/tasks/",
        data={
            "title": "Mülldienst",
            "difficulty_points": "2",
            "recurrence": "WEEKLY",
            "anchor_weekday": "1",
            "required_assignees": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    definition = db.session.query(TaskDefinition).one()
    assert definition.anchor_weekday == 1
    assert definition.recurrence == Recurrence.WEEKLY

    occurrence = db.session.query(TaskOccurrence).first()
    assert occurrence is not None
    # period_start fällt auf einen Dienstag.
    assert occurrence.period_start.weekday() == 1

    # Im Kalender der betreffenden Woche taucht der Titel auf.
    week_param = occurrence.period_start.strftime("%Y-%m-%d")
    cal = client.get(f"/tasks/alle?week={week_param}")
    assert cal.status_code == 200
    assert "Mülldienst".encode("utf-8") in cal.data


def test_resident_cannot_open_new_form(app):
    user = _make_user("Lina", role=Role.HAUSBEWOHNER)
    client = app.test_client()
    _login(client, user)

    response = client.get("/tasks/new")
    assert response.status_code == 403


def test_hauswart_can_create_one_time_task(app):
    hauswart = _make_user("Hannes", role=Role.HAUSWART)
    _make_user("Bewohner", role=Role.HAUSBEWOHNER)
    client = app.test_client()
    _login(client, hauswart)

    due = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d")
    response = client.post(
        "/tasks/",
        data={
            "title": "Fenster putzen",
            "description": "",
            "difficulty_points": "3",
            "recurrence": "NONE",
            "recurrence_interval_days": "",
            "required_assignees": "1",
            "due_date": due,
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    definitions = db.session.query(TaskDefinition).all()
    assert len(definitions) == 1
    assert definitions[0].title == "Fenster putzen"

    occurrences = db.session.query(TaskOccurrence).all()
    assert len(occurrences) == 1
    assert len(occurrences[0].assignments) == 1


def test_create_weekly_task_generates_two_occurrences(app):
    hauswart = _make_user("Hannes", role=Role.HAUSWART)
    _make_user("Bewohner", role=Role.HAUSBEWOHNER)
    client = app.test_client()
    _login(client, hauswart)

    response = client.post(
        "/tasks/",
        data={
            "title": "Mülldienst",
            "difficulty_points": "2",
            "recurrence": "WEEKLY",
            "required_assignees": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    occurrences = db.session.query(TaskOccurrence).all()
    assert len(occurrences) == 2


def test_new_form_has_no_rotation_field(app):
    hauswart = _make_user("Hannes", role=Role.HAUSWART)
    client = app.test_client()
    _login(client, hauswart)

    response = client.get("/tasks/new")
    assert response.status_code == 200
    assert b"rotation_period_days" not in response.data
    # Neue Felder sind vorhanden.
    assert b"anchor_weekday" in response.data
    assert b"anchor_day_of_month" in response.data


def test_create_monthly_task_sets_day_of_month(app):
    hauswart = _make_user("Hannes", role=Role.HAUSWART)
    _make_user("Bewohner", role=Role.HAUSBEWOHNER)
    client = app.test_client()
    _login(client, hauswart)

    response = client.post(
        "/tasks/",
        data={
            "title": "Großputz",
            "difficulty_points": "5",
            "recurrence": "MONTHLY",
            "anchor_day_of_month": "15",
            "required_assignees": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    definition = db.session.query(TaskDefinition).one()
    assert definition.anchor_day_of_month == 15
    assert definition.recurrence == Recurrence.MONTHLY

    occurrence = db.session.query(TaskOccurrence).first()
    assert occurrence is not None
    assert occurrence.period_start.day == 15


def test_create_rejects_bad_weekday(app):
    hauswart = _make_user("Hannes", role=Role.HAUSWART)
    client = app.test_client()
    _login(client, hauswart)

    response = client.post(
        "/tasks/",
        data={
            "title": "Mülldienst",
            "difficulty_points": "2",
            "recurrence": "WEEKLY",
            "anchor_weekday": "9",
            "required_assignees": "1",
        },
    )
    assert response.status_code == 400
    assert "Wochentag".encode("utf-8") in response.data


def test_mark_done_calendar_swap_returns_fragment(app):
    # Creator ist HAUSWART (darf anlegen) UND HAUSBEWOHNER (eligible, wird also
    # selbst zugewiesen) — so kann derselbe eingeloggte Client die Aufgabe
    # erledigen, ohne mitten im Test den User zu wechseln.
    user = _make_user("Hannes", role=Role.HAUSWART)
    db.session.add(UserRole(user_id=user.id, role=Role.HAUSBEWOHNER))
    db.session.commit()
    client = app.test_client()
    _login(client, user)

    # Einmalige Aufgabe heute → 1 Occurrence + 1 Assignment (an den Creator).
    today = date.today().strftime("%Y-%m-%d")
    client.post(
        "/tasks/",
        data={
            "title": "Spülen",
            "difficulty_points": "1",
            "recurrence": "NONE",
            "required_assignees": "1",
            "due_date": today,
        },
    )
    occurrence = db.session.query(TaskOccurrence).one()
    assert occurrence.assignments[0].user_id == user.id

    # view=week → Kalender-Fragment als HTMX-Swap zurück.
    resp = client.post(
        f"/tasks/{occurrence.id}/done?view=week",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert f"calendar-entry-{occurrence.id}".encode("utf-8") in resp.data
    assert "Erledigt".encode("utf-8") in resp.data


def test_create_rejects_missing_title(app):
    hauswart = _make_user("Hannes", role=Role.HAUSWART)
    client = app.test_client()
    _login(client, hauswart)

    response = client.post(
        "/tasks/",
        data={
            "title": "",
            "difficulty_points": "3",
            "recurrence": "WEEKLY",
            "required_assignees": "1",
        },
    )
    assert response.status_code == 400
    assert "Titel".encode("utf-8") in response.data


def test_new_form_offers_kind_selector(app):
    hauswart = _make_user("Hannes", role=Role.HAUSWART)
    client = app.test_client()
    _login(client, hauswart)

    response = client.get("/tasks/new")
    assert response.status_code == 200
    # Typ-Selector vorhanden mit beiden Optionen.
    assert b'name="kind"' in response.data
    assert b'value="AUFGABE"' in response.data
    assert b'value="DIENST"' in response.data
    assert "Dienst (Zeitraum-Verantwortung)".encode("utf-8") in response.data


def test_create_dienst_persists_kind(app):
    hauswart = _make_user("Hannes", role=Role.HAUSWART)
    _make_user("Bewohner", role=Role.HAUSBEWOHNER)
    client = app.test_client()
    _login(client, hauswart)

    response = client.post(
        "/tasks/",
        data={
            "title": "Mülldienst",
            "kind": "DIENST",
            "difficulty_points": "2",
            "recurrence": "WEEKLY",
            "anchor_weekday": "0",
            "required_assignees": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    definition = db.session.query(TaskDefinition).one()
    assert definition.kind == TaskKind.DIENST


def test_create_defaults_to_aufgabe_when_kind_missing(app):
    hauswart = _make_user("Hannes", role=Role.HAUSWART)
    _make_user("Bewohner", role=Role.HAUSBEWOHNER)
    client = app.test_client()
    _login(client, hauswart)

    response = client.post(
        "/tasks/",
        data={
            "title": "Spülen",
            "difficulty_points": "1",
            "recurrence": "WEEKLY",
            "anchor_weekday": "0",
            "required_assignees": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    definition = db.session.query(TaskDefinition).one()
    assert definition.kind == TaskKind.AUFGABE


def test_calendar_shows_dienst_badge(app):
    hauswart = _make_user("Hannes", role=Role.HAUSWART)
    _make_user("Bewohner", role=Role.HAUSBEWOHNER)
    client = app.test_client()
    _login(client, hauswart)

    client.post(
        "/tasks/",
        data={
            "title": "Mülldienst",
            "kind": "DIENST",
            "difficulty_points": "2",
            "recurrence": "WEEKLY",
            "anchor_weekday": "1",
            "required_assignees": "1",
        },
    )
    occurrence = db.session.query(TaskOccurrence).first()
    assert occurrence is not None

    week_param = occurrence.period_start.strftime("%Y-%m-%d")
    cal = client.get(f"/tasks/alle?week={week_param}")
    assert cal.status_code == 200
    assert "Mülldienst".encode("utf-8") in cal.data
    # Typ-Badge "Dienst" + violette Border (brand-400) sichtbar.
    assert b"Dienst" in cal.data
    assert b"border-brand-400" in cal.data


def test_deactivate_sets_flag(app):
    hauswart = _make_user("Hannes", role=Role.HAUSWART)
    client = app.test_client()
    _login(client, hauswart)

    definition = TaskDefinition(
        title="X", difficulty_points=1, required_assignees=1, is_active=True
    )
    db.session.add(definition)
    db.session.commit()

    response = client.post(f"/tasks/{definition.id}/deactivate")
    assert response.status_code == 302

    db.session.refresh(definition)
    assert definition.is_active is False


# ---------------------------------------------------------------------------
# Entwurf + Aktivieren (Bootstrapping vor dem WG-Start)
# ---------------------------------------------------------------------------


def test_create_as_draft_generates_no_occurrences(app):
    """Entwurf (wiederkehrend): is_active=False, keine Occurrence/Zuweisung."""

    hauswart = _make_user("Hannes", role=Role.HAUSWART)
    _make_user("Bewohner", role=Role.HAUSBEWOHNER)
    client = app.test_client()
    _login(client, hauswart)

    response = client.post(
        "/tasks/",
        data={
            "title": "Mülldienst",
            "difficulty_points": "2",
            "recurrence": "WEEKLY",
            "anchor_weekday": "1",
            "required_assignees": "1",
            "save_as_draft": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/tasks/verwalten")

    definition = db.session.query(TaskDefinition).one()
    assert definition.is_active is False
    assert db.session.query(TaskOccurrence).count() == 0


def test_save_as_draft_ignored_for_one_time_task(app):
    """Einmalige Aufgaben kennen keinen Entwurf — Flag wird ignoriert."""

    hauswart = _make_user("Hannes", role=Role.HAUSWART)
    db.session.add(UserRole(user_id=hauswart.id, role=Role.HAUSBEWOHNER))
    db.session.commit()
    client = app.test_client()
    _login(client, hauswart)

    due = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
    response = client.post(
        "/tasks/",
        data={
            "title": "Fenster putzen",
            "difficulty_points": "1",
            "recurrence": "NONE",
            "required_assignees": "1",
            "due_date": due,
            "save_as_draft": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    definition = db.session.query(TaskDefinition).one()
    assert definition.is_active is True
    assert db.session.query(TaskOccurrence).count() == 1


def test_activate_draft_generates_and_distributes(app):
    """Aktivieren erzeugt Occurrences und verteilt sie fair auf beide Bewohner."""

    hauswart = _make_user("Hannes", role=Role.HAUSWART)
    _make_user("Anna", role=Role.HAUSBEWOHNER)
    _make_user("Bea", role=Role.HAUSBEWOHNER)
    client = app.test_client()
    _login(client, hauswart)

    client.post(
        "/tasks/",
        data={
            "title": "Mülldienst",
            "difficulty_points": "2",
            "recurrence": "WEEKLY",
            "anchor_weekday": "1",
            "required_assignees": "1",
            "save_as_draft": "1",
        },
    )
    definition = db.session.query(TaskDefinition).one()
    assert db.session.query(TaskOccurrence).count() == 0

    response = client.post(f"/tasks/{definition.id}/activate")
    assert response.status_code == 302

    db.session.refresh(definition)
    assert definition.is_active is True

    occurrences = db.session.query(TaskOccurrence).all()
    assert len(occurrences) == 2
    assignee_ids = {a.user_id for o in occurrences for a in o.assignments}
    # Auf beide Bewohner verteilt — nicht alles auf eine Person.
    assert len(assignee_ids) == 2


def test_activate_all_activates_every_draft(app):
    hauswart = _make_user("Hannes", role=Role.HAUSWART)
    _make_user("Anna", role=Role.HAUSBEWOHNER)
    client = app.test_client()
    _login(client, hauswart)

    for title in ("Müll", "Putzen"):
        client.post(
            "/tasks/",
            data={
                "title": title,
                "difficulty_points": "1",
                "recurrence": "WEEKLY",
                "anchor_weekday": "0",
                "required_assignees": "1",
                "save_as_draft": "1",
            },
        )
    assert (
        db.session.query(TaskDefinition).filter_by(is_active=False).count() == 2
    )

    response = client.post("/tasks/aktivieren-alle")
    assert response.status_code == 302
    assert (
        db.session.query(TaskDefinition).filter_by(is_active=False).count() == 0
    )
    assert db.session.query(TaskOccurrence).count() > 0


def test_manage_page_lists_drafts(app):
    hauswart = _make_user("Hannes", role=Role.HAUSWART)
    client = app.test_client()
    _login(client, hauswart)

    client.post(
        "/tasks/",
        data={
            "title": "Putzdienst",
            "difficulty_points": "1",
            "recurrence": "WEEKLY",
            "anchor_weekday": "0",
            "required_assignees": "1",
            "save_as_draft": "1",
        },
    )

    resp = client.get("/tasks/verwalten")
    assert resp.status_code == 200
    assert "Entwurf".encode("utf-8") in resp.data
    assert "Putzdienst".encode("utf-8") in resp.data
    assert "Alle aktivieren".encode("utf-8") in resp.data


def test_resident_cannot_access_manage(app):
    user = _make_user("Lina", role=Role.HAUSBEWOHNER)
    client = app.test_client()
    _login(client, user)
    assert client.get("/tasks/verwalten").status_code == 403
