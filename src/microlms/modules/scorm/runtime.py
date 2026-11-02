"""SCORM runtime services: attempts, commits and SCORM-to-xAPI conversion.

The player page (see ``player.html``) exposes the SCORM runtime object to the
SCO (``API`` for SCORM 1.2, ``API_1484_11`` for SCORM 2004) and reports the
runtime data model back through typed payloads shaped after the SCORM 2004
data model - the player normalizes SCORM 1.2 fields (``cmi.core.*``,
``lesson_status``, HH:MM:SS timespans) before reporting, so this module is
edition-neutral. It owns the server side: starting an attempt, applying
committed cmi data, converting the transitions to xAPI statements per the ADL
SCORM profile (initialized / completed / passed / failed / terminated, plus
responded for each answered cmi.interactions entry), recording the activity's
progress measure from the SCOs' cmi.progress_measure, marking the activity
variant satisfied once every SCO of the package is (activity progress then
drives enrollment completion), and announcing every recorded commit to the
client hooks (``ScormRuntimeCommitted``).
"""

import enum
import re
import secrets
import uuid
from datetime import UTC, datetime
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from microlms.domain.activities import (
    get_activity_variant,
    record_activity_progress_measure,
    record_variant_satisfied,
)
from microlms.domain.enrollments import get_enrollment
from microlms.events import EventHooks, ScormRuntimeCommitted, commit_with_events, queue_event
from microlms.lrs import LrsClient, build_actor
from microlms.modules.scorm.models import (
    CompletionStatus,
    ScormAttempt,
    ScormResourceType,
    ScormSco,
    SuccessStatus,
)

CATEGORY_SCORM_PROFILE = "https://w3id.org/xapi/scorm"
ACTIVITY_TYPE_LESSON = "http://adlnet.gov/expapi/activities/lesson"
ACTIVITY_TYPE_INTERACTION = "http://adlnet.gov/expapi/activities/cmi.interaction"

VERB_INITIALIZED = "http://adlnet.gov/expapi/verbs/initialized"
VERB_COMPLETED = "http://adlnet.gov/expapi/verbs/completed"
VERB_PASSED = "http://adlnet.gov/expapi/verbs/passed"
VERB_FAILED = "http://adlnet.gov/expapi/verbs/failed"
VERB_TERMINATED = "http://adlnet.gov/expapi/verbs/terminated"
VERB_RESPONDED = "http://adlnet.gov/expapi/verbs/responded"

# ISO 8601 duration limited to the day/time components SCOs actually send;
# year/month components are ambiguous (no fixed length) and are rejected
_DURATION_PATTERN = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)


class ScormDurationError(Exception):
    """Raised when a SCO reports a cmi.session_time we cannot interpret."""


def parse_duration_seconds(value: str) -> float:
    """Parse an ISO 8601 day/time duration (``PT1H30M5.5S``) into seconds."""
    match = _DURATION_PATTERN.match(value)
    if match is None or value in ("P", "PT"):
        raise ScormDurationError(
            f"Invalid cmi.session_time {value!r}: expected an ISO 8601 "
            "duration using day/time components, e.g. PT1H30M5S"
        )
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = float(match.group("seconds") or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def format_duration(total_seconds: float) -> str:
    """Format seconds as the ISO 8601 duration the cmi.total_time element uses."""
    whole = int(total_seconds)
    fraction = round(total_seconds - whole, 2)
    hours, remainder = divmod(whole, 3600)
    minutes, seconds = divmod(remainder, 60)
    seconds_text = f"{seconds + fraction:g}" if fraction else str(seconds)
    return f"PT{hours}H{minutes}M{seconds_text}S"


class InteractionType(enum.StrEnum):
    """SCORM 2004 ``cmi.interactions.n.type`` vocabulary.

    The values are identical to the xAPI ``interactionType`` vocabulary, so
    they map through unchanged (scorm-again already enforces this vocabulary
    against the SCO).
    """

    TRUE_FALSE = "true-false"
    CHOICE = "choice"
    FILL_IN = "fill-in"
    LONG_FILL_IN = "long-fill-in"
    MATCHING = "matching"
    PERFORMANCE = "performance"
    SEQUENCING = "sequencing"
    LIKERT = "likert"
    NUMERIC = "numeric"
    OTHER = "other"


class RuntimeInteraction(BaseModel):
    """One ``cmi.interactions`` entry as exported by the player."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=4000)
    type: InteractionType | None
    learner_response: str | None = Field(max_length=8000)
    # cmi result vocabulary (correct/incorrect/unanticipated/neutral) or a
    # stringified number for numeric results
    result: str | None = Field(max_length=200)
    # cmi.interactions.n.correct_responses.m.pattern values, in order
    correct_responses: list[str] = Field(max_length=100)
    description: str | None = Field(max_length=8000)
    # cmi.interactions.n.latency, an ISO 8601 duration
    latency: str | None = Field(max_length=100)


class RuntimeCommit(BaseModel):
    """The cmi fields the player reports on Commit."""

    model_config = ConfigDict(extra="forbid")

    completion_status: CompletionStatus
    success_status: SuccessStatus
    score_scaled: float | None = Field(ge=-1.0, le=1.0)
    score_raw: float | None
    # cmi.progress_measure; always None for SCORM 1.2, which has no such element
    progress_measure: float | None = Field(ge=0.0, le=1.0)
    location: str | None = Field(max_length=1000)
    suspend_data: str | None = Field(max_length=64000)
    # The full cmi.interactions journal of the session so far; the server
    # emits statements only for entries beyond what it already reported
    interactions: list[RuntimeInteraction] = Field(max_length=1000)


class RuntimeTermination(RuntimeCommit):
    """The final Commit of a session, sent by Terminate."""

    # cmi.session_time as reported by the SCO; accumulated into total_time
    session_time: str
    # cmi.exit; "suspend" makes the next launch resume this attempt
    exit_mode: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _statement(
    actor: dict[str, object],
    verb_id: str,
    verb_display: str,
    sco: ScormSco,
    registration: uuid.UUID,
    result: dict[str, object] | None,
) -> dict[str, object]:
    statement: dict[str, object] = {
        "actor": actor,
        "verb": {"id": verb_id, "display": {"en-US": verb_display}},
        "object": {
            "objectType": "Activity",
            "id": sco.activity_id,
            "definition": {
                "type": ACTIVITY_TYPE_LESSON,
                "name": {"en-US": sco.title},
            },
        },
        "context": {
            "registration": str(registration),
            "contextActivities": {
                "category": [{"id": CATEGORY_SCORM_PROFILE}],
                "grouping": [{"id": sco.course_activity_id}],
            },
        },
        "timestamp": _now(),
    }
    if result is not None:
        statement["result"] = result
    return statement


def _interaction_statement(
    actor: dict[str, object],
    sco: ScormSco,
    registration: uuid.UUID,
    interaction: RuntimeInteraction,
) -> dict[str, object]:
    """A ``responded`` statement for one interaction, per the ADL SCORM profile.

    The interaction is its own activity under the SCO's IRI, the learner
    response goes to ``result.response``, and the SCO/course land in the
    context as parent/grouping.
    """
    definition: dict[str, object] = {"type": ACTIVITY_TYPE_INTERACTION}
    if interaction.description is not None:
        definition["description"] = {"en-US": interaction.description}
    if interaction.type is not None:
        definition["interactionType"] = interaction.type.value
        # The xAPI spec ties correctResponsesPattern to interaction activities
        # carrying an interactionType, so it is only included alongside one
        if interaction.correct_responses:
            definition["correctResponsesPattern"] = list(interaction.correct_responses)

    result: dict[str, object] = {}
    if interaction.learner_response is not None:
        result["response"] = interaction.learner_response
    if interaction.result == "correct":
        result["success"] = True
    elif interaction.result == "incorrect":
        result["success"] = False
    if interaction.latency is not None:
        result["duration"] = interaction.latency

    return {
        "actor": actor,
        "verb": {"id": VERB_RESPONDED, "display": {"en-US": "responded"}},
        "object": {
            "objectType": "Activity",
            "id": f"{sco.activity_id}/interaction/{quote(interaction.id, safe='')}",
            "definition": definition,
        },
        "context": {
            "registration": str(registration),
            "contextActivities": {
                "category": [{"id": CATEGORY_SCORM_PROFILE}],
                "parent": [{"id": sco.activity_id}],
                "grouping": [{"id": sco.course_activity_id}],
            },
        },
        "timestamp": _now(),
        **({"result": result} if result else {}),
    }


def _score_result(commit: RuntimeCommit) -> dict[str, object]:
    score: dict[str, object] = {}
    if commit.score_scaled is not None:
        score["scaled"] = commit.score_scaled
    if commit.score_raw is not None:
        score["raw"] = commit.score_raw
    return {"score": score} if score else {}


def start_attempt(
    session: Session,
    lrs: LrsClient,
    sco: ScormSco,
    user_id: uuid.UUID,
    enrollment_id: int,
    actor_home_page: str,
    token_expires_at: datetime,
) -> ScormAttempt:
    """Return the enrollment's attempt for this SCO, creating it on first launch.

    The attempt row is stable per (SCO, enrollment) so relaunching resumes the
    previously committed runtime data; only the player credential is rotated so
    stale player windows lose access. The new credential works until
    ``token_expires_at`` or the session's Terminate, whichever comes first.
    Emits an ``initialized`` statement.
    """
    attempt = session.scalars(
        select(ScormAttempt).where(
            ScormAttempt.sco_id == sco.id,
            ScormAttempt.enrollment_id == enrollment_id,
        )
    ).first()
    if attempt is None:
        attempt = ScormAttempt(
            id=uuid.uuid4(),
            sco_id=sco.id,
            user_id=user_id,
            enrollment_id=enrollment_id,
            auth_token=secrets.token_urlsafe(32),
            token_expires_at=token_expires_at,
            completion_status=CompletionStatus.UNKNOWN,
            success_status=SuccessStatus.UNKNOWN,
            score_scaled=None,
            score_raw=None,
            progress_measure=None,
            location=None,
            suspend_data=None,
            total_time_seconds=0.0,
            suspended=False,
            satisfied=False,
            interactions_reported=0,
        )
        session.add(attempt)
    else:
        attempt.auth_token = secrets.token_urlsafe(32)
        attempt.token_expires_at = token_expires_at
        # The runtime interactions array starts empty on every launch (it is
        # not seeded from the attempt), so the journal cursor restarts too
        attempt.interactions_reported = 0
    session.commit()
    session.refresh(attempt)

    actor = build_actor(user_id, actor_home_page)
    registration = get_enrollment(session, enrollment_id).registration
    lrs.post_statement(_statement(actor, VERB_INITIALIZED, "initialized", sco, registration, None))
    return attempt


def _apply_commit(attempt: ScormAttempt, commit: RuntimeCommit) -> tuple[bool, bool]:
    """Copy the reported cmi fields onto the attempt.

    Returns (newly_completed, success_transition): whether completion was
    reached for the first time, and whether success left ``unknown`` (the SCO
    committing passed or failed for the first time).
    """
    newly_completed = (
        commit.completion_status is CompletionStatus.COMPLETED
        and attempt.completion_status is not CompletionStatus.COMPLETED
    )
    success_transition = (
        commit.success_status is not SuccessStatus.UNKNOWN
        and attempt.success_status is SuccessStatus.UNKNOWN
    )
    attempt.completion_status = commit.completion_status
    attempt.success_status = commit.success_status
    attempt.score_scaled = commit.score_scaled
    attempt.score_raw = commit.score_raw
    attempt.progress_measure = commit.progress_measure
    attempt.location = commit.location
    attempt.suspend_data = commit.suspend_data
    return newly_completed, success_transition


def _all_scos_satisfied(session: Session, sco: ScormSco, enrollment_id: int) -> bool:
    """Whether every trackable SCO of the variant is satisfied for the enrollment."""
    sco_ids = set(
        session.scalars(
            select(ScormSco.id).where(
                ScormSco.variant_id == sco.variant_id,
                ScormSco.scorm_type == ScormResourceType.SCO,
            )
        )
    )
    satisfied_ids = set(
        session.scalars(
            select(ScormAttempt.sco_id).where(
                ScormAttempt.enrollment_id == enrollment_id,
                ScormAttempt.satisfied.is_(True),
            )
        )
    )
    return sco_ids <= satisfied_ids


def _variant_progress_measure(session: Session, sco: ScormSco, enrollment_id: int) -> float:
    """The package's progress for the enrollment: the mean over its trackable
    SCOs of 1 for a satisfied SCO, else its cmi.progress_measure, else 0."""
    sco_ids = list(
        session.scalars(
            select(ScormSco.id).where(
                ScormSco.variant_id == sco.variant_id,
                ScormSco.scorm_type == ScormResourceType.SCO,
            )
        )
    )
    attempts = {
        attempt.sco_id: attempt
        for attempt in session.scalars(
            select(ScormAttempt).where(
                ScormAttempt.enrollment_id == enrollment_id,
                ScormAttempt.sco_id.in_(sco_ids),
            )
        )
    }

    def sco_measure(sco_id: int) -> float:
        attempt = attempts.get(sco_id)
        if attempt is None:
            return 0.0
        if attempt.satisfied:
            return 1.0
        return attempt.progress_measure if attempt.progress_measure is not None else 0.0

    return sum(sco_measure(sco_id) for sco_id in sco_ids) / len(sco_ids)


def _announce_commit(
    session: Session, hooks: EventHooks, attempt: ScormAttempt, sco: ScormSco
) -> None:
    """Dispatch ``ScormRuntimeCommitted`` for a commit already recorded.

    Runs last, after the statements are posted, so a failing client hook
    never loses the recorded runtime data, the statements or the
    satisfaction.
    """
    queue_event(
        session,
        ScormRuntimeCommitted(
            enrollment_id=attempt.enrollment_id,
            activity_id=get_activity_variant(session, sco.variant_id).activity_id,
            sco_id=sco.id,
            suspend_data=attempt.suspend_data,
            package_storage_prefix=sco.storage_prefix,
        ),
    )
    commit_with_events(session, hooks)


def _record(
    session: Session,
    hooks: EventHooks,
    lrs: LrsClient,
    attempt: ScormAttempt,
    sco: ScormSco,
    commit: RuntimeCommit,
    actor_home_page: str,
) -> list[dict[str, object]]:
    """Apply a commit and return the statements its transitions produced.

    A commit carrying cmi.progress_measure updates the activity's progress
    measure (see ``_variant_progress_measure``). A SCO attempt is satisfied
    once it is completed or passed; when the last
    trackable SCO of the package becomes satisfied, the activity variant is
    recorded as satisfied, which completes the still active enrollment once
    every activity of the course is (emitting ``CourseCompleted`` via hooks).

    Commits carry the session's full cmi.interactions journal; entries beyond
    the attempt's cursor are new and become ``responded`` statements. Entries
    a SCO rewrites in place are deliberately not re-reported.
    """
    newly_completed, success_transition = _apply_commit(attempt, commit)

    new_interactions: list[RuntimeInteraction] = []
    if len(commit.interactions) > attempt.interactions_reported:
        new_interactions = commit.interactions[attempt.interactions_reported :]
        attempt.interactions_reported = len(commit.interactions)

    newly_satisfied = not attempt.satisfied and (
        attempt.completion_status is CompletionStatus.COMPLETED
        or attempt.success_status is SuccessStatus.PASSED
    )
    if newly_satisfied:
        attempt.satisfied = True
    if commit.progress_measure is not None:
        record_activity_progress_measure(
            session,
            attempt.enrollment_id,
            get_activity_variant(session, sco.variant_id).activity_id,
            _variant_progress_measure(session, sco, attempt.enrollment_id),
        )
    session.commit()

    actor = build_actor(attempt.user_id, actor_home_page)
    registration = get_enrollment(session, attempt.enrollment_id).registration
    statements: list[dict[str, object]] = [
        _interaction_statement(actor, sco, registration, interaction)
        for interaction in new_interactions
    ]
    if newly_completed:
        result: dict[str, object] = {"completion": True, **_score_result(commit)}
        statements.append(_statement(actor, VERB_COMPLETED, "completed", sco, registration, result))
    if success_transition:
        passed = commit.success_status is SuccessStatus.PASSED
        verb_id = VERB_PASSED if passed else VERB_FAILED
        verb_display = "passed" if passed else "failed"
        result = {"success": passed, **_score_result(commit)}
        statements.append(_statement(actor, verb_id, verb_display, sco, registration, result))

    if newly_satisfied and _all_scos_satisfied(session, sco, attempt.enrollment_id):
        record_variant_satisfied(session, attempt.enrollment_id, sco.variant_id)
        commit_with_events(session, hooks)

    return statements


def record_commit(
    session: Session,
    hooks: EventHooks,
    lrs: LrsClient,
    attempt: ScormAttempt,
    sco: ScormSco,
    commit: RuntimeCommit,
    actor_home_page: str,
) -> None:
    """Persist a mid-session Commit, post the resulting statements, then
    announce the commit to the client hooks."""
    for statement in _record(session, hooks, lrs, attempt, sco, commit, actor_home_page):
        lrs.post_statement(statement)
    _announce_commit(session, hooks, attempt, sco)


def record_termination(
    session: Session,
    hooks: EventHooks,
    lrs: LrsClient,
    attempt: ScormAttempt,
    sco: ScormSco,
    termination: RuntimeTermination,
    actor_home_page: str,
) -> None:
    """Persist the final Commit of a session and post a ``terminated`` statement.

    The reported session time is accumulated into the attempt's total time,
    and ``cmi.exit == "suspend"`` marks the attempt so the next launch enters
    with ``cmi.entry == "resume"``. The session is over, so its player token
    expires in the same commit: a late or replayed report cannot overwrite
    the final state, and resuming takes a new launch. The commit is announced
    to the client hooks last, as for a mid-session Commit.
    """
    session_seconds = parse_duration_seconds(termination.session_time)
    statements = _record(session, hooks, lrs, attempt, sco, termination, actor_home_page)

    attempt.total_time_seconds += session_seconds
    attempt.suspended = termination.exit_mode == "suspend"
    attempt.token_expires_at = datetime.now(UTC)
    session.commit()

    actor = build_actor(attempt.user_id, actor_home_page)
    registration = get_enrollment(session, attempt.enrollment_id).registration
    statements.append(
        _statement(
            actor,
            VERB_TERMINATED,
            "terminated",
            sco,
            registration,
            {"duration": format_duration(session_seconds)},
        )
    )
    for statement in statements:
        lrs.post_statement(statement)
    _announce_commit(session, hooks, attempt, sco)
