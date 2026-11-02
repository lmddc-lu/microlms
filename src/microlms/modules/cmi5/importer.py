"""cmi5 package import: parse ``cmi5.xml`` and store the package files.

Importing a package attaches one language variant to a course activity
(creating the activity when none is targeted) and creates one ``lms_cmi5_au``
per Assignable Unit, storing every file in the zip under a storage prefix of
its own so the AUs can be served and launched later. A variant's package can
later be replaced in place, keeping the variant and the activity progress
recorded through it.
"""

import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from xml.etree import ElementTree

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from microlms.domain.activities import (
    ActivityNotFoundError,
    ActivityVariant,
    add_activity_variant,
    create_activity,
    get_activity,
    get_module_variant,
)
from microlms.domain.courses import get_course
from microlms.domain.languages import ensure_language_tag
from microlms.modules.cmi5.models import Cmi5Au, Cmi5Session, LaunchMethod, MoveOn
from microlms.storage.base import StorageBackend

MODULE_NAME = "cmi5"

CMI5_NS = "https://w3id.org/xapi/profiles/cmi5/v1/CourseStructure.xsd"
COURSE_STRUCTURE_ENTRY = "cmi5.xml"


class Cmi5ImportError(Exception):
    """Raised when a cmi5 package is missing or its course structure is invalid."""


@dataclass(frozen=True, slots=True)
class ParsedAu:
    publisher_id: str
    title: str
    description: str
    move_on: MoveOn
    mastery_score: float | None
    launch_method: LaunchMethod
    launch_url: str
    launch_parameters: str | None


@dataclass(frozen=True, slots=True)
class ParsedCourse:
    publisher_id: str
    title: str
    description: str
    aus: tuple[ParsedAu, ...]


@dataclass(frozen=True, slots=True)
class ImportedAu:
    id: int
    publisher_id: str
    title: str
    launch_method: LaunchMethod


@dataclass(frozen=True, slots=True)
class ImportResult:
    course_id: int
    activity_id: int
    variant_id: int
    lang: str
    aus: tuple[ImportedAu, ...]


@dataclass(frozen=True, slots=True)
class ReplaceResult:
    imported: ImportResult
    # Storage prefixes of the replaced package, to delete after the commit
    stale_prefixes: tuple[str, ...]


def _tag(name: str) -> str:
    return f"{{{CMI5_NS}}}{name}"


def _langstring(parent: ElementTree.Element | None) -> str:
    """Return the first langstring text under a title/description element."""
    if parent is None:
        return ""
    langstring = parent.find(_tag("langstring"))
    if langstring is None or langstring.text is None:
        return ""
    return langstring.text.strip()


def _parse_au(element: ElementTree.Element) -> ParsedAu:
    publisher_id = element.get("id")
    url = element.find(_tag("url"))
    if publisher_id is None or url is None or url.text is None:
        raise Cmi5ImportError("AU is missing a required 'id' attribute or <url>")

    mastery_score = element.get("masteryScore")
    return ParsedAu(
        publisher_id=publisher_id,
        title=_langstring(element.find(_tag("title"))),
        description=_langstring(element.find(_tag("description"))),
        move_on=MoveOn(element.get("moveOn", MoveOn.NOT_APPLICABLE.value)),
        mastery_score=float(mastery_score) if mastery_score is not None else None,
        launch_method=LaunchMethod(element.get("launchMethod", LaunchMethod.ANY_WINDOW.value)),
        launch_url=url.text.strip(),
        launch_parameters=element.get("launchParameters"),
    )


def _collect_aus(parent: ElementTree.Element) -> list[ParsedAu]:
    """Collect AUs in document order, descending into nested blocks."""
    aus: list[ParsedAu] = []
    for child in parent:
        if child.tag == _tag("au"):
            aus.append(_parse_au(child))
        elif child.tag == _tag("block"):
            aus.extend(_collect_aus(child))
    return aus


def parse_course_structure(xml_bytes: bytes) -> ParsedCourse:
    """Parse a cmi5 ``cmi5.xml`` document into a course and its AUs."""
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as error:
        raise Cmi5ImportError(f"cmi5.xml is not valid XML: {error}") from error

    course = root.find(_tag("course"))
    if course is None or course.get("id") is None:
        raise Cmi5ImportError("cmi5.xml has no <course> element with an 'id'")

    aus = _collect_aus(root)
    if not aus:
        raise Cmi5ImportError("cmi5.xml declares no Assignable Units")

    return ParsedCourse(
        publisher_id=course.get("id"),
        title=_langstring(course.find(_tag("title"))),
        description=_langstring(course.find(_tag("description"))),
        aus=tuple(aus),
    )


@contextmanager
def _open_package(zip_bytes: bytes) -> Iterator[tuple[zipfile.ZipFile, ParsedCourse]]:
    """Open an uploaded zip and parse its course structure, for the duration
    of the block."""
    try:
        archive = zipfile.ZipFile(BytesIO(zip_bytes))
    except zipfile.BadZipFile as error:
        raise Cmi5ImportError(f"Uploaded file is not a valid zip: {error}") from error
    with archive:
        try:
            xml_bytes = archive.read(COURSE_STRUCTURE_ENTRY)
        except KeyError as error:
            raise Cmi5ImportError(f"Package has no {COURSE_STRUCTURE_ENTRY} at its root") from error
        yield archive, parse_course_structure(xml_bytes)


def _store_package(
    session: Session,
    storage: StorageBackend,
    archive: zipfile.ZipFile,
    parsed: ParsedCourse,
    variant_id: int,
) -> list[Cmi5Au]:
    """Store every file of the package under a fresh prefix of the variant and
    create its AUs. Each package gets its own prefix, so a replaced package's
    files stay servable until the replacement has committed."""
    storage_prefix = f"{MODULE_NAME}/{variant_id}-{uuid.uuid4().hex}"
    for name in archive.namelist():
        if name.endswith("/"):
            continue
        storage.put(f"{storage_prefix}/{name}", archive.read(name))

    aus = [
        Cmi5Au(
            variant_id=variant_id,
            au_index=index,
            publisher_id=au.publisher_id,
            title=au.title,
            description=au.description,
            move_on=au.move_on,
            mastery_score=au.mastery_score,
            launch_method=au.launch_method,
            launch_url=au.launch_url,
            launch_parameters=au.launch_parameters,
            storage_prefix=storage_prefix,
        )
        for index, au in enumerate(parsed.aus)
    ]
    session.add_all(aus)
    session.flush()
    return aus


def _import_result(
    course_id: int, activity_id: int, variant: ActivityVariant, aus: list[Cmi5Au]
) -> ImportResult:
    return ImportResult(
        course_id=course_id,
        activity_id=activity_id,
        variant_id=variant.id,
        lang=variant.lang,
        aus=tuple(
            ImportedAu(
                id=au.id,
                publisher_id=au.publisher_id,
                title=au.title,
                launch_method=au.launch_method,
            )
            for au in aus
        ),
    )


def _delete_variant_rows(session: Session, variant_id: int) -> tuple[str, ...]:
    """Delete the AUs of a variant and the learner sessions on them; returns
    the storage prefixes their package files live under."""
    aus = session.scalars(select(Cmi5Au).where(Cmi5Au.variant_id == variant_id)).all()
    prefixes = tuple(sorted({au.storage_prefix for au in aus}))
    au_ids = [au.id for au in aus]
    if au_ids:
        session.execute(delete(Cmi5Session).where(Cmi5Session.au_id.in_(au_ids)))
    session.execute(delete(Cmi5Au).where(Cmi5Au.variant_id == variant_id))
    return prefixes


def import_package(
    session: Session,
    storage: StorageBackend,
    zip_bytes: bytes,
    course_id: int,
    activity_id: int | None,
    lang: str,
) -> ImportResult:
    """Import a cmi5 package as one language variant of a course activity.

    With ``activity_id`` the package becomes the ``lang`` variant of that
    activity (raising ActivityLanguageExistsError when taken); without it a
    new activity titled from the course structure is appended to the course.
    """
    with _open_package(zip_bytes) as (archive, parsed):
        # Validate the target before creating anything so a bad request
        # leaves no orphan activity behind
        ensure_language_tag(lang)
        course = get_course(session, course_id)
        if activity_id is None:
            activity = create_activity(session, course.id, parsed.title)
        else:
            activity = get_activity(session, activity_id)
            if activity.course_id != course.id:
                raise ActivityNotFoundError(
                    f"Activity {activity_id} does not belong to course {course_id}"
                )
        variant = add_activity_variant(session, activity.id, lang, MODULE_NAME)
        aus = _store_package(session, storage, archive, parsed, variant.id)
    return _import_result(course.id, activity.id, variant, aus)


def replace_package(
    session: Session, storage: StorageBackend, zip_bytes: bytes, variant_id: int
) -> ReplaceResult:
    """Replace the package of a cmi5 variant, keeping the variant itself.

    The variant's AUs and every learner session on them are deleted, so
    learners start the new package afresh; the progress the activity recorded
    (satisfaction, launches) is kept. Raises ActivityVariantModuleError when
    another module plays the variant. The old package files stay in storage:
    the caller deletes ``stale_prefixes`` once the transaction has committed.
    """
    with _open_package(zip_bytes) as (archive, parsed):
        variant = get_module_variant(session, variant_id, MODULE_NAME)
        activity = get_activity(session, variant.activity_id)
        stale_prefixes = _delete_variant_rows(session, variant.id)
        aus = _store_package(session, storage, archive, parsed, variant.id)
    return ReplaceResult(
        imported=_import_result(activity.course_id, activity.id, variant, aus),
        stale_prefixes=stale_prefixes,
    )


def purge_variant(session: Session, storage: StorageBackend, variant_id: int) -> None:
    """Remove all cmi5 content of a variant: its AUs, any learner sessions on
    them, and the stored package files. Runs within the caller's transaction
    (no commit); the caller deletes the variant row and commits."""
    for prefix in _delete_variant_rows(session, variant_id):
        storage.delete_prefix(prefix)
