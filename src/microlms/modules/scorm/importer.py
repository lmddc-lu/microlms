"""SCORM package import: parse ``imsmanifest.xml`` and store the package files.

Importing a package attaches one language variant to a course activity
(creating the activity when none is targeted) and creates one
``lms_scorm_sco`` per launchable item of the default organization, storing
every file in the zip under a storage prefix of its own so the SCOs can be
served and launched later. A variant's package can later be replaced in
place, keeping the variant and the activity progress recorded through it.
SCORM 1.2 and SCORM 2004 packages are supported; the manifest's
``<schemaversion>`` decides the edition, which is stored on every SCO so
launch serves the matching runtime API.
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
from microlms.modules.scorm.models import (
    ScormAttempt,
    ScormResourceType,
    ScormSco,
    ScormVersion,
)
from microlms.storage.base import StorageBackend

MODULE_NAME = "scorm"

# IMS Content Packaging namespaces: SCORM 2004 manifests use the imsglobal
# one, SCORM 1.2 manifests the older imsproject one. The root tag decides
# which namespace the manifest's elements live in.
CP_2004_NS = "http://www.imsglobal.org/xsd/imscp_v1p1"
CP_12_NS = "http://www.imsproject.org/xsd/imscp_rootv1p1p2"
CP_NAMESPACES = (CP_2004_NS, CP_12_NS)
ADLCP_2004_NS = "http://www.adlnet.org/xsd/adlcp_v1p3"
ADLCP_12_NS = "http://www.adlnet.org/xsd/adlcp_rootv1p2"
# The adlcp scormType attribute per edition; SCORM 1.2 spells it lowercase,
# but authoring tools also emit the 2004 casing, so both are accepted
SCORM_TYPE_ATTRS: dict[ScormVersion, tuple[str, ...]] = {
    ScormVersion.SCORM_2004: (f"{{{ADLCP_2004_NS}}}scormType",),
    ScormVersion.SCORM_12: (f"{{{ADLCP_12_NS}}}scormtype", f"{{{ADLCP_12_NS}}}scormType"),
}
XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"
MANIFEST_ENTRY = "imsmanifest.xml"
ACTIVITY_IRI_PREFIX = "urn:microlms:scorm"


class ScormImportError(Exception):
    """Raised when a SCORM package is missing or its manifest is invalid."""


@dataclass(frozen=True, slots=True)
class ParsedItem:
    identifier: str
    title: str
    scorm_type: ScormResourceType
    launch_url: str


@dataclass(frozen=True, slots=True)
class ParsedManifest:
    identifier: str
    title: str
    version: ScormVersion
    items: tuple[ParsedItem, ...]


@dataclass(frozen=True, slots=True)
class ImportedSco:
    id: int
    identifier: str
    title: str
    scorm_type: ScormResourceType


@dataclass(frozen=True, slots=True)
class ImportResult:
    course_id: int
    activity_id: int
    variant_id: int
    lang: str
    version: ScormVersion
    scos: tuple[ImportedSco, ...]


@dataclass(frozen=True, slots=True)
class ReplaceResult:
    imported: ImportResult
    # Storage prefixes of the replaced package, to delete after the commit
    stale_prefixes: tuple[str, ...]


def _cp(cp_ns: str, name: str) -> str:
    return f"{{{cp_ns}}}{name}"


def _text(cp_ns: str, parent: ElementTree.Element, tag: str) -> str:
    element = parent.find(_cp(cp_ns, tag))
    if element is None or element.text is None:
        return ""
    return element.text.strip()


def _cp_namespace(root: ElementTree.Element) -> str:
    for cp_ns in CP_NAMESPACES:
        if root.tag == _cp(cp_ns, "manifest"):
            return cp_ns
    raise ScormImportError(
        f"imsmanifest.xml root {root.tag!r} is not an IMS CP <manifest> in a "
        "known namespace (SCORM 1.2 uses imscp_rootv1p1p2, SCORM 2004 imscp_v1p1)"
    )


def _schema_version(cp_ns: str, root: ElementTree.Element) -> ScormVersion:
    metadata = root.find(_cp(cp_ns, "metadata"))
    version = _text(cp_ns, metadata, "schemaversion") if metadata is not None else ""
    if not version:
        raise ScormImportError(
            "imsmanifest.xml declares no <schemaversion>; "
            "SCORM 1.2 and SCORM 2004 packages are supported"
        )
    if version == "1.2":
        return ScormVersion.SCORM_12
    if "2004" in version or version == "CAM 1.3":
        return ScormVersion.SCORM_2004
    raise ScormImportError(
        f"Unsupported SCORM version {version!r}: "
        "only SCORM 1.2 and SCORM 2004 packages are supported"
    )


def _default_organization(cp_ns: str, root: ElementTree.Element) -> ElementTree.Element:
    organizations = root.find(_cp(cp_ns, "organizations"))
    if organizations is None:
        raise ScormImportError("imsmanifest.xml has no <organizations> element")
    default_id = organizations.get("default")
    candidates = organizations.findall(_cp(cp_ns, "organization"))
    if not candidates:
        raise ScormImportError("imsmanifest.xml declares no <organization>")
    for organization in candidates:
        if organization.get("identifier") == default_id:
            return organization
    return candidates[0]


def _join_url(*parts: str) -> str:
    """Concatenate xml:base fragments and an href into one relative URL."""
    segments = [part.strip("/") for part in parts if part]
    return "/".join(segments)


def _launch_url(base: str, href: str, parameters: str | None) -> str:
    url = _join_url(base, href) if not href.startswith(("http://", "https://")) else href
    if parameters is None:
        return url
    query = parameters.lstrip("?&")
    if not query:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{query}"


def _resources_by_identifier(
    cp_ns: str,
    root: ElementTree.Element,
) -> tuple[str, dict[str, ElementTree.Element]]:
    resources = root.find(_cp(cp_ns, "resources"))
    if resources is None:
        raise ScormImportError("imsmanifest.xml has no <resources> element")
    by_identifier: dict[str, ElementTree.Element] = {}
    for resource in resources.findall(_cp(cp_ns, "resource")):
        identifier = resource.get("identifier")
        if identifier is not None:
            by_identifier[identifier] = resource
    return resources.get(XML_BASE, ""), by_identifier


def _scorm_type(resource: ElementTree.Element, version: ScormVersion) -> str | None:
    for attribute in SCORM_TYPE_ATTRS[version]:
        value = resource.get(attribute)
        if value is not None:
            return value
    return None


def _parse_item(
    item: ElementTree.Element,
    cp_ns: str,
    version: ScormVersion,
    resources_base: str,
    resources: dict[str, ElementTree.Element],
) -> ParsedItem:
    identifier = item.get("identifier")
    reference = item.get("identifierref")
    if identifier is None or reference is None:
        raise ScormImportError("Launchable <item> is missing identifier attributes")
    resource = resources.get(reference)
    if resource is None:
        raise ScormImportError(f"Item {identifier!r} references unknown resource {reference!r}")
    href = resource.get("href")
    if href is None:
        raise ScormImportError(
            f"Resource {reference!r} referenced by item {identifier!r} has no href"
        )
    scorm_type = _scorm_type(resource, version)
    if scorm_type not in (ScormResourceType.SCO.value, ScormResourceType.ASSET.value):
        raise ScormImportError(
            f"Resource {reference!r} has missing or invalid adlcp:scormType "
            f"{scorm_type!r} (expected 'sco' or 'asset')"
        )
    base = _join_url(resources_base, resource.get(XML_BASE, ""))
    return ParsedItem(
        identifier=identifier,
        title=_text(cp_ns, item, "title"),
        scorm_type=ScormResourceType(scorm_type),
        launch_url=_launch_url(base, href, item.get("parameters")),
    )


def _collect_items(
    parent: ElementTree.Element,
    cp_ns: str,
    version: ScormVersion,
    resources_base: str,
    resources: dict[str, ElementTree.Element],
) -> list[ParsedItem]:
    """Collect launchable items in document order, descending into groupings."""
    items: list[ParsedItem] = []
    for item in parent.findall(_cp(cp_ns, "item")):
        if item.get("identifierref") is not None:
            items.append(_parse_item(item, cp_ns, version, resources_base, resources))
        items.extend(_collect_items(item, cp_ns, version, resources_base, resources))
    return items


def parse_manifest(xml_bytes: bytes) -> ParsedManifest:
    """Parse a SCORM 1.2 or 2004 ``imsmanifest.xml`` into its launchable items."""
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as error:
        raise ScormImportError(f"imsmanifest.xml is not valid XML: {error}") from error

    cp_ns = _cp_namespace(root)
    identifier = root.get("identifier")
    if identifier is None:
        raise ScormImportError("imsmanifest.xml <manifest> root has no 'identifier'")
    version = _schema_version(cp_ns, root)

    organization = _default_organization(cp_ns, root)
    resources_base, resources = _resources_by_identifier(cp_ns, root)
    items = _collect_items(organization, cp_ns, version, resources_base, resources)
    if not any(item.scorm_type is ScormResourceType.SCO for item in items):
        raise ScormImportError(
            "The default organization declares no item launching a 'sco' "
            "resource, so nothing can be tracked"
        )

    return ParsedManifest(
        identifier=identifier,
        title=_text(cp_ns, organization, "title"),
        version=version,
        items=tuple(items),
    )


@contextmanager
def _open_package(zip_bytes: bytes) -> Iterator[tuple[zipfile.ZipFile, ParsedManifest]]:
    """Open an uploaded zip and parse its manifest, for the duration of the block."""
    try:
        archive = zipfile.ZipFile(BytesIO(zip_bytes))
    except zipfile.BadZipFile as error:
        raise ScormImportError(f"Uploaded file is not a valid zip: {error}") from error
    with archive:
        try:
            xml_bytes = archive.read(MANIFEST_ENTRY)
        except KeyError as error:
            raise ScormImportError(f"Package has no {MANIFEST_ENTRY} at its root") from error
        yield archive, parse_manifest(xml_bytes)


def _store_package(
    session: Session,
    storage: StorageBackend,
    archive: zipfile.ZipFile,
    parsed: ParsedManifest,
    variant_id: int,
) -> list[ScormSco]:
    """Store every file of the package under a fresh prefix of the variant and
    create its SCOs. Each package gets its own prefix, so a replaced package's
    files stay servable until the replacement has committed."""
    storage_prefix = f"{MODULE_NAME}/{variant_id}-{uuid.uuid4().hex}"
    for name in archive.namelist():
        if name.endswith("/"):
            continue
        storage.put(f"{storage_prefix}/{name}", archive.read(name))

    course_activity_id = f"{ACTIVITY_IRI_PREFIX}:{parsed.identifier}"
    scos = [
        ScormSco(
            variant_id=variant_id,
            version=parsed.version,
            sco_index=index,
            identifier=item.identifier,
            title=item.title,
            scorm_type=item.scorm_type,
            launch_url=item.launch_url,
            activity_id=f"{course_activity_id}:{item.identifier}",
            course_activity_id=course_activity_id,
            storage_prefix=storage_prefix,
        )
        for index, item in enumerate(parsed.items)
    ]
    session.add_all(scos)
    session.flush()
    return scos


def _import_result(
    course_id: int,
    activity_id: int,
    variant: ActivityVariant,
    version: ScormVersion,
    scos: list[ScormSco],
) -> ImportResult:
    return ImportResult(
        course_id=course_id,
        activity_id=activity_id,
        variant_id=variant.id,
        lang=variant.lang,
        version=version,
        scos=tuple(
            ImportedSco(
                id=sco.id,
                identifier=sco.identifier,
                title=sco.title,
                scorm_type=sco.scorm_type,
            )
            for sco in scos
        ),
    )


def _delete_variant_rows(session: Session, variant_id: int) -> tuple[str, ...]:
    """Delete the SCOs of a variant and the learner attempts on them; returns
    the storage prefixes their package files live under."""
    scos = session.scalars(select(ScormSco).where(ScormSco.variant_id == variant_id)).all()
    prefixes = tuple(sorted({sco.storage_prefix for sco in scos}))
    sco_ids = [sco.id for sco in scos]
    if sco_ids:
        session.execute(delete(ScormAttempt).where(ScormAttempt.sco_id.in_(sco_ids)))
    session.execute(delete(ScormSco).where(ScormSco.variant_id == variant_id))
    return prefixes


def import_package(
    session: Session,
    storage: StorageBackend,
    zip_bytes: bytes,
    course_id: int,
    activity_id: int | None,
    lang: str,
) -> ImportResult:
    """Import a SCORM package as one language variant of a course activity.

    With ``activity_id`` the package becomes the ``lang`` variant of that
    activity (raising ActivityLanguageExistsError when taken); without it a
    new activity titled from the manifest is appended to the course.
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
        scos = _store_package(session, storage, archive, parsed, variant.id)
    return _import_result(course.id, activity.id, variant, parsed.version, scos)


def replace_package(
    session: Session, storage: StorageBackend, zip_bytes: bytes, variant_id: int
) -> ReplaceResult:
    """Replace the package of a scorm variant, keeping the variant itself.

    The variant's SCOs and every learner attempt on them are deleted, so
    learners start the new package afresh; the progress the activity recorded
    (satisfaction, launches) is kept. Raises ActivityVariantModuleError when
    another module plays the variant. The old package files stay in storage:
    the caller deletes ``stale_prefixes`` once the transaction has committed.
    """
    with _open_package(zip_bytes) as (archive, parsed):
        variant = get_module_variant(session, variant_id, MODULE_NAME)
        activity = get_activity(session, variant.activity_id)
        stale_prefixes = _delete_variant_rows(session, variant.id)
        scos = _store_package(session, storage, archive, parsed, variant.id)
    return ReplaceResult(
        imported=_import_result(activity.course_id, activity.id, variant, parsed.version, scos),
        stale_prefixes=stale_prefixes,
    )


def purge_variant(session: Session, storage: StorageBackend, variant_id: int) -> None:
    """Remove all scorm content of a variant: its SCOs, any learner attempts on
    them, and the stored package files. Runs within the caller's transaction
    (no commit); the caller deletes the variant row and commits."""
    for prefix in _delete_variant_rows(session, variant_id):
        storage.delete_prefix(prefix)
