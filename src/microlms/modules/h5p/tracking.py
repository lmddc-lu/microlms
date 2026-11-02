"""H5P tracking services: attempts and statement observation.

H5P content emits xAPI statements in the browser (``H5P.externalDispatcher``);
the player page forwards them to the runtime endpoint, authenticated by an
opaque per-launch token. This module owns the server side: starting an
attempt, stamping the canonical actor and registration onto the reported
statements before they go to the LRS, and marking the activity variant
satisfied once the root content reports completion (activity progress then
drives enrollment completion).
"""

import secrets
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from microlms.domain.activities import record_variant_satisfied
from microlms.domain.enrollments import get_enrollment
from microlms.events import EventHooks, commit_with_events
from microlms.lrs import LrsClient, build_actor
from microlms.modules.h5p.models import H5pAttempt, H5pContent

VERB_COMPLETED = "http://adlnet.gov/expapi/verbs/completed"
VERB_ANSWERED = "http://adlnet.gov/expapi/verbs/answered"

# Verbs the root content reports when the learner finishes it: content types
# with an internal structure (quizzes, presentations) emit ``completed``,
# single-question types emit ``answered``
SATISFYING_VERB_IDS = frozenset((VERB_COMPLETED, VERB_ANSWERED))


def start_attempt(
    session: Session,
    content: H5pContent,
    user_id: uuid.UUID,
    enrollment_id: int,
    token_expires_at: datetime,
) -> H5pAttempt:
    """Return the enrollment's attempt for this content, creating it on first
    launch. The attempt row is stable per (content, enrollment) so completion
    stays sticky; only the player credential is rotated so stale player
    windows lose access, and it works until ``token_expires_at``. Commits
    itself: the token must be persisted before the launch URL is handed
    out."""
    attempt = session.scalars(
        select(H5pAttempt).where(
            H5pAttempt.content_id == content.id,
            H5pAttempt.enrollment_id == enrollment_id,
        )
    ).first()
    if attempt is None:
        attempt = H5pAttempt(
            id=uuid.uuid4(),
            content_id=content.id,
            user_id=user_id,
            enrollment_id=enrollment_id,
            auth_token=secrets.token_urlsafe(32),
            token_expires_at=token_expires_at,
            satisfied=False,
        )
        session.add(attempt)
    else:
        attempt.auth_token = secrets.token_urlsafe(32)
        attempt.token_expires_at = token_expires_at
    session.commit()
    session.refresh(attempt)
    return attempt


def canonicalize_statements(
    statements: Sequence[Mapping[str, object]],
    attempt: H5pAttempt,
    registration: uuid.UUID,
    actor_home_page: str,
) -> list[dict[str, object]]:
    """Stamp the canonical actor and the enrollment registration onto the
    player-reported statements, overriding whatever the browser sent, so LRS
    identities cannot be spoofed and statements group per enrollment like the
    other content modules' do."""
    actor = build_actor(attempt.user_id, actor_home_page)
    canonical: list[dict[str, object]] = []
    for statement in statements:
        stamped = dict(statement)
        stamped["actor"] = dict(actor)
        context = stamped.get("context")
        stamped["context"] = dict(context) if isinstance(context, Mapping) else {}
        stamped["context"]["registration"] = str(registration)
        canonical.append(stamped)
    return canonical


def _reports_root_satisfied(statement: Mapping[str, object], activity_iri: str) -> bool:
    """Whether a statement reports the root content finished: a satisfying
    verb on the root activity (sub-content statements carry the IRI plus a
    ``subContentId`` query) with a result not explicitly incomplete."""
    verb = statement.get("verb")
    verb_id = verb.get("id") if isinstance(verb, Mapping) else None
    if verb_id not in SATISFYING_VERB_IDS:
        return False
    obj = statement.get("object")
    object_id = obj.get("id") if isinstance(obj, Mapping) else None
    if object_id != activity_iri:
        return False
    result = statement.get("result")
    return not (isinstance(result, Mapping) and result.get("completion") is False)


def observe_statements(
    session: Session,
    hooks: EventHooks,
    lrs: LrsClient,
    attempt: H5pAttempt,
    content: H5pContent,
    statements: Sequence[Mapping[str, object]],
    actor_home_page: str,
) -> None:
    """Forward player-reported statements to the LRS and satisfy the variant
    when the root content reports completion.

    Statements are canonicalized (actor, registration) before forwarding.
    When the root content first reports ``completed``/``answered``, the
    activity variant is recorded as satisfied, which completes the still
    active enrollment once every activity of the course is (emitting
    ``CourseCompleted`` via hooks).
    """
    if not statements:
        return
    registration = get_enrollment(session, attempt.enrollment_id).registration
    canonical = canonicalize_statements(statements, attempt, registration, actor_home_page)
    lrs.post_statements(canonical)

    if attempt.satisfied:
        return
    if not any(_reports_root_satisfied(statement, content.activity_iri) for statement in canonical):
        return
    attempt.satisfied = True
    record_variant_satisfied(session, attempt.enrollment_id, content.variant_id)
    commit_with_events(session, hooks)
