"""dienste review

Revision ID: de8394e513fd
Revises: bc9bed354278
Create Date: 2026-05-28 16:33:44.835358

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'de8394e513fd'
down_revision: Union[str, Sequence[str], None] = 'bc9bed354278'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # NOT-NULL-Spalten brauchen einen server_default, damit bestehende Zeilen
    # (Remote-DB hat reale Daten) beim ADD COLUMN einen gültigen Wert
    # bekommen. Den server_default droppen wir danach wieder — die Python-
    # seitigen Modell-Defaults (TaskKind.AUFGABE / ReviewStatus.PENDING)
    # bleiben die alleinige Quelle für neue Zeilen.
    bind = op.get_bind()

    # PG-Enum-Typen explizit anlegen (pg8000/Alembic legt sie bei add_column
    # mit server_default NICHT zuverlässig implizit an). create_type=False auf
    # den Spalten verhindert danach ein zweites CREATE TYPE.
    review_status_enum = sa.Enum(
        'PENDING', 'APPROVED', 'REJECTED', name='review_status'
    )
    task_kind_enum = sa.Enum('AUFGABE', 'DIENST', name='task_kind')
    review_status_enum.create(bind, checkfirst=True)
    task_kind_enum.create(bind, checkfirst=True)

    review_status_col = sa.Enum(
        'PENDING', 'APPROVED', 'REJECTED', name='review_status', create_type=False
    )
    task_kind_col = sa.Enum(
        'AUFGABE', 'DIENST', name='task_kind', create_type=False
    )

    op.add_column(
        'task_assignments',
        sa.Column(
            'review_status',
            review_status_col,
            nullable=False,
            server_default='PENDING',
        ),
    )
    op.add_column('task_assignments', sa.Column('reviewed_by_id', sa.UUID(), nullable=True))
    op.add_column('task_assignments', sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('task_assignments', sa.Column('review_note', sa.String(length=500), nullable=True))
    op.create_foreign_key(
        'fk_task_assignments_reviewed_by_id_users',
        'task_assignments',
        'users',
        ['reviewed_by_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.add_column(
        'task_definitions',
        sa.Column(
            'kind',
            task_kind_col,
            nullable=False,
            server_default='AUFGABE',
        ),
    )

    # server_default wieder entfernen: Werte für künftige Zeilen liefert das
    # ORM (mapped_column default=...), nicht die DB.
    op.alter_column('task_assignments', 'review_status', server_default=None)
    op.alter_column('task_definitions', 'kind', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('task_definitions', 'kind')
    op.drop_constraint(
        'fk_task_assignments_reviewed_by_id_users',
        'task_assignments',
        type_='foreignkey',
    )
    op.drop_column('task_assignments', 'review_note')
    op.drop_column('task_assignments', 'reviewed_at')
    op.drop_column('task_assignments', 'reviewed_by_id')
    op.drop_column('task_assignments', 'review_status')
    # Enum-Typen entfernen (PG legt sie bei add_column implizit an).
    sa.Enum(name='task_kind').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='review_status').drop(op.get_bind(), checkfirst=True)
