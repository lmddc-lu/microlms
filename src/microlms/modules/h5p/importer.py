"""H5P package import: parse ``h5p.json`` and store the package files.

An ``.h5p`` file is a zip holding one content item: ``h5p.json`` (title, main
library, dependencies), ``content/content.json`` (the authored parameters) and
one directory per bundled JavaScript library. Importing attaches the package
as one language variant of a course activity (creating the activity when none
is targeted) and stores every file under a storage prefix of its own so the
standalone player can fetch them later; a variant's package can later be
replaced in place, keeping the variant and the activity progress recorded
through it. The bundled libraries are required: packages exported "without
libraries" cannot run and are rejected.
"""

import json
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO

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
from microlms.modules.h5p.models import H5pAttempt, H5pContent
from microlms.storage.base import StorageBackend

MODULE_NAME = "h5p"

PACKAGE_MANIFEST = "h5p.json"
CONTENT_ENTRY = "content/content.json"
ACTIVITY_IRI_PREFIX = "urn:microlms:h5p"


class H5pImportError(Exception):
    """Raised when an H5P package is missing or its manifest is invalid."""


def _version_number(value: object) -> int | None:
    """A library version component from h5p.json: the spec says number, but
    packages exported by the official framework serialize strings ("1")."""
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


@dataclass(frozen=True, slots=True)
class ParsedPackage:
    title: str
    main_library: str
    # Root directory of the main library inside the package (e.g.
    # "H5P.Blanks-1.14"), derived from the preloadedDependencies entry
    main_library_dir: str


@dataclass(frozen=True, slots=True)
class ImportedContent:
    id: int
    title: str
    main_library: str


@dataclass(frozen=True, slots=True)
class ImportResult:
    course_id: int
    activity_id: int
    variant_id: int
    lang: str
    content: ImportedContent


@dataclass(frozen=True, slots=True)
class ReplaceResult:
    imported: ImportResult
    # Storage prefixes of the replaced package, to delete after the commit
    stale_prefixes: tuple[str, ...]


def parse_package_manifest(manifest_bytes: bytes) -> ParsedPackage:
    """Parse an ``h5p.json`` into the fields the player needs."""
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as error:
        raise H5pImportError(f"{PACKAGE_MANIFEST} is not valid JSON: {error}") from error
    if not isinstance(manifest, dict):
        raise H5pImportError(f"{PACKAGE_MANIFEST} must hold a JSON object")

    title = manifest.get("title")
    if not isinstance(title, str) or not title.strip():
        raise H5pImportError(f"{PACKAGE_MANIFEST} declares no 'title'")
    main_library = manifest.get("mainLibrary")
    if not isinstance(main_library, str) or not main_library:
        raise H5pImportError(f"{PACKAGE_MANIFEST} declares no 'mainLibrary'")

    dependencies = manifest.get("preloadedDependencies")
    if not isinstance(dependencies, list):
        raise H5pImportError(f"{PACKAGE_MANIFEST} declares no 'preloadedDependencies'")
    for dependency in dependencies:
        if not isinstance(dependency, dict) or dependency.get("machineName") != main_library:
            continue
        major = _version_number(dependency.get("majorVersion"))
        minor = _version_number(dependency.get("minorVersion"))
        if major is not None and minor is not None:
            return ParsedPackage(
                title=title.strip(),
                main_library=main_library,
                main_library_dir=f"{main_library}-{major}.{minor}",
            )
    raise H5pImportError(
        f"{PACKAGE_MANIFEST} lists no preloadedDependencies entry for its "
        f"mainLibrary {main_library!r}, so the library version is unknown"
    )


@contextmanager
def _open_package(zip_bytes: bytes) -> Iterator[tuple[zipfile.ZipFile, ParsedPackage]]:
    """Open an uploaded ``.h5p`` and validate it, for the duration of the block."""
    try:
        archive = zipfile.ZipFile(BytesIO(zip_bytes))
    except zipfile.BadZipFile as error:
        raise H5pImportError(f"Uploaded file is not a valid zip: {error}") from error
    with archive:
        try:
            manifest_bytes = archive.read(PACKAGE_MANIFEST)
        except KeyError as error:
            raise H5pImportError(f"Package has no {PACKAGE_MANIFEST} at its root") from error
        parsed = parse_package_manifest(manifest_bytes)

        names = set(archive.namelist())
        if CONTENT_ENTRY not in names:
            raise H5pImportError(f"Package has no {CONTENT_ENTRY}")
        if f"{parsed.main_library_dir}/library.json" not in names:
            raise H5pImportError(
                f"Package does not bundle its main library {parsed.main_library_dir!r}; "
                "export the .h5p with libraries included (e.g. from Lumi), packages "
                "exported without libraries cannot be played"
            )
        yield archive, parsed


def _store_package(
    session: Session,
    storage: StorageBackend,
    archive: zipfile.ZipFile,
    parsed: ParsedPackage,
    variant_id: int,
    activity_iri: str,
) -> H5pContent:
    """Store every file of the package under a fresh prefix of the variant and
    create its content row. Each package gets its own prefix, so a replaced
    package's files stay servable until the replacement has committed."""
    storage_prefix = f"{MODULE_NAME}/{variant_id}-{uuid.uuid4().hex}"
    for name in archive.namelist():
        if name.endswith("/"):
            continue
        storage.put(f"{storage_prefix}/{name}", archive.read(name))

    content = H5pContent(
        variant_id=variant_id,
        title=parsed.title,
        main_library=parsed.main_library,
        activity_iri=activity_iri,
        storage_prefix=storage_prefix,
    )
    session.add(content)
    session.flush()
    return content


def _import_result(
    course_id: int, activity_id: int, variant: ActivityVariant, content: H5pContent
) -> ImportResult:
    return ImportResult(
        course_id=course_id,
        activity_id=activity_id,
        variant_id=variant.id,
        lang=variant.lang,
        content=ImportedContent(
            id=content.id,
            title=content.title,
            main_library=content.main_library,
        ),
    )


def _delete_variant_rows(session: Session, variant_id: int) -> tuple[str, ...]:
    """Delete the content rows of a variant and the learner attempts on them;
    returns the storage prefixes their package files live under."""
    contents = session.scalars(select(H5pContent).where(H5pContent.variant_id == variant_id)).all()
    prefixes = tuple(sorted({content.storage_prefix for content in contents}))
    content_ids = [content.id for content in contents]
    if content_ids:
        session.execute(delete(H5pAttempt).where(H5pAttempt.content_id.in_(content_ids)))
    session.execute(delete(H5pContent).where(H5pContent.variant_id == variant_id))
    return prefixes


def import_package(
    session: Session,
    storage: StorageBackend,
    zip_bytes: bytes,
    course_id: int,
    activity_id: int | None,
    lang: str,
) -> ImportResult:
    """Import an ``.h5p`` package as one language variant of a course activity.

    With ``activity_id`` the package becomes the ``lang`` variant of that
    activity (raising ActivityLanguageExistsError when taken); without it a
    new activity titled from the package is appended to the course.
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
        content = _store_package(
            session,
            storage,
            archive,
            parsed,
            variant.id,
            f"{ACTIVITY_IRI_PREFIX}:{uuid.uuid4()}",
        )
    return _import_result(course.id, activity.id, variant, content)


def replace_package(
    session: Session, storage: StorageBackend, zip_bytes: bytes, variant_id: int
) -> ReplaceResult:
    """Replace the package of an H5P variant, keeping the variant itself.

    The variant's content row and every learner attempt on it are deleted, so
    learners start the new package afresh; the progress the activity recorded
    (satisfaction, launches) and the activity IRI the LRS knows the content by
    are kept. Raises ActivityVariantModuleError when another module plays the
    variant. The old package files stay in storage: the caller deletes
    ``stale_prefixes`` once the transaction has committed.
    """
    with _open_package(zip_bytes) as (archive, parsed):
        variant = get_module_variant(session, variant_id, MODULE_NAME)
        activity = get_activity(session, variant.activity_id)
        activity_iri = session.scalars(
            select(H5pContent.activity_iri).where(H5pContent.variant_id == variant.id)
        ).one()
        stale_prefixes = _delete_variant_rows(session, variant.id)
        content = _store_package(session, storage, archive, parsed, variant.id, activity_iri)
    return ReplaceResult(
        imported=_import_result(activity.course_id, activity.id, variant, content),
        stale_prefixes=stale_prefixes,
    )


def purge_variant(session: Session, storage: StorageBackend, variant_id: int) -> None:
    """Remove all H5P content of a variant: its content rows, any learner
    attempts on them, and the stored package files. Runs within the caller's
    transaction (no commit); the caller deletes the variant row and commits."""
    for prefix in _delete_variant_rows(session, variant_id):
        storage.delete_prefix(prefix)
