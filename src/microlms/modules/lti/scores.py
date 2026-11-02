"""AGS score publishing (Assignment and Grade Services 2.0, section 3.4).

Each resource link owns one declarative line item. The tool publishes the
learner's latest score to it; the platform keeps the latest one per
enrollment, ignores updates older than the one on record (3.4.9), and
records the variant satisfied once the tool reports the activity
``Completed`` - completion tracks the learner's activity progress, not the
grading status, so a completed activity awaiting manual grading still counts.
"""

import enum
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from microlms.domain.activities import record_variant_satisfied
from microlms.domain.enrollments import find_enrollment
from microlms.modules.lti.links import link_course
from microlms.modules.lti.models import LtiLink, LtiResult


class ScoreActivityProgress(enum.StrEnum):
    """The learner's status towards completing the activity (AGS 3.4.7)."""

    INITIALIZED = "Initialized"
    STARTED = "Started"
    IN_PROGRESS = "InProgress"
    SUBMITTED = "Submitted"
    COMPLETED = "Completed"


class ScoreGradingProgress(enum.StrEnum):
    """The status of the grading process (AGS 3.4.8)."""

    FULLY_GRADED = "FullyGraded"
    PENDING = "Pending"
    PENDING_MANUAL = "PendingManual"
    FAILED = "Failed"
    NOT_READY = "NotReady"


class ScoreRecipientNotEnrolledError(Exception):
    """Raised when a score names a user not enrolled in the link's course."""


@dataclass(frozen=True, slots=True)
class ScoreUpdate:
    """One score the tool published for a learner."""

    user_id: uuid.UUID
    timestamp: datetime
    score_given: float | None
    score_maximum: float | None
    activity_progress: ScoreActivityProgress
    grading_progress: ScoreGradingProgress
    comment: str | None


def record_score(session: Session, link: LtiLink, score: ScoreUpdate) -> LtiResult | None:
    """Store the score as the enrollment's latest result for the link and
    return that result.

    Returns None, changing nothing, when the result on record carries a
    later timestamp (an out-of-order update). A ``Completed`` activity
    progress records the variant satisfied, which completes the still active
    enrollment once every activity of the course is (queuing
    ``CourseCompleted`` for the transaction owner's ``commit_with_events``).
    Raises ScoreRecipientNotEnrolledError when the user has no enrollment in
    the link's course.
    """
    course = link_course(session, link)
    enrollment = find_enrollment(session, score.user_id, course.id)
    if enrollment is None:
        raise ScoreRecipientNotEnrolledError(
            f"User {score.user_id} is not enrolled in course {course.id} of resource link "
            f"{link.resource_link_id}"
        )
    result = session.scalars(
        select(LtiResult).where(
            LtiResult.link_id == link.id, LtiResult.enrollment_id == enrollment.id
        )
    ).first()
    if result is not None and result.timestamp > score.timestamp:
        return None
    if result is None:
        result = LtiResult(link_id=link.id, enrollment_id=enrollment.id)
        session.add(result)
    result.score_given = score.score_given
    result.score_maximum = score.score_maximum
    result.activity_progress = score.activity_progress
    result.grading_progress = score.grading_progress
    result.comment = score.comment
    result.timestamp = score.timestamp.astimezone(UTC)
    session.flush()
    if score.activity_progress is ScoreActivityProgress.COMPLETED:
        record_variant_satisfied(session, enrollment.id, link.variant_id)
    return result
