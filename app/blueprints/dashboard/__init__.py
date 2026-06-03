"""Dashboard-Blueprint: Home-Seite mit 'Heute' und 'Diese Woche'.

Zeigt dem eingeloggten + approved User einen Tagesüberblick: heute fällige
Aufgaben, diese Woche fällige Aufgaben, Einkaufs-Top-5, kommende Abwesenheiten,
und einen WG-Score-Footer.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, render_template
from flask_login import current_user, login_required
from sqlalchemy import and_, or_, select

from app.domain.enums import UserStatus
from app.extensions import db
from app.models.absence import Absence
from app.models.shopping import ShoppingItem
from app.models.task import TaskOccurrence
from app.models.user import User
from app.services import handovers, scheduling
from app.services.notifications import (
    assignments_for_this_week,
    assignments_for_today,
    format_de,
    week_bounds,
)

bp = Blueprint("dashboard", __name__, template_folder="../../templates/dashboard")


def _vorname(user: User) -> str:
    name = (user.name or user.email or "").strip()
    if not name:
        return "WG-Bewohner"
    return name.split(" ")[0]


def _household_score_summary(today: date) -> tuple[float | None, float | None]:
    """Liefert ``(my_score, household_average)`` über alle approved User.

    Nutzt ``effective_scores_for`` — EINE Query für alle Bewohner statt N
    Einzel-Queries (Performance: jede Query ist ein Remote-Roundtrip).
    """
    try:
        from app.services.scheduling import effective_scores_for  # type: ignore
    except ImportError:
        return None, None
    if not callable(effective_scores_for):
        return None, None

    users = (
        db.session.execute(
            select(User).where(User.status == UserStatus.APPROVED)
        )
        .scalars()
        .all()
    )
    if not users:
        return None, None

    score_map = effective_scores_for(db.session, list(users))
    scores = [float(s) for s in score_map.values()]
    my_score = score_map.get(current_user.id)

    if not scores:
        return my_score, None
    average = sum(scores) / len(scores)
    return my_score, average


def _upcoming_absences(today: date, days: int = 14) -> list[Absence]:
    until = today + timedelta(days=days)
    stmt = (
        select(Absence)
        .where(
            or_(
                and_(Absence.start_date >= today, Absence.start_date <= until),
                and_(Absence.start_date <= today, Absence.end_date >= today),
            )
        )
        .order_by(Absence.start_date.asc())
    )
    return list(db.session.execute(stmt).scalars().all())


def _top_shopping_items(limit: int = 5) -> list[ShoppingItem]:
    stmt = (
        select(ShoppingItem)
        .where(ShoppingItem.bought_at.is_(None))
        .order_by(ShoppingItem.added_at.desc())
        .limit(limit)
    )
    return list(db.session.execute(stmt).scalars().all())


def _current_duties(today: date) -> list[TaskOccurrence]:
    """Laufende DIENST-Occurrences des eingeloggten Users.

    Dünner Wrapper um ``scheduling.current_duties_for`` — die eigentliche
    Query lebt im Service, damit Dashboard und „Meine"-Aufgaben-Ansicht
    dieselbe Quelle nutzen.
    """
    return scheduling.current_duties_for(db.session, current_user, today)


def _user_stats() -> dict:
    """Persönliche Statistiken (erledigt/Punkte/verpasst/Zuverlässigkeit)."""
    try:
        return scheduling.user_task_stats(db.session, current_user)
    except Exception:  # noqa: BLE001 — Dashboard darf an Stats nie scheitern.
        return {"completed": 0, "points": 0, "missed": 0, "reliability": None}


@bp.route("/")
@login_required
def index() -> str:
    today = date.today()
    monday, sunday = week_bounds(today)

    today_rows = assignments_for_today(db.session, current_user, today=today)
    week_rows = assignments_for_this_week(
        db.session, current_user, today=today, exclude_today=True
    )
    shopping_items = _top_shopping_items(limit=5)
    upcoming = _upcoming_absences(today)
    my_score, avg_score = _household_score_summary(today)
    current_duties = _current_duties(today)
    stats = _user_stats()
    open_offers_count = handovers.open_offer_count(db.session)

    return render_template(
        "dashboard/index.html",
        open_offers_count=open_offers_count,
        vorname=_vorname(current_user),
        today=today,
        today_human=format_de(today),
        monday=monday,
        sunday=sunday,
        today_rows=today_rows,
        week_rows=week_rows,
        shopping_items=shopping_items,
        upcoming_absences=upcoming,
        my_score=my_score,
        avg_score=avg_score,
        current_duties=current_duties,
        stats=stats,
        now=datetime.now(timezone.utc),
    )
