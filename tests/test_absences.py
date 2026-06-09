"""Tests für den Absences-Blueprint.

Strategie: weil mehrere Agenten parallel an derselben remote DB arbeiten und
das Schema (alembic migration) zwischenzeitlich gedroppt/wiederhergestellt
wird, fahren wir die Tests gegen ein *frisches In-Memory-SQLite* und legen
das Schema via ``db.create_all()`` an. Die PostgreSQL-UUID-Typen funktionieren
unter SQLite, weil SQLAlchemy für SQLite einen CHAR(32)-Fallback nutzt.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app import create_app
from app.domain.enums import Role, UserStatus
from app.extensions import db
from app.models.absence import Absence
from app.models.user import User, UserRole


# ---------------------------------------------------------------------------
# Lokale App- und DB-Fixtures (eigener Scope, kollidiert nicht mit conftest)
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(monkeypatch: pytest.MonkeyPatch) -> Flask:
    """Eigene App mit In-Memory-SQLite — kein Race mit anderen Agenten.

    Wir patchen die Config-Klasse, BEVOR ``create_app`` die DB-Engine bindet.
    Auf SQLite mappt SQLAlchemy ``UUID(as_uuid=True)`` automatisch auf CHAR(32).
    """
    from app.config import DevConfig

    monkeypatch.setattr(DevConfig, "SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
    application = create_app("dev")
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with application.app_context():
        # AuditLog hat eine JSONB-Spalte → unter SQLite nicht erzeugbar.
        # Wir bauen daher nur die Tabellen, die unsere Tests brauchen.
        tables = [
            User.__table__,
            UserRole.__table__,
            Absence.__table__,
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

    Wir poppen ``g._login_user``, weil die Fixture den AppContext über mehrere
    Requests offen hält und Flask-Login sonst den User der vorherigen Request
    weiterverwendet.
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


def test_create_absence_calls_reassign(app: Flask, client: FlaskClient) -> None:
    user = _mk_user("Alice", roles=[Role.HAUSBEWOHNER])
    _login(client, user)

    start = date.today() + timedelta(days=1)
    end = date.today() + timedelta(days=3)

    with patch("app.blueprints.absences.routes.reassign_open_overlap") as mock_reassign:
        mock_reassign.return_value = 2
        resp = client.post(
            "/absences/",
            data={
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "reason": "Urlaub",
            },
            follow_redirects=False,
        )

    assert resp.status_code in (302, 303)
    mock_reassign.assert_called_once()
    args, _kwargs = mock_reassign.call_args
    # Signatur: reassign_open_overlap(session, user, start_date, end_date)
    assert args[1].id == user.id
    assert args[2] == start
    assert args[3] == end

    found = db.session.query(Absence).filter_by(user_id=user.id).one_or_none()
    assert found is not None
    assert found.start_date == start
    assert found.end_date == end
    assert found.reason == "Urlaub"


def test_create_absence_end_before_start_is_400(app: Flask, client: FlaskClient) -> None:
    user = _mk_user("Bob", roles=[Role.HAUSBEWOHNER])
    _login(client, user)

    start = date.today() + timedelta(days=5)
    end = date.today() + timedelta(days=1)

    with patch("app.blueprints.absences.routes.reassign_open_overlap") as mock_reassign:
        resp = client.post(
            "/absences/",
            data={
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            },
        )

    assert resp.status_code == 400
    mock_reassign.assert_not_called()
    assert db.session.query(Absence).filter_by(user_id=user.id).count() == 0


def test_non_owner_cannot_delete_other_users_absence(app: Flask, client: FlaskClient) -> None:
    alice = _mk_user("Alice", roles=[Role.HAUSBEWOHNER])
    bob = _mk_user("Bob", roles=[Role.HAUSBEWOHNER])
    today = date.today()
    absence = Absence(
        user_id=alice.id,
        start_date=today,
        end_date=today,
        reason="privat",
    )
    db.session.add(absence)
    db.session.commit()

    _login(client, bob)
    resp = client.post(f"/absences/{absence.id}/delete")
    assert resp.status_code == 403
    assert db.session.get(Absence, absence.id) is not None


def test_owner_can_delete_own_absence(app: Flask, client: FlaskClient) -> None:
    alice = _mk_user("Alice", roles=[Role.HAUSBEWOHNER])
    today = date.today()
    absence = Absence(
        user_id=alice.id,
        start_date=today,
        end_date=today,
    )
    db.session.add(absence)
    db.session.commit()

    _login(client, alice)
    resp = client.post(f"/absences/{absence.id}/delete")
    assert resp.status_code in (302, 303)
    assert db.session.get(Absence, absence.id) is None


def test_hauswart_can_delete_others_absence(app: Flask, client: FlaskClient) -> None:
    alice = _mk_user("Alice", roles=[Role.HAUSBEWOHNER])
    hauswart = _mk_user("Heinz", roles=[Role.HAUSBEWOHNER, Role.HAUSWART])
    today = date.today()
    absence = Absence(
        user_id=alice.id,
        start_date=today,
        end_date=today,
    )
    db.session.add(absence)
    db.session.commit()

    _login(client, hauswart)
    resp = client.post(f"/absences/{absence.id}/delete")
    assert resp.status_code in (302, 303)
    assert db.session.get(Absence, absence.id) is None


# ---------------------------------------------------------------------------
# Grid-Index
# ---------------------------------------------------------------------------


def test_grid_index_returns_200_with_names_and_cells(
    app: Flask, client: FlaskClient
) -> None:
    alice = _mk_user("Alice", roles=[Role.HAUSBEWOHNER])
    _mk_user("Bob", roles=[Role.HAUSBEWOHNER])
    _login(client, alice)

    # 2026-05-28 ist ein Donnerstag → die Woche läuft Mo 25.05. bis So 31.05.
    resp = client.get("/absences/?week=2026-05-28")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Alice" in body
    assert "Bob" in body
    # Genau 7 Tage: Montag (25.) und Sonntag (31.) der Woche müssen da sein.
    assert f"cell-{alice.id}-2026-05-25" in body
    assert f"cell-{alice.id}-2026-05-31" in body
    # Tage außerhalb der Woche dürfen NICHT erscheinen.
    assert f"cell-{alice.id}-2026-05-24" not in body


def test_grid_index_defaults_to_current_week(
    app: Flask, client: FlaskClient
) -> None:
    alice = _mk_user("Alice", roles=[Role.HAUSBEWOHNER])
    _login(client, alice)

    resp = client.get("/absences/")
    assert resp.status_code == 200
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    body = resp.get_data(as_text=True)
    assert f"cell-{alice.id}-{monday.isoformat()}" in body


# ---------------------------------------------------------------------------
# Toggle
# ---------------------------------------------------------------------------


def _toggle(client: FlaskClient, user: User, day: date):
    return client.post(
        "/absences/toggle",
        data={"user_id": str(user.id), "date": day.isoformat()},
        headers={"HX-Request": "true"},
    )


def test_toggle_present_to_absent_creates_single_day(
    app: Flask, client: FlaskClient
) -> None:
    alice = _mk_user("Alice", roles=[Role.HAUSBEWOHNER])
    _login(client, alice)
    day = date(2026, 5, 15)

    with patch("app.blueprints.absences.routes.reassign_open_overlap") as mock_reassign:
        mock_reassign.return_value = 0
        resp = _toggle(client, alice, day)

    assert resp.status_code == 200
    rows = db.session.query(Absence).filter_by(user_id=alice.id).all()
    assert len(rows) == 1
    assert rows[0].start_date == day
    assert rows[0].end_date == day
    mock_reassign.assert_called_once()
    args, _ = mock_reassign.call_args
    assert args[1].id == alice.id
    assert args[2] == day
    assert args[3] == day


def test_toggle_absent_to_present_deletes_single_day(
    app: Flask, client: FlaskClient
) -> None:
    alice = _mk_user("Alice", roles=[Role.HAUSBEWOHNER])
    day = date(2026, 5, 15)
    db.session.add(Absence(user_id=alice.id, start_date=day, end_date=day))
    db.session.commit()
    _login(client, alice)

    with patch("app.blueprints.absences.routes.reassign_open_overlap") as mock_reassign:
        mock_reassign.return_value = 0
        resp = _toggle(client, alice, day)

    assert resp.status_code == 200
    assert db.session.query(Absence).filter_by(user_id=alice.id).count() == 0


def test_toggle_middle_of_range_splits_into_two(
    app: Flask, client: FlaskClient
) -> None:
    alice = _mk_user("Alice", roles=[Role.HAUSBEWOHNER])
    start = date(2026, 5, 10)
    end = date(2026, 5, 20)
    punch = date(2026, 5, 15)
    db.session.add(
        Absence(user_id=alice.id, start_date=start, end_date=end, reason="Urlaub")
    )
    db.session.commit()
    _login(client, alice)

    with patch("app.blueprints.absences.routes.reassign_open_overlap") as mock_reassign:
        mock_reassign.return_value = 0
        resp = _toggle(client, alice, punch)

    assert resp.status_code == 200
    rows = (
        db.session.query(Absence)
        .filter_by(user_id=alice.id)
        .order_by(Absence.start_date)
        .all()
    )
    assert len(rows) == 2
    assert (rows[0].start_date, rows[0].end_date) == (start, punch - timedelta(days=1))
    assert (rows[1].start_date, rows[1].end_date) == (punch + timedelta(days=1), end)
    # Grund wird in die neue Hälfte kopiert.
    assert rows[1].reason == "Urlaub"
    # Der gestanzte Tag ist in keiner der beiden Ranges mehr.
    for r in rows:
        assert not (r.start_date <= punch <= r.end_date)


def test_toggle_start_edge_shrinks_range(app: Flask, client: FlaskClient) -> None:
    alice = _mk_user("Alice", roles=[Role.HAUSBEWOHNER])
    start = date(2026, 5, 10)
    end = date(2026, 5, 20)
    db.session.add(Absence(user_id=alice.id, start_date=start, end_date=end))
    db.session.commit()
    _login(client, alice)

    with patch("app.blueprints.absences.routes.reassign_open_overlap") as mock_reassign:
        mock_reassign.return_value = 0
        resp = _toggle(client, alice, start)

    assert resp.status_code == 200
    rows = db.session.query(Absence).filter_by(user_id=alice.id).all()
    assert len(rows) == 1
    assert rows[0].start_date == start + timedelta(days=1)
    assert rows[0].end_date == end


def test_toggle_end_edge_shrinks_range(app: Flask, client: FlaskClient) -> None:
    alice = _mk_user("Alice", roles=[Role.HAUSBEWOHNER])
    start = date(2026, 5, 10)
    end = date(2026, 5, 20)
    db.session.add(Absence(user_id=alice.id, start_date=start, end_date=end))
    db.session.commit()
    _login(client, alice)

    with patch("app.blueprints.absences.routes.reassign_open_overlap") as mock_reassign:
        mock_reassign.return_value = 0
        resp = _toggle(client, alice, end)

    assert resp.status_code == 200
    rows = db.session.query(Absence).filter_by(user_id=alice.id).all()
    assert len(rows) == 1
    assert rows[0].start_date == start
    assert rows[0].end_date == end - timedelta(days=1)


def test_hausbewohner_cannot_toggle_other_users_cell(
    app: Flask, client: FlaskClient
) -> None:
    alice = _mk_user("Alice", roles=[Role.HAUSBEWOHNER])
    bob = _mk_user("Bob", roles=[Role.HAUSBEWOHNER])
    _login(client, bob)
    day = date(2026, 5, 15)

    with patch("app.blueprints.absences.routes.reassign_open_overlap") as mock_reassign:
        resp = _toggle(client, alice, day)

    assert resp.status_code == 403
    mock_reassign.assert_not_called()
    assert db.session.query(Absence).filter_by(user_id=alice.id).count() == 0


def test_hausbewohner_can_toggle_own_cell(app: Flask, client: FlaskClient) -> None:
    bob = _mk_user("Bob", roles=[Role.HAUSBEWOHNER])
    _login(client, bob)
    day = date(2026, 5, 15)

    with patch("app.blueprints.absences.routes.reassign_open_overlap") as mock_reassign:
        mock_reassign.return_value = 0
        resp = _toggle(client, bob, day)

    assert resp.status_code == 200
    assert db.session.query(Absence).filter_by(user_id=bob.id).count() == 1


def test_hauswart_can_toggle_other_users_cell(
    app: Flask, client: FlaskClient
) -> None:
    alice = _mk_user("Alice", roles=[Role.HAUSBEWOHNER])
    hauswart = _mk_user("Heinz", roles=[Role.HAUSBEWOHNER, Role.HAUSWART])
    _login(client, hauswart)
    day = date(2026, 5, 15)

    with patch("app.blueprints.absences.routes.reassign_open_overlap") as mock_reassign:
        mock_reassign.return_value = 0
        resp = _toggle(client, alice, day)

    assert resp.status_code == 200
    assert db.session.query(Absence).filter_by(user_id=alice.id).count() == 1


def test_toggle_non_htmx_redirects_to_week(app: Flask, client: FlaskClient) -> None:
    alice = _mk_user("Alice", roles=[Role.HAUSBEWOHNER])
    _login(client, alice)
    day = date(2026, 5, 15)

    with patch("app.blueprints.absences.routes.reassign_open_overlap") as mock_reassign:
        mock_reassign.return_value = 0
        resp = client.post(
            "/absences/toggle",
            data={"user_id": str(alice.id), "date": day.isoformat()},
        )

    assert resp.status_code in (302, 303)
    assert "week=2026-05-15" in resp.headers["Location"]
