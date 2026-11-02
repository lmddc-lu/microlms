"""microLMS core: a reusable LMS engine consumed by client projects.

Client projects import :func:`create_app`, pass their own settings, database
engine (:func:`create_db_engine`) and backend implementations, and mount extra
routers on the returned app.
"""

from microlms.app import create_app
from microlms.authz import (
    DEFAULT_ACCESS_POLICY,
    AccessPolicy,
    GrantedPermissions,
    Permission,
    has_platform_permission,
    holds_permission,
    orgs_granting,
)
from microlms.config import Settings
from microlms.db.base import UnsupportedDatabaseError, create_db_engine, create_session_factory
from microlms.events import NullEventHooks, commit_with_events
from microlms.scheduler import PeriodicTask

__all__ = [
    "DEFAULT_ACCESS_POLICY",
    "AccessPolicy",
    "GrantedPermissions",
    "NullEventHooks",
    "PeriodicTask",
    "Permission",
    "Settings",
    "UnsupportedDatabaseError",
    "commit_with_events",
    "create_app",
    "create_db_engine",
    "create_session_factory",
    "has_platform_permission",
    "holds_permission",
    "orgs_granting",
]
