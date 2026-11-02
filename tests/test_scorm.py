"""scorm module: SCORM 2004 and 1.2 launch loops and example package import."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from microlms import DEFAULT_ACCESS_POLICY, Settings, create_app, create_db_engine
from microlms.auth import AnonymousAuthBackend
from microlms.events import NullEventHooks, ScormRuntimeCommitted
from microlms.storage import FilesystemStorage
from tests.helpers import (
    ALL_CORE_ROUTERS,
    DEFAULT_ORG_SLUG,
    FIXTURES_DIR,
    SCORM_MANIFEST,
    FakeLrs,
    RecordingHooks,
    scorm_zip,
    seed_admin,
    verb_ids,
)

_SCORM_12_MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<manifest identifier="com.example.golf12" version="1"
          xmlns="http://www.imsproject.org/xsd/imscp_rootv1p1p2"
          xmlns:adlcp="http://www.adlnet.org/xsd/adlcp_rootv1p2">
  <metadata>
    <schema>ADL SCORM</schema>
    <schemaversion>1.2</schemaversion>
  </metadata>
  <organizations default="org1">
    <organization identifier="org1">
      <title>Golf Explained</title>
      <item identifier="item_1" identifierref="resource_1">
        <title>Playing the Game</title>
      </item>
    </organization>
  </organizations>
  <resources>
    <resource identifier="resource_1" type="webcontent" adlcp:scormtype="sco"
              href="index.html">
      <file href="index.html"/>
    </resource>
  </resources>
</manifest>
"""


def test_scorm_full_launch_loop(tmp_path: Path, database_url: str) -> None:

    # The scorm module needs the optional `scorm` extra (httpx); skip without it
    pytest.importorskip("httpx")

    from microlms.lrs import get_lrs_client

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
        modules=["scorm"],
        schedules=[],
        access_policy=DEFAULT_ACCESS_POLICY,
    )
    app.dependency_overrides[get_lrs_client] = lambda: fake_lrs

    with TestClient(app) as client:
        assert client.get("/api/v1/scorm/status").json()["status"] == "ready"

        course_id = client.post(
            "/api/v1/courses",
            headers=admin_headers,
            json={
                "owner_org_id": None,
                "slug": "golf",
                "title": "Golf Explained",
                "description": "",
            },
        ).json()["id"]

        # Packages declaring an unsupported SCORM edition are rejected
        rejected = client.post(
            "/api/v1/scorm/import",
            headers=admin_headers,
            files={
                "file": (
                    "pkg10.zip",
                    scorm_zip(SCORM_MANIFEST.replace("2004 3rd Edition", "1.0")),
                    "application/zip",
                )
            },
            data={"course_id": str(course_id), "lang": "en"},
        )
        assert rejected.status_code == 400
        assert "Unsupported SCORM version" in rejected.json()["detail"]

        # Import attaches the package to the course as a new activity variant
        # (one SCO) and stores the package files
        imported = client.post(
            "/api/v1/scorm/import",
            headers=admin_headers,
            files={"file": ("pkg.zip", scorm_zip(SCORM_MANIFEST), "application/zip")},
            data={"course_id": str(course_id), "lang": "en"},
        )
        assert imported.status_code == 201
        assert imported.json()["course_id"] == course_id
        assert imported.json()["version"] == "2004"
        variant_id = imported.json()["variant_id"]
        sco = imported.json()["scos"][0]
        assert sco["identifier"] == "item_1"
        assert sco["scorm_type"] == "sco"
        assert sco["title"] == "Playing the Game"
        # The course stays a draft until published for learners
        client.post(f"/api/v1/courses/{course_id}/publish", headers=admin_headers)

        # A learner not enrolled in the course cannot launch
        learner = client.post("/auth/register").json()
        learner_headers = {"Authorization": f"Bearer {learner['token']}"}
        assert (
            client.post(
                f"/api/v1/scorm/scos/{sco['id']}/launch", headers=learner_headers
            ).status_code
            == 403
        )

        # The SCO listing follows course visibility (no enrollment needed)
        listed = client.get(f"/api/v1/scorm/variants/{variant_id}/scos", headers=learner_headers)
        assert listed.status_code == 200
        assert [(s["id"], s["title"]) for s in listed.json()] == [(sco["id"], sco["title"])]
        assert (
            client.get("/api/v1/scorm/variants/999999/scos", headers=learner_headers).status_code
            == 404
        )

        # Enroll, then launch: an attempt is created, `initialized` is emitted
        # and the player URL carries the per-launch token
        enrollment = client.post(
            "/api/v1/enrollments",
            headers=admin_headers,
            json={"user_id": learner["user_id"], "course_id": course_id},
        ).json()
        launch = client.post(f"/api/v1/scorm/scos/{sco['id']}/launch", headers=learner_headers)
        assert launch.status_code == 200
        player_url = launch.json()["player_url"]
        token = player_url.rsplit("/", 1)[-1]
        assert "http://adlnet.gov/expapi/verbs/initialized" in verb_ids(fake_lrs.statements)
        sco_activity_id = next(
            s
            for s in fake_lrs.statements
            if s["verb"]["id"] == "http://adlnet.gov/expapi/verbs/initialized"
        )["object"]["id"]

        # The player page targets the SCORM 2004 runtime build and embeds the
        # launch URL with parameters
        player = client.get(f"/api/v1/scorm/player/{token}")
        assert player.status_code == 200
        assert '"version": "2004"' in player.text
        assert "/api/v1/scorm/player-assets/scorm-again-2004.min.js" in player.text
        assert f"/api/v1/scorm/content/{token}/index.html?lang=en" in player.text
        assert client.get("/api/v1/scorm/player/wrong-token").status_code == 401

        # Stored content is streamed under the per-launch token, never by SCO id
        content_url = f"/api/v1/scorm/content/{token}/index.html"
        content = client.get(content_url)
        assert content.status_code == 200
        assert "GOLF-SCO-MARKER" in content.text
        assert content.headers["accept-ranges"] == "bytes"
        assert content.headers["content-length"] == str(len(content.content))
        assert client.get(f"/api/v1/scorm/content/{token}/missing.mp4").status_code == 404
        assert client.get(f"/api/v1/scorm/content/{sco['id']}/index.html").status_code == 401
        assert client.get("/api/v1/scorm/content/wrong-token/index.html").status_code == 401
        # A path climbing out of the package is refused, even back into a
        # stored package (here its own)
        (package_dir,) = (tmp_path / "storage" / "scorm").iterdir()
        climbing = f"/api/v1/scorm/content/{token}/..%2F{package_dir.name}%2Findex.html"
        assert client.get(climbing).status_code == 404

        # Range requests get 206 partial responses (video seeking relies on this)
        size = len(content.content)
        partial = client.get(content_url, headers={"Range": "bytes=3-7"})
        assert partial.status_code == 206
        assert partial.content == content.content[3:8]
        assert partial.headers["content-range"] == f"bytes 3-7/{size}"
        assert partial.headers["content-length"] == "5"

        # Open-ended and suffix ranges resolve against the object size
        tail = client.get(content_url, headers={"Range": "bytes=5-"})
        assert tail.status_code == 206
        assert tail.content == content.content[5:]
        suffix = client.get(content_url, headers={"Range": "bytes=-4"})
        assert suffix.status_code == 206
        assert suffix.content == content.content[-4:]

        # A range past the end is unsatisfiable; a malformed one is ignored
        beyond = client.get(content_url, headers={"Range": f"bytes={size}-"})
        assert beyond.status_code == 416
        assert beyond.headers["content-range"] == f"bytes */{size}"
        ignored = client.get(content_url, headers={"Range": "bytes=7-3"})
        assert ignored.status_code == 200
        assert ignored.content == content.content

        # First entry is ab-initio with an untouched data model
        state = client.get(f"/api/v1/scorm/runtime/{token}").json()
        assert state["entry"] == "ab-initio"
        assert state["completion_status"] == "unknown"
        assert state["learner_id"] == str(learner["user_id"])
        assert state["total_time"] == "PT0H0M0S"

        # A mid-session commit that is not completed does not complete the
        # course, but its cmi.progress_measure becomes the activity's progress
        # and the course progress
        incomplete = {
            "completion_status": "incomplete",
            "success_status": "unknown",
            "score_scaled": None,
            "score_raw": None,
            "progress_measure": 0.4,
            "location": "page-2",
            "suspend_data": "half-way",
            "interactions": [],
        }
        assert (
            client.post(f"/api/v1/scorm/runtime/{token}/commit", json=incomplete).status_code == 204
        )
        assert len(hooks.completed) == 0
        activities = client.get(
            f"/api/v1/courses/{course_id}/activities", headers=learner_headers
        ).json()
        assert [a["progress_measure"] for a in activities] == [0.4]
        own = client.get("/api/v1/enrollments/me", headers=learner_headers).json()
        assert [e["progress"] for e in own] == [0.4]

        # Every recorded commit is announced to the client hooks with the
        # SCO's suspend_data, which the core stores but does not interpret
        committed = hooks.scorm_commits[-1]
        assert committed.package_storage_prefix.startswith(f"scorm/{variant_id}-")
        assert committed == ScormRuntimeCommitted(
            enrollment_id=enrollment["id"],
            activity_id=activities[0]["id"],
            sco_id=sco["id"],
            suspend_data="half-way",
            package_storage_prefix=committed.package_storage_prefix,
        )

        # A progress measure outside 0..1 is refused
        assert (
            client.post(
                f"/api/v1/scorm/runtime/{token}/commit",
                json={**incomplete, "progress_measure": 1.5},
            ).status_code
            == 422
        )

        # Quiz answers: each new cmi.interactions entry becomes one `responded`
        # statement; recommitting the same journal reports nothing again
        def responded() -> list[dict]:
            return [
                s
                for s in fake_lrs.statements
                if s["verb"]["id"] == "http://adlnet.gov/expapi/verbs/responded"
            ]

        question_1 = {
            "id": "q1 choice",
            "type": "choice",
            "learner_response": "b",
            "result": "correct",
            "correct_responses": ["b"],
            "description": "Which answer is right?",
            "latency": "PT4S",
        }
        question_2 = {
            "id": "q2",
            "type": "fill-in",
            "learner_response": "wrong answer",
            "result": "incorrect",
            "correct_responses": ["right answer"],
            "description": None,
            "latency": None,
        }
        with_answers = {**incomplete, "interactions": [question_1, question_2]}
        assert (
            client.post(f"/api/v1/scorm/runtime/{token}/commit", json=with_answers).status_code
            == 204
        )
        assert len(responded()) == 2
        first = responded()[0]
        assert first["object"]["id"] == f"{sco_activity_id}/interaction/q1%20choice"
        assert first["object"]["definition"] == {
            "type": "http://adlnet.gov/expapi/activities/cmi.interaction",
            "description": {"en-US": "Which answer is right?"},
            "interactionType": "choice",
            "correctResponsesPattern": ["b"],
        }
        assert first["result"] == {"response": "b", "success": True, "duration": "PT4S"}
        assert first["context"]["contextActivities"]["parent"] == [{"id": sco_activity_id}]
        assert responded()[1]["result"] == {"response": "wrong answer", "success": False}

        assert (
            client.post(f"/api/v1/scorm/runtime/{token}/commit", json=with_answers).status_code
            == 204
        )
        assert len(responded()) == 2

        # A grown journal only reports the appended entry
        question_3 = {**question_2, "id": "q3", "result": "neutral"}
        third = {**incomplete, "interactions": [question_1, question_2, question_3]}
        assert client.post(f"/api/v1/scorm/runtime/{token}/commit", json=third).status_code == 204
        assert len(responded()) == 3
        assert "success" not in responded()[2].get("result", {})

        # Completing and passing the SCO satisfies it: completed + passed go to
        # the LRS and the enrollment completes (CourseCompleted via hooks)
        completed = {
            "completion_status": "completed",
            "success_status": "passed",
            "score_scaled": 0.9,
            "score_raw": 90.0,
            "progress_measure": 1.0,
            "location": "end",
            "suspend_data": "done",
            "interactions": [],
        }
        assert (
            client.post(f"/api/v1/scorm/runtime/{token}/commit", json=completed).status_code == 204
        )
        verbs = verb_ids(fake_lrs.statements)
        assert "http://adlnet.gov/expapi/verbs/completed" in verbs
        assert "http://adlnet.gov/expapi/verbs/passed" in verbs
        assert len(hooks.completed) == 1
        assert hooks.completed[0].course_id == course_id
        own = client.get("/api/v1/enrollments/me", headers=learner_headers).json()
        assert [e["progress"] for e in own] == [1.0]

        # Terminate reports the session time and suspends the attempt
        assert (
            client.post(
                f"/api/v1/scorm/runtime/{token}/terminate",
                json={**completed, "session_time": "PT1M30S", "exit_mode": "suspend"},
            ).status_code
            == 204
        )
        assert "http://adlnet.gov/expapi/verbs/terminated" in verb_ids(fake_lrs.statements)

        # Every statement carries the enrollment's registration; the terminated
        # one reports the session time as result.duration
        terminated = next(
            s
            for s in fake_lrs.statements
            if s["verb"]["id"] == "http://adlnet.gov/expapi/verbs/terminated"
        )
        assert terminated["context"]["registration"] == enrollment["registration"]
        assert terminated["result"] == {"duration": "PT0H1M30S"}
        assert {s["context"]["registration"] for s in fake_lrs.statements} == {
            enrollment["registration"]
        }

        # Terminate ends the session: the token no longer reads the state,
        # accepts reports or serves the content
        assert client.get(f"/api/v1/scorm/runtime/{token}").status_code == 401
        late = client.post(f"/api/v1/scorm/runtime/{token}/commit", json=incomplete)
        assert late.status_code == 401
        assert client.get(content_url).status_code == 401

        # Relaunching rotates the token and resumes the persisted attempt
        relaunch = client.post(f"/api/v1/scorm/scos/{sco['id']}/launch", headers=learner_headers)
        new_token = relaunch.json()["player_url"].rsplit("/", 1)[-1]
        assert new_token != token
        resumed = client.get(f"/api/v1/scorm/runtime/{new_token}").json()
        assert resumed["entry"] == "resume"
        assert resumed["suspend_data"] == "done"
        assert resumed["progress_measure"] == 1.0
        assert resumed["total_time"] == "PT0H1M30S"

        # The interactions cursor resets per launch: the new session's journal
        # starts at index 0 and its entries are reported again
        retry = {**incomplete, "interactions": [question_1]}
        assert (
            client.post(f"/api/v1/scorm/runtime/{new_token}/commit", json=retry).status_code == 204
        )
        assert len(responded()) == 4

    # Completion fired exactly once even though commits kept coming after
    assert len(hooks.completed) == 1


def test_scorm12_full_launch_loop(tmp_path: Path, database_url: str) -> None:

    # The scorm module needs the optional `scorm` extra (httpx); skip without it
    pytest.importorskip("httpx")

    from microlms.lrs import get_lrs_client

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
        modules=["scorm"],
        schedules=[],
        access_policy=DEFAULT_ACCESS_POLICY,
    )
    app.dependency_overrides[get_lrs_client] = lambda: fake_lrs

    with TestClient(app) as client:
        course_id = client.post(
            "/api/v1/courses",
            headers=admin_headers,
            json={
                "owner_org_id": None,
                "slug": "golf12",
                "title": "Golf Explained",
                "description": "",
            },
        ).json()["id"]

        # Import detects SCORM 1.2 from the manifest (imscp_rootv1p1p2 content
        # packaging namespace, lowercase scormtype attribute) and reports it
        imported = client.post(
            "/api/v1/scorm/import",
            headers=admin_headers,
            files={"file": ("pkg12.zip", scorm_zip(_SCORM_12_MANIFEST), "application/zip")},
            data={"course_id": str(course_id), "lang": "en"},
        )
        assert imported.status_code == 201
        assert imported.json()["version"] == "1.2"
        sco = imported.json()["scos"][0]
        assert sco["scorm_type"] == "sco"
        assert sco["title"] == "Playing the Game"
        client.post(f"/api/v1/courses/{course_id}/publish", headers=admin_headers)

        learner = client.post("/auth/register").json()
        learner_headers = {"Authorization": f"Bearer {learner['token']}"}
        client.post(
            "/api/v1/enrollments",
            headers=admin_headers,
            json={"user_id": learner["user_id"], "course_id": course_id},
        )
        launch = client.post(f"/api/v1/scorm/scos/{sco['id']}/launch", headers=learner_headers)
        assert launch.status_code == 200
        token = launch.json()["player_url"].rsplit("/", 1)[-1]
        assert "http://adlnet.gov/expapi/verbs/initialized" in verb_ids(fake_lrs.statements)

        # The player page targets the SCORM 1.2 runtime build (window.API)
        player = client.get(f"/api/v1/scorm/player/{token}")
        assert player.status_code == 200
        assert '"version": "1.2"' in player.text
        assert "/api/v1/scorm/player-assets/scorm-again-12.min.js" in player.text

        # Both runtime builds are served; unknown asset names are refused
        asset = client.get("/api/v1/scorm/player-assets/scorm-again-12.min.js")
        assert asset.status_code == 200
        assert "Scorm12API" in asset.text
        assert client.get("/api/v1/scorm/player-assets/missing.js").status_code == 404

        # The runtime endpoints are edition-neutral: the player normalizes the
        # cmi.core data model into the same payload before reporting
        completed = {
            "completion_status": "completed",
            "success_status": "passed",
            "score_scaled": 0.85,
            "score_raw": 85.0,
            "progress_measure": None,
            "location": "end",
            "suspend_data": None,
            "interactions": [],
        }
        assert (
            client.post(f"/api/v1/scorm/runtime/{token}/commit", json=completed).status_code == 204
        )
        verbs = verb_ids(fake_lrs.statements)
        assert "http://adlnet.gov/expapi/verbs/completed" in verbs
        assert "http://adlnet.gov/expapi/verbs/passed" in verbs

    assert len(hooks.completed) == 1
    assert hooks.completed[0].course_id == course_id


def test_scorm_golf_example_packages_import(tmp_path: Path, database_url: str) -> None:
    """Import real-world SCORM packages, one per supported edition.

    The fixtures are Rustici Software's "Golf Explained - Run-time Basic
    Calls" example packages (SCORM 1.2 and SCORM 2004 3rd Edition), vendored
    from https://scorm.com/scorm-explained/technical-scorm/golf-examples/.
    Unlike the synthetic manifests above, these exercise a manifest with XSD
    files at the package root, a single SCO launched from a subdirectory
    (shared/launchpage.html) and nested content folders.
    """

    # The scorm module needs the optional `scorm` extra (httpx); skip without it
    pytest.importorskip("httpx")

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
        modules=["scorm"],
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
                "slug": "golf-real",
                "title": "Golf Explained",
                "description": "",
            },
        ).json()["id"]

        # Content is served under a launch, so a learner is enrolled up front
        client.post(f"/api/v1/courses/{course_id}/publish", headers=admin_headers)
        learner = client.post("/auth/register").json()
        learner_headers = {"Authorization": f"Bearer {learner['token']}"}
        client.post(
            "/api/v1/enrollments",
            headers=admin_headers,
            json={"user_id": learner["user_id"], "course_id": course_id},
        )

        for fixture, version in (("golf_scorm12.zip", "1.2"), ("golf_scorm2004.zip", "2004")):
            imported = client.post(
                "/api/v1/scorm/import",
                headers=admin_headers,
                files={"file": (fixture, (FIXTURES_DIR / fixture).read_bytes(), "application/zip")},
                data={"course_id": str(course_id), "lang": "en"},
            )
            assert imported.status_code == 201, imported.text
            assert imported.json()["version"] == version
            scos = imported.json()["scos"]
            assert len(scos) == 1
            sco = scos[0]
            assert sco["identifier"] == "item_1"
            assert sco["scorm_type"] == "sco"
            assert sco["title"] == "Golf Explained"

            # The stored package serves its launch page and nested assets with
            # content types guessed from the file names
            launch = client.post(f"/api/v1/scorm/scos/{sco['id']}/launch", headers=learner_headers)
            content = f"/api/v1/scorm/content/{launch.json()['player_url'].rsplit('/', 1)[-1]}"
            launch_page = client.get(f"{content}/shared/launchpage.html")
            assert launch_page.status_code == 200
            assert "Course Launch Page" in launch_page.text
            lesson = client.get(f"{content}/Etiquette/Course.html")
            assert lesson.status_code == 200
            image = client.get(f"{content}/Etiquette/course.jpg")
            assert image.status_code == 200
            assert image.headers["content-type"] == "image/jpeg"
