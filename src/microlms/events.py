"""Typed domain events, the client hooks surface, and post-commit dispatch.

Domain services queue events on their session (:func:`queue_event`); the owner
of the transaction - the core routers' audit recorder, or any code composing
domain services - commits and dispatches them together with
:func:`commit_with_events`. Events therefore fire only for work that really
committed, and several services composed into one transaction dispatch all
their events after the single commit.

A client project implements :class:`EventHooks` (e.g. to POST to a webhook or
sync an HR tool) and passes it to ``create_app``. Hooks run after the commit,
outside the transaction that queued the event: a hook that uses the database
opens its own sessions from ``microlms.create_session_factory`` over the
engine passed to ``create_app``, so it shares the app's connection pool.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session

__all__ = [
    "CourseCompleted",
    "CourseOrgAssigned",
    "CourseOrgUnassigned",
    "CoursePublished",
    "EnrollmentDueSoon",
    "EventHooks",
    "NullEventHooks",
    "PasswordResetRequested",
    "RoleGranted",
    "RoleRevoked",
    "ScormRuntimeCommitted",
    "SessionEnded",
    "UserCreated",
    "UserEnrolled",
    "commit_with_events",
]


@dataclass(frozen=True, slots=True)
class CourseCompleted:
    """Emitted when a learner completes a course enrollment."""

    user_id: uuid.UUID
    course_id: int
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class UserEnrolled:
    """Emitted when a learner is enrolled in a course."""

    user_id: uuid.UUID
    course_id: int
    enrolled_at: datetime
    # Deadline set at enrollment time, None when the enrollment has none
    due_at: datetime | None


@dataclass(frozen=True, slots=True)
class EnrollmentDueSoon:
    """Emitted once per enrollment when its due date enters the notification
    window (client projects turn it into a reminder)."""

    enrollment_id: int
    user_id: uuid.UUID
    course_id: int
    due_at: datetime


@dataclass(frozen=True, slots=True)
class UserCreated:
    """Emitted when an account is created through management.

    Client projects typically turn it into an invitation email telling the
    new user how to activate the account (e.g. a set-your-password link),
    sent to ``email`` when the account has one. Self-registration and
    externally provisioned ghost identities do not emit it: those users need
    no invitation.
    """

    user_id: uuid.UUID
    full_name: str
    # None when the account was created without an email
    email: str | None
    # Org roles held in ``org_id``, or platform roles when ``org_id`` is None
    roles: tuple[str, ...]
    org_id: int | None
    # The user's preferred language (BCP 47), None when none was chosen
    lang: str | None


@dataclass(frozen=True, slots=True)
class RoleGranted:
    """Emitted when an existing account gains a role it did not hold.

    Roles given at account creation are carried by :class:`UserCreated`
    instead, so a client reacting to roles (e.g. enrolling by role) handles
    both events. Re-granting a role the user already holds emits nothing.
    """

    user_id: uuid.UUID
    role: str
    # Organization the org role is held in; None for a platform role
    org_id: int | None


@dataclass(frozen=True, slots=True)
class RoleRevoked:
    """Emitted for each role an account loses: a single revocation, or every
    org role held in an organization the user is removed from.

    Deleting an account emits nothing: its roles go with it.
    """

    user_id: uuid.UUID
    role: str
    # Organization the org role was held in; None for a platform role
    org_id: int | None


@dataclass(frozen=True, slots=True)
class SessionEnded:
    """Emitted when an LMS session is ended before its time limit: a logout,
    a revoked refresh token, a refresh token reuse, a new password or a
    deactivated account (one event per session ended). A session reaching
    its time limit ends without an event.

    ``session_id`` is the ``AuthenticatedUser.session_id`` the session's
    requests carried, so a client that recorded work against the session
    (e.g. sign-ins it relayed to other sites) can undo it.
    """

    user_id: uuid.UUID
    session_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class PasswordResetRequested:
    """Emitted when a user requests a self-service password reset.

    The core never sends mail: the client project delivers the token to the
    user out-of-band (typically an emailed reset link) and the user consumes
    it at ``POST /auth/reset-password``.
    """

    user_id: uuid.UUID
    email: str
    full_name: str
    # The user's preferred language (BCP 47), None when none was chosen
    lang: str | None
    token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CoursePublished:
    """Emitted when a course becomes published: from draft, or un-archived.

    Publishing an already published course emits nothing.
    """

    course_id: int


@dataclass(frozen=True, slots=True)
class CourseOrgAssigned:
    """Emitted when a course is assigned to an organization it was not.

    Creating an org-owned course emits it for the owning organization, while
    the course is still a draft; :class:`CoursePublished` follows when the
    course goes live.
    """

    course_id: int
    org_id: int


@dataclass(frozen=True, slots=True)
class CourseOrgUnassigned:
    """Emitted when a course loses an organization assignment.

    Removing the last assignment makes the course global, visible from
    every organization, so this event can widen visibility as well.
    """

    course_id: int
    org_id: int


@dataclass(frozen=True, slots=True)
class ScormRuntimeCommitted:
    """Emitted after every SCORM runtime commit (Commit and Terminate) once
    the core has recorded it.

    Carries the SCO's ``cmi.suspend_data``, which the core stores but never
    interprets, so a client can derive what an authoring tool keeps only
    there (e.g. its own progress) and record it through the domain services,
    such as ``record_activity_progress_measure``. The package's storage
    prefix lets the client read the package files it needs for that (e.g.
    the course structure) through the storage backend it passed to
    ``create_app``.
    """

    enrollment_id: int
    activity_id: int
    sco_id: int
    # None when the SCO never wrote cmi.suspend_data
    suspend_data: str | None
    # Storage key prefix of the package root: a package file at ``path``
    # (relative to the zip root) is stored under ``f"{prefix}/{path}"``
    package_storage_prefix: str


class EventHooks(Protocol):
    """Callbacks invoked on domain events; the surface client projects implement.

    Subclass :class:`NullEventHooks` rather than implementing this protocol
    from scratch: override the events you handle, and events added in future
    core versions fall through to its no-ops instead of crashing dispatch
    with a missing method.
    """

    def on_user_created(self, event: UserCreated) -> None: ...

    def on_user_enrolled(self, event: UserEnrolled) -> None: ...

    def on_course_completed(self, event: CourseCompleted) -> None: ...

    def on_enrollment_due_soon(self, event: EnrollmentDueSoon) -> None: ...

    def on_password_reset_requested(self, event: PasswordResetRequested) -> None: ...

    def on_role_granted(self, event: RoleGranted) -> None: ...

    def on_role_revoked(self, event: RoleRevoked) -> None: ...

    def on_course_published(self, event: CoursePublished) -> None: ...

    def on_course_org_assigned(self, event: CourseOrgAssigned) -> None: ...

    def on_course_org_unassigned(self, event: CourseOrgUnassigned) -> None: ...

    def on_scorm_runtime_committed(self, event: ScormRuntimeCommitted) -> None: ...

    def on_session_ended(self, event: SessionEnded) -> None: ...


DomainEvent = (
    UserCreated
    | UserEnrolled
    | CourseCompleted
    | EnrollmentDueSoon
    | PasswordResetRequested
    | RoleGranted
    | RoleRevoked
    | CoursePublished
    | CourseOrgAssigned
    | CourseOrgUnassigned
    | ScormRuntimeCommitted
    | SessionEnded
)

# Session.info key holding the events queued by domain services since the
# last dispatch
_PENDING_EVENTS_KEY = "microlms_pending_events"


class UnknownEventTypeError(Exception):
    """Raised when a queued event has no matching EventHooks method."""


def queue_event(session: Session, event: DomainEvent) -> None:
    """Queue a domain event for dispatch after the session's next commit.

    Domain services call this instead of invoking hooks directly, so an event
    only ever reaches the client once the transaction that produced it is
    committed (see :func:`commit_with_events`). A rolled-back session simply
    discards its queue with the rest of its state when it closes.
    """
    pending: list[DomainEvent] = session.info.setdefault(_PENDING_EVENTS_KEY, [])
    pending.append(event)


def _dispatch(hooks: EventHooks, event: DomainEvent) -> None:
    match event:
        case UserCreated():
            hooks.on_user_created(event)
        case UserEnrolled():
            hooks.on_user_enrolled(event)
        case CourseCompleted():
            hooks.on_course_completed(event)
        case EnrollmentDueSoon():
            hooks.on_enrollment_due_soon(event)
        case PasswordResetRequested():
            hooks.on_password_reset_requested(event)
        case RoleGranted():
            hooks.on_role_granted(event)
        case RoleRevoked():
            hooks.on_role_revoked(event)
        case CoursePublished():
            hooks.on_course_published(event)
        case CourseOrgAssigned():
            hooks.on_course_org_assigned(event)
        case CourseOrgUnassigned():
            hooks.on_course_org_unassigned(event)
        case ScormRuntimeCommitted():
            hooks.on_scorm_runtime_committed(event)
        case SessionEnded():
            hooks.on_session_ended(event)
        case _:
            raise UnknownEventTypeError(f"No hook dispatch for event {event!r}")


def commit_with_events(session: Session, hooks: EventHooks) -> None:
    """Commit the transaction, then dispatch the events it queued, in order.

    The single supported way to finish a transaction that ran domain services:
    a failing commit propagates without dispatching anything, so the client
    never observes an event for work that did not happen.
    """
    session.commit()
    pending: list[DomainEvent] = session.info.pop(_PENDING_EVENTS_KEY, [])
    for event in pending:
        _dispatch(hooks, event)


class NullEventHooks:
    """Hooks implementation that ignores every event.

    Doubles as the recommended base class for client hooks (see
    :class:`EventHooks`): pass it as-is to ignore all events, or subclass it
    and override the ones the project reacts to.
    """

    def on_user_created(self, event: UserCreated) -> None:
        return None

    def on_user_enrolled(self, event: UserEnrolled) -> None:
        return None

    def on_course_completed(self, event: CourseCompleted) -> None:
        return None

    def on_enrollment_due_soon(self, event: EnrollmentDueSoon) -> None:
        return None

    def on_password_reset_requested(self, event: PasswordResetRequested) -> None:
        return None

    def on_role_granted(self, event: RoleGranted) -> None:
        return None

    def on_role_revoked(self, event: RoleRevoked) -> None:
        return None

    def on_course_published(self, event: CoursePublished) -> None:
        return None

    def on_course_org_assigned(self, event: CourseOrgAssigned) -> None:
        return None

    def on_course_org_unassigned(self, event: CourseOrgUnassigned) -> None:
        return None

    def on_scorm_runtime_committed(self, event: ScormRuntimeCommitted) -> None:
        return None

    def on_session_ended(self, event: SessionEnded) -> None:
        return None
