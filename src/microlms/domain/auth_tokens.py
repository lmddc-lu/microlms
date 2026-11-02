"""Anonymous bearer tokens: a uuid4 credential mapped to a learner.

Learners register without supplying any identity and receive an opaque token.
The token is the only credential; there is no password and no username.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Uuid, func
from sqlalchemy.orm import Mapped, Session, mapped_column

from microlms.db.base import Base
from microlms.domain.audit import record_audit
from microlms.domain.users import (
    User,
    UserNotFoundError,
    UserRole,
    get_user,
    memberships_for,
)

__all__: list[str] = []


class AnonymousToken(Base):
    __tablename__ = "lms_anonymous_token"

    token: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("lms_user.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TokenNotFoundError(Exception):
    """Raised when a token does not resolve to a known user."""


def register_anonymous_learner(session: Session, org_id: int) -> tuple[User, uuid.UUID]:
    """Create a nameless learner in ``org_id`` and issue a uuid4 token for it.

    User, token and audit entry share the caller's transaction, so a token
    never dangles without a user. Returns the flushed user and its token; the
    caller commits.
    """
    user = User(
        full_name="",
        memberships=memberships_for(org_id, {UserRole.LEARNER}),
    )
    session.add(user)
    session.flush()
    token = uuid.uuid4()
    session.add(AnonymousToken(token=token, user_id=user.id))
    # Self-registration: the new user is its own actor; atomic with the insert
    record_audit(
        session,
        actor_id=user.id,
        impersonated_id=None,
        action="user.registered",
        target_type="user",
        target_id=str(user.id),
        detail=None,
    )
    session.flush()
    return user, token


def resolve_anonymous_token(session: Session, token: uuid.UUID) -> User:
    """Return the user owning the token, or raise TokenNotFoundError."""
    record = session.get(AnonymousToken, token)
    if record is None:
        raise TokenNotFoundError("Unknown anonymous token")
    try:
        return get_user(session, record.user_id)
    except UserNotFoundError as error:
        raise TokenNotFoundError(str(error)) from error
