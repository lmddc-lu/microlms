"""Permission-based authorization, modelled on Kubernetes RBAC.

The core enforces *permissions*, never role names. A role is a label for a
set of permission strings; a client project supplies an :class:`AccessPolicy`
to ``create_app`` that says what each role grants and is free to compose its
own roles on top of, or instead of, the bundled ones. Every guarded action
checks one specific permission ("may this user publish a course"), so
authorization is decoupled from any particular role vocabulary.

Permissions are concrete dot-separated strings named resource-first
(``lms.course.publish``); there are no wildcards and no deny rules, grants
only add up. :class:`Permission` enumerates the ones the core itself checks;
the ``lms.`` namespace belongs to the core, so a grant naming an ``lms.``
string that is not a :class:`Permission` member is refused. A client project
may grant additional permission strings in its own namespace (e.g.
``"demo.reports.inspect"``) and gate its routers on them via the
``microlms.api.deps`` dependency factories - the core carries them through
without interpreting them.

A role is assigned at a scope, like a Kubernetes role binding, and any role
may be assigned at either one:

- *Platform* (``lms_user_role``, a ClusterRoleBinding): the role's
  permissions apply in every organization and on the platform-global
  resources (orgs, audit, xAPI administration, LTI tool registration, org
  membership changes), which only platform assignments reach.
- *Org* (``lms_org_membership``, a RoleBinding): the role's permissions apply
  inside that one organization (courses scope by their owning org). A user
  may hold different roles in different organizations.

*Self* is never permission-gated: the ``/me`` endpoints (own profile, own
enrollments, own password) work for any authenticated user, so a permission
is always about acting on others. Scope never carries over between
organizations: a user who is a manager in one org and a learner in another
manages users in the first only. A user's *effective* permissions at an org
are their platform grants plus their grants in that org; at platform scope,
their platform grants alone (:func:`permissions_at`).

No escalation (Kubernetes' role binding restriction): an actor may assign a
role at a scope only when they already hold every permission of the role at
that scope (:func:`can_bind`), and may act on an account only when, at every
scope checked, they hold every permission the account holds there
(:func:`can_manage`). So a manager can never hand out, or take over, more
than they hold. A client keeps accounts out of every manager's reach by
granting them a permission managers do not hold.

Holding a role's permissions is enough to assign it, so by default the
holders of a role may hand it out to others and act on each other. A role
listed in ``AccessPolicy.assign_permissions`` also requires its *assign
permission*: to assign the role, or to act on an account holding it, an
actor must hold that permission on top of the role's own. The role does not
grant it, so a client keeps a role out of its own holders' reach (an org
manager who must not appoint or manage fellow managers) by naming a
permission only the roles above it grant.

``DEFAULT_ACCESS_POLICY`` holds the bundled :class:`UserRole` grants (admin
holds every core permission, manager manages its org's accounts, instructor
authors its org's courses). Clients pass it as-is, extend it, or replace it
entirely.
"""

import enum
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from microlms.domain.users import UserRole

# Namespace owned by the core: every string in it is a Permission member
CORE_NAMESPACE = "lms."


class Permission(enum.StrEnum):
    """One specific action the core checks, independent of how roles are named.

    Every member is org-scoped (granted everywhere by a platform assignment)
    unless its comment says a platform assignment is required.
    """

    # User account management
    USER_READ = "lms.user.read"
    USER_CREATE = "lms.user.create"
    USER_EDIT = "lms.user.edit"
    USER_DEACTIVATE = "lms.user.deactivate"
    USER_ACTIVATE = "lms.user.activate"
    USER_DELETE = "lms.user.delete"
    USER_ROLES_EDIT = "lms.user.roles.edit"
    USER_RESET_PASSWORD = "lms.user.reset-password"
    USER_IMPERSONATE = "lms.user.impersonate"
    # Attaching/detaching users to organizations is cross-org by nature:
    # platform assignment required
    USER_ORG_EDIT = "lms.user.org.edit"

    # Organization management: platform assignment required
    ORG_READ = "lms.org.read"
    ORG_CREATE = "lms.org.create"
    ORG_DELETE = "lms.org.delete"

    # Course authoring, scoped by the course's owning org. COURSE_EDIT also
    # covers activities, prerequisites, org visibility, and reading drafts.
    COURSE_CREATE = "lms.course.create"
    COURSE_EDIT = "lms.course.edit"
    COURSE_PUBLISH = "lms.course.publish"
    COURSE_ARCHIVE = "lms.course.archive"
    COURSE_DELETE = "lms.course.delete"

    # Enrollment management, scoped by the enrollee's org
    ENROLLMENT_READ = "lms.enrollment.read"
    ENROLLMENT_CREATE = "lms.enrollment.create"
    ENROLLMENT_COMPLETE = "lms.enrollment.complete"
    # The due-soon notification trigger fires globally: platform assignment required
    ENROLLMENT_NOTIFY = "lms.enrollment.notify"

    # View and consume the published course catalog (visibility rules apply);
    # a user without it sees no content. CONTENT_IMPORT gates importing
    # packages (scorm/cmi5 modules), scoped by the course's owning org.
    CONTENT_ACCESS = "lms.content.access"
    CONTENT_IMPORT = "lms.content.import"

    # The platform-wide audit trail: platform assignment required
    AUDIT_READ = "lms.audit.read"

    # xAPI module administration: platform assignment required
    XAPI_CONFIG_READ = "lms.xapi.config.read"
    XAPI_KEYS_MANAGE = "lms.xapi.keys.manage"
    # xAPI reporting over learner statements, scoped by the learner's org
    XAPI_STATEMENTS_READ = "lms.xapi.statements.read"

    # LTI tool registration (the lti module): platform assignment required
    LTI_TOOLS_MANAGE = "lms.lti.tools.manage"


_CORE_PERMISSIONS = frozenset(Permission)


@dataclass(frozen=True, slots=True)
class AccessPolicy:
    """The deployment's authorization policy, passed to ``create_app``.

    ``roles`` maps every role label the deployment uses to the permission
    strings it grants: core :class:`Permission` members (StrEnum, so plain
    strings) and client-defined strings the client's own routers check. A
    role absent from it is unknown and cannot be assigned.

    ``assign_permissions`` maps a role to the permission an actor must hold,
    on top of every permission of the role, to assign it or act on its
    holders (see the module docstring); a role absent from it needs only its
    own permissions.
    """

    roles: Mapping[str, frozenset[str]]
    assign_permissions: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class GrantedPermissions:
    """The permissions a user holds: from platform assignments, and from org
    assignments per organization id."""

    platform: frozenset[str]
    orgs: Mapping[int, frozenset[str]]


# Default policy for the bundled roles: the admin holds every core permission;
# the manager manages users and enrollments; the instructor authors courses and
# imports content; every bundled role can access the catalog. Content import
# runs package scripts on the app origin with the viewer's session, so only
# holders of it can hand out the instructor role.
DEFAULT_ACCESS_POLICY = AccessPolicy(
    roles={
        UserRole.ADMIN: frozenset(Permission),
        UserRole.MANAGER: frozenset(
            {
                Permission.USER_READ,
                Permission.USER_CREATE,
                Permission.USER_EDIT,
                Permission.USER_DEACTIVATE,
                Permission.USER_ACTIVATE,
                Permission.USER_ROLES_EDIT,
                Permission.USER_RESET_PASSWORD,
                Permission.ENROLLMENT_READ,
                Permission.ENROLLMENT_CREATE,
                Permission.ENROLLMENT_COMPLETE,
                Permission.XAPI_STATEMENTS_READ,
                Permission.CONTENT_ACCESS,
            }
        ),
        UserRole.INSTRUCTOR: frozenset(
            {
                Permission.COURSE_CREATE,
                Permission.COURSE_EDIT,
                Permission.COURSE_PUBLISH,
                Permission.COURSE_ARCHIVE,
                Permission.COURSE_DELETE,
                Permission.CONTENT_IMPORT,
                Permission.CONTENT_ACCESS,
            }
        ),
        UserRole.LEARNER: frozenset({Permission.CONTENT_ACCESS}),
    },
    assign_permissions={},
)


def has_platform_permission(granted: GrantedPermissions, permission: str) -> bool:
    """Whether a platform assignment grants ``permission`` (in every organization)."""
    return permission in granted.platform


def orgs_granting(granted: GrantedPermissions, permission: str) -> frozenset[int]:
    """The organizations in which an org assignment grants ``permission``."""
    return frozenset(org_id for org_id, grants in granted.orgs.items() if permission in grants)


def holds_permission(granted: GrantedPermissions, permission: str) -> bool:
    """Whether any assignment grants ``permission``, anywhere; for scope-free gates."""
    return has_platform_permission(granted, permission) or bool(orgs_granting(granted, permission))


def permissions_at(granted: GrantedPermissions, org_id: int | None) -> frozenset[str]:
    """The effective permissions at a scope: platform grants plus the grants
    in organization ``org_id``, or platform grants alone when it is None."""
    if org_id is None:
        return granted.platform
    return granted.platform | granted.orgs.get(org_id, frozenset())


def can_bind(
    actor: GrantedPermissions, role: str, org_id: int | None, policy: AccessPolicy
) -> bool:
    """Whether ``actor`` may assign (or revoke) ``role`` at a scope: they hold
    every permission of the role there, and its assign permission when the
    policy names one. ``role`` must be in the policy."""
    return _requirements_of((role,), policy) <= permissions_at(actor, org_id)


def can_manage(
    actor: GrantedPermissions, required: GrantedPermissions, scopes: Iterable[int | None]
) -> bool:
    """Whether ``actor`` may act on an account whose management needs
    ``required`` (see :func:`permissions_to_manage`): at every scope in
    ``scopes`` (org ids, None for platform), the actor holds every permission
    required there."""
    return all(permissions_at(required, scope) <= permissions_at(actor, scope) for scope in scopes)


def validate_access_policy(policy: AccessPolicy) -> None:
    """Reject grant strings the core would misread.

    Wildcards are not supported, so a grant containing ``*`` would silently
    match nothing; a concrete ``lms.`` string that is not a
    :class:`Permission` member is a typo the core would never check. An assign
    permission is checked the same way, and must belong to a role of the
    policy. Called by ``create_app`` on the policy it receives.
    """
    for role, permissions in policy.roles.items():
        for permission in permissions:
            _validate_permission(f"Role {role!r} grants", permission)
    for role, permission in policy.assign_permissions.items():
        if role not in policy.roles:
            raise ValueError(
                f"Assign permission {permission!r} names role {role!r}, which is not in "
                "AccessPolicy.roles"
            )
        _validate_permission(f"Role {role!r} has assign permission", permission)


def _validate_permission(subject: str, permission: str) -> None:
    """Raise ValueError when ``permission`` is a wildcard or an unknown ``lms.`` string."""
    if "*" in permission:
        raise ValueError(
            f"{subject} {permission!r}: wildcards are not supported, "
            "list each permission (e.g. frozenset(microlms.Permission) for every core one)"
        )
    if permission.startswith(CORE_NAMESPACE) and permission not in _CORE_PERMISSIONS:
        raise ValueError(
            f"{subject} unknown core permission {permission!r}: the "
            f"{CORE_NAMESPACE!r} namespace holds only microlms.Permission members"
        )


def permissions_for(
    platform_roles: Iterable[str],
    org_roles: Mapping[int, frozenset[str]],
    policy: AccessPolicy,
) -> GrantedPermissions:
    """The permissions conferred by a user's platform and org assignments.

    Roles absent from the policy grant nothing, so an unknown role never
    silently confers a permission.
    """
    return GrantedPermissions(
        platform=_grants_of(platform_roles, policy),
        orgs={org_id: _grants_of(roles, policy) for org_id, roles in org_roles.items()},
    )


def permissions_to_manage(
    platform_roles: Iterable[str],
    org_roles: Mapping[int, frozenset[str]],
    policy: AccessPolicy,
) -> GrantedPermissions:
    """The permissions an actor must hold, per scope, to act on an account
    with these platform and org assignments: every permission its roles
    grant there, plus the assign permission of each of those roles that has
    one. Roles absent from the policy require nothing, as they grant nothing.
    """
    return GrantedPermissions(
        platform=_requirements_of(platform_roles, policy),
        orgs={org_id: _requirements_of(roles, policy) for org_id, roles in org_roles.items()},
    )


def _grants_of(roles: Iterable[str], policy: AccessPolicy) -> frozenset[str]:
    return frozenset().union(*(policy.roles.get(role, frozenset()) for role in roles))


def _requirements_of(roles: Iterable[str], policy: AccessPolicy) -> frozenset[str]:
    """What assigning ``roles`` needs: their grants plus their assign permissions."""
    held = list(roles)
    assign = frozenset(
        policy.assign_permissions[role] for role in held if role in policy.assign_permissions
    )
    return _grants_of(held, policy) | assign
