"""Domain-Enums.

Wir nutzen `enum.StrEnum`, damit die Werte sowohl in SQLAlchemy als auch in
Jinja-Templates direkt als Strings funktionieren.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    HAUSBEWOHNER = "HAUSBEWOHNER"
    HAUSWART = "HAUSWART"
    ADMIN = "ADMIN"


class UserStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class Recurrence(StrEnum):
    NONE = "NONE"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    BIWEEKLY = "BIWEEKLY"
    MONTHLY = "MONTHLY"
    CUSTOM = "CUSTOM"


class TaskStatus(StrEnum):
    OPEN = "OPEN"
    DONE = "DONE"
    SKIPPED = "SKIPPED"


class AssignmentStatus(StrEnum):
    OPEN = "OPEN"
    DONE = "DONE"
    SKIPPED = "SKIPPED"


class TaskKind(StrEnum):
    AUFGABE = "AUFGABE"  # abhakbar, fällig an einem Tag
    DIENST = "DIENST"  # Zeitraum-Verantwortung, am Ende abhaken


class ReviewStatus(StrEnum):
    PENDING = "PENDING"  # noch nicht geprüft
    APPROVED = "APPROVED"  # Hauswart bestätigt
    REJECTED = "REJECTED"  # Hauswart abgelehnt -> Punkte entzogen
    EXCUSED = "EXCUSED"  # entschuldigt (z. B. krank) – neutral: keine Punkte, keine Strafe


class KarmaKind(StrEnum):
    HONOR = "HONOR"  # Ehrenpunkt (positiv) – vom Hauswart für Extra-Leistung vergeben
    PENALTY = "PENALTY"  # Negativ-Karma (Strafe) – schlecht/nicht erledigt


class HandoverStatus(StrEnum):
    """Status eines Abgabe-Angebots auf der Aufgaben-Börse."""

    OPEN = "OPEN"  # auf der Börse, wartet auf Übernahme
    CLAIMED = "CLAIMED"  # übernommen -> Assignment transferiert
    CANCELLED = "CANCELLED"  # vom Anbieter zurückgezogen / auto-geschlossen
