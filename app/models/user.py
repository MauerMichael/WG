"""User- und UserRole-Modelle."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from flask_login import UserMixin
from sqlalchemy import DateTime, Enum, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import Role, UserStatus
from app.extensions import db

if TYPE_CHECKING:
    from app.models.absence import Absence
    from app.models.shopping import ShoppingItem
    from app.models.task import TaskAssignment, TaskDefinition


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    google_sub: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, name="user_status"),
        nullable=False,
        default=UserStatus.PENDING,
    )
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_assigned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    roles: Mapped[list["UserRole"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    absences: Mapped[list["Absence"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    assignments: Mapped[list["TaskAssignment"]] = relationship(
        back_populates="user",
        foreign_keys="TaskAssignment.user_id",
        # FK ist ON DELETE CASCADE + user_id NOT NULL: ohne passive_deletes
        # versucht das ORM beim Löschen, user_id auf NULL zu setzen
        # (IntegrityError). passive_deletes überlässt das Aufräumen der DB.
        passive_deletes=True,
    )
    created_task_definitions: Mapped[list["TaskDefinition"]] = relationship(
        back_populates="created_by",
        foreign_keys="TaskDefinition.created_by_id",
    )
    added_shopping_items: Mapped[list["ShoppingItem"]] = relationship(
        back_populates="added_by",
        foreign_keys="ShoppingItem.added_by_id",
    )

    def get_id(self) -> str:  # Flask-Login erwartet str.
        return str(self.id)


class UserRole(db.Model):
    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role", name="uq_user_roles_user_role"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[Role] = mapped_column(Enum(Role, name="role"), nullable=False)

    user: Mapped[User] = relationship(back_populates="roles")
