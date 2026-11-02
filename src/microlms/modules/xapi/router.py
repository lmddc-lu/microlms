"""xAPI ingestion and readback: statements in from a trusted external system,
statements and aggregates out for dashboards.

Ingestion: every request carries a service key (``X-Ingest-Key``) that
authorizes it - this is how an external activity source proves it is trusted.
Valid keys are the statically configured ones (``MICROLMS_XAPI_INGEST_KEYS``)
plus the runtime-managed ones platform managers issue and revoke through the
``/keys`` endpoints; ``/config`` exposes the ingestion contract (endpoint,
credential header, accepted statement shape) for supervision. An optional user
bearer token (``Authorization``) binds the statements to that authenticated
user; without it, the GUID carried in ``actor.account.name`` provisions an
anonymous guest identity on first sight and attaches to it afterwards.

The LMS stamps the canonical actor on every statement so ingested results share
one identity with web and content activity, tags it with the
``urn:microlms:xapi:ingested`` category context activity so reads can tell
ingested results from content activity, then forwards the batch to the LRS
with server-side credentials, keeping them off the client. Statements may be
sent singly or as a list, with their own timestamps, so an offline session can
sync later.

Readback: the read endpoints use the normal bearer-authenticated statement-read
permission (platform-scoped actors global, org-scoped actors pinned to their
org), not the service key. One user's statements can be listed individually; the aggregate
endpoint returns only distribution counts, never individual identifiers. Both
take ``ingested_only`` to keep only the statements received through ingestion.
"""

import json
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from microlms.api.deps import (
    AccessPolicyDep,
    AuditDep,
    ManagementScope,
    SessionDep,
    SettingsDep,
    configured_org_id,
    forbid_unmanageable_target,
    get_user_in_scope,
    require_platform_permission,
    require_scoped_permission,
    resolve_org_filter,
)
from microlms.auth.base import AuthBackend, AuthenticatedUser, AuthenticationError
from microlms.authz import Permission
from microlms.domain.enrollments import EnrollmentNotFoundError, get_enrollment
from microlms.domain.users import list_user_ids, provision_external_user
from microlms.lrs import (
    XAPI_VERSION,
    LrsClient,
    LrsClientDep,
    StatementsPage,
    build_actor,
    resolve_actor_home_page,
)
from microlms.modules.xapi.keys import (
    IngestKeyNotFoundError,
    any_ingest_keys,
    create_ingest_key,
    list_ingest_keys,
    revoke_ingest_key,
    use_active_key,
)
from microlms.modules.xapi.statements import (
    INGESTED_CATEGORY,
    StatementError,
    aggregate_statements,
    parse_actor_guid,
    sum_durations,
    tag_ingested,
)

# Statements fetched per LRS page when aggregating
AGGREGATE_PAGE_LIMIT = 500
# Hard ceiling on statements scanned per aggregate query; reaching it is
# reported via ``truncated``, never silently
MAX_AGGREGATE_STATEMENTS = 10_000

# Request header carrying the service key that authorizes ingestion
INGEST_KEY_HEADER = "X-Ingest-Key"

# Session-time statements: both content modules report the time spent in a
# session as ``result.duration`` on their ``terminated`` statement (other
# durations, e.g. interaction latencies, overlap it and must not be added)
VERB_TERMINATED = "http://adlnet.gov/expapi/verbs/terminated"


class _Verb(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str


class _Object(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str


class IngestStatement(BaseModel):
    """An xAPI statement accepted for ingestion; verb.id and object.id required.

    Any other xAPI fields (context, timestamp, id, ...) pass through untouched;
    only the actor is normalized to the platform's canonical identity.
    """

    model_config = ConfigDict(extra="allow")

    actor: dict[str, object] | None = None
    verb: _Verb
    object: _Object
    result: dict[str, object] | None = None


class IngestResult(BaseModel):
    stored: int


class IngestKeyCreate(BaseModel):
    name: str


class IngestKeyRead(BaseModel):
    """An issued service key; ``key`` is the credential itself, exposed only
    through the admin-gated endpoints so an operator can hand it to the
    external application (and re-read it later)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    key: str
    is_active: bool
    created_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None


class IngestConfigRead(BaseModel):
    """The ingestion contract, for supervision: where an external application
    pushes, how it authenticates, and what a submission must look like."""

    ingest_url: str
    key_header: str
    xapi_version: str
    accepts_batch: bool
    required_fields: list[str]
    guest_ingestion_enabled: bool
    guest_role: str | None
    guest_org_slug: str | None
    static_key_count: int
    lrs_url: str | None


class UserStatementsRead(BaseModel):
    """One page of a user's statements; ``more`` signals further pages exist
    (narrow the filters or lower ``limit`` boundaries to page through)."""

    statements: list[dict[str, object]]
    more: bool


class TimeSpentRead(BaseModel):
    """Time a learner spent on an enrollment, summed from the LRS.

    ``seconds`` totals ``result.duration`` over the enrollment's ``terminated``
    statements; ``counted`` of the ``statement_count`` scanned statements
    carried a parseable duration. ``truncated`` reports that the scan hit its
    statement ceiling and the total covers only the scanned prefix.
    """

    seconds: float
    statement_count: int
    counted: int
    truncated: bool


class StatementAggregate(BaseModel):
    """Distribution counts over the matching statements; carries no individual
    identifiers. ``truncated`` reports that the scan hit its statement ceiling
    and the counts cover only the scanned prefix."""

    statement_count: int
    actor_count: int
    by_verb: dict[str, int]
    by_activity: dict[str, int]
    by_response: dict[str, int]
    truncated: bool


def _statement_filters(
    verb: str | None,
    activity: str | None,
    since: datetime | None,
    until: datetime | None,
) -> dict[str, object]:
    """Standard xAPI statement query parameters for the given filters."""
    filters: dict[str, object] = {}
    if verb is not None:
        filters["verb"] = verb
    if activity is not None:
        filters["activity"] = activity
    if since is not None:
        filters["since"] = since.isoformat()
    if until is not None:
        filters["until"] = until.isoformat()
    return filters


def _ingested_filters(activity: str | None) -> dict[str, object]:
    """xAPI query parameters keeping only ingested statements: those carrying
    the ingestion category among their context activities.

    xAPI queries take a single ``activity``, so an activity filter cannot be
    combined with this one (422).
    """
    if activity is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="ingested_only cannot be combined with an activity filter",
        )
    return {"activity": INGESTED_CATEGORY, "related_activities": "true"}


def _scan_statements(
    lrs: LrsClient, filters: dict[str, object]
) -> tuple[list[dict[str, object]], bool]:
    """Collect matching statements across pages, up to the aggregate ceiling."""
    statements: list[dict[str, object]] = []
    page: StatementsPage = lrs.get_statements({**filters, "limit": AGGREGATE_PAGE_LIMIT})
    while True:
        statements.extend(page.statements)
        if page.more is None:
            return statements, False
        if len(statements) >= MAX_AGGREGATE_STATEMENTS:
            return statements, True
        page = lrs.get_more(page.more)


def _require_ingest_key(request: Request, session: SessionDep, settings: SettingsDep) -> None:
    """Authorize the calling application by its service key.

    A key is valid when it is one of the statically configured keys or an
    active runtime-managed one; using a managed key stamps its last use. A
    deployment with no key configured anywhere answers 503, so a missing
    credential and a not-yet-configured instance stay distinguishable.
    """
    key = request.headers.get(INGEST_KEY_HEADER)
    if key is not None:
        if key in settings.xapi_ingest_keys:
            return
        if use_active_key(session, key) is not None:
            return
    if not settings.xapi_ingest_keys and not any_ingest_keys(session):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "xAPI ingestion is not configured "
                "(issue an ingestion key or set MICROLMS_XAPI_INGEST_KEYS)"
            ),
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid ingestion key",
    )


def _authenticated_actor(request: Request, session: SessionDep) -> AuthenticatedUser | None:
    """Resolve the authenticated user when a bearer token is present, else None.

    A guest submission carries no ``Authorization`` header; an authenticated
    submission does, and an invalid credential is rejected rather than
    downgraded to a guest.
    """
    if request.headers.get("Authorization") is None:
        return None
    auth: AuthBackend = request.app.state.auth
    try:
        return auth.authenticate(session, request)
    except AuthenticationError as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(error)) from error


def build_router() -> APIRouter:
    router = APIRouter(prefix="/xapi", tags=["xapi"])

    @router.get("/status")
    def module_status() -> dict[str, str]:
        return {"module": "xapi", "status": "ready"}

    @router.post("/statements", dependencies=[Depends(_require_ingest_key)])
    def ingest(
        request: Request,
        session: SessionDep,
        settings: SettingsDep,
        lrs: LrsClientDep,
        payload: Annotated[IngestStatement | list[IngestStatement], Body()],
    ) -> IngestResult:
        statements = payload if isinstance(payload, list) else [payload]
        actor_user = _authenticated_actor(request, session)
        home_page = resolve_actor_home_page(settings, request)

        outgoing: list[dict[str, object]] = []
        for statement in statements:
            try:
                supplied = parse_actor_guid(statement.actor)
            except StatementError as error:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
                ) from error

            if actor_user is not None:
                if supplied is not None and supplied != actor_user.id:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Statement actor does not match the authenticated user",
                    )
                guid = actor_user.id
            else:
                if supplied is None:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Guest statement must carry a GUID in actor.account.name",
                    )
                if settings.xapi_guest_role is None or settings.xapi_guest_org_slug is None:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Guest ingestion is disabled (set MICROLMS_XAPI_GUEST_ROLE and "
                        "MICROLMS_XAPI_GUEST_ORG_SLUG)",
                    )
                guest = provision_external_user(
                    session,
                    supplied,
                    configured_org_id(
                        session, settings.xapi_guest_org_slug, "MICROLMS_XAPI_GUEST_ORG_SLUG"
                    ),
                    settings.xapi_guest_role,
                )
                if not guest.is_active:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="This identity is deactivated and cannot submit statements",
                    )
                guid = supplied

            try:
                body = tag_ingested(statement.model_dump(exclude_none=True))
            except StatementError as error:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
                ) from error
            body["actor"] = build_actor(guid, home_page)
            outgoing.append(body)

        if outgoing:
            lrs.post_statements(outgoing)
        return IngestResult(stored=len(outgoing))

    @router.get(
        "/config",
        dependencies=[Depends(require_platform_permission(Permission.XAPI_CONFIG_READ))],
    )
    def read_ingest_config(request: Request, settings: SettingsDep) -> IngestConfigRead:
        """The ingestion contract as this deployment exposes it; admin only.

        ``ingest_url`` is derived from the URL the request came through, so it
        reflects the origin the admin is supervising from.
        """
        return IngestConfigRead(
            ingest_url=str(request.url_for("ingest")),
            key_header=INGEST_KEY_HEADER,
            xapi_version=XAPI_VERSION,
            accepts_batch=True,
            required_fields=["verb.id", "object.id"],
            guest_ingestion_enabled=settings.xapi_guest_role is not None,
            guest_role=settings.xapi_guest_role,
            guest_org_slug=settings.xapi_guest_org_slug,
            static_key_count=len(settings.xapi_ingest_keys),
            lrs_url=settings.lrs_url,
        )

    @router.get(
        "/keys", dependencies=[Depends(require_platform_permission(Permission.XAPI_KEYS_MANAGE))]
    )
    def read_ingest_keys(session: SessionDep) -> list[IngestKeyRead]:
        """Every issued key, newest first, revoked ones included; admin only."""
        return [IngestKeyRead.model_validate(key) for key in list_ingest_keys(session)]

    @router.post(
        "/keys",
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_platform_permission(Permission.XAPI_KEYS_MANAGE))],
    )
    def add_ingest_key(
        session: SessionDep, audit: AuditDep, payload: IngestKeyCreate
    ) -> IngestKeyRead:
        """Issue a new key for an external application; admin only.

        The key material is generated server-side and returned in the response
        (and by ``GET /keys``); the audit trail records the issuance but never
        the key itself.
        """
        ingest_key = create_ingest_key(session, payload.name)
        audit.record("xapi_key.created", "xapi_key", str(ingest_key.id), {"name": ingest_key.name})
        return IngestKeyRead.model_validate(ingest_key)

    @router.post(
        "/keys/{key_id}/revoke",
        dependencies=[Depends(require_platform_permission(Permission.XAPI_KEYS_MANAGE))],
    )
    def revoke_key(session: SessionDep, audit: AuditDep, key_id: int) -> IngestKeyRead:
        """Revoke a key so submissions presenting it are refused; admin only."""
        try:
            ingest_key = revoke_ingest_key(session, key_id)
        except IngestKeyNotFoundError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        audit.record("xapi_key.revoked", "xapi_key", str(ingest_key.id), {"name": ingest_key.name})
        return IngestKeyRead.model_validate(ingest_key)

    @router.get("/users/{user_id}/statements")
    def read_user_statements(
        request: Request,
        session: SessionDep,
        settings: SettingsDep,
        scope: Annotated[
            ManagementScope,
            Depends(require_scoped_permission(Permission.XAPI_STATEMENTS_READ)),
        ],
        lrs: LrsClientDep,
        policy: AccessPolicyDep,
        user_id: uuid.UUID,
        verb: str | None = None,
        activity: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        ingested_only: bool = False,
    ) -> UserStatementsRead:
        """One user's statements, as stored; managers only within their org,
        and never for accounts holding a permission the actor lacks (like the users
        router).

        Queries by the same canonical actor the platform stamps at write time,
        so it matches ingested and content-module statements alike;
        ``ingested_only`` keeps the ingested ones.
        """
        target = get_user_in_scope(session, scope, user_id)
        forbid_unmanageable_target(scope, target, policy)
        params = _statement_filters(verb, activity, since, until)
        if ingested_only:
            params = {**params, **_ingested_filters(activity)}
        params["agent"] = json.dumps(
            build_actor(user_id, resolve_actor_home_page(settings, request))
        )
        if limit is not None:
            params["limit"] = limit
        page = lrs.get_statements(params)
        return UserStatementsRead(statements=page.statements, more=page.more is not None)

    @router.get("/enrollments/{enrollment_id}/time-spent")
    def read_enrollment_time_spent(
        request: Request,
        session: SessionDep,
        settings: SettingsDep,
        scope: Annotated[
            ManagementScope,
            Depends(require_scoped_permission(Permission.XAPI_STATEMENTS_READ)),
        ],
        lrs: LrsClientDep,
        policy: AccessPolicyDep,
        enrollment_id: int,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> TimeSpentRead:
        """Time the learner spent on one enrollment; managers only within
        their org, and never for accounts holding a permission the actor lacks.

        Sums ``result.duration`` over the ``terminated`` statements carrying
        the enrollment's registration, so scorm and cmi5 sessions count alike
        regardless of which activity of the course they belong to.
        """
        try:
            enrollment = get_enrollment(session, enrollment_id)
        except EnrollmentNotFoundError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        target = get_user_in_scope(session, scope, enrollment.user_id)
        forbid_unmanageable_target(scope, target, policy)
        params = _statement_filters(VERB_TERMINATED, None, since, until)
        params["agent"] = json.dumps(
            build_actor(enrollment.user_id, resolve_actor_home_page(settings, request))
        )
        params["registration"] = str(enrollment.registration)
        statements, truncated = _scan_statements(lrs, params)
        total = sum_durations(statements)
        return TimeSpentRead(
            seconds=total.seconds,
            statement_count=len(statements),
            counted=total.counted,
            truncated=truncated,
        )

    @router.get("/statements/aggregate")
    def read_statement_aggregate(
        session: SessionDep,
        scope: Annotated[
            ManagementScope,
            Depends(require_scoped_permission(Permission.XAPI_STATEMENTS_READ)),
        ],
        lrs: LrsClientDep,
        role: str | None = None,
        org_id: int | None = None,
        verb: str | None = None,
        activity: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        ingested_only: bool = False,
    ) -> StatementAggregate:
        """Aggregate statement counts, optionally narrowed to an organization
        and/or a role, and to ingested statements with ``ingested_only``;
        managers only within their org.

        Only distributions leave this endpoint: statements are attributed to
        users server-side and reduced to counts, so no individual identifier
        is exposed (a statement not attributable to a user in the requested
        organization/role is excluded).
        """
        effective_org = resolve_org_filter(scope, org_id)
        allowed = (
            list_user_ids(session, org_ids=effective_org, role=role)
            if effective_org is not None or role is not None
            else None
        )
        filters = _statement_filters(verb, activity, since, until)
        if ingested_only:
            filters = {**filters, **_ingested_filters(activity)}
        statements, truncated = _scan_statements(lrs, filters)
        counts = aggregate_statements(statements, allowed)
        return StatementAggregate(
            statement_count=counts.statement_count,
            actor_count=counts.actor_count,
            by_verb=counts.by_verb,
            by_activity=counts.by_activity,
            by_response=counts.by_response,
            truncated=truncated,
        )

    return router
