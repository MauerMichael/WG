"""task steps + uhrzeit

Revision ID: e8b1c4d2a7f3
Revises: d4f1a8b2c903
Create Date: 2026-06-25 12:00:00.000000

Hinzu kommen:
- ``task_definitions.default_due_time`` (TIME NULL) — Default-Uhrzeit für
  generierte Occurrences (z.B. „Müll Di 18:00").
- ``task_occurrences.due_time`` (TIME NULL) — Uhrzeit pro Termin (Default aus
  Definition kopiert, kann pro Termin überschrieben werden).
- ``task_steps`` — Schritte einer mehrteiligen Aufgabe (Geschirrspüler
  einräumen + ausräumen). Alle Schritte einer Occurrence gehen an denselben
  Assignee.
- ``task_step_completions`` — Tracking welcher Schritt von welcher Zuweisung
  abgehakt ist.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e8b1c4d2a7f3'
down_revision: Union[str, Sequence[str], None] = 'd4f1a8b2c903'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Optional Uhrzeit auf Definition + Occurrence.
    op.add_column(
        'task_definitions',
        sa.Column('default_due_time', sa.Time(), nullable=True),
    )
    op.add_column(
        'task_occurrences',
        sa.Column('due_time', sa.Time(), nullable=True),
    )

    # Schritte-Tabelle.
    op.create_table(
        'task_steps',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('task_definition_id', sa.UUID(), nullable=False),
        sa.Column('step_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('day_offset', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('time_of_day', sa.Time(), nullable=True),
        sa.ForeignKeyConstraint(
            ['task_definition_id'], ['task_definitions.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'task_definition_id', 'step_order', name='uq_task_step_def_order'
        ),
    )
    op.alter_column('task_steps', 'step_order', server_default=None)
    op.alter_column('task_steps', 'day_offset', server_default=None)

    # Erledigt-Tracking pro (Assignment, Step).
    op.create_table(
        'task_step_completions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('assignment_id', sa.UUID(), nullable=False),
        sa.Column('step_id', sa.UUID(), nullable=False),
        sa.Column(
            'completed_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['assignment_id'], ['task_assignments.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(
            ['step_id'], ['task_steps.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'assignment_id', 'step_id', name='uq_task_step_completion_assignment_step'
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('task_step_completions')
    op.drop_table('task_steps')
    op.drop_column('task_occurrences', 'due_time')
    op.drop_column('task_definitions', 'default_due_time')
