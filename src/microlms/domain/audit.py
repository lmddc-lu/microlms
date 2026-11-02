"""Audit log: a persistent journal of user and content mutations.

Every management mutation, self-registration and externally provisioned
identity leaves one entry. ``actor_id`` carries no foreign key on purpose:
the trail must survive the deletion of the acting or provisioned user, and
externally provisioned identities have no actor at all.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, String, Uuid, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from microlms.db.base import Base

__all__: list[str] = []


class AuditLogEntry(Base):
    __tablename__ = "lms_audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    # The real authenticated identity; None for system-side entries such as
    # externally provisioned identities
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    # Set when the actor was impersonating: the identity the request acted as
    impersonated_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # Dotted action label, e.g. "user.created", "course.published"
    action: Mapped[str] = mapped_column(String(100))
    target_type: Mapped[str] = mapped_column(String(50))
    target_id: Mapped[str] = mapped_column(String(100), index=True)
    # Small action-specific payload (changed fields, granted role, ...)
    detail: Mapped[dict[str, object] | None] = mapped_column(JSON)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def record_audit(
    session: Session,
    actor_id: uuid.UUID | None,
    impersonated_id: uuid.UUID | None,
    action: str,
    target_type: str,
    target_id: str,
    detail: dict[str, object] | None,
) -> None:
    """Add an audit entry to the session WITHOUT committing.

    The entry joins the caller's transaction: domain services get an entry
    atomic with their writes, and the API recorder commits the whole request
    transaction after adding it.
    """
    session.add(
        AuditLogEntry(
            actor_id=actor_id,
            impersonated_id=impersonated_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
        )
    )


def list_audit(
    session: Session,
    actor_id: uuid.UUID | None,
    target_type: str | None,
    target_id: str | None,
    action: str | None,
    limit: int | None,
    offset: int | None,
) -> list[AuditLogEntry]:
    """Audit entries matching every given filter, newest first.

    ``offset`` skips that many of the newest matching entries, so ``limit``
    and ``offset`` page through the journal; either works alone."""
    query = select(AuditLogEntry).order_by(AuditLogEntry.id.desc())
    if actor_id is not None:
        query = query.where(AuditLogEntry.actor_id == actor_id)
    if target_type is not None:
        query = query.where(AuditLogEntry.target_type == target_type)
    if target_id is not None:
        query = query.where(AuditLogEntry.target_id == target_id)
    if action is not None:
        query = query.where(AuditLogEntry.action == action)
    if limit is not None:
        query = query.limit(limit)
    if offset is not None:
        query = query.offset(offset)
    return list(session.scalars(query))
