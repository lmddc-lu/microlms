"""Access policies, assignment scope and the no-escalation rule."""

import uuid
from pathlib import Path

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from microlms import (
    DEFAULT_ACCESS_POLICY,
    AccessPolicy,
    Permission,
    Settings,
    create_app,
    create_db_engine,
    create_session_factory,
)
from microlms.api.deps import require_permission
from microlms.auth import AnonymousAuthBackend
from microlms.domain.auth_tokens import AnonymousToken
from microlms.domain.users import User, role_assignments_for
from microlms.events import NullEventHooks
from microlms.storage import FilesystemStorage
from tests.helpers import (
    ALL_CORE_ROUTERS,
    DEFAULT_ORG_SLUG,
    default_org_id,
    issue_token,
    seed_admin,
    seed_default_org,
)


def test_custom_role_vocabulary_and_permissions(tmp_path: Path, database_url: str) -> None:
    """A deployment maps its own role labels to per-action permission grants."""
    # A vocabulary the core has never heard of: `superuser` manages user
    # accounts platform-wide and holds the client-defined `inspect_reports`
    # permission, `member` may access content, `viewer` grants nothing. No
    # role grants any course-authoring permission.
    user_management = {member for member in Permission if member.startswith("lms.user.")}
    access_policy = AccessPolicy(
        roles={
            "superuser": frozenset({*user_management, "inspect_reports"}),
            "member": frozenset({Permission.CONTENT_ACCESS}),
            "viewer": frozenset(),
        },
        assign_permissions={},
    )

    # Seed a superuser out-of-band and hand it an anonymous token; the org
    # roles are held in the default organization
    seed_default_org(database_url)
    default_org = default_org_id(database_url)
    token = uuid.uuid4()
    engine = create_db_engine(database_url, echo=False)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        su = User(
            full_name="Su",
            role_assignments=role_assignments_for({"superuser"}),
        )
        session.add(su)
        session.flush()
        session.add(AnonymousToken(token=token, user_id=su.id))
        session.commit()
    engine.dispose()
    su_headers = {"Authorization": f"Bearer {token}"}

    app = create_app(
        settings=Settings(database_url=database_url, registration_org_slug=DEFAULT_ORG_SLUG),
        engine=create_db_engine(database_url, echo=False),
        auth=AnonymousAuthBackend(),
        storage=FilesystemStorage(tmp_path / "storage"),
        hooks=NullEventHooks(),
        routers=ALL_CORE_ROUTERS,
        modules=[],
        schedules=[],
        access_policy=access_policy,
    )

    # A client router gated on a client-defined permission string: the core
    # carries the permission through without interpreting it.
    reports = APIRouter()

    @reports.get("/api/v1/reports", dependencies=[Depends(require_permission("inspect_reports"))])
    def read_reports() -> dict[str, int]:
        return {"count": 0}

    app.include_router(reports)

    with TestClient(app) as client:
        # The user-read permission lets the superuser list users
        assert client.get("/api/v1/users", headers=su_headers).status_code == 200

        # A user can be created with a permission-less custom role
        created = client.post(
            "/api/v1/users",
            headers=su_headers,
            json={"email": None, "full_name": "V", "roles": ["viewer"], "org_id": default_org},
        )
        assert created.status_code == 201
        assert created.json()["orgs"] == [
            {"org_id": default_org, "name": DEFAULT_ORG_SLUG, "roles": ["viewer"]}
        ]

        # A role outside the configured vocabulary is rejected
        assert (
            client.post(
                "/api/v1/users",
                headers=su_headers,
                json={"email": None, "full_name": "X", "roles": ["ghost"], "org_id": default_org},
            ).status_code
            == 422
        )

        # The permission-less role cannot manage anything
        viewer_headers = {
            "Authorization": f"Bearer {issue_token(database_url, created.json()['id'])}"
        }
        assert client.get("/api/v1/users", headers=viewer_headers).status_code == 403

        # The client-defined permission gates the client router: granted to the
        # superuser role, denied to the viewer, and 401 without credentials
        assert client.get("/api/v1/reports", headers=su_headers).status_code == 200
        assert client.get("/api/v1/reports", headers=viewer_headers).status_code == 403
        assert client.get("/api/v1/reports").status_code == 401

        # Every action checks its own permission: the user-managing superuser
        # was granted neither content access (the catalog refuses it) nor
        # course authoring (creating a course refuses it).
        assert client.get("/api/v1/courses", headers=viewer_headers).status_code == 403
        assert client.get("/api/v1/courses", headers=su_headers).status_code == 403
        assert (
            client.post(
                "/api/v1/courses",
                headers=su_headers,
                json={"owner_org_id": None, "slug": "c", "title": "C", "description": ""},
            ).status_code
            == 403
        )
        # ...and, lacking content access, cannot hand out the member role
        # granting it (no escalation)
        assert (
            client.post(
                "/api/v1/users",
                headers=su_headers,
                json={"email": None, "full_name": "M", "roles": ["member"], "org_id": default_org},
            ).status_code
            == 403
        )

    # A wildcard grant would silently never match, and an unknown core
    # permission is a typo the core never checks, so create_app refuses
    # either policy up front.
    for broken, message in (
        ("lms.user.*", "wildcards are not supported"),
        ("lms.course.edt", "unknown core permission"),
    ):
        with pytest.raises(ValueError, match=message):
            create_app(
                settings=Settings(
                    database_url=database_url, registration_org_slug=DEFAULT_ORG_SLUG
                ),
                engine=create_db_engine(database_url, echo=False),
                auth=AnonymousAuthBackend(),
                storage=FilesystemStorage(tmp_path / "storage"),
                hooks=NullEventHooks(),
                routers=ALL_CORE_ROUTERS,
                modules=[],
                schedules=[],
                access_policy=AccessPolicy(
                    roles={"broken": frozenset({broken})}, assign_permissions={}
                ),
            )


def test_scope_and_no_escalation_follow_the_assignment(tmp_path: Path, database_url: str) -> None:
    """Permissions apply at the scope a role is assigned at, and nobody
    assigns a role or acts on an account holding a permission they lack."""
    admin_token = seed_admin(database_url)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    # `auditor` reads the audit trail; `reporter` and `commenter` hold
    # client-defined permissions. The admin holds every one of them.
    app = create_app(
        settings=Settings(database_url=database_url, registration_org_slug=DEFAULT_ORG_SLUG),
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
                "admin": frozenset(Permission) | {"demo.reports.inspect", "demo.comments.post"},
                "auditor": frozenset({Permission.AUDIT_READ}),
                "reporter": frozenset({"demo.reports.inspect"}),
                "commenter": frozenset({"demo.comments.post"}),
            },
            assign_permissions={},
        ),
    )

    def _new_user(client: TestClient, roles: list[str], org_id: int) -> dict:
        created = client.post(
            "/api/v1/users",
            headers=admin_headers,
            json={"email": None, "full_name": "U", "roles": roles, "org_id": org_id},
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
        boss = _new_user(client, ["manager", "commenter"], org_a)
        assert (
            client.put(
                f"/api/v1/users/{boss['id']}/roles/auditor", headers=admin_headers
            ).status_code
            == 200
        )
        a_learner = _new_user(client, ["learner"], org_a)
        b_learner = _new_user(client, ["learner"], org_b)

        # The auditor platform role reaches the platform-global audit trail...
        assert client.get("/api/v1/audit", headers=boss["headers"]).status_code == 200
        # ...but does not widen the manager role: user management stays
        # pinned to the manager's own org
        assert (
            client.get(f"/api/v1/users/{b_learner['id']}", headers=boss["headers"]).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/users",
                headers=boss["headers"],
                json={"email": None, "full_name": "X", "roles": ["learner"], "org_id": org_b},
            ).status_code
            == 403
        )

        # No escalation: the manager assigns roles whose permissions they hold
        # in the org (commenter, and manager itself), never one granting
        # something they lack (reporter, instructor), and platform roles not
        # at all without a platform assignment of the role-editing permission
        roles_url = f"/api/v1/users/{a_learner['id']}/orgs/{org_a}/roles"
        assignable = client.get(
            f"/api/v1/users/me/assignable-roles?org_id={org_a}", headers=boss["headers"]
        ).json()
        # (the platform auditor grant applies inside A too)
        assert assignable == ["auditor", "commenter", "learner", "manager"]
        assert client.get("/api/v1/users/me/assignable-roles", headers=boss["headers"]).json() == [
            "auditor"
        ]
        assert client.put(f"{roles_url}/reporter", headers=boss["headers"]).status_code == 403
        assert client.put(f"{roles_url}/instructor", headers=boss["headers"]).status_code == 403
        assert (
            client.put(
                f"/api/v1/users/{a_learner['id']}/roles/auditor", headers=boss["headers"]
            ).status_code
            == 403
        )
        granted = client.put(f"{roles_url}/commenter", headers=boss["headers"])
        assert granted.status_code == 200
        assert granted.json()["orgs"] == [
            {"org_id": org_a, "name": "A", "roles": ["commenter", "learner"]}
        ]
        # A manager may make a peer and still act on them: they hold no more
        promoted = client.put(f"{roles_url}/manager", headers=boss["headers"])
        assert promoted.status_code == 200
        assert (
            client.post(
                f"/api/v1/users/{a_learner['id']}/deactivate", headers=boss["headers"]
            ).status_code
            == 200
        )

        # An account holding a permission the manager lacks is out of reach:
        # not read, not listed, not acted on
        reporter = _new_user(client, ["reporter"], org_a)
        assert (
            client.get(f"/api/v1/users/{reporter['id']}", headers=boss["headers"]).status_code
            == 403
        )
        listed = client.get("/api/v1/users", headers=boss["headers"]).json()
        assert reporter["id"] not in {user["id"] for user in listed}
        assert a_learner["id"] in {user["id"] for user in listed}
        assert (
            client.post(
                f"/api/v1/users/{reporter['id']}/deactivate", headers=boss["headers"]
            ).status_code
            == 403
        )

        # A user holds a different role in each org: a manager of A who is a
        # learner in B manages A's users only
        dual = _new_user(client, ["manager"], org_a)
        assert (
            client.put(
                f"/api/v1/users/{dual['id']}/orgs/{org_b}/roles/learner", headers=admin_headers
            ).status_code
            == 200
        )
        assert (
            client.get(f"/api/v1/users/{reporter['id']}", headers=dual["headers"]).status_code
            == 403
        )
        assert (
            client.get(f"/api/v1/users/{b_learner['id']}", headers=dual["headers"]).status_code
            == 403
        )

        # A user in A and B: A's manager reads them (seeing A's roles only) and
        # manages their roles in A, but account-wide actions need every org of
        # the user in reach
        shared = _new_user(client, ["learner"], org_a)
        client.put(
            f"/api/v1/users/{shared['id']}/orgs/{org_b}/roles/learner", headers=admin_headers
        )
        read = client.get(f"/api/v1/users/{shared['id']}", headers=boss["headers"])
        assert read.status_code == 200
        assert read.json()["orgs"] == [{"org_id": org_a, "name": "A", "roles": ["learner"]}]
        assert (
            client.put(
                f"/api/v1/users/{shared['id']}/orgs/{org_a}/roles/commenter",
                headers=boss["headers"],
            ).status_code
            == 200
        )
        for account_action in ("deactivate", "activate"):
            assert (
                client.post(
                    f"/api/v1/users/{shared['id']}/{account_action}", headers=boss["headers"]
                ).status_code
                == 403
            )
        assert (
            client.delete(f"/api/v1/users/{shared['id']}", headers=boss["headers"]).status_code
            == 403
        )

        # Any role may be assigned at platform scope: manager held on the
        # platform manages users in every organization
        roaming = _new_user(client, ["learner"], org_a)
        assert (
            client.put(
                f"/api/v1/users/{roaming['id']}/roles/manager", headers=admin_headers
            ).status_code
            == 200
        )
        assert (
            client.get(f"/api/v1/users/{b_learner['id']}", headers=roaming["headers"]).status_code
            == 200
        )
        # ...yet the platform-global endpoints still need their own permission
        assert client.get("/api/v1/orgs", headers=roaming["headers"]).status_code == 403


def test_assign_permission_keeps_a_role_out_of_its_holders_reach(
    tmp_path: Path, database_url: str
) -> None:
    """A role with an assign permission is assigned, and its holders acted
    on, only by actors holding that permission too: managers neither appoint
    nor manage fellow managers, while the admin, who holds it, does both."""
    admin_token = seed_admin(database_url)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    def _app(policy: AccessPolicy) -> FastAPI:
        return create_app(
            settings=Settings(database_url=database_url, registration_org_slug=DEFAULT_ORG_SLUG),
            engine=create_db_engine(database_url, echo=False),
            auth=AnonymousAuthBackend(),
            storage=FilesystemStorage(tmp_path / "storage"),
            hooks=NullEventHooks(),
            routers=ALL_CORE_ROUTERS,
            modules=[],
            schedules=[],
            access_policy=policy,
        )

    # An assign permission must be a valid permission string of a known role
    for assign_permissions, message in (
        ({"ghost": "demo.ghost.assign"}, "not in AccessPolicy.roles"),
        ({"manager": "demo.*"}, "wildcards are not supported"),
        ({"manager": "lms.manager.assign"}, "unknown core permission"),
    ):
        with pytest.raises(ValueError, match=message):
            _app(
                AccessPolicy(
                    roles=DEFAULT_ACCESS_POLICY.roles, assign_permissions=assign_permissions
                )
            )

    app = _app(
        AccessPolicy(
            roles={
                **DEFAULT_ACCESS_POLICY.roles,
                "admin": frozenset(Permission) | {"demo.manager.assign"},
            },
            assign_permissions={"manager": "demo.manager.assign"},
        )
    )

    def _new_user(client: TestClient, roles: list[str], org_id: int) -> dict:
        created = client.post(
            "/api/v1/users",
            headers=admin_headers,
            json={"email": None, "full_name": "U", "roles": roles, "org_id": org_id},
        )
        assert created.status_code == 201
        user = created.json()
        user["headers"] = {"Authorization": f"Bearer {issue_token(database_url, user['id'])}"}
        return user

    with TestClient(app) as client:
        org = client.post(
            "/api/v1/orgs", headers=admin_headers, json={"slug": "a", "name": "A"}
        ).json()["id"]
        boss = _new_user(client, ["manager"], org)
        peer = _new_user(client, ["manager"], org)
        learner = _new_user(client, ["learner"], org)

        # The manager holds every permission of the role, but not its assign
        # permission: they cannot hand it out...
        assert client.get(
            f"/api/v1/users/me/assignable-roles?org_id={org}", headers=boss["headers"]
        ).json() == ["learner"]
        assert (
            client.put(
                f"/api/v1/users/{learner['id']}/orgs/{org}/roles/manager", headers=boss["headers"]
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/users",
                headers=boss["headers"],
                json={"email": None, "full_name": "X", "roles": ["manager"], "org_id": org},
            ).status_code
            == 403
        )
        # ...nor read, list or act on a fellow manager, while learners stay in reach
        assert client.get(f"/api/v1/users/{peer['id']}", headers=boss["headers"]).status_code == 403
        listed = {
            user["id"] for user in client.get("/api/v1/users", headers=boss["headers"]).json()
        }
        assert peer["id"] not in listed
        assert learner["id"] in listed
        assert (
            client.post(
                f"/api/v1/users/{peer['id']}/deactivate", headers=boss["headers"]
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/v1/users/{learner['id']}/deactivate", headers=boss["headers"]
            ).status_code
            == 200
        )

        # The admin holds the assign permission: they appoint managers and
        # act on them, impersonation included
        assert (
            client.put(
                f"/api/v1/users/{learner['id']}/orgs/{org}/roles/manager", headers=admin_headers
            ).status_code
            == 200
        )
        assert client.get(f"/api/v1/users/{peer['id']}", headers=admin_headers).status_code == 200
        viewed = client.get(
            "/api/v1/users/me", headers={**admin_headers, "X-Impersonate-User": boss["id"]}
        )
        assert viewed.status_code == 200
        assert viewed.json()["id"] == boss["id"]


def test_org_scoped_course_authoring_and_platform_gates(tmp_path: Path, database_url: str) -> None:
    """Authoring follows the uniform reach rule: org-scoped authors act only
    on courses their org owns, and platform-global actions are refused
    without platform scope even when the permission is granted."""
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
        # "org_admin" holds the org permissions through an org-reach role, so
        # the platform-global org endpoints must refuse it
        access_policy=AccessPolicy(
            roles={
                **DEFAULT_ACCESS_POLICY.roles,
                "org_admin": frozenset(
                    {Permission.ORG_READ, Permission.ORG_CREATE, Permission.ORG_DELETE}
                ),
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
        author_a = _new_user(client, "ia@a.example", ["instructor"], org_a)
        author_b = _new_user(client, "ib@b.example", ["instructor"], org_b)
        wannabe = _new_user(client, "wannabe@a.example", ["org_admin"], org_a)

        # An org author must name an owning org in their reach...
        for owner in (None, org_b):
            assert (
                client.post(
                    "/api/v1/courses",
                    headers=author_a["headers"],
                    json={"owner_org_id": owner, "slug": "x", "title": "X", "description": ""},
                ).status_code
                == 403
            )
        # ...and the course is owned by and visible to that org
        created = client.post(
            "/api/v1/courses",
            headers=author_a["headers"],
            json={
                "owner_org_id": org_a,
                "slug": "a-course",
                "title": "A course",
                "description": "",
            },
        )
        assert created.status_code == 201
        assert created.json()["owner_org_id"] == org_a
        assert created.json()["org_ids"] == [org_a]
        course_id = created.json()["id"]

        # In-reach authoring works end to end (edit, publish)
        assert (
            client.put(
                f"/api/v1/courses/{course_id}",
                headers=author_a["headers"],
                json={"slug": "a-course", "title": "A course v2", "description": ""},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/v1/courses/{course_id}/publish", headers=author_a["headers"]
            ).status_code
            == 200
        )

        # The other org's author neither edits nor reads it (visible to A only)
        assert (
            client.put(
                f"/api/v1/courses/{course_id}",
                headers=author_b["headers"],
                json={"slug": "a-course", "title": "hijack", "description": ""},
            ).status_code
            == 403
        )
        assert (
            client.get(f"/api/v1/courses/{course_id}", headers=author_b["headers"]).status_code
            == 403
        )

        # Drafts stay inside the owning org: the author's listing shows their
        # org's draft, the other org's listing does not
        draft_id = client.post(
            "/api/v1/courses",
            headers=author_a["headers"],
            json={"owner_org_id": org_a, "slug": "a-draft", "title": "A draft", "description": ""},
        ).json()["id"]
        assert draft_id in [
            c["id"] for c in client.get("/api/v1/courses", headers=author_a["headers"]).json()
        ]
        assert draft_id not in [
            c["id"] for c in client.get("/api/v1/courses", headers=author_b["headers"]).json()
        ]

        # A platform-owned course is out of an org author's reach; a
        # platform-scoped author reaches everything
        platform_course = client.post(
            "/api/v1/courses",
            headers=admin_headers,
            json={"owner_org_id": None, "slug": "global", "title": "Global", "description": ""},
        ).json()
        assert platform_course["owner_org_id"] is None
        assert (
            client.put(
                f"/api/v1/courses/{platform_course['id']}",
                headers=author_a["headers"],
                json={"slug": "global", "title": "hijack", "description": ""},
            ).status_code
            == 403
        )
        assert (
            client.put(
                f"/api/v1/courses/{course_id}",
                headers=admin_headers,
                json={"slug": "a-course", "title": "A course v3", "description": ""},
            ).status_code
            == 200
        )

        # Visibility edits are pinned to the author's own org
        assert (
            client.put(
                f"/api/v1/courses/{course_id}/orgs/{org_b}", headers=author_a["headers"]
            ).status_code
            == 403
        )

        # Platform-global actions refuse the permission without platform scope
        assert client.get("/api/v1/orgs", headers=wannabe["headers"]).status_code == 403
        assert client.get("/api/v1/orgs", headers=admin_headers).status_code == 200
