"""LTI resource links and the resource link launch (LTI Core 1.3 section 5).

A resource link places one tool resource in a course as one language variant
of an activity. Launching it follows the OIDC third-party initiated login
(Security Framework 5.1.1): the platform sends the browser to the tool's
login URL with an opaque ``lti_message_hint`` bound to the learner, the tool
answers with an authentication request carrying that hint back, and the
platform posts a signed ``id_token`` holding the launch claims to one of the
tool's registered redirect URIs.
"""

import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from microlms.domain.activities import (
    ActivityNotFoundError,
    add_activity_variant,
    create_activity,
    get_activity,
    get_activity_variant,
)
from microlms.domain.courses import Course, get_course
from microlms.domain.languages import ensure_language_tag
from microlms.domain.users import User
from microlms.modules.lti.models import LtiLaunch, LtiLink, LtiResult, LtiTool
from microlms.modules.lti.security import (
    ID_TOKEN_LIFETIME,
    SCOPE_LINEITEM_READONLY,
    SCOPE_SCORE,
)
from microlms.modules.lti.tools import get_tool
from microlms.storage.base import StorageBackend

MODULE_NAME = "lti"

# Time the learner's browser has to complete the OIDC login round trip
LAUNCH_LIFETIME = timedelta(minutes=5)

LTI_VERSION = "1.3.0"
MESSAGE_TYPE_RESOURCE_LINK = "LtiResourceLinkRequest"
ROLE_LEARNER = "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"
CONTEXT_TYPE_COURSE_OFFERING = "http://purl.imsglobal.org/vocab/lis/v2/course#CourseOffering"

_CLAIM = "https://purl.imsglobal.org/spec/lti/claim"
CLAIM_MESSAGE_TYPE = f"{_CLAIM}/message_type"
CLAIM_VERSION = f"{_CLAIM}/version"
CLAIM_DEPLOYMENT_ID = f"{_CLAIM}/deployment_id"
CLAIM_TARGET_LINK_URI = f"{_CLAIM}/target_link_uri"
CLAIM_RESOURCE_LINK = f"{_CLAIM}/resource_link"
CLAIM_ROLES = f"{_CLAIM}/roles"
CLAIM_CONTEXT = f"{_CLAIM}/context"
CLAIM_CUSTOM = f"{_CLAIM}/custom"
CLAIM_AGS_ENDPOINT = "https://purl.imsglobal.org/spec/lti-ags/claim/endpoint"


class LtiLinkNotFoundError(Exception):
    """Raised when no resource link matches the identifier."""


class LtiLaunchError(Exception):
    """Raised when an authentication request does not match a pending launch."""


@dataclass(frozen=True, slots=True)
class PendingLaunch:
    """A consumed launch: the link and learner the login was started for."""

    link_id: int
    user_id: uuid.UUID


def create_link(
    session: Session,
    course_id: int,
    activity_id: int | None,
    lang: str,
    tool_id: int,
    title: str,
    target_link_uri: str,
    custom: Mapping[str, str],
    score_maximum: float,
) -> LtiLink:
    """Place a tool resource as the ``lang`` variant of a course activity.

    With ``activity_id`` the link becomes the ``lang`` variant of that
    activity (raising ActivityLanguageExistsError when taken); without it a
    new activity titled after the link is appended to the course.
    """
    # Validate the target before creating anything so a bad request leaves
    # no orphan activity behind
    ensure_language_tag(lang)
    course = get_course(session, course_id)
    tool = get_tool(session, tool_id)
    if activity_id is None:
        activity = create_activity(session, course.id, title)
    else:
        activity = get_activity(session, activity_id)
        if activity.course_id != course.id:
            raise ActivityNotFoundError(
                f"Activity {activity_id} does not belong to course {course_id}"
            )
    variant = add_activity_variant(session, activity.id, lang, MODULE_NAME)
    link = LtiLink(
        variant_id=variant.id,
        tool_id=tool.id,
        resource_link_id=str(uuid.uuid4()),
        title=title,
        target_link_uri=target_link_uri,
        custom=dict(custom),
        score_maximum=score_maximum,
    )
    session.add(link)
    session.flush()
    return link


def get_link(session: Session, link_id: int) -> LtiLink:
    link = session.get(LtiLink, link_id)
    if link is None:
        raise LtiLinkNotFoundError(f"No LTI resource link with id {link_id}")
    return link


def get_variant_link(session: Session, variant_id: int) -> LtiLink:
    """The resource link backing an activity variant."""
    link = session.scalars(select(LtiLink).where(LtiLink.variant_id == variant_id)).first()
    if link is None:
        raise LtiLinkNotFoundError(f"Variant {variant_id} holds no LTI resource link")
    return link


def link_course(session: Session, link: LtiLink) -> Course:
    """The course the link's activity belongs to."""
    variant = get_activity_variant(session, link.variant_id)
    return get_course(session, get_activity(session, variant.activity_id).course_id)


def start_launch(session: Session, link: LtiLink, user_id: uuid.UUID, now: datetime) -> LtiLaunch:
    """Open a launch of the link for the learner, returning the pending
    launch whose ``message_hint`` goes to the tool. Expired launches that
    were never completed are dropped first, so abandoned ones do not pile up."""
    session.execute(delete(LtiLaunch).where(LtiLaunch.expires_at < now))
    launch = LtiLaunch(
        message_hint=secrets.token_urlsafe(32),
        link_id=link.id,
        user_id=user_id,
        expires_at=now + LAUNCH_LIFETIME,
    )
    session.add(launch)
    session.flush()
    return launch


def consume_launch(session: Session, message_hint: str, now: datetime) -> PendingLaunch:
    """Delete the pending launch the hint names and return what it was for.

    The delete is the check: a launch can complete once only. Raises
    LtiLaunchError when the hint is unknown, already used, or expired.
    """
    row = session.execute(
        delete(LtiLaunch)
        .where(LtiLaunch.message_hint == message_hint)
        .returning(LtiLaunch.link_id, LtiLaunch.user_id, LtiLaunch.expires_at)
    ).one_or_none()
    if row is None:
        raise LtiLaunchError("lti_message_hint names no pending launch (unknown or already used)")
    link_id, user_id, expires_at = row
    if expires_at <= now:
        raise LtiLaunchError("The launch expired before the tool completed the login")
    return PendingLaunch(link_id=link_id, user_id=user_id)


def build_launch_claims(
    issuer: str,
    tool: LtiTool,
    link: LtiLink,
    course: Course,
    user: User,
    nonce: str,
    lineitem_url: str,
    now: datetime,
) -> dict[str, object]:
    """The id_token claims of a resource link launch (LTI Core 5.3/5.4).

    The learner is identified by ``sub``, their stable platform user id.
    Their name and email are added only for a tool registered to receive them (see
    :func:`identity_claims`). The AGS claim advertises the link's
    single line item with the score and read-only line item scopes.
    """
    claims: dict[str, object] = {
        "iss": issuer,
        "aud": tool.client_id,
        "azp": tool.client_id,
        "sub": str(user.id),
        "iat": int(now.timestamp()),
        "exp": int((now + ID_TOKEN_LIFETIME).timestamp()),
        "nonce": nonce,
        CLAIM_MESSAGE_TYPE: MESSAGE_TYPE_RESOURCE_LINK,
        CLAIM_VERSION: LTI_VERSION,
        CLAIM_DEPLOYMENT_ID: tool.deployment_id,
        CLAIM_TARGET_LINK_URI: link.target_link_uri,
        CLAIM_RESOURCE_LINK: {"id": link.resource_link_id, "title": link.title},
        CLAIM_ROLES: [ROLE_LEARNER],
        CLAIM_CONTEXT: {
            "id": str(course.id),
            "title": course.title,
            "type": [CONTEXT_TYPE_COURSE_OFFERING],
        },
        CLAIM_AGS_ENDPOINT: {
            "scope": [SCOPE_LINEITEM_READONLY, SCOPE_SCORE],
            "lineitem": lineitem_url,
        },
    }
    if link.custom:
        claims[CLAIM_CUSTOM] = dict(link.custom)
    if tool.share_identity:
        claims.update(identity_claims(user))
    return claims


def identity_claims(user: User) -> dict[str, str]:
    """The OIDC standard claims describing the learner (LTI Core 5.3.6).

    The core keeps a single ``full_name``, so ``given_name`` is its first
    word and ``family_name`` the rest (omitted for a one-word name); tools
    such as Moodle build the account's first and last names from these two.
    Empty values are left out rather than sent blank.
    """
    claims: dict[str, str] = {}
    full_name = " ".join(user.full_name.split())
    if full_name:
        given_name, _, family_name = full_name.partition(" ")
        claims["name"] = full_name
        claims["given_name"] = given_name
        if family_name:
            claims["family_name"] = family_name
    if user.email:
        claims["email"] = user.email
    return claims


def purge_variant(session: Session, storage: StorageBackend, variant_id: int) -> None:
    """Remove the resource link of a variant with its pending launches and
    results. Runs within the caller's transaction (no commit); the caller
    deletes the variant row and commits. An LTI link stores no files."""
    link_ids = list(session.scalars(select(LtiLink.id).where(LtiLink.variant_id == variant_id)))
    if link_ids:
        session.execute(delete(LtiLaunch).where(LtiLaunch.link_id.in_(link_ids)))
        session.execute(delete(LtiResult).where(LtiResult.link_id.in_(link_ids)))
    session.execute(delete(LtiLink).where(LtiLink.variant_id == variant_id))
