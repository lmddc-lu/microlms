"""Demo client: anonymous learners upload SCORM packages and play them.

Run with: uv run uvicorn main:app --reload (or via docker compose).

Every visitor registers without credentials (``AnonymousAuthBackend``) as a
``learner`` of the ``playground`` organization, seeded by this demo's
migration. The demo's access policy lets that role create and publish
courses, import packages and enroll, so any visitor can upload a package and
play it. Package scripts run on the app origin, so only run this locally.
"""

from pathlib import Path

from fastapi.responses import FileResponse

from microlms import AccessPolicy, Permission, Settings, create_app, create_db_engine
from microlms.auth import AnonymousAuthBackend
from microlms.events import NullEventHooks
from microlms.storage import FilesystemStorage

DEMO_DIR = Path(__file__).parent

# The anonymous backend registers every visitor as "learner"
ACCESS_POLICY = AccessPolicy(
    roles={
        "learner": frozenset(
            {
                Permission.CONTENT_ACCESS,
                Permission.CONTENT_IMPORT,
                Permission.COURSE_CREATE,
                Permission.COURSE_PUBLISH,
                Permission.ENROLLMENT_CREATE,
            }
        ),
    },
    assign_permissions={},
)

settings = Settings()
app = create_app(
    settings=settings,
    engine=create_db_engine(settings.database_url, echo=settings.debug),
    auth=AnonymousAuthBackend(),
    storage=FilesystemStorage(DEMO_DIR / "var" / "storage"),
    hooks=NullEventHooks(),
    routers=["users", "courses", "enrollments"],
    modules=["scorm"],
    schedules=[],
    access_policy=ACCESS_POLICY,
)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """The demo's single page."""
    return FileResponse(DEMO_DIR / "index.html")
