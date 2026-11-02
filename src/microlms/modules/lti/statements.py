"""xAPI statements recording LTI activity in the LRS.

An LTI tool keeps the learner's interactions to itself; what the platform
sees is the launch and the scores the tool publishes back. Both are recorded
as xAPI statements with the canonical actor and the enrollment registration,
like the other content modules' statements, so LTI activities show up in the
same reports. The object is a stable IRI per resource link, never a tool URL.
"""

import uuid
from datetime import datetime

from microlms.lrs import build_actor
from microlms.modules.lti.models import LtiLink
from microlms.modules.lti.scores import ScoreActivityProgress, ScoreUpdate

VERB_LAUNCHED = "http://adlnet.gov/expapi/verbs/launched"
VERB_COMPLETED = "http://adlnet.gov/expapi/verbs/completed"
VERB_SCORED = "http://adlnet.gov/expapi/verbs/scored"

ACTIVITY_IRI_PREFIX = "urn:microlms:lti"

# Namespace for the ids of score statements, see score_statement
_SCORE_STATEMENT_NAMESPACE = uuid.UUID("5d3f4c1e-8a2b-4f6e-9c7d-1b0a2e3f4d5c")


def link_activity_iri(link: LtiLink) -> str:
    """The canonical xAPI activity id of a resource link."""
    return f"{ACTIVITY_IRI_PREFIX}:{link.resource_link_id}"


def _statement(
    statement_id: uuid.UUID,
    verb_id: str,
    verb_display: str,
    link: LtiLink,
    lang: str,
    user_id: uuid.UUID,
    actor_home_page: str,
    registration: uuid.UUID,
    timestamp: datetime,
) -> dict[str, object]:
    return {
        "id": str(statement_id),
        "actor": build_actor(user_id, actor_home_page),
        "verb": {"id": verb_id, "display": {"en-US": verb_display}},
        "object": {
            "objectType": "Activity",
            "id": link_activity_iri(link),
            "definition": {"name": {lang: link.title}},
        },
        "context": {"registration": str(registration)},
        "timestamp": timestamp.isoformat(),
    }


def launched_statement(
    link: LtiLink,
    lang: str,
    user_id: uuid.UUID,
    actor_home_page: str,
    registration: uuid.UUID,
    timestamp: datetime,
) -> dict[str, object]:
    """The learner entered the tool: sent when the platform signs the launch."""
    return _statement(
        uuid.uuid4(),
        VERB_LAUNCHED,
        "launched",
        link,
        lang,
        user_id,
        actor_home_page,
        registration,
        timestamp,
    )


def _score_result(score: ScoreUpdate) -> dict[str, object]:
    """The xAPI result of a score. xAPI requires ``raw`` within ``max`` and
    ``scaled`` within [-1, 1], while AGS allows a score above its maximum
    (3.4.4): such a score keeps ``raw`` only."""
    result: dict[str, object] = {}
    if score.score_given is not None:
        if score.score_maximum is not None and score.score_given <= score.score_maximum:
            result["score"] = {
                "raw": score.score_given,
                "min": 0,
                "max": score.score_maximum,
                "scaled": score.score_given / score.score_maximum,
            }
        else:
            result["score"] = {"raw": score.score_given}
    if score.activity_progress is ScoreActivityProgress.COMPLETED:
        result["completion"] = True
    if score.comment:
        result["response"] = score.comment
    return result


def score_statement(
    link: LtiLink,
    lang: str,
    enrollment_id: int,
    score: ScoreUpdate,
    actor_home_page: str,
    registration: uuid.UUID,
) -> dict[str, object] | None:
    """The statement for a score the tool published, or None when it reports
    neither completion nor a score (a progress-only update).

    ``completed`` when the tool reports the activity completed, ``scored``
    otherwise. The statement id derives from the link, the enrollment and the
    tool's score timestamp, which AGS makes unique per update (3.4.9): a tool
    retrying the same update produces the same statement, which the LRS
    accepts once. The timestamp is the tool's, so the statement dates the
    learner's activity rather than the tool's delayed sync.
    """
    completed = score.activity_progress is ScoreActivityProgress.COMPLETED
    if not completed and score.score_given is None:
        return None
    statement = _statement(
        uuid.uuid5(
            _SCORE_STATEMENT_NAMESPACE,
            f"{link.resource_link_id}:{enrollment_id}:{score.timestamp.isoformat()}",
        ),
        VERB_COMPLETED if completed else VERB_SCORED,
        "completed" if completed else "scored",
        link,
        lang,
        score.user_id,
        actor_home_page,
        registration,
        score.timestamp,
    )
    statement["result"] = _score_result(score)
    return statement
