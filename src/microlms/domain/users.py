"""User model and services.

Users are identified by a server-generated, immutable UUID that is exposed
everywhere: API responses, JWT subject, and xAPI actor. The email is a
profile field like the name, optional (anonymous learners and
identities from a provider sending none have no email) and unique among
users; the email/password sign-in logs users in by it, keeping only the
password (:mod:`microlms.domain.password_credentials`).

A user holds roles in two places: *platform roles* held outside any
organization (``lms_user_role``) and *org roles* held inside one organization
(``lms_org_membership``, one row per user, org and role). A user belongs to
every organization they hold at least one org role in, so they can be a
manager in one org and a learner in another. Every user holds at least one
role across both places.

Roles are free-form labels: :class:`UserRole` names the bundled ones, but the
columns store any string, so a client project can attach its own roles. What a
role *grants*, and whether it is a platform or an org role, is decided by the
access policy in :mod:`microlms.authz`, not by this module.
"""

import enum
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Select, String, Uuid, distinct, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from microlms.db.base import Base
from microlms.domain.audit import record_audit
from microlms.domain.languages import ensure_language_tag
from microlms.domain.refresh_tokens import revoke_user_refresh_tokens
from microlms.events import RoleGranted, RoleRevoked, UserCreated, queue_event

__all__ = [
    "EmailAlreadyRegisteredError",
    "User",
    "UserNotFoundError",
    "UserRole",
    "get_user",
    "list_users_in_orgs",
]

if TYPE_CHECKING:
    from microlms.domain.orgs import Org


class UserRole(enum.StrEnum):
    """The bundled role labels; clients may store other strings too."""

    LEARNER = "learner"
    INSTRUCTOR = "instructor"
    MANAGER = "manager"
    ADMIN = "admin"


class User(Base):
    __tablename__ = "lms_user"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    full_name: Mapped[str] = mapped_column(String(200))
    # Where the user is reached, and what they sign in with when they have a
    # password; None when the user has no email
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    # Deactivated accounts keep their data but are refused authentication
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Last successful login through an auth backend's login endpoint; None
    # until the first one
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Preferred language as a BCP 47 tag, for what clients send the user
    # outside the app (e.g. emails); None until one is chosen
    lang: Mapped[str | None] = mapped_column(String(35))
    # selectin so roles are loaded with the user and never lazy-load after
    # the request session is closed
    role_assignments: Mapped[list["UserRoleAssignment"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )
    memberships: Mapped[list["OrgMembership"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def roles(self) -> frozenset[str]:
        """Platform roles, held outside any organization."""
        return frozenset(assignment.role for assignment in self.role_assignments)

    @property
    def org_roles(self) -> Mapping[int, frozenset[str]]:
        """Org roles by organization id; the keys are the user's organizations."""
        return org_roles_of(self.memberships)

    @property
    def org_ids(self) -> frozenset[int]:
        """The organizations the user belongs to."""
        return frozenset(membership.org_id for membership in self.memberships)


class UserRoleAssignment(Base):
    __tablename__ = "lms_user_role"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("lms_user.id", ondelete="CASCADE"), primary_key=True
    )
    # Free-form label, not a fixed enum, so clients can add their own roles
    role: Mapped[str] = mapped_column(String(50), primary_key=True)


class OrgMembership(Base):
    """One org role a user holds inside one organization."""

    __tablename__ = "lms_org_membership"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("lms_user.id", ondelete="CASCADE"), primary_key=True
    )
    org_id: Mapped[int] = mapped_column(ForeignKey("lms_org.id"), primary_key=True)
    # Free-form label, like UserRoleAssignment.role
    role: Mapped[str] = mapped_column(String(50), primary_key=True)
    # selectin, like the user's roles, so the organization's name is at hand
    # without a lazy load
    org: Mapped["Org"] = relationship(lazy="selectin")


def org_roles_of(memberships: Iterable[OrgMembership]) -> Mapping[int, frozenset[str]]:
    """Group membership rows into org roles by organization id."""
    grouped: dict[int, set[str]] = {}
    for membership in memberships:
        grouped.setdefault(membership.org_id, set()).add(membership.role)
    return {org_id: frozenset(roles) for org_id, roles in grouped.items()}


class UserNotFoundError(Exception):
    """Raised when no user exists for the given identifier."""


class EmailAlreadyRegisteredError(Exception):
    """Raised when an email is already the email of another user."""


class NoRolesError(Exception):
    """Raised when a user would be created without any role."""


class RoleNotGrantedError(Exception):
    """Raised when revoking a role the user does not hold."""


class LastRoleRemovalError(Exception):
    """Raised when revoking a user's only role; every user holds at least one."""


def role_assignments_for(roles: Iterable[str]) -> list["UserRoleAssignment"]:
    """Build platform-role rows for a role set; raise if the set is empty.

    Sorted so the insert order is deterministic.
    """
    return [UserRoleAssignment(role=role) for role in _non_empty_roles(roles)]


def memberships_for(org_id: int, roles: Iterable[str]) -> list["OrgMembership"]:
    """Build org-role rows for a role set in one org; raise if the set is empty.

    Sorted so the insert order is deterministic.
    """
    return [OrgMembership(org_id=org_id, role=role) for role in _non_empty_roles(roles)]


def _non_empty_roles(roles: Iterable[str]) -> list[str]:
    role_set = {str(role) for role in roles}
    if not role_set:
        raise NoRolesError("A user must hold at least one role")
    return sorted(role_set)


def get_user(session: Session, user_id: uuid.UUID) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise UserNotFoundError(f"No user with id {user_id}")
    return user


@dataclass(frozen=True)
class UserFilter:
    """Conditions a listed user must meet; None leaves a condition open.

    ``search`` matches a case-insensitive substring of the email or the full
    name. ``org_ids`` keeps users belonging to any of those organizations.
    ``role`` keeps users holding it: as an org role inside ``org_ids`` when
    that filter is set, otherwise as a platform role or an org role anywhere.
    ``is_active`` keeps active (True) or deactivated (False) accounts. ``ids``
    keeps only the users with those ids.
    """

    search: str | None
    org_ids: frozenset[int] | None
    role: str | None
    is_active: bool | None
    ids: frozenset[uuid.UUID] | None


def _like_pattern(term: str) -> str:
    """A LIKE pattern matching ``term`` anywhere, its wildcards taken literally."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _filtered_users(filters: UserFilter) -> Select[tuple[User]]:
    """Select the users meeting every condition of ``filters``, unordered."""
    query = select(User)
    if filters.search is not None:
        pattern = _like_pattern(filters.search)
        query = query.where(
            or_(User.email.ilike(pattern, escape="\\"), User.full_name.ilike(pattern, escape="\\"))
        )
    if filters.org_ids is not None:
        query = query.where(User.id.in_(members_of(filters.org_ids)))
    if filters.role is not None:
        org_holders = select(OrgMembership.user_id).where(OrgMembership.role == filters.role)
        if filters.org_ids is not None:
            org_holders = org_holders.where(OrgMembership.org_id.in_(list(filters.org_ids)))
            query = query.where(User.id.in_(org_holders))
        else:
            platform_holders = select(UserRoleAssignment.user_id).where(
                UserRoleAssignment.role == filters.role
            )
            query = query.where(or_(User.id.in_(org_holders), User.id.in_(platform_holders)))
    if filters.is_active is not None:
        query = query.where(User.is_active == filters.is_active)
    if filters.ids is not None:
        query = query.where(User.id.in_(list(filters.ids)))
    return query


def list_users(
    session: Session, filters: UserFilter, limit: int | None, offset: int | None
) -> list[User]:
    """Users meeting ``filters``, oldest first. ``limit`` and ``offset`` page
    the list; either works alone (offset without limit skips the oldest and
    returns the rest)."""
    query = _filtered_users(filters).order_by(User.created_at, User.id)
    if limit is not None:
        query = query.limit(limit)
    if offset is not None:
        query = query.offset(offset)
    return list(session.scalars(query))


def count_users(session: Session, filters: UserFilter) -> int:
    """Number of users meeting ``filters``."""
    matching = _filtered_users(filters).subquery()
    return session.scalar(select(func.count()).select_from(matching)) or 0


def count_users_by_role(session: Session, filters: UserFilter) -> Mapping[str, int]:
    """Number of users meeting ``filters`` holding each role.

    With ``filters.org_ids`` set, only org roles held inside those
    organizations count; otherwise platform roles and org roles anywhere do.
    A user holding a role in several organizations counts once for it.
    """
    matching = _filtered_users(filters).with_only_columns(User.id)
    held = select(OrgMembership.role, OrgMembership.user_id).where(
        OrgMembership.user_id.in_(matching)
    )
    if filters.org_ids is not None:
        held = held.where(OrgMembership.org_id.in_(list(filters.org_ids)))
    else:
        held = held.union(
            select(UserRoleAssignment.role, UserRoleAssignment.user_id).where(
                UserRoleAssignment.user_id.in_(matching)
            )
        )
    rows = held.subquery()
    counted = session.execute(
        select(rows.c.role, func.count(distinct(rows.c.user_id))).group_by(rows.c.role)
    )
    return dict(counted.tuples().all())


def count_users_by_org(session: Session, filters: UserFilter) -> Mapping[int, int]:
    """Number of users meeting ``filters`` in each organization, as
    :func:`count_users` counts them with ``filters.org_ids`` set to that one
    organization: with ``filters.role`` set, only those holding the role inside
    it. With ``filters.org_ids`` set, only those organizations are counted;
    organizations without such a user are left out.
    """
    matching = _filtered_users(replace(filters, role=None)).with_only_columns(User.id)
    query = select(OrgMembership.org_id, func.count(distinct(OrgMembership.user_id))).where(
        OrgMembership.user_id.in_(matching)
    )
    if filters.role is not None:
        query = query.where(OrgMembership.role == filters.role)
    if filters.org_ids is not None:
        query = query.where(OrgMembership.org_id.in_(list(filters.org_ids)))
    counted = session.execute(query.group_by(OrgMembership.org_id))
    return dict(counted.tuples().all())


def members_of(org_ids: Iterable[int]):  # noqa: ANN201 - SQLAlchemy subquery
    """Subquery of the ids of users belonging to any of the organizations."""
    return select(OrgMembership.user_id).where(OrgMembership.org_id.in_(list(org_ids)))


def list_users_in_orgs(session: Session, org_ids: frozenset[int]) -> list[User]:
    """Users belonging to any of the organizations, oldest first."""
    return list(
        session.scalars(
            select(User).where(User.id.in_(members_of(org_ids))).order_by(User.created_at, User.id)
        )
    )


def update_user(session: Session, user_id: uuid.UUID, full_name: str, lang: str | None) -> User:
    """Replace the mutable profile fields of a user (``lang`` None = no
    preferred language).

    Raises InvalidLanguageTagError when ``lang`` is not a BCP 47 tag.
    """
    if lang is not None:
        ensure_language_tag(lang)
    user = get_user(session, user_id)
    user.full_name = full_name
    user.lang = lang
    session.flush()
    return user


def find_user_by_email(session: Session, email: str) -> User | None:
    """The user with this email, or None when no user has it."""
    return session.scalar(select(User).where(User.email == email))


def set_user_email(session: Session, user_id: uuid.UUID, email: str | None) -> User:
    """Give a user ``email``, or none with None.

    A user without an email cannot sign in with a password: their password,
    if any, is kept and works again once they have an email. Raises
    EmailAlreadyRegisteredError when another user has the email.
    """
    user = get_user(session, user_id)
    _ensure_email_free(session, email, user.id)
    user.email = email
    session.flush()
    return user


def _ensure_email_free(session: Session, email: str | None, owner_id: uuid.UUID | None) -> None:
    """Refuse ``email`` when a user other than ``owner_id`` has it."""
    if email is None:
        return
    other = find_user_by_email(session, email)
    if other is not None and other.id != owner_id:
        raise EmailAlreadyRegisteredError(f"Email {email} is already registered")


def record_login(session: Session, user_id: uuid.UUID, logged_in_at: datetime) -> User:
    """Stamp a successful login on the account's ``last_login_at``."""
    user = get_user(session, user_id)
    user.last_login_at = logged_in_at
    session.flush()
    return user


def deactivate_user(session: Session, user_id: uuid.UUID) -> User:
    """Refuse the account authentication while keeping its data; idempotent.

    Also ends every session of the account, so reactivating it later does
    not revive them.
    """
    user = get_user(session, user_id)
    user.is_active = False
    revoke_user_refresh_tokens(session, user_id)
    session.flush()
    return user


def reactivate_user(session: Session, user_id: uuid.UUID) -> User:
    """Allow a deactivated account to authenticate again; idempotent."""
    user = get_user(session, user_id)
    user.is_active = True
    session.flush()
    return user


def delete_user(session: Session, user_id: uuid.UUID) -> None:
    """Delete a user; every row keyed to them follows via ON DELETE CASCADE."""
    user = get_user(session, user_id)
    session.delete(user)
    session.flush()


def list_user_ids(
    session: Session, org_ids: frozenset[int] | None, role: str | None
) -> frozenset[uuid.UUID]:
    """Ids of the users matching every given filter; None leaves a filter open.

    ``org_ids`` keeps users belonging to any of those organizations. ``role``
    keeps users holding it: as an org role inside ``org_ids`` when that filter
    is set, otherwise as a platform role or an org role anywhere.
    """
    if org_ids is not None:
        query = select(OrgMembership.user_id).where(OrgMembership.org_id.in_(list(org_ids)))
        if role is not None:
            query = query.where(OrgMembership.role == role)
        return frozenset(session.scalars(query))
    if role is None:
        return frozenset(session.scalars(select(User.id)))
    platform = select(UserRoleAssignment.user_id).where(UserRoleAssignment.role == role)
    in_org = select(OrgMembership.user_id).where(OrgMembership.role == role)
    return frozenset(session.scalars(platform.union(in_org)))


def create_user(
    session: Session,
    full_name: str,
    roles: Iterable[str],
    org_id: int | None,
    lang: str | None,
    email: str | None,
) -> User:
    """Create a user holding ``roles`` as org roles in ``org_id``, or as
    platform roles when ``org_id`` is None, with ``lang`` as preferred
    language (None = none chosen yet) and ``email`` (None = none).

    Raises InvalidLanguageTagError when ``lang`` is not a BCP 47 tag and
    EmailAlreadyRegisteredError when another user has the email.
    """
    role_list = _non_empty_roles(roles)
    if lang is not None:
        ensure_language_tag(lang)
    _ensure_email_free(session, email, None)
    if org_id is None:
        user = User(
            full_name=full_name,
            email=email,
            lang=lang,
            role_assignments=role_assignments_for(role_list),
        )
    else:
        user = User(
            full_name=full_name,
            email=email,
            lang=lang,
            memberships=memberships_for(org_id, role_list),
        )
    session.add(user)
    session.flush()
    queue_event(
        session,
        UserCreated(
            user_id=user.id,
            full_name=user.full_name,
            email=user.email,
            roles=tuple(role_list),
            org_id=org_id,
            lang=user.lang,
        ),
    )
    return user


def provision_external_user(session: Session, user_id: uuid.UUID, org_id: int, role: str) -> User:
    """Return the user with this id, creating a minimal profile if absent.

    For identities that originate outside the platform and carry their own id
    (e.g. a device-generated GUID submitting xAPI): the first submission creates
    a nameless profile holding ``role`` as an org role in
    ``org_id``; later submissions attach to the same profile. Idempotent under
    concurrent first submissions - a losing insert reconciles to the row the
    winner created.

    Unlike the other services this one owns its transaction: the concurrent
    reconciliation needs the commit/rollback to happen right here, so callers
    must invoke it before starting other uncommitted work on the session.
    """
    user = session.get(User, user_id)
    if user is not None:
        return user
    user = User(id=user_id, full_name="", memberships=memberships_for(org_id, {role}))
    session.add(user)
    # System-side creation: no authenticated actor; atomic with the insert
    record_audit(
        session,
        actor_id=None,
        impersonated_id=None,
        action="user.provisioned",
        target_type="user",
        target_id=str(user_id),
        detail={"role": role, "org_id": org_id},
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.get(User, user_id)
        if existing is None:
            raise
        return existing
    session.refresh(user)
    return user


def grant_role(session: Session, user_id: uuid.UUID, role: str) -> User:
    """Add a platform role to a user; granting an already-held role changes nothing."""
    user = get_user(session, user_id)
    if role not in user.roles:
        user.role_assignments.append(UserRoleAssignment(role=role))
        session.flush()
        queue_event(session, RoleGranted(user_id=user.id, role=role, org_id=None))
    return user


def revoke_role(session: Session, user_id: uuid.UUID, role: str) -> User:
    """Remove a platform role from a user; the user's last role cannot be removed."""
    user = get_user(session, user_id)
    target = next(
        (assignment for assignment in user.role_assignments if assignment.role == role),
        None,
    )
    if target is None:
        raise RoleNotGrantedError(f"User {user_id} does not hold the platform role {role}")
    if len(user.role_assignments) + len(user.memberships) == 1:
        raise LastRoleRemovalError(f"Cannot revoke {role}: it is the only role of user {user_id}")
    # delete-orphan cascade turns the removal into a DELETE of the row
    user.role_assignments.remove(target)
    session.flush()
    queue_event(session, RoleRevoked(user_id=user.id, role=role, org_id=None))
    return user
