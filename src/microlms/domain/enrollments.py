"""Enrollment model and services.

Completion queues the ``CourseCompleted`` event so client projects can react
(webhooks, HR sync, certificates, ...); the transaction owner dispatches it
via :func:`microlms.events.commit_with_events`.
"""

import enum
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    ColumnElement,
    DateTime,
    Enum,
    ForeignKey,
    UniqueConstraint,
    Uuid,
    distinct,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from microlms.db.base import Base
from microlms.domain.courses import CoursePrerequisite
from microlms.domain.users import OrgMembership, members_of
from microlms.events import CourseCompleted, EnrollmentDueSoon, UserEnrolled, queue_event

__all__ = [
    "AlreadyEnrolledError",
    "CoursePrerequisitesNotMetError",
    "Enrollment",
    "EnrollmentStatus",
    "enroll_user",
    "find_enrollment",
    "list_enrollments",
    "notify_due_soon",
]


class EnrollmentStatus(enum.StrEnum):
    """An enrollment is active until every activity of its course is
    satisfied, then completed."""

    ACTIVE = "active"
    COMPLETED = "completed"


class Enrollment(Base):
    """One user enrolled into one course."""

    __tablename__ = "lms_enrollment"
    __table_args__ = (UniqueConstraint("user_id", "course_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("lms_user.id", ondelete="CASCADE"))
    course_id: Mapped[int] = mapped_column(ForeignKey("lms_course.id"))
    status: Mapped[EnrollmentStatus] = mapped_column(
        Enum(EnrollmentStatus, name="enrollment_status")
    )
    # xAPI registration for this enrollment: content modules stamp it on every
    # statement they record, so an enrollment's LRS trail is queryable by the
    # standard `registration` statement filter
    registration: Mapped[uuid.UUID] = mapped_column(Uuid)
    enrolled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Deadline for completing the course, None when the enrollment has none
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the due-soon notification fired; stamps make each fire exactly once
    due_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EnrollmentNotFoundError(Exception):
    """Raised when no enrollment exists for the given identifier."""


class AlreadyEnrolledError(Exception):
    """Raised when enrolling a user into a course they are already enrolled in."""


class CoursePrerequisitesNotMetError(Exception):
    """Raised when enrolling into a course whose prerequisites the user has
    not completed."""


def get_enrollment(session: Session, enrollment_id: int) -> Enrollment:
    enrollment = session.get(Enrollment, enrollment_id)
    if enrollment is None:
        raise EnrollmentNotFoundError(f"No enrollment with id {enrollment_id}")
    return enrollment


def find_enrollment(session: Session, user_id: uuid.UUID, course_id: int) -> Enrollment | None:
    """The user's enrollment in the course, None when not enrolled."""
    return session.scalars(
        select(Enrollment).where(Enrollment.user_id == user_id, Enrollment.course_id == course_id)
    ).first()


def unmet_prerequisite_course_ids(
    session: Session, user_id: uuid.UUID, course_id: int
) -> frozenset[int]:
    """The course's prerequisite courses the user has not completed."""
    required = frozenset(
        session.scalars(
            select(CoursePrerequisite.prerequisite_course_id).where(
                CoursePrerequisite.course_id == course_id
            )
        )
    )
    if not required:
        return frozenset()
    completed = frozenset(
        session.scalars(
            select(Enrollment.course_id).where(
                Enrollment.user_id == user_id,
                Enrollment.status == EnrollmentStatus.COMPLETED,
                Enrollment.course_id.in_(required),
            )
        )
    )
    return required - completed


def list_enrollments(
    session: Session,
    user_id: uuid.UUID | None,
    course_id: int | None,
    status: EnrollmentStatus | None,
    org_ids: frozenset[int] | None,
) -> list[Enrollment]:
    """Enrollments matching every given filter; None leaves a filter open.

    ``org_ids`` keeps enrollees belonging to any of those organizations.
    """
    query = select(Enrollment).order_by(Enrollment.id)
    if user_id is not None:
        query = query.where(Enrollment.user_id == user_id)
    if course_id is not None:
        query = query.where(Enrollment.course_id == course_id)
    if status is not None:
        query = query.where(Enrollment.status == status)
    if org_ids is not None:
        query = query.where(Enrollment.user_id.in_(members_of(org_ids)))
    return list(session.scalars(query))


@dataclass(frozen=True)
class EnrollmentCounts:
    """Enrollments by status; ``overdue`` are the active ones past their due date."""

    active: int
    completed: int
    overdue: int


def _status_counts(counted: ColumnElement[object], now: datetime) -> tuple[ColumnElement[int], ...]:
    """The active, completed and overdue counts of ``counted`` (the enrollment
    id, or its distinct values when a join repeats enrollments), for a select
    over enrollments; ``now`` decides what is overdue."""
    return (
        func.count(counted).filter(Enrollment.status == EnrollmentStatus.ACTIVE),
        func.count(counted).filter(Enrollment.status == EnrollmentStatus.COMPLETED),
        func.count(counted).filter(
            Enrollment.status == EnrollmentStatus.ACTIVE,
            Enrollment.due_at.is_not(None),
            Enrollment.due_at < now,
        ),
    )


def count_enrollments(
    session: Session,
    user_id: uuid.UUID | None,
    course_id: int | None,
    org_ids: frozenset[int] | None,
    now: datetime,
) -> EnrollmentCounts:
    """Count the enrollments matching every given filter, as
    :func:`list_enrollments` filters them; ``now`` decides what is overdue."""
    query = select(*_status_counts(Enrollment.id, now))
    if user_id is not None:
        query = query.where(Enrollment.user_id == user_id)
    if course_id is not None:
        query = query.where(Enrollment.course_id == course_id)
    if org_ids is not None:
        query = query.where(Enrollment.user_id.in_(members_of(org_ids)))
    active, completed, overdue = session.execute(query).one()
    return EnrollmentCounts(active=active, completed=completed, overdue=overdue)


def count_enrollments_by_org(
    session: Session, org_ids: frozenset[int] | None, now: datetime
) -> Mapping[int, EnrollmentCounts]:
    """Count the enrollments of each organization's members, as
    :func:`count_enrollments` counts them with ``org_ids`` set to that one
    organization; ``now`` decides what is overdue. With ``org_ids`` set, only
    those organizations are counted; organizations without an enrolled member
    are left out.
    """
    # A member holding several roles in an organization has one membership
    # row per role, so each enrollment is counted once per organization
    query = (
        select(OrgMembership.org_id, *_status_counts(distinct(Enrollment.id), now))
        .join(OrgMembership, OrgMembership.user_id == Enrollment.user_id)
        .group_by(OrgMembership.org_id)
    )
    if org_ids is not None:
        query = query.where(OrgMembership.org_id.in_(list(org_ids)))
    return {
        org_id: EnrollmentCounts(active=active, completed=completed, overdue=overdue)
        for org_id, active, completed, overdue in session.execute(query).tuples()
    }


def count_enrollments_by_course(
    session: Session, org_ids: frozenset[int] | None, now: datetime
) -> Mapping[int, EnrollmentCounts]:
    """Count the enrollments of each course, as :func:`count_enrollments`
    counts them with ``course_id`` set to that course and the same ``org_ids``;
    ``now`` decides what is overdue. Courses without an enrollment are left out.
    """
    query = select(Enrollment.course_id, *_status_counts(Enrollment.id, now)).group_by(
        Enrollment.course_id
    )
    if org_ids is not None:
        query = query.where(Enrollment.user_id.in_(members_of(org_ids)))
    return {
        course_id: EnrollmentCounts(active=active, completed=completed, overdue=overdue)
        for course_id, active, completed, overdue in session.execute(query).tuples()
    }


def enroll_user(
    session: Session,
    user_id: uuid.UUID,
    course_id: int,
    due_at: datetime | None,
) -> Enrollment:
    """Enroll a user into a course.

    Raises AlreadyEnrolledError when the user already holds an enrollment in
    the course, and CoursePrerequisitesNotMetError when the course requires
    other courses the user has not completed. Prerequisites gate only this
    moment: an enrollment already held stays valid whatever edges are added
    later.
    """
    if find_enrollment(session, user_id, course_id) is not None:
        raise AlreadyEnrolledError(f"User {user_id} is already enrolled in course {course_id}")
    missing = unmet_prerequisite_course_ids(session, user_id, course_id)
    if missing:
        raise CoursePrerequisitesNotMetError(
            f"Course {course_id} requires completing course(s) {sorted(missing)} first"
        )
    enrollment = Enrollment(
        user_id=user_id,
        course_id=course_id,
        status=EnrollmentStatus.ACTIVE,
        registration=uuid.uuid4(),
        due_at=due_at,
    )
    session.add(enrollment)
    session.flush()
    queue_event(
        session,
        UserEnrolled(
            user_id=user_id,
            course_id=course_id,
            enrolled_at=enrollment.enrolled_at,
            due_at=due_at,
        ),
    )
    return enrollment


def notify_due_soon(session: Session, within: timedelta) -> int:
    """Queue ``EnrollmentDueSoon`` for active enrollments due within the window.

    Overdue enrollments count as due. Each enrollment fires exactly once: the
    notification is stamped in ``due_notified_at`` in the same transaction as
    the queued events, so the caller's ``commit_with_events`` makes stamp and
    event atomic. Returns how many were queued.
    """
    now = datetime.now(UTC)
    due = list(
        session.scalars(
            select(Enrollment)
            .where(
                Enrollment.status == EnrollmentStatus.ACTIVE,
                Enrollment.due_at.is_not(None),
                Enrollment.due_at <= now + within,
                Enrollment.due_notified_at.is_(None),
            )
            .order_by(Enrollment.id)
        )
    )
    for enrollment in due:
        enrollment.due_notified_at = now
    session.flush()
    for enrollment in due:
        queue_event(
            session,
            EnrollmentDueSoon(
                enrollment_id=enrollment.id,
                user_id=enrollment.user_id,
                course_id=enrollment.course_id,
                due_at=enrollment.due_at,
            ),
        )
    return len(due)


def complete_enrollment(session: Session, enrollment_id: int) -> Enrollment:
    enrollment = get_enrollment(session, enrollment_id)
    completed_at = datetime.now(UTC)
    enrollment.status = EnrollmentStatus.COMPLETED
    enrollment.completed_at = completed_at
    session.flush()
    queue_event(
        session,
        CourseCompleted(
            user_id=enrollment.user_id,
            course_id=enrollment.course_id,
            completed_at=completed_at,
        ),
    )
    return enrollment
