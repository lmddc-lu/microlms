"""Organization model and services.

Organizations are tenants. A user belongs to every organization they hold
an org role in (``lms_org_membership``), possibly several with a different
role in each. A single-tenant deployment creates one organization for its
learners; only platform roles live outside any organization.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, exists, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from microlms.db.base import Base
from microlms.domain.users import (
    LastRoleRemovalError,
    OrgMembership,
    RoleNotGrantedError,
    User,
    get_user,
)
from microlms.events import RoleGranted, RoleRevoked, queue_event

__all__ = [
    "Org",
    "list_orgs",
]


class Org(Base):
    __tablename__ = "lms_org"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrgNotFoundError(Exception):
    """Raised when no organization exists for the given identifier."""


class OrgInUseError(Exception):
    """Raised when deleting an organization that users or courses still reference."""


class NoOrgMembershipError(Exception):
    """Raised when removing a user from an organization they do not belong to."""


class SlugAlreadyUsedError(Exception):
    """Raised when a slug is already taken by another organization."""


def get_org(session: Session, org_id: int) -> Org:
    org = session.get(Org, org_id)
    if org is None:
        raise OrgNotFoundError(f"No organization with id {org_id}")
    return org


def get_org_by_slug(session: Session, slug: str) -> Org:
    org = session.scalar(select(Org).where(Org.slug == slug))
    if org is None:
        raise OrgNotFoundError(f"No organization with slug {slug!r}")
    return org


def list_orgs(session: Session) -> list[Org]:
    return list(session.scalars(select(Org).order_by(Org.id)))


def create_org(session: Session, slug: str, name: str) -> Org:
    """Create an organization.

    Raises SlugAlreadyUsedError when the slug belongs to another organization.
    """
    if session.scalar(select(Org).where(Org.slug == slug)) is not None:
        raise SlugAlreadyUsedError(f"Slug {slug} is already used by another organization")
    org = Org(slug=slug, name=name)
    session.add(org)
    session.flush()
    return org


def delete_org(session: Session, org_id: int) -> None:
    """Delete an empty organization.

    Raises OrgInUseError while members, course assignments or owned courses
    still reference it, so deleting never silently orphans users, widens
    course visibility, or turns an org's courses platform-owned.
    """
    # Imported in the function: courses.py imports this module at module level
    from microlms.domain.courses import Course, CourseOrgAssignment

    org = get_org(session, org_id)
    has_members = session.scalar(select(exists().where(OrgMembership.org_id == org_id)))
    has_courses = session.scalar(select(exists().where(CourseOrgAssignment.org_id == org_id)))
    has_owned = session.scalar(select(exists().where(Course.owner_org_id == org_id)))
    if has_members or has_courses or has_owned:
        raise OrgInUseError(
            f"Organization {org_id} still has members, course assignments or owned courses"
        )
    session.delete(org)
    session.flush()


def grant_org_role(session: Session, user_id: uuid.UUID, org_id: int, role: str) -> User:
    """Give a user an org role inside an organization, making them a member
    if they were not; granting an already-held org role changes nothing."""
    user = get_user(session, user_id)
    org = get_org(session, org_id)
    if role not in user.org_roles.get(org.id, frozenset()):
        user.memberships.append(OrgMembership(org_id=org.id, role=role))
        session.flush()
        queue_event(session, RoleGranted(user_id=user.id, role=role, org_id=org.id))
    return user


def revoke_org_role(session: Session, user_id: uuid.UUID, org_id: int, role: str) -> User:
    """Remove an org role; revoking the last one in the org ends the membership.

    The user's last role overall cannot be removed.
    """
    user = get_user(session, user_id)
    get_org(session, org_id)
    target = next(
        (m for m in user.memberships if m.org_id == org_id and m.role == role),
        None,
    )
    if target is None:
        raise RoleNotGrantedError(
            f"User {user_id} does not hold the role {role} in organization {org_id}"
        )
    if len(user.role_assignments) + len(user.memberships) == 1:
        raise LastRoleRemovalError(f"Cannot revoke {role}: it is the only role of user {user_id}")
    # delete-orphan cascade turns the removal into a DELETE of the row
    user.memberships.remove(target)
    session.flush()
    queue_event(session, RoleRevoked(user_id=user.id, role=role, org_id=org_id))
    return user


def remove_org_membership(session: Session, user_id: uuid.UUID, org_id: int) -> User:
    """Remove a user from an organization with every org role they hold there.

    Raises NoOrgMembershipError when the user is not a member, and
    LastRoleRemovalError when those were the user's only roles.
    """
    user = get_user(session, user_id)
    get_org(session, org_id)
    in_org = [m for m in user.memberships if m.org_id == org_id]
    if not in_org:
        raise NoOrgMembershipError(f"User {user_id} does not belong to organization {org_id}")
    if len(user.role_assignments) + len(user.memberships) == len(in_org):
        raise LastRoleRemovalError(
            f"Cannot remove user {user_id} from organization {org_id}: "
            "it holds the user's only roles"
        )
    for membership in in_org:
        user.memberships.remove(membership)
    session.flush()
    for membership in in_org:
        queue_event(session, RoleRevoked(user_id=user.id, role=membership.role, org_id=org_id))
    return user
