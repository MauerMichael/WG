"""task handover offers (Aufgaben-Börse)

Revision ID: c2f8a1b3d4e6
Revises: a1c4e7f9d2b8
Create Date: 2026-06-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c2f8a1b3d4e6'
down_revision: Union[str, Sequence[str], None] = 'a1c4e7f9d2b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()

    # Neuer Enum-Typ — expliziter CREATE TYPE für pg8000 (legt ihn nicht selbst
    # via add_column an). Die Enum-Spalte wird per add_column (create_type=False)
    # angehängt, damit create_table nicht erneut CREATE TYPE versucht.
    handover_status = sa.Enum(
        'OPEN', 'CLAIMED', 'CANCELLED', name='handover_status'
    )
    handover_status.create(bind, checkfirst=True)
    handover_status_col = sa.Enum(
        'OPEN', 'CLAIMED', 'CANCELLED', name='handover_status', create_type=False
    )

    op.create_table(
        'task_handover_offers',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('assignment_id', sa.UUID(), nullable=False),
        sa.Column('offered_by_id', sa.UUID(), nullable=False),
        sa.Column('note', sa.String(length=500), nullable=True),
        sa.Column('claimed_by_id', sa.UUID(), nullable=True),
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['assignment_id'], ['task_assignments.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(
            ['offered_by_id'], ['users.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(
            ['claimed_by_id'], ['users.id'], ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    # NOT-NULL-Enum-Spalte mit kurzlebigem server_default (Tabelle leer); danach
    # wieder entfernen, damit der ORM-Default die alleinige Quelle bleibt.
    op.add_column(
        'task_handover_offers',
        sa.Column(
            'status', handover_status_col, nullable=False, server_default='OPEN'
        ),
    )
    op.alter_column('task_handover_offers', 'status', server_default=None)

    op.create_index(
        'ix_task_handover_offers_assignment_id',
        'task_handover_offers',
        ['assignment_id'],
    )
    # Höchstens EIN offenes Angebot pro Assignment (partielles Unique).
    op.create_index(
        'uq_handover_one_open_per_assignment',
        'task_handover_offers',
        ['assignment_id'],
        unique=True,
        postgresql_where=sa.text("status = 'OPEN'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        'uq_handover_one_open_per_assignment',
        table_name='task_handover_offers',
    )
    op.drop_index(
        'ix_task_handover_offers_assignment_id',
        table_name='task_handover_offers',
    )
    op.drop_table('task_handover_offers')
    # `handover_status` wird nur von dieser Tabelle genutzt -> Drop ist sicher.
    sa.Enum(name='handover_status').drop(op.get_bind(), checkfirst=True)
