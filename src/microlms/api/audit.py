"""Audit log endpoints: the persistent journal of user and content mutations."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from microlms.api.deps import SessionDep, require_platform_permission
from microlms.authz import Permission
from microlms.domain.audit import list_audit

router = APIRouter(prefix="/audit", tags=["audit"])


class AuditEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    # None for system-side entries (e.g. externally provisioned identities)
    actor_id: uuid.UUID | None
    # Set when the actor was impersonating: the identity the request acted as
    impersonated_id: uuid.UUID | None
    action: str
    target_type: str
    target_id: str
    detail: dict[str, object] | None
    at: datetime


@router.get("", dependencies=[Depends(require_platform_permission(Permission.AUDIT_READ))])
def read_audit(
    session: SessionDep,
    actor_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    action: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[AuditEntryRead]:
    """The audit journal, newest first; every filter is optional. ``limit`` and
    ``offset`` page through it (newest first)."""
    found = list_audit(
        session,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        action=action,
        limit=limit,
        offset=offset,
    )
    return [AuditEntryRead.model_validate(entry) for entry in found]
