"""password auth (Username + Passwort statt Google-OAuth)

Revision ID: d4f1a8b2c903
Revises: c2f8a1b3d4e6
Create Date: 2026-06-09 12:00:00.000000

Schema-Migration fuer den Wechsel von Google-OAuth auf klassisches
Username+Passwort-Login:

- ``username`` (String 64, unique, NOT NULL) wird neu eingefuehrt. Bestands-Rows
  werden aus der bisherigen E-Mail befuellt (Substring vor dem ``@``).
- ``password_hash`` (nullable) speichert den Werkzeug-pbkdf2-Hash.
- ``must_change_password`` (Boolean, NOT NULL, default true) erzwingt beim
  ersten Login einen Passwort-Wechsel.
- ``email`` wird auf ``nullable=True`` gelockert (vorher NOT NULL). Bestehende
  Werte bleiben erhalten, koennen aber bei neu angelegten Accounts fehlen.

``google_sub`` bleibt absichtlich erhalten (additiv, keine Drop-Migration).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4f1a8b2c903'
down_revision: Union[str, Sequence[str], None] = 'c2f8a1b3d4e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 1) Neue Spalten zuerst nullable hinzufuegen, damit Bestands-Rows nicht
    #    sofort einen Wert brauchen.
    op.add_column(
        'users',
        sa.Column('username', sa.String(length=64), nullable=True),
    )
    op.add_column(
        'users',
        sa.Column('password_hash', sa.String(length=255), nullable=True),
    )
    op.add_column(
        'users',
        sa.Column(
            'must_change_password',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
    )

    # 2) E-Mail auf nullable=True lockern.
    op.alter_column(
        'users',
        'email',
        existing_type=sa.String(length=255),
        nullable=True,
    )

    # 3) Backfill username aus email. Auf Postgres POSITION/SUBSTRING, sonst
    #    Fallback fuer SQLite (Tests laufen leer durch, weil die Migration im
    #    Test-Setup nie ausgefuehrt wird — create_all() macht das Schema; trotzdem
    #    duerfen wir keine PG-only Syntax in der Migration roh werfen, falls die
    #    Migration jemals gegen SQLite gefahren wird).
    if dialect == 'postgresql':
        op.execute(
            """
            UPDATE users
               SET username = CASE
                 WHEN email IS NULL THEN NULL
                 WHEN position('@' in email) > 0
                   THEN lower(substring(email FROM 1 FOR position('@' in email) - 1))
                 ELSE lower(email)
               END
             WHERE username IS NULL
            """
        )
    else:
        # SQLite (Tests): instr() statt position(), lower() + substr().
        op.execute(
            """
            UPDATE users
               SET username = CASE
                 WHEN email IS NULL THEN NULL
                 WHEN instr(email, '@') > 0
                   THEN lower(substr(email, 1, instr(email, '@') - 1))
                 ELSE lower(email)
               END
             WHERE username IS NULL
            """
        )

    # 4) username NOT NULL setzen.
    op.alter_column(
        'users',
        'username',
        existing_type=sa.String(length=64),
        nullable=False,
    )

    # 5) Unique-Constraint auf username.
    op.create_unique_constraint('uq_users_username', 'users', ['username'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('uq_users_username', 'users', type_='unique')
    # E-Mail wieder NOT NULL — nur sinnvoll, wenn alle Rows eine Mail haben.
    op.alter_column(
        'users',
        'email',
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.drop_column('users', 'must_change_password')
    op.drop_column('users', 'password_hash')
    op.drop_column('users', 'username')
