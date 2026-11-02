"""Request-scoped dependencies and shared authorization loaders.

Dependencies resolve app state configured by ``create_app``. Endpoints are
gated on one specific permission each, at the scope of the assignment that
grants it (see :mod:`microlms.authz`): ``require_scoped_permission`` for
org-scoped actions (it also resolves which organizations the actor reaches,
see :class:`ManagementScope`), ``require_platform_permission`` for actions on
platform-global resources (refused unless a platform assignment grants the
permission), and ``require_permission`` for scope-free gates like catalog
access and client-defined permissions. Acting on another account also goes
through :func:`forbid_unmanageable_target`, the no-escalation rule. The
loaders (``get_user_in_scope``, ``get_account_in_scope``,
``get_course_in_scope``, ``get_readable_course``, ``list_readable_courses``,
``get_activity_in_course``, ``get_enrollment_for_launch``) enforce
scope/visibility and translate domain errors into HTTP responses; core
routers, content modules and client routers replacing a core router all
share them, so a replacement router never re-implements authorization.
"""

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from microlms.auth.base import (
    AuthBackend,
    AuthenticatedUser,
    AuthenticationError,
    authenticated_user_from,
)
from microlms.authz import (
    AccessPolicy,
    GrantedPermissions,
    Permission,
    can_manage,
    has_platform_permission,
    holds_permission,
    orgs_granting,
    permissions_for,
    permissions_to_manage,
)
from microlms.config import Settings
from microlms.domain.activities import (
    Activity,
    ActivityNotFoundError,
    VariantContentPurge,
    get_activity,
    locked_activity_ids,
    satisfied_activity_ids,
)
from microlms.domain.audit import record_audit
from microlms.domain.courses import (
    Course,
    CourseNotFoundError,
    CourseStatus,
    course_visible_to,
    get_course,
    list_courses,
    list_courses_for_org_author,
    list_courses_visible_to,
)
from microlms.domain.enrollments import Enrollment, find_enrollment
from microlms.domain.orgs import OrgNotFoundError, get_org_by_slug
from microlms.domain.users import User, UserNotFoundError, get_user
from microlms.events import EventHooks, commit_with_events
from microlms.storage.base import StorageBackend

__all__ = [
    "AccessPolicyDep",
    "AuditDep",
    "AuditRecorder",
    "CurrentPermissionsDep",
    "CurrentUserDep",
    "HooksDep",
    "ManagementScope",
    "SessionDep",
    "SettingsDep",
    "forbid_unmanageable_target",
    "get_account_in_scope",
    "get_activity_in_course",
    "get_course_in_scope",
    "get_enrollment_for_launch",
    "get_readable_course",
    "get_user_in_scope",
    "list_readable_courses",
    "require_permission",
    "require_platform_permission",
    "require_scoped_permission",
]

# Request header that switches the request identity to another user ("view
# as"); only holders of the impersonate permission may send it, and the real
# identity keeps the blame in the audit log.
IMPERSONATE_HEADER = "X-Impersonate-User"


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_hooks(request: Request) -> EventHooks:
    return request.app.state.hooks


def get_storage(request: Request) -> StorageBackend:
    return request.app.state.storage


def get_variant_purger(request: Request) -> VariantContentPurge:
    return request.app.state.variant_purger


def get_access_policy(request: Request) -> AccessPolicy:
    return request.app.state.access_policy


def get_session(request: Request) -> Iterator[Session]:
    session: Session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[Session, Depends(get_session)]


def configured_org_id(session: Session, slug: str, env_var: str) -> int:
    """Id of the organization a setting names by slug; 503 when it does not
    exist, since that is a deployment misconfiguration."""
    try:
        return get_org_by_slug(session, slug).id
    except OrgNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{env_var} names a missing organization: {error}",
        ) from error


def get_registration_org(settings: SettingsDep, session: SessionDep) -> int:
    """Id of the organization self-registered users join.

    403 when ``Settings.registration_org_slug`` is unset (self-registration
    is then disabled), 503 when it names an organization that does not exist.
    """
    if settings.registration_org_slug is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Self-registration is disabled: set MICROLMS_REGISTRATION_ORG_SLUG to "
            "the organization new users join",
        )
    return configured_org_id(
        session, settings.registration_org_slug, "MICROLMS_REGISTRATION_ORG_SLUG"
    )


RegistrationOrgDep = Annotated[int, Depends(get_registration_org)]


def get_current_user(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
) -> AuthenticatedUser:
    # The backend owns credential extraction (bearer header, cookie, ...),
    # so the whole request is handed over rather than a pre-parsed token.
    auth: AuthBackend = request.app.state.auth
    try:
        user = auth.authenticate(session, request)
    except AuthenticationError as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(error)) from error
    impersonated = request.headers.get(IMPERSONATE_HEADER)
    if impersonated is None:
        return user
    return _impersonate(request, session, user, impersonated)


def _impersonate(
    request: Request,
    session: Session,
    real_user: AuthenticatedUser,
    target_ref: str,
) -> AuthenticatedUser:
    """Resolve the request as another identity ("view as").

    Requires the impersonate permission; an actor holding it through org
    assignments may only impersonate accounts whose every organization is one
    they hold it in, and only accounts whose permissions they hold themselves
    there (no escalation). Accounts holding any platform-wide permission and
    deactivated accounts cannot be impersonated. The real identity is kept on
    ``request.state.impersonator`` so the audit log blames the real actor.
    """
    policy: AccessPolicy = request.app.state.access_policy
    actor = permissions_for(real_user.roles, real_user.org_roles, policy)
    platform_wide = has_platform_permission(actor, Permission.USER_IMPERSONATE)
    actor_orgs = orgs_granting(actor, Permission.USER_IMPERSONATE)
    if not platform_wide and not actor_orgs:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Impersonation requires the {Permission.USER_IMPERSONATE} permission",
        )
    try:
        target_id = uuid.UUID(target_ref)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{IMPERSONATE_HEADER} is not a valid user id: {target_ref!r}",
        ) from error
    try:
        target = get_user(session, target_id)
    except UserNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    granted = permissions_for(target.roles, target.org_roles, policy)
    if granted.platform:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Accounts holding platform-wide permissions cannot be impersonated",
        )
    if not platform_wide and not (target.org_ids and target.org_ids <= actor_orgs):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Impersonation is limited to accounts whose every organization is one "
            "you may impersonate in",
        )
    required = permissions_to_manage(target.roles, target.org_roles, policy)
    if not can_manage(actor, required, target.org_ids):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Impersonation is limited to accounts whose permissions, and the "
            "permissions assigning their roles, you hold yourself in each of their organizations",
        )
    if not target.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Deactivated accounts cannot be impersonated",
        )
    request.state.impersonator = real_user
    return authenticated_user_from(target)


CurrentUserDep = Annotated[AuthenticatedUser, Depends(get_current_user)]
AccessPolicyDep = Annotated[AccessPolicy, Depends(get_access_policy)]


def get_current_permissions(
    current_user: CurrentUserDep,
    policy: AccessPolicyDep,
) -> GrantedPermissions:
    """Permissions the authenticated user holds, from platform assignments and
    per org: core :class:`Permission` values and client-defined strings."""
    return permissions_for(current_user.roles, current_user.org_roles, policy)


CurrentPermissionsDep = Annotated[GrantedPermissions, Depends(get_current_permissions)]


def require_permission(
    permission: str,
) -> Callable[[AuthenticatedUser, GrantedPermissions], AuthenticatedUser]:
    """Dependency factory: authorize only users whose roles grant ``permission``.

    For scope-free gates - catalog access and client-defined permission
    strings (e.g. ``require_permission("demo.reports.inspect")``). Org-scoped
    actions use :func:`require_scoped_permission`, platform-global actions
    :func:`require_platform_permission`; where the granting role is assigned
    does not matter here. Layers on top of get_current_user, so a missing or
    invalid token still fails as 401 before the permission is ever checked.
    """

    def dependency(
        current_user: CurrentUserDep, permissions: CurrentPermissionsDep
    ) -> AuthenticatedUser:
        if not holds_permission(permissions, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action requires the {permission} permission",
            )
        return current_user

    return dependency


@dataclass(frozen=True, slots=True)
class ManagementScope:
    """Who is acting, which organizations they reach, and what they hold.

    ``org_ids`` None = platform-wide reach (a platform assignment grants the
    checked permission); a set = limited to the organizations where an org
    assignment of the actor grants it (never empty). ``permissions`` is
    everything the actor holds, which the no-escalation checks compare
    against.
    """

    user: AuthenticatedUser
    org_ids: frozenset[int] | None
    permissions: GrantedPermissions


def require_scoped_permission(
    permission: str,
) -> Callable[[AuthenticatedUser, GrantedPermissions], ManagementScope]:
    """Dependency factory: authorize ``permission`` and resolve the actor's reach.

    For actions on org-scoped resources (users, enrollments, courses). Where
    the granting role is assigned decides how far the actor acts: a platform
    assignment makes the returned scope platform-wide, org assignments pin the
    action to the organizations where they grant ``permission``. Roles
    granting other permissions, or this one in other organizations, never
    widen it.
    """

    def dependency(
        current_user: CurrentUserDep, permissions: CurrentPermissionsDep
    ) -> ManagementScope:
        if has_platform_permission(permissions, permission):
            return ManagementScope(user=current_user, org_ids=None, permissions=permissions)
        org_ids = orgs_granting(permissions, permission)
        if not org_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action requires the {permission} permission",
            )
        return ManagementScope(user=current_user, org_ids=org_ids, permissions=permissions)

    return dependency


def require_platform_permission(
    permission: str,
) -> Callable[[AuthenticatedUser, GrantedPermissions], AuthenticatedUser]:
    """Dependency factory: authorize ``permission`` on a platform-global resource.

    For actions on resources with no org dimension (orgs, audit, xAPI
    administration, org membership changes): a platform assignment must grant
    the permission, so granting it through an org assignment never confers
    platform-wide power - the request is refused instead.
    """

    def dependency(
        current_user: CurrentUserDep, permissions: CurrentPermissionsDep
    ) -> AuthenticatedUser:
        if has_platform_permission(permissions, permission):
            return current_user
        if orgs_granting(permissions, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action acts platform-wide and requires the {permission} "
                "permission from a platform assignment",
            )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"This action requires the {permission} permission",
        )

    return dependency


def resolve_org_filter(scope: ManagementScope, org_id: int | None) -> frozenset[int] | None:
    """Effective organization filter for a scoped query (None = no filter).

    A platform-scoped actor filters freely (``org_id`` as given, None = every
    organization); an org-scoped actor is pinned to the organizations in
    their reach, and asking for another one is refused rather than silently
    narrowed.
    """
    if scope.org_ids is None:
        return None if org_id is None else frozenset({org_id})
    if org_id is None:
        return scope.org_ids
    if org_id not in scope.org_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Managers can only query organizations in their reach",
        )
    return frozenset({org_id})


def require_org_in_scope(scope: ManagementScope, org_id: int) -> None:
    """403 when an org-scoped actor names an organization outside their reach."""
    if scope.org_ids is not None and org_id not in scope.org_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Organization {org_id} is outside your reach",
        )


def target_scopes(scope: ManagementScope, target: User) -> frozenset[int | None]:
    """The scopes the no-escalation rule compares at when acting on ``target``:
    the platform and every organization of the account for a platform-wide
    actor, the account's organizations in reach for an org-scoped one."""
    if scope.org_ids is None:
        return frozenset({None, *target.org_ids})
    return target.org_ids & scope.org_ids


def is_manageable_target(scope: ManagementScope, target: User, policy: AccessPolicy) -> bool:
    """Whether the actor holds, at every scope compared, every permission
    ``target`` holds there and the assign permission of each role it holds
    there (see :func:`microlms.authz.can_manage`)."""
    required = permissions_to_manage(target.roles, target.org_roles, policy)
    return can_manage(scope.permissions, required, target_scopes(scope, target))


def forbid_unmanageable_target(scope: ManagementScope, target: User, policy: AccessPolicy) -> None:
    """403 when ``target`` holds a permission the actor lacks at a scope
    compared, or a role whose assign permission the actor lacks there, so
    acting on the account would escalate the actor."""
    if not is_manageable_target(scope, target, policy):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only act on accounts whose permissions, and the permissions "
            "assigning their roles, you hold yourself",
        )


def _load_user(session: Session, user_id: uuid.UUID) -> User:
    try:
        return get_user(session, user_id)
    except UserNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


def get_user_in_scope(session: Session, scope: ManagementScope, user_id: uuid.UUID) -> User:
    """Load a user sharing an organization with the acting scope: 404 unknown,
    403 when an org-scoped actor reaches none of the user's organizations.

    For actions about the user's place in those organizations (reading them,
    their enrollments and statements, their org roles). Actions on the
    account as a whole use :func:`get_account_in_scope`.
    """
    user = _load_user(session, user_id)
    if scope.org_ids is not None and not user.org_ids & scope.org_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is outside your organizations",
        )
    return user


def get_account_in_scope(session: Session, scope: ManagementScope, user_id: uuid.UUID) -> User:
    """Load a user for an account-wide action (profile, activation, deletion,
    password): 404 unknown, 403 unless an org-scoped actor reaches every
    organization the user belongs to, so one org never changes an account
    another org relies on."""
    user = _load_user(session, user_id)
    if scope.org_ids is not None and not (user.org_ids and user.org_ids <= scope.org_ids):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account-wide actions need every organization of the user in your reach",
        )
    return user


def get_course_in_scope(session: Session, scope: ManagementScope, course_id: int) -> Course:
    """Load a course for an authoring action, enforcing the acting scope:
    404 unknown, 403 when an org-scoped author touches a course owned by no
    organization in their reach (platform-owned ones included)."""
    try:
        course = get_course(session, course_id)
    except CourseNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    if scope.org_ids is not None and course.owner_org_id not in scope.org_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Course is not owned by an organization in your reach",
        )
    return course


def get_readable_course(
    session: Session,
    current_user: AuthenticatedUser,
    permissions: GrantedPermissions,
    course_id: int,
) -> Course:
    """Load a course the user may read: 404 if unknown, 403 unless the user
    is an author in reach of the course (course-edit permission from a
    platform role, or from an org role in the owning org - authors see
    drafts) or the course is published and visible to one of their orgs.

    Shared by the core courses router, content modules exposing course-scoped
    reads (e.g. the scorm SCO listing), and client routers replacing either.
    """
    try:
        course = get_course(session, course_id)
    except CourseNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    is_author_in_reach = has_platform_permission(permissions, Permission.COURSE_EDIT) or (
        course.owner_org_id in orgs_granting(permissions, Permission.COURSE_EDIT)
    )
    if not is_author_in_reach:
        if course.status != CourseStatus.PUBLISHED:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Course is not published",
            )
        if not course_visible_to(course, current_user.org_ids):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Course is not visible to your organizations",
            )
    return course


def list_readable_courses(
    session: Session, current_user: AuthenticatedUser, permissions: GrantedPermissions
) -> list[Course]:
    """The courses the user may read, by id, under the rules of
    :func:`get_readable_course`: platform authors see every course, org
    authors the courses owned by the orgs they author in plus the published
    catalog visible to their orgs, everyone else that visible catalog.

    Shared by the core courses router and client routers listing course data.
    """
    if has_platform_permission(permissions, Permission.COURSE_EDIT):
        return list_courses(session)
    author_org_ids = orgs_granting(permissions, Permission.COURSE_EDIT)
    if author_org_ids:
        return list_courses_for_org_author(session, author_org_ids, current_user.org_ids)
    return list_courses_visible_to(session, current_user.org_ids)


def get_activity_in_course(session: Session, course_id: int, activity_id: int) -> Activity:
    """Load an activity, 404ing unless it exists and belongs to the course."""
    try:
        activity = get_activity(session, activity_id)
    except ActivityNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    if activity.course_id != course_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Activity {activity_id} does not belong to course {course_id}",
        )
    return activity


def get_enrollment_for_launch(session: Session, user_id: uuid.UUID, activity_id: int) -> Enrollment:
    """Load the enrollment that authorizes launching the activity's content:
    404 on an unknown activity, 403 when the user is not enrolled in its
    course, 409 when the course is sequential and an earlier activity is
    still unsatisfied.

    Shared by the content modules' launch endpoints and client routers
    launching content, so sequential-course locking is enforced identically
    everywhere.
    """
    try:
        activity = get_activity(session, activity_id)
    except ActivityNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    enrollment = find_enrollment(session, user_id, activity.course_id)
    if enrollment is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Learner is not enrolled in this course",
        )
    satisfied = satisfied_activity_ids(session, enrollment.id)
    if activity.id in locked_activity_ids(session, activity.course_id, satisfied):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Activity is locked: the course is sequential and an earlier "
            "activity is not satisfied yet",
        )
    return enrollment


@dataclass(frozen=True, slots=True)
class AuditRecorder:
    """Records the audit entry and finishes the request's transaction.

    ``record`` is the mutation finale of an endpoint: it adds the audit entry,
    commits everything the domain services flushed into the session - so the
    change and its audit trail land atomically - and then dispatches the
    domain events the services queued (see
    :func:`microlms.events.commit_with_events`).

    Under impersonation ``actor_id`` is the real impersonating user and
    ``impersonated_id`` the identity acted as, so the trail never blames the
    impersonated user for the actor's actions.
    """

    session: Session
    hooks: EventHooks
    actor_id: uuid.UUID
    impersonated_id: uuid.UUID | None

    def record(
        self,
        action: str,
        target_type: str,
        target_id: str,
        detail: dict[str, object] | None,
    ) -> None:
        record_audit(
            self.session,
            actor_id=self.actor_id,
            impersonated_id=self.impersonated_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
        )
        commit_with_events(self.session, self.hooks)


def get_audit_recorder(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    current_user: CurrentUserDep,
    hooks: Annotated[EventHooks, Depends(get_hooks)],
) -> AuditRecorder:
    impersonator: AuthenticatedUser | None = getattr(request.state, "impersonator", None)
    if impersonator is not None:
        return AuditRecorder(
            session=session, hooks=hooks, actor_id=impersonator.id, impersonated_id=current_user.id
        )
    return AuditRecorder(
        session=session, hooks=hooks, actor_id=current_user.id, impersonated_id=None
    )


HooksDep = Annotated[EventHooks, Depends(get_hooks)]
StorageDep = Annotated[StorageBackend, Depends(get_storage)]
VariantPurgerDep = Annotated[VariantContentPurge, Depends(get_variant_purger)]
AuditDep = Annotated[AuditRecorder, Depends(get_audit_recorder)]
