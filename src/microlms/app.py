"""Application factory: the single supported entry point for client projects.

Everything a client can customize goes through the ``create_app`` parameters;
core internals are not a supported surface.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.openapi.models import SecurityScheme
from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from microlms.api import CoreRouterName, build_core_router
from microlms.api.deps import get_current_user
from microlms.auth.base import AuthBackend
from microlms.authz import AccessPolicy, validate_access_policy
from microlms.config import Settings
from microlms.db.base import create_session_factory
from microlms.domain.activities import ActivityVariant
from microlms.events import EventHooks
from microlms.modules import (
    ContentModuleNotEnabledError,
    ModuleName,
    build_module_router,
    build_variant_purger,
)
from microlms.scheduler import PeriodicTask, run_periodic
from microlms.storage.base import StorageBackend

_SECURITY_SCHEME_NAME = "AuthBackend"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine: Engine = app.state.engine
    schedules: Sequence[PeriodicTask] = app.state.schedules
    runners = [asyncio.create_task(run_periodic(engine, task)) for task in schedules]
    yield
    for runner in runners:
        runner.cancel()
    await asyncio.gather(*runners, return_exceptions=True)
    engine.dispose()


def _resolves_current_user(dependant: Dependant) -> bool:
    """Whether a route's dependency tree authenticates the request."""
    if dependant.call is get_current_user:
        return True
    return any(_resolves_current_user(sub) for sub in dependant.dependencies)


def _install_openapi_security(app: FastAPI, scheme: SecurityScheme) -> None:
    """Advertise the auth backend's security scheme in the OpenAPI schema.

    FastAPI derives security schemes from route dependencies at definition
    time, but the backend is only known here, so the generated schema is
    patched instead: the scheme goes into components and every operation that
    resolves the current user gets the requirement. Swagger UI then offers
    Authorize on exactly the authenticated endpoints.
    """
    default_openapi = app.openapi

    def openapi_with_security() -> dict[str, Any]:
        schema = default_openapi()
        components = schema.setdefault("components", {})
        components.setdefault("securitySchemes", {})[_SECURITY_SCHEME_NAME] = scheme.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
        # Routers are included lazily, so the flat route list only exists at
        # schema-generation time; iter_route_contexts is what get_openapi
        # itself iterates. This also covers routers the client mounts after
        # create_app returns.
        for context in iter_route_contexts(app.routes):
            route = context.original_route
            if not isinstance(route, APIRoute) or not context.include_in_schema:
                continue
            if not _resolves_current_user(route.dependant):
                continue
            for method in context.methods or ():
                operation = schema["paths"][context.path_format][method.lower()]
                operation["security"] = [{_SECURITY_SCHEME_NAME: []}]
        return schema

    app.openapi = openapi_with_security  # type: ignore[method-assign]


def create_app(
    settings: Settings,
    engine: Engine,
    auth: AuthBackend,
    storage: StorageBackend,
    hooks: EventHooks,
    routers: Sequence[CoreRouterName],
    modules: Sequence[ModuleName],
    schedules: Sequence[PeriodicTask],
    access_policy: AccessPolicy,
) -> FastAPI:
    """Build a microLMS application from client-provided backends.

    The client project owns the instances it passes in; the core wires them
    into request dependencies and mounts the selected core routers and content
    modules. ``engine`` is the database engine the app runs on, built with
    ``microlms.create_db_engine``; the client shares it with its own database
    work (event hooks, periodic tasks) through
    ``microlms.create_session_factory``, so each process holds a single
    connection pool. The app disposes the engine's pool when it shuts down.
    ``routers`` selects which core routers mount; any subset composes, because
    routers depend only on domain services and never on each other - a
    deselected router only removes its endpoints, and a client may mount its
    own replacement built on ``microlms.domain``. ``schedules`` lists the
    client's periodic background tasks, run inside the app lifespan (see
    :mod:`microlms.scheduler`). ``access_policy`` maps each role label the
    deployment uses to the permission strings it grants (see
    :mod:`microlms.authz`); pass ``microlms.DEFAULT_ACCESS_POLICY`` for the
    bundled roles, or an extended/replaced policy for custom ones. A wildcard
    grant, an unknown core permission, an ``xapi_guest_role`` absent from the
    policy, or a guest role without its guest org (or the reverse) is
    rejected here rather than silently never matching.
    """
    validate_access_policy(access_policy)
    guest_role = settings.xapi_guest_role
    if (guest_role is None) != (settings.xapi_guest_org_slug is None):
        raise ValueError(
            "Settings.xapi_guest_role and Settings.xapi_guest_org_slug must be set together: "
            "guests hold the role inside that organization"
        )
    if guest_role is not None and guest_role not in access_policy.roles:
        raise ValueError(
            f"Settings.xapi_guest_role {guest_role!r} must be a role of the access "
            "policy: guests hold it inside the xapi_guest_org_slug organization"
        )
    app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=_lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.auth = auth
    app.state.storage = storage
    app.state.hooks = hooks
    app.state.schedules = schedules
    app.state.access_policy = access_policy

    # Per-variant content teardown, dispatched on the variant's owning module.
    # Built over the enabled modules so deleting an activity/variant removes the
    # content module's rows and stored files (see domain.activities delete_*).
    module_purgers = {name: build_variant_purger(name) for name in modules}

    def purge_variant_content(session: Session, variant: ActivityVariant) -> None:
        purger = module_purgers.get(variant.module)
        if purger is None:
            raise ContentModuleNotEnabledError(
                f"Cannot delete content owned by module {variant.module!r}: "
                "it is not enabled for this app"
            )
        purger(session, storage, variant.id)

    app.state.variant_purger = purge_variant_content

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok", "app": settings.app_name}

    for router_name in routers:
        app.include_router(build_core_router(router_name), prefix=settings.api_prefix)

    for module_name in modules:
        app.include_router(build_module_router(module_name), prefix=settings.api_prefix)

    auth_router = auth.router()
    if auth_router is not None:
        app.include_router(auth_router, prefix="/auth", tags=["auth"])

    # After all routers are mounted, so every secured route is known
    security_scheme = auth.openapi_security()
    if security_scheme is not None:
        _install_openapi_security(app, security_scheme)

    return app
