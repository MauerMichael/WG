"""Routen für den Absences-Blueprint."""

from __future__ import annotations

import logging
import uuid
from datetime import date, timedelta

from flask import (
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import select

from app.blueprints.absences import bp
from app.blueprints.auth import user_has_any_role
from app.domain.enums import Role, UserStatus
from app.extensions import db
from app.models.absence import Absence
from app.models.user import User

logger = logging.getLogger(__name__)

# Reassignment-Hook aus dem Tasks-Agent. Wenn dieser noch nicht da ist, fangen
# wir den ImportError ab, damit der App-Boot nicht crasht.
try:
    from app.services.scheduling import reassign_open_overlap  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - hängt von paralleler Welle-2-Arbeit ab.
    reassign_open_overlap = None
    logger.warning(
        "app.services.scheduling.reassign_open_overlap nicht verfügbar — "
        "Abwesenheiten werden gespeichert, aber kein Reassignment ausgelöst.",
    )


# Kurze Wochentags-Labels für die 7-Spalten-Wochenansicht (Mo=0 … So=6).
_WEEKDAY_SHORT = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _can_modify(absence: Absence) -> bool:
    """True, wenn der eingeloggte User die Abwesenheit ändern darf."""
    if not current_user.is_authenticated:
        return False
    if absence.user_id == current_user.id:
        return True
    return user_has_any_role(current_user, Role.HAUSWART, Role.ADMIN)


def _can_assign_other_user() -> bool:
    """Nur Hauswart/Admin dürfen Abwesenheiten für andere User eintragen."""
    return user_has_any_role(current_user, Role.HAUSWART, Role.ADMIN)


def _can_toggle_user(user_id: uuid.UUID) -> bool:
    """Eigene Zellen immer; fremde nur Hauswart/Admin."""
    if not current_user.is_authenticated:
        return False
    if user_id == current_user.id:
        return True
    return user_has_any_role(current_user, Role.HAUSWART, Role.ADMIN)


def _approved_users() -> list[User]:
    stmt = (
        select(User)
        .where(User.status == UserStatus.APPROVED)
        .order_by(User.name.asc())
    )
    return list(db.session.scalars(stmt).all())


def _absences_overlapping(month_start: date, month_end: date) -> list[Absence]:
    """EINE Query: alle Absences, die den Monat überlappen."""
    stmt = select(Absence).where(
        Absence.start_date <= month_end,
        Absence.end_date >= month_start,
    )
    return list(db.session.scalars(stmt).all())


def _absent_dates_by_user(
    month_start: date, month_end: date
) -> dict[uuid.UUID, set[date]]:
    """Pro User ein Set abwesender Tage, geschnitten mit dem Monat."""
    result: dict[uuid.UUID, set[date]] = {}
    for absence in _absences_overlapping(month_start, month_end):
        days = result.setdefault(absence.user_id, set())
        day = max(absence.start_date, month_start)
        last = min(absence.end_date, month_end)
        while day <= last:
            days.add(day)
            day += timedelta(days=1)
    return result


def _is_absent_on(user_id: uuid.UUID, day: date) -> bool:
    """True, wenn ``day`` von einer Absence dieses Users gedeckt ist."""
    stmt = select(Absence.id).where(
        Absence.user_id == user_id,
        Absence.start_date <= day,
        Absence.end_date >= day,
    )
    return db.session.scalars(stmt).first() is not None


# ---------------------------------------------------------------------------
# Routen
# ---------------------------------------------------------------------------


@bp.route("/", methods=["GET"])
@login_required
def index():
    today = date.today()
    # Wochen-Fenster (Mo–So) aus ?week=YYYY-MM-DD (beliebiger Tag der Woche).
    # 7 Spalten passen auf jedes Handy ohne horizontales Scrollen.
    anchor = _parse_date((request.args.get("week") or "").strip()) or today
    monday = anchor - timedelta(days=anchor.weekday())
    sunday = monday + timedelta(days=6)

    users = _approved_users()
    absent_map = _absent_dates_by_user(monday, sunday)

    # Tage-Liste (genau 7) mit Metadaten für den Header.
    days: list[dict] = []
    for offset in range(7):
        day = monday + timedelta(days=offset)
        days.append(
            {
                "date": day,
                "day": day.day,
                "weekday": _WEEKDAY_SHORT[day.weekday()],
                "is_weekend": day.weekday() >= 5,
                "is_today": day == today,
            }
        )

    # Zeilen: pro User die Tage mit Absent-Flag + Toggle-Berechtigung.
    rows: list[dict] = []
    for u in users:
        absent_days = absent_map.get(u.id, set())
        can_toggle = _can_toggle_user(u.id)
        cells = [
            {
                "user_id": u.id,
                "date": meta["date"],
                "is_absent": meta["date"] in absent_days,
                "is_today": meta["is_today"],
                "is_weekend": meta["is_weekend"],
                "can_toggle": can_toggle,
            }
            for meta in days
        ]
        rows.append({"user": u, "cells": cells})

    iso = monday.isocalendar()
    return render_template(
        "absences/index.html",
        today=today,
        week_label=(
            f"KW {iso.week} · {monday.strftime('%d.%m.')}–{sunday.strftime('%d.%m.%Y')}"
        ),
        days=days,
        rows=rows,
        prev_week=(monday - timedelta(days=7)).isoformat(),
        next_week=(monday + timedelta(days=7)).isoformat(),
        this_week=today.isoformat(),
    )


@bp.route("/new", methods=["GET"])
@login_required
def new():
    today = date.today()
    users = _approved_users()
    can_assign_other = _can_assign_other_user()
    return render_template(
        "absences/new.html",
        users=users,
        can_assign_other=can_assign_other,
        default_user_id=current_user.id,
        today=today,
    )


@bp.route("/", methods=["POST"])
@login_required
def create():
    start = _parse_date(request.form.get("start_date"))
    end_raw = request.form.get("end_date") or None
    end = _parse_date(end_raw) if end_raw else start
    reason = (request.form.get("reason") or "").strip() or None

    # Wer ist Subject? Default = current_user; Admin/Hauswart darf wählen.
    target_user_id = current_user.id
    if _can_assign_other_user():
        raw = (request.form.get("user_id") or "").strip()
        if raw:
            try:
                target_user_id = uuid.UUID(raw)
            except ValueError:
                target_user_id = current_user.id

    target_user = db.session.get(User, target_user_id)
    if target_user is None or target_user.status != UserStatus.APPROVED:
        abort(400, description="Ungültiger Nutzer.")

    if start is None or end is None:
        abort(400, description="Start- und Enddatum sind erforderlich.")
    if end < start:
        abort(400, description="Enddatum darf nicht vor Startdatum liegen.")

    absence = Absence(
        user_id=target_user.id,
        start_date=start,
        end_date=end,
        reason=reason,
    )
    db.session.add(absence)
    db.session.commit()

    # AFTER commit: Reassignment.
    reassigned = 0
    if reassign_open_overlap is not None:
        try:
            result = reassign_open_overlap(
                db.session, target_user, start, end, skip_dienst=True
            )
            # Versuche, eine sinnvolle Zahl zu extrahieren — der Tasks-Agent
            # darf entweder int oder list/sequence zurückgeben.
            if isinstance(result, int):
                reassigned = result
            elif result is not None:
                try:
                    reassigned = len(result)
                except TypeError:
                    reassigned = 0
            db.session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("reassign_open_overlap fehlgeschlagen")
            db.session.rollback()

    flash(
        f"Abwesenheit eingetragen. {reassigned} Aufgaben wurden neu verteilt.",
        "success",
    )

    return redirect(url_for("absences.index"))


@bp.route("/toggle", methods=["POST"])
@login_required
def toggle():
    """Toggelt einen einzelnen Tag (anwesend ↔ abwesend) für einen User.

    HTMX: gibt das aktualisierte Zell-Partial zurück (outerHTML-Swap).
    Non-HTMX: Redirect zurück aufs Grid des betroffenen Monats.
    """
    raw_user = (request.form.get("user_id") or request.args.get("user_id") or "").strip()
    raw_date = (request.form.get("date") or request.args.get("date") or "").strip()

    try:
        target_user_id = uuid.UUID(raw_user)
    except ValueError:
        abort(400, description="Ungültiger Nutzer.")

    day = _parse_date(raw_date)
    if day is None:
        abort(400, description="Ungültiges Datum.")

    if not _can_toggle_user(target_user_id):
        abort(403)

    target_user = db.session.get(User, target_user_id)
    if target_user is None or target_user.status != UserStatus.APPROVED:
        abort(400, description="Ungültiger Nutzer.")

    # Alle Absences dieses Users, die ``day`` decken.
    covering = list(
        db.session.scalars(
            select(Absence).where(
                Absence.user_id == target_user_id,
                Absence.start_date <= day,
                Absence.end_date >= day,
            )
        )
    )

    if not covering:
        # present → absent: 1-Tages-Absence anlegen.
        db.session.add(
            Absence(
                user_id=target_user_id,
                start_date=day,
                end_date=day,
                reason=None,
            )
        )
    else:
        # absent → present: ``day`` aus jeder deckenden Absence herausstanzen.
        for absence in covering:
            if absence.start_date == day and absence.end_date == day:
                # Einzeltag → löschen.
                db.session.delete(absence)
            elif absence.start_date == day:
                # Kante vorn → Start um einen Tag nach hinten.
                absence.start_date = day + timedelta(days=1)
            elif absence.end_date == day:
                # Kante hinten → Ende um einen Tag nach vorn.
                absence.end_date = day - timedelta(days=1)
            else:
                # Mitte → in zwei Absences splitten:
                #   alt:  start .. day-1
                #   neu:  day+1 .. end (Grund kopieren)
                original_end = absence.end_date
                absence.end_date = day - timedelta(days=1)
                db.session.add(
                    Absence(
                        user_id=target_user_id,
                        start_date=day + timedelta(days=1),
                        end_date=original_end,
                        reason=absence.reason,
                    )
                )

    db.session.commit()

    # Reassignment für genau diesen Tag (mirror von `create`).
    if reassign_open_overlap is not None:
        try:
            reassign_open_overlap(db.session, target_user, day, day, skip_dienst=True)
            db.session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("reassign_open_overlap fehlgeschlagen")
            db.session.rollback()

    is_absent = _is_absent_on(target_user_id, day)

    if request.headers.get("HX-Request") == "true":
        cell = {
            "user_id": target_user_id,
            "date": day,
            "is_absent": is_absent,
            "is_today": day == date.today(),
            "is_weekend": day.weekday() >= 5,
            "can_toggle": _can_toggle_user(target_user_id),
        }
        return render_template("absences/_cell.html", cell=cell)

    return redirect(url_for("absences.index", week=day.isoformat()))


@bp.route("/<uuid:absence_id>/delete", methods=["POST"])
@login_required
def delete(absence_id: uuid.UUID):
    absence = db.session.get(Absence, absence_id)
    if absence is None:
        abort(404)
    if not _can_modify(absence):
        abort(403)

    db.session.delete(absence)
    db.session.commit()
    flash("Abwesenheit gelöscht.", "info")
    return redirect(url_for("absences.index"))
