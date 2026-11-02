"""LTI 1.3 platform HTTP endpoints: registration, links, launch and services.

Three audiences:

- Administrators register tools (``/tools``) and authors place tool resources
  in courses (``/links``), bearer-authenticated like the rest of the API.
- The learner's browser runs the launch: the launch endpoint hands out the
  tool's login URL, and the OIDC authorization endpoint (``/auth``) answers
  the tool's authentication request with an auto-posted ``id_token``. The
  browser carries no API credential there; the single-use
  ``lti_message_hint`` identifies the launch.
- Tools call the services server to server: the token endpoint issues access
  tokens against a client assertion, and the AGS line item and score
  endpoints accept them.
"""

import html
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Body, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    TypeAdapter,
    model_validator,
)
from pydantic.alias_generators import to_camel
from sqlalchemy.orm import Session

from microlms.api.deps import (
    AuditDep,
    CurrentPermissionsDep,
    CurrentUserDep,
    HooksDep,
    ManagementScope,
    SessionDep,
    SettingsDep,
    get_course_in_scope,
    get_enrollment_for_launch,
    get_readable_course,
    require_permission,
    require_platform_permission,
    require_scoped_permission,
)
from microlms.auth.base import AuthenticationError, bearer_token
from microlms.authz import Permission
from microlms.config import Settings
from microlms.domain.activities import (
    ActivityLanguageExistsError,
    ActivityNotFoundError,
    ActivityVariantNotFoundError,
    get_activity,
    get_activity_variant,
    record_activity_launched,
)
from microlms.domain.courses import CourseNotFoundError
from microlms.domain.enrollments import find_enrollment, get_enrollment
from microlms.domain.languages import InvalidLanguageTagError
from microlms.domain.users import get_user
from microlms.events import commit_with_events
from microlms.lrs import LrsClient, LrsClientDep, resolve_actor_home_page
from microlms.modules.lti.links import (
    LtiLaunchError,
    LtiLinkNotFoundError,
    build_launch_claims,
    consume_launch,
    create_link,
    get_link,
    get_variant_link,
    link_course,
    start_launch,
)
from microlms.modules.lti.models import LtiLink, LtiTool
from microlms.modules.lti.scores import (
    ScoreActivityProgress,
    ScoreGradingProgress,
    ScoreRecipientNotEnrolledError,
    ScoreUpdate,
    record_score,
)
from microlms.modules.lti.security import (
    ACCESS_TOKEN_LIFETIME,
    CLIENT_ASSERTION_TYPE,
    GRANTABLE_SCOPES,
    SCOPE_LINEITEM_READONLY,
    SCOPE_SCORE,
    AccessGrant,
    AccessTokenError,
    ClientAssertionError,
    PlatformConfig,
    PlatformConfigDep,
    ToolKeySetClientDep,
    ToolKeySetError,
    issue_access_token,
    public_key_set,
    sign_message,
    unverified_client_id,
    verify_access_token,
    verify_client_assertion,
)
from microlms.modules.lti.statements import launched_statement, score_statement
from microlms.modules.lti.tools import (
    ClientAssertionReplayedError,
    LtiToolNotFoundError,
    get_tool,
    get_tool_by_client_id,
    list_tools,
    record_client_assertion,
    register_tool,
    set_tool_identity_sharing,
)

LINEITEM_MEDIA_TYPE = "application/vnd.ims.lis.v2.lineitem+json"
SCORE_MEDIA_TYPE = "application/vnd.ims.lis.v1.score+json"

_HTTP_URL = TypeAdapter(HttpUrl)


def _http_url(value: str) -> str:
    """Validate an http(s) URL but keep it verbatim: redirect URIs are
    compared exactly, so pydantic's normalization must not rewrite them."""
    _HTTP_URL.validate_python(value)
    return value


UrlStr = Annotated[str, AfterValidator(_http_url)]


class ToolCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    oidc_login_url: UrlStr
    redirect_uris: list[UrlStr] = Field(min_length=1)
    jwks_url: UrlStr
    # Whether launches share the learner's name and email with the tool
    share_identity: bool


class ToolIdentitySharingUpdate(BaseModel):
    share_identity: bool


class PlatformRegistrationRead(BaseModel):
    """The platform side of the security contract, configured in the tool."""

    issuer: str
    client_id: str
    deployment_id: str
    authorization_url: str
    token_url: str
    jwks_url: str


class ToolRead(BaseModel):
    id: int
    name: str
    oidc_login_url: str
    redirect_uris: list[str]
    jwks_url: str
    share_identity: bool
    platform: PlatformRegistrationRead


class LinkCreate(BaseModel):
    course_id: int
    # None appends a new activity titled after the link
    activity_id: int | None = None
    lang: str
    tool_id: int
    title: str = Field(min_length=1, max_length=500)
    target_link_uri: UrlStr
    # Custom properties sent in the launch; values are always strings
    custom: dict[str, str] = Field(default_factory=dict)
    score_maximum: float = Field(gt=0)


class LinkRead(BaseModel):
    id: int
    tool_id: int
    resource_link_id: str
    title: str
    target_link_uri: str
    custom: dict[str, str]
    score_maximum: float


class LinkCreateRead(BaseModel):
    course_id: int
    activity_id: int
    variant_id: int
    lang: str
    link: LinkRead


class LaunchRead(BaseModel):
    # The tool's third-party initiated login URL, for the browser to open
    launch_url: str


class ScoreIn(BaseModel):
    """An AGS score (``application/vnd.ims.lis.v1.score+json``). Extension
    properties the platform does not know are ignored (AGS 3.1.2)."""

    model_config = ConfigDict(alias_generator=to_camel)

    user_id: str
    # ISO 8601 with a time zone designator (AGS 3.4.9)
    timestamp: AwareDatetime
    score_given: float | None = Field(default=None, ge=0)
    score_maximum: float | None = Field(default=None, gt=0)
    activity_progress: ScoreActivityProgress
    grading_progress: ScoreGradingProgress
    comment: str | None = None

    @model_validator(mode="after")
    def _maximum_with_given(self) -> "ScoreIn":
        if self.score_given is not None and self.score_maximum is None:
            raise ValueError("scoreMaximum must be present when scoreGiven is present")
        return self


def _api_base(request: Request, settings: Settings) -> str:
    return str(request.base_url).rstrip("/") + settings.api_prefix


def _lineitem_url(api_base: str, link: LtiLink) -> str:
    return f"{api_base}/lti/lineitems/{link.id}"


def _tool_read(tool: LtiTool, platform: PlatformConfig, api_base: str) -> ToolRead:
    return ToolRead(
        id=tool.id,
        name=tool.name,
        oidc_login_url=tool.oidc_login_url,
        redirect_uris=tool.redirect_uris,
        jwks_url=tool.jwks_url,
        share_identity=tool.share_identity,
        platform=PlatformRegistrationRead(
            issuer=platform.issuer,
            client_id=tool.client_id,
            deployment_id=tool.deployment_id,
            authorization_url=f"{api_base}/lti/auth",
            token_url=f"{api_base}/lti/token",
            jwks_url=f"{api_base}/lti/jwks",
        ),
    )


def _link_read(link: LtiLink) -> LinkRead:
    return LinkRead(
        id=link.id,
        tool_id=link.tool_id,
        resource_link_id=link.resource_link_id,
        title=link.title,
        target_link_uri=link.target_link_uri,
        custom=link.custom,
        score_maximum=link.score_maximum,
    )


def _link_or_404(session: Session, link_id: int) -> LtiLink:
    try:
        return get_link(session, link_id)
    except LtiLinkNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


def _with_query(url: str, params: Mapping[str, str]) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode(params)}"


def _auto_post_page(action: str, fields: Mapping[str, str]) -> HTMLResponse:
    """A page that form-posts ``fields`` to ``action`` on load (OAuth 2.0
    form post response mode), with a button for browsers without script."""
    inputs = "".join(
        f'<input type="hidden" name="{html.escape(name)}" value="{html.escape(value)}">'
        for name, value in fields.items()
    )
    page = (
        '<!doctype html><html><head><meta charset="utf-8"><title>Launching</title></head>'
        f'<body><form method="post" action="{html.escape(action)}">{inputs}'
        '<noscript><button type="submit">Continue</button></noscript></form>'
        "<script>document.forms[0].submit();</script></body></html>"
    )
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


def _token_error(error: str, description: str) -> JSONResponse:
    """An RFC 6749 section 5.2 error response; the Security Framework
    mandates HTTP 400 for refused token requests."""
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status.HTTP_400_BAD_REQUEST,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def _service_grant(request: Request, platform: PlatformConfig, scope: str) -> AccessGrant:
    """The access grant of a tool's service request, requiring ``scope``."""
    try:
        grant = verify_access_token(platform, bearer_token(request))
    except (AuthenticationError, AccessTokenError) as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(error),
            headers={"WWW-Authenticate": "Bearer"},
        ) from error
    if scope not in grant.scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access token lacks scope {scope!r}",
            headers={"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{scope}"'},
        )
    return grant


def _link_for_grant(session: Session, grant: AccessGrant, link_id: int) -> LtiLink:
    """The link a service request targets, 404 unless the calling tool owns
    it, so a tool never learns about another tool's line items."""
    link = _link_or_404(session, link_id)
    if get_tool(session, link.tool_id).client_id != grant.client_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"No line item {link_id} for this tool"
        )
    return link


def _authorize(
    session: Session,
    platform: PlatformConfig,
    lrs: LrsClient,
    actor_home_page: str,
    api_base: str,
    params: Mapping[str, str],
) -> HTMLResponse:
    """Answer an OIDC authentication request (Security Framework 5.1.1.2-3).

    ``client_id`` and ``redirect_uri`` are checked first and failures there
    are refused outright, so nothing is ever posted to an unregistered URI.
    Past that point every failure is an OIDC error response posted back to
    the redirect URI, and success records a ``launched`` statement in the
    LRS and posts the signed ``id_token``.
    """
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    try:
        tool = get_tool_by_client_id(session, client_id)
    except LtiToolNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    if redirect_uri not in tool.redirect_uris:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"redirect_uri {redirect_uri!r} is not registered for client_id {client_id!r}",
        )

    state = params.get("state")

    def error_response(error: str, description: str) -> HTMLResponse:
        fields = {"error": error, "error_description": description}
        if state is not None:
            fields["state"] = state
        return _auto_post_page(redirect_uri, fields)

    if params.get("response_type") != "id_token":
        return error_response("unsupported_response_type", "response_type must be 'id_token'")
    if "openid" not in params.get("scope", "").split():
        return error_response("invalid_scope", "scope must include 'openid'")
    if params.get("response_mode") != "form_post":
        return error_response("invalid_request", "response_mode must be 'form_post'")
    if params.get("prompt") != "none":
        return error_response("invalid_request", "prompt must be 'none'")
    nonce = params.get("nonce")
    if not nonce:
        return error_response("invalid_request", "nonce is required")

    now = datetime.now(UTC)
    try:
        pending = consume_launch(session, params.get("lti_message_hint", ""), now)
    except LtiLaunchError as error:
        return error_response("login_required", str(error))
    # The consumed launch stays consumed whatever the outcome below
    session.commit()
    link = get_link(session, pending.link_id)
    if link.tool_id != tool.id:
        return error_response("invalid_request", "The launch was started for another tool")
    if params.get("login_hint") != str(pending.user_id):
        return error_response("login_required", "login_hint does not match the launching user")

    course = link_course(session, link)
    enrollment = find_enrollment(session, pending.user_id, course.id)
    if enrollment is None:
        return error_response("login_required", "The learner is no longer enrolled in the course")
    claims = build_launch_claims(
        platform.issuer,
        tool,
        link,
        course,
        get_user(session, pending.user_id),
        nonce,
        _lineitem_url(api_base, link),
        now,
    )
    lrs.post_statement(
        launched_statement(
            link,
            get_activity_variant(session, link.variant_id).lang,
            pending.user_id,
            actor_home_page,
            enrollment.registration,
            now,
        )
    )
    fields = {"id_token": sign_message(platform, claims)}
    if state is not None:
        fields["state"] = state
    return _auto_post_page(redirect_uri, fields)


def build_router() -> APIRouter:
    router = APIRouter(prefix="/lti", tags=["lti"])

    @router.get("/status")
    def module_status() -> dict[str, str]:
        return {"module": "lti", "status": "ready"}

    @router.get("/jwks")
    def platform_key_set(platform: PlatformConfigDep) -> JSONResponse:
        """The platform's public JWK Set, verifying its id_tokens."""
        return JSONResponse(
            public_key_set(platform), headers={"Cache-Control": "public, max-age=3600"}
        )

    @router.post(
        "/tools",
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_platform_permission(Permission.LTI_TOOLS_MANAGE))],
    )
    def add_tool(
        request: Request,
        session: SessionDep,
        settings: SettingsDep,
        platform: PlatformConfigDep,
        audit: AuditDep,
        payload: ToolCreate,
    ) -> ToolRead:
        """Register a tool; the response's ``platform`` block is what the
        tool's administrator configures on their side."""
        tool = register_tool(
            session,
            payload.name,
            payload.oidc_login_url,
            payload.redirect_uris,
            payload.jwks_url,
            payload.share_identity,
        )
        audit.record(
            "lti_tool.registered",
            "lti_tool",
            str(tool.id),
            {"name": tool.name, "share_identity": tool.share_identity},
        )
        return _tool_read(tool, platform, _api_base(request, settings))

    @router.patch(
        "/tools/{tool_id}",
        dependencies=[Depends(require_platform_permission(Permission.LTI_TOOLS_MANAGE))],
    )
    def update_tool_identity_sharing(
        request: Request,
        session: SessionDep,
        settings: SettingsDep,
        platform: PlatformConfigDep,
        audit: AuditDep,
        tool_id: int,
        payload: ToolIdentitySharingUpdate,
    ) -> ToolRead:
        """Turn sharing the learner's name and email with the tool on or off,
        from the next launch on. The registration and its ids are kept, so
        the tool side needs no change."""
        try:
            tool = set_tool_identity_sharing(session, tool_id, payload.share_identity)
        except LtiToolNotFoundError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        audit.record(
            "lti_tool.identity_sharing_changed",
            "lti_tool",
            str(tool.id),
            {"share_identity": tool.share_identity},
        )
        return _tool_read(tool, platform, _api_base(request, settings))

    @router.get(
        "/tools",
        dependencies=[Depends(require_platform_permission(Permission.LTI_TOOLS_MANAGE))],
    )
    def read_tools(
        request: Request, session: SessionDep, settings: SettingsDep, platform: PlatformConfigDep
    ) -> list[ToolRead]:
        """Every registered tool, newest first."""
        api_base = _api_base(request, settings)
        return [_tool_read(tool, platform, api_base) for tool in list_tools(session)]

    @router.post("/links", status_code=status.HTTP_201_CREATED)
    def add_link(
        session: SessionDep,
        scope: Annotated[
            ManagementScope, Depends(require_scoped_permission(Permission.CONTENT_IMPORT))
        ],
        audit: AuditDep,
        payload: LinkCreate,
    ) -> LinkCreateRead:
        """Place a tool resource as the ``lang`` variant of a course activity;
        without ``activity_id`` a new activity is appended to the course.
        Org-scoped authors only reach courses their org owns."""
        get_course_in_scope(session, scope, payload.course_id)
        try:
            link = create_link(
                session,
                payload.course_id,
                payload.activity_id,
                payload.lang,
                payload.tool_id,
                payload.title,
                payload.target_link_uri,
                payload.custom,
                payload.score_maximum,
            )
        except InvalidLanguageTagError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
            ) from error
        except (CourseNotFoundError, ActivityNotFoundError, LtiToolNotFoundError) as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ActivityLanguageExistsError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        variant = get_activity_variant(session, link.variant_id)
        audit.record(
            "activity.imported",
            "course",
            str(payload.course_id),
            {
                "activity_id": variant.activity_id,
                "variant_id": variant.id,
                "lang": variant.lang,
                "lti_tool_id": link.tool_id,
            },
        )
        return LinkCreateRead(
            course_id=payload.course_id,
            activity_id=variant.activity_id,
            variant_id=variant.id,
            lang=variant.lang,
            link=_link_read(link),
        )

    @router.get(
        "/variants/{variant_id}/link",
        dependencies=[Depends(require_permission(Permission.CONTENT_ACCESS))],
    )
    def read_variant_link(
        session: SessionDep,
        current_user: CurrentUserDep,
        permissions: CurrentPermissionsDep,
        variant_id: int,
    ) -> LinkRead:
        """The resource link of an activity variant; same visibility as the
        course detail endpoint, so a client can render the launchable item."""
        try:
            variant = get_activity_variant(session, variant_id)
            link = get_variant_link(session, variant.id)
        except (ActivityVariantNotFoundError, LtiLinkNotFoundError) as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        course_id = get_activity(session, variant.activity_id).course_id
        get_readable_course(session, current_user, permissions, course_id)
        return _link_read(link)

    @router.post("/links/{link_id}/launch")
    def launch_link(
        session: SessionDep,
        hooks: HooksDep,
        platform: PlatformConfigDep,
        current_user: CurrentUserDep,
        link_id: int,
    ) -> LaunchRead:
        """Start a launch for the enrolled learner (third-party initiated
        login, Security Framework 5.1.1.1): the returned URL is the tool's
        login endpoint carrying the issuer, the learner as ``login_hint``,
        the target link and the single-use ``lti_message_hint``."""
        link = _link_or_404(session, link_id)
        variant = get_activity_variant(session, link.variant_id)
        enrollment = get_enrollment_for_launch(session, current_user.id, variant.activity_id)
        tool = get_tool(session, link.tool_id)
        launched_at = datetime.now(UTC)
        record_activity_launched(session, enrollment.id, variant.activity_id, launched_at)
        launch = start_launch(session, link, current_user.id, launched_at)
        # The hint must be persisted before the browser reaches the tool
        commit_with_events(session, hooks)
        return LaunchRead(
            launch_url=_with_query(
                tool.oidc_login_url,
                {
                    "iss": platform.issuer,
                    "login_hint": str(current_user.id),
                    "target_link_uri": link.target_link_uri,
                    "lti_message_hint": launch.message_hint,
                    "lti_deployment_id": tool.deployment_id,
                    "client_id": tool.client_id,
                },
            )
        )

    @router.get("/auth", response_class=HTMLResponse)
    def authorize_get(
        request: Request,
        session: SessionDep,
        settings: SettingsDep,
        platform: PlatformConfigDep,
        lrs: LrsClientDep,
    ) -> HTMLResponse:
        """OIDC authorization endpoint, authentication request as a GET."""
        return _authorize(
            session,
            platform,
            lrs,
            resolve_actor_home_page(settings, request),
            _api_base(request, settings),
            dict(request.query_params),
        )

    @router.post("/auth", response_class=HTMLResponse)
    async def authorize_post(
        request: Request,
        session: SessionDep,
        settings: SettingsDep,
        platform: PlatformConfigDep,
        lrs: LrsClientDep,
    ) -> HTMLResponse:
        """OIDC authorization endpoint, authentication request as a form POST."""
        form = await request.form()
        params = {name: value for name, value in form.items() if isinstance(value, str)}
        return _authorize(
            session,
            platform,
            lrs,
            resolve_actor_home_page(settings, request),
            _api_base(request, settings),
            params,
        )

    @router.post("/token")
    def issue_token(
        request: Request,
        session: SessionDep,
        settings: SettingsDep,
        platform: PlatformConfigDep,
        key_sets: ToolKeySetClientDep,
        grant_type: Annotated[str, Form()],
        client_assertion_type: Annotated[str, Form()],
        client_assertion: Annotated[str, Form()],
        scope: Annotated[str, Form()],
    ) -> JSONResponse:
        """OAuth 2.0 client credentials grant with a JWT client assertion
        (Security Framework 4.1.1). Grants the requested scopes the platform
        offers; refuses when none of them is offered."""
        if grant_type != "client_credentials":
            return _token_error("unsupported_grant_type", "grant_type must be client_credentials")
        if client_assertion_type != CLIENT_ASSERTION_TYPE:
            return _token_error(
                "invalid_request", f"client_assertion_type must be {CLIENT_ASSERTION_TYPE}"
            )
        granted = frozenset(scope.split()) & GRANTABLE_SCOPES
        if not granted:
            return _token_error(
                "invalid_scope",
                f"None of the requested scopes is offered; offered: {sorted(GRANTABLE_SCOPES)}",
            )
        try:
            tool = get_tool_by_client_id(session, unverified_client_id(client_assertion))
        except (ClientAssertionError, LtiToolNotFoundError) as error:
            return _token_error("invalid_client", str(error))
        try:
            key_set = key_sets.fetch(tool.jwks_url)
        except (httpx.HTTPError, ToolKeySetError) as error:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Could not load the key set of tool {tool.id} from {tool.jwks_url}: "
                f"{error}",
            ) from error
        now = datetime.now(UTC)
        token_url = f"{_api_base(request, settings)}/lti/token"
        try:
            assertion = verify_client_assertion(
                client_assertion, tool.client_id, key_set, token_url
            )
            record_client_assertion(session, tool.id, assertion.jti, assertion.expires_at, now)
        except (ClientAssertionError, ClientAssertionReplayedError) as error:
            return _token_error("invalid_client", str(error))
        return JSONResponse(
            {
                "access_token": issue_access_token(platform, tool.client_id, granted, now),
                "token_type": "bearer",
                "expires_in": int(ACCESS_TOKEN_LIFETIME.total_seconds()),
                "scope": " ".join(sorted(granted)),
            },
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    @router.get("/lineitems/{link_id}")
    def read_lineitem(
        request: Request,
        session: SessionDep,
        settings: SettingsDep,
        platform: PlatformConfigDep,
        link_id: int,
    ) -> JSONResponse:
        """The link's line item (AGS 3.2), for its tool only."""
        grant = _service_grant(request, platform, SCOPE_LINEITEM_READONLY)
        link = _link_for_grant(session, grant, link_id)
        return JSONResponse(
            {
                "id": _lineitem_url(_api_base(request, settings), link),
                "label": link.title,
                "scoreMaximum": link.score_maximum,
                "resourceLinkId": link.resource_link_id,
            },
            media_type=LINEITEM_MEDIA_TYPE,
        )

    @router.post("/lineitems/{link_id}/scores", status_code=status.HTTP_204_NO_CONTENT)
    def publish_score(
        request: Request,
        session: SessionDep,
        settings: SettingsDep,
        hooks: HooksDep,
        platform: PlatformConfigDep,
        lrs: LrsClientDep,
        link_id: int,
        score: Annotated[ScoreIn, Body(media_type=SCORE_MEDIA_TYPE)],
    ) -> Response:
        """Accept a learner's score for the link's line item (AGS 3.4). An
        update older than the one on record is ignored, as the spec requires.

        An applied score is recorded in the LRS (``completed`` or ``scored``)
        before the transaction commits: when the LRS refuses it, the score is
        not stored either and the request fails, so the tool retries it
        rather than the LRS silently missing it."""
        grant = _service_grant(request, platform, SCOPE_SCORE)
        link = _link_for_grant(session, grant, link_id)
        try:
            user_id = uuid.UUID(score.user_id)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"userId {score.user_id!r} is not a user id this platform issued",
            ) from error
        update = ScoreUpdate(
            user_id=user_id,
            timestamp=score.timestamp,
            score_given=score.score_given,
            score_maximum=score.score_maximum,
            activity_progress=score.activity_progress,
            grading_progress=score.grading_progress,
            comment=score.comment,
        )
        try:
            result = record_score(session, link, update)
        except ScoreRecipientNotEnrolledError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        if result is not None:
            statement = score_statement(
                link,
                get_activity_variant(session, link.variant_id).lang,
                result.enrollment_id,
                update,
                resolve_actor_home_page(settings, request),
                get_enrollment(session, result.enrollment_id).registration,
            )
            if statement is not None:
                lrs.post_statement(statement)
        commit_with_events(session, hooks)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
