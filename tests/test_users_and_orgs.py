"""Organizations, account lifecycle, self-registration, audit and impersonation."""

import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from microlms import DEFAULT_ACCESS_POLICY, AccessPolicy, Settings, create_app, create_db_engine
from microlms.auth import AnonymousAuthBackend
from microlms.events import (
    CourseOrgAssigned,
    CourseOrgUnassigned,
    NullEventHooks,
    RoleGranted,
    RoleRevoked,
)
from microlms.storage import FilesystemStorage
from tests.helpers import (
    ALL_CORE_ROUTERS,
    DEFAULT_ORG_SLUG,
    GUEST_ORG_SLUG,
    RecordingHooks,
    issue_token,
    seed_admin,
)


def test_org_lifecycle_and_manager_scoping(tmp_path: Path, database_url: str) -> None:
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
        # Org CRUD is admin-only
        learner_token = client.post("/auth/register").json()["token"]
        learner_headers = {"Authorization": f"Bearer {learner_token}"}
        assert client.get("/api/v1/orgs").status_code == 401
        assert client.get("/api/v1/orgs", headers=learner_headers).status_code == 403

        org_a = client.post(
            "/api/v1/orgs", headers=admin_headers, json={"slug": "acme", "name": "Acme"}
        )
        assert org_a.status_code == 201
        org_a_id = org_a.json()["id"]
        org_b_id = client.post(
            "/api/v1/orgs", headers=admin_headers, json={"slug": "globex", "name": "Globex"}
        ).json()["id"]
        duplicate_slug = client.post(
            "/api/v1/orgs", headers=admin_headers, json={"slug": "acme", "name": "Acme Again"}
        )
        assert duplicate_slug.status_code == 409
        assert [o["slug"] for o in client.get("/api/v1/orgs", headers=admin_headers).json()] == [
            DEFAULT_ORG_SLUG,
            "acme",
            "globex",
        ]
        assert (
            client.get(f"/api/v1/orgs/{org_a_id}", headers=admin_headers).json()["name"] == "Acme"
        )
        assert client.get("/api/v1/orgs/999", headers=admin_headers).status_code == 404

        # Membership: set at creation, join a second org, leave; 404 paths
        member = client.post(
            "/api/v1/users",
            headers=admin_headers,
            json={
                "email": "m@acme.example",
                "full_name": "Member",
                "roles": ["learner"],
                "org_id": org_a_id,
            },
        )
        assert member.status_code == 201
        assert member.json()["orgs"] == [{"org_id": org_a_id, "name": "Acme", "roles": ["learner"]}]
        member_id = member.json()["id"]

        # Creating a user in an unknown org is rejected
        assert (
            client.post(
                "/api/v1/users",
                headers=admin_headers,
                json={"email": None, "full_name": "X", "roles": ["learner"], "org_id": 999},
            ).status_code
            == 404
        )

        # A user belongs to several orgs, with its own roles in each
        joined = client.put(
            f"/api/v1/users/{member_id}/orgs/{org_b_id}/roles/manager", headers=admin_headers
        )
        assert joined.status_code == 200
        assert joined.json()["orgs"] == [
            {"org_id": org_a_id, "name": "Acme", "roles": ["learner"]},
            {"org_id": org_b_id, "name": "Globex", "roles": ["manager"]},
        ]
        assert hooks.granted == [
            RoleGranted(user_id=uuid.UUID(member_id), role="manager", org_id=org_b_id)
        ]
        assert (
            client.put(
                f"/api/v1/users/{member_id}/orgs/999/roles/learner", headers=admin_headers
            ).status_code
            == 404
        )
        assert (
            client.put(
                f"/api/v1/users/{uuid.uuid4()}/orgs/{org_a_id}/roles/learner",
                headers=admin_headers,
            ).status_code
            == 404
        )

        # An org with members cannot be deleted
        assert client.delete(f"/api/v1/orgs/{org_a_id}", headers=admin_headers).status_code == 409

        # Leaving an org drops every role held there; leaving again is 404,
        # and the user's last org cannot be left
        left = client.delete(f"/api/v1/users/{member_id}/orgs/{org_a_id}", headers=admin_headers)
        assert left.status_code == 200
        assert left.json()["orgs"] == [{"org_id": org_b_id, "name": "Globex", "roles": ["manager"]}]
        assert hooks.revoked == [
            RoleRevoked(user_id=uuid.UUID(member_id), role="learner", org_id=org_a_id)
        ]
        assert (
            client.delete(
                f"/api/v1/users/{member_id}/orgs/{org_a_id}", headers=admin_headers
            ).status_code
            == 404
        )
        assert (
            client.delete(
                f"/api/v1/users/{member_id}/orgs/{org_b_id}", headers=admin_headers
            ).status_code
            == 409
        )

        # The emptied org can be deleted
        assert client.delete(f"/api/v1/orgs/{org_a_id}", headers=admin_headers).status_code == 204
        assert client.delete(f"/api/v1/orgs/{org_a_id}", headers=admin_headers).status_code == 404
        client.delete(f"/api/v1/users/{member_id}", headers=admin_headers)

        # --- Manager scoping ---
        org_a_id = client.post(
            "/api/v1/orgs", headers=admin_headers, json={"slug": "a", "name": "Org A"}
        ).json()["id"]
        manager = client.post(
            "/api/v1/users",
            headers=admin_headers,
            json={
                "email": "boss@a.example",
                "full_name": "Boss",
                "roles": ["manager"],
                "org_id": org_a_id,
            },
        ).json()
        manager_headers = {"Authorization": f"Bearer {issue_token(database_url, manager['id'])}"}
        a_learner = client.post(
            "/api/v1/users",
            headers=admin_headers,
            json={"email": None, "full_name": "A1", "roles": ["learner"], "org_id": org_a_id},
        ).json()
        b_learner = client.post(
            "/api/v1/users",
            headers=admin_headers,
            json={"email": None, "full_name": "B1", "roles": ["learner"], "org_id": org_b_id},
        ).json()

        # A manager lists only their own org's users holding nothing they lack
        # (their own account included)
        listed = client.get("/api/v1/users", headers=manager_headers)
        assert listed.status_code == 200
        assert {u["id"] for u in listed.json()} == {a_learner["id"], manager["id"]}

        # Out-of-org users exist but are forbidden, not hidden as 404
        assert (
            client.get(f"/api/v1/users/{b_learner['id']}", headers=manager_headers).status_code
            == 403
        )
        assert (
            client.get(f"/api/v1/users/{uuid.uuid4()}", headers=manager_headers).status_code == 404
        )

        # A manager reads their own account: it holds nothing they lack
        assert (
            client.get(f"/api/v1/users/{manager['id']}", headers=manager_headers).status_code == 200
        )
        assert (
            client.get(f"/api/v1/users/{manager['id']}", headers=admin_headers).status_code == 200
        )

        # Managers create users only in their own org, with roles whose
        # permissions they hold there
        created = client.post(
            "/api/v1/users",
            headers=manager_headers,
            json={
                "email": None,
                "full_name": "A2",
                "roles": ["learner"],
                "org_id": org_a_id,
                "lang": "fr",
            },
        )
        assert created.status_code == 201
        assert created.json()["lang"] == "fr"
        assert created.json()["orgs"] == [
            {"org_id": org_a_id, "name": "Org A", "roles": ["learner"]}
        ]

        # Management creation emitted UserCreated for the client hooks
        invited = [e for e in hooks.created if str(e.user_id) == created.json()["id"]]
        assert [(e.full_name, e.roles, e.org_id, e.lang) for e in invited] == [
            ("A2", ("learner",), org_a_id, "fr")
        ]
        assert (
            client.post(
                "/api/v1/users",
                headers=manager_headers,
                json={"email": None, "full_name": "X", "roles": ["instructor"], "org_id": org_a_id},
            ).status_code
            == 403
        )
        # ...and never outside an organization
        assert (
            client.post(
                "/api/v1/users",
                headers=manager_headers,
                json={"email": None, "full_name": "X", "roles": ["admin"], "org_id": None},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/users",
                headers=manager_headers,
                json={"email": None, "full_name": "X", "roles": ["learner"], "org_id": org_b_id},
            ).status_code
            == 403
        )

        # Listing filters and counts follow the same scope: a manager filters
        # inside their reach, seeing only accounts holding nothing they lack
        def listed_names(headers: dict, query: str) -> list[str]:
            found = client.get(f"/api/v1/users?{query}", headers=headers)
            assert found.status_code == 200
            assert found.headers["X-Total-Count"] == str(len(found.json()))
            return sorted(user["full_name"] for user in found.json())

        assert listed_names(manager_headers, "search=a2") == ["A2"]
        # Listing by id leaves out accounts out of reach and unknown ids, so
        # a page names its rows in one request
        assert listed_names(
            manager_headers, f"id={a_learner['id']}&id={b_learner['id']}&id={uuid.uuid4()}"
        ) == ["A1"]
        assert listed_names(admin_headers, f"id={a_learner['id']}&id={b_learner['id']}") == [
            "A1",
            "B1",
        ]
        too_many = "&".join(f"id={uuid.uuid4()}" for _ in range(101))
        assert client.get(f"/api/v1/users?{too_many}", headers=admin_headers).status_code == 422
        assert listed_names(manager_headers, "role=learner") == ["A1", "A2"]
        assert listed_names(manager_headers, "role=manager") == ["Boss"]
        assert listed_names(manager_headers, "is_active=false") == []
        assert (
            client.get(f"/api/v1/users?org_id={org_b_id}", headers=manager_headers).status_code
            == 403
        )
        assert client.get("/api/v1/users/counts", headers=manager_headers).json() == {
            "total": 3,
            "by_role": {"learner": 2, "manager": 1},
        }
        # Counts per organization follow the same scope, one row per org in reach
        assert client.get("/api/v1/users/counts/by-org", headers=manager_headers).json() == [
            {"org_id": org_a_id, "total": 3}
        ]
        assert client.get(
            "/api/v1/users/counts/by-org?role=learner", headers=manager_headers
        ).json() == [{"org_id": org_a_id, "total": 2}]
        # An admin filters anywhere; the role filter applies inside the
        # filtered organization, and LIKE wildcards are matched literally
        assert listed_names(admin_headers, f"org_id={org_a_id}") == ["A1", "A2", "Boss"]
        assert listed_names(admin_headers, f"org_id={org_a_id}&role=manager") == ["Boss"]
        assert listed_names(admin_headers, f"org_id={org_b_id}&role=manager") == []
        assert listed_names(admin_headers, "search=%25") == []
        assert client.get(
            f"/api/v1/users/counts?org_id={org_a_id}", headers=admin_headers
        ).json() == {"total": 3, "by_role": {"learner": 2, "manager": 1}}
        # Per organization, an admin gets what /counts answers for each org
        learners_by_org = {
            row["org_id"]: row["total"]
            for row in client.get(
                "/api/v1/users/counts/by-org?role=learner", headers=admin_headers
            ).json()
        }
        assert learners_by_org[org_a_id] == 2
        for org_id in (org_a_id, org_b_id):
            assert (
                learners_by_org.get(org_id, 0)
                == client.get(
                    f"/api/v1/users/counts?org_id={org_id}&role=learner", headers=admin_headers
                ).json()["total"]
            )
        everyone = client.get("/api/v1/users/counts", headers=admin_headers).json()
        assert everyone["total"] == len(client.get("/api/v1/users", headers=admin_headers).json())
        # Boss is the only manager left (the Globex one was deleted above)
        assert everyone["by_role"]["manager"] == 1

        # Role management inside the org, only for roles whose permissions the
        # manager holds: learner yes, instructor no (content import runs
        # package scripts on the app origin, so it never spreads down)
        a_roles = f"/api/v1/users/{a_learner['id']}/orgs/{org_a_id}/roles"
        granted = client.put(f"{a_roles}/learner", headers=manager_headers)
        assert granted.status_code == 200
        assert granted.json()["orgs"] == [
            {"org_id": org_a_id, "name": "Org A", "roles": ["learner"]}
        ]
        assert client.put(f"{a_roles}/instructor", headers=manager_headers).status_code == 403
        # Platform roles are out of an org manager's reach
        assert (
            client.put(
                f"/api/v1/users/{a_learner['id']}/roles/admin", headers=manager_headers
            ).status_code
            == 403
        )
        # Another org's roles are out of reach, and adding an outsider to the
        # manager's own org needs a platform role
        assert (
            client.put(
                f"/api/v1/users/{b_learner['id']}/orgs/{org_b_id}/roles/instructor",
                headers=manager_headers,
            ).status_code
            == 403
        )
        assert (
            client.put(
                f"/api/v1/users/{b_learner['id']}/orgs/{org_a_id}/roles/learner",
                headers=manager_headers,
            ).status_code
            == 403
        )
        # Nobody escalates themselves either: the manager lacks the instructor's
        # permissions, so cannot take the role
        assert (
            client.put(
                f"/api/v1/users/{manager['id']}/orgs/{org_a_id}/roles/instructor",
                headers=manager_headers,
            ).status_code
            == 403
        )

        # Plain learners still cannot manage anything
        assert client.get("/api/v1/users", headers=learner_headers).status_code == 403

        # --- Course visibility ---
        assert client.get("/api/v1/courses").status_code == 401

        global_course = client.post(
            "/api/v1/courses",
            headers=admin_headers,
            json={
                "owner_org_id": None,
                "slug": "for-everyone",
                "title": "For everyone",
                "description": "",
            },
        ).json()
        assert global_course["org_ids"] == []
        a_course = client.post(
            "/api/v1/courses",
            headers=admin_headers,
            json={"owner_org_id": None, "slug": "a-only", "title": "A only", "description": ""},
        ).json()
        for course in (global_course, a_course):
            client.post(f"/api/v1/courses/{course['id']}/publish", headers=admin_headers)

        assigned = client.put(
            f"/api/v1/courses/{a_course['id']}/orgs/{org_a_id}", headers=admin_headers
        )
        assert assigned.status_code == 200
        assert assigned.json()["org_ids"] == [org_a_id]
        # Idempotent re-assign; unknown course or org is 404
        assert client.put(
            f"/api/v1/courses/{a_course['id']}/orgs/{org_a_id}", headers=admin_headers
        ).json()["org_ids"] == [org_a_id]
        assert (
            client.put(f"/api/v1/courses/999/orgs/{org_a_id}", headers=admin_headers).status_code
            == 404
        )
        assert (
            client.put(
                f"/api/v1/courses/{a_course['id']}/orgs/999", headers=admin_headers
            ).status_code
            == 404
        )

        # Members of org A see global + A courses; others see only global ones
        a_headers = {"Authorization": f"Bearer {issue_token(database_url, a_learner['id'])}"}
        b_headers = {"Authorization": f"Bearer {issue_token(database_url, b_learner['id'])}"}
        assert {c["id"] for c in client.get("/api/v1/courses", headers=a_headers).json()} == {
            global_course["id"],
            a_course["id"],
        }
        assert {c["id"] for c in client.get("/api/v1/courses", headers=b_headers).json()} == {
            global_course["id"]
        }
        assert {c["id"] for c in client.get("/api/v1/courses", headers=learner_headers).json()} == {
            global_course["id"]
        }
        assert client.get(f"/api/v1/courses/{a_course['id']}", headers=a_headers).status_code == 200
        assert client.get(f"/api/v1/courses/{a_course['id']}", headers=b_headers).status_code == 403
        assert (
            client.get(f"/api/v1/courses/{global_course['id']}", headers=b_headers).status_code
            == 200
        )
        # Admins see everything regardless of assignments
        assert {c["id"] for c in client.get("/api/v1/courses", headers=admin_headers).json()} >= {
            global_course["id"],
            a_course["id"],
        }

        # An org with course assignments (and no members) cannot be deleted
        org_c_id = client.post(
            "/api/v1/orgs", headers=admin_headers, json={"slug": "c", "name": "Org C"}
        ).json()["id"]
        client.put(f"/api/v1/courses/{a_course['id']}/orgs/{org_c_id}", headers=admin_headers)
        assert client.delete(f"/api/v1/orgs/{org_c_id}", headers=admin_headers).status_code == 409
        client.delete(f"/api/v1/courses/{a_course['id']}/orgs/{org_c_id}", headers=admin_headers)
        assert client.delete(f"/api/v1/orgs/{org_c_id}", headers=admin_headers).status_code == 204
        assert hooks.org_assigned[-1] == CourseOrgAssigned(
            course_id=a_course["id"], org_id=org_c_id
        )
        assert hooks.org_unassigned == [
            CourseOrgUnassigned(course_id=a_course["id"], org_id=org_c_id)
        ]

        # Unassigning the last org makes the course global again
        unassigned = client.delete(
            f"/api/v1/courses/{a_course['id']}/orgs/{org_a_id}", headers=admin_headers
        )
        assert unassigned.status_code == 200
        assert unassigned.json()["org_ids"] == []
        assert (
            client.delete(
                f"/api/v1/courses/{a_course['id']}/orgs/{org_a_id}", headers=admin_headers
            ).status_code
            == 404
        )
        assert client.get(f"/api/v1/courses/{a_course['id']}", headers=b_headers).status_code == 200

        # --- Enrollments under manager scope ---
        client.put(f"/api/v1/courses/{a_course['id']}/orgs/{org_a_id}", headers=admin_headers)
        b_course = client.post(
            "/api/v1/courses",
            headers=admin_headers,
            json={"owner_org_id": None, "slug": "b-only", "title": "B only", "description": ""},
        ).json()
        client.post(f"/api/v1/courses/{b_course['id']}/publish", headers=admin_headers)
        client.put(f"/api/v1/courses/{b_course['id']}/orgs/{org_b_id}", headers=admin_headers)

        # A manager enrolls own-org users into global and own-org courses
        enrolled = client.post(
            "/api/v1/enrollments",
            headers=manager_headers,
            json={"user_id": a_learner["id"], "course_id": global_course["id"]},
        )
        assert enrolled.status_code == 201
        assert (
            client.post(
                "/api/v1/enrollments",
                headers=manager_headers,
                json={"user_id": a_learner["id"], "course_id": a_course["id"]},
            ).status_code
            == 201
        )

        # Out-of-org users and out-of-org courses are forbidden
        assert (
            client.post(
                "/api/v1/enrollments",
                headers=manager_headers,
                json={"user_id": b_learner["id"], "course_id": global_course["id"]},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/enrollments",
                headers=manager_headers,
                json={"user_id": a_learner["id"], "course_id": b_course["id"]},
            ).status_code
            == 403
        )

        # Unknown user or course is 404 (admins included, instead of an FK error)
        assert (
            client.post(
                "/api/v1/enrollments",
                headers=admin_headers,
                json={"user_id": str(uuid.uuid4()), "course_id": global_course["id"]},
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/v1/enrollments",
                headers=manager_headers,
                json={"user_id": a_learner["id"], "course_id": 999},
            ).status_code
            == 404
        )

        # Plain learners cannot enroll or complete; a manager completes the
        # enrollments of their own org's members
        assert (
            client.post(
                "/api/v1/enrollments",
                headers=learner_headers,
                json={"user_id": a_learner["id"], "course_id": global_course["id"]},
            ).status_code
            == 403
        )
        enrollment_id = enrolled.json()["id"]
        assert (
            client.post(
                f"/api/v1/enrollments/{enrollment_id}/complete", headers=learner_headers
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/v1/enrollments/{enrollment_id}/complete", headers=manager_headers
            ).status_code
            == 200
        )


def test_account_lifecycle(tmp_path: Path, database_url: str) -> None:
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
        schedules=[],
        access_policy=DEFAULT_ACCESS_POLICY,
    )

    with TestClient(app) as client:
        org_a = client.post(
            "/api/v1/orgs", headers=admin_headers, json={"slug": "a", "name": "A"}
        ).json()["id"]
        org_b = client.post(
            "/api/v1/orgs", headers=admin_headers, json={"slug": "b", "name": "B"}
        ).json()["id"]

        def new_user(email: str, roles: list[str], org_id: int | None) -> dict:
            user = client.post(
                "/api/v1/users",
                headers=admin_headers,
                json={"email": email, "full_name": email, "roles": roles, "org_id": org_id},
            ).json()
            user["headers"] = {"Authorization": f"Bearer {issue_token(database_url, user['id'])}"}
            return user

        manager = new_user("boss@a.example", ["manager"], org_a)
        manager_two = new_user("boss2@a.example", ["manager"], org_a)
        learner_a = new_user("la@a.example", ["learner"], org_a)
        learner_b = new_user("lb@b.example", ["learner"], org_b)

        # Profile update, in scope; the response carries the new activity flag
        updated = client.put(
            f"/api/v1/users/{learner_a['id']}",
            headers=manager["headers"],
            json={"email": "renamed@a.example", "full_name": "Renamed", "lang": "fr-BE"},
        )
        assert updated.status_code == 200
        assert updated.json()["email"] == "renamed@a.example"
        assert updated.json()["lang"] == "fr-BE"
        assert (
            client.put(
                f"/api/v1/users/{learner_a['id']}",
                headers=manager["headers"],
                json={"email": "renamed@a.example", "full_name": "Renamed", "lang": "not a tag"},
            ).status_code
            == 400
        )
        assert updated.json()["is_active"] is True

        # Email conflicts, out-of-org targets and targets holding a permission
        # the manager lacks are refused; a peer manager holds nothing more
        assert (
            client.put(
                f"/api/v1/users/{learner_a['id']}",
                headers=manager["headers"],
                json={"email": "boss@a.example", "full_name": "Renamed", "lang": None},
            ).status_code
            == 409
        )
        assert (
            client.put(
                f"/api/v1/users/{learner_b['id']}",
                headers=manager["headers"],
                json={"email": None, "full_name": "X", "lang": None},
            ).status_code
            == 403
        )
        author = new_user("author@a.example", ["instructor"], org_a)
        assert (
            client.put(
                f"/api/v1/users/{author['id']}",
                headers=manager["headers"],
                json={"email": None, "full_name": "X", "lang": None},
            ).status_code
            == 403
        )
        assert (
            client.put(
                f"/api/v1/users/{manager_two['id']}",
                headers=manager["headers"],
                json={"email": "boss2@a.example", "full_name": "Boss Two", "lang": None},
            ).status_code
            == 200
        )

        # A published course to observe the enrollment cascade
        course_id = client.post(
            "/api/v1/courses",
            headers=admin_headers,
            json={"owner_org_id": None, "slug": "c1", "title": "C1", "description": ""},
        ).json()["id"]
        client.post(f"/api/v1/courses/{course_id}/publish", headers=admin_headers)
        assert (
            client.post(
                "/api/v1/enrollments",
                headers=admin_headers,
                json={"user_id": learner_a["id"], "course_id": course_id},
            ).status_code
            == 201
        )

        # Deactivation kills the existing token and blocks enrollment
        deactivated = client.post(
            f"/api/v1/users/{learner_a['id']}/deactivate", headers=manager["headers"]
        )
        assert deactivated.status_code == 200
        assert deactivated.json()["is_active"] is False
        assert client.get("/api/v1/users/me", headers=learner_a["headers"]).status_code == 401
        assert (
            client.post(
                "/api/v1/enrollments",
                headers=admin_headers,
                json={"user_id": learner_b["id"], "course_id": course_id},
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/api/v1/enrollments",
                headers=admin_headers,
                json={"user_id": learner_a["id"], "course_id": course_id},
            ).status_code
            == 409
        )

        # Reactivation restores authentication
        assert (
            client.post(
                f"/api/v1/users/{learner_a['id']}/activate", headers=manager["headers"]
            ).status_code
            == 200
        )
        assert client.get("/api/v1/users/me", headers=learner_a["headers"]).status_code == 200

        # Delete is admin-only and cascades to every row keyed to the user
        assert (
            client.delete(
                f"/api/v1/users/{learner_a['id']}", headers=manager["headers"]
            ).status_code
            == 403
        )
        assert (
            client.delete(f"/api/v1/users/{learner_a['id']}", headers=admin_headers).status_code
            == 204
        )
        assert (
            client.get(f"/api/v1/users/{learner_a['id']}", headers=admin_headers).status_code == 404
        )
        assert client.get("/api/v1/users/me", headers=learner_a["headers"]).status_code == 401
        remaining = client.get("/api/v1/enrollments", headers=admin_headers).json()
        assert [e["user_id"] for e in remaining] == [learner_b["id"]]
        assert (
            client.delete(f"/api/v1/users/{learner_a['id']}", headers=admin_headers).status_code
            == 404
        )


def test_self_registration_and_guest_org_settings(tmp_path: Path, database_url: str) -> None:
    """Self-registration is refused unless a registration org is configured,
    and a guest role without its guest org is refused at startup."""

    def build(settings: Settings) -> FastAPI:
        return create_app(
            settings=settings,
            engine=create_db_engine(database_url, echo=False),
            auth=AnonymousAuthBackend(),
            storage=FilesystemStorage(tmp_path / "storage"),
            hooks=NullEventHooks(),
            routers=ALL_CORE_ROUTERS,
            modules=[],
            schedules=[],
            access_policy=AccessPolicy(
                roles={
                    **DEFAULT_ACCESS_POLICY.roles,
                    "guest": frozenset(),
                },
                assign_permissions={},
            ),
        )

    # Accounts are only created by management when no registration org is set
    with TestClient(build(Settings(database_url=database_url))) as client:
        refused = client.post("/auth/register")
        assert refused.status_code == 403
        assert "MICROLMS_REGISTRATION_ORG_SLUG" in refused.json()["detail"]

    # Guests hold their role inside the guest org, so one without the other
    # is a misconfiguration
    with pytest.raises(ValueError, match="must be set together"):
        build(Settings(database_url=database_url, xapi_guest_role="guest"))
    with pytest.raises(ValueError, match="must be set together"):
        build(Settings(database_url=database_url, xapi_guest_org_slug=GUEST_ORG_SLUG))
    # ...and the guest role must be part of the policy
    with pytest.raises(ValueError, match="must be a role of the access policy"):
        build(
            Settings(
                database_url=database_url,
                xapi_guest_role="ghost",
                xapi_guest_org_slug=GUEST_ORG_SLUG,
            )
        )


def test_audit_and_impersonation(tmp_path: Path, database_url: str) -> None:
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
        schedules=[],
        access_policy=DEFAULT_ACCESS_POLICY,
    )

    with TestClient(app) as client:
        admin_id = client.get("/api/v1/users/me", headers=admin_headers).json()["id"]
        org_id = client.post(
            "/api/v1/orgs", headers=admin_headers, json={"slug": "a", "name": "A"}
        ).json()["id"]

        def new_user(email: str, roles: list[str], user_org: int | None) -> dict:
            user = client.post(
                "/api/v1/users",
                headers=admin_headers,
                json={"email": email, "full_name": email, "roles": roles, "org_id": user_org},
            ).json()
            user["headers"] = {"Authorization": f"Bearer {issue_token(database_url, user['id'])}"}
            return user

        manager = new_user("boss@a.example", ["manager"], org_id)
        learner = new_user("l@a.example", ["learner"], org_id)
        admin_two = new_user("root2@example.com", ["admin"], None)
        dormant = new_user("dormant@a.example", ["learner"], org_id)
        client.post(f"/api/v1/users/{dormant['id']}/deactivate", headers=admin_headers)

        # Self-registration leaves its own trail, actor = the new user
        registered = client.post("/auth/register").json()
        course = client.post(
            "/api/v1/courses",
            headers=admin_headers,
            json={"owner_org_id": None, "slug": "c", "title": "C", "description": ""},
        ).json()
        client.post(f"/api/v1/courses/{course['id']}/publish", headers=admin_headers)

        # The journal is admin-only
        assert client.get("/api/v1/audit").status_code == 401
        assert client.get("/api/v1/audit", headers=manager["headers"]).status_code == 403
        assert client.get("/api/v1/audit", headers=learner["headers"]).status_code == 403

        # Newest-first journal with the expected trail, filterable
        actions_entries = client.get("/api/v1/audit", headers=admin_headers).json()
        actions = [e["action"] for e in actions_entries]
        assert actions[:2] == ["course.published", "course.created"]
        assert "user.registered" in actions and "user.deactivated" in actions
        created = client.get("/api/v1/audit?action=user.created", headers=admin_headers).json()
        assert {e["target_id"] for e in created} == {
            manager["id"],
            learner["id"],
            admin_two["id"],
            dormant["id"],
        }
        assert all(e["actor_id"] == admin_id for e in created)
        registration_trail = client.get(
            f"/api/v1/audit?action=user.registered&target_id={registered['user_id']}",
            headers=admin_headers,
        ).json()
        assert [e["actor_id"] for e in registration_trail] == [registered["user_id"]]
        assert len(client.get("/api/v1/audit?limit=3", headers=admin_headers).json()) == 3
        # limit + offset page through the journal without overlap
        page_1 = client.get("/api/v1/audit?limit=3", headers=admin_headers).json()
        page_2 = client.get("/api/v1/audit?limit=3&offset=3", headers=admin_headers).json()
        assert [e["id"] for e in page_1] == [e["id"] for e in actions_entries[:3]]
        assert [e["id"] for e in page_2] == [e["id"] for e in actions_entries[3:6]]

        # Impersonation: the admin sees the platform exactly as the target
        as_learner = client.get(
            "/api/v1/users/me",
            headers={**admin_headers, "X-Impersonate-User": learner["id"]},
        )
        assert as_learner.status_code == 200
        assert as_learner.json()["id"] == learner["id"]
        as_manager_headers = {**admin_headers, "X-Impersonate-User": manager["id"]}
        listed = client.get("/api/v1/users", headers=as_manager_headers)
        assert listed.status_code == 200
        assert all([org["org_id"] for org in user["orgs"]] == [org_id] for user in listed.json())

        # Refusals: non-admin caller, platform-manager target, deactivated
        # target, unknown target, malformed header
        assert (
            client.get(
                "/api/v1/users/me",
                headers={**learner["headers"], "X-Impersonate-User": manager["id"]},
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/api/v1/users/me",
                headers={**admin_headers, "X-Impersonate-User": admin_two["id"]},
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/api/v1/users/me",
                headers={**admin_headers, "X-Impersonate-User": dormant["id"]},
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/api/v1/users/me",
                headers={**admin_headers, "X-Impersonate-User": str(uuid.uuid4())},
            ).status_code
            == 404
        )
        assert (
            client.get(
                "/api/v1/users/me",
                headers={**admin_headers, "X-Impersonate-User": "not-a-uuid"},
            ).status_code
            == 400
        )

        # A mutation done while impersonating blames the real admin
        created_as_manager = client.post(
            "/api/v1/users",
            headers=as_manager_headers,
            json={
                "email": "new@a.example",
                "full_name": "New",
                "roles": ["learner"],
                "org_id": org_id,
            },
        )
        assert created_as_manager.status_code == 201
        entry = client.get(
            f"/api/v1/audit?action=user.created&target_id={created_as_manager.json()['id']}",
            headers=admin_headers,
        ).json()[0]
        assert entry["actor_id"] == admin_id
        assert entry["impersonated_id"] == manager["id"]
