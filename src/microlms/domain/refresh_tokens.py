"""Refresh tokens: long-lived, rotated credentials that renew access tokens.

Backs the OAuth 2.0 ``refresh_token`` grant (RFC 6749 section 6). A login
starts a session, stored as a token family: every refresh consumes the
presented token and issues its successor in the same family. Each token
expires a fixed time after it was issued (an idle limit that slides with
use), and no token outlives its family's absolute limit, set at login.

Only the SHA-256 digest of a token is stored, so a database leak hands out
no working token. A consumed token stays on record until its family
expires: presenting it again means two parties hold the session, so the
whole family is revoked (refresh token rotation with reuse detection, OAuth
2.0 Security Best Current Practice, RFC 9700 section 4.14.2). The thief and
the legitimate client are both cut off, and the user logs in again.

The family id is the session id (``AuthenticatedUser.session_id``). Ending a
session queues :class:`microlms.events.SessionEnded`; the time-limit cleanup
does not.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, Uuid, delete, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from microlms.db.base import Base
from microlms.events import SessionEnded, queue_event

__all__: list[str] = []


class RefreshToken(Base):
    """One refresh token of a session (token family)."""

    __tablename__ = "lms_refresh_token"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    family_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("lms_user.id", ondelete="CASCADE"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Absolute limit of the session, shared by every token of the family
    family_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # When the token was exchanged for its successor (None = still usable)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


@dataclass(frozen=True)
class RefreshGrant:
    """The session a consumed refresh token belonged to."""

    user_id: uuid.UUID
    family_id: uuid.UUID
    family_expires_at: datetime


class InvalidRefreshTokenError(Exception):
    """Raised when a refresh token is unknown, revoked or expired."""


class RefreshTokenReusedError(InvalidRefreshTokenError):
    """Raised when an already consumed refresh token is presented again.

    Its family is revoked before this is raised; the caller commits the
    revocation even though the request fails.
    """

    def __init__(self, user_id: uuid.UUID, family_id: uuid.UUID) -> None:
        # The message reaches the client, so it names no session
        super().__init__("Refresh token was already used; its session is revoked")
        self.user_id = user_id
        self.family_id = family_id


def _hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_refresh_token(
    session: Session,
    user_id: uuid.UUID,
    family_id: uuid.UUID,
    expires_at: datetime,
    family_expires_at: datetime,
) -> str:
    """Issue a random refresh token in a session; the caller commits.

    A new ``family_id`` starts a session, an existing one continues it.
    Also clears the user's sessions past their absolute limit, which no
    longer serve reuse detection. Returns the raw token; only its digest is
    stored.
    """
    session.execute(
        delete(RefreshToken).where(
            RefreshToken.user_id == user_id,
            RefreshToken.family_expires_at <= datetime.now(UTC),
        )
    )
    token = secrets.token_urlsafe(32)
    session.add(
        RefreshToken(
            token_hash=_hash_refresh_token(token),
            family_id=family_id,
            user_id=user_id,
            expires_at=expires_at,
            family_expires_at=family_expires_at,
            used_at=None,
        )
    )
    session.flush()
    return token


def consume_refresh_token(session: Session, token: str) -> RefreshGrant:
    """Mark a refresh token used and return the session it belongs to.

    The conditional update is the check: of two concurrent requests
    presenting the same token, only one marks it, and the other is treated
    as reuse. Raises RefreshTokenReusedError (after revoking the family)
    for a token already used, and InvalidRefreshTokenError for an unknown,
    revoked or expired one. The caller commits.
    """
    now = datetime.now(UTC)
    token_hash = _hash_refresh_token(token)
    row = session.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == token_hash, RefreshToken.used_at.is_(None))
        .values(used_at=now)
        .returning(
            RefreshToken.user_id,
            RefreshToken.family_id,
            RefreshToken.expires_at,
            RefreshToken.family_expires_at,
        )
    ).one_or_none()
    if row is None:
        used = session.execute(
            select(RefreshToken.user_id, RefreshToken.family_id).where(
                RefreshToken.token_hash == token_hash
            )
        ).one_or_none()
        if used is None:
            raise InvalidRefreshTokenError("Refresh token is unknown or revoked")
        user_id, family_id = used
        session.execute(delete(RefreshToken).where(RefreshToken.family_id == family_id))
        session.flush()
        queue_event(session, SessionEnded(user_id=user_id, session_id=family_id))
        raise RefreshTokenReusedError(user_id, family_id)
    user_id, family_id, expires_at, family_expires_at = row
    if expires_at <= now or family_expires_at <= now:
        raise InvalidRefreshTokenError("Refresh token has expired")
    return RefreshGrant(user_id=user_id, family_id=family_id, family_expires_at=family_expires_at)


def revoke_refresh_token(session: Session, token: str) -> None:
    """End the session a refresh token belongs to, whatever the token's
    state, queuing SessionEnded; an unknown token changes nothing. The caller
    commits."""
    found = session.execute(
        select(RefreshToken.user_id, RefreshToken.family_id).where(
            RefreshToken.token_hash == _hash_refresh_token(token)
        )
    ).one_or_none()
    if found is None:
        return
    user_id, family_id = found
    session.execute(delete(RefreshToken).where(RefreshToken.family_id == family_id))
    session.flush()
    queue_event(session, SessionEnded(user_id=user_id, session_id=family_id))


def revoke_user_refresh_tokens(session: Session, user_id: uuid.UUID) -> None:
    """End every session of a user, queuing SessionEnded for each; the
    caller commits."""
    family_ids = session.scalars(
        select(RefreshToken.family_id)
        .where(RefreshToken.user_id == user_id)
        .distinct()
        .order_by(RefreshToken.family_id)
    ).all()
    session.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id))
    session.flush()
    for family_id in family_ids:
        queue_event(session, SessionEnded(user_id=user_id, session_id=family_id))
