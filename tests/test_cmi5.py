"""cmi5 module: launch loop, xAPI proxy and example package import."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from microlms import (
    DEFAULT_ACCESS_POLICY,
    Settings,
    create_app,
    create_db_engine,
    create_session_factory,
)
from microlms.auth import AnonymousAuthBackend
from microlms.events import NullEventHooks
from microlms.storage import FilesystemStorage
from tests.helpers import (
    ALL_CORE_ROUTERS,
    DEFAULT_ORG_SLUG,
    FIXTURES_DIR,
    FakeLrs,
    RecordingHooks,
    cmi5_zip,
    seed_admin,
    verb_ids,
)


def test_proxy_reconciles_document_write_concurrency() -> None:
    # AUs PUT State/Profile documents unconditionally; lrsql needs a concurrency
    # header once the document exists, so the proxy supplies one.
    pytest.importorskip("httpx")
    from microlms.modules.cmi5.router import _reconcile_concurrency

    class FakeEtagLrs:
        def __init__(self, etag: str | None) -> None:
            self.etag = etag
            self.calls = 0

        def document_etag(self, path: str, query: str) -> str | None:
            self.calls += 1
            return self.etag

    # Existing document -> If-Match with its ETag
    existing = _reconcile_concurrency(
        FakeEtagLrs('"e1"'), "PUT", "activities/state", "stateId=x", {}
    )
    assert existing["If-Match"] == '"e1"'

    # Absent document -> If-None-Match * (create only)
    absent = _reconcile_concurrency(FakeEtagLrs(None), "PUT", "activities/state", "stateId=x", {})
    assert absent["If-None-Match"] == "*"

    # Statements are not a document resource: untouched, no preflight
    stmt_lrs = FakeEtagLrs('"e"')
    stmt = _reconcile_concurrency(stmt_lrs, "POST", "statements", "", {})
    assert "If-Match" not in stmt and "If-None-Match" not in stmt
    assert stmt_lrs.calls == 0

    # A caller-supplied concurrency header is left alone (no preflight)
    conditional_lrs = FakeEtagLrs('"e"')
    _reconcile_concurrency(conditional_lrs, "PUT", "activities/state", "", {"if-match": '"x"'})
    assert conditional_lrs.calls == 0


def test_cmi5_full_launch_loop(tmp_path: Path, database_url: str) -> None:

    # The cmi5 module needs the optional `cmi5` extra (httpx); skip without it
    pytest.importorskip("httpx")

    import json
    from datetime import UTC, datetime, timedelta
    from urllib.parse import parse_qs, urlparse

    from sqlalchemy import update

    from microlms.lrs import get_lrs_client
    from microlms.modules.cmi5.models import Cmi5Session

    admin_token = seed_admin(database_url)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    hooks = RecordingHooks()
    fake_lrs = FakeLrs()
    app = create_app(
        settings=Settings(database_url=database_url, registration_org_slug=DEFAULT_ORG_SLUG),
        engine=create_db_engine(database_url, echo=False),
        auth=AnonymousAuthBackend(),
        storage=FilesystemStorage(tmp_path / "storage"),
        hooks=hooks,
        routers=ALL_CORE_ROUTERS,
        modules=["cmi5"],
        schedules=[],
        access_policy=DEFAULT_ACCESS_POLICY,
    )
    app.dependency_overrides[get_lrs_client] = lambda: fake_lrs

    with TestClient(app) as client:
        assert client.get("/api/v1/cmi5/status").json()["status"] == "ready"

        # Import attaches the package to a course as a new activity variant
        course_id = client.post(
            "/api/v1/courses",
            headers=admin_headers,
            json={"owner_org_id": None, "slug": "geology", "title": "Geology", "description": ""},
        ).json()["id"]
        imported = client.post(
            "/api/v1/cmi5/import",
            headers=admin_headers,
            files={"file": ("pkg.zip", cmi5_zip(), "application/zip")},
            data={"course_id": str(course_id), "lang": "en"},
        )
        assert imported.status_code == 201
        assert imported.json()["course_id"] == course_id
        assert imported.json()["lang"] == "en"
        au = imported.json()["aus"][0]
        assert au["publisher_id"] == "https://example.com/course/geology/au1"
        assert au["launch_method"] == "OwnWindow"
        # The variant's AUs are listed for launchable rendering
        aus_listed = client.get(
            f"/api/v1/cmi5/variants/{imported.json()['variant_id']}/aus", headers=admin_headers
        )
        assert [a["id"] for a in aus_listed.json()] == [au["id"]]
        # The course stays a draft until published for learners
        client.post(f"/api/v1/courses/{course_id}/publish", headers=admin_headers)

        # A learner registers but is not yet enrolled, so launch is forbidden
        learner = client.post("/auth/register").json()
        learner_headers = {"Authorization": f"Bearer {learner['token']}"}
        assert (
            client.post(f"/api/v1/cmi5/aus/{au['id']}/launch", headers=learner_headers).status_code
            == 403
        )

        # Enroll, then launch: the URL carries all five cmi5 params and the LMS
        # writes LaunchData + a launched statement
        enrollment = client.post(
            "/api/v1/enrollments",
            headers=admin_headers,
            json={"user_id": learner["user_id"], "course_id": course_id},
        ).json()
        launch = client.post(f"/api/v1/cmi5/aus/{au['id']}/launch", headers=learner_headers)
        assert launch.status_code == 200
        query = parse_qs(urlparse(launch.json()["launch_url"]).query)
        assert query["activityId"] == [au["publisher_id"]]
        assert set(query) >= {"endpoint", "fetch", "actor", "activityId", "registration"}
        # The cmi5 registration is the enrollment's own
        assert query["registration"] == [enrollment["registration"]]
        assert any(state_id == "LMS.LaunchData" for _, state_id, _ in fake_lrs.states)
        assert "http://adlnet.gov/expapi/verbs/launched" in verb_ids(fake_lrs.statements)

        session_id = urlparse(query["endpoint"][0]).path.split("/")[-2]

        # The launch URL serves the stored content under the session id, never
        # by AU id
        launch_url = launch.json()["launch_url"]
        assert urlparse(launch_url).path == f"/api/v1/cmi5/content/{session_id}/index.html"
        content = client.get(launch_url)
        assert content.status_code == 200
        assert "GEOLOGY-AU-MARKER" in content.text
        assert content.headers["accept-ranges"] == "bytes"
        partial = client.get(launch_url, headers={"Range": "bytes=0-9"})
        assert partial.status_code == 206
        assert partial.content == content.content[:10]
        assert client.get(f"/api/v1/cmi5/content/{au['id']}/index.html").status_code == 422
        assert client.get(f"/api/v1/cmi5/content/{uuid.uuid4()}/index.html").status_code == 404

        # Relaunching reuses the registration (so the AU resumes) but is a new
        # session with its own endpoint/token
        relaunch = client.post(f"/api/v1/cmi5/aus/{au['id']}/launch", headers=learner_headers)
        relaunch_query = parse_qs(urlparse(relaunch.json()["launch_url"]).query)
        assert relaunch_query["registration"] == query["registration"]
        assert urlparse(relaunch_query["endpoint"][0]).path != urlparse(query["endpoint"][0]).path

        # Fetch yields the token once; a second call reports it is spent
        fetched = client.post(f"/api/v1/cmi5/sessions/{session_id}/fetch")
        auth_token = fetched.json()["auth-token"]
        assert client.post(f"/api/v1/cmi5/sessions/{session_id}/fetch").json()["error-code"] == "1"

        xapi = f"/api/v1/cmi5/sessions/{session_id}/xapi/statements"
        basic = {"Authorization": f"Basic {auth_token}"}

        # A wrong session token is rejected by the proxy
        assert (
            client.post(xapi, headers={"Authorization": "Basic wrong"}, json=[]).status_code == 401
        )

        # The proxy confines the session to its own learner: only the xAPI
        # resources an AU needs, statements written as the launch actor, and
        # reads pinned to the session's agent and registration
        actor = json.loads(query["actor"][0])
        session_xapi = f"/api/v1/cmi5/sessions/{session_id}/xapi"
        assert client.get(f"{session_xapi}/admin/env", headers=basic).status_code == 404
        assert client.get(f"{session_xapi}/..%2Fadmin%2Fenv", headers=basic).status_code == 404
        assert client.delete(xapi, headers=basic).status_code == 405
        other = {"objectType": "Agent", "account": {**actor["account"], "name": str(uuid.uuid4())}}
        forged = {
            "actor": other,
            "verb": {"id": "http://adlnet.gov/expapi/verbs/completed"},
            "object": {"id": au["publisher_id"]},
        }
        assert client.post(xapi, headers=basic, json=forged).status_code == 403
        assert (
            client.post(
                xapi,
                headers=basic,
                json={
                    **forged,
                    "actor": actor,
                    "verb": {"id": "http://adlnet.gov/expapi/verbs/voided"},
                },
            ).status_code
            == 403
        )
        assert client.get(f"{xapi}?statementId={uuid.uuid4()}", headers=basic).status_code == 400
        widened = client.get(
            xapi,
            headers=basic,
            params={"agent": json.dumps(other), "registration": str(uuid.uuid4()), "verb": "v"},
        )
        assert widened.status_code == 200
        assert parse_qs(fake_lrs.forwarded_queries[-1]) == {
            "verb": ["v"],
            "agent": [json.dumps(actor)],
            "registration": query["registration"],
        }
        assert ("POST", "statements") not in fake_lrs.forwarded

        # The cmi5 progress result extension (integer 0-100, on any AU
        # statement) becomes the activity's progress and the course progress
        progressed = {
            "actor": actor,
            "verb": {"id": "http://adlnet.gov/expapi/verbs/progressed"},
            "object": {"id": au["publisher_id"]},
            "result": {"extensions": {"https://w3id.org/xapi/cmi5/result/extensions/progress": 40}},
        }
        assert client.post(xapi, headers=basic, json=progressed).status_code == 200
        activities = client.get(
            f"/api/v1/courses/{course_id}/activities", headers=learner_headers
        ).json()
        assert [a["progress_measure"] for a in activities] == [0.4]
        own = client.get("/api/v1/enrollments/me", headers=learner_headers).json()
        assert [e["progress"] for e in own] == [0.4]

        # The AU reports passed + completed; moveOn=CompletedOrPassed is satisfied,
        # so the LMS emits satisfied and completes the enrollment
        statements = [
            {
                "actor": actor,
                "verb": {"id": "http://adlnet.gov/expapi/verbs/passed"},
                "object": {"id": au["publisher_id"]},
                "result": {"score": {"scaled": 0.9}, "success": True},
            },
            {
                "actor": actor,
                "verb": {"id": "http://adlnet.gov/expapi/verbs/completed"},
                "object": {"id": au["publisher_id"]},
            },
        ]
        posted = client.post(xapi, headers=basic, json=statements)
        assert posted.status_code == 200
        assert ("POST", "statements") in fake_lrs.forwarded
        assert "https://w3id.org/xapi/adl/verbs/satisfied" in verb_ids(fake_lrs.statements)
        own = client.get("/api/v1/enrollments/me", headers=learner_headers).json()
        assert [e["progress"] for e in own] == [1.0]

        # Once the AU has terminated, its session token stops working
        terminated = {
            "actor": actor,
            "verb": {"id": "http://adlnet.gov/expapi/verbs/terminated"},
            "object": {"id": au["publisher_id"]},
        }
        assert client.post(xapi, headers=basic, json=terminated).status_code == 200
        assert client.get(xapi, headers=basic).status_code == 401
        assert client.get(launch_url).status_code == 401

        # The relaunched session stops working once Settings.launch_max_age
        # has passed: its fetch URL is spent and its content refused
        relaunch_session_id = urlparse(relaunch_query["endpoint"][0]).path.split("/")[-2]
        engine = create_db_engine(database_url, echo=False)
        with create_session_factory(engine)() as db_session:
            db_session.execute(
                update(Cmi5Session)
                .where(Cmi5Session.id == uuid.UUID(relaunch_session_id))
                .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
            )
            db_session.commit()
        engine.dispose()
        expired_fetch = client.post(f"/api/v1/cmi5/sessions/{relaunch_session_id}/fetch")
        assert expired_fetch.json()["error-code"] == "1"
        assert client.get(relaunch.json()["launch_url"]).status_code == 401

    assert len(hooks.completed) == 1
    assert hooks.completed[0].course_id == course_id


def test_cmi5_catapult_example_package_import(tmp_path: Path, database_url: str) -> None:
    """Import a real-world cmi5 package.

    The fixture is ADL's "Introduction to Geology - Multi AU at Root" example
    package (multi_au_framed) from the CATAPULT project, vendored from
    https://github.com/adlnet/CATAPULT/tree/main/course_examples with its
    video/ and img/ media removed to keep the fixture small - cmi5.xml only
    references the AU launch URLs, so the trim leaves the manifest intact.
    Unlike the synthetic CMI5_XML above, it exercises multiple AUs sharing
    one launch page through query-string parameters and nested content
    folders.
    """

    # The cmi5 module needs the optional `cmi5` extra (httpx); skip without it
    pytest.importorskip("httpx")

    from urllib.parse import urlparse

    from microlms.lrs import get_lrs_client

    admin_token = seed_admin(database_url)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    app = create_app(
        settings=Settings(database_url=database_url, registration_org_slug=DEFAULT_ORG_SLUG),
        engine=create_db_engine(database_url, echo=False),
        auth=AnonymousAuthBackend(),
        storage=FilesystemStorage(tmp_path / "storage"),
        hooks=NullEventHooks(),
        routers=ALL_CORE_ROUTERS,
        modules=["cmi5"],
        schedules=[],
        access_policy=DEFAULT_ACCESS_POLICY,
    )
    app.dependency_overrides[get_lrs_client] = lambda: FakeLrs()

    with TestClient(app) as client:
        course_id = client.post(
            "/api/v1/courses",
            headers=admin_headers,
            json={
                "owner_org_id": None,
                "slug": "geology-real",
                "title": "Introduction to Geology",
                "description": "",
            },
        ).json()["id"]

        imported = client.post(
            "/api/v1/cmi5/import",
            headers=admin_headers,
            files={
                "file": (
                    "geology_cmi5.zip",
                    (FIXTURES_DIR / "geology_cmi5.zip").read_bytes(),
                    "application/zip",
                )
            },
            data={"course_id": str(course_id), "lang": "en"},
        )
        assert imported.status_code == 201, imported.text
        assert imported.json()["lang"] == "en"
        aus = imported.json()["aus"]
        assert len(aus) == 8
        first = aus[0]
        assert first["publisher_id"] == (
            "https://w3id.org/xapi/cmi5/catapult/lts/course/geology-intro-multi-au-framed/1"
        )
        assert first["title"] == "Introduction to Geology"
        # The manifest sets no launchMethod, so the spec default applies
        assert first["launch_method"] == "AnyWindow"

        # All AUs share one stored launch page; nested assets are served with
        # content types guessed from the file names
        client.post(f"/api/v1/courses/{course_id}/publish", headers=admin_headers)
        learner = client.post("/auth/register").json()
        learner_headers = {"Authorization": f"Bearer {learner['token']}"}
        client.post(
            "/api/v1/enrollments",
            headers=admin_headers,
            json={"user_id": learner["user_id"], "course_id": course_id},
        )
        launch_urls = [
            client.post(f"/api/v1/cmi5/aus/{au['id']}/launch", headers=learner_headers).json()[
                "launch_url"
            ]
            for au in aus
        ]
        for launch_url in launch_urls:
            assert client.get(launch_url).status_code == 200
        served = urlparse(launch_urls[0]).path.rsplit("/", 1)[0]
        script = client.get(f"{served}/js/course_cmi5.js")
        assert script.status_code == 200
        assert script.headers["content-type"].startswith("text/javascript")
        style = client.get(f"{served}/style/framed.css")
        assert style.status_code == 200
        assert style.headers["content-type"].startswith("text/css")
