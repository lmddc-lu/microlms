"""SCORM HTTP endpoints: import, content serving, launch, player and runtime.

The player page is the load-bearing piece: SCORM content expects the LMS to
expose a JavaScript runtime object in a parent window (``API`` for SCORM 1.2,
``API_1484_11`` for SCORM 2004), so launch returns a player URL that hosts the
SCO in an iframe and serves the runtime build matching the package's edition.
The player normalizes both data models into one typed payload for the runtime
endpoints below, authenticated by an opaque per-launch token, and the server
converts the transitions to xAPI statements.
"""

import json
from datetime import UTC, datetime
from importlib import resources
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, Response
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
from microlms.domain.users import User
from microlms.lrs import LrsClientDep, resolve_actor_home_page
from microlms.modules.scorm.importer import (
    ImportResult,
    ScormImportError,
    import_package,
    replace_package,
)
from microlms.modules.scorm.models import (
    CompletionStatus,
    ScormAttempt,
    ScormResourceType,
    ScormSco,
    ScormVersion,
    SuccessStatus,
)
from microlms.modules.scorm.runtime import (
    RuntimeCommit,
    RuntimeTermination,
    ScormDurationError,
    format_duration,
    record_commit,
    record_termination,
    start_attempt,
)

_PLAYER_TEMPLATE = (
    resources.files("microlms.modules.scorm").joinpath("player.html").read_text(encoding="utf-8")
)
_PLAYER_CONFIG_PLACEHOLDER = "__SCORM_PLAYER_CONFIG__"
# Vendored scorm-again builds, one per SCORM edition: the Scorm12API (window
# API) and Scorm2004API (window API_1484_11) implementations the player page
# loads (see the version banner in the files themselves)
_RUNTIME_ASSET_NAMES: dict[ScormVersion, str] = {
    ScormVersion.SCORM_12: "scorm-again-12.min.js",
    ScormVersion.SCORM_2004: "scorm-again-2004.min.js",
}
_RUNTIME_JS: dict[str, str] = {
    name: resources.files("microlms.modules.scorm").joinpath(name).read_text(encoding="utf-8")
    for name in _RUNTIME_ASSET_NAMES.values()
}
_RUNTIME_SRC_PLACEHOLDER = "__SCORM_RUNTIME_SRC__"


class ScormScoRead(BaseModel):
    id: int
    identifier: str
    title: str
    scorm_type: ScormResourceType


class ScormImportRead(BaseModel):
    course_id: int
    activity_id: int
    variant_id: int
    lang: str
    version: ScormVersion
    scos: list[ScormScoRead]


class PlayerLaunchRead(BaseModel):
    player_url: str


class RuntimeStateRead(BaseModel):
    """The initial cmi values the player seeds its data model with."""

    learner_id: str
    learner_name: str
    entry: str
    completion_status: CompletionStatus
    success_status: SuccessStatus
    score_scaled: float | None
    score_raw: float | None
    progress_measure: float | None
    location: str | None
    suspend_data: str | None
    total_time: str
    credit: str
    mode: str


def _sco_or_404(session: SessionDep, sco_id: int) -> ScormSco:
    sco = session.get(ScormSco, sco_id)
    if sco is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown SCO")
    return sco


def _import_read(result: ImportResult) -> ScormImportRead:
    return ScormImportRead(
        course_id=result.course_id,
        activity_id=result.activity_id,
        variant_id=result.variant_id,
        lang=result.lang,
        version=result.version,
        scos=[
            ScormScoRead(
                id=sco.id,
                identifier=sco.identifier,
                title=sco.title,
                scorm_type=sco.scorm_type,
            )
            for sco in result.scos
        ],
    )


def _course_id_for_variant(session: SessionDep, variant: ActivityVariant) -> int:
    return get_activity(session, variant.activity_id).course_id


def _attempt_for_token(session: SessionDep, token: str) -> ScormAttempt:
    attempt = session.scalars(select(ScormAttempt).where(ScormAttempt.auth_token == token)).first()
    if attempt is None or attempt.token_expires_at <= datetime.now(UTC):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unknown or expired SCORM player token",
        )
    return attempt


def build_router() -> APIRouter:
    router = APIRouter(prefix="/scorm", tags=["scorm"])

    @router.get("/status")
    def module_status() -> dict[str, str]:
        return {"module": "scorm", "status": "ready"}

    @router.post(
        "/import",
        status_code=status.HTTP_201_CREATED,
    )
    async def import_scorm_package(
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
    ) -> ScormImportRead:
        """Import a package as the ``lang`` variant of a course activity;
        without ``activity_id`` a new activity is appended to the course.
        Org-scoped importers only reach courses their org owns."""
        get_course_in_scope(session, scope, course_id)
        try:
            result = import_package(
                session, storage, await file.read(), course_id, activity_id, lang
            )
        except (ScormImportError, InvalidLanguageTagError) as error:
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
    async def replace_scorm_package(
        session: SessionDep,
        scope: Annotated[
            ManagementScope, Depends(require_scoped_permission(Permission.CONTENT_IMPORT))
        ],
        storage: StorageDep,
        audit: AuditDep,
        cleanup: BackgroundTasks,
        variant_id: int,
        file: UploadFile,
    ) -> ScormImportRead:
        """Replace the package of a scorm variant in place.

        Its SCOs and every learner attempt on them are deleted, so learners start
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
        except ScormImportError as error:
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
        "/variants/{variant_id}/scos",
        dependencies=[Depends(require_permission(Permission.CONTENT_ACCESS))],
    )
    def list_variant_scos(
        session: SessionDep,
        current_user: CurrentUserDep,
        permissions: CurrentPermissionsDep,
        variant_id: int,
    ) -> list[ScormScoRead]:
        """The SCOs of an activity variant, in manifest order; same visibility
        as the course detail endpoint, so a client can render launchable items."""
        try:
            variant = get_activity_variant(session, variant_id)
        except ActivityVariantNotFoundError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        course_id = _course_id_for_variant(session, variant)
        get_readable_course(session, current_user, permissions, course_id)
        scos = session.scalars(
            select(ScormSco).where(ScormSco.variant_id == variant.id).order_by(ScormSco.sco_index)
        ).all()
        return [
            ScormScoRead(
                id=sco.id,
                identifier=sco.identifier,
                title=sco.title,
                scorm_type=sco.scorm_type,
            )
            for sco in scos
        ]

    @router.get("/content/{token}/{file_path:path}")
    def serve_content(
        request: Request, session: SessionDep, storage: StorageDep, token: str, file_path: str
    ) -> Response:
        """Stream a file of the package the player token launched, honoring
        Range requests so large media (e.g. videos) plays and seeks in the
        browser. The token scopes the content to an enrolled learner's launch
        (the iframe carries no API credential); relaunching rotates it."""
        attempt = _attempt_for_token(session, token)
        sco = _sco_or_404(session, attempt.sco_id)
        return serve_package_file(
            storage, sco.storage_prefix, file_path, request.headers.get("range")
        )

    @router.post("/scos/{sco_id}/launch")
    def launch_sco(
        request: Request,
        session: SessionDep,
        settings: SettingsDep,
        lrs: LrsClientDep,
        current_user: CurrentUserDep,
        sco_id: int,
    ) -> PlayerLaunchRead:
        sco = _sco_or_404(session, sco_id)
        variant = get_activity_variant(session, sco.variant_id)
        enrollment = get_enrollment_for_launch(session, current_user.id, variant.activity_id)
        launched_at = datetime.now(UTC)
        record_activity_launched(session, enrollment.id, variant.activity_id, launched_at)
        api_base = str(request.base_url).rstrip("/") + settings.api_prefix
        attempt = start_attempt(
            session,
            lrs,
            sco,
            current_user.id,
            enrollment.id,
            resolve_actor_home_page(settings, request),
            launched_at + settings.launch_max_age,
        )
        return PlayerLaunchRead(player_url=f"{api_base}/scorm/player/{attempt.auth_token}")

    @router.get("/player-assets/{asset_name}")
    def player_runtime_asset(asset_name: str) -> Response:
        asset = _RUNTIME_JS.get(asset_name)
        if asset is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Unknown player asset"
            )
        return Response(
            content=asset,
            media_type="text/javascript",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @router.get("/player/{token}", response_class=HTMLResponse)
    def player_page(
        request: Request, session: SessionDep, settings: SettingsDep, token: str
    ) -> HTMLResponse:
        attempt = _attempt_for_token(session, token)
        sco = _sco_or_404(session, attempt.sco_id)
        api_base = str(request.base_url).rstrip("/") + settings.api_prefix
        config = {
            "title": sco.title,
            "version": sco.version.value,
            "contentUrl": f"{api_base}/scorm/content/{token}/{sco.launch_url}",
            "stateUrl": f"{api_base}/scorm/runtime/{token}",
            "commitUrl": f"{api_base}/scorm/runtime/{token}/commit",
            "terminateUrl": f"{api_base}/scorm/runtime/{token}/terminate",
        }
        # <-escape so the JSON can never close the surrounding <script>
        config_json = json.dumps(config).replace("<", "\\u003c")
        asset_name = _RUNTIME_ASSET_NAMES[sco.version]
        page = _PLAYER_TEMPLATE.replace(
            _RUNTIME_SRC_PLACEHOLDER, f"{api_base}/scorm/player-assets/{asset_name}"
        ).replace(_PLAYER_CONFIG_PLACEHOLDER, config_json)
        return HTMLResponse(page)

    @router.get("/runtime/{token}")
    def runtime_state(session: SessionDep, token: str) -> RuntimeStateRead:
        attempt = _attempt_for_token(session, token)
        learner = session.get(User, attempt.user_id)
        return RuntimeStateRead(
            learner_id=str(attempt.user_id),
            learner_name=(learner.full_name if learner is not None else "") or "",
            entry="resume" if attempt.suspended else "ab-initio",
            completion_status=attempt.completion_status,
            success_status=attempt.success_status,
            score_scaled=attempt.score_scaled,
            score_raw=attempt.score_raw,
            progress_measure=attempt.progress_measure,
            location=attempt.location,
            suspend_data=attempt.suspend_data,
            total_time=format_duration(attempt.total_time_seconds),
            credit="credit",
            mode="normal",
        )

    @router.post("/runtime/{token}/commit", status_code=status.HTTP_204_NO_CONTENT)
    def runtime_commit(
        request: Request,
        session: SessionDep,
        settings: SettingsDep,
        hooks: HooksDep,
        lrs: LrsClientDep,
        token: str,
        commit: RuntimeCommit,
    ) -> None:
        attempt = _attempt_for_token(session, token)
        sco = _sco_or_404(session, attempt.sco_id)
        record_commit(
            session, hooks, lrs, attempt, sco, commit, resolve_actor_home_page(settings, request)
        )

    @router.post("/runtime/{token}/terminate", status_code=status.HTTP_204_NO_CONTENT)
    def runtime_terminate(
        request: Request,
        session: SessionDep,
        settings: SettingsDep,
        hooks: HooksDep,
        lrs: LrsClientDep,
        token: str,
        termination: RuntimeTermination,
    ) -> None:
        attempt = _attempt_for_token(session, token)
        sco = _sco_or_404(session, attempt.sco_id)
        try:
            record_termination(
                session,
                hooks,
                lrs,
                attempt,
                sco,
                termination,
                resolve_actor_home_page(settings, request),
            )
        except ScormDurationError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
            ) from error

    return router
