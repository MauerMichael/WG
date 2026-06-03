"""Service für freiwillige Sonderleistungen (``ExtraContribution``).

Reine Funktionen analog ``app.services.scheduling``: jede nimmt die Session als
ersten Parameter. Das Genehmigen delegiert die Karma-Buchung an
``scheduling.award_honor`` — die Punkte-Logik lebt also an einer Stelle.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.domain.enums import ReviewStatus
from app.models.extra import ExtraContribution
from app.services.scheduling import award_honor

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.user import User


def submit_contribution(
    session: "Session", user: "User", description: str
) -> ExtraContribution:
    """Bewohner reicht eine Sonderleistung ein (Status PENDING)."""

    contribution = ExtraContribution(
        user_id=user.id,
        description=description.strip(),
        status=ReviewStatus.PENDING,
    )
    session.add(contribution)
    session.flush()
    return contribution


def approve_contribution(
    session: "Session",
    contribution: ExtraContribution,
    reviewer: "User",
    honor_points: int,
    note: str | None = None,
) -> ExtraContribution:
    """Hauswart genehmigt + vergibt Ehrenpunkte → erzeugt ein HONOR-KarmaEvent.

    Idempotent: ist die Leistung bereits APPROVED, passiert nichts (keine
    doppelten Ehrenpunkte).
    """

    if contribution.status == ReviewStatus.APPROVED:
        return contribution

    points = max(int(honor_points), 1)
    contribution.status = ReviewStatus.APPROVED
    contribution.honor_points = points
    contribution.awarded_by_id = reviewer.id
    contribution.awarded_at = datetime.now(timezone.utc)
    contribution.review_note = note

    award_honor(
        session,
        contribution.user,
        points,
        by_user=reviewer,
        note=note or f"Extra-Leistung: {contribution.description[:60]}",
    )
    session.flush()
    return contribution


def reject_contribution(
    session: "Session",
    contribution: ExtraContribution,
    reviewer: "User",
    note: str | None = None,
) -> ExtraContribution:
    """Hauswart lehnt eine Sonderleistung ab (keine Ehrenpunkte)."""

    contribution.status = ReviewStatus.REJECTED
    contribution.awarded_by_id = reviewer.id
    contribution.awarded_at = datetime.now(timezone.utc)
    contribution.honor_points = None
    contribution.review_note = note
    session.flush()
    return contribution


def pending_contributions(session: "Session") -> list[ExtraContribution]:
    """Alle noch zu prüfenden Sonderleistungen (älteste zuerst)."""

    stmt = (
        select(ExtraContribution)
        .where(ExtraContribution.status == ReviewStatus.PENDING)
        .order_by(ExtraContribution.created_at.asc())
    )
    return list(session.scalars(stmt))


def user_contributions(
    session: "Session", user: "User"
) -> list[ExtraContribution]:
    """Eigene Sonderleistungen eines Bewohners (neueste zuerst)."""

    stmt = (
        select(ExtraContribution)
        .where(ExtraContribution.user_id == user.id)
        .order_by(ExtraContribution.created_at.desc())
    )
    return list(session.scalars(stmt))
