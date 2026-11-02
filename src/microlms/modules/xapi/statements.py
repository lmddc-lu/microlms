"""Pure helpers for validating, reading and aggregating xAPI statements."""

import json
import re
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# ISO 8601 duration limited to week/day/time components; year/month components
# are ambiguous (no fixed length) and do not appear in ``result.duration``
_DURATION_PATTERN = re.compile(
    r"^P(?:(?P<weeks>\d+(?:\.\d+)?)W)?"
    r"(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)


# Category context activity stamped on every statement received through the
# ingestion endpoint, so reads can tell them apart from the statements the
# content modules write for web activity
INGESTED_CATEGORY = "urn:microlms:xapi:ingested"


class StatementError(Exception):
    """Raised when a statement is malformed and must be rejected."""


def tag_ingested(statement: Mapping[str, object]) -> dict[str, object]:
    """A copy of ``statement`` with :data:`INGESTED_CATEGORY` added to its
    ``context.contextActivities.category``, keeping the categories it carries.

    Raises :class:`StatementError` when ``context``, ``contextActivities`` or
    ``category`` is present with a shape xAPI does not allow (category is an
    activity object or a list of them).
    """
    context = statement.get("context", {})
    if not isinstance(context, Mapping):
        raise StatementError("context must be an object")
    context_activities = context.get("contextActivities", {})
    if not isinstance(context_activities, Mapping):
        raise StatementError("context.contextActivities must be an object")
    category = context_activities.get("category", [])
    if isinstance(category, Mapping):
        category = [category]
    if not isinstance(category, list):
        raise StatementError(
            "context.contextActivities.category must be an activity or a list of them"
        )
    tagged_category = [*category, {"id": INGESTED_CATEGORY, "objectType": "Activity"}]
    return {
        **statement,
        "context": {
            **context,
            "contextActivities": {**context_activities, "category": tagged_category},
        },
    }


def parse_actor_guid(actor: Mapping[str, object] | None) -> uuid.UUID | None:
    """Return the GUID in ``actor.account.name``, or None when none is present.

    Raises :class:`StatementError` when a name is present but is not a valid
    GUID, so a malformed or fraudulent actor is rejected rather than silently
    provisioned.
    """
    if not isinstance(actor, Mapping):
        return None
    account = actor.get("account")
    if not isinstance(account, Mapping):
        return None
    name = account.get("name")
    if name is None:
        return None
    if not isinstance(name, str):
        raise StatementError("actor.account.name must be a string")
    try:
        return uuid.UUID(name)
    except ValueError as error:
        raise StatementError(f"actor.account.name is not a valid GUID: {name!r}") from error


def parse_result_duration_seconds(value: str) -> float | None:
    """Seconds in an ISO 8601 ``result.duration``, or None when unparseable.

    Lenient on purpose: stored statements may come from writers whose duration
    format we do not control, and an unparseable one is simply not countable
    (callers report how many statements were counted).
    """
    match = _DURATION_PATTERN.match(value)
    if match is None or value in ("P", "PT"):
        return None
    weeks = float(match.group("weeks") or 0)
    days = float(match.group("days") or 0)
    hours = float(match.group("hours") or 0)
    minutes = float(match.group("minutes") or 0)
    seconds = float(match.group("seconds") or 0)
    return weeks * 604800 + days * 86400 + hours * 3600 + minutes * 60 + seconds


@dataclass(frozen=True, slots=True)
class DurationSum:
    """Total of the parseable ``result.duration`` values in a set of
    statements; ``counted`` says how many statements contributed."""

    seconds: float
    counted: int


def sum_durations(statements: Sequence[Mapping[str, object]]) -> DurationSum:
    """Sum ``result.duration`` across statements, skipping statements without
    a parseable duration."""
    total = 0.0
    counted = 0
    for statement in statements:
        duration = _nested_str(statement, "result", "duration")
        if duration is None:
            continue
        seconds = parse_result_duration_seconds(duration)
        if seconds is None:
            continue
        total += seconds
        counted += 1
    return DurationSum(seconds=total, counted=counted)


@dataclass(frozen=True, slots=True)
class StatementCounts:
    """Aggregate counters over a set of statements; carries no individual
    identifiers, only distribution counts."""

    statement_count: int
    actor_count: int
    by_verb: dict[str, int]
    by_activity: dict[str, int]
    by_response: dict[str, int]


def _lenient_actor_guid(statement: Mapping[str, object]) -> uuid.UUID | None:
    """The GUID in ``actor.account.name``, or None when absent or not a GUID.

    Lenient on purpose: stored statements may come from writers that do not
    stamp the platform's canonical actor, and those are simply not attributable
    to a platform user.
    """
    try:
        actor = statement.get("actor")
        return parse_actor_guid(actor if isinstance(actor, Mapping) else None)
    except StatementError:
        return None


def _nested_str(statement: Mapping[str, object], outer: str, inner: str) -> str | None:
    value = statement.get(outer)
    if not isinstance(value, Mapping):
        return None
    field = value.get(inner)
    return field if isinstance(field, str) else None


def aggregate_statements(
    statements: Sequence[Mapping[str, object]],
    allowed_actors: frozenset[uuid.UUID] | None,
) -> StatementCounts:
    """Count statements by verb, activity and result response.

    ``allowed_actors`` restricts the aggregate to statements attributable to
    those users (statements without an attributable actor are excluded); None
    aggregates everything. Distinct actors are keyed by the whole actor object,
    so non-canonical actors still count once each.
    """
    actors: set[str] = set()
    by_verb: Counter[str] = Counter()
    by_activity: Counter[str] = Counter()
    by_response: Counter[str] = Counter()
    statement_count = 0
    for statement in statements:
        if allowed_actors is not None and _lenient_actor_guid(statement) not in allowed_actors:
            continue
        statement_count += 1
        actors.add(json.dumps(statement.get("actor"), sort_keys=True))
        verb_id = _nested_str(statement, "verb", "id")
        if verb_id is not None:
            by_verb[verb_id] += 1
        activity_id = _nested_str(statement, "object", "id")
        if activity_id is not None:
            by_activity[activity_id] += 1
        response = _nested_str(statement, "result", "response")
        if response is not None:
            by_response[response] += 1
    return StatementCounts(
        statement_count=statement_count,
        actor_count=len(actors),
        by_verb=dict(by_verb),
        by_activity=dict(by_activity),
        by_response=dict(by_response),
    )
