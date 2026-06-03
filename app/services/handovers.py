"""Service für die Aufgaben-Börse (``TaskHandoverOffer``).

Reine Funktionen analog ``app.services.contributions`` / ``scheduling``: jede
nimmt die Session als ersten Parameter.

* ``offer_assignment`` — „Abgeben": erzeugt ein OPEN-Angebot, **ohne** die
  Zuweisung anzufassen (der Anbieter bleibt „Hauptmann" und trägt weiterhin das
  Penalty-Risiko).
* ``claim_offer`` — „Übernehmen": transferiert die Zuweisung auf den Übernehmer
  und schließt das Angebot (CLAIMED).
* ``cancel_offer`` — „Zurückziehen" durch den Anbieter (CANCELLED).
* ``close_open_offer_for`` — schließt ein offenes Angebot automatisch, wenn der
  Anbieter die Aufgabe doch selbst erledigt.

Guard-Verletzungen werfen ``HandoverError`` (deutsche Meldung) — die Routes
fangen sie und zeigen einen Flash. AuditLog-Einträge entstehen hier im Service,
da es mehrere Aufrufer gibt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import joinedload, selectinload

from app.domain.enums import AssignmentStatus, HandoverStatus, TaskStatus
from app.models.audit import AuditLog
from app.models.handover import TaskHandoverOffer
from app.models.task import TaskAssignment, TaskOccurrence
from app.services.scheduling import _utcnow

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.user import User


class HandoverError(Exception):
    """Guard-Verletzung beim Abgeben / Übernehmen / Zurückziehen."""


def _audit(
    session: "Session",
    by_user: "User | None",
    action: str,
    offer: TaskHandoverOffer,
    extra: dict | None = None,
) -> None:
    payload = {
        "offer_id": str(offer.id),
        "assignment_id": str(offer.assignment_id),
    }
    if extra:
        payload.update(extra)
    session.add(
        AuditLog(
            user_id=by_user.id if by_user else None,
            action=action,
            entity_type="task_handover_offer",
            entity_id=offer.id,
            payload=payload,
        )
    )


def _open_offer_for(
    session: "Session", assignment: TaskAssignment
) -> TaskHandoverOffer | None:
    """Das aktuell offene Angebot für ein Assignment (oder ``None``)."""

    return session.scalars(
        select(TaskHandoverOffer).where(
            TaskHandoverOffer.assignment_id == assignment.id,
            TaskHandoverOffer.status == HandoverStatus.OPEN,
        )
    ).first()


def offer_assignment(
    session: "Session",
    assignment: TaskAssignment,
    by_user: "User",
    note: str | None = None,
) -> TaskHandoverOffer:
    """Bewohner gibt eine eigene offene Zuweisung an die Börse ab.

    Die Zuweisung bleibt unverändert — der Anbieter bleibt zuständig, bis jemand
    übernimmt.
    """

    if assignment.user_id != by_user.id:
        raise HandoverError("Das ist nicht deine Aufgabe.")
    if assignment.status != AssignmentStatus.OPEN:
        raise HandoverError("Nur offene Aufgaben können abgegeben werden.")
    if assignment.occurrence.status != TaskStatus.OPEN:
        raise HandoverError("Diese Aufgabe ist nicht mehr offen.")
    if _open_offer_for(session, assignment) is not None:
        raise HandoverError("Diese Aufgabe ist bereits abgegeben.")

    clean = (note or "").strip()
    offer = TaskHandoverOffer(
        assignment_id=assignment.id,
        offered_by_id=by_user.id,
        status=HandoverStatus.OPEN,
        note=clean[:500] or None,
    )
    session.add(offer)
    session.flush()
    _audit(session, by_user, "handover.offer", offer)
    return offer


def cancel_offer(
    session: "Session", offer: TaskHandoverOffer, by_user: "User"
) -> TaskHandoverOffer:
    """Anbieter zieht ein offenes Angebot zurück."""

    if offer.status != HandoverStatus.OPEN:
        raise HandoverError("Diese Abgabe ist nicht mehr offen.")
    if offer.offered_by_id != by_user.id:
        raise HandoverError("Nur der Anbieter kann die Abgabe zurückziehen.")

    offer.status = HandoverStatus.CANCELLED
    session.flush()
    _audit(session, by_user, "handover.cancel", offer)
    return offer


def claim_offer(
    session: "Session", offer: TaskHandoverOffer, by_user: "User"
) -> TaskHandoverOffer:
    """Ein anderer Bewohner übernimmt das Angebot → Zuweisung wandert über."""

    # Race-Re-Check zuerst: die Route lädt das Angebot frisch (ggf. mit
    # FOR UPDATE), hier prüfen wir den Status erneut.
    if offer.status != HandoverStatus.OPEN:
        raise HandoverError("Diese Aufgabe wurde bereits übernommen.")
    if offer.offered_by_id == by_user.id:
        raise HandoverError("Du kannst deine eigene Abgabe nicht übernehmen.")

    assignment = offer.assignment
    if assignment.status != AssignmentStatus.OPEN:
        raise HandoverError("Diese Aufgabe ist nicht mehr offen.")
    if assignment.occurrence.status != TaskStatus.OPEN:
        raise HandoverError("Diese Aufgabe ist nicht mehr offen.")
    if any(a.user_id == by_user.id for a in assignment.occurrence.assignments):
        raise HandoverError("Du bist hier schon eingeteilt.")

    now = _utcnow()
    # Zuweisung transferieren — dieselbe Zeile wechselt den Besitzer.
    assignment.user_id = by_user.id
    assignment.assigned_during_absence = False
    by_user.last_assigned_at = now

    offer.status = HandoverStatus.CLAIMED
    offer.claimed_by_id = by_user.id
    offer.claimed_at = now
    session.flush()
    _audit(
        session,
        by_user,
        "handover.claim",
        offer,
        {"offered_by": str(offer.offered_by_id), "claimed_by": str(by_user.id)},
    )
    return offer


def close_open_offer_for(
    session: "Session", assignment: TaskAssignment, reason: str
) -> int:
    """Schließt das offene Angebot eines Assignments (CANCELLED).

    Aufgerufen, wenn der Anbieter die Aufgabe doch selbst erledigt — die offene
    Abgabe soll dann automatisch verschwinden. Gibt 1 zurück, wenn ein Angebot
    geschlossen wurde, sonst 0.
    """

    offer = _open_offer_for(session, assignment)
    if offer is None:
        return 0
    offer.status = HandoverStatus.CANCELLED
    session.flush()
    _audit(
        session,
        assignment.user,
        "handover.auto_close",
        offer,
        {"reason": reason},
    )
    return 1


def _board_filter(stmt):
    """Gemeinsamer Board-Filter: nur valide, aktuell sichtbare Angebote.

    OPEN-Angebot **und** Assignment noch OPEN **und** Occurrence noch OPEN — so
    verschwinden erledigte/überfällige Abgaben automatisch vom Brett, ohne dass
    ``mark_done`` / ``apply_overdue_penalties`` jede Zeile aktiv schließen müssen.
    """

    return (
        stmt.join(
            TaskAssignment, TaskAssignment.id == TaskHandoverOffer.assignment_id
        )
        .join(TaskOccurrence, TaskOccurrence.id == TaskAssignment.occurrence_id)
        .where(
            TaskHandoverOffer.status == HandoverStatus.OPEN,
            TaskAssignment.status == AssignmentStatus.OPEN,
            TaskOccurrence.status == TaskStatus.OPEN,
        )
    )


def open_offers(session: "Session") -> list[TaskHandoverOffer]:
    """Alle aktuell offenen Abgaben für die Börse (älteste Fälligkeit zuerst)."""

    stmt = (
        _board_filter(select(TaskHandoverOffer))
        .options(
            joinedload(TaskHandoverOffer.offered_by),
            selectinload(TaskHandoverOffer.assignment).options(
                joinedload(TaskAssignment.occurrence).options(
                    joinedload(TaskOccurrence.task_definition),
                    selectinload(TaskOccurrence.assignments).joinedload(
                        TaskAssignment.user
                    ),
                ),
            ),
        )
        .order_by(
            TaskOccurrence.due_date.asc(), TaskHandoverOffer.created_at.asc()
        )
    )
    return list(session.scalars(stmt).unique())


def open_offer_count(session: "Session") -> int:
    """Anzahl aktuell offener Abgaben (für Nav-/Dashboard-Hinweis)."""

    stmt = _board_filter(select(func.count(TaskHandoverOffer.id)))
    return int(session.scalar(stmt) or 0)
