"""Course model, lifecycle status, org visibility, and services.

A course starts as a draft, is published to become visible and enrollable,
and is archived to leave the catalog while existing enrollments survive.
Non-published courses are seen only by their authors (course-edit holders in
the owning organization, or platform-wide ones).

A course belongs to at most one *owning* organization (``owner_org_id``,
None = platform-owned): org-scoped authors act only on courses owned by an
organization they author in. Ownership is separate from *visibility*: a
course assigned to no organization is global - visible to every
authenticated user - while assigning it to organizations
(``lms_course_org``) restricts visibility to their members; a user belonging
to several organizations sees what any of them sees.

A course has a reference ``title`` and ``description``, used wherever no
language is known, plus optional per-language display texts
(``lms_course_translation``) that clients pick from.

A course may require other courses completed first (``lms_course_prerequisite``);
enrollment refuses while a prerequisite is uncompleted. A sequential course
additionally locks each activity until every earlier activity is satisfied
(see :func:`microlms.domain.activities.locked_activity_ids`).
"""

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    delete,
    exists,
    func,
    or_,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from microlms.db.base import Base
from microlms.domain.languages import ensure_language_tag
from microlms.domain.orgs import get_org
from microlms.events import (
    CourseOrgAssigned,
    CourseOrgUnassigned,
    CoursePublished,
    queue_event,
)

__all__ = [
    "Course",
    "CourseNotFoundError",
    "CourseStatus",
    "CourseTranslation",
    "course_visible_to",
    "get_course",
    "list_courses_visible_to",
]

if TYPE_CHECKING:
    from microlms.domain.activities import VariantContentPurge


class CourseStatus(enum.StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class Course(Base):
    __tablename__ = "lms_course"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text())
    status: Mapped[CourseStatus] = mapped_column(
        Enum(CourseStatus, name="course_status"), default=CourseStatus.DRAFT
    )
    # Sequential courses lock each activity until every earlier one is satisfied
    sequential: Mapped[bool] = mapped_column(Boolean(), default=False)
    # Owning organization: org-scoped authors act only on their org's courses.
    # None = platform-owned (only platform-scoped authors touch it).
    owner_org_id: Mapped[int | None] = mapped_column(ForeignKey("lms_org.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # selectin so assignments load with the course and never lazy-load after
    # the request session is closed
    org_assignments: Mapped[list["CourseOrgAssignment"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )
    # selectin for the same reason; foreign_keys disambiguates the edge
    # table's two references onto lms_course
    prerequisites: Mapped[list["CoursePrerequisite"]] = relationship(
        cascade="all, delete-orphan",
        lazy="selectin",
        foreign_keys="CoursePrerequisite.course_id",
    )
    # selectin for the same reason as above
    translations: Mapped[list["CourseTranslation"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", order_by="CourseTranslation.lang"
    )

    @property
    def org_ids(self) -> frozenset[int]:
        return frozenset(assignment.org_id for assignment in self.org_assignments)

    @property
    def prerequisite_ids(self) -> frozenset[int]:
        return frozenset(edge.prerequisite_course_id for edge in self.prerequisites)


class CourseOrgAssignment(Base):
    __tablename__ = "lms_course_org"

    course_id: Mapped[int] = mapped_column(ForeignKey("lms_course.id"), primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("lms_org.id"), primary_key=True)


class CoursePrerequisite(Base):
    """One prerequisite edge: the course requires the prerequisite completed."""

    __tablename__ = "lms_course_prerequisite"

    course_id: Mapped[int] = mapped_column(ForeignKey("lms_course.id"), primary_key=True)
    prerequisite_course_id: Mapped[int] = mapped_column(
        ForeignKey("lms_course.id"), primary_key=True
    )


class CourseTranslation(Base):
    """The title and description of a course in one language."""

    __tablename__ = "lms_course_translation"

    course_id: Mapped[int] = mapped_column(ForeignKey("lms_course.id"), primary_key=True)
    lang: Mapped[str] = mapped_column(String(35), primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text())


class CourseNotFoundError(Exception):
    """Raised when no course exists for the given identifier."""


class CourseOrgNotAssignedError(Exception):
    """Raised when unassigning an organization the course is not assigned to."""


class CoursePrerequisiteCycleError(Exception):
    """Raised when a prerequisite edge would make a course require itself."""


class CoursePrerequisiteNotSetError(Exception):
    """Raised when removing a prerequisite the course does not have."""


class CourseTranslationNotFoundError(Exception):
    """Raised when removing a translation the course does not have."""


class SlugAlreadyUsedError(Exception):
    """Raised when a slug is already taken by another course."""


def get_course(session: Session, course_id: int) -> Course:
    course = session.get(Course, course_id)
    if course is None:
        raise CourseNotFoundError(f"No course with id {course_id}")
    return course


def list_courses(session: Session) -> list[Course]:
    return list(session.scalars(select(Course).order_by(Course.id)))


def create_course(
    session: Session,
    slug: str,
    title: str,
    description: str,
    sequential: bool,
    owner_org_id: int | None,
) -> Course:
    """Create a course as a draft; publish it to make it visible to learners.

    ``owner_org_id`` sets the owning organization (None = platform-owned) and
    must reference an existing org.
    """
    if session.scalar(select(Course).where(Course.slug == slug)) is not None:
        raise SlugAlreadyUsedError(f"Slug {slug} is already used by another course")
    if owner_org_id is not None:
        get_org(session, owner_org_id)
    course = Course(
        slug=slug,
        title=title,
        description=description,
        sequential=sequential,
        owner_org_id=owner_org_id,
    )
    session.add(course)
    session.flush()
    return course


def update_course(
    session: Session, course_id: int, slug: str, title: str, description: str, sequential: bool
) -> Course:
    """Replace the editable fields of a course.

    Raises SlugAlreadyUsedError when the slug belongs to another course.
    """
    course = get_course(session, course_id)
    other = session.scalar(select(Course).where(Course.slug == slug))
    if other is not None and other.id != course.id:
        raise SlugAlreadyUsedError(f"Slug {slug} is already used by another course")
    course.slug = slug
    course.title = title
    course.description = description
    course.sequential = sequential
    session.flush()
    return course


def publish_course(session: Session, course_id: int) -> Course:
    """Make a course visible and enrollable; idempotent, also un-archives."""
    course = get_course(session, course_id)
    if course.status is not CourseStatus.PUBLISHED:
        course.status = CourseStatus.PUBLISHED
        session.flush()
        queue_event(session, CoursePublished(course_id=course.id))
    return course


def archive_course(session: Session, course_id: int) -> Course:
    """Remove a course from the catalog; existing enrollments survive."""
    course = get_course(session, course_id)
    course.status = CourseStatus.ARCHIVED
    session.flush()
    return course


def delete_course(session: Session, course_id: int, purge: "VariantContentPurge") -> None:
    """Delete a course and everything scoped to it: its activities (each
    variant's content module data and stored files, via ``purge``), every
    enrollment (with the progress, attempts and sessions cascaded on the
    enrollment), its org assignments, and every prerequisite edge it appears
    in - courses that required it no longer do. Irreversible - use
    :func:`archive_course` to merely hide a course from the catalog."""
    # Imported here to avoid a module-level cycle: activities depends on courses
    from microlms.domain.activities import delete_activity, list_course_activities
    from microlms.domain.enrollments import Enrollment

    course = get_course(session, course_id)
    for activity_id in [activity.id for activity in list_course_activities(session, course_id)]:
        delete_activity(session, activity_id, purge)
    session.execute(delete(Enrollment).where(Enrollment.course_id == course_id))
    # The course's own edges cascade with the relationship; edges of other
    # courses requiring this one need the explicit reverse cleanup
    session.execute(
        delete(CoursePrerequisite).where(CoursePrerequisite.prerequisite_course_id == course_id)
    )
    session.delete(course)
    session.flush()


def course_visible_to(course: Course, org_ids: frozenset[int]) -> bool:
    """Whether a course is visible from any of the given organizations.

    A course with no org assignment is global; assigned courses are visible
    only to members of those orgs. An empty ``org_ids`` = a user belonging to
    no organization.
    """
    assigned = course.org_ids
    return not assigned or bool(assigned & org_ids)


def _visibility_condition(org_ids: frozenset[int]):  # noqa: ANN202 - SQLAlchemy boolean clause
    """SQL condition for "visible from any of the given organizations"."""
    no_assignment = ~exists().where(CourseOrgAssignment.course_id == Course.id)
    if not org_ids:
        return no_assignment
    in_orgs = exists().where(
        (CourseOrgAssignment.course_id == Course.id)
        & (CourseOrgAssignment.org_id.in_(list(org_ids)))
    )
    return or_(no_assignment, in_orgs)


def list_courses_visible_to(session: Session, org_ids: frozenset[int]) -> list[Course]:
    """Published courses visible from any of the given organizations.

    Non-published courses never reach this listing; authors use
    :func:`list_courses` (platform scope) or :func:`list_courses_for_org_author`
    and see every status of the courses in their reach.
    """
    return list(
        session.scalars(
            select(Course)
            .where(Course.status == CourseStatus.PUBLISHED, _visibility_condition(org_ids))
            .order_by(Course.id)
        )
    )


def list_courses_for_org_author(
    session: Session, author_org_ids: frozenset[int], org_ids: frozenset[int]
) -> list[Course]:
    """Courses an org-scoped author sees: every course owned by an org they
    author in (``author_org_ids``, any status, drafts included) plus the
    published courses visible from any org they belong to (``org_ids``)."""
    owned = Course.owner_org_id.in_(list(author_org_ids))
    published_visible = (Course.status == CourseStatus.PUBLISHED) & _visibility_condition(org_ids)
    return list(
        session.scalars(select(Course).where(or_(owned, published_visible)).order_by(Course.id))
    )


def assign_course_to_org(session: Session, course_id: int, org_id: int) -> Course:
    """Assign a course to an organization; idempotent when already assigned."""
    course = get_course(session, course_id)
    org = get_org(session, org_id)
    if org.id not in course.org_ids:
        course.org_assignments.append(CourseOrgAssignment(org_id=org.id))
        session.flush()
        queue_event(session, CourseOrgAssigned(course_id=course.id, org_id=org.id))
    return course


def unassign_course_from_org(session: Session, course_id: int, org_id: int) -> Course:
    """Remove an org assignment; removing the last one makes the course global."""
    course = get_course(session, course_id)
    get_org(session, org_id)
    target = next(
        (a for a in course.org_assignments if a.org_id == org_id),
        None,
    )
    if target is None:
        raise CourseOrgNotAssignedError(
            f"Course {course_id} is not assigned to organization {org_id}"
        )
    # delete-orphan cascade turns the removal into a DELETE of the row
    course.org_assignments.remove(target)
    session.flush()
    queue_event(session, CourseOrgUnassigned(course_id=course.id, org_id=org_id))
    return course


def _ensure_no_prerequisite_cycle(
    session: Session, course_id: int, prerequisite_course_id: int
) -> None:
    """Reject the edge when the course is reachable from the prerequisite
    through existing prerequisite edges (self-reference included): the cycle
    would leave every course on it permanently unenrollable."""
    if course_id == prerequisite_course_id:
        raise CoursePrerequisiteCycleError(f"Course {course_id} cannot require itself")
    edges: dict[int, set[int]] = {}
    for from_id, to_id in session.execute(
        select(CoursePrerequisite.course_id, CoursePrerequisite.prerequisite_course_id)
    ):
        edges.setdefault(from_id, set()).add(to_id)
    to_visit = [prerequisite_course_id]
    seen: set[int] = set()
    while to_visit:
        current = to_visit.pop()
        if current == course_id:
            raise CoursePrerequisiteCycleError(
                f"Course {prerequisite_course_id} already requires course {course_id} "
                f"(directly or through its own prerequisites), so requiring it back "
                f"would create a cycle"
            )
        if current in seen:
            continue
        seen.add(current)
        to_visit.extend(edges.get(current, ()))


def add_course_prerequisite(
    session: Session, course_id: int, prerequisite_course_id: int
) -> Course:
    """Require another course completed before enrolling into this one;
    idempotent when already required.

    Raises CoursePrerequisiteCycleError when the edge would make a course
    require itself, directly or transitively.
    """
    course = get_course(session, course_id)
    prerequisite = get_course(session, prerequisite_course_id)
    if prerequisite.id in course.prerequisite_ids:
        return course
    _ensure_no_prerequisite_cycle(session, course.id, prerequisite.id)
    course.prerequisites.append(CoursePrerequisite(prerequisite_course_id=prerequisite.id))
    session.flush()
    return course


def remove_course_prerequisite(
    session: Session, course_id: int, prerequisite_course_id: int
) -> Course:
    """Drop a prerequisite; existing enrollments are untouched either way.

    Raises CoursePrerequisiteNotSetError when the course does not require it.
    """
    course = get_course(session, course_id)
    get_course(session, prerequisite_course_id)
    target = next(
        (
            edge
            for edge in course.prerequisites
            if edge.prerequisite_course_id == prerequisite_course_id
        ),
        None,
    )
    if target is None:
        raise CoursePrerequisiteNotSetError(
            f"Course {course_id} does not require course {prerequisite_course_id}"
        )
    # delete-orphan cascade turns the removal into a DELETE of the row
    course.prerequisites.remove(target)
    session.flush()
    return course


def set_course_translation(
    session: Session, course_id: int, lang: str, title: str, description: str
) -> CourseTranslation:
    """Set the course's title and description in ``lang``, replacing the
    translation already held for that language.

    Raises InvalidLanguageTagError on a malformed tag.
    """
    ensure_language_tag(lang)
    course = get_course(session, course_id)
    translation = next((t for t in course.translations if t.lang == lang), None)
    if translation is None:
        translation = CourseTranslation(lang=lang, title=title, description=description)
        course.translations.append(translation)
    else:
        translation.title = title
        translation.description = description
    session.flush()
    return translation


def remove_course_translation(session: Session, course_id: int, lang: str) -> None:
    """Drop the course's translation in ``lang``."""
    course = get_course(session, course_id)
    translation = next((t for t in course.translations if t.lang == lang), None)
    if translation is None:
        raise CourseTranslationNotFoundError(f"Course {course_id} has no {lang!r} translation")
    # delete-orphan cascade turns the removal into a DELETE of the row
    course.translations.remove(translation)
    session.flush()
