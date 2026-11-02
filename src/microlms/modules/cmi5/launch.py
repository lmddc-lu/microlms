"""cmi5 launch and statement logic.

Pure builders for the launch URL, the ``LMS.LaunchData`` State document and the
LMS-generated statements, plus the two services that bracket a session: starting
it (write launch data, emit ``launched``) and observing AU statements to evaluate
``moveOn``, record the activity's progress measure and drive enrollment
completion.
"""

import json
import secrets
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from microlms.domain.activities import (
    get_activity_variant,
    record_activity_progress_measure,
    record_variant_satisfied,
)
from microlms.domain.enrollments import get_enrollment
from microlms.events import EventHooks, commit_with_events
from microlms.lrs import LrsClient, build_actor
from microlms.modules.cmi5.models import Cmi5Au, Cmi5Session, LaunchMode, MoveOn

CATEGORY_CMI5 = "https://w3id.org/xapi/cmi5/context/categories/cmi5"
SESSIONID_EXTENSION = "https://w3id.org/xapi/cmi5/context/extensions/sessionid"
# cmi5 spec 9.5.5.1: an integer 0 to 100, the AU's level of completion; an AU
# may set it on any statement
PROGRESS_EXTENSION = "https://w3id.org/xapi/cmi5/result/extensions/progress"
LAUNCH_DATA_STATE_ID = "LMS.LaunchData"

VERB_LAUNCHED = "http://adlnet.gov/expapi/verbs/launched"
VERB_COMPLETED = "http://adlnet.gov/expapi/verbs/completed"
VERB_PASSED = "http://adlnet.gov/expapi/verbs/passed"
VERB_FAILED = "http://adlnet.gov/expapi/verbs/failed"
VERB_TERMINATED = "http://adlnet.gov/expapi/verbs/terminated"
VERB_SATISFIED = "https://w3id.org/xapi/adl/verbs/satisfied"
VERB_VOIDED = "http://adlnet.gov/expapi/verbs/voided"


class Cmi5ProgressError(Exception):
    """Raised when an AU statement carries a progress result extension that is
    not an integer from 0 to 100."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def build_launch_data(au: Cmi5Au, session: Cmi5Session) -> dict[str, object]:
    """The ``LMS.LaunchData`` State document the AU reads on start."""
    data: dict[str, object] = {
        "contextTemplate": {
            "contextActivities": {"grouping": [{"id": au.publisher_id}]},
            "extensions": {SESSIONID_EXTENSION: str(session.id)},
        },
        "launchMode": session.launch_mode.value,
        "moveOn": au.move_on.value,
    }
    if au.mastery_score is not None:
        data["masteryScore"] = au.mastery_score
    return data


def _context(au: Cmi5Au, session: Cmi5Session) -> dict[str, object]:
    return {
        "registration": str(session.registration),
        "contextActivities": {
            "category": [{"id": CATEGORY_CMI5}],
            "grouping": [{"id": au.publisher_id}],
        },
        "extensions": {SESSIONID_EXTENSION: str(session.id)},
    }


def _statement(
    actor: Mapping[str, object],
    verb_id: str,
    verb_display: str,
    au: Cmi5Au,
    session: Cmi5Session,
) -> dict[str, object]:
    return {
        "actor": dict(actor),
        "verb": {"id": verb_id, "display": {"en-US": verb_display}},
        "object": {"objectType": "Activity", "id": au.publisher_id},
        "context": _context(au, session),
        "timestamp": _now(),
    }


def build_launched_statement(
    actor: Mapping[str, object], au: Cmi5Au, session: Cmi5Session
) -> dict[str, object]:
    return _statement(actor, VERB_LAUNCHED, "launched", au, session)


def build_satisfied_statement(
    actor: Mapping[str, object], au: Cmi5Au, session: Cmi5Session
) -> dict[str, object]:
    return _statement(actor, VERB_SATISFIED, "satisfied", au, session)


def build_launch_url(
    api_base_url: str, au: Cmi5Au, session: Cmi5Session, actor: Mapping[str, object]
) -> str:
    """The AU content URL with the five required cmi5 launch parameters.

    ``api_base_url`` is the deployment origin plus the API prefix the cmi5
    module mounts under (e.g. ``https://lms.example.com/api/v1``).
    """
    base = api_base_url.rstrip("/")
    content_url = f"{base}/cmi5/content/{session.id}/{au.launch_url}"
    params = {
        "endpoint": f"{base}/cmi5/sessions/{session.id}/xapi",
        "fetch": f"{base}/cmi5/sessions/{session.id}/fetch",
        "actor": json.dumps(actor),
        "activityId": au.publisher_id,
        "registration": str(session.registration),
    }
    separator = "&" if "?" in content_url else "?"
    return f"{content_url}{separator}{urlencode(params)}"


def start_session(
    session: Session,
    lrs: LrsClient,
    au: Cmi5Au,
    user_id: uuid.UUID,
    enrollment_id: int,
    api_base_url: str,
    actor_home_page: str,
    expires_at: datetime,
) -> tuple[Cmi5Session, str]:
    """Create a launch session, write its launch data, emit ``launched``.

    The registration is the enrollment's own (a cmi5 registration is one
    attempt at the course, shared across launches and AUs), so relaunching
    resumes: the AU finds the State documents it saved under that registration.
    Each launch is still its own session with its own token and session id,
    which serve content and relay xAPI until ``expires_at`` or the AU's
    ``terminated``, whichever comes first.

    Returns the persisted session and the fully built AU launch URL.
    """
    registration = get_enrollment(session, enrollment_id).registration
    cmi5_session = Cmi5Session(
        id=uuid.uuid4(),
        au_id=au.id,
        user_id=user_id,
        enrollment_id=enrollment_id,
        registration=registration,
        auth_token=secrets.token_urlsafe(32),
        expires_at=expires_at,
        launch_mode=LaunchMode.NORMAL,
        fetched=False,
        completed=False,
        passed=None,
        satisfied=False,
        terminated=False,
        progress_measure=None,
    )
    session.add(cmi5_session)
    session.commit()
    session.refresh(cmi5_session)

    actor = build_actor(user_id, actor_home_page)
    lrs.put_state(
        au.publisher_id,
        actor,
        str(cmi5_session.registration),
        LAUNCH_DATA_STATE_ID,
        build_launch_data(au, cmi5_session),
    )
    lrs.post_statement(build_launched_statement(actor, au, cmi5_session))
    launch_url = build_launch_url(api_base_url, au, cmi5_session, actor)
    return cmi5_session, launch_url


def _is_satisfied(move_on: MoveOn, cmi5_session: Cmi5Session) -> bool:
    passed = cmi5_session.passed is True
    completed = cmi5_session.completed
    match move_on:
        case MoveOn.PASSED:
            return passed
        case MoveOn.COMPLETED:
            return completed
        case MoveOn.COMPLETED_AND_PASSED:
            return completed and passed
        case MoveOn.COMPLETED_OR_PASSED:
            return completed or passed
        case MoveOn.NOT_APPLICABLE:
            return cmi5_session.terminated


def _all_aus_satisfied(session: Session, au: Cmi5Au, enrollment_id: int) -> bool:
    """Whether every AU of the variant is satisfied for the enrollment."""
    au_ids = set(session.scalars(select(Cmi5Au.id).where(Cmi5Au.variant_id == au.variant_id)))
    satisfied_ids = set(
        session.scalars(
            select(Cmi5Session.au_id).where(
                Cmi5Session.enrollment_id == enrollment_id,
                Cmi5Session.satisfied.is_(True),
            )
        )
    )
    return au_ids <= satisfied_ids


def _reported_progress(statement: Mapping[str, object]) -> float | None:
    """The statement's progress result extension as a fraction 0 to 1, None
    when the statement carries none."""
    result = statement.get("result")
    if not isinstance(result, Mapping):
        return None
    extensions = result.get("extensions")
    if not isinstance(extensions, Mapping) or PROGRESS_EXTENSION not in extensions:
        return None
    value = extensions[PROGRESS_EXTENSION]
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise Cmi5ProgressError(
            f"Statement {statement.get('id')!r} reports progress {value!r} in "
            f"{PROGRESS_EXTENSION}; cmi5 requires an integer from 0 to 100"
        )
    return value / 100


def _variant_progress_measure(session: Session, au: Cmi5Au, enrollment_id: int) -> float:
    """The package's progress for the enrollment: the mean over its AUs of 1
    for a satisfied AU, else the highest progress any of its sessions
    reported, else 0."""
    au_ids = list(session.scalars(select(Cmi5Au.id).where(Cmi5Au.variant_id == au.variant_id)))
    satisfied_ids: set[int] = set()
    best: dict[int, float] = {}
    for observed in session.scalars(
        select(Cmi5Session).where(
            Cmi5Session.enrollment_id == enrollment_id,
            Cmi5Session.au_id.in_(au_ids),
        )
    ):
        if observed.satisfied:
            satisfied_ids.add(observed.au_id)
        if observed.progress_measure is not None:
            best[observed.au_id] = max(best.get(observed.au_id, 0.0), observed.progress_measure)
    measures = [1.0 if au_id in satisfied_ids else best.get(au_id, 0.0) for au_id in au_ids]
    return sum(measures) / len(measures)


def observe_statements(
    session: Session,
    hooks: EventHooks,
    lrs: LrsClient,
    cmi5_session: Cmi5Session,
    au: Cmi5Au,
    statements: Sequence[Mapping[str, object]],
    actor_home_page: str,
) -> None:
    """Record cmi5 progress from AU statements and satisfy the AU when due.

    A progress result extension updates the session's progress measure and
    the activity's (see ``_variant_progress_measure``); a malformed one raises
    Cmi5ProgressError. Only Normal-mode sessions record progress and
    satisfaction. When ``moveOn`` first
    becomes satisfied, the LMS emits a ``satisfied`` statement; once every AU
    of the package is satisfied the activity variant is recorded as
    satisfied, which completes the still active enrollment once every
    activity of the course is (emitting ``CourseCompleted`` via hooks).
    """
    if cmi5_session.launch_mode is not LaunchMode.NORMAL:
        return

    changed = False
    progress_changed = False
    for statement in statements:
        progress = _reported_progress(statement)
        if progress is not None and progress != cmi5_session.progress_measure:
            cmi5_session.progress_measure = progress
            progress_changed = True
        verb = statement.get("verb")
        verb_id = verb.get("id") if isinstance(verb, Mapping) else None
        if verb_id == VERB_COMPLETED and not cmi5_session.completed:
            cmi5_session.completed = True
            changed = True
        elif verb_id == VERB_PASSED and cmi5_session.passed is not True:
            cmi5_session.passed = True
            changed = True
        elif verb_id == VERB_FAILED and cmi5_session.passed is None:
            cmi5_session.passed = False
            changed = True
        elif verb_id == VERB_TERMINATED and not cmi5_session.terminated:
            cmi5_session.terminated = True
            changed = True

    if not changed and not progress_changed:
        return

    newly_satisfied = not cmi5_session.satisfied and _is_satisfied(au.move_on, cmi5_session)
    if newly_satisfied:
        cmi5_session.satisfied = True
    if progress_changed:
        record_activity_progress_measure(
            session,
            cmi5_session.enrollment_id,
            get_activity_variant(session, au.variant_id).activity_id,
            _variant_progress_measure(session, au, cmi5_session.enrollment_id),
        )
    session.commit()

    if not newly_satisfied:
        return

    actor = build_actor(cmi5_session.user_id, actor_home_page)
    lrs.post_statement(build_satisfied_statement(actor, au, cmi5_session))
    if _all_aus_satisfied(session, au, cmi5_session.enrollment_id):
        record_variant_satisfied(session, cmi5_session.enrollment_id, au.variant_id)
        commit_with_events(session, hooks)
