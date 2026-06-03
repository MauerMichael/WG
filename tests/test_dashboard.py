"""Tests für das Dashboard (`/`).

Wir patchen DB- und Auth-Layer, damit die Tests ohne Postgres-Verbindung und
ohne echte Google-OAuth-Session laufen können.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app.blueprints import dashboard as dashboard_bp
from app.domain.enums import (
    AssignmentStatus,
    Recurrence,
    TaskKind,
    TaskStatus,
    UserStatus,
)
from app.services import notifications


# ---------------------------------------------------------------------------
# Helpers / Fakes
# ---------------------------------------------------------------------------


def _fake_user(name: str = "Mia Mustermensch", email: str = "mia@example.com"):
    user = SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        email=email,
        status=UserStatus.APPROVED,
        is_authenticated=True,
        is_active=True,
        is_anonymous=False,
    )
    user.get_id = lambda: str(user.id)
    return user


def _fake_assignment_row(title: str, points: int, due: date, kind: TaskKind = TaskKind.AUFGABE):
    # `recurrence` + `required_assignees` werden vom `task_type_*`-Macro
    # gelesen (Welle 1) — auch fuer AUFGABE-Tests noetig.
    definition = SimpleNamespace(
        title=title,
        difficulty_points=points,
        kind=kind,
        recurrence=Recurrence.NONE,
        required_assignees=1,
    )
    occurrence = SimpleNamespace(
        id=uuid.uuid4(),
        period_start=due,
        period_end=due,
        due_date=due,
        status=TaskStatus.OPEN,
    )
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        status=AssignmentStatus.OPEN,
    )
    return assignment, occurrence, definition


def _fake_duty(title: str, points: int, period_end: date):
    """Eine laufende DIENST-Occurrence für den Banner."""
    definition = SimpleNamespace(
        id=uuid.uuid4(),
        title=title,
        difficulty_points=points,
        kind=TaskKind.DIENST,
        recurrence=Recurrence.WEEKLY,
        required_assignees=1,
    )
    occurrence = SimpleNamespace(
        id=uuid.uuid4(),
        period_start=period_end - timedelta(days=7),
        period_end=period_end,
        due_date=period_end,
        status=TaskStatus.OPEN,
        task_definition=definition,
        assignments=[],
    )
    return occurrence


@pytest.fixture()
def stub_auth(app: Flask, monkeypatch: pytest.MonkeyPatch):
    """Erlaubt allen Requests durch und setzt einen approved Fake-User."""
    user = _fake_user()

    # Auth-Guard: jede Request akzeptieren.
    for func_list in app.before_request_funcs.values():
        func_list.clear()

    # current_user-Proxy im Dashboard-Modul ersetzen.
    monkeypatch.setattr(dashboard_bp, "current_user", user, raising=True)
    # login_required ist ein Decorator, der current_user prüft — er nutzt den
    # globalen Flask-Login-Proxy. Wir deaktivieren ihn via Config.
    app.config["LOGIN_DISABLED"] = True

    return user


@pytest.fixture()
def stub_db(monkeypatch: pytest.MonkeyPatch):
    """Hält ``db.session`` ruhig und liefert konfigurierbare Antworten."""
    state: dict[str, Any] = {
        "today_rows": [],
        "week_rows": [],
        "shopping": [],
        "absences": [],
        "duties": [],
        "stats": {"completed": 0, "points": 0, "missed": 0, "reliability": None},
    }

    def fake_today(_session, _user, today=None):  # noqa: ARG001
        return state["today_rows"]

    def fake_week(_session, _user, today=None, exclude_today=True):  # noqa: ARG001
        return state["week_rows"]

    monkeypatch.setattr(dashboard_bp, "assignments_for_today", fake_today)
    monkeypatch.setattr(dashboard_bp, "assignments_for_this_week", fake_week)
    monkeypatch.setattr(
        dashboard_bp, "_top_shopping_items", lambda limit=5: state["shopping"][:limit]
    )
    monkeypatch.setattr(
        dashboard_bp,
        "_upcoming_absences",
        lambda today, days=14: state["absences"],
    )
    monkeypatch.setattr(dashboard_bp, "_current_duties", lambda today: state["duties"])
    monkeypatch.setattr(dashboard_bp, "_user_stats", lambda: state["stats"])
    # Score-Helper ebenfalls neutralisieren (scheduling-Stub kann (None,None) liefern,
    # aber wir wollen es in den Tests deterministisch halten).
    monkeypatch.setattr(
        dashboard_bp, "_household_score_summary", lambda today: (None, None)
    )

    return state


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dashboard_renders_sections(
    client: FlaskClient, stub_auth, stub_db
) -> None:
    today = date.today()
    stub_db["today_rows"] = [_fake_assignment_row("Müll rausbringen", 3, today)]
    stub_db["week_rows"] = [
        _fake_assignment_row("Küche putzen", 5, today + timedelta(days=2))
    ]

    response = client.get("/")
    assert response.status_code == 200
    body = response.data.decode("utf-8")

    assert "Heute" in body
    assert "Diese Woche" in body
    assert "Müll rausbringen" in body
    assert "Küche putzen" in body
    assert f"Hallo, {stub_auth.name.split(' ')[0]}" in body


def test_dashboard_empty_state(
    client: FlaskClient, stub_auth, stub_db
) -> None:
    # Keine Aufgaben in keinem Bucket.
    response = client.get("/")
    assert response.status_code == 200
    body = response.data.decode("utf-8")

    assert "Keine Aufgaben heute." in body
    assert "Keine weiteren Aufgaben in dieser Woche." in body
    # Sicherstellen, dass keine Emojis im Empty-State stehen.
    assert "🎉" not in body


def test_dashboard_renders_stats_section(
    client: FlaskClient, stub_auth, stub_db
) -> None:
    stub_db["stats"] = {
        "completed": 7,
        "points": 21,
        "missed": 2,
        "reliability": 0.78,
    }

    response = client.get("/")
    assert response.status_code == 200
    body = response.data.decode("utf-8")

    assert "Deine Statistik" in body
    # Kompakte Stat-Kacheln (mobil 3-spaltig).
    assert "erledigt" in body
    assert "Punkte" in body
    assert "verpasst" in body
    assert "Zuverlässigkeit" in body
    # 0.78 -> 78%
    assert "78%" in body


def test_dashboard_stats_reliability_none_shows_dash(
    client: FlaskClient, stub_auth, stub_db
) -> None:
    stub_db["stats"] = {
        "completed": 0,
        "points": 0,
        "missed": 0,
        "reliability": None,
    }

    response = client.get("/")
    assert response.status_code == 200
    body = response.data.decode("utf-8")

    assert "Deine Statistik" in body
    # reliability None -> Strich statt Prozentwert.
    assert "—" in body


def test_dashboard_shows_current_duty_banner(
    client: FlaskClient, stub_auth, stub_db
) -> None:
    period_end = date.today() + timedelta(days=4)
    stub_db["duties"] = [_fake_duty("Mülldienst", 3, period_end)]

    response = client.get("/")
    assert response.status_code == 200
    body = response.data.decode("utf-8")

    assert "Dein Dienst" in body
    assert "Mülldienst" in body
    assert f"bis {period_end.strftime('%d.%m.')}" in body


def test_dashboard_no_duty_banner_when_empty(
    client: FlaskClient, stub_auth, stub_db
) -> None:
    # Keine laufenden Dienste -> Banner rendert nichts.
    response = client.get("/")
    assert response.status_code == 200
    body = response.data.decode("utf-8")

    assert "Dein Dienst" not in body


def test_format_de_helper() -> None:
    # 2026-05-27 ist ein Mittwoch.
    assert notifications.format_de(date(2026, 5, 27)) == "Mittwoch, 27. Mai 2026"


def test_week_bounds_helper() -> None:
    monday, sunday = notifications.week_bounds(date(2026, 5, 27))
    assert monday == date(2026, 5, 25)
    assert sunday == date(2026, 5, 31)
