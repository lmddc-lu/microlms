"""Course endpoints, visibility-scoped by organization.

Authoring actions are org-scoped like every other permission: an org-scoped
author acts only on courses owned by an organization they author in
(``get_course_in_scope``), a platform-scoped author on all of them. Reading
follows visibility instead (``get_readable_course``).
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import AfterValidator, BaseModel, ConfigDict
from sqlalchemy import select

from microlms.api.deps import (
    AuditDep,
    CurrentPermissionsDep,
    CurrentUserDep,
    ManagementScope,
    SessionDep,
    VariantPurgerDep,
    get_activity_in_course,
    get_course_in_scope,
    get_readable_course,
    list_readable_courses,
    require_org_in_scope,
    require_permission,
    require_scoped_permission,
)
from microlms.authz import Permission
from microlms.domain.activities import (
    ActivityTranslationNotFoundError,
    ActivityVariantNotFoundError,
    activity_progress_measures,
    delete_activity,
    delete_activity_variant,
    last_launch_times,
    list_course_activities,
    locked_activity_ids,
    move_activity,
    remove_activity_translation,
    rename_activity,
    satisfied_activity_ids,
    set_activity_translation,
)
from microlms.domain.courses import (
    CourseNotFoundError,
    CourseOrgNotAssignedError,
    CoursePrerequisiteCycleError,
    CoursePrerequisiteNotSetError,
    CourseStatus,
    CourseTranslationNotFoundError,
    SlugAlreadyUsedError,
    add_course_prerequisite,
    archive_course,
    assign_course_to_org,
    create_course,
    delete_course,
    publish_course,
    remove_course_prerequisite,
    remove_course_translation,
    set_course_translation,
    unassign_course_from_org,
    update_course,
)
from microlms.domain.enrollments import Enrollment
from microlms.domain.languages import InvalidLanguageTagError
from microlms.domain.orgs import OrgNotFoundError

router = APIRouter(prefix="/courses", tags=["courses"])

# Sorted so id lists read from frozenset properties are deterministic
SortedIdsRead = Annotated[list[int], AfterValidator(sorted)]

# One scope dependency per authoring action: each endpoint gates on its own
# permission, and the resolved scope pins org-scoped authors to the courses
# their org owns.
CourseCreateScope = Annotated[
    ManagementScope, Depends(require_scoped_permission(Permission.COURSE_CREATE))
]
CourseEditScope = Annotated[
    ManagementScope, Depends(require_scoped_permission(Permission.COURSE_EDIT))
]
CoursePublishScope = Annotated[
    ManagementScope, Depends(require_scoped_permission(Permission.COURSE_PUBLISH))
]
CourseArchiveScope = Annotated[
    ManagementScope, Depends(require_scoped_permission(Permission.COURSE_ARCHIVE))
]
CourseDeleteScope = Annotated[
    ManagementScope, Depends(require_scoped_permission(Permission.COURSE_DELETE))
]


class CourseUpdate(BaseModel):
    slug: str
    title: str
    description: str
    # Sequential courses lock each activity until every earlier one is satisfied
    sequential: bool = False


class CourseCreate(CourseUpdate):
    # Owning organization; None = platform-owned (platform-wide authors only)
    owner_org_id: int | None


class CourseTranslationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    lang: str
    title: str
    description: str


def _sorted_by_lang(translations: list[CourseTranslationRead]) -> list[CourseTranslationRead]:
    # A translation added in the current request sits at the end of the
    # loaded collection, so the order is restored here
    return sorted(translations, key=lambda translation: translation.lang)


class CourseTranslationUpdate(BaseModel):
    title: str
    description: str


class CourseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    title: str
    description: str
    status: CourseStatus
    sequential: bool
    # Owning organization (None = platform-owned); decides which org's
    # authors may act on the course
    owner_org_id: int | None
    org_ids: SortedIdsRead
    # Courses that must be completed before enrolling into this one
    prerequisite_ids: SortedIdsRead
    # Display texts per language, sorted by language; ``title`` and
    # ``description`` above are the reference texts
    translations: Annotated[list[CourseTranslationRead], AfterValidator(_sorted_by_lang)]


class ActivityVariantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    lang: str
    # Content module owning the variant's package ("scorm", "cmi5", ...);
    # tells the client which listing/launch endpoints apply
    module: str


class ActivityTranslationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    lang: str
    title: str


class ActivityTranslationUpdate(BaseModel):
    title: str


class ActivityRead(BaseModel):
    id: int
    position: int
    title: str
    # Whether the requesting user's enrollment satisfied this activity
    # (through any language variant); false without an enrollment
    satisfied: bool
    # Whether launching this activity would currently be refused for the
    # requesting user: true in a sequential course while an earlier activity
    # is unsatisfied (with no enrollment, everything past the first activity)
    locked: bool
    # When the requesting user's enrollment last launched this activity
    # (through any language variant); None if never launched or not enrolled
    last_launched_at: datetime | None
    # How far the requesting user's enrollment got in this activity, 0 to 1,
    # as last reported by its content; None if the content never reported a
    # progress measure or not enrolled
    progress_measure: float | None
    variants: list[ActivityVariantRead]
    # Display titles per language, sorted by language; ``title`` above is the
    # reference title
    translations: list[ActivityTranslationRead]


class ActivityRename(BaseModel):
    title: str


class ActivityMove(BaseModel):
    # Target 0-based position within the course; clamped to the valid range
    position: int


@router.get("", dependencies=[Depends(require_permission(Permission.CONTENT_ACCESS))])
def read_courses(
    session: SessionDep, current_user: CurrentUserDep, permissions: CurrentPermissionsDep
) -> list[CourseRead]:
    found = list_readable_courses(session, current_user, permissions)
    return [CourseRead.model_validate(course) for course in found]


@router.get("/{course_id}", dependencies=[Depends(require_permission(Permission.CONTENT_ACCESS))])
def read_course(
    session: SessionDep,
    current_user: CurrentUserDep,
    permissions: CurrentPermissionsDep,
    course_id: int,
) -> CourseRead:
    course = get_readable_course(session, current_user, permissions, course_id)
    return CourseRead.model_validate(course)


def read_course_activities_for(
    session: SessionDep, current_user: CurrentUserDep, course_id: int
) -> list[ActivityRead]:
    """Build the activity list of a course with the requesting user's
    per-activity satisfaction, last launch and progress measure (empty without
    an enrollment). The caller has already authorized access to the course."""
    enrollment = session.scalars(
        select(Enrollment).where(
            Enrollment.user_id == current_user.id, Enrollment.course_id == course_id
        )
    ).first()
    satisfied = (
        satisfied_activity_ids(session, enrollment.id) if enrollment is not None else frozenset()
    )
    launched = last_launch_times(session, enrollment.id) if enrollment is not None else {}
    measures = activity_progress_measures(session, enrollment.id) if enrollment is not None else {}
    locked = locked_activity_ids(session, course_id, satisfied)
    return [
        ActivityRead(
            id=activity.id,
            position=activity.position,
            title=activity.title,
            satisfied=activity.id in satisfied,
            locked=activity.id in locked,
            last_launched_at=launched.get(activity.id),
            progress_measure=measures.get(activity.id),
            variants=[ActivityVariantRead.model_validate(variant) for variant in activity.variants],
            translations=[
                ActivityTranslationRead.model_validate(translation)
                for translation in sorted(activity.translations, key=lambda t: t.lang)
            ],
        )
        for activity in list_course_activities(session, course_id)
    ]


@router.get(
    "/{course_id}/activities", dependencies=[Depends(require_permission(Permission.CONTENT_ACCESS))]
)
def read_course_activities(
    session: SessionDep,
    current_user: CurrentUserDep,
    permissions: CurrentPermissionsDep,
    course_id: int,
) -> list[ActivityRead]:
    """The activities of a course with their language variants, in course
    order; same visibility as the course detail endpoint."""
    course = get_readable_course(session, current_user, permissions, course_id)
    return read_course_activities_for(session, current_user, course.id)


@router.patch("/{course_id}/activities/{activity_id}")
def rename_course_activity(
    session: SessionDep,
    current_user: CurrentUserDep,
    scope: CourseEditScope,
    audit: AuditDep,
    course_id: int,
    activity_id: int,
    payload: ActivityRename,
) -> list[ActivityRead]:
    """Rename an activity; returns the course's activities in order."""
    get_course_in_scope(session, scope, course_id)
    get_activity_in_course(session, course_id, activity_id)
    rename_activity(session, activity_id, payload.title)
    audit.record("activity.renamed", "activity", str(activity_id), {"course_id": course_id})
    return read_course_activities_for(session, current_user, course_id)


@router.put("/{course_id}/activities/{activity_id}/translations/{lang}")
def set_course_activity_translation(
    session: SessionDep,
    current_user: CurrentUserDep,
    scope: CourseEditScope,
    audit: AuditDep,
    course_id: int,
    activity_id: int,
    lang: str,
    payload: ActivityTranslationUpdate,
) -> list[ActivityRead]:
    """Set an activity's title in one language, replacing any held for it;
    returns the course's activities in order."""
    get_course_in_scope(session, scope, course_id)
    get_activity_in_course(session, course_id, activity_id)
    try:
        set_activity_translation(session, activity_id, lang, payload.title)
    except InvalidLanguageTagError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    audit.record(
        "activity.translation_set",
        "activity",
        str(activity_id),
        {"course_id": course_id, "lang": lang},
    )
    return read_course_activities_for(session, current_user, course_id)


@router.delete("/{course_id}/activities/{activity_id}/translations/{lang}")
def remove_course_activity_translation(
    session: SessionDep,
    current_user: CurrentUserDep,
    scope: CourseEditScope,
    audit: AuditDep,
    course_id: int,
    activity_id: int,
    lang: str,
) -> list[ActivityRead]:
    """Drop an activity's title in one language; returns the course's
    activities in order."""
    get_course_in_scope(session, scope, course_id)
    get_activity_in_course(session, course_id, activity_id)
    try:
        remove_activity_translation(session, activity_id, lang)
    except ActivityTranslationNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    audit.record(
        "activity.translation_removed",
        "activity",
        str(activity_id),
        {"course_id": course_id, "lang": lang},
    )
    return read_course_activities_for(session, current_user, course_id)


@router.post("/{course_id}/activities/{activity_id}/move")
def move_course_activity(
    session: SessionDep,
    current_user: CurrentUserDep,
    scope: CourseEditScope,
    audit: AuditDep,
    course_id: int,
    activity_id: int,
    payload: ActivityMove,
) -> list[ActivityRead]:
    """Reorder an activity within its course; returns the new order."""
    get_course_in_scope(session, scope, course_id)
    get_activity_in_course(session, course_id, activity_id)
    move_activity(session, activity_id, payload.position)
    audit.record(
        "activity.moved",
        "activity",
        str(activity_id),
        {"course_id": course_id, "position": payload.position},
    )
    return read_course_activities_for(session, current_user, course_id)


@router.delete("/{course_id}/activities/{activity_id}")
def delete_course_activity(
    session: SessionDep,
    current_user: CurrentUserDep,
    scope: CourseEditScope,
    audit: AuditDep,
    purge: VariantPurgerDep,
    course_id: int,
    activity_id: int,
) -> list[ActivityRead]:
    """Delete an activity, all its language variants (content and stored files
    included) and the progress recorded against it; returns the remaining
    activities in order."""
    get_course_in_scope(session, scope, course_id)
    get_activity_in_course(session, course_id, activity_id)
    delete_activity(session, activity_id, purge)
    audit.record("activity.deleted", "activity", str(activity_id), {"course_id": course_id})
    return read_course_activities_for(session, current_user, course_id)


@router.delete("/{course_id}/activities/{activity_id}/variants/{variant_id}")
def delete_course_activity_variant(
    session: SessionDep,
    current_user: CurrentUserDep,
    scope: CourseEditScope,
    audit: AuditDep,
    purge: VariantPurgerDep,
    course_id: int,
    activity_id: int,
    variant_id: int,
) -> list[ActivityRead]:
    """Delete one language variant of an activity (its content and stored files
    included); returns the course's activities in order."""
    get_course_in_scope(session, scope, course_id)
    get_activity_in_course(session, course_id, activity_id)
    try:
        delete_activity_variant(session, activity_id, variant_id, purge)
    except ActivityVariantNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    audit.record(
        "activity_variant.deleted",
        "activity",
        str(activity_id),
        {"course_id": course_id, "variant_id": variant_id},
    )
    return read_course_activities_for(session, current_user, course_id)


@router.post("", status_code=status.HTTP_201_CREATED)
def add_course(
    session: SessionDep, scope: CourseCreateScope, audit: AuditDep, payload: CourseCreate
) -> CourseRead:
    """Create a course as a draft; publish it to make it visible to learners.

    ``owner_org_id`` names the owning organization: org-scoped authors must
    pick one they author in, platform-wide authors may also leave it None for
    a platform-owned, globally visible course. An org-owned course starts
    visible to its owning org.
    """
    if payload.owner_org_id is None:
        if scope.org_ids is not None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Org-scoped authors must name an owning organization in their reach",
            )
    else:
        require_org_in_scope(scope, payload.owner_org_id)
    try:
        course = create_course(
            session,
            slug=payload.slug,
            title=payload.title,
            description=payload.description,
            sequential=payload.sequential,
            owner_org_id=payload.owner_org_id,
        )
    except SlugAlreadyUsedError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except OrgNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    if payload.owner_org_id is not None:
        assign_course_to_org(session, course.id, payload.owner_org_id)
    audit.record("course.created", "course", str(course.id), {"slug": course.slug})
    return CourseRead.model_validate(course)


@router.put("/{course_id}")
def edit_course(
    session: SessionDep,
    scope: CourseEditScope,
    audit: AuditDep,
    course_id: int,
    payload: CourseUpdate,
) -> CourseRead:
    """Replace the editable fields of a course; its status is untouched."""
    get_course_in_scope(session, scope, course_id)
    try:
        course = update_course(
            session,
            course_id,
            slug=payload.slug,
            title=payload.title,
            description=payload.description,
            sequential=payload.sequential,
        )
    except SlugAlreadyUsedError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    audit.record("course.updated", "course", str(course.id), {"slug": course.slug})
    return CourseRead.model_validate(course)


@router.put("/{course_id}/translations/{lang}")
def set_translation(
    session: SessionDep,
    scope: CourseEditScope,
    audit: AuditDep,
    course_id: int,
    lang: str,
    payload: CourseTranslationUpdate,
) -> CourseRead:
    """Set a course's title and description in one language, replacing any
    held for it."""
    course = get_course_in_scope(session, scope, course_id)
    try:
        set_course_translation(session, course_id, lang, payload.title, payload.description)
    except InvalidLanguageTagError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    audit.record("course.translation_set", "course", str(course_id), {"lang": lang})
    return CourseRead.model_validate(course)


@router.delete("/{course_id}/translations/{lang}")
def remove_translation(
    session: SessionDep, scope: CourseEditScope, audit: AuditDep, course_id: int, lang: str
) -> CourseRead:
    """Drop a course's title and description in one language."""
    course = get_course_in_scope(session, scope, course_id)
    try:
        remove_course_translation(session, course_id, lang)
    except CourseTranslationNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    audit.record("course.translation_removed", "course", str(course_id), {"lang": lang})
    return CourseRead.model_validate(course)


@router.post("/{course_id}/publish")
def publish(
    session: SessionDep, scope: CoursePublishScope, audit: AuditDep, course_id: int
) -> CourseRead:
    """Make a course visible and enrollable; idempotent, also un-archives."""
    get_course_in_scope(session, scope, course_id)
    course = publish_course(session, course_id)
    audit.record("course.published", "course", str(course.id), None)
    return CourseRead.model_validate(course)


@router.post("/{course_id}/archive")
def archive(
    session: SessionDep, scope: CourseArchiveScope, audit: AuditDep, course_id: int
) -> CourseRead:
    """Remove a course from the catalog; existing enrollments survive."""
    get_course_in_scope(session, scope, course_id)
    course = archive_course(session, course_id)
    audit.record("course.archived", "course", str(course.id), None)
    return CourseRead.model_validate(course)


@router.delete("/{course_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_course(
    session: SessionDep,
    scope: CourseDeleteScope,
    audit: AuditDep,
    purge: VariantPurgerDep,
    course_id: int,
) -> None:
    """Delete a course and everything scoped to it (activities, their content
    and stored files, enrollments and progress). Irreversible; archive instead
    to only hide it from the catalog."""
    get_course_in_scope(session, scope, course_id)
    delete_course(session, course_id, purge)
    audit.record("course.deleted", "course", str(course_id), None)


def _forbid_foreign_org_visibility(scope: ManagementScope, org_id: int) -> None:
    """403 when an org-scoped author changes visibility for an org outside their reach."""
    if scope.org_ids is not None and org_id not in scope.org_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Org-scoped authors can only change visibility for organizations in their reach",
        )


@router.put("/{course_id}/orgs/{org_id}")
def add_org_assignment(
    session: SessionDep, scope: CourseEditScope, audit: AuditDep, course_id: int, org_id: int
) -> CourseRead:
    """Assign a course to an organization; idempotent when already assigned."""
    get_course_in_scope(session, scope, course_id)
    _forbid_foreign_org_visibility(scope, org_id)
    try:
        course = assign_course_to_org(session, course_id, org_id)
    except OrgNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    audit.record("course.org_assigned", "course", str(course_id), {"org_id": org_id})
    return CourseRead.model_validate(course)


@router.delete("/{course_id}/orgs/{org_id}")
def remove_org_assignment(
    session: SessionDep, scope: CourseEditScope, audit: AuditDep, course_id: int, org_id: int
) -> CourseRead:
    """Unassign an organization; removing the last one makes the course global."""
    get_course_in_scope(session, scope, course_id)
    _forbid_foreign_org_visibility(scope, org_id)
    try:
        course = unassign_course_from_org(session, course_id, org_id)
    except (OrgNotFoundError, CourseOrgNotAssignedError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    audit.record("course.org_unassigned", "course", str(course_id), {"org_id": org_id})
    return CourseRead.model_validate(course)


@router.put("/{course_id}/prerequisites/{prerequisite_course_id}")
def add_prerequisite(
    session: SessionDep,
    scope: CourseEditScope,
    audit: AuditDep,
    course_id: int,
    prerequisite_course_id: int,
) -> CourseRead:
    """Require another course completed before enrolling into this one;
    idempotent when already required. Existing enrollments are untouched."""
    get_course_in_scope(session, scope, course_id)
    try:
        course = add_course_prerequisite(session, course_id, prerequisite_course_id)
    except CourseNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except CoursePrerequisiteCycleError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    audit.record(
        "course.prerequisite_added",
        "course",
        str(course_id),
        {"prerequisite_course_id": prerequisite_course_id},
    )
    return CourseRead.model_validate(course)


@router.delete("/{course_id}/prerequisites/{prerequisite_course_id}")
def remove_prerequisite(
    session: SessionDep,
    scope: CourseEditScope,
    audit: AuditDep,
    course_id: int,
    prerequisite_course_id: int,
) -> CourseRead:
    """Drop a prerequisite; enrollments it blocked become possible again."""
    get_course_in_scope(session, scope, course_id)
    try:
        course = remove_course_prerequisite(session, course_id, prerequisite_course_id)
    except (CourseNotFoundError, CoursePrerequisiteNotSetError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    audit.record(
        "course.prerequisite_removed",
        "course",
        str(course_id),
        {"prerequisite_course_id": prerequisite_course_id},
    )
    return CourseRead.model_validate(course)
