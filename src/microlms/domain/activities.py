"""Course activities, their language variants, and per-enrollment progress.

A course is an ordered list of activities; each activity carries one content
package per language (``lms_activity_variant``). Content modules attach their
imported packages to a variant and report satisfaction per variant; an
activity is satisfied once any of its language variants is, and the
enrollment completes once every activity of the course is satisfied.
An activity has a reference ``title`` plus optional per-language display
titles (``lms_activity_translation``) that clients pick from.
Content modules also record each launch, so clients can tell which
activities a learner has started and which one they opened last, and the
progress measure the content reports, so clients can tell how far a learner
got in an activity and in the course.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    delete,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from microlms.db.base import Base
from microlms.domain.courses import get_course
from microlms.domain.enrollments import (
    Enrollment,
    EnrollmentStatus,
    complete_enrollment,
    get_enrollment,
)
from microlms.domain.languages import ensure_language_tag

__all__ = [
    "Activity",
    "ActivityLaunch",
    "ActivityNotLaunchedError",
    "ActivityTranslation",
    "ActivityVariant",
    "InvalidProgressMeasureError",
    "activity_progress_measures",
    "enrollment_progress",
    "last_launch_times",
    "list_course_activities",
    "record_activity_progress_measure",
    "satisfied_activity_ids",
]


class Activity(Base):
    """One content unit of a course, in course order."""

    __tablename__ = "lms_activity"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("lms_course.id"))
    position: Mapped[int] = mapped_column(Integer())
    title: Mapped[str] = mapped_column(String(500))
    # selectin so variants load with the activity and never lazy-load after
    # the request session is closed
    variants: Mapped[list["ActivityVariant"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", order_by="ActivityVariant.lang"
    )
    # selectin for the same reason as the variants
    translations: Mapped[list["ActivityTranslation"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", order_by="ActivityTranslation.lang"
    )


class ActivityTranslation(Base):
    """The title of an activity in one language."""

    __tablename__ = "lms_activity_translation"

    activity_id: Mapped[int] = mapped_column(ForeignKey("lms_activity.id"), primary_key=True)
    lang: Mapped[str] = mapped_column(String(35), primary_key=True)
    title: Mapped[str] = mapped_column(String(500))


class ActivityVariant(Base):
    """One language edition of an activity, backed by one content package.

    ``module`` names the content module that owns the package ("scorm",
    "cmi5", ...) so clients know which launch surface applies.
    """

    __tablename__ = "lms_activity_variant"
    __table_args__ = (UniqueConstraint("activity_id", "lang"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    activity_id: Mapped[int] = mapped_column(ForeignKey("lms_activity.id"))
    lang: Mapped[str] = mapped_column(String(35))
    module: Mapped[str] = mapped_column(String(50))


class ActivityProgress(Base):
    """An activity satisfied by one enrollment, via one of its variants."""

    __tablename__ = "lms_activity_progress"
    __table_args__ = (UniqueConstraint("enrollment_id", "activity_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("lms_enrollment.id", ondelete="CASCADE"))
    activity_id: Mapped[int] = mapped_column(ForeignKey("lms_activity.id"))
    variant_id: Mapped[int] = mapped_column(ForeignKey("lms_activity_variant.id"))
    satisfied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ActivityLaunch(Base):
    """When one enrollment last launched an activity, via any of its variants,
    and how far it has got in it."""

    __tablename__ = "lms_activity_launch"
    __table_args__ = (UniqueConstraint("enrollment_id", "activity_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("lms_enrollment.id", ondelete="CASCADE"))
    activity_id: Mapped[int] = mapped_column(ForeignKey("lms_activity.id"))
    last_launched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Fraction of the activity the learner went through, 0 to 1, as last
    # reported by its content; None until the content reports one
    progress_measure: Mapped[float | None] = mapped_column(Float())


class ActivityNotFoundError(Exception):
    """Raised when no activity exists for the given identifier."""


class ActivityVariantNotFoundError(Exception):
    """Raised when no activity variant exists for the given identifier."""


class ActivityVariantModuleError(Exception):
    """Raised when a content module acts on a variant another module plays."""


class ActivityTranslationNotFoundError(Exception):
    """Raised when removing a translation the activity does not have."""


class ActivityLanguageExistsError(Exception):
    """Raised when the activity already has a variant for the language."""


class ActivityNotLaunchedError(Exception):
    """Raised when recording progress on an activity the enrollment never launched."""


class InvalidProgressMeasureError(Exception):
    """Raised when a progress measure falls outside 0..1."""


def get_activity(session: Session, activity_id: int) -> Activity:
    activity = session.get(Activity, activity_id)
    if activity is None:
        raise ActivityNotFoundError(f"No activity with id {activity_id}")
    return activity


def get_activity_variant(session: Session, variant_id: int) -> ActivityVariant:
    variant = session.get(ActivityVariant, variant_id)
    if variant is None:
        raise ActivityVariantNotFoundError(f"No activity variant with id {variant_id}")
    return variant


def get_module_variant(session: Session, variant_id: int, module: str) -> ActivityVariant:
    """The variant, provided ``module`` plays it; raises
    ActivityVariantModuleError when another content module does."""
    variant = get_activity_variant(session, variant_id)
    if variant.module != module:
        raise ActivityVariantModuleError(
            f"Variant {variant_id} is played by module {variant.module!r}, not {module!r}"
        )
    return variant


def list_course_activities(session: Session, course_id: int) -> list[Activity]:
    """The activities of a course in course order; 404s on an unknown course."""
    get_course(session, course_id)
    return list(
        session.scalars(
            select(Activity)
            .where(Activity.course_id == course_id)
            .order_by(Activity.position, Activity.id)
        )
    )


def create_activity(session: Session, course_id: int, title: str) -> Activity:
    """Append an activity to a course; content comes from variant imports."""
    course = get_course(session, course_id)
    next_position = session.scalar(
        select(func.count()).select_from(Activity).where(Activity.course_id == course.id)
    )
    activity = Activity(course_id=course.id, position=next_position, title=title)
    session.add(activity)
    session.flush()
    return activity


def rename_activity(session: Session, activity_id: int, title: str) -> Activity:
    """Change an activity's title; 404s on an unknown activity."""
    activity = get_activity(session, activity_id)
    activity.title = title
    session.flush()
    return activity


def move_activity(session: Session, activity_id: int, new_position: int) -> list[Activity]:
    """Move an activity to a new 0-based position within its course, reindexing
    its siblings to keep positions contiguous and in course order. A position
    outside the range is clamped to the nearest end. Returns the activities of
    the course in their new order."""
    activity = get_activity(session, activity_id)
    ordered = list_course_activities(session, activity.course_id)
    ordered.remove(activity)
    target = max(0, min(new_position, len(ordered)))
    ordered.insert(target, activity)
    for index, item in enumerate(ordered):
        item.position = index
    session.flush()
    return list_course_activities(session, activity.course_id)


# Tears down the content module's data for one variant (rows and stored files),
# within the caller's transaction. The delete services below call it for each
# variant before its row is removed, so no foreign key is left dangling.
VariantContentPurge = Callable[[Session, "ActivityVariant"], None]


def delete_activity_variant(
    session: Session, activity_id: int, variant_id: int, purge: VariantContentPurge
) -> None:
    """Delete one language variant of an activity: its content module data and
    stored files (via ``purge``), the progress recorded through it, then the
    variant row. 404s unless the variant belongs to the activity."""
    variant = get_activity_variant(session, variant_id)
    if variant.activity_id != activity_id:
        raise ActivityVariantNotFoundError(
            f"Variant {variant_id} does not belong to activity {activity_id}"
        )
    purge(session, variant)
    session.execute(delete(ActivityProgress).where(ActivityProgress.variant_id == variant_id))
    session.delete(variant)
    session.flush()


def delete_activity(session: Session, activity_id: int, purge: VariantContentPurge) -> None:
    """Delete an activity, every language variant it holds (content module data
    and stored files via ``purge``) and the progress and launches recorded
    against it, then reindex the course's remaining activities to keep
    positions contiguous.

    Enrollment completion is not recomputed: an enrollment already completed
    stays completed even if a still-unsatisfied activity is removed."""
    activity = get_activity(session, activity_id)
    course_id = activity.course_id
    for variant in list(activity.variants):
        purge(session, variant)
    session.execute(delete(ActivityProgress).where(ActivityProgress.activity_id == activity_id))
    session.execute(delete(ActivityLaunch).where(ActivityLaunch.activity_id == activity_id))
    session.delete(activity)
    session.flush()
    remaining = list_course_activities(session, course_id)
    for index, item in enumerate(remaining):
        item.position = index
    session.flush()


def add_activity_variant(
    session: Session, activity_id: int, lang: str, module: str
) -> ActivityVariant:
    """Add a language variant to an activity.

    Raises ActivityLanguageExistsError when the activity already has a
    variant for the language, and InvalidLanguageTagError on a malformed tag.
    """
    activity = get_activity(session, activity_id)
    ensure_language_tag(lang)
    if any(existing.lang == lang for existing in activity.variants):
        raise ActivityLanguageExistsError(
            f"Activity {activity_id} already has a variant for language {lang!r}"
        )
    variant = ActivityVariant(activity_id=activity.id, lang=lang, module=module)
    session.add(variant)
    session.flush()
    return variant


def locked_activity_ids(
    session: Session, course_id: int, satisfied_ids: frozenset[int]
) -> frozenset[int]:
    """The course's activities not launchable given the satisfied set.

    In a sequential course an activity is locked while any activity before it
    (by position) is unsatisfied; the first unsatisfied activity itself stays
    launchable, and so do satisfied ones before it (review). A non-sequential
    course locks nothing.
    """
    course = get_course(session, course_id)
    if not course.sequential:
        return frozenset()
    locked: set[int] = set()
    blocked = False
    for activity in list_course_activities(session, course_id):
        if blocked:
            locked.add(activity.id)
        if activity.id not in satisfied_ids:
            blocked = True
    return frozenset(locked)


def satisfied_activity_ids(session: Session, enrollment_id: int) -> frozenset[int]:
    """The activities the enrollment has satisfied (via any variant)."""
    return frozenset(
        session.scalars(
            select(ActivityProgress.activity_id).where(
                ActivityProgress.enrollment_id == enrollment_id
            )
        )
    )


def record_variant_satisfied(
    session: Session, enrollment_id: int, variant_id: int
) -> ActivityProgress:
    """Mark a variant's activity as satisfied for an enrollment.

    Idempotent per activity: an activity already satisfied through another
    variant (or an earlier call) keeps its original progress row. Once every
    activity of the enrollment's course is satisfied, the still active
    enrollment is completed (which queues ``CourseCompleted`` for the
    transaction owner's ``commit_with_events``).
    """
    variant = get_activity_variant(session, variant_id)
    enrollment = get_enrollment(session, enrollment_id)
    progress = session.scalars(
        select(ActivityProgress).where(
            ActivityProgress.enrollment_id == enrollment_id,
            ActivityProgress.activity_id == variant.activity_id,
        )
    ).first()
    if progress is None:
        progress = ActivityProgress(
            enrollment_id=enrollment_id,
            activity_id=variant.activity_id,
            variant_id=variant.id,
        )
        session.add(progress)
        session.flush()

    course_activity_ids = set(
        session.scalars(select(Activity.id).where(Activity.course_id == enrollment.course_id))
    )
    if (
        course_activity_ids <= satisfied_activity_ids(session, enrollment_id)
        and enrollment.status is EnrollmentStatus.ACTIVE
    ):
        complete_enrollment(session, enrollment_id)
    return progress


def last_launch_times(session: Session, enrollment_id: int) -> Mapping[int, datetime]:
    """When the enrollment last launched each activity, by activity id; never
    launched activities are absent."""
    rows = session.execute(
        select(ActivityLaunch.activity_id, ActivityLaunch.last_launched_at).where(
            ActivityLaunch.enrollment_id == enrollment_id
        )
    )
    return dict(rows.tuples().all())


def record_activity_launched(
    session: Session, enrollment_id: int, activity_id: int, launched_at: datetime
) -> ActivityLaunch:
    """Stamp ``launched_at`` as the enrollment's latest launch of the activity.

    Content modules call it from their launch endpoints, before the commit
    that persists their own launch state.
    """
    launch = session.scalars(
        select(ActivityLaunch).where(
            ActivityLaunch.enrollment_id == enrollment_id,
            ActivityLaunch.activity_id == activity_id,
        )
    ).first()
    if launch is None:
        launch = ActivityLaunch(
            enrollment_id=enrollment_id, activity_id=activity_id, last_launched_at=launched_at
        )
        session.add(launch)
    else:
        launch.last_launched_at = launched_at
    session.flush()
    return launch


def activity_progress_measures(session: Session, enrollment_id: int) -> Mapping[int, float]:
    """How far the enrollment got in each activity (0 to 1), by activity id;
    activities whose content never reported a progress measure are absent."""
    rows = session.execute(
        select(ActivityLaunch.activity_id, ActivityLaunch.progress_measure).where(
            ActivityLaunch.enrollment_id == enrollment_id,
            ActivityLaunch.progress_measure.is_not(None),
        )
    )
    return {activity_id: measure for activity_id, measure in rows.tuples() if measure is not None}


def record_activity_progress_measure(
    session: Session, enrollment_id: int, activity_id: int, progress_measure: float
) -> ActivityLaunch:
    """Store how far the enrollment got in the activity, as a fraction 0 to 1.

    Content modules call it when their content reports progress; a client may
    call it too when it derives progress from content data the core does not
    interpret. The activity must have been launched by the enrollment, since
    content only reports progress while it runs. Raises
    InvalidProgressMeasureError outside 0..1 and ActivityNotLaunchedError
    without a launch.
    """
    if not 0.0 <= progress_measure <= 1.0:
        raise InvalidProgressMeasureError(
            f"Progress measure {progress_measure} for activity {activity_id} of enrollment "
            f"{enrollment_id} is outside 0..1"
        )
    launch = session.scalars(
        select(ActivityLaunch).where(
            ActivityLaunch.enrollment_id == enrollment_id,
            ActivityLaunch.activity_id == activity_id,
        )
    ).first()
    if launch is None:
        raise ActivityNotLaunchedError(
            f"Enrollment {enrollment_id} never launched activity {activity_id}, "
            "so no progress can be recorded for it"
        )
    launch.progress_measure = progress_measure
    session.flush()
    return launch


def enrollment_progress(session: Session, enrollments: Sequence[Enrollment]) -> Mapping[int, float]:
    """How far each enrollment got in its course (0 to 1), by enrollment id.

    The course progress is the mean over the course's activities of 1 for a
    satisfied activity, else its reported progress measure, else 0. A
    completed enrollment is 1; an active one in a course without activities
    is 0.
    """
    enrollment_ids = [enrollment.id for enrollment in enrollments]
    course_ids = {enrollment.course_id for enrollment in enrollments}
    activities_by_course: dict[int, list[int]] = {}
    for course_id, activity_id in session.execute(
        select(Activity.course_id, Activity.id).where(Activity.course_id.in_(course_ids))
    ).tuples():
        activities_by_course.setdefault(course_id, []).append(activity_id)
    satisfied = set(
        session.execute(
            select(ActivityProgress.enrollment_id, ActivityProgress.activity_id).where(
                ActivityProgress.enrollment_id.in_(enrollment_ids)
            )
        ).tuples()
    )
    measures: dict[tuple[int, int], float] = {}
    for enrollment_id, activity_id, measure in session.execute(
        select(
            ActivityLaunch.enrollment_id,
            ActivityLaunch.activity_id,
            ActivityLaunch.progress_measure,
        ).where(
            ActivityLaunch.enrollment_id.in_(enrollment_ids),
            ActivityLaunch.progress_measure.is_not(None),
        )
    ).tuples():
        if measure is not None:
            measures[(enrollment_id, activity_id)] = measure

    progress: dict[int, float] = {}
    for enrollment in enrollments:
        activity_ids = activities_by_course.get(enrollment.course_id, [])
        if enrollment.status is EnrollmentStatus.COMPLETED:
            progress[enrollment.id] = 1.0
        elif not activity_ids:
            progress[enrollment.id] = 0.0
        else:
            progress[enrollment.id] = sum(
                1.0
                if (enrollment.id, activity_id) in satisfied
                else measures.get((enrollment.id, activity_id), 0.0)
                for activity_id in activity_ids
            ) / len(activity_ids)
    return progress


def set_activity_translation(
    session: Session, activity_id: int, lang: str, title: str
) -> ActivityTranslation:
    """Set the activity's title in ``lang``, replacing the translation already
    held for that language.

    Raises InvalidLanguageTagError on a malformed tag.
    """
    ensure_language_tag(lang)
    activity = get_activity(session, activity_id)
    translation = next((t for t in activity.translations if t.lang == lang), None)
    if translation is None:
        translation = ActivityTranslation(lang=lang, title=title)
        activity.translations.append(translation)
    else:
        translation.title = title
    session.flush()
    return translation


def remove_activity_translation(session: Session, activity_id: int, lang: str) -> None:
    """Drop the activity's translation in ``lang``."""
    activity = get_activity(session, activity_id)
    translation = next((t for t in activity.translations if t.lang == lang), None)
    if translation is None:
        raise ActivityTranslationNotFoundError(
            f"Activity {activity_id} has no {lang!r} translation"
        )
    # delete-orphan cascade turns the removal into a DELETE of the row
    activity.translations.remove(translation)
    session.flush()
