"""SQLAlchemy-Modelle.

Re-Exports aller Model-Klassen, damit `from app.models import *` für
Alembic-Autogenerate und Tests alle Tabellen registriert.
"""

from app.models.absence import Absence
from app.models.audit import AuditLog
from app.models.extra import ExtraContribution
from app.models.handover import TaskHandoverOffer
from app.models.karma import KarmaEvent
from app.models.shopping import ShoppingItem
from app.models.task import (
    TaskAssignment,
    TaskDefinition,
    TaskDefinitionEligibleUser,
    TaskOccurrence,
    TaskStep,
    TaskStepCompletion,
)
from app.models.user import User, UserRole

__all__ = [
    "User",
    "UserRole",
    "Absence",
    "TaskDefinition",
    "TaskDefinitionEligibleUser",
    "TaskOccurrence",
    "TaskAssignment",
    "TaskStep",
    "TaskStepCompletion",
    "ShoppingItem",
    "AuditLog",
    "KarmaEvent",
    "ExtraContribution",
    "TaskHandoverOffer",
]
