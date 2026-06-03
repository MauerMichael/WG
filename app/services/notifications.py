"""E-Mail-Komposition und SMTP-Versand für den täglichen WG-Digest.

Reine Funktionen, die mit einer übergebenen SQLAlchemy-Session arbeiten und
für SMTP-Settings auf ``current_app.config`` zurückgreifen. Keine globalen
Side-Effects beim Import.
"""

from __future__ import annotations

import logging
import re
import smtplib
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from typing import TYPE_CHECKING

from flask import current_app, url_for
from sqlalchemy import select

from app.domain.enums import AssignmentStatus, TaskStatus, UserStatus
from app.models.task import TaskAssignment, TaskDefinition, TaskOccurrence
from app.models.user import User

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Datumshelfer (deutsch)
# ---------------------------------------------------------------------------

_WEEKDAYS_DE = [
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
]

_MONTHS_DE = [
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]


def format_de(d: date) -> str:
    """Formatiert ein Datum als ``Mittwoch, 27. Mai 2026``."""
    return f"{_WEEKDAYS_DE[d.weekday()]}, {d.day}. {_MONTHS_DE[d.month - 1]} {d.year}"


def format_de_short(d: date) -> str:
    """Kurzformat ``27. Mai 2026`` ohne Wochentag."""
    return f"{d.day}. {_MONTHS_DE[d.month - 1]} {d.year}"


def week_bounds(today: date) -> tuple[date, date]:
    """Gibt (Montag, Sonntag) der Woche zurück, in der ``today`` liegt."""
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


# ---------------------------------------------------------------------------
# Abfragen
# ---------------------------------------------------------------------------


def _open_assignments_in_range(
    session: "Session",
    user: User,
    range_start: date,
    range_end: date,
) -> list[tuple[TaskAssignment, TaskOccurrence, TaskDefinition]]:
    """Lädt offene Assignments des Users, deren Occurrence-Periode den Bereich
    ``[range_start, range_end]`` (inklusiv) überlappt.

    Liefert Tuples ``(assignment, occurrence, definition)`` sortiert nach
    ``period_start`` und Titel — vermeidet damit lazy-load Roundtrips beim
    Templaterendern.
    """
    stmt = (
        select(TaskAssignment, TaskOccurrence, TaskDefinition)
        .join(TaskOccurrence, TaskAssignment.occurrence_id == TaskOccurrence.id)
        .join(TaskDefinition, TaskOccurrence.task_definition_id == TaskDefinition.id)
        .where(
            TaskAssignment.user_id == user.id,
            TaskAssignment.status == AssignmentStatus.OPEN,
            TaskOccurrence.status == TaskStatus.OPEN,
            TaskOccurrence.period_start <= range_end,
            TaskOccurrence.period_end >= range_start,
        )
        .order_by(TaskOccurrence.period_start.asc(), TaskDefinition.title.asc())
    )
    return list(session.execute(stmt).all())


def assignments_for_today(
    session: "Session",
    user: User,
    today: date | None = None,
) -> list[tuple[TaskAssignment, TaskOccurrence, TaskDefinition]]:
    today = today or date.today()
    return _open_assignments_in_range(session, user, today, today)


def assignments_for_this_week(
    session: "Session",
    user: User,
    today: date | None = None,
    *,
    exclude_today: bool = True,
) -> list[tuple[TaskAssignment, TaskOccurrence, TaskDefinition]]:
    today = today or date.today()
    _, sunday = week_bounds(today)
    start = today + timedelta(days=1) if exclude_today else today
    if start > sunday:
        return []
    return _open_assignments_in_range(session, user, start, sunday)


# ---------------------------------------------------------------------------
# Komposition
# ---------------------------------------------------------------------------


def _render_html_digest(
    user: User,
    today: date,
    today_rows: list[tuple[TaskAssignment, TaskOccurrence, TaskDefinition]],
    week_rows: list[tuple[TaskAssignment, TaskOccurrence, TaskDefinition]],
    dashboard_url: str | None,
) -> str:
    """Erzeugt einen schlichten, inline-stylten HTML-Body — kein Tailwind."""
    vorname = (user.name or user.email or "WG-Bewohner").split(" ")[0]

    def _li(rows: list[tuple[TaskAssignment, TaskOccurrence, TaskDefinition]]) -> str:
        if not rows:
            return (
                '<p style="margin:8px 0;color:#475569;">Nichts zu tun. Genieß den Tag.</p>'
            )
        items = []
        for _assignment, occurrence, definition in rows:
            points = definition.difficulty_points
            due = format_de_short(occurrence.due_date)
            items.append(
                "<li style=\"margin:6px 0;\">"
                f"<strong>{_escape(definition.title)}</strong> "
                f"<span style=\"color:#64748b;\">— {points} Pkt · fällig {due}</span>"
                "</li>"
            )
        return (
            '<ul style="margin:8px 0 16px;padding-left:20px;color:#0f172a;">'
            + "".join(items)
            + "</ul>"
        )

    link_html = (
        f'<p style="margin:16px 0;"><a href="{_escape(dashboard_url)}" '
        'style="color:#2563eb;text-decoration:underline;">Zum Dashboard</a></p>'
        if dashboard_url
        else ""
    )

    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif;'
        'max-width:560px;margin:0 auto;padding:24px;color:#0f172a;">'
        f'<h1 style="font-size:20px;margin:0 0 4px;">Hallo {_escape(vorname)},</h1>'
        f'<p style="margin:0 0 16px;color:#475569;">deine WG-Aufgaben für '
        f"{_escape(format_de(today))}.</p>"
        '<h2 style="font-size:16px;margin:16px 0 4px;">Heute</h2>'
        f"{_li(today_rows)}"
        '<h2 style="font-size:16px;margin:16px 0 4px;">Diese Woche</h2>'
        f"{_li(week_rows)}"
        f"{link_html}"
        '<p style="margin:24px 0 0;font-size:12px;color:#94a3b8;">'
        "WG-Organisation · Tagesdigest</p>"
        "</div>"
    )


def _escape(value: str | None) -> str:
    if value is None:
        return ""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _html_to_text(html: str) -> str:
    """Naiver HTML→Text-Fallback für die ``text/plain``-Alt-Variante."""
    text = re.sub(r"<\s*br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</\s*(p|li|h1|h2|h3|div|ul)\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    # Zusammenklappen von Mehrfach-Leerzeilen.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def compose_daily_digest(
    session: "Session",
    user: User,
    today: date | None = None,
) -> tuple[str, str] | None:
    """Baut Subject + HTML-Body für den User. ``None`` wenn nichts zu melden."""
    today = today or date.today()
    today_rows = assignments_for_today(session, user, today=today)
    week_rows = assignments_for_this_week(session, user, today=today, exclude_today=True)

    if not today_rows and not week_rows:
        return None

    count_today = len(today_rows)
    subject = f"WG-Aufgaben für heute ({count_today} offen)"

    dashboard_url: str | None
    try:
        dashboard_url = url_for("dashboard.index", _external=True)
    except RuntimeError:
        # Kein Request-Context — wir laufen wahrscheinlich im Cronscript ohne
        # SERVER_NAME-Setup. In dem Fall verzichten wir auf den Link.
        dashboard_url = None

    html_body = _render_html_digest(user, today, today_rows, week_rows, dashboard_url)
    return subject, html_body


# ---------------------------------------------------------------------------
# Versand
# ---------------------------------------------------------------------------


def send_email(to: str, subject: str, html_body: str) -> None:
    """Versendet eine HTML-E-Mail via stdlib ``smtplib``.

    Liest SMTP-Settings aus ``current_app.config``. Wirft ``RuntimeError``
    bei fehlender Konfiguration und propagiert SMTP-Fehler unverändert.
    """
    config = current_app.config
    host = config.get("SMTP_HOST")
    if not host:
        raise RuntimeError("SMTP_HOST not configured")
    port = int(config.get("SMTP_PORT") or 587)
    username = config.get("SMTP_USER")
    password = config.get("SMTP_PASSWORD")
    sender = config.get("SMTP_FROM") or username
    if not sender:
        raise RuntimeError("SMTP_FROM (or SMTP_USER) not configured")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(_html_to_text(html_body))
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.ehlo()
        try:
            smtp.starttls()
            smtp.ehlo()
        except smtplib.SMTPNotSupportedError:
            # Server unterstützt kein STARTTLS — wir akzeptieren das, weil
            # interne Relays häufig ohne TLS arbeiten.
            logger.debug("SMTP-Server unterstützt kein STARTTLS, sende unverschlüsselt.")
        if username and password:
            smtp.login(username, password)
        smtp.send_message(msg)
    logger.info("E-Mail an %s verschickt: %s", to, subject)


def send_daily_digests(session: "Session", today: date | None = None) -> int:
    """Versendet den Digest an jeden approved User mit offenen Aufgaben.

    Liefert die Anzahl tatsächlich verschickter E-Mails. Bei Versandfehlern
    wird die Exception nach Logging neu geworfen, damit der Aufrufer (CLI-Script)
    den Prozess mit Exit-Code 1 beenden kann.
    """
    today = today or date.today()
    users = (
        session.execute(
            select(User).where(User.status == UserStatus.APPROVED).order_by(User.email.asc())
        )
        .scalars()
        .all()
    )

    sent = 0
    failures: list[tuple[str, BaseException]] = []
    for user in users:
        if not user.email:
            continue
        try:
            digest = compose_daily_digest(session, user, today=today)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Digest-Komposition für %s fehlgeschlagen", user.email)
            failures.append((user.email, exc))
            continue
        if digest is None:
            continue
        subject, html_body = digest
        try:
            send_email(user.email, subject, html_body)
            sent += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("E-Mail-Versand an %s fehlgeschlagen", user.email)
            failures.append((user.email, exc))

    if failures:
        # Erste Fehlerursache als Wurzel weiterreichen — der Skript-Runner
        # entscheidet, was er damit macht.
        first_email, first_exc = failures[0]
        raise RuntimeError(
            f"{len(failures)} E-Mail(s) konnten nicht verschickt werden "
            f"(erste: {first_email}: {first_exc})"
        ) from first_exc

    return sent


# Stille Linter, die `datetime` als ungenutzt markieren würden, wenn sich die
# obigen Annotationen einmal ändern.
_ = datetime
