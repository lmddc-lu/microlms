"""cmi5 HTTP endpoints: import, content serving, launch, fetch and xAPI proxy.

The proxy is the load-bearing piece: the launched AU sends every xAPI call as
``Authorization: Basic <auth-token>`` to the session endpoint. We resolve the
opaque token to a session, forward to the LRS with server credentials, and tap
statement writes to evaluate ``moveOn`` and complete the enrollment.

The server credentials reach every learner's data, so the proxy confines a
session to its own learner: only the xAPI resources an AU needs are relayed,
statement writes must carry the session's actor, reads are pinned to the
session's agent and registration, and the token stops working once the AU
has terminated.
"""

import json
import secrets
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import parse_qsl, urlencode

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel
from sqlalchemy import select

from microlms.api.deps import (
    AuditDep,
    CurrentPermissionsDep,
    CurrentUserDep,
    HooksDep,
    ManagementScope,
    SessionDep,
    SettingsDep,
    StorageDep,
    get_course_in_scope,
    get_enrollment_for_launch,
    get_readable_course,
    require_permission,
    require_scoped_permission,
)
from microlms.api.serving import serve_package_file
from microlms.authz import Permission
from microlms.domain.activities import (
    ActivityLanguageExistsError,
    ActivityNotFoundError,
    ActivityVariant,
    ActivityVariantModuleError,
    ActivityVariantNotFoundError,
    get_activity,
    get_activity_variant,
    record_activity_launched,
)
from microlms.domain.courses import CourseNotFoundError
from microlms.domain.languages import InvalidLanguageTagError
from microlms.lrs import LrsClient, LrsClientDep, build_actor, resolve_actor_home_page
from microlms.modules.cmi5.importer import (
    Cmi5ImportError,
    ImportResult,
    import_package,
    replace_package,
)
from microlms.modules.cmi5.launch import VERB_VOIDED, observe_statements, start_session
from microlms.modules.cmi5.models import Cmi5Au, Cmi5Session, LaunchMethod

# Upstream response headers not safe to relay verbatim
_STRIPPED_RESPONSE_HEADERS = frozenset(
    {"content-length", "content-encoding", "transfer-encoding", "connection"}
)
# xAPI document resources subject to ETag concurrency on writes
_DOCUMENT_RESOURCES = frozenset({"activities/state", "agents/profile"})
# The xAPI resources an AU may reach through the session proxy and the methods
# allowed on each. Any other path (other LRS endpoints, ``..`` escapes out of
# the xAPI prefix) is refused; activity-wide documents are shared by every
# learner, so they are read-only.
_PROXIED_METHODS: Mapping[str, frozenset[str]] = {
    "statements": frozenset({"GET", "PUT", "POST"}),
    "activities/state": frozenset({"GET", "PUT", "POST", "DELETE"}),
    "agents/profile": frozenset({"GET", "PUT", "POST", "DELETE"}),
    "activities/profile": frozenset({"GET"}),
    "activities": frozenset({"GET"}),
    "agents": frozenset({"GET"}),
    "about": frozenset({"GET"}),
}
# Statement query parameters that would widen a read beyond the session's own
# statements: single-statement lookups ignore the agent filter, and
# related_agents matches statements other learners wrote about this one
_REFUSED_STATEMENT_QUERY = frozenset({"statementId", "voidedStatementId", "related_agents"})
_IDENTIFIER_PROPERTIES = frozenset({"mbox", "mbox_sha1sum", "openid", "account"})


class Cmi5AuRead(BaseModel):
    id: int
    publisher_id: str
    title: str
    launch_method: LaunchMethod


class Cmi5ImportRead(BaseModel):
    course_id: int
    activity_id: int
    variant_id: int
    lang: str
    aus: list[Cmi5AuRead]


class LaunchRead(BaseModel):
    launch_url: str
    launch_method: LaunchMethod


def _import_read(result: ImportResult) -> Cmi5ImportRead:
    return Cmi5ImportRead(
        course_id=result.course_id,
        activity_id=result.activity_id,
        variant_id=result.variant_id,
        lang=result.lang,
        aus=[
            Cmi5AuRead(
                id=au.id,
                publisher_id=au.publisher_id,
                title=au.title,
                launch_method=au.launch_method,
            )
            for au in result.aus
        ],
    )


def _course_id_for_variant(session: SessionDep, variant: ActivityVariant) -> int:
    return get_activity(session, variant.activity_id).course_id


def _resolve_session(session: SessionDep, session_id: uuid.UUID) -> Cmi5Session:
    cmi5_session = session.get(Cmi5Session, session_id)
    if cmi5_session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown cmi5 session")
    return cmi5_session


def _is_expired(cmi5_session: Cmi5Session) -> bool:
    """Whether the session outlived Settings.launch_max_age."""
    return cmi5_session.expires_at <= datetime.now(UTC)


def _reconcile_concurrency(
    lrs: LrsClient, method: str, path: str, query: str, headers: object
) -> dict[str, str]:
    """Add a concurrency header to unconditional document writes.

    The State/Profile resources require If-Match or If-None-Match on writes to
    an existing document. AUs that omit them (e.g. cmi5.js suspend data) would
    get a 409, so the proxy preflights the ETag and supplies the right header:
    If-Match for an existing document, If-None-Match ``*`` for a new one.
    """
    effective = dict(headers)
    lowered = {name.lower() for name in effective}
    is_document_write = method in ("PUT", "POST", "DELETE") and path in _DOCUMENT_RESOURCES
    if not is_document_write or "if-match" in lowered or "if-none-match" in lowered:
        return effective
    etag = lrs.document_etag(path, query)
    if etag is not None:
        effective["If-Match"] = etag
    else:
        effective["If-None-Match"] = "*"
    return effective


def _check_proxied(method: str, xapi_path: str) -> None:
    allowed = _PROXIED_METHODS.get(xapi_path)
    if allowed is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"xAPI resource {xapi_path!r} is not available through the cmi5 proxy",
        )
    if method not in allowed:
        raise HTTPException(
            status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
            detail=f"{method} on xAPI resource {xapi_path!r} is not allowed through the cmi5 proxy",
        )


def _pinned_params(
    xapi_path: str, actor: Mapping[str, object], cmi5_session: Cmi5Session
) -> dict[str, str]:
    """The query parameters forced to the session's values for a resource.

    Learner-scoped resources are pinned to the session's agent, and those
    keyed by registration to the session's registration, so an AU never
    reads or writes another learner's data whatever it sends.
    """
    agent = json.dumps(actor)
    registration = str(cmi5_session.registration)
    match xapi_path:
        case "statements" | "activities/state":
            return {"agent": agent, "registration": registration}
        case "agents/profile" | "agents":
            return {"agent": agent}
        case _:
            return {}


def _scoped_query(method: str, xapi_path: str, query: str, pinned: Mapping[str, str]) -> str:
    """The request query with the pinned parameters replacing the AU's own.

    Statement writes keep their query (``statementId`` on PUT); they are
    confined by the actor check on their body instead.
    """
    if xapi_path == "statements" and method != "GET":
        return query
    params = parse_qsl(query, keep_blank_values=True)
    if xapi_path == "statements":
        refused = sorted({name for name, _ in params} & _REFUSED_STATEMENT_QUERY)
        if refused:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Statement query parameters {refused} are not allowed through the cmi5 proxy"
                ),
            )
    kept = [(name, value) for name, value in params if name not in pinned]
    return urlencode([*kept, *pinned.items()])


def _is_session_actor(actor: object, expected: Mapping[str, object]) -> bool:
    """Whether a statement actor is the session's learner: an Agent identified
    by the session's account and nothing else."""
    if not isinstance(actor, Mapping):
        return False
    if actor.get("objectType", "Agent") != "Agent":
        return False
    identifiers = _IDENTIFIER_PROPERTIES & actor.keys()
    return identifiers == {"account"} and actor["account"] == expected["account"]


def _statement_writes(body: bytes, actor: Mapping[str, object]) -> list[Mapping[str, object]]:
    """Parse a statement write and check every statement belongs to the
    session: its actor is the session's learner and it voids nothing."""
    try:
        payload = json.loads(body)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"cmi5 proxy statement writes must be a JSON body: {error}",
        ) from error
    statements = payload if isinstance(payload, list) else [payload]
    for statement in statements:
        if not isinstance(statement, Mapping):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="cmi5 proxy statement writes must be a statement or a list of statements",
            )
        if not _is_session_actor(statement.get("actor"), actor):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Statement actor {statement.get('actor')!r} is not the session's "
                    f"learner {actor!r}"
                ),
            )
        verb = statement.get("verb")
        if isinstance(verb, Mapping) and verb.get("id") == VERB_VOIDED:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Voiding statements is not allowed through the cmi5 proxy",
            )
    return statements


def _bearer_basic_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, credential = header.partition(" ")
    if scheme != "Basic" or not credential:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing cmi5 session credential",
        )
    return credential


def _observe(
    session: SessionDep,
    hooks: HooksDep,
    lrs: LrsClient,
    cmi5_session: Cmi5Session,
    statements: Sequence[Mapping[str, object]],
    actor_home_page: str,
) -> None:
    au = session.get(Cmi5Au, cmi5_session.au_id)
    if au is None:
        return
    observe_statements(session, hooks, lrs, cmi5_session, au, statements, actor_home_page)


def build_router() -> APIRouter:
    router = APIRouter(prefix="/cmi5", tags=["cmi5"])

    @router.get("/status")
    def module_status() -> dict[str, str]:
        return {"module": "cmi5", "status": "ready"}

    @router.post(
        "/import",
        status_code=status.HTTP_201_CREATED,
    )
    async def import_cmi5_package(
        session: SessionDep,
        scope: Annotated[
            ManagementScope, Depends(require_scoped_permission(Permission.CONTENT_IMPORT))
        ],
        storage: StorageDep,
        audit: AuditDep,
        file: UploadFile,
        course_id: Annotated[int, Form()],
        lang: Annotated[str, Form()],
        activity_id: Annotated[int | None, Form()] = None,
    ) -> Cmi5ImportRead:
        """Import a package as the ``lang`` variant of a course activity;
        without ``activity_id`` a new activity is appended to the course.
        Org-scoped importers only reach courses their org owns."""
        get_course_in_scope(session, scope, course_id)
        try:
            result = import_package(
                session, storage, await file.read(), course_id, activity_id, lang
            )
        except (Cmi5ImportError, InvalidLanguageTagError) as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
            ) from error
        except (CourseNotFoundError, ActivityNotFoundError) as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ActivityLanguageExistsError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        audit.record(
            "activity.imported",
            "course",
            str(result.course_id),
            {
                "activity_id": result.activity_id,
                "variant_id": result.variant_id,
                "lang": result.lang,
            },
        )
        return _import_read(result)

    @router.put("/variants/{variant_id}/package")
    async def replace_cmi5_package(
        session: SessionDep,
        scope: Annotated[
            ManagementScope, Depends(require_scoped_permission(Permission.CONTENT_IMPORT))
        ],
        storage: StorageDep,
        audit: AuditDep,
        cleanup: BackgroundTasks,
        variant_id: int,
        file: UploadFile,
    ) -> Cmi5ImportRead:
        """Replace the package of a cmi5 variant in place.

        Its AUs and every learner session on them are deleted, so learners start
        the new package afresh, while the activity progress recorded through
        the variant is kept. Org-scoped importers only reach courses their
        org owns."""
        try:
            variant = get_activity_variant(session, variant_id)
        except ActivityVariantNotFoundError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        get_course_in_scope(session, scope, _course_id_for_variant(session, variant))
        try:
            replaced = replace_package(session, storage, await file.read(), variant.id)
        except Cmi5ImportError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
            ) from error
        except ActivityVariantModuleError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        result = replaced.imported
        audit.record(
            "activity_variant.replaced",
            "activity",
            str(result.activity_id),
            {"course_id": result.course_id, "variant_id": result.variant_id, "lang": result.lang},
        )
        # The old package files are unreferenced once the replacement has
        # committed (audit.record commits), so they are deleted after the response
        for prefix in replaced.stale_prefixes:
            cleanup.add_task(storage.delete_prefix, prefix)
        return _import_read(result)

    @router.get(
        "/variants/{variant_id}/aus",
        dependencies=[Depends(require_permission(Permission.CONTENT_ACCESS))],
    )
    def list_variant_aus(
        session: SessionDep,
        current_user: CurrentUserDep,
        permissions: CurrentPermissionsDep,
        variant_id: int,
    ) -> list[Cmi5AuRead]:
        """The AUs of an activity variant, in course-structure order; same
        visibility as the course detail endpoint, so a client can render
        launchable items."""
        try:
            variant = get_activity_variant(session, variant_id)
        except ActivityVariantNotFoundError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        course_id = _course_id_for_variant(session, variant)
        get_readable_course(session, current_user, permissions, course_id)
        aus = session.scalars(
            select(Cmi5Au).where(Cmi5Au.variant_id == variant.id).order_by(Cmi5Au.au_index)
        ).all()
        return [
            Cmi5AuRead(
                id=au.id,
                publisher_id=au.publisher_id,
                title=au.title,
                launch_method=au.launch_method,
            )
            for au in aus
        ]

    @router.get("/content/{session_id}/{file_path:path}")
    def serve_content(
        request: Request,
        session: SessionDep,
        storage: StorageDep,
        session_id: uuid.UUID,
        file_path: str,
    ) -> Response:
        """Stream a file of the package the cmi5 session launched, honoring
        Range requests so large media (e.g. videos) plays and seeks in the
        browser. The unguessable session id scopes the content to an enrolled
        learner's launch (the AU window carries no API credential)."""
        cmi5_session = _resolve_session(session, session_id)
        if cmi5_session.terminated or _is_expired(cmi5_session):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="cmi5 session is terminated or expired; launch the AU again",
            )
        au = session.get(Cmi5Au, cmi5_session.au_id)
        if au is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown AU")
        return serve_package_file(
            storage, au.storage_prefix, file_path, request.headers.get("range")
        )

    @router.post("/aus/{au_id}/launch")
    def launch_au(
        request: Request,
        session: SessionDep,
        settings: SettingsDep,
        lrs: LrsClientDep,
        current_user: CurrentUserDep,
        au_id: int,
    ) -> LaunchRead:
        au = session.get(Cmi5Au, au_id)
        if au is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown AU")
        variant = get_activity_variant(session, au.variant_id)
        enrollment = get_enrollment_for_launch(session, current_user.id, variant.activity_id)
        launched_at = datetime.now(UTC)
        record_activity_launched(session, enrollment.id, variant.activity_id, launched_at)
        _, launch_url = start_session(
            session,
            lrs,
            au,
            current_user.id,
            enrollment.id,
            str(request.base_url).rstrip("/") + settings.api_prefix,
            resolve_actor_home_page(settings, request),
            launched_at + settings.launch_max_age,
        )
        return LaunchRead(launch_url=launch_url, launch_method=au.launch_method)

    @router.post("/sessions/{session_id}/fetch")
    def fetch_auth_token(session: SessionDep, session_id: uuid.UUID) -> dict[str, str]:
        cmi5_session = _resolve_session(session, session_id)
        if cmi5_session.fetched or _is_expired(cmi5_session):
            return {
                "error-code": "1",
                "error-text": "Fetch URL already used or expired",
            }
        cmi5_session.fetched = True
        session.commit()
        return {"auth-token": cmi5_session.auth_token}

    async def xapi_proxy(
        request: Request,
        session: SessionDep,
        settings: SettingsDep,
        hooks: HooksDep,
        lrs: LrsClientDep,
        session_id: uuid.UUID,
        xapi_path: str,
    ) -> Response:
        token = _bearer_basic_token(request)
        cmi5_session = _resolve_session(session, session_id)
        if not secrets.compare_digest(token.encode(), cmi5_session.auth_token.encode()):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid cmi5 session token",
            )
        if cmi5_session.terminated or _is_expired(cmi5_session):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="cmi5 session is terminated or expired; launch the AU again",
            )
        _check_proxied(request.method, xapi_path)

        actor_home_page = resolve_actor_home_page(settings, request)
        actor = build_actor(cmi5_session.user_id, actor_home_page)
        body = await request.body()
        is_statement_write = xapi_path == "statements" and request.method != "GET"
        statements = _statement_writes(body, actor) if is_statement_write else []
        query = _scoped_query(
            request.method,
            xapi_path,
            request.url.query,
            _pinned_params(xapi_path, actor, cmi5_session),
        )
        headers = _reconcile_concurrency(lrs, request.method, xapi_path, query, request.headers)
        # todo: this is synchronous and blocks event loop
        upstream = lrs.forward(request.method, xapi_path, query, headers, body)

        if is_statement_write and upstream.is_success:
            _observe(session, hooks, lrs, cmi5_session, statements, actor_home_page)

        headers = {
            name: value
            for name, value in upstream.headers.items()
            if name.lower() not in _STRIPPED_RESPONSE_HEADERS
        }
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=headers,
        )

    # One route per method so each gets a distinct OpenAPI operationId; a single
    # multi-method route would reuse one id across all methods and collide.
    for method in ("GET", "PUT", "POST", "DELETE"):
        router.add_api_route(
            "/sessions/{session_id}/xapi/{xapi_path:path}",
            xapi_proxy,
            methods=[method],
            operation_id=f"cmi5_xapi_proxy_{method.lower()}",
        )

    return router
