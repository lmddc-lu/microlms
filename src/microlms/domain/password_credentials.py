"""Email/password sign-in backing the JWT auth backend.

A user signs in with their email (:attr:`microlms.domain.users.User.email`)
and a password once they have a row in ``lms_password_credential``: it holds
the hashed password and the login lockout state, and is created when the
password is first set (an invited account has none until then). The
single-use password reset tokens live in ``lms_password_reset_token``.
Hashing and verification live in the JWT auth backend (:mod:`microlms.auth.jwt`)
so this module - which the migration environment imports - stays free of the
optional password-hashing dependency.
"""

import hashlib
import secrets
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, ForeignKey, String, case, delete, or_, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from microlms.db.base import Base
from microlms.domain.audit import record_audit
from microlms.domain.refresh_tokens import revoke_user_refresh_tokens
from microlms.domain.users import (
    EmailAlreadyRegisteredError,
    User,
    find_user_by_email,
    get_user,
    memberships_for,
    role_assignments_for,
)

__all__ = [
    "create_user_with_password",
]


class PasswordCredential(Base):
    """The password of a user, with its login lockout state."""

    __tablename__ = "lms_password_credential"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("lms_user.id", ondelete="CASCADE"), primary_key=True
    )
    hashed_password: Mapped[str] = mapped_column(String(255))
    # Lockout state: password attempts since the last correct one, and until
    # when attempts are refused once the ceiling was hit (None = not locked)
    failed_attempts: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PasswordResetToken(Base):
    """An outstanding password reset (or invitation) token.

    Only the SHA-256 digest of the token is stored, so a database leak does
    not hand out working reset links. A row is deleted when its token is
    consumed, when a newer token is issued for the same user, and whenever
    the user's password is set by any path.
    """

    __tablename__ = "lms_password_reset_token"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("lms_user.id", ondelete="CASCADE"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class InvalidPasswordResetTokenError(Exception):
    """Raised when a reset token is unknown, already used, revoked or expired."""


class NoSignInEmailError(Exception):
    """Raised when setting a password on an account without an email to sign
    in with."""


def _hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def get_password_credential(session: Session, user_id: uuid.UUID) -> PasswordCredential | None:
    """Return the stored credential for a user, or None when unset."""
    return session.get(PasswordCredential, user_id)


def create_user_with_password(
    session: Session,
    email: str,
    full_name: str,
    org_id: int | None,
    roles: Iterable[str],
    hashed_password: str,
) -> User:
    """Create a user holding ``roles`` as org roles in ``org_id`` (or as
    platform roles when it is None, e.g. to bootstrap an admin), its password
    credential and the audit entry in one transaction; the caller commits.

    The caller supplies an already-hashed password so this module needs no
    hashing dependency. Raises EmailAlreadyRegisteredError when the email is
    already taken.
    """
    if find_user_by_email(session, email) is not None:
        raise EmailAlreadyRegisteredError(f"Email {email} is already registered")
    if org_id is None:
        user = User(full_name=full_name, email=email, role_assignments=role_assignments_for(roles))
    else:
        user = User(full_name=full_name, email=email, memberships=memberships_for(org_id, roles))
    session.add(user)
    session.flush()
    session.add(PasswordCredential(user_id=user.id, hashed_password=hashed_password))
    # Self-registration: the new user is its own actor; atomic with the insert
    record_audit(
        session,
        actor_id=user.id,
        impersonated_id=None,
        action="user.registered",
        target_type="user",
        target_id=str(user.id),
        detail={"email": email},
    )
    session.flush()
    return user


def set_password(session: Session, user_id: uuid.UUID, hashed_password: str) -> None:
    """Set or replace a user's password, clearing any lockout.

    One operation covers provisioning (management-created accounts have no
    password yet) and reset; a reset also unlocks a locked account. Every
    session of the user ends, so a new password locks out whoever held the
    old one. Raises NoSignInEmailError when the user has no email to log in
    with.
    """
    if get_user(session, user_id).email is None:
        raise NoSignInEmailError(f"User {user_id} has no email to sign in with a password")
    credential = get_password_credential(session, user_id)
    if credential is None:
        session.add(PasswordCredential(user_id=user_id, hashed_password=hashed_password))
    else:
        credential.hashed_password = hashed_password
        credential.failed_attempts = 0
        credential.locked_until = None
    # A new password revokes every reset or invitation link still out there
    session.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id))
    revoke_user_refresh_tokens(session, user_id)
    session.flush()


def issue_password_reset_token(session: Session, user_id: uuid.UUID, expires_at: datetime) -> str:
    """Issue a random single-use reset token for a user; the caller commits.

    Revokes the user's previous tokens, so only the latest link works.
    Returns the raw token; only its digest is stored.
    """
    session.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id))
    token = secrets.token_urlsafe(32)
    session.add(
        PasswordResetToken(
            token_hash=_hash_reset_token(token), user_id=user_id, expires_at=expires_at
        )
    )
    session.flush()
    return token


def consume_password_reset_token(session: Session, token: str) -> uuid.UUID:
    """Delete a reset token and return the user it was issued to.

    The delete is the check: of two concurrent requests presenting the same
    token, only one deletes the row, so a token can never be used twice.
    Raises InvalidPasswordResetTokenError when the token is unknown (never
    issued, already used or revoked) or expired. The caller commits.
    """
    row = session.execute(
        delete(PasswordResetToken)
        .where(PasswordResetToken.token_hash == _hash_reset_token(token))
        .returning(PasswordResetToken.user_id, PasswordResetToken.expires_at)
    ).one_or_none()
    if row is None:
        raise InvalidPasswordResetTokenError("Password reset token is unknown or already used")
    user_id, expires_at = row
    if expires_at <= datetime.now(UTC):
        raise InvalidPasswordResetTokenError("Password reset token has expired")
    return user_id


def start_login_attempt(
    session: Session, user_id: uuid.UUID, max_failures: int, lockout_minutes: int
) -> bool:
    """Count a password attempt before its password is checked; False when
    the credential is locked, refusing the attempt unchecked.

    An attempt counts as a failure until :func:`clear_login_failures`
    records its success. The attempt reaching ``max_failures`` locks the
    credential for ``lockout_minutes`` and restarts the count. The lockout
    check and the count are one conditional update, so concurrent attempts
    queue on the row: at most ``max_failures`` passwords are checked per
    lockout, however many requests arrive at once.

    Commits right here, unlike the other services: the attempt must count
    even when the request then fails.
    """
    now = datetime.now(UTC)
    reaches_ceiling = PasswordCredential.failed_attempts + 1 >= max_failures
    counted = session.execute(
        update(PasswordCredential)
        .where(
            PasswordCredential.user_id == user_id,
            or_(PasswordCredential.locked_until.is_(None), PasswordCredential.locked_until <= now),
        )
        .values(
            failed_attempts=case(
                (reaches_ceiling, 0), else_=PasswordCredential.failed_attempts + 1
            ),
            locked_until=case(
                (reaches_ceiling, now + timedelta(minutes=lockout_minutes)), else_=None
            ),
        )
        .returning(PasswordCredential.user_id)
    ).one_or_none()
    session.commit()
    return counted is not None


def clear_login_failures(session: Session, user_id: uuid.UUID) -> None:
    """Reset the lockout state after a correct password.

    Commits right here, like :func:`start_login_attempt`: login bookkeeping
    is independent of whatever transaction the request may otherwise own.
    """
    session.execute(
        update(PasswordCredential)
        .where(PasswordCredential.user_id == user_id)
        .values(failed_attempts=0, locked_until=None)
    )
    session.commit()
