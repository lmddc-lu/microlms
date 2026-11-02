"""Runtime-managed service keys for xAPI ingestion.

A key authorizes an external activity source to push statements (the
``X-Ingest-Key`` header). Keys are issued with server-generated material,
revocable, and stamp their last use, so an operator can both rotate an
integration's credential and see whether the integration is alive. Statically
configured keys (``Settings.xapi_ingest_keys``) stay valid alongside as
deployment-level bootstrap credentials.
"""

import secrets
from datetime import UTC, datetime

from sqlalchemy import DateTime, String, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from microlms.db.base import Base


class IngestKeyNotFoundError(Exception):
    def __init__(self, key_id: int) -> None:
        super().__init__(f"Ingestion key {key_id} does not exist")


class IngestKey(Base):
    __tablename__ = "lms_xapi_ingest_key"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Label of the external application the key was issued to
    name: Mapped[str] = mapped_column(String(100))
    key: Mapped[str] = mapped_column(String(64), unique=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Stamped on every authorized submission, so a silent integration is visible
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def create_ingest_key(session: Session, name: str) -> IngestKey:
    """Issue a new active key with server-generated key material."""
    ingest_key = IngestKey(name=name, key=secrets.token_urlsafe(32))
    session.add(ingest_key)
    session.flush()
    return ingest_key


def list_ingest_keys(session: Session) -> list[IngestKey]:
    """All issued keys, newest first, revoked ones included."""
    return list(session.scalars(select(IngestKey).order_by(IngestKey.id.desc())))


def get_ingest_key(session: Session, key_id: int) -> IngestKey:
    ingest_key = session.get(IngestKey, key_id)
    if ingest_key is None:
        raise IngestKeyNotFoundError(key_id)
    return ingest_key


def revoke_ingest_key(session: Session, key_id: int) -> IngestKey:
    """Deactivate a key; submissions presenting it are refused afterwards.

    Idempotent: revoking an already revoked key keeps its original
    ``revoked_at``.
    """
    ingest_key = get_ingest_key(session, key_id)
    if ingest_key.is_active:
        ingest_key.is_active = False
        ingest_key.revoked_at = datetime.now(UTC)
        session.flush()
    return ingest_key


def use_active_key(session: Session, key: str) -> IngestKey | None:
    """The active key matching ``key``, its last use stamped; None otherwise.

    Commits right here, unlike the other services: the stamp is ingestion
    bookkeeping that must persist regardless of how the submission itself
    ends.
    """
    ingest_key = session.scalars(
        select(IngestKey).where(IngestKey.key == key, IngestKey.is_active.is_(True))
    ).one_or_none()
    if ingest_key is None:
        return None
    ingest_key.last_used_at = datetime.now(UTC)
    session.commit()
    return ingest_key


def any_ingest_keys(session: Session) -> bool:
    """Whether at least one key has ever been issued (active or revoked)."""
    return session.scalars(select(IngestKey.id).limit(1)).first() is not None
