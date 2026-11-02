"""h5p module: launch loop and example package import."""

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
)

_H5P_MANIFEST = {
    "title": "Fill in the Blanks",
    "language": "en",
    "mainLibrary": "H5P.Blanks",
    "embedTypes": ["div"],
    # Version components as strings: the spec says numbers, but packages
    # exported by the official framework serialize strings
    "preloadedDependencies": [
        {"machineName": "H5P.Blanks", "majorVersion": "1", "minorVersion": "14"},
    ],
}


def _h5p_zip() -> bytes:
    import io
    import json
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("h5p.json", json.dumps(_H5P_MANIFEST))
        archive.writestr("content/content.json", '{"text": "H5P-CONTENT-MARKER"}')
        archive.writestr(
            "H5P.Blanks-1.14/library.json",
            json.dumps(
                {
                    "title": "Fill in the Blanks",
                    "machineName": "H5P.Blanks",
                    "majorVersion": 1,
                    "minorVersion": 14,
                    "patchVersion": 0,
                    "runnable": 1,
                }
            ),
        )
        archive.writestr("H5P.Blanks-1.14/blanks.js", "// H5P-LIB-MARKER")
    return buffer.getvalue()


def test_h5p_full_launch_loop(tmp_path: Path, database_url: str) -> None:
    import re
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    # The h5p module needs the optional `h5p` extra (httpx); skip without it
    pytest.importorskip("httpx")

    from microlms.lrs import get_lrs_client
    from microlms.modules.h5p.models import H5pAttempt

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
        modules=["h5p"],
        schedules=[],
        access_policy=DEFAULT_ACCESS_POLICY,
    )
    app.dependency_overrides[get_lrs_client] = lambda: fake_lrs

    with TestClient(app) as client:
        assert client.get("/api/v1/h5p/status").json()["status"] == "ready"

        course_id = client.post(
            "/api/v1/courses",
            headers=admin_headers,
            json={"owner_org_id": None, "slug": "blanks", "title": "Blanks", "description": ""},
        ).json()["id"]

        # A zip that is not an H5P package is rejected with a clear error
        rejected = client.post(
            "/api/v1/h5p/import",
            headers=admin_headers,
            files={"file": ("pkg.h5p", cmi5_zip(), "application/zip")},
            data={"course_id": str(course_id), "lang": "en"},
        )
        assert rejected.status_code == 400
        assert "h5p.json" in rejected.json()["detail"]

        # Import attaches the package to a course as a new activity variant
        imported = client.post(
            "/api/v1/h5p/import",
            headers=admin_headers,
            files={"file": ("pkg.h5p", _h5p_zip(), "application/zip")},
            data={"course_id": str(course_id), "lang": "en"},
        )
        assert imported.status_code == 201
        assert imported.json()["course_id"] == course_id
        assert imported.json()["lang"] == "en"
        content = imported.json()["content"]
        assert content["main_library"] == "H5P.Blanks"
        # The variant's content item is readable for launchable rendering
        listed = client.get(
            f"/api/v1/h5p/variants/{imported.json()['variant_id']}/content", headers=admin_headers
        )
        assert listed.json()["id"] == content["id"]
        client.post(f"/api/v1/courses/{course_id}/publish", headers=admin_headers)

        # The vendored player assets are served, nested paths included
        main_js = client.get("/api/v1/h5p/player-assets/main.bundle.js")
        assert main_js.status_code == 200
        assert main_js.headers["content-type"].startswith("text/javascript")
        assert client.get("/api/v1/h5p/player-assets/styles/h5p.css").status_code == 200
        assert client.get("/api/v1/h5p/player-assets/missing.js").status_code == 404

        # A learner registers but is not yet enrolled, so launch is forbidden
        learner = client.post("/auth/register").json()
        learner_headers = {"Authorization": f"Bearer {learner['token']}"}
        assert (
            client.post(
                f"/api/v1/h5p/contents/{content['id']}/launch", headers=learner_headers
            ).status_code
            == 403
        )

        # Enroll, then launch: the player page carries the content and asset
        # URLs plus the canonical object IRI minted at import
        enrollment = client.post(
            "/api/v1/enrollments",
            headers=admin_headers,
            json={"user_id": learner["user_id"], "course_id": course_id},
        ).json()
        launch = client.post(
            f"/api/v1/h5p/contents/{content['id']}/launch", headers=learner_headers
        )
        assert launch.status_code == 200
        player_url = launch.json()["player_url"]
        token = player_url.rsplit("/", 1)[-1]
        page = client.get(f"/api/v1/h5p/player/{token}")
        assert page.status_code == 200
        assert f"/api/v1/h5p/content/{token}" in page.text

        # Stored package files are streamed under the per-launch token, never
        # by content id
        served = client.get(f"/api/v1/h5p/content/{token}/content/content.json")
        assert served.status_code == 200
        assert "H5P-CONTENT-MARKER" in served.text
        assert client.get(f"/api/v1/h5p/content/{content['id']}/h5p.json").status_code == 401
        iri_match = re.search(r"urn:microlms:h5p:[0-9a-f-]+", page.text)
        assert iri_match is not None
        object_iri = iri_match.group(0)

        # Relaunching rotates the token, so the stale player page loses access
        relaunch = client.post(
            f"/api/v1/h5p/contents/{content['id']}/launch", headers=learner_headers
        )
        new_token = relaunch.json()["player_url"].rsplit("/", 1)[-1]
        assert new_token != token
        assert client.get(f"/api/v1/h5p/player/{token}").status_code == 401
        assert client.get(f"/api/v1/h5p/content/{token}/h5p.json").status_code == 401

        statements_url = f"/api/v1/h5p/runtime/{new_token}/statements"
        assert client.post("/api/v1/h5p/runtime/wrong/statements", json=[]).status_code == 401

        # A sub-content statement is forwarded with the canonical actor and
        # the enrollment registration stamped, but does not satisfy anything
        spoofed_actor = {"objectType": "Agent", "account": {"name": "someone-else"}}
        answered = {
            "actor": spoofed_actor,
            "verb": {"id": "http://adlnet.gov/expapi/verbs/answered"},
            "object": {"id": f"{object_iri}?subContentId=aa-bb"},
            "result": {"completion": True, "score": {"scaled": 1}},
        }
        assert client.post(statements_url, json=[answered]).status_code == 204
        forwarded = fake_lrs.statements[-1]
        assert forwarded["actor"]["account"]["name"] == learner["user_id"]
        assert forwarded["context"]["registration"] == enrollment["registration"]
        assert hooks.completed == []

        # The root content reporting completed satisfies the activity, which
        # completes the enrollment (the course has this one activity)
        completed = {
            "actor": spoofed_actor,
            "verb": {"id": "http://adlnet.gov/expapi/verbs/completed"},
            "object": {"id": object_iri},
            "result": {"completion": True, "score": {"scaled": 0.8}},
        }
        assert client.post(statements_url, json=[completed]).status_code == 204
        # Reporting again stays idempotent
        assert client.post(statements_url, json=[completed]).status_code == 204

        # The token stops working once Settings.launch_max_age has passed
        engine = create_db_engine(database_url, echo=False)
        with create_session_factory(engine)() as db_session:
            db_session.execute(
                update(H5pAttempt).values(token_expires_at=datetime.now(UTC) - timedelta(minutes=1))
            )
            db_session.commit()
        engine.dispose()
        assert client.post(statements_url, json=[completed]).status_code == 401
        assert client.get(f"/api/v1/h5p/player/{new_token}").status_code == 401
        assert client.get(f"/api/v1/h5p/content/{new_token}/h5p.json").status_code == 401

    assert len(hooks.completed) == 1
    assert hooks.completed[0].course_id == course_id


def test_h5p_example_packages_import(tmp_path: Path, database_url: str) -> None:
    """Import real-world H5P packages, exported with their libraries bundled.

    The fixtures are the h5p-standalone player's own test package (H5P.TrueFalse
    "Hello World", vendored from https://github.com/tunapanda/h5p-standalone
    test/h5p-test.h5p) and Lumi's H5P.DragText example (vendored from
    https://github.com/Lumieducation/H5P-Nodejs-library
    test/data/example-packages/H5P.DragText.h5p). Unlike the synthetic
    _H5P_MANIFEST above, these exercise real framework exports: library version
    numbers serialized as strings in h5p.json, a full dependency set of bundled
    library directories, and library scripts in nested folders.
    """

    # The h5p module needs the optional `h5p` extra (httpx); skip without it
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
        modules=["h5p"],
        schedules=[],
        access_policy=DEFAULT_ACCESS_POLICY,
    )
    app.dependency_overrides[get_lrs_client] = lambda: FakeLrs()

    fixtures = (
        ("truefalse_h5p.h5p", "H5P.TrueFalse", "Hello World", "H5P.TrueFalse-1.6"),
        ("dragtext_h5p.h5p", "H5P.DragText", "Drag the Words", "H5P.DragText-1.8"),
    )
    with TestClient(app) as client:
        course_id = client.post(
            "/api/v1/courses",
            headers=admin_headers,
            json={
                "owner_org_id": None,
                "slug": "h5p-real",
                "title": "H5P Examples",
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

        for fixture, main_library, title, library_dir in fixtures:
            imported = client.post(
                "/api/v1/h5p/import",
                headers=admin_headers,
                files={"file": (fixture, (FIXTURES_DIR / fixture).read_bytes(), "application/zip")},
                data={"course_id": str(course_id), "lang": "en"},
            )
            assert imported.status_code == 201, imported.text
            content = imported.json()["content"]
            assert content["main_library"] == main_library
            assert content["title"] == title

            # The stored package serves the player's entry points and the
            # bundled library files with content types guessed from the names
            launch = client.post(
                f"/api/v1/h5p/contents/{content['id']}/launch", headers=learner_headers
            )
            served = f"/api/v1/h5p/content/{launch.json()['player_url'].rsplit('/', 1)[-1]}"
            manifest = client.get(f"{served}/h5p.json")
            assert manifest.status_code == 200
            assert manifest.json()["mainLibrary"] == main_library
            params = client.get(f"{served}/content/content.json")
            assert params.status_code == 200
            library = client.get(f"{served}/{library_dir}/library.json")
            assert library.status_code == 200
            assert library.json()["machineName"] == main_library
            assert any(item["path"].endswith(".js") for item in library.json()["preloadedJs"])
            script_path = library.json()["preloadedJs"][0]["path"]
            script = client.get(f"{served}/{library_dir}/{script_path}")
            assert script.status_code == 200
            assert script.headers["content-type"].startswith("text/javascript")
