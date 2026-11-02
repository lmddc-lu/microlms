"""Enrollment endpoints; completion triggers client event hooks."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from microlms.api.deps import (
    AuditDep,
    HooksDep,
    ManagementScope,
    SessionDep,
    get_user_in_scope,
    require_permission,
    require_platform_permission,
    require_scoped_permission,
    resolve_org_filter,
)
from microlms.auth.base import AuthenticatedUser
from microlms.authz import Permission
from microlms.domain.activities import enrollment_progress
from microlms.domain.courses import (
    CourseNotFoundError,
    CourseStatus,
    course_visible_to,
    get_course,
)
from microlms.domain.enrollments import (
    AlreadyEnrolledError,
    CoursePrerequisitesNotMetError,
    Enrollment,
    EnrollmentCounts,
    EnrollmentNotFoundError,
    EnrollmentStatus,
    complete_enrollment,
    count_enrollments,
    count_enrollments_by_course,
    count_enrollments_by_org,
    enroll_user,
    get_enrollment,
    list_enrollments,
    notify_due_soon,
)
from microlms.events import commit_with_events

router = APIRouter(prefix="/enrollments", tags=["enrollments"])


class EnrollmentCreate(BaseModel):
    user_id: uuid.UUID
    course_id: int
    # Optional completion deadline; drives the due-soon notification event
    due_at: datetime | None = None


class EnrollmentRead(BaseModel):
    id: int
    user_id: uuid.UUID
    course_id: int
    status: EnrollmentStatus
    # The enrollment's xAPI registration, stamped on its LRS statements
    registration: uuid.UUID
    enrolled_at: datetime
    completed_at: datetime | None
    due_at: datetime | None
    # How far the learner got in the course, 0 to 1: the mean over its
    # activities of 1 when satisfied, else the progress measure the content
    # reported, else 0; 1 once the enrollment is completed
    progress: float


def _enrollments_read(session: Session, enrollments: Sequence[Enrollment]) -> list[EnrollmentRead]:
    """Build the read models of enrollments together with their course progress."""
    progress = enrollment_progress(session, enrollments)
    return [
        EnrollmentRead(
            id=enrollment.id,
            user_id=enrollment.user_id,
            course_id=enrollment.course_id,
            status=enrollment.status,
            registration=enrollment.registration,
            enrolled_at=enrollment.enrolled_at,
            completed_at=enrollment.completed_at,
            due_at=enrollment.due_at,
            progress=progress[enrollment.id],
        )
        for enrollment in enrollments
    ]


class EnrollmentCountsRead(BaseModel):
    """Enrollments by status; ``overdue`` are the active ones past their due date."""

    total: int
    active: int
    completed: int
    overdue: int


class OrgEnrollmentCountsRead(EnrollmentCountsRead):
    """The enrollment counts of one organization's members."""

    org_id: int


class CourseEnrollmentCountsRead(EnrollmentCountsRead):
    """The enrollment counts of one course."""

    course_id: int


def _counts_read(counts: EnrollmentCounts) -> EnrollmentCountsRead:
    """The response form of enrollment counts, with their total."""
    return EnrollmentCountsRead(
        total=counts.active + counts.completed,
        active=counts.active,
        completed=counts.completed,
        overdue=counts.overdue,
    )


class DueSoonResult(BaseModel):
    """How many enrollments entered the notification window and fired."""

    notified: int


@router.get("/me")
def read_own_enrollments(
    session: SessionDep,
    current_user: Annotated[
        AuthenticatedUser, Depends(require_permission(Permission.CONTENT_ACCESS))
    ],
    status: EnrollmentStatus | None = None,
) -> list[EnrollmentRead]:
    """The current user's own enrollments; content access required because
    enrollments expose course ids."""
    found = list_enrollments(
        session, user_id=current_user.id, course_id=None, status=status, org_ids=None
    )
    return _enrollments_read(session, found)


@router.get("")
def read_enrollments(
    session: SessionDep,
    scope: Annotated[
        ManagementScope, Depends(require_scoped_permission(Permission.ENROLLMENT_READ))
    ],
    user_id: uuid.UUID | None = None,
    course_id: int | None = None,
    status: EnrollmentStatus | None = None,
    org_id: int | None = None,
) -> list[EnrollmentRead]:
    """List enrollments; managers only for members of their organizations."""
    effective_org = resolve_org_filter(scope, org_id)
    if user_id is not None:
        get_user_in_scope(session, scope, user_id)
    found = list_enrollments(
        session, user_id=user_id, course_id=course_id, status=status, org_ids=effective_org
    )
    return _enrollments_read(session, found)


@router.get("/counts")
def read_enrollment_counts(
    session: SessionDep,
    scope: Annotated[
        ManagementScope, Depends(require_scoped_permission(Permission.ENROLLMENT_READ))
    ],
    user_id: uuid.UUID | None = None,
    course_id: int | None = None,
    org_id: int | None = None,
) -> EnrollmentCountsRead:
    """Count the enrollments ``GET /enrollments`` lists with the same filters,
    by status; managers only for members of their organizations."""
    effective_org = resolve_org_filter(scope, org_id)
    if user_id is not None:
        get_user_in_scope(session, scope, user_id)
    counts = count_enrollments(
        session,
        user_id=user_id,
        course_id=course_id,
        org_ids=effective_org,
        now=datetime.now(UTC),
    )
    return _counts_read(counts)


@router.get("/counts/by-org")
def read_enrollment_counts_by_org(
    session: SessionDep,
    scope: Annotated[
        ManagementScope, Depends(require_scoped_permission(Permission.ENROLLMENT_READ))
    ],
) -> list[OrgEnrollmentCountsRead]:
    """Count, for every organization in reach, the enrollments
    ``GET /enrollments/counts`` counts with ``org_id`` set to it, so a dashboard
    needs one request rather than one per organization. Ordered by
    organization id; organizations without an enrolled member are left out."""
    by_org = count_enrollments_by_org(
        session, org_ids=resolve_org_filter(scope, None), now=datetime.now(UTC)
    )
    return [
        OrgEnrollmentCountsRead(org_id=org_id, **_counts_read(counts).model_dump())
        for org_id, counts in sorted(by_org.items())
    ]


@router.get("/counts/by-course")
def read_enrollment_counts_by_course(
    session: SessionDep,
    scope: Annotated[
        ManagementScope, Depends(require_scoped_permission(Permission.ENROLLMENT_READ))
    ],
    org_id: int | None = None,
) -> list[CourseEnrollmentCountsRead]:
    """Count, for every course with enrollments, the enrollments
    ``GET /enrollments/counts`` counts with ``course_id`` set to it and the same
    ``org_id``, so a course list needs one request rather than one per course;
    managers only for members of their organizations. Ordered by course id;
    courses without an enrollment are left out."""
    by_course = count_enrollments_by_course(
        session, org_ids=resolve_org_filter(scope, org_id), now=datetime.now(UTC)
    )
    return [
        CourseEnrollmentCountsRead(course_id=course_id, **_counts_read(counts).model_dump())
        for course_id, counts in sorted(by_course.items())
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
def add_enrollment(
    session: SessionDep,
    scope: Annotated[
        ManagementScope, Depends(require_scoped_permission(Permission.ENROLLMENT_CREATE))
    ],
    audit: AuditDep,
    payload: EnrollmentCreate,
) -> EnrollmentRead:
    """Enroll a user; managers only for members of their organizations, into
    courses visible to an organization the manager and the user share.

    Only active users can be enrolled, and only into published courses whose
    prerequisites the user has completed.
    """
    target = get_user_in_scope(session, scope, payload.user_id)
    if not target.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User is deactivated and cannot be enrolled",
        )
    try:
        course = get_course(session, payload.course_id)
    except CourseNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    if course.status != CourseStatus.PUBLISHED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Course is not published and cannot be enrolled into",
        )
    if scope.org_ids is not None and not course_visible_to(course, target.org_ids & scope.org_ids):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Course is not visible to an organization you share with the user",
        )
    try:
        enrollment = enroll_user(
            session, user_id=target.id, course_id=course.id, due_at=payload.due_at
        )
    except (AlreadyEnrolledError, CoursePrerequisitesNotMetError) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    audit.record(
        "enrollment.created",
        "enrollment",
        str(enrollment.id),
        {"user_id": str(target.id), "course_id": course.id},
    )
    return _enrollments_read(session, [enrollment])[0]


@router.post(
    "/notify-due-soon",
    dependencies=[Depends(require_platform_permission(Permission.ENROLLMENT_NOTIFY))],
)
def notify_due_enrollments(session: SessionDep, hooks: HooksDep, within_days: int) -> DueSoonResult:
    """Fire the due-soon event for enrollments due within the given window.

    Client projects call this on their own schedule (e.g. a daily cron) and
    turn the emitted events into reminders. Each enrollment fires once.
    """
    notified = notify_due_soon(session, timedelta(days=within_days))
    commit_with_events(session, hooks)
    return DueSoonResult(notified=notified)


@router.post("/{enrollment_id}/complete")
def mark_completed(
    session: SessionDep,
    scope: Annotated[
        ManagementScope, Depends(require_scoped_permission(Permission.ENROLLMENT_COMPLETE))
    ],
    audit: AuditDep,
    enrollment_id: int,
) -> EnrollmentRead:
    """Manual completion override; content modules drive real completion.

    Org-scoped actors complete only enrollments of their organizations' members.
    """
    try:
        enrollment = get_enrollment(session, enrollment_id)
    except EnrollmentNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    get_user_in_scope(session, scope, enrollment.user_id)
    completed = complete_enrollment(session, enrollment_id)
    audit.record("enrollment.completed", "enrollment", str(enrollment_id), None)
    return _enrollments_read(session, [completed])[0]
