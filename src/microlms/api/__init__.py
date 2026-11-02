"""HTTP layer: routers and request-scoped dependencies.

Core routers are selected per deployment via ``create_app(routers=[...])``.
Each router depends only on ``domain/`` services and ``api/deps.py``, never on
another router, so any subset composes: deselecting a router removes its
endpoints while the domain services and tables behind it stay available (e.g. a
client may replace the core courses router with its own and core enrollments
keep working against the courses it creates).

Unlike content modules, core routers are imported eagerly: they have no
optional dependencies, and importing them registers every core domain model on
``Base.metadata`` regardless of which routers a deployment selects.
"""

from typing import Literal

from fastapi import APIRouter

from microlms.api import audit, courses, enrollments, orgs, users

CoreRouterName = Literal["users", "orgs", "courses", "enrollments", "audit"]

CORE_ROUTERS: dict[CoreRouterName, APIRouter] = {
    "users": users.router,
    "orgs": orgs.router,
    "courses": courses.router,
    "enrollments": enrollments.router,
    "audit": audit.router,
}


class UnknownCoreRouterError(Exception):
    """Raised when create_app receives a core router name that does not exist."""


def build_core_router(name: CoreRouterName) -> APIRouter:
    try:
        return CORE_ROUTERS[name]
    except KeyError:
        raise UnknownCoreRouterError(
            f"Unknown core router {name!r}, available: {sorted(CORE_ROUTERS)}"
        ) from None
