"""Task-Modelle: Definition, Eligibility-M2M, Occurrence, Assignment."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import (
    AssignmentStatus,
    Recurrence,
    ReviewStatus,
    TaskKind,
    TaskStatus,
)
from app.extensions import db

if TYPE_CHECKING:
    from app.models.handover import TaskHandoverOffer
    from app.models.user import User


class TaskDefinition(db.Model):
    __tablename__ = "task_definitions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    kind: Mapped[TaskKind] = mapped_column(
        Enum(TaskKind, name="task_kind"),
        nullable=False,
        default=TaskKind.AUFGABE,
    )
    difficulty_points: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    recurrence: Mapped[Recurrence] = mapped_column(
        Enum(Recurrence, name="recurrence"),
        nullable=False,
        default=Recurrence.NONE,
    )
    # Nur noch für CUSTOM ("Eigenes Intervall in Tagen").
    recurrence_interval_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Wochentag-Anker für WEEKLY/BIWEEKLY (0=Mo … 6=So).
    anchor_weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Tag-im-Monat-Anker für MONTHLY (1–28).
    anchor_day_of_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    required_assignees: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    created_by: Mapped["User | None"] = relationship(
        back_populates="created_task_definitions",
        foreign_keys=[created_by_id],
    )
    eligible_users: Mapped[list["TaskDefinitionEligibleUser"]] = relationship(
        back_populates="task_definition",
        cascade="all, delete-orphan",
    )
    occurrences: Mapped[list["TaskOccurrence"]] = relationship(
        back_populates="task_definition",
        cascade="all, delete-orphan",
    )


class TaskDefinitionEligibleUser(db.Model):
    __tablename__ = "task_definition_eligible_users"
    __table_args__ = (
        UniqueConstraint(
            "task_definition_id",
            "user_id",
            name="uq_task_def_eligible_user",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    task_definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("task_definitions.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    task_definition: Mapped[TaskDefinition] = relationship(back_populates="eligible_users")


class TaskOccurrence(db.Model):
    __tablename__ = "task_occurrences"
    __table_args__ = (
        UniqueConstraint(
            "task_definition_id",
            "period_start",
            name="uq_task_occurrence_def_period",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    task_definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("task_definitions.id", ondelete="CASCADE"),
        nullable=False,
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status"),
        nullable=False,
        default=TaskStatus.OPEN,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    task_definition: Mapped[TaskDefinition] = relationship(back_populates="occurrences")
    assignments: Mapped[list["TaskAssignment"]] = relationship(
        back_populates="occurrence",
        cascade="all, delete-orphan",
    )


class TaskAssignment(db.Model):
    __tablename__ = "task_assignments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    occurrence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("task_occurrences.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[AssignmentStatus] = mapped_column(
        Enum(AssignmentStatus, name="assignment_status"),
        nullable=False,
        default=AssignmentStatus.OPEN,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    points_earned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    assigned_during_absence: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    review_status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus, name="review_status"),
        nullable=False,
        default=ReviewStatus.PENDING,
    )
    reviewed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    occurrence: Mapped[TaskOccurrence] = relationship(back_populates="assignments")
    user: Mapped["User"] = relationship(
        back_populates="assignments",
        foreign_keys=[user_id],
    )
    reviewed_by: Mapped["User | None"] = relationship(foreign_keys=[reviewed_by_id])
    # Abgabe-Angebote auf der Börse (i.d.R. 0–1 offene; Historie bleibt erhalten).
    # Kein passive_deletes: der ORM löscht die Angebote beim Entfernen der
    # Zuweisung selbst (Abwesenheits-Neuverteilung ruft session.delete) — SQLite
    # erzwingt die DB-FK-Cascade nicht. Die FK-ondelete=CASCADE bleibt für
    # DB-seitige Deletes (z. B. User-Entfernung) als Absicherung.
    handover_offers: Mapped[list["TaskHandoverOffer"]] = relationship(
        back_populates="assignment",
        cascade="all, delete-orphan",
    )
