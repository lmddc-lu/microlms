"""xapi module: statement ingestion, reads, aggregates and ingest keys."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from microlms import (
    DEFAULT_ACCESS_POLICY,
    AccessPolicy,
    Settings,
    create_app,
    create_db_engine,
    create_session_factory,
)
from microlms.auth import AnonymousAuthBackend
from microlms.domain.users import User
from microlms.events import NullEventHooks
from microlms.storage import FilesystemStorage
from tests.helpers import (
    ALL_CORE_ROUTERS,
    DEFAULT_ORG_SLUG,
    GUEST_ORG_SLUG,
    FakeLrs,
    issue_token,
    org_id_of,
    seed_admin,
    seed_default_org,
    seed_org,
)


def test_xapi_ingestion_loop(tmp_path: Path, database_url: str) -> None:

    # The xapi module needs the optional `xapi` extra (httpx); skip without it
    pytest.importorskip("httpx")

    from microlms.lrs import get_lrs_client

    # Guests are provisioned into their dedicated organization, apart from
    # the organization self-registered members join
    seed_org(database_url, GUEST_ORG_SLUG)
    seed_default_org(database_url)

    fake_lrs = FakeLrs()
    app = create_app(
        settings=Settings(
            database_url=database_url,
            registration_org_slug=DEFAULT_ORG_SLUG,
            xapi_ingest_keys=frozenset({"ingest-key"}),
            xapi_guest_role="guest",
            xapi_guest_org_slug=GUEST_ORG_SLUG,
            lrs_actor_home_page="https://platform.example/",
        ),
        engine=create_db_engine(database_url, echo=False),
        auth=AnonymousAuthBackend(),
        storage=FilesystemStorage(tmp_path / "storage"),
        hooks=NullEventHooks(),
        routers=ALL_CORE_ROUTERS,
        modules=["xapi"],
        schedules=[],
        access_policy=AccessPolicy(
            roles={
                **DEFAULT_ACCESS_POLICY.roles,
                "guest": frozenset(),
            },
            assign_permissions={},
        ),
    )
    app.dependency_overrides[get_lrs_client] = lambda: fake_lrs

    survey = {
        "verb": {"id": "http://adlnet.gov/expapi/verbs/answered"},
        "object": {"id": "https://example.com/survey/q1"},
        "result": {"response": "yes"},
    }
    key = {"X-Ingest-Key": "ingest-key"}

    with TestClient(app) as client:
        assert client.get("/api/v1/xapi/status").json()["status"] == "ready"

        # No key and wrong key are both rejected
        assert client.post("/api/v1/xapi/statements", json=survey).status_code == 401
        assert (
            client.post(
                "/api/v1/xapi/statements", json=survey, headers={"X-Ingest-Key": "nope"}
            ).status_code
            == 401
        )

        # A guest statement must carry a GUID; a malformed one is rejected
        assert client.post("/api/v1/xapi/statements", json=survey, headers=key).status_code == 400
        malformed = {**survey, "actor": {"account": {"name": "not-a-uuid"}}}
        assert (
            client.post("/api/v1/xapi/statements", json=malformed, headers=key).status_code == 400
        )

        # verb.id / object.id are required (pydantic validation)
        assert (
            client.post(
                "/api/v1/xapi/statements", json={"object": {"id": "x"}}, headers=key
            ).status_code
            == 422
        )

        # A guest GUID provisions a ghost profile and stores the statement, with
        # a client-supplied timestamp preserved (offline sync)
        guest_guid = str(uuid.uuid4())
        guest_stmt = {
            **survey,
            "actor": {"objectType": "Agent", "account": {"name": guest_guid, "homePage": "device"}},
            "timestamp": "2026-07-09T10:00:00Z",
        }
        stored = client.post("/api/v1/xapi/statements", json=guest_stmt, headers=key)
        assert stored.status_code == 200
        assert stored.json()["stored"] == 1

        # A second submission for the same GUID attaches to the same profile
        assert (
            client.post("/api/v1/xapi/statements", json=guest_stmt, headers=key).json()["stored"]
            == 1
        )

        # An authenticated user submits with their bearer token; the actor is
        # bound to them
        member = client.post("/auth/register").json()
        bearer = {"Authorization": f"Bearer {member['token']}", **key}
        assert (
            client.post("/api/v1/xapi/statements", json=survey, headers=bearer).status_code == 200
        )

        # An authenticated user cannot write a statement for someone else's GUID
        spoof = {**survey, "actor": {"account": {"name": str(uuid.uuid4())}}}
        assert client.post("/api/v1/xapi/statements", json=spoof, headers=bearer).status_code == 403

        # A batch of statements is accepted in one call
        batch = client.post("/api/v1/xapi/statements", json=[guest_stmt, guest_stmt], headers=key)
        assert batch.json()["stored"] == 2

    # The ghost profile exists: one guest user, no email, holding the guest
    # role - and its provisioning left exactly one actor-less audit entry
    # despite the repeated submissions
    from sqlalchemy import select

    from microlms.domain.audit import AuditLogEntry

    engine = create_db_engine(database_url, echo=False)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        ghost = session.get(User, uuid.UUID(guest_guid))
        assert ghost is not None
        assert ghost.email is None
        assert ghost.roles == frozenset()
        assert ghost.org_roles == {org_id_of(database_url, GUEST_ORG_SLUG): frozenset({"guest"})}
        provisioned = list(
            session.scalars(select(AuditLogEntry).where(AuditLogEntry.action == "user.provisioned"))
        )
        assert [entry.target_id for entry in provisioned] == [guest_guid]
        assert provisioned[0].actor_id is None
    engine.dispose()

    # Every forwarded statement carries the canonical account actor, and the
    # guest's client timestamp survived the round-trip
    names = {s["actor"]["account"]["name"] for s in fake_lrs.statements}
    assert guest_guid in names and member["user_id"] in names
    # The configured stable IRI (trailing slash stripped) is stamped on every
    # actor, regardless of the hostname the request came through
    assert all(
        s["actor"]["account"]["homePage"] == "https://platform.example" for s in fake_lrs.statements
    )
    assert any(s.get("timestamp") == "2026-07-09T10:00:00Z" for s in fake_lrs.statements)


def test_xapi_read_and_aggregate(tmp_path: Path, monkeypatch, database_url: str) -> None:

    # The xapi module needs the optional `xapi` extra (httpx); skip without it
    pytest.importorskip("httpx")

    from microlms.lrs import get_lrs_client
    from microlms.modules.xapi import router as xapi_router

    admin_token = seed_admin(database_url)
    seed_org(database_url, GUEST_ORG_SLUG)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    fake_lrs = FakeLrs()
    app = create_app(
        settings=Settings(
            database_url=database_url,
            registration_org_slug=DEFAULT_ORG_SLUG,
            xapi_ingest_keys=frozenset({"ingest-key"}),
            xapi_guest_role="guest",
            xapi_guest_org_slug=GUEST_ORG_SLUG,
        ),
        engine=create_db_engine(database_url, echo=False),
        auth=AnonymousAuthBackend(),
        storage=FilesystemStorage(tmp_path / "storage"),
        hooks=NullEventHooks(),
        routers=ALL_CORE_ROUTERS,
        modules=["xapi"],
        schedules=[],
        access_policy=AccessPolicy(
            roles={
                **DEFAULT_ACCESS_POLICY.roles,
                "guest": frozenset(),
            },
            assign_permissions={},
        ),
    )
    app.dependency_overrides[get_lrs_client] = lambda: fake_lrs

    key = {"X-Ingest-Key": "ingest-key"}

    def _survey(response: str) -> dict:
        return {
            "verb": {"id": "http://adlnet.gov/expapi/verbs/answered"},
            "object": {"id": "https://example.com/survey/q1"},
            "result": {"response": response},
        }

    with TestClient(app) as client:
        org_a = client.post(
            "/api/v1/orgs", headers=admin_headers, json={"slug": "a", "name": "A"}
        ).json()["id"]
        org_b = client.post(
            "/api/v1/orgs", headers=admin_headers, json={"slug": "b", "name": "B"}
        ).json()["id"]

        def _new_user(email: str, roles: list[str], org_id: int | None) -> dict:
            user = client.post(
                "/api/v1/users",
                headers=admin_headers,
                json={"email": email, "full_name": email, "roles": roles, "org_id": org_id},
            ).json()
            user["headers"] = {"Authorization": f"Bearer {issue_token(database_url, user['id'])}"}
            return user

        manager = _new_user("boss@a.example", ["manager"], org_a)
        user_a = _new_user("ua@a.example", ["learner"], org_a)
        user_b = _new_user("ub@b.example", ["learner"], org_b)

        # Ingest: two answers from user_a, one from user_b, one anonymous guest
        for response in ("yes", "no"):
            assert (
                client.post(
                    "/api/v1/xapi/statements",
                    json=_survey(response),
                    headers={**key, **user_a["headers"]},
                ).status_code
                == 200
            )
        assert (
            client.post(
                "/api/v1/xapi/statements", json=_survey("yes"), headers={**key, **user_b["headers"]}
            ).status_code
            == 200
        )
        guest_guid = str(uuid.uuid4())
        guest_stmt = {**_survey("yes"), "actor": {"account": {"name": guest_guid}}}
        assert (
            client.post("/api/v1/xapi/statements", json=guest_stmt, headers=key).status_code == 200
        )

        # Per-user readback is management-scoped
        path_a = f"/api/v1/xapi/users/{user_a['id']}/statements"
        assert client.get(path_a).status_code == 401
        assert client.get(path_a, headers=user_a["headers"]).status_code == 403
        page = client.get(path_a, headers=manager["headers"]).json()
        assert len(page["statements"]) == 2 and page["more"] is False
        assert {s["actor"]["account"]["name"] for s in page["statements"]} == {user_a["id"]}
        assert (
            client.get(
                f"/api/v1/xapi/users/{user_b['id']}/statements", headers=manager["headers"]
            ).status_code
            == 403
        )
        assert (
            client.get(
                f"/api/v1/xapi/users/{uuid.uuid4()}/statements", headers=manager["headers"]
            ).status_code
            == 404
        )
        # ...and, like the users router, never reaches accounts holding a
        # permission the manager lacks, even in the manager's own org
        author = _new_user("author@a.example", ["instructor"], org_a)
        assert (
            client.get(
                f"/api/v1/xapi/users/{author['id']}/statements", headers=manager["headers"]
            ).status_code
            == 403
        )
        assert (
            client.get(
                f"/api/v1/xapi/users/{author['id']}/statements", headers=admin_headers
            ).status_code
            == 200
        )

        # Global aggregate: every statement counted, no individual identifiers
        aggregate = client.get("/api/v1/xapi/statements/aggregate", headers=admin_headers)
        body = aggregate.json()
        assert body["statement_count"] == 4 and body["actor_count"] == 3
        assert body["by_verb"] == {"http://adlnet.gov/expapi/verbs/answered": 4}
        assert body["by_response"] == {"yes": 3, "no": 1}
        assert body["truncated"] is False
        for guid in (guest_guid, user_a["id"], user_b["id"]):
            assert guid not in aggregate.text

        # An org manager aggregates only their own organization
        scoped = client.get("/api/v1/xapi/statements/aggregate", headers=manager["headers"]).json()
        assert scoped["statement_count"] == 2 and scoped["actor_count"] == 1
        assert (
            client.get(
                f"/api/v1/xapi/statements/aggregate?org_id={org_b}", headers=manager["headers"]
            ).status_code
            == 403
        )

        # Role filter isolates the anonymous guest population
        guests = client.get(
            "/api/v1/xapi/statements/aggregate?role=guest", headers=admin_headers
        ).json()
        assert guests["statement_count"] == 1 and guests["actor_count"] == 1

        # Time spent on an enrollment: terminated durations recorded under the
        # enrollment's registration, other registrations and bad durations skipped
        course_id = client.post(
            "/api/v1/courses",
            headers=admin_headers,
            json={
                "owner_org_id": None,
                "slug": "xapi-basics",
                "title": "xAPI Basics",
                "description": "",
            },
        ).json()["id"]
        client.post(f"/api/v1/courses/{course_id}/publish", headers=admin_headers)
        enrollment = client.post(
            "/api/v1/enrollments",
            headers=admin_headers,
            json={"user_id": user_a["id"], "course_id": course_id},
        ).json()

        def _terminated(duration: str, registration: str) -> dict:
            return {
                "actor": {"objectType": "Agent", "account": {"name": user_a["id"]}},
                "verb": {"id": "http://adlnet.gov/expapi/verbs/terminated"},
                "object": {"id": "https://example.com/course/lesson"},
                "context": {"registration": registration},
                "result": {"duration": duration},
            }

        fake_lrs.statements.extend(
            [
                _terminated("PT1M30S", enrollment["registration"]),
                _terminated("PT2M", enrollment["registration"]),
                _terminated("PT1H", str(uuid.uuid4())),
                _terminated("bogus", enrollment["registration"]),
            ]
        )
        time_path = f"/api/v1/xapi/enrollments/{enrollment['id']}/time-spent"
        spent = client.get(time_path, headers=admin_headers).json()
        assert spent["seconds"] == 210.0
        assert spent["statement_count"] == 3 and spent["counted"] == 2
        assert spent["truncated"] is False

        # Same management scoping as the per-user readback
        assert client.get(time_path, headers=manager["headers"]).status_code == 200
        assert client.get(time_path, headers=user_a["headers"]).status_code == 403
        assert (
            client.get(
                "/api/v1/xapi/enrollments/999999/time-spent", headers=admin_headers
            ).status_code
            == 404
        )

        # Ingested statements carry the ingestion category, keeping categories
        # the sender set, so reads can leave out content activity such as the
        # terminated statements above
        tagged = {**_survey("maybe"), "context": {"contextActivities": {"category": {"id": "c"}}}}
        assert (
            client.post(
                "/api/v1/xapi/statements", json=tagged, headers={**key, **user_a["headers"]}
            ).status_code
            == 200
        )
        assert fake_lrs.statements[-1]["context"]["contextActivities"]["category"] == [
            {"id": "c"},
            {"id": "urn:microlms:xapi:ingested", "objectType": "Activity"},
        ]
        ingested = client.get(
            "/api/v1/xapi/statements/aggregate?ingested_only=true", headers=admin_headers
        ).json()
        assert ingested["statement_count"] == 5
        assert set(ingested["by_verb"]) == {"http://adlnet.gov/expapi/verbs/answered"}
        assert (
            client.get("/api/v1/xapi/statements/aggregate", headers=admin_headers).json()[
                "statement_count"
            ]
            == 9
        )
        own = client.get(f"{path_a}?ingested_only=true", headers=manager["headers"]).json()
        assert len(own["statements"]) == 3
        assert len(client.get(path_a, headers=manager["headers"]).json()["statements"]) == 7
        assert (
            client.get(
                "/api/v1/xapi/statements/aggregate?ingested_only=true&activity=x",
                headers=admin_headers,
            ).status_code
            == 422
        )
        malformed = {**_survey("no"), "context": {"contextActivities": {"category": "c"}}}
        assert (
            client.post(
                "/api/v1/xapi/statements", json=malformed, headers={**key, **user_a["headers"]}
            ).status_code
            == 400
        )

        # Paging is followed transparently; hitting the scan ceiling is reported
        fake_lrs.page_size = 2
        paged = client.get("/api/v1/xapi/statements/aggregate", headers=admin_headers).json()
        assert paged["statement_count"] == 9 and paged["truncated"] is False
        monkeypatch.setattr(xapi_router, "MAX_AGGREGATE_STATEMENTS", 2)
        capped = client.get("/api/v1/xapi/statements/aggregate", headers=admin_headers).json()
        assert capped["statement_count"] == 2 and capped["truncated"] is True


def test_xapi_ingest_key_management(tmp_path: Path, database_url: str) -> None:

    # The xapi module needs the optional `xapi` extra (httpx); skip without it
    pytest.importorskip("httpx")

    from microlms.lrs import get_lrs_client

    admin_token = seed_admin(database_url)
    seed_org(database_url, GUEST_ORG_SLUG)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    fake_lrs = FakeLrs()
    # No statically configured key: ingestion authorization must come from the
    # runtime-managed keys alone
    app = create_app(
        settings=Settings(
            database_url=database_url,
            registration_org_slug=DEFAULT_ORG_SLUG,
            xapi_guest_role="guest",
            xapi_guest_org_slug=GUEST_ORG_SLUG,
        ),
        engine=create_db_engine(database_url, echo=False),
        auth=AnonymousAuthBackend(),
        storage=FilesystemStorage(tmp_path / "storage"),
        hooks=NullEventHooks(),
        routers=ALL_CORE_ROUTERS,
        modules=["xapi"],
        schedules=[],
        access_policy=AccessPolicy(
            roles={
                **DEFAULT_ACCESS_POLICY.roles,
                "guest": frozenset(),
            },
            assign_permissions={},
        ),
    )
    app.dependency_overrides[get_lrs_client] = lambda: fake_lrs

    guest_stmt = {
        "verb": {"id": "http://adlnet.gov/expapi/verbs/answered"},
        "object": {"id": "https://example.com/survey/q1"},
        "actor": {"account": {"name": str(uuid.uuid4())}},
    }

    with TestClient(app) as client:
        learner = client.post("/auth/register").json()
        learner_headers = {"Authorization": f"Bearer {learner['token']}"}

        # With no key configured anywhere, ingestion reports itself unconfigured
        assert client.post("/api/v1/xapi/statements", json=guest_stmt).status_code == 503
        assert (
            client.post(
                "/api/v1/xapi/statements", json=guest_stmt, headers={"X-Ingest-Key": "nope"}
            ).status_code
            == 503
        )

        # The supervision surface is admin-only
        assert client.get("/api/v1/xapi/config").status_code == 401
        assert client.get("/api/v1/xapi/config", headers=learner_headers).status_code == 403
        assert client.get("/api/v1/xapi/keys", headers=learner_headers).status_code == 403
        assert (
            client.post(
                "/api/v1/xapi/keys", headers=learner_headers, json={"name": "rogue"}
            ).status_code
            == 403
        )

        # The config snapshot states the ingestion contract
        config = client.get("/api/v1/xapi/config", headers=admin_headers).json()
        assert config["ingest_url"].endswith("/api/v1/xapi/statements")
        assert config["key_header"] == "X-Ingest-Key"
        assert config["xapi_version"] == "1.0.3"
        assert config["accepts_batch"] is True
        assert config["required_fields"] == ["verb.id", "object.id"]
        assert config["guest_ingestion_enabled"] is True and config["guest_role"] == "guest"
        assert config["guest_org_slug"] == GUEST_ORG_SLUG
        assert config["static_key_count"] == 0
        assert config["lrs_url"] is None

        # An admin issues a key: material is generated server-side
        created = client.post(
            "/api/v1/xapi/keys", headers=admin_headers, json={"name": "Ingestion app"}
        )
        assert created.status_code == 201
        issued = created.json()
        assert issued["name"] == "Ingestion app" and issued["is_active"] is True
        assert len(issued["key"]) > 20 and issued["last_used_at"] is None

        # The issued key authorizes ingestion and its use is stamped
        assert (
            client.post(
                "/api/v1/xapi/statements",
                json=guest_stmt,
                headers={"X-Ingest-Key": issued["key"]},
            ).status_code
            == 200
        )
        listed = client.get("/api/v1/xapi/keys", headers=admin_headers).json()
        assert [k["id"] for k in listed] == [issued["id"]]
        assert listed[0]["key"] == issued["key"] and listed[0]["last_used_at"] is not None

        # Revocation shuts the key off; a configured deployment answers 401,
        # not 503
        assert (
            client.post("/api/v1/xapi/keys/9999/revoke", headers=admin_headers).status_code == 404
        )
        revoked = client.post(
            f"/api/v1/xapi/keys/{issued['id']}/revoke", headers=admin_headers
        ).json()
        assert revoked["is_active"] is False and revoked["revoked_at"] is not None
        assert (
            client.post(
                "/api/v1/xapi/statements",
                json=guest_stmt,
                headers={"X-Ingest-Key": issued["key"]},
            ).status_code
            == 401
        )

        # Key lifecycle is audited, without ever recording the key material
        audit = client.get("/api/v1/audit?target_type=xapi_key", headers=admin_headers)
        actions = [entry["action"] for entry in audit.json()]
        assert actions == ["xapi_key.revoked", "xapi_key.created"]
        assert issued["key"] not in audit.text
