"""TaskHandoverOffer-Modell: Aufgaben-Börse („Abgeben → Übernehmen").

Ein Bewohner gibt eine eigene offene ``TaskAssignment`` ab; daraus entsteht ein
OPEN-Angebot, das auf der Börse (``/tasks/boerse``) für alle sichtbar ist. Bis
jemand übernimmt, bleibt der Anbieter „Hauptmann": seine Zuweisung (inkl.
Penalty-Risiko) bleibt unangetastet. Beim Übernehmen wandert die Zuweisung auf
den Übernehmer; das Angebot wird CLAIMED. Der Anbieter kann ein offenes Angebot
zurückziehen (CANCELLED).

Die Service-Logik lebt in ``app.services.handovers`` — analog zu
``app.services.contributions`` für die Extra-Leistungen.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import HandoverStatus
from app.extensions import db

if TYPE_CHECKING:
    from app.models.task import TaskAssignment
    from app.models.user import User


class TaskHandoverOffer(db.Model):
    __tablename__ = "task_handover_offers"
    __table_args__ = (
        # Höchstens EIN offenes Angebot pro Assignment. Partielles Unique:
        # CLAIMED/CANCELLED-Zeilen bleiben erlaubt (ein Assignment darf nach
        # Übernahme/Rückzug erneut angeboten werden). Postgres und SQLite
        # unterstützen partielle Indizes; der Service-Guard in
        # ``offer_assignment`` ist die primäre Absicherung, dieser Index die
        # Defense-in-Depth gegen parallele Requests.
        Index(
            "uq_handover_one_open_per_assignment",
            "assignment_id",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
            sqlite_where=text("status = 'OPEN'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    assignment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("task_assignments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    offered_by_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[HandoverStatus] = mapped_column(
        Enum(HandoverStatus, name="handover_status"),
        nullable=False,
        default=HandoverStatus.OPEN,
    )
    # Optionaler Grund („bin im Stress"), Länge wie review_note.
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    claimed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    assignment: Mapped["TaskAssignment"] = relationship(
        back_populates="handover_offers",
        foreign_keys=[assignment_id],
    )
    offered_by: Mapped["User"] = relationship(foreign_keys=[offered_by_id])
    claimed_by: Mapped["User | None"] = relationship(foreign_keys=[claimed_by_id])
