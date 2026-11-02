"""H5P HTTP endpoints: import, content serving, launch, player and runtime.

The player page is the load-bearing piece: it runs the vendored h5p-standalone
player (served by the player-assets endpoint) against the extracted package
files, subscribes to the xAPI events the content emits in the browser, and
forwards each statement to the runtime endpoint below, authenticated by an
opaque per-launch token. The server stamps the canonical actor and forwards to
the LRS, so no LRS credential ever reaches the client.
"""

import json
import mimetypes
from datetime import UTC, datetime
from importlib import resources
from importlib.resources.abc import Traversable
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
from microlms.lrs import LrsClientDep, resolve_actor_home_page
from microlms.modules.h5p.importer import (
    H5pImportError,
    ImportResult,
    import_package,
    replace_package,
)
from microlms.modules.h5p.models import H5pAttempt, H5pContent
from microlms.modules.h5p.tracking import observe_statements, start_attempt

_PLAYER_TEMPLATE = (
    resources.files("microlms.modules.h5p").joinpath("player.html").read_text(encoding="utf-8")
)
_PLAYER_CONFIG_PLACEHOLDER = "__H5P_PLAYER_CONFIG__"
_MAIN_SRC_PLACEHOLDER = "__H5P_MAIN_SRC__"

# Media types the vendored assets need that mimetypes may not know
_ASSET_MEDIA_TYPES = {
    ".js": "text/javascript",
    ".css": "text/css",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


def _load_player_assets() -> dict[str, bytes]:
    """The vendored h5p-standalone dist files (see the assets directory),
    keyed by their path relative to it. Loading everything up front makes the
    asset endpoint an allowlist lookup, immune to path traversal."""
    assets: dict[str, bytes] = {}

    def walk(node: Traversable, prefix: str) -> None:
        for child in node.iterdir():
            path = f"{prefix}{child.name}"
            if child.is_dir():
                walk(child, f"{path}/")
            else:
                assets[path] = child.read_bytes()

    walk(resources.files("microlms.modules.h5p").joinpath("assets"), "")
    return assets


_PLAYER_ASSETS = _load_player_assets()


class H5pContentRead(BaseModel):
    id: int
    title: str
    main_library: str


class H5pImportRead(BaseModel):
    course_id: int
    activity_id: int
    variant_id: int
    lang: str
    content: H5pContentRead


class PlayerLaunchRead(BaseModel):
    player_url: str


def _content_or_404(session: SessionDep, content_id: int) -> H5pContent:
    content = session.get(H5pContent, content_id)
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown H5P content")
    return content


def _import_read(result: ImportResult) -> H5pImportRead:
    return H5pImportRead(
        course_id=result.course_id,
        activity_id=result.activity_id,
        variant_id=result.variant_id,
        lang=result.lang,
        content=H5pContentRead(
            id=result.content.id,
            title=result.content.title,
            main_library=result.content.main_library,
        ),
    )


def _course_id_for_variant(session: SessionDep, variant: ActivityVariant) -> int:
    return get_activity(session, variant.activity_id).course_id


def _attempt_for_token(session: SessionDep, token: str) -> H5pAttempt:
    attempt = session.scalars(select(H5pAttempt).where(H5pAttempt.auth_token == token)).first()
    if attempt is None or attempt.token_expires_at <= datetime.now(UTC):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unknown or expired H5P player token",
        )
    return attempt


def build_router() -> APIRouter:
    router = APIRouter(prefix="/h5p", tags=["h5p"])

    @router.get("/status")
    def module_status() -> dict[str, str]:
        return {"module": "h5p", "status": "ready"}

    @router.post(
        "/import",
        status_code=status.HTTP_201_CREATED,
    )
    async def import_h5p_package(
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
    ) -> H5pImportRead:
        """Import a package as the ``lang`` variant of a course activity;
        without ``activity_id`` a new activity is appended to the course.
        Org-scoped importers only reach courses their org owns."""
        get_course_in_scope(session, scope, course_id)
        try:
            result = import_package(
                session, storage, await file.read(), course_id, activity_id, lang
            )
        except (H5pImportError, InvalidLanguageTagError) as error:
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
    async def replace_h5p_package(
        session: SessionDep,
        scope: Annotated[
            ManagementScope, Depends(require_scoped_permission(Permission.CONTENT_IMPORT))
        ],
        storage: StorageDep,
        audit: AuditDep,
        cleanup: BackgroundTasks,
        variant_id: int,
        file: UploadFile,
    ) -> H5pImportRead:
        """Replace the package of an H5P variant in place.

        Its content and every learner attempt on it are deleted, so learners start
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
        except H5pImportError as error:
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
        "/variants/{variant_id}/content",
        dependencies=[Depends(require_permission(Permission.CONTENT_ACCESS))],
    )
    def get_variant_content(
        session: SessionDep,
        current_user: CurrentUserDep,
        permissions: CurrentPermissionsDep,
        variant_id: int,
    ) -> H5pContentRead:
        """The content item of an activity variant; same visibility as the
        course detail endpoint, so a client can render the launchable item."""
        try:
            variant = get_activity_variant(session, variant_id)
        except ActivityVariantNotFoundError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        course_id = _course_id_for_variant(session, variant)
        get_readable_course(session, current_user, permissions, course_id)
        content = session.scalars(
            select(H5pContent).where(H5pContent.variant_id == variant.id)
        ).first()
        if content is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Variant {variant_id} holds no H5P content",
            )
        return H5pContentRead(
            id=content.id,
            title=content.title,
            main_library=content.main_library,
        )

    @router.get("/content/{token}/{file_path:path}")
    def serve_content(
        request: Request, session: SessionDep, storage: StorageDep, token: str, file_path: str
    ) -> Response:
        """Stream a file of the package the player token launched, honoring
        Range requests so large media (e.g. videos) plays and seeks in the
        browser. The token scopes the content to an enrolled learner's launch
        (the player carries no API credential); relaunching rotates it."""
        attempt = _attempt_for_token(session, token)
        content = _content_or_404(session, attempt.content_id)
        return serve_package_file(
            storage, content.storage_prefix, file_path, request.headers.get("range")
        )

    @router.post("/contents/{content_id}/launch")
    def launch_content(
        request: Request,
        session: SessionDep,
        settings: SettingsDep,
        current_user: CurrentUserDep,
        content_id: int,
    ) -> PlayerLaunchRead:
        content = _content_or_404(session, content_id)
        variant = get_activity_variant(session, content.variant_id)
        enrollment = get_enrollment_for_launch(session, current_user.id, variant.activity_id)
        launched_at = datetime.now(UTC)
        record_activity_launched(session, enrollment.id, variant.activity_id, launched_at)
        api_base = str(request.base_url).rstrip("/") + settings.api_prefix
        attempt = start_attempt(
            session, content, current_user.id, enrollment.id, launched_at + settings.launch_max_age
        )
        return PlayerLaunchRead(player_url=f"{api_base}/h5p/player/{attempt.auth_token}")

    @router.get("/player-assets/{asset_path:path}")
    def player_asset(asset_path: str) -> Response:
        asset = _PLAYER_ASSETS.get(asset_path)
        if asset is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Unknown player asset"
            )
        suffix = "." + asset_path.rsplit(".", 1)[-1]
        media_type = (
            _ASSET_MEDIA_TYPES.get(suffix)
            or mimetypes.guess_type(asset_path)[0]
            or "application/octet-stream"
        )
        return Response(
            content=asset,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @router.get("/player/{token}", response_class=HTMLResponse)
    def player_page(
        request: Request, session: SessionDep, settings: SettingsDep, token: str
    ) -> HTMLResponse:
        attempt = _attempt_for_token(session, token)
        content = _content_or_404(session, attempt.content_id)
        api_base = str(request.base_url).rstrip("/") + settings.api_prefix
        config = {
            "title": content.title,
            "contentUrl": f"{api_base}/h5p/content/{token}",
            "assetsUrl": f"{api_base}/h5p/player-assets",
            "objectIri": content.activity_iri,
            "statementsUrl": f"{api_base}/h5p/runtime/{token}/statements",
        }
        # <-escape so the JSON can never close the surrounding <script>
        config_json = json.dumps(config).replace("<", "\\u003c")
        page = _PLAYER_TEMPLATE.replace(
            _MAIN_SRC_PLACEHOLDER, f"{api_base}/h5p/player-assets/main.bundle.js"
        ).replace(_PLAYER_CONFIG_PLACEHOLDER, config_json)
        return HTMLResponse(page)

    @router.post("/runtime/{token}/statements", status_code=status.HTTP_204_NO_CONTENT)
    def runtime_statements(
        request: Request,
        session: SessionDep,
        settings: SettingsDep,
        hooks: HooksDep,
        lrs: LrsClientDep,
        token: str,
        statements: list[dict[str, object]],
    ) -> None:
        """Receive the xAPI statements the content emitted in the player,
        stamp the canonical actor and registration, forward them to the LRS
        and record satisfaction when the root content reports completion."""
        attempt = _attempt_for_token(session, token)
        content = _content_or_404(session, attempt.content_id)
        observe_statements(
            session,
            hooks,
            lrs,
            attempt,
            content,
            statements,
            resolve_actor_home_page(settings, request),
        )

    return router
