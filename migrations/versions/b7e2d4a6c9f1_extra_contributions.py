"""extra contributions

Revision ID: b7e2d4a6c9f1
Revises: f3a9c1d2b4e7
Create Date: 2026-05-28 18:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e2d4a6c9f1'
down_revision: Union[str, Sequence[str], None] = 'f3a9c1d2b4e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()

    # `review_status` existiert bereits (aus dienste_review) -> checkfirst macht
    # das hier zum No-op. Die Enum-Spalte wird per add_column (create_type=False)
    # angehängt statt inline in create_table, sonst würde create_table unter
    # SQLAlchemy 2.0 ein erneutes CREATE TYPE auf den bestehenden Typ versuchen.
    review_status_enum = sa.Enum(
        'PENDING', 'APPROVED', 'REJECTED', name='review_status'
    )
    review_status_enum.create(bind, checkfirst=True)
    review_status_col = sa.Enum(
        'PENDING', 'APPROVED', 'REJECTED', name='review_status', create_type=False
    )

    op.create_table(
        'extra_contributions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('description', sa.String(length=2000), nullable=False),
        sa.Column('honor_points', sa.Integer(), nullable=True),
        sa.Column('awarded_by_id', sa.UUID(), nullable=True),
        sa.Column('awarded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('review_note', sa.String(length=500), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['awarded_by_id'], ['users.id'], ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    # NOT-NULL-Enum-Spalte mit kurzlebigem server_default (Tabelle leer); danach
    # wieder entfernen, damit der ORM-Default die alleinige Quelle bleibt.
    op.add_column(
        'extra_contributions',
        sa.Column(
            'status', review_status_col, nullable=False, server_default='PENDING'
        ),
    )
    op.alter_column('extra_contributions', 'status', server_default=None)
    op.create_index(
        'ix_extra_contributions_user_id', 'extra_contributions', ['user_id']
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        'ix_extra_contributions_user_id', table_name='extra_contributions'
    )
    op.drop_table('extra_contributions')
    # `review_status` NICHT droppen — task_assignments nutzt den Typ weiter.
