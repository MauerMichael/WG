"""Pytest-Fixtures für die WG-App.

Liefert eine konfigurierte Flask-App und einen Test-Client. Damit die
Scheduling- und Tasks-Tests ohne Remote-Postgres laufen, zwingen wir hier vor
dem ersten App-Import die ``DATABASE_URL`` auf SQLite und registrieren einen
SQLite-Compiler-Fallback für die PG-spezifische ``JSONB``-Spalte (nur
``AuditLog.payload`` nutzt sie und wird von den Tests nie geschrieben).
"""

from __future__ import annotations

import os

# WICHTIG: VOR dem ``from app import …`` ausführen, damit ``app.config`` beim
# Modul-Import direkt SQLite sieht und nicht erst die remote Postgres-URL aus
# ``.env`` cached.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import pytest  # noqa: E402
from flask import Flask  # noqa: E402
from flask.testing import FlaskClient  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402

# Modelle muessen vor create_all() importiert sein, damit ihre Tabellen registriert sind.
from app import models  # noqa: E402,F401


@compiles(JSONB, "sqlite")
def _jsonb_to_sqlite_text(_element, _compiler, **_kw):  # noqa: ANN001
    return "TEXT"


@pytest.fixture()
def app() -> Flask:
    application = create_app("dev")
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with application.app_context():
        db.create_all()
        try:
            yield application
        finally:
            db.session.remove()
            db.drop_all()


@pytest.fixture()
def client(app: Flask) -> FlaskClient:
    return app.test_client()
