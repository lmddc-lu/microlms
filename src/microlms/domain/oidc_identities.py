"""OpenID Connect sign-in backing the OIDC auth backend.

A user signs in through an OpenID Connect provider when they have a row in
``lms_oidc_identity``: the provider's issuer and the subject (``sub`` claim)
it knows the user by. Like the password credential's email, these
identifiers belong to the sign-in method, not to the user. The provider owns
the roles of the users it signs in: every sign-in replaces them with the
roles its claims map to (:func:`sync_user_roles`).
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, Session, mapped_column

from microlms.db.base import Base
from microlms.domain.audit import record_audit
from microlms.domain.orgs import grant_org_role, revoke_org_role
from microlms.domain.users import (
    EmailAlreadyRegisteredError,
    NoRolesError,
    OrgMembership,
    User,
    UserRoleAssignment,
    find_user_by_email,
    get_user,
    grant_role,
    revoke_role,
)

__all__: list[str] = []


class OidcIdentity(Base):
    """The OpenID Connect sign-in of a user at one provider."""

    __tablename__ = "lms_oidc_identity"

    # The provider's issuer identifier and the user's subject there; OpenID
    # Connect guarantees the pair is unique and never reassigned
    issuer: Mapped[str] = mapped_column(String(255), primary_key=True)
    subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("lms_user.id", ondelete="CASCADE"), index=True
    )


@dataclass(frozen=True)
class AssignedRoles:
    """The roles a user should hold: platform roles, and org roles by
    organization id."""

    platform: frozenset[str]
    orgs: Mapping[int, frozenset[str]]

    def is_empty(self) -> bool:
        return not self.platform and not any(self.orgs.values())


def find_identity_user(session: Session, issuer: str, subject: str) -> User | None:
    """The user signing in as ``subject`` at ``issuer``, or None when unknown."""
    identity = session.get(OidcIdentity, (issuer, subject))
    return None if identity is None else get_user(session, identity.user_id)


def create_user_with_oidc_identity(
    session: Session,
    issuer: str,
    subject: str,
    full_name: str,
    email: str | None,
    roles: AssignedRoles,
) -> User:
    """Create a user holding ``roles``, its OIDC identity and the audit entry
    in one transaction; the caller commits.

    Raises NoRolesError when ``roles`` is empty and
    EmailAlreadyRegisteredError when another user has the email.
    """
    _ensure_roles(roles)
    if email is not None and find_user_by_email(session, email) is not None:
        raise EmailAlreadyRegisteredError(f"Email {email} is already registered")
    user = User(
        full_name=full_name,
        email=email,
        role_assignments=[UserRoleAssignment(role=role) for role in sorted(roles.platform)],
        memberships=[
            OrgMembership(org_id=org_id, role=role)
            for org_id, org_roles in roles.orgs.items()
            for role in sorted(org_roles)
        ],
    )
    session.add(user)
    session.flush()
    session.add(OidcIdentity(issuer=issuer, subject=subject, user_id=user.id))
    # First sign-in: the new user is its own actor; atomic with the insert
    record_audit(
        session,
        actor_id=user.id,
        impersonated_id=None,
        action="user.registered",
        target_type="user",
        target_id=str(user.id),
        detail={"issuer": issuer, "subject": subject},
    )
    session.flush()
    return user


def assigned_roles_of(user: User) -> AssignedRoles:
    """The roles a user holds now."""
    return AssignedRoles(platform=user.roles, orgs=user.org_roles)


def sync_user_roles(session: Session, user_id: uuid.UUID, roles: AssignedRoles) -> User:
    """Replace a user's roles with ``roles``; the caller commits.

    Grants come before revocations, so the user never passes through holding
    no role. Each change emits the usual ``RoleGranted``/``RoleRevoked``
    event. Raises NoRolesError when ``roles`` is empty.
    """
    _ensure_roles(roles)
    held = assigned_roles_of(get_user(session, user_id))
    for role in roles.platform - held.platform:
        grant_role(session, user_id, role)
    for org_id, org_roles in roles.orgs.items():
        for role in org_roles - held.orgs.get(org_id, frozenset()):
            grant_org_role(session, user_id, org_id, role)
    for role in held.platform - roles.platform:
        revoke_role(session, user_id, role)
    for org_id, org_roles in held.orgs.items():
        for role in org_roles - roles.orgs.get(org_id, frozenset()):
            revoke_org_role(session, user_id, org_id, role)
    return get_user(session, user_id)


def _ensure_roles(roles: AssignedRoles) -> None:
    if roles.is_empty():
        raise NoRolesError("A user must hold at least one role")
