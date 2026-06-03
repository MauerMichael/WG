"""Alembic-Environment für die WG-App.

Holt die SQLAlchemy-Metadaten direkt aus `app.extensions.db.metadata`. Das
Importieren von `app.models` (Star-Import) registriert alle Tabellen, ohne
dass wir hier jedes Modell namentlich aufzählen müssen. Die DB-URL kommt
nicht aus `alembic.ini`, sondern aus der Flask-Config (die wiederum aus
`.env` liest).
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# Projekt-Root auf sys.path, damit `import app` funktioniert,
# wenn alembic aus dem Projekt-Root ausgeführt wird.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import *  # noqa: F401, F403, E402  – registriert alle Modelle

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Flask-App nur einmalig bauen, um SQLALCHEMY_DATABASE_URI zu holen.
_flask_app = create_app("dev")
config.set_main_option(
    "sqlalchemy.url",
    _flask_app.config["SQLALCHEMY_DATABASE_URI"],
)

target_metadata = db.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
