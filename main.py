"""Development entry point for working on the core itself.

Run with: uv run uvicorn main:app --reload
Requires MICROLMS_DATABASE_URL (see .env.example).

Client projects do not use this file; they build their own app the same way
with their own settings and backends.
"""

from pathlib import Path

from microlms import DEFAULT_ACCESS_POLICY, Settings, create_app, create_db_engine
from microlms.auth import LocalAuthBackend
from microlms.events import NullEventHooks
from microlms.storage import FilesystemStorage

settings = Settings()
app = create_app(
    settings=settings,
    engine=create_db_engine(settings.database_url, echo=settings.debug),
    auth=LocalAuthBackend(),
    storage=FilesystemStorage(Path("var/storage")),
    hooks=NullEventHooks(),
    routers=["users", "orgs", "courses", "enrollments", "audit"],
    modules=["scorm", "cmi5", "h5p"],
    schedules=[],
    access_policy=DEFAULT_ACCESS_POLICY,
)
