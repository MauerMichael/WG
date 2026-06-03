"""Tests für ``app.services.notifications``.

Wir fassen SMTP nicht an — alles Versand-relevante wird gepatcht. Die Tests
laufen ohne DB, indem wir die Query-Helfer monkeypatchen.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from flask import Flask

from app.domain.enums import AssignmentStatus, TaskStatus, UserStatus
from app.services import notifications


def _user(name: str = "Mia Mustermensch", email: str = "mia@example.com"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        email=email,
        status=UserStatus.APPROVED,
    )


def _row(title: str, points: int, due: date):
    definition = SimpleNamespace(title=title, difficulty_points=points)
    occurrence = SimpleNamespace(
        id=uuid.uuid4(),
        period_start=due,
        period_end=due,
        due_date=due,
        status=TaskStatus.OPEN,
    )
    assignment = SimpleNamespace(id=uuid.uuid4(), status=AssignmentStatus.OPEN)
    return assignment, occurrence, definition


# ---------------------------------------------------------------------------
# compose_daily_digest
# ---------------------------------------------------------------------------


def test_compose_digest_returns_subject_and_body(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user()
    today = date(2026, 5, 27)
    today_rows = [_row("Müll rausbringen", 3, today)]
    week_rows = [_row("Küche putzen", 5, today + timedelta(days=2))]

    monkeypatch.setattr(
        notifications, "assignments_for_today", lambda _s, _u, today=None: today_rows
    )
    monkeypatch.setattr(
        notifications,
        "assignments_for_this_week",
        lambda _s, _u, today=None, exclude_today=True: week_rows,
    )

    with app.test_request_context("/"):
        result = notifications.compose_daily_digest(None, user, today=today)

    assert result is not None
    subject, html_body = result
    assert subject == "WG-Aufgaben für heute (1 offen)"
    assert "Hallo Mia" in html_body
    assert "Müll rausbringen" in html_body
    assert "Küche putzen" in html_body
    assert "Mittwoch, 27. Mai 2026" in html_body


def test_compose_digest_returns_none_when_no_assignments(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        notifications, "assignments_for_today", lambda _s, _u, today=None: []
    )
    monkeypatch.setattr(
        notifications,
        "assignments_for_this_week",
        lambda _s, _u, today=None, exclude_today=True: [],
    )

    with app.test_request_context("/"):
        result = notifications.compose_daily_digest(None, _user(), today=date(2026, 5, 27))

    assert result is None


# ---------------------------------------------------------------------------
# send_email
# ---------------------------------------------------------------------------


class _FakeSMTP:
    """Minimal-Drop-In für ``smtplib.SMTP`` als Context-Manager."""

    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sent: list = []
        self.logged_in: tuple | None = None
        self.starttls_called = False
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def ehlo(self):
        pass

    def starttls(self):
        self.starttls_called = True

    def login(self, user, pw):
        self.logged_in = (user, pw)

    def send_message(self, msg):
        self.sent.append(msg)


def test_send_email_uses_smtp_config(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeSMTP.instances.clear()
    app.config["SMTP_HOST"] = "smtp.example.com"
    app.config["SMTP_PORT"] = 2525
    app.config["SMTP_USER"] = "wg-bot"
    app.config["SMTP_PASSWORD"] = "secret"
    app.config["SMTP_FROM"] = "wg-bot@example.com"

    monkeypatch.setattr("app.services.notifications.smtplib.SMTP", _FakeSMTP)

    with app.app_context():
        notifications.send_email(
            "mia@example.com",
            "Test",
            "<p>Hallo <strong>Mia</strong></p>",
        )

    assert len(_FakeSMTP.instances) == 1
    smtp = _FakeSMTP.instances[0]
    assert smtp.host == "smtp.example.com"
    assert smtp.port == 2525
    assert smtp.logged_in == ("wg-bot", "secret")
    assert len(smtp.sent) == 1
    msg = smtp.sent[0]
    assert msg["To"] == "mia@example.com"
    assert msg["From"] == "wg-bot@example.com"
    assert msg["Subject"] == "Test"


def test_send_email_raises_without_smtp_host(app: Flask) -> None:
    app.config["SMTP_HOST"] = None
    with app.app_context():
        with pytest.raises(RuntimeError, match="SMTP_HOST"):
            notifications.send_email("mia@example.com", "x", "<p>x</p>")


# ---------------------------------------------------------------------------
# send_daily_digests
# ---------------------------------------------------------------------------


def test_send_daily_digests_counts_sent(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_a = _user(email="a@example.com")
    user_b = _user(email="b@example.com")
    user_c = _user(email="c@example.com")  # ohne Aufgaben → kein Versand.

    # ``send_daily_digests`` lädt User über ``session.execute`` — wir patchen
    # statt einer echten Session ein Stub-Objekt.
    class _ScalarResult:
        def __init__(self, items):
            self._items = items

        def all(self):
            return self._items

    class _ExecResult:
        def __init__(self, items):
            self._items = items

        def scalars(self):
            return _ScalarResult(self._items)

    fake_session = SimpleNamespace(
        execute=lambda _stmt: _ExecResult([user_a, user_b, user_c])
    )

    def _compose(_session, user, today=None):
        if user.email == "c@example.com":
            return None
        return (f"Subj für {user.email}", f"<p>Body für {user.email}</p>")

    monkeypatch.setattr(notifications, "compose_daily_digest", _compose)

    sent_to: list[str] = []

    def _send(to, subject, html_body):  # noqa: ARG001
        sent_to.append(to)

    monkeypatch.setattr(notifications, "send_email", _send)

    with app.app_context():
        count = notifications.send_daily_digests(fake_session, today=date(2026, 5, 27))

    assert count == 2
    assert sent_to == ["a@example.com", "b@example.com"]


def test_send_daily_digests_raises_on_failure(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(email="boom@example.com")

    class _ScalarResult:
        def __init__(self, items):
            self._items = items

        def all(self):
            return self._items

    class _ExecResult:
        def __init__(self, items):
            self._items = items

        def scalars(self):
            return _ScalarResult(self._items)

    fake_session = SimpleNamespace(execute=lambda _stmt: _ExecResult([user]))

    monkeypatch.setattr(
        notifications,
        "compose_daily_digest",
        lambda _s, _u, today=None: ("S", "<p>B</p>"),
    )

    def _boom(to, subject, html_body):  # noqa: ARG001
        raise RuntimeError("SMTP down")

    monkeypatch.setattr(notifications, "send_email", _boom)

    with app.app_context():
        with pytest.raises(RuntimeError, match="konnten nicht verschickt werden"):
            notifications.send_daily_digests(fake_session, today=date(2026, 5, 27))
