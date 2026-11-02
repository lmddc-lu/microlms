"""create_app composition: client routers, API prefix, router selection, schedules."""

import time
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from microlms import (
    DEFAULT_ACCESS_POLICY,
    PeriodicTask,
    Settings,
    create_app,
    create_db_engine,
    create_session_factory,
)
from microlms.api import CoreRouterName, UnknownCoreRouterError
from microlms.auth import AnonymousAuthBackend
from microlms.domain.orgs import get_org_by_slug
from microlms.domain.users import UserRole
from microlms.events import NullEventHooks
from microlms.storage import FilesystemStorage
from tests.helpers import (
    ALL_CORE_ROUTERS,
    DEFAULT_ORG_SLUG,
    RecordingHooks,
    default_org_id,
    seed_admin,
    seed_default_org,
)


def test_client_router_composes_services_in_one_transaction(
    tmp_path: Path, database_url: str
) -> None:
    """A client endpoint composes two domain services atomically: one commit
    covers both, events dispatch only after it, and a failure between the
    services rolls everything back without any event firing."""
    admin_token = seed_admin(database_url)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    hooks = RecordingHooks()

    app = create_app(
        settings=Settings(database_url=database_url, registration_org_slug=DEFAULT_ORG_SLUG),
        engine=create_db_engine(database_url, echo=False),
        auth=AnonymousAuthBackend(),
        storage=FilesystemStorage(tmp_path / "storage"),
        hooks=hooks,
        routers=ALL_CORE_ROUTERS,
        modules=[],
        schedules=[],
        access_policy=DEFAULT_ACCESS_POLICY,
    )

    from microlms.api.deps import HooksDep, SessionDep
    from microlms.domain.courses import CourseNotFoundError, get_course
    from microlms.domain.enrollments import enroll_user
    from microlms.domain.users import create_user
    from microlms.events import commit_with_events

    onboard = APIRouter()

    @onboard.post("/api/v1/onboard", status_code=201)
    def onboard_learner(
        session: SessionDep, request_hooks: HooksDep, email: str, course_id: int
    ) -> dict[str, str]:
        # create_user runs first on purpose: an unknown course must roll it back
        user = create_user(
            session,
            full_name="Onboarded",
            roles={UserRole.LEARNER},
            org_id=None,
            lang=None,
            email=email,
        )
        try:
            course = get_course(session, course_id)
        except CourseNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        enrollment = enroll_user(session, user_id=user.id, course_id=course.id, due_at=None)
        commit_with_events(session, request_hooks)
        return {"user_id": str(user.id), "enrollment_id": str(enrollment.id)}

    app.include_router(onboard)

    with TestClient(app) as client:
        course = client.post(
            "/api/v1/courses",
            headers=admin_headers,
            json={"owner_org_id": None, "slug": "compose", "title": "Compose", "description": ""},
        ).json()
        client.post(f"/api/v1/courses/{course['id']}/publish", headers=admin_headers)
        users_before = len(client.get("/api/v1/users", headers=admin_headers).json())

        # Failure between the two services: nothing persists, nothing fires
        failed = client.post(
            "/api/v1/onboard", params={"email": "gone@example.com", "course_id": 999}
        )
        assert failed.status_code == 404
        assert len(client.get("/api/v1/users", headers=admin_headers).json()) == users_before
        assert hooks.created == []
        assert hooks.enrolled == []

        # Success: one transaction, both events dispatched after the commit
        created = client.post(
            "/api/v1/onboard", params={"email": "new@example.com", "course_id": course["id"]}
        )
        assert created.status_code == 201
        created_id = created.json()["user_id"]
        assert [str(event.user_id) for event in hooks.created] == [created_id]
        created_user = client.get(f"/api/v1/users/{created_id}", headers=admin_headers).json()
        assert created_user["email"] == "new@example.com"
        assert [event.course_id for event in hooks.enrolled] == [course["id"]]
        enrollments = client.get(
            "/api/v1/enrollments",
            headers=admin_headers,
            params={"course_id": course["id"]},
        ).json()
        assert len(enrollments) == 1


def test_custom_api_prefix(tmp_path: Path, database_url: str) -> None:
    """Core routers mount under Settings.api_prefix instead of /api/v1."""
    admin_token = seed_admin(database_url)
    headers = {"Authorization": f"Bearer {admin_token}"}
    app = create_app(
        settings=Settings(
            database_url=database_url, registration_org_slug=DEFAULT_ORG_SLUG, api_prefix="/lms"
        ),
        engine=create_db_engine(database_url, echo=False),
        auth=AnonymousAuthBackend(),
        storage=FilesystemStorage(tmp_path / "storage"),
        hooks=NullEventHooks(),
        routers=ALL_CORE_ROUTERS,
        modules=[],
        schedules=[],
        access_policy=DEFAULT_ACCESS_POLICY,
    )
    with TestClient(app) as client:
        assert client.get("/lms/users", headers=headers).status_code == 200
        assert client.get("/api/v1/users", headers=headers).status_code == 404


def test_core_router_selection(tmp_path: Path, database_url: str) -> None:
    """Any subset of core routers composes; deselection only removes endpoints."""

    admin_token = seed_admin(database_url)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    settings = Settings(database_url=database_url, registration_org_slug=DEFAULT_ORG_SLUG)

    def build(routers: list[CoreRouterName]) -> FastAPI:
        return create_app(
            settings=settings,
            engine=create_db_engine(database_url, echo=False),
            auth=AnonymousAuthBackend(),
            storage=FilesystemStorage(tmp_path / "storage"),
            hooks=NullEventHooks(),
            routers=routers,
            modules=[],
            schedules=[],
            access_policy=DEFAULT_ACCESS_POLICY,
        )

    # Each router boots alone; /health is always mounted
    for name in ALL_CORE_ROUTERS:
        with TestClient(build([name])) as client:
            assert client.get("/health").status_code == 200

    # Deselected routers are absent while selected ones keep working against
    # the shared domain layer: enrollments answer without the courses router
    # mounted, and a missing course is the usual domain 404, not a crash
    with TestClient(build(["users", "enrollments"])) as client:
        admin_id = client.get("/api/v1/users/me", headers=admin_headers).json()["id"]
        assert client.get("/api/v1/courses", headers=admin_headers).status_code == 404
        enrollment = client.post(
            "/api/v1/enrollments",
            headers=admin_headers,
            json={"user_id": admin_id, "course_id": 999},
        )
        assert enrollment.status_code == 404
        assert "course" in enrollment.json()["detail"].lower()

    with pytest.raises(UnknownCoreRouterError):
        build(["nonexistent"])  # type: ignore[list-item]


def test_periodic_schedules_run_in_lifespan(tmp_path: Path, database_url: str) -> None:
    seed_default_org(database_url)

    # The task reads the database through the engine the app runs on, the way
    # a client shares one connection pool between the app and its own work
    app_engine = create_db_engine(database_url, echo=False)
    session_factory = create_session_factory(app_engine)
    ticks: list[int] = []

    def tick() -> None:
        with session_factory() as session:
            ticks.append(get_org_by_slug(session, DEFAULT_ORG_SLUG).id)

    app = create_app(
        settings=Settings(database_url=database_url, registration_org_slug=DEFAULT_ORG_SLUG),
        engine=app_engine,
        auth=AnonymousAuthBackend(),
        storage=FilesystemStorage(tmp_path / "storage"),
        hooks=NullEventHooks(),
        routers=[],
        modules=[],
        schedules=[PeriodicTask(name="test:tick", interval=timedelta(seconds=60), run=tick)],
        access_policy=DEFAULT_ACCESS_POLICY,
    )

    # The startup run fires shortly after the lifespan begins; the next one
    # would land a minute later, so exactly one tick arrives during the test
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        deadline = time.monotonic() + 5
        while not ticks and time.monotonic() < deadline:
            time.sleep(0.01)
    assert ticks == [default_org_id(database_url)]
