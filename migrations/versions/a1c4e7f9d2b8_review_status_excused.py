"""review_status: EXCUSED ergänzen (entschuldigt – neutral)

Revision ID: a1c4e7f9d2b8
Revises: b7e2d4a6c9f1
Create Date: 2026-06-03 12:00:00.000000

Fügt dem PG-nativen Enum ``review_status`` den dritten Review-Ausgang
``EXCUSED`` hinzu (Hauswart entschuldigt eine Zuweisung, z. B. bei Krankheit:
keine Punkte, keine Strafe). ``ALTER TYPE ... ADD VALUE`` läuft in PostgreSQL
nicht im selben Transaktionsblock, in dem der Wert anschließend genutzt würde,
daher der ``autocommit_block``.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a1c4e7f9d2b8'
down_revision: Union[str, Sequence[str], None] = 'b7e2d4a6c9f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE review_status ADD VALUE IF NOT EXISTS 'EXCUSED'")


def downgrade() -> None:
    """Downgrade schema."""
    # PostgreSQL kann einen einzelnen Enum-Wert nicht direkt entfernen (nur per
    # vollständigem Neuaufbau des Typs, der an bestehenden Zeilen scheitern
    # würde). Bewusst No-op — der zusätzliche Wert ist harmlos, wenn ungenutzt.
    pass
