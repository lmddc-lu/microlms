"""User endpoints.

A user holds platform roles (outside any organization) and org roles (inside
one organization each); any role may be assigned at either scope. Org-scoped
managers see and manage users through the organizations in their reach: they
read a user and edit their org roles when they share an organization, and act
on the account as a whole (profile, activation, deletion) only when every
organization of the user is in their reach. Adding a user to an organization
they are not in yet, removing them from one, and editing platform roles need
a platform assignment.

No escalation, for every actor: a role is assigned or revoked only by someone
holding all its permissions at that scope, and an account is read or acted on
only by someone holding everything it holds at the scopes compared (see
:mod:`microlms.authz`). Accounts out of an actor's reach are left out of
their listings and counts.

``email`` is a profile field, unique among users and optional (None = no
email); with the JWT backend it is also what the user signs in with, so
removing it stops their password login until they have one again.
"""

import uuid
from collections import Counter
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import AfterValidator, BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from microlms.api.deps import (
    AccessPolicyDep,
    AuditDep,
    CurrentPermissionsDep,
    CurrentUserDep,
    ManagementScope,
    SessionDep,
    forbid_unmanageable_target,
    get_account_in_scope,
    get_user_in_scope,
    is_manageable_target,
    require_org_in_scope,
    require_platform_permission,
    require_scoped_permission,
    resolve_org_filter,
)
from microlms.auth.base import AuthenticatedUser
from microlms.authz import (
    AccessPolicy,
    GrantedPermissions,
    Permission,
    can_bind,
    has_platform_permission,
)
from microlms.domain.languages import InvalidLanguageTagError
from microlms.domain.orgs import (
    NoOrgMembershipError,
    OrgNotFoundError,
    get_org,
    grant_org_role,
    remove_org_membership,
    revoke_org_role,
)
from microlms.domain.users import (
    EmailAlreadyRegisteredError,
    LastRoleRemovalError,
    RoleNotGrantedError,
    User,
    UserFilter,
    UserNotFoundError,
    count_users,
    count_users_by_org,
    count_users_by_role,
    create_user,
    deactivate_user,
    delete_user,
    get_user,
    grant_role,
    list_users,
    reactivate_user,
    revoke_role,
    set_user_email,
    update_user,
)

router = APIRouter(prefix="/users", tags=["users"])

# Sorted so the role list is deterministic when read from the roles frozenset
RolesRead = Annotated[list[str], AfterValidator(sorted)]

# One scope dependency per user-management action: each endpoint gates on its
# own permission, and the resolved scope pins org-scoped actors to their orgs.
UserReadScope = Annotated[ManagementScope, Depends(require_scoped_permission(Permission.USER_READ))]
UserCreateScope = Annotated[
    ManagementScope, Depends(require_scoped_permission(Permission.USER_CREATE))
]
UserEditScope = Annotated[ManagementScope, Depends(require_scoped_permission(Permission.USER_EDIT))]
UserDeactivateScope = Annotated[
    ManagementScope, Depends(require_scoped_permission(Permission.USER_DEACTIVATE))
]
UserActivateScope = Annotated[
    ManagementScope, Depends(require_scoped_permission(Permission.USER_ACTIVATE))
]
UserDeleteScope = Annotated[
    ManagementScope, Depends(require_scoped_permission(Permission.USER_DELETE))
]
UserRolesEditScope = Annotated[
    ManagementScope, Depends(require_scoped_permission(Permission.USER_ROLES_EDIT))
]


class UserCreate(BaseModel):
    # None creates an account without an email
    email: EmailStr | None
    full_name: str
    # Org roles in ``org_id``, or platform roles when ``org_id`` is None
    roles: Annotated[list[str], Field(min_length=1)]
    org_id: int | None
    # Preferred language as a BCP 47 tag; None (or omitted) leaves it unchosen
    lang: str | None = None


class OrgRolesRead(BaseModel):
    org_id: int
    # The organization's display name, readable by its members, who cannot
    # list organizations themselves
    name: str
    roles: RolesRead


class UserRead(BaseModel):
    id: uuid.UUID
    # None when the account has no email
    email: EmailStr | None
    full_name: str
    # Platform roles, held outside any organization
    roles: RolesRead
    # Org roles per organization, limited to the reader's reach
    orgs: list[OrgRolesRead]
    is_active: bool
    created_at: datetime
    # None until the account first logs in
    last_login_at: datetime | None
    # Preferred language as a BCP 47 tag; None until one is chosen
    lang: str | None


class UserCounts(BaseModel):
    """How many listed users there are, and how many hold each role."""

    total: int
    # Users holding each role; org roles count only inside the listed
    # organizations when the listing is limited to some
    by_role: dict[str, int]


class OrgUserCount(BaseModel):
    """How many listed users one organization has."""

    org_id: int
    total: int


class UserUpdate(BaseModel):
    """Full replacement of the mutable profile fields (``email`` None = no
    email, which stops a password login until the user has one again)."""

    email: EmailStr | None
    full_name: str
    lang: str | None


class MeRead(BaseModel):
    id: uuid.UUID
    email: EmailStr | None
    full_name: str
    roles: RolesRead
    orgs: list[OrgRolesRead]
    lang: str | None


class MeUpdate(BaseModel):
    """Self-service replacement of the own-profile fields.

    Unlike the management ``UserUpdate``, the email is not part of it: users
    sign in and recover their password with it, so changing one's own takes
    proof of identity, which the auth backend asks for (the JWT backend's
    ``PUT /auth/me/email`` asks for the current password).
    """

    full_name: str
    lang: str | None


def _orgs_read(user: User, org_ids: frozenset[int] | None) -> list[OrgRolesRead]:
    """The user's org roles by organization, limited to ``org_ids`` (None = all)."""
    names = {membership.org_id: membership.org.name for membership in user.memberships}
    return [
        OrgRolesRead(org_id=org_id, name=names[org_id], roles=list(roles))
        for org_id, roles in sorted(user.org_roles.items())
        if org_ids is None or org_id in org_ids
    ]


def _user_read(user: User, org_ids: frozenset[int] | None) -> UserRead:
    """A user as seen from an acting scope's ``org_ids``: org-scoped actors
    see only the org roles held in organizations in their reach (None = all)."""
    return UserRead(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        roles=list(user.roles),
        orgs=_orgs_read(user, org_ids),
        is_active=user.is_active,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        lang=user.lang,
    )


def _me_read(session: Session, current_user: AuthenticatedUser) -> MeRead:
    user = get_user(session, current_user.id)
    return MeRead(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        roles=list(user.roles),
        orgs=_orgs_read(user, None),
        lang=user.lang,
    )


@router.get("/me")
def read_current_user(session: SessionDep, current_user: CurrentUserDep) -> MeRead:
    return _me_read(session, current_user)


@router.get("/me/assignable-roles")
def read_assignable_roles(
    permissions: CurrentPermissionsDep,
    policy: AccessPolicyDep,
    org_id: Annotated[
        int | None, Query(description="Organization to assign in; omit for platform roles")
    ] = None,
) -> RolesRead:
    """The roles the current user may assign or revoke in organization
    ``org_id`` (or on the platform when omitted): those whose every permission,
    and assign permission when the policy names one, they hold there. Only the
    no-escalation rule is applied; assigning still needs the user-management
    permission of the endpoint used."""
    return [role for role in policy.roles if can_bind(permissions, role, org_id, policy)]


@router.put("/me")
def edit_current_user(
    session: SessionDep,
    current_user: CurrentUserDep,
    audit: AuditDep,
    payload: MeUpdate,
) -> MeRead:
    """Update the authenticated user's own profile fields."""
    try:
        update_user(session, current_user.id, full_name=payload.full_name, lang=payload.lang)
    except InvalidLanguageTagError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    audit.record(
        "user.updated",
        "user",
        str(current_user.id),
        {"full_name": payload.full_name, "lang": payload.lang},
    )
    return _me_read(session, current_user)


def _require_known_roles(policy: AccessPolicy, roles: set[str]) -> None:
    """422 when a role is not part of the deployment's configured vocabulary."""
    unknown = sorted(roles - set(policy.roles))
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown role(s): {unknown}",
        )


def _forbid_escalating_roles(
    actor: GrantedPermissions, roles: set[str], org_id: int | None, policy: AccessPolicy
) -> None:
    """403 when the actor lacks a permission of a role, or its assign
    permission, at the scope it is assigned or revoked at (org ``org_id``, or
    the platform when None)."""
    escalating = sorted(role for role in roles if not can_bind(actor, role, org_id, policy))
    if escalating:
        where = "on the platform" if org_id is None else f"in organization {org_id}"
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Assigning role(s) {escalating} needs permissions you do not hold {where}",
        )


def _load_target(session: Session, user_id: uuid.UUID) -> User:
    """404 when the user does not exist."""
    try:
        return get_user(session, user_id)
    except UserNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


def _require_org(session: Session, org_id: int) -> None:
    """404 when the organization does not exist."""
    try:
        get_org(session, org_id)
    except OrgNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


def _user_filter(
    scope: ManagementScope,
    search: str | None,
    org_id: int | None,
    role: str | None,
    is_active: bool | None,
    ids: list[uuid.UUID] | None,
) -> UserFilter:
    """The listing filter of a request: org-scoped actors are pinned to the
    organizations in their reach, and naming another one is refused."""
    return UserFilter(
        search=search,
        org_ids=resolve_org_filter(scope, org_id),
        role=role,
        ids=None if ids is None else frozenset(ids),
        is_active=is_active,
    )


def _manages_everyone(scope: ManagementScope, policy: AccessPolicy) -> bool:
    """Whether a platform-wide actor holds every permission of every role, so
    no account can be out of their reach and listings need no filtering."""
    every_permission = frozenset().union(*policy.roles.values())
    return scope.org_ids is None and every_permission <= scope.permissions.platform


def _visible_users(
    session: Session, scope: ManagementScope, policy: AccessPolicy, filters: UserFilter
) -> list[User]:
    """Every user the actor may list, oldest first.

    Accounts holding a permission the actor lacks are left out; that filter is
    permission-based, so it happens here rather than in the query.
    """
    return [
        user
        for user in list_users(session, filters, limit=None, offset=None)
        if is_manageable_target(scope, user, policy)
    ]


def _counted_orgs(user: User, filters: UserFilter) -> frozenset[int]:
    """The organizations a listed user counts in: those listed (any when the
    listing is unlimited) where they hold ``filters.role``, or any role when
    it is None."""
    return frozenset(
        org_id
        for org_id, roles in user.org_roles.items()
        if (filters.org_ids is None or org_id in filters.org_ids)
        and (filters.role is None or filters.role in roles)
    )


def _held_roles(user: User, org_ids: frozenset[int] | None) -> frozenset[str]:
    """The roles a user counts for: org roles inside ``org_ids``, or platform
    roles plus org roles anywhere when it is None."""
    if org_ids is None:
        return user.roles.union(*user.org_roles.values())
    return frozenset().union(
        *(roles for org_id, roles in user.org_roles.items() if org_id in org_ids)
    )


UserSearch = Annotated[
    str | None, Query(description="Case-insensitive part of the email or full name")
]
UserOrgFilter = Annotated[int | None, Query(description="Only members of this organization")]
UserRoleFilter = Annotated[
    str | None,
    Query(description="Only holders of this role (inside the listed organizations, if limited)"),
]
UserActiveFilter = Annotated[
    bool | None, Query(description="Only active (true) or deactivated (false) accounts")
]
UserIdFilter = Annotated[
    list[uuid.UUID] | None,
    Query(
        alias="id",
        max_length=100,
        description="Only these users (repeat the parameter, at most 100); ids of deleted "
        "accounts or of accounts out of reach are left out",
    ),
]


@router.get("")
def read_users(
    session: SessionDep,
    scope: UserReadScope,
    policy: AccessPolicyDep,
    response: Response,
    search: UserSearch = None,
    org_id: UserOrgFilter = None,
    role: UserRoleFilter = None,
    is_active: UserActiveFilter = None,
    ids: UserIdFilter = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[UserRead]:
    """List users in scope matching the filters, leaving out accounts holding
    a permission the actor lacks.

    ``limit``/``offset`` page the matching list (oldest first); omit both to
    get every user. The total number of matching users is returned in the
    ``X-Total-Count`` header so a client can render page numbers."""
    filters = _user_filter(scope, search, org_id, role, is_active, ids)
    if _manages_everyone(scope, policy):
        total = count_users(session, filters)
        found = list_users(session, filters, limit=limit, offset=offset)
    else:
        visible = _visible_users(session, scope, policy, filters)
        total = len(visible)
        start = offset or 0
        end = start + limit if limit is not None else None
        found = visible[start:end]
    response.headers["X-Total-Count"] = str(total)
    return [_user_read(user, scope.org_ids) for user in found]


@router.get("/counts")
def read_user_counts(
    session: SessionDep,
    scope: UserReadScope,
    policy: AccessPolicyDep,
    search: UserSearch = None,
    org_id: UserOrgFilter = None,
    role: UserRoleFilter = None,
    is_active: UserActiveFilter = None,
) -> UserCounts:
    """Count the users ``GET /users`` lists with the same filters, in total
    and per role, so a dashboard needs no full listing."""
    filters = _user_filter(scope, search, org_id, role, is_active, None)
    if _manages_everyone(scope, policy):
        return UserCounts(
            total=count_users(session, filters), by_role=count_users_by_role(session, filters)
        )
    visible = _visible_users(session, scope, policy, filters)
    by_role = Counter(held for user in visible for held in _held_roles(user, filters.org_ids))
    return UserCounts(total=len(visible), by_role=dict(by_role))


@router.get("/counts/by-org")
def read_user_counts_by_org(
    session: SessionDep,
    scope: UserReadScope,
    policy: AccessPolicyDep,
    role: UserRoleFilter = None,
    is_active: UserActiveFilter = None,
) -> list[OrgUserCount]:
    """Count, for every organization in reach, the users ``GET /users/counts``
    counts with ``org_id`` set to it and the same filters, so a dashboard needs
    one request rather than one per organization. Ordered by organization id;
    organizations without such a user are left out."""
    filters = _user_filter(scope, None, None, role, is_active, None)
    if _manages_everyone(scope, policy):
        counts = count_users_by_org(session, filters)
    else:
        visible = _visible_users(session, scope, policy, filters)
        counts = Counter(org_id for user in visible for org_id in _counted_orgs(user, filters))
    return [OrgUserCount(org_id=org_id, total=total) for org_id, total in sorted(counts.items())]


@router.get("/{user_id}")
def read_user(
    session: SessionDep,
    scope: UserReadScope,
    policy: AccessPolicyDep,
    user_id: uuid.UUID,
) -> UserRead:
    """Read a user in scope holding no permission the actor lacks."""
    target = get_user_in_scope(session, scope, user_id)
    forbid_unmanageable_target(scope, target, policy)
    return _user_read(target, scope.org_ids)


@router.post("", status_code=status.HTTP_201_CREATED)
def add_user(
    session: SessionDep,
    scope: UserCreateScope,
    policy: AccessPolicyDep,
    audit: AuditDep,
    payload: UserCreate,
) -> UserRead:
    """Create a user holding ``roles`` as org roles in ``org_id``, or as
    platform roles when ``org_id`` is None (platform-wide actors only).

    Org-scoped managers create users only inside an organization in their
    reach, and nobody assigns a role granting a permission they do not hold
    at that scope.
    """
    roles = set(payload.roles)
    _require_known_roles(policy, roles)
    if payload.org_id is None:
        if scope.org_ids is not None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Managers can only create users inside one of their organizations",
            )
    else:
        require_org_in_scope(scope, payload.org_id)
        _require_org(session, payload.org_id)
    _forbid_escalating_roles(scope.permissions, roles, payload.org_id, policy)
    try:
        user = create_user(
            session,
            full_name=payload.full_name,
            roles=roles,
            org_id=payload.org_id,
            lang=payload.lang,
            email=payload.email,
        )
    except EmailAlreadyRegisteredError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except InvalidLanguageTagError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    audit.record(
        "user.created",
        "user",
        str(user.id),
        {
            "email": payload.email,
            "roles": sorted(roles),
            "org_id": payload.org_id,
            "lang": user.lang,
        },
    )
    return _user_read(user, scope.org_ids)


@router.put("/{user_id}")
def edit_user(
    session: SessionDep,
    scope: UserEditScope,
    policy: AccessPolicyDep,
    audit: AuditDep,
    user_id: uuid.UUID,
    payload: UserUpdate,
) -> UserRead:
    """Update a user's profile; org managers only for accounts whose every
    organization is in their reach."""
    target = get_account_in_scope(session, scope, user_id)
    forbid_unmanageable_target(scope, target, policy)
    try:
        updated = update_user(session, target.id, full_name=payload.full_name, lang=payload.lang)
        updated = set_user_email(session, target.id, payload.email)
    except EmailAlreadyRegisteredError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except InvalidLanguageTagError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    audit.record(
        "user.updated",
        "user",
        str(target.id),
        {"email": payload.email, "full_name": payload.full_name, "lang": payload.lang},
    )
    return _user_read(updated, scope.org_ids)


@router.post("/{user_id}/deactivate")
def deactivate(
    session: SessionDep,
    scope: UserDeactivateScope,
    policy: AccessPolicyDep,
    audit: AuditDep,
    user_id: uuid.UUID,
) -> UserRead:
    """Refuse the account authentication while keeping its data; idempotent."""
    target = get_account_in_scope(session, scope, user_id)
    forbid_unmanageable_target(scope, target, policy)
    deactivated = deactivate_user(session, target.id)
    audit.record("user.deactivated", "user", str(target.id), None)
    return _user_read(deactivated, scope.org_ids)


@router.post("/{user_id}/activate")
def activate(
    session: SessionDep,
    scope: UserActivateScope,
    policy: AccessPolicyDep,
    audit: AuditDep,
    user_id: uuid.UUID,
) -> UserRead:
    """Allow a deactivated account to authenticate again; idempotent."""
    target = get_account_in_scope(session, scope, user_id)
    forbid_unmanageable_target(scope, target, policy)
    reactivated = reactivate_user(session, target.id)
    audit.record("user.activated", "user", str(target.id), None)
    return _user_read(reactivated, scope.org_ids)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_user(
    session: SessionDep,
    scope: UserDeleteScope,
    policy: AccessPolicyDep,
    audit: AuditDep,
    user_id: uuid.UUID,
) -> None:
    """Delete a user; every row keyed to them follows via ON DELETE CASCADE.

    Account-wide like the other account actions: an org-scoped actor deletes
    only accounts whose every organization is in their reach, and never one
    holding a permission they lack.
    """
    target = get_account_in_scope(session, scope, user_id)
    forbid_unmanageable_target(scope, target, policy)
    try:
        delete_user(session, target.id)
    except UserNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    audit.record("user.deleted", "user", str(user_id), None)


@router.put(
    "/{user_id}/roles/{role}",
    dependencies=[Depends(require_platform_permission(Permission.USER_ROLES_EDIT))],
)
def add_platform_role(
    session: SessionDep,
    scope: UserRolesEditScope,
    policy: AccessPolicyDep,
    audit: AuditDep,
    user_id: uuid.UUID,
    role: str,
) -> UserRead:
    """Assign a role at platform scope; assigning an already-held role is a
    no-op.

    Platform roles act everywhere, so only platform-wide actors assign them,
    and only roles whose permissions they hold platform-wide.
    """
    _require_known_roles(policy, {role})
    target = _load_target(session, user_id)
    forbid_unmanageable_target(scope, target, policy)
    _forbid_escalating_roles(scope.permissions, {role}, None, policy)
    granted = grant_role(session, user_id, role)
    audit.record("user.role_granted", "user", str(user_id), {"role": role})
    return _user_read(granted, None)


@router.delete(
    "/{user_id}/roles/{role}",
    dependencies=[Depends(require_platform_permission(Permission.USER_ROLES_EDIT))],
)
def remove_platform_role(
    session: SessionDep,
    scope: UserRolesEditScope,
    policy: AccessPolicyDep,
    audit: AuditDep,
    user_id: uuid.UUID,
    role: str,
) -> UserRead:
    """Revoke a platform role; the user's last role cannot be revoked."""
    _require_known_roles(policy, {role})
    target = _load_target(session, user_id)
    forbid_unmanageable_target(scope, target, policy)
    _forbid_escalating_roles(scope.permissions, {role}, None, policy)
    try:
        revoked = revoke_role(session, user_id, role)
    except RoleNotGrantedError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except LastRoleRemovalError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    audit.record("user.role_revoked", "user", str(user_id), {"role": role})
    return _user_read(revoked, None)


@router.put("/{user_id}/orgs/{org_id}/roles/{role}")
def add_org_role(
    session: SessionDep,
    scope: UserRolesEditScope,
    policy: AccessPolicyDep,
    audit: AuditDep,
    user_id: uuid.UUID,
    org_id: int,
    role: str,
) -> UserRead:
    """Assign a role inside an organization; a no-op when already held.

    Managers assign roles whose permissions they hold in that organization to
    members of an organization in their reach. Granting the first role in an
    organization adds the user to it, which is cross-org by nature and needs
    ``lms.user.org.edit`` from a platform assignment.
    """
    _require_known_roles(policy, {role})
    require_org_in_scope(scope, org_id)
    _require_org(session, org_id)
    target = _load_target(session, user_id)
    if org_id not in target.org_ids and not has_platform_permission(
        scope.permissions, Permission.USER_ORG_EDIT
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"User is not a member of organization {org_id}: adding them requires "
            f"the {Permission.USER_ORG_EDIT} permission from a platform assignment",
        )
    forbid_unmanageable_target(scope, target, policy)
    _forbid_escalating_roles(scope.permissions, {role}, org_id, policy)
    granted = grant_org_role(session, user_id, org_id, role)
    audit.record("user.org_role_granted", "user", str(user_id), {"org_id": org_id, "role": role})
    return _user_read(granted, scope.org_ids)


@router.delete("/{user_id}/orgs/{org_id}/roles/{role}")
def remove_org_role(
    session: SessionDep,
    scope: UserRolesEditScope,
    policy: AccessPolicyDep,
    audit: AuditDep,
    user_id: uuid.UUID,
    org_id: int,
    role: str,
) -> UserRead:
    """Revoke an org role; revoking the last one in an organization removes
    the user from it. The user's last role overall cannot be revoked."""
    _require_known_roles(policy, {role})
    require_org_in_scope(scope, org_id)
    target = get_user_in_scope(session, scope, user_id)
    forbid_unmanageable_target(scope, target, policy)
    _forbid_escalating_roles(scope.permissions, {role}, org_id, policy)
    try:
        revoked = revoke_org_role(session, user_id, org_id, role)
    except (OrgNotFoundError, RoleNotGrantedError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except LastRoleRemovalError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    audit.record("user.org_role_revoked", "user", str(user_id), {"org_id": org_id, "role": role})
    return _user_read(revoked, scope.org_ids)


@router.delete(
    "/{user_id}/orgs/{org_id}",
    dependencies=[Depends(require_platform_permission(Permission.USER_ORG_EDIT))],
)
def remove_from_org(
    session: SessionDep, audit: AuditDep, user_id: uuid.UUID, org_id: int
) -> UserRead:
    """Remove a user from an organization with every org role held there.

    Cross-org by nature, so it requires a platform assignment.
    """
    try:
        removed = remove_org_membership(session, user_id, org_id)
    except (UserNotFoundError, OrgNotFoundError, NoOrgMembershipError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except LastRoleRemovalError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    audit.record("user.org_removed", "user", str(user_id), {"org_id": org_id})
    return _user_read(removed, None)
