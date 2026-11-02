"""Enrollment loop, enrollment queries and due dates."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from microlms import DEFAULT_ACCESS_POLICY, AccessPolicy, Settings, create_app, create_db_engine
from microlms.auth import AnonymousAuthBackend
from microlms.events import CoursePublished, NullEventHooks, RoleGranted, RoleRevoked
from microlms.storage import FilesystemStorage
from tests.helpers import (
    ALL_CORE_ROUTERS,
    DEFAULT_ORG_SLUG,
    RecordingHooks,
    default_org_id,
    issue_token,
    seed_admin,
)


def test_full_enrollment_loop(tmp_path: Path, database_url: str) -> None:
    admin_token = seed_admin(database_url)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    settings = Settings(database_url=database_url, registration_org_slug=DEFAULT_ORG_SLUG)
    hooks = RecordingHooks()
    app = create_app(
        settings=settings,
        engine=create_db_engine(database_url, echo=False),
        auth=AnonymousAuthBackend(),
        storage=FilesystemStorage(tmp_path / "storage"),
        hooks=hooks,
        routers=ALL_CORE_ROUTERS,
        modules=["scorm", "h5p"],
        schedules=[],
        access_policy=DEFAULT_ACCESS_POLICY,
    )

    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        # Anonymous registration issues a uuid4 token usable as a bearer credential
        registration = client.post("/auth/register")
        assert registration.status_code == 201
        token = registration.json()["token"]

        me = client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        assert me.json()["id"] == registration.json()["user_id"]
        assert me.json()["email"] is None
        # Self-registration lands the learner in the default organization
        assert me.json()["roles"] == []
        assert me.json()["orgs"] == [
            {
                "org_id": default_org_id(database_url),
                "name": DEFAULT_ORG_SLUG,
                "roles": ["learner"],
            }
        ]

        # Missing or unknown tokens are rejected
        assert client.get("/api/v1/users/me").status_code == 401
        bad = client.get(
            "/api/v1/users/me",
            headers={"Authorization": "Bearer not-a-uuid"},
        )
        assert bad.status_code == 401

        # Selected modules are mounted, unselected ones are not
        assert client.get("/api/v1/scorm/status").status_code == 200
        assert client.get("/api/v1/h5p/status").status_code == 200
        assert client.get("/api/v1/cmi5/status").status_code == 404

        # Sensitive endpoints reject anonymous callers (401) and non-admins (403)
        learner_headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/v1/users").status_code == 401
        assert client.get("/api/v1/users", headers=learner_headers).status_code == 403
        assert (
            client.post(
                "/api/v1/courses",
                headers=learner_headers,
                json={"owner_org_id": None, "slug": "x", "title": "X", "description": ""},
            ).status_code
            == 403
        )

        # Admins can list users and reach every protected endpoint
        assert client.get("/api/v1/users", headers=admin_headers).status_code == 200

        default_org = default_org_id(database_url)
        user = client.post(
            "/api/v1/users",
            headers=admin_headers,
            json={
                "email": "jane@example.com",
                "full_name": "Jane Doe",
                "roles": ["learner"],
                "org_id": default_org,
            },
        )
        assert user.status_code == 201

        # Any known role may be assigned at either scope; unknown ones are refused
        assert (
            client.post(
                "/api/v1/users",
                headers=admin_headers,
                json={"email": None, "full_name": "X", "roles": ["ghost"], "org_id": None},
            ).status_code
            == 422
        )

        # Identities without an email (e.g. some SSO providers) are valid users
        no_email_user = client.post(
            "/api/v1/users",
            headers=admin_headers,
            json={
                "email": None,
                "full_name": "Badge 4127",
                "roles": ["learner"],
                "org_id": default_org,
            },
        )
        assert no_email_user.status_code == 201
        assert no_email_user.json()["email"] is None

        # Listing pages with limit/offset and reports the in-scope total in a
        # header; paging leaves the total unchanged and covers the full list
        full = client.get("/api/v1/users", headers=admin_headers)
        total = int(full.headers["X-Total-Count"])
        assert total == len(full.json())
        assert total >= 3
        first = client.get("/api/v1/users?limit=1&offset=0", headers=admin_headers)
        assert len(first.json()) == 1
        assert int(first.headers["X-Total-Count"]) == total
        paged_ids = []
        for page_offset in range(total):
            page = client.get(
                f"/api/v1/users?limit=1&offset={page_offset}", headers=admin_headers
            ).json()
            paged_ids.extend(entry["id"] for entry in page)
        assert paged_ids == [entry["id"] for entry in full.json()]

        # A user cannot be created without a role
        assert (
            client.post(
                "/api/v1/users",
                headers=admin_headers,
                json={"email": None, "full_name": "No Role", "roles": [], "org_id": default_org},
            ).status_code
            == 422
        )

        # Role management: grant is idempotent, org roles accumulate per org
        jane_id = user.json()["id"]
        jane_roles = f"/api/v1/users/{jane_id}/orgs/{default_org}/roles"
        granted = client.put(f"{jane_roles}/instructor", headers=admin_headers)
        assert granted.status_code == 200
        assert granted.json()["orgs"] == [
            {"org_id": default_org, "name": DEFAULT_ORG_SLUG, "roles": ["instructor", "learner"]}
        ]
        regranted = client.put(f"{jane_roles}/instructor", headers=admin_headers)
        assert regranted.json()["orgs"][0]["roles"] == ["instructor", "learner"]
        # Only the grant that added the role is announced
        assert hooks.granted == [
            RoleGranted(user_id=uuid.UUID(jane_id), role="instructor", org_id=default_org)
        ]
        # Roles outside the policy are refused at either scope
        assert client.put(f"{jane_roles}/ghost", headers=admin_headers).status_code == 422
        assert (
            client.put(f"/api/v1/users/{jane_id}/roles/ghost", headers=admin_headers).status_code
            == 422
        )

        # Revoke removes the role; the last role cannot be revoked
        revoked = client.delete(f"{jane_roles}/instructor", headers=admin_headers)
        assert revoked.status_code == 200
        assert revoked.json()["orgs"][0]["roles"] == ["learner"]
        assert hooks.revoked == [
            RoleRevoked(user_id=uuid.UUID(jane_id), role="instructor", org_id=default_org)
        ]
        assert client.delete(f"{jane_roles}/instructor", headers=admin_headers).status_code == 404
        assert client.delete(f"{jane_roles}/learner", headers=admin_headers).status_code == 409

        # Role management is admin-only and 404s on unknown users
        assert client.put(f"{jane_roles}/instructor", headers=learner_headers).status_code == 403
        assert (
            client.put(
                f"/api/v1/users/{uuid.uuid4()}/orgs/{default_org}/roles/instructor",
                headers=admin_headers,
            ).status_code
            == 404
        )

        course = client.post(
            "/api/v1/courses",
            headers=admin_headers,
            json={
                "owner_org_id": None,
                "slug": "onboarding",
                "title": "Onboarding",
                "description": "Intro",
            },
        )
        assert course.status_code == 201
        assert course.json()["status"] == "draft"

        # A draft course cannot be enrolled into; publishing unlocks it
        assert (
            client.post(
                "/api/v1/enrollments",
                headers=admin_headers,
                json={"user_id": user.json()["id"], "course_id": course.json()["id"]},
            ).status_code
            == 409
        )
        published = client.post(
            f"/api/v1/courses/{course.json()['id']}/publish", headers=admin_headers
        )
        assert published.status_code == 200
        assert published.json()["status"] == "published"
        # Publishing again is idempotent and announces nothing new
        client.post(f"/api/v1/courses/{course.json()['id']}/publish", headers=admin_headers)
        assert hooks.published == [CoursePublished(course_id=course.json()["id"])]

        enrollment = client.post(
            "/api/v1/enrollments",
            headers=admin_headers,
            json={"user_id": user.json()["id"], "course_id": course.json()["id"]},
        )
        assert enrollment.status_code == 201
        assert enrollment.json()["status"] == "active"

        completed = client.post(
            f"/api/v1/enrollments/{enrollment.json()['id']}/complete",
            headers=admin_headers,
        )
        assert completed.status_code == 200
        assert completed.json()["status"] == "completed"
        assert completed.json()["completed_at"] is not None

    assert len(hooks.enrolled) == 1
    assert len(hooks.completed) == 1
    assert hooks.completed[0].course_id == course.json()["id"]


def test_enrollment_queries(tmp_path: Path, database_url: str) -> None:
    admin_token = seed_admin(database_url)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    app = create_app(
        settings=Settings(database_url=database_url, registration_org_slug=DEFAULT_ORG_SLUG),
        engine=create_db_engine(database_url, echo=False),
        auth=AnonymousAuthBackend(),
        storage=FilesystemStorage(tmp_path / "storage"),
        hooks=NullEventHooks(),
        routers=ALL_CORE_ROUTERS,
        modules=[],
        # "guest" grants nothing, so it cannot read its own enrollments either
        schedules=[],
        access_policy=AccessPolicy(
            roles={
                **DEFAULT_ACCESS_POLICY.roles,
                "guest": frozenset(),
            },
            assign_permissions={},
        ),
    )

    def _new_user(client: TestClient, email: str, roles: list[str], org_id: int | None) -> dict:
        created = client.post(
            "/api/v1/users",
            headers=admin_headers,
            json={"email": email, "full_name": email, "roles": roles, "org_id": org_id},
        )
        assert created.status_code == 201
        user = created.json()
        user["headers"] = {"Authorization": f"Bearer {issue_token(database_url, user['id'])}"}
        return user

    with TestClient(app) as client:
        org_a = client.post(
            "/api/v1/orgs", headers=admin_headers, json={"slug": "a", "name": "A"}
        ).json()["id"]
        org_b = client.post(
            "/api/v1/orgs", headers=admin_headers, json={"slug": "b", "name": "B"}
        ).json()["id"]
        manager = _new_user(client, "boss@a.example", ["manager"], org_a)
        learner_a = _new_user(client, "la@a.example", ["learner"], org_a)
        learner_b = _new_user(client, "lb@b.example", ["learner"], org_b)
        guest = _new_user(client, "ghost@example.com", ["guest"], default_org_id(database_url))
        course_id = client.post(
            "/api/v1/courses",
            headers=admin_headers,
            json={"owner_org_id": None, "slug": "c1", "title": "C1", "description": ""},
        ).json()["id"]
        client.post(f"/api/v1/courses/{course_id}/publish", headers=admin_headers)

        for learner in (learner_a, learner_b):
            enrolled = client.post(
                "/api/v1/enrollments",
                headers=admin_headers,
                json={"user_id": learner["id"], "course_id": course_id},
            )
            assert enrolled.status_code == 201
            learner["enrollment_id"] = enrolled.json()["id"]
        completed = client.post(
            f"/api/v1/enrollments/{learner_b['enrollment_id']}/complete", headers=admin_headers
        )
        assert completed.status_code == 200

        # Self view: own rows only, filterable by status, content access required
        assert client.get("/api/v1/enrollments/me").status_code == 401
        assert client.get("/api/v1/enrollments/me", headers=guest["headers"]).status_code == 403
        own = client.get("/api/v1/enrollments/me", headers=learner_a["headers"]).json()
        assert [e["user_id"] for e in own] == [learner_a["id"]]
        assert (
            client.get(
                "/api/v1/enrollments/me?status=completed", headers=learner_a["headers"]
            ).json()
            == []
        )
        assert (
            client.get(
                "/api/v1/enrollments/me?status=completed", headers=learner_b["headers"]
            ).json()[0]["status"]
            == "completed"
        )

        # Management listing needs the enrollment-read permission
        assert client.get("/api/v1/enrollments", headers=learner_a["headers"]).status_code == 403

        # An org manager sees their own org, and cannot reach outside it
        in_scope = client.get("/api/v1/enrollments", headers=manager["headers"]).json()
        assert [e["user_id"] for e in in_scope] == [learner_a["id"]]
        assert (
            client.get(
                f"/api/v1/enrollments?org_id={org_b}", headers=manager["headers"]
            ).status_code
            == 403
        )
        assert (
            client.get(
                f"/api/v1/enrollments?user_id={learner_b['id']}", headers=manager["headers"]
            ).status_code
            == 403
        )
        assert (
            client.get(
                f"/api/v1/enrollments?user_id={learner_a['id']}", headers=manager["headers"]
            ).json()
            == in_scope
        )

        # A platform manager lists globally with free filters
        everything = client.get("/api/v1/enrollments", headers=admin_headers).json()
        assert {e["user_id"] for e in everything} == {learner_a["id"], learner_b["id"]}
        assert [
            e["user_id"]
            for e in client.get(
                "/api/v1/enrollments?status=completed", headers=admin_headers
            ).json()
        ] == [learner_b["id"]]
        assert [
            e["user_id"]
            for e in client.get(f"/api/v1/enrollments?org_id={org_a}", headers=admin_headers).json()
        ] == [learner_a["id"]]
        assert (
            client.get(
                f"/api/v1/enrollments?course_id={course_id + 1}", headers=admin_headers
            ).json()
            == []
        )


def test_enrollment_due_dates(tmp_path: Path, database_url: str) -> None:
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

    with TestClient(app) as client:
        course_id = client.post(
            "/api/v1/courses",
            headers=admin_headers,
            json={"owner_org_id": None, "slug": "c", "title": "C", "description": ""},
        ).json()["id"]
        client.post(f"/api/v1/courses/{course_id}/publish", headers=admin_headers)

        def enroll(email: str, due_at: str | None) -> dict:
            user = client.post(
                "/api/v1/users",
                headers=admin_headers,
                json={
                    "email": email,
                    "full_name": email,
                    "roles": ["learner"],
                    "org_id": default_org_id(database_url),
                },
            ).json()
            enrolled = client.post(
                "/api/v1/enrollments",
                headers=admin_headers,
                json={"user_id": user["id"], "course_id": course_id, "due_at": due_at},
            )
            assert enrolled.status_code == 201
            return {"user": user, "enrollment": enrolled.json()}

        tomorrow = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        next_month = (datetime.now(UTC) + timedelta(days=30)).isoformat()
        due_soon = enroll("soon@example.com", tomorrow)
        due_later = enroll("later@example.com", next_month)
        no_deadline = enroll("nodue@example.com", None)
        completed = enroll("done@example.com", tomorrow)
        client.post(
            f"/api/v1/enrollments/{completed['enrollment']['id']}/complete", headers=admin_headers
        )

        # Enrolling twice into the same course is a conflict, not a crash
        again_enrolled = client.post(
            "/api/v1/enrollments",
            headers=admin_headers,
            json={"user_id": due_soon["user"]["id"], "course_id": course_id, "due_at": None},
        )
        assert again_enrolled.status_code == 409

        # The deadline flows into the response and the enrollment event
        assert due_soon["enrollment"]["due_at"] is not None
        assert no_deadline["enrollment"]["due_at"] is None
        assert [e.due_at is not None for e in hooks.enrolled] == [True, True, False, True]

        # Notification is admin-only, requires a window, and fires exactly
        # once per enrollment: not for far deadlines, no deadline, completed
        learner_headers = {
            "Authorization": f"Bearer {issue_token(database_url, due_soon['user']['id'])}"
        }
        assert client.post("/api/v1/enrollments/notify-due-soon?within_days=7").status_code == 401
        assert (
            client.post(
                "/api/v1/enrollments/notify-due-soon?within_days=7", headers=learner_headers
            ).status_code
            == 403
        )
        assert (
            client.post("/api/v1/enrollments/notify-due-soon", headers=admin_headers).status_code
            == 422
        )
        first = client.post(
            "/api/v1/enrollments/notify-due-soon?within_days=7", headers=admin_headers
        )
        assert first.json() == {"notified": 1}
        assert [e.enrollment_id for e in hooks.due_soon] == [due_soon["enrollment"]["id"]]
        assert hooks.due_soon[0].user_id == uuid.UUID(due_soon["user"]["id"])
        again = client.post(
            "/api/v1/enrollments/notify-due-soon?within_days=7", headers=admin_headers
        )
        assert again.json() == {"notified": 0}

        # A wider window picks up the remaining deadline
        wide = client.post(
            "/api/v1/enrollments/notify-due-soon?within_days=60", headers=admin_headers
        )
        assert wide.json() == {"notified": 1}
        assert hooks.due_soon[-1].enrollment_id == due_later["enrollment"]["id"]

        # Counts by status, with the active enrollments past their deadline
        # as overdue (a completed one never is)
        yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        enroll("late@example.com", yesterday)
        counts = {"total": 5, "active": 4, "completed": 1, "overdue": 1}
        assert client.get("/api/v1/enrollments/counts", headers=admin_headers).json() == counts
        filtered = client.get(
            f"/api/v1/enrollments/counts?course_id={course_id}"
            f"&org_id={default_org_id(database_url)}",
            headers=admin_headers,
        )
        assert filtered.json() == counts
        mine = client.get(
            f"/api/v1/enrollments/counts?user_id={due_soon['user']['id']}", headers=admin_headers
        )
        assert mine.json() == {"total": 1, "active": 1, "completed": 0, "overdue": 0}
        learner_counts = client.get("/api/v1/enrollments/counts", headers=learner_headers)
        assert learner_counts.status_code == 403
        # Per organization: every learner here belongs to the default org
        assert client.get("/api/v1/enrollments/counts/by-org", headers=admin_headers).json() == [
            {"org_id": default_org_id(database_url), **counts}
        ]
        assert (
            client.get("/api/v1/enrollments/counts/by-org", headers=learner_headers).status_code
            == 403
        )
        # Per course: every enrollment here is in the one course
        assert client.get("/api/v1/enrollments/counts/by-course", headers=admin_headers).json() == [
            {"course_id": course_id, **counts}
        ]
        assert client.get(
            f"/api/v1/enrollments/counts/by-course?org_id={default_org_id(database_url)}",
            headers=admin_headers,
        ).json() == [{"course_id": course_id, **counts}]
        assert (
            client.get("/api/v1/enrollments/counts/by-course", headers=learner_headers).status_code
            == 403
        )
