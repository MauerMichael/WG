"""KarmaEvent-Modell: Ehrenpunkte (HONOR) und Strafen (PENALTY).

Ein Ledger separat von ``TaskAssignment.points_earned``. Beide Arten speichern
``points`` als positive Magnitude; das Vorzeichen steckt im ``kind``. Der
Fairness-Score (``app.services.scheduling.effective_scores_for``) summiert
HONOR positiv (80-Tage-Fenster) und PENALTY negativ (40-Tage-Fenster) auf die
Task-Punkte.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import KarmaKind
from app.extensions import db

if TYPE_CHECKING:
    from app.models.user import User


class KarmaEvent(db.Model):
    __tablename__ = "karma_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[KarmaKind] = mapped_column(
        Enum(KarmaKind, name="karma_kind"),
        nullable=False,
    )
    # Positive Magnitude — das Vorzeichen ergibt sich aus `kind`.
    points: Mapped[int] = mapped_column(Integer, nullable=False)
    # Entstehungszeitpunkt — Basis für das Decay-Fenster (HONOR 80d / PENALTY 40d).
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    occurrence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("task_occurrences.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    user: Mapped["User"] = relationship(foreign_keys=[user_id])
    created_by: Mapped["User | None"] = relationship(foreign_keys=[created_by_id])
