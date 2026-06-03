"""ExtraContribution-Modell: freiwillige Sonderleistungen.

Ein Bewohner reicht eine Leistung ein, die in keiner regulären Aufgabe steht
(``status=PENDING``). Der Hauswart vergibt beim Genehmigen Ehrenpunkte, was über
``app.services.contributions.approve_contribution`` ein HONOR-``KarmaEvent``
erzeugt (positiver Score). Status nutzt das bestehende ``ReviewStatus``-Enum.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import ReviewStatus
from app.extensions import db

if TYPE_CHECKING:
    from app.models.user import User


class ExtraContribution(db.Model):
    __tablename__ = "extra_contributions"

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
    description: Mapped[str] = mapped_column(String(2000), nullable=False)
    status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus, name="review_status"),
        nullable=False,
        default=ReviewStatus.PENDING,
    )
    # Beim Genehmigen vergebene Ehrenpunkte (NULL solange PENDING/REJECTED).
    honor_points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    awarded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    awarded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    review_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    user: Mapped["User"] = relationship(foreign_keys=[user_id])
    awarded_by: Mapped["User | None"] = relationship(foreign_keys=[awarded_by_id])
