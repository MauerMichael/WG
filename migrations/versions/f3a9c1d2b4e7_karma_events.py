"""karma events

Revision ID: f3a9c1d2b4e7
Revises: de8394e513fd
Create Date: 2026-05-28 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a9c1d2b4e7'
down_revision: Union[str, Sequence[str], None] = 'de8394e513fd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()

    # Enum-Typ explizit + idempotent anlegen. Die Enum-Spalte wird danach per
    # add_column (mit create_type=False) angehängt — NICHT inline in
    # create_table, weil create_table unter SQLAlchemy 2.0 ein ungeschütztes
    # CREATE TYPE auslöst (ignoriert create_type=False) und mit dem Pre-Create
    # kollidieren würde. Gleiches, bewährtes Muster wie in dienste_review.
    karma_kind_enum = sa.Enum('HONOR', 'PENALTY', name='karma_kind')
    karma_kind_enum.create(bind, checkfirst=True)
    karma_kind_col = sa.Enum(
        'HONOR', 'PENALTY', name='karma_kind', create_type=False
    )

    op.create_table(
        'karma_events',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('points', sa.Integer(), nullable=False),
        sa.Column(
            'occurred_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column('created_by_id', sa.UUID(), nullable=True),
        sa.Column('note', sa.String(length=500), nullable=True),
        sa.Column('occurrence_id', sa.UUID(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['created_by_id'], ['users.id'], ondelete='SET NULL'
        ),
        sa.ForeignKeyConstraint(
            ['occurrence_id'], ['task_occurrences.id'], ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    # Enum-Spalte separat (Tabelle ist neu/leer -> NOT NULL ohne Default ok).
    op.add_column(
        'karma_events', sa.Column('kind', karma_kind_col, nullable=False)
    )
    op.create_index(
        'ix_karma_events_user_id', 'karma_events', ['user_id']
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_karma_events_user_id', table_name='karma_events')
    op.drop_table('karma_events')
    sa.Enum(name='karma_kind').drop(op.get_bind(), checkfirst=True)
