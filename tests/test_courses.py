"""Course lifecycle, activities, language variants, prerequisites and sequencing."""

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from microlms import DEFAULT_ACCESS_POLICY, Settings, create_app, create_db_engine
from microlms.auth import AnonymousAuthBackend
from microlms.events import NullEventHooks
from microlms.storage import FilesystemStorage
from tests.helpers import (
    ALL_CORE_ROUTERS,
    DEFAULT_ORG_SLUG,
    SCORM_MANIFEST,
    FakeLrs,
    RecordingHooks,
    scorm_zip,
    seed_admin,
)


def test_course_lifecycle(tmp_path: Path, database_url: str) -> None:
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
        learner = client.post("/auth/register").json()
        learner_headers = {"Authorization": f"Bearer {learner['token']}"}

        # A new course is a draft: admins see it, learners do not
        course = client.post(
            "/api/v1/courses",
            headers=admin_headers,
            json={"owner_org_id": None, "slug": "geo", "title": "Geology", "description": ""},
        ).json()
        assert course["status"] == "draft"
        assert client.get("/api/v1/courses", headers=learner_headers).json() == []
        assert (
            client.get(f"/api/v1/courses/{course['id']}", headers=learner_headers).status_code
            == 403
        )
        assert [c["id"] for c in client.get("/api/v1/courses", headers=admin_headers).json()] == [
            course["id"]
        ]
        assert (
            client.post(
                "/api/v1/enrollments",
                headers=admin_headers,
                json={"user_id": learner["user_id"], "course_id": course["id"]},
            ).status_code
            == 409
        )

        # Publishing makes it visible and enrollable
        assert (
            client.post(f"/api/v1/courses/{course['id']}/publish", headers=admin_headers).json()[
                "status"
            ]
            == "published"
        )
        assert [c["id"] for c in client.get("/api/v1/courses", headers=learner_headers).json()] == [
            course["id"]
        ]
        assert (
            client.post(
                "/api/v1/enrollments",
                headers=admin_headers,
                json={"user_id": learner["user_id"], "course_id": course["id"]},
            ).status_code
            == 201
        )

        # Editing replaces the fields without touching the status; slugs stay unique
        other = client.post(
            "/api/v1/courses",
            headers=admin_headers,
            json={"owner_org_id": None, "slug": "bio", "title": "Biology", "description": ""},
        ).json()
        edited = client.put(
            f"/api/v1/courses/{course['id']}",
            headers=admin_headers,
            json={"slug": "geo-2", "title": "Geology II", "description": "Rocks"},
        )
        assert edited.status_code == 200
        assert edited.json()["slug"] == "geo-2"
        assert edited.json()["status"] == "published"
        assert (
            client.put(
                f"/api/v1/courses/{other['id']}",
                headers=admin_headers,
                json={"slug": "geo-2", "title": "Biology", "description": ""},
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/v1/courses",
                headers=admin_headers,
                json={
                    "owner_org_id": None,
                    "slug": "geo-2",
                    "title": "Duplicate",
                    "description": "",
                },
            ).status_code
            == 409
        )

        # Archiving hides the course again while the enrollment survives
        assert (
            client.post(f"/api/v1/courses/{course['id']}/archive", headers=admin_headers).json()[
                "status"
            ]
            == "archived"
        )
        assert client.get("/api/v1/courses", headers=learner_headers).json() == []
        assert (
            client.get(f"/api/v1/courses/{course['id']}", headers=learner_headers).status_code
            == 403
        )
        own = client.get("/api/v1/enrollments/me", headers=learner_headers).json()
        assert [e["course_id"] for e in own] == [course["id"]]

        # Re-publishing an archived course brings it back
        assert (
            client.post(f"/api/v1/courses/{course['id']}/publish", headers=admin_headers).json()[
                "status"
            ]
            == "published"
        )
        assert [c["id"] for c in client.get("/api/v1/courses", headers=learner_headers).json()] == [
            course["id"]
        ]


def test_multi_activity_course_and_language_variants(tmp_path: Path, database_url: str) -> None:

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

    def import_variant(client: TestClient, course_id: int, lang: str, activity_id: int | None):
        data = {"course_id": str(course_id), "lang": lang}
        if activity_id is not None:
            data["activity_id"] = str(activity_id)
        return client.post(
            "/api/v1/scorm/import",
            headers=admin_headers,
            files={"file": ("pkg.zip", scorm_zip(SCORM_MANIFEST), "application/zip")},
            data=data,
        )

    def complete_sco(client: TestClient, sco_id: int, headers: dict[str, str]) -> None:
        launch = client.post(f"/api/v1/scorm/scos/{sco_id}/launch", headers=headers)
        assert launch.status_code == 200
        token = launch.json()["player_url"].rsplit("/", 1)[-1]
        payload = {
            "completion_status": "completed",
            "success_status": "passed",
            "score_scaled": 1.0,
            "score_raw": 100.0,
            "progress_measure": None,
            "location": None,
            "suspend_data": None,
            "interactions": [],
        }
        commit = client.post(f"/api/v1/scorm/runtime/{token}/commit", json=payload)
        assert commit.status_code == 204

    with TestClient(app) as client:
        course_id = client.post(
            "/api/v1/courses",
            headers=admin_headers,
            json={
                "owner_org_id": None,
                "slug": "safety",
                "title": "Safety Training",
                "description": "",
            },
        ).json()["id"]

        # Importing without an activity target appends a new activity; a second
        # import into that activity adds a language variant
        first = import_variant(client, course_id, "en", None).json()
        activity_1 = first["activity_id"]
        fr = import_variant(client, course_id, "fr", activity_1)
        assert fr.status_code == 201
        assert fr.json()["activity_id"] == activity_1

        # A taken language conflicts; malformed tags and unknown targets are refused
        assert import_variant(client, course_id, "fr", activity_1).status_code == 409
        assert import_variant(client, course_id, "not a lang", None).status_code == 400
        assert import_variant(client, 999999, "en", None).status_code == 404
        assert import_variant(client, course_id, "en", 999999).status_code == 404

        # A second activity of the same course, own package
        second = import_variant(client, course_id, "en", None).json()
        activity_2 = second["activity_id"]
        assert activity_2 != activity_1

        client.post(f"/api/v1/courses/{course_id}/publish", headers=admin_headers)

        learner = client.post("/auth/register").json()
        learner_headers = {"Authorization": f"Bearer {learner['token']}"}
        client.post(
            "/api/v1/enrollments",
            headers=admin_headers,
            json={"user_id": learner["user_id"], "course_id": course_id},
        )

        # The course lists its activities in order with their language variants
        activities = client.get(
            f"/api/v1/courses/{course_id}/activities", headers=learner_headers
        ).json()
        assert [a["id"] for a in activities] == [activity_1, activity_2]
        assert [a["position"] for a in activities] == [0, 1]
        assert [(v["lang"], v["module"]) for v in activities[0]["variants"]] == [
            ("en", "scorm"),
            ("fr", "scorm"),
        ]
        assert [v["lang"] for v in activities[1]["variants"]] == ["en"]
        assert [a["satisfied"] for a in activities] == [False, False]
        assert [a["last_launched_at"] for a in activities] == [None, None]

        # Courses and activities carry per-language display texts next to
        # their reference title; setting a language again replaces it
        translations_url = f"/api/v1/courses/{course_id}/translations"
        for lang, title in [("fr", "Accueil"), ("en", "Welcome"), ("fr", "Bienvenue")]:
            set_translation = client.put(
                f"{translations_url}/{lang}",
                headers=admin_headers,
                json={"title": title, "description": f"{title}!"},
            )
            assert set_translation.status_code == 200
        assert set_translation.json()["translations"] == [
            {"lang": "en", "title": "Welcome", "description": "Welcome!"},
            {"lang": "fr", "title": "Bienvenue", "description": "Bienvenue!"},
        ]
        assert (
            client.put(
                f"{translations_url}/not_a_tag",
                headers=admin_headers,
                json={"title": "x", "description": "x"},
            ).status_code
            == 400
        )
        assert (
            client.put(
                f"{translations_url}/de",
                headers=learner_headers,
                json={"title": "x", "description": "x"},
            ).status_code
            == 403
        )
        activity_translations_url = (
            f"/api/v1/courses/{course_id}/activities/{activity_1}/translations"
        )
        translated = client.put(
            f"{activity_translations_url}/fr", headers=admin_headers, json={"title": "Intro FR"}
        )
        assert translated.status_code == 200
        # Learners read every translation and pick their language themselves
        course_read = client.get(f"/api/v1/courses/{course_id}", headers=learner_headers).json()
        assert [t["lang"] for t in course_read["translations"]] == ["en", "fr"]
        activities = client.get(
            f"/api/v1/courses/{course_id}/activities", headers=learner_headers
        ).json()
        assert [a["translations"] for a in activities] == [
            [{"lang": "fr", "title": "Intro FR"}],
            [],
        ]
        # Removing drops one language; removing it again is 404
        removed = client.delete(f"{translations_url}/fr", headers=admin_headers)
        assert [t["lang"] for t in removed.json()["translations"]] == ["en"]
        assert client.delete(f"{translations_url}/fr", headers=admin_headers).status_code == 404
        untranslated = client.delete(f"{activity_translations_url}/fr", headers=admin_headers)
        assert untranslated.json()[0]["translations"] == []
        assert (
            client.delete(f"{activity_translations_url}/fr", headers=admin_headers).status_code
            == 404
        )

        # Admins rename and reorder activities; both return the new ordering
        renamed = client.patch(
            f"/api/v1/courses/{course_id}/activities/{activity_1}",
            headers=admin_headers,
            json={"title": "Introduction"},
        )
        assert renamed.status_code == 200
        assert next(a["title"] for a in renamed.json() if a["id"] == activity_1) == "Introduction"

        moved = client.post(
            f"/api/v1/courses/{course_id}/activities/{activity_2}/move",
            headers=admin_headers,
            json={"position": 0},
        )
        assert moved.status_code == 200
        assert [a["id"] for a in moved.json()] == [activity_2, activity_1]
        assert [a["position"] for a in moved.json()] == [0, 1]

        # An activity from another course is not reachable through this course
        assert (
            client.post(
                f"/api/v1/courses/{course_id}/activities/999999/move",
                headers=admin_headers,
                json={"position": 0},
            ).status_code
            == 404
        )
        # Learners cannot mutate activities
        assert (
            client.patch(
                f"/api/v1/courses/{course_id}/activities/{activity_1}",
                headers=learner_headers,
                json={"title": "nope"},
            ).status_code
            == 403
        )

        # Restore the original order for the completion flow below
        client.post(
            f"/api/v1/courses/{course_id}/activities/{activity_1}/move",
            headers=admin_headers,
            json={"position": 0},
        )

        # Completing one language variant satisfies the whole activity, but the
        # course is not complete while the other activity is unsatisfied
        complete_sco(client, fr.json()["scos"][0]["id"], learner_headers)
        activities = client.get(
            f"/api/v1/courses/{course_id}/activities", headers=learner_headers
        ).json()
        assert [a["satisfied"] for a in activities] == [True, False]
        assert len(hooks.completed) == 0
        # The launch is stamped on the activity, whichever variant ran
        first_launch = activities[0]["last_launched_at"]
        assert first_launch is not None
        assert activities[1]["last_launched_at"] is None

        # Completing the other language of an already satisfied activity
        # changes nothing, apart from the activity's latest launch
        complete_sco(client, first["scos"][0]["id"], learner_headers)
        assert len(hooks.completed) == 0
        activities = client.get(
            f"/api/v1/courses/{course_id}/activities", headers=learner_headers
        ).json()
        relaunch = activities[0]["last_launched_at"]
        assert datetime.fromisoformat(relaunch) > datetime.fromisoformat(first_launch)

        # Completing the last activity completes the enrollment
        complete_sco(client, second["scos"][0]["id"], learner_headers)
        activities = client.get(
            f"/api/v1/courses/{course_id}/activities", headers=learner_headers
        ).json()
        assert [a["satisfied"] for a in activities] == [True, True]
        assert len(hooks.completed) == 1
        assert hooks.completed[0].course_id == course_id
        # The most recent launch tells which activity the learner opened last
        assert datetime.fromisoformat(activities[1]["last_launched_at"]) > datetime.fromisoformat(
            relaunch
        )

        enrollment = client.get("/api/v1/enrollments/me", headers=learner_headers).json()[0]
        assert enrollment["status"] == "completed"

        scorm_root = tmp_path / "storage" / "scorm"

        def package_dirs(variant_id: int) -> list[Path]:
            return list(scorm_root.glob(f"{variant_id}-*"))

        # Replacing a variant's package keeps the variant and the progress
        # recorded through it, but resets the learners' scorm attempts
        fr_variant = fr.json()["variant_id"]
        old_fr_dirs = package_dirs(fr_variant)
        assert len(old_fr_dirs) == 1
        fr_sco = fr.json()["scos"][0]["id"]
        suspended = client.post(f"/api/v1/scorm/scos/{fr_sco}/launch", headers=learner_headers)
        suspended_token = suspended.json()["player_url"].rsplit("/", 1)[-1]
        replace_url = f"/api/v1/scorm/variants/{fr_variant}/package"
        new_package = {"file": ("pkg.zip", scorm_zip(SCORM_MANIFEST), "application/zip")}
        assert (
            client.put(replace_url, headers=learner_headers, files=new_package).status_code == 403
        )
        assert (
            client.put(
                "/api/v1/scorm/variants/999999/package", headers=admin_headers, files=new_package
            ).status_code
            == 404
        )
        # A broken package is refused and leaves the current one in place
        broken = client.put(
            replace_url,
            headers=admin_headers,
            files={"file": ("pkg.zip", b"not a zip", "application/zip")},
        )
        assert broken.status_code == 400
        assert package_dirs(fr_variant) == old_fr_dirs
        replaced = client.put(replace_url, headers=admin_headers, files=new_package)
        assert replaced.status_code == 200
        assert replaced.json()["variant_id"] == fr_variant
        assert replaced.json()["activity_id"] == activity_1
        assert replaced.json()["lang"] == "fr"
        new_fr_sco = replaced.json()["scos"][0]["id"]
        assert new_fr_sco != fr_sco
        # The old package's SCOs, attempts and files are gone
        assert (
            client.post(f"/api/v1/scorm/scos/{fr_sco}/launch", headers=learner_headers).status_code
            == 404
        )
        assert client.get(f"/api/v1/scorm/runtime/{suspended_token}").status_code == 401
        new_fr_dirs = package_dirs(fr_variant)
        assert len(new_fr_dirs) == 1
        assert new_fr_dirs != old_fr_dirs
        # The activity stays satisfied and the enrollment completed
        activities = client.get(
            f"/api/v1/courses/{course_id}/activities", headers=learner_headers
        ).json()
        assert [a["satisfied"] for a in activities] == [True, True]
        assert (
            client.get("/api/v1/enrollments/me", headers=learner_headers).json()[0]["status"]
            == "completed"
        )
        # The learner starts the new package afresh
        relaunched = client.post(f"/api/v1/scorm/scos/{new_fr_sco}/launch", headers=learner_headers)
        relaunch_token = relaunched.json()["player_url"].rsplit("/", 1)[-1]
        fresh = client.get(f"/api/v1/scorm/runtime/{relaunch_token}").json()
        assert fresh["entry"] == "ab-initio"
        assert fresh["completion_status"] == "unknown"

        # Deleting a language variant removes its scorm content and stored files
        after_variant_delete = client.delete(
            f"/api/v1/courses/{course_id}/activities/{activity_1}/variants/{fr_variant}",
            headers=admin_headers,
        )
        assert after_variant_delete.status_code == 200
        activity_1_after = next(a for a in after_variant_delete.json() if a["id"] == activity_1)
        assert [v["lang"] for v in activity_1_after["variants"]] == ["en"]
        assert package_dirs(fr_variant) == []
        # The other variant of the same activity keeps its files
        assert len(package_dirs(first["variant_id"])) == 1
        # A variant that does not belong to the activity 404s
        assert (
            client.delete(
                f"/api/v1/courses/{course_id}/activities/{activity_1}/variants/{second['variant_id']}",
                headers=admin_headers,
            ).status_code
            == 404
        )

        # Deleting a whole activity removes it, its variants' files, and
        # reindexes the survivors
        second_variant = second["variant_id"]
        assert len(package_dirs(second_variant)) == 1
        after_activity_delete = client.delete(
            f"/api/v1/courses/{course_id}/activities/{activity_2}",
            headers=admin_headers,
        )
        assert after_activity_delete.status_code == 200
        assert [a["id"] for a in after_activity_delete.json()] == [activity_1]
        assert [a["position"] for a in after_activity_delete.json()] == [0]
        assert package_dirs(second_variant) == []
        # Deleting it again 404s (it is gone)
        assert (
            client.delete(
                f"/api/v1/courses/{course_id}/activities/{activity_2}",
                headers=admin_headers,
            ).status_code
            == 404
        )

        # Deleting the whole course removes it, its remaining activity content
        # and files, and the enrollment; archiving would only hide it
        remaining_variant = first["variant_id"]
        assert len(package_dirs(remaining_variant)) == 1
        course_url = f"/api/v1/courses/{course_id}"
        assert client.delete(course_url, headers=learner_headers).status_code == 403
        assert client.delete(course_url, headers=admin_headers).status_code == 204
        assert package_dirs(remaining_variant) == []
        assert client.get(course_url, headers=admin_headers).status_code == 404
        assert client.get("/api/v1/enrollments/me", headers=learner_headers).json() == []
        # Deleting a course that is already gone 404s
        assert client.delete(course_url, headers=admin_headers).status_code == 404

    assert len(hooks.completed) == 1


def test_course_prerequisites(tmp_path: Path, database_url: str) -> None:
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

    def make_course(client: TestClient, slug: str) -> int:
        course = client.post(
            "/api/v1/courses",
            headers=admin_headers,
            json={"owner_org_id": None, "slug": slug, "title": slug.title(), "description": ""},
        ).json()
        client.post(f"/api/v1/courses/{course['id']}/publish", headers=admin_headers)
        return course["id"]

    with TestClient(app) as client:
        basics = make_course(client, "basics")
        advanced = make_course(client, "advanced")
        expert = make_course(client, "expert")

        learner = client.post("/auth/register").json()
        learner_headers = {"Authorization": f"Bearer {learner['token']}"}

        # Admins declare prerequisite edges; adding one is idempotent and
        # reflected in the course's prerequisite_ids
        required = client.put(
            f"/api/v1/courses/{advanced}/prerequisites/{basics}", headers=admin_headers
        )
        assert required.status_code == 200
        assert required.json()["prerequisite_ids"] == [basics]
        again = client.put(
            f"/api/v1/courses/{advanced}/prerequisites/{basics}", headers=admin_headers
        )
        assert again.json()["prerequisite_ids"] == [basics]
        client.put(f"/api/v1/courses/{expert}/prerequisites/{advanced}", headers=admin_headers)

        # Learners cannot manage prerequisites; unknown courses 404
        assert (
            client.put(
                f"/api/v1/courses/{advanced}/prerequisites/{basics}", headers=learner_headers
            ).status_code
            == 403
        )
        assert (
            client.put(
                f"/api/v1/courses/{advanced}/prerequisites/999999", headers=admin_headers
            ).status_code
            == 404
        )

        # Self-reference and transitive cycles are refused
        assert (
            client.put(
                f"/api/v1/courses/{basics}/prerequisites/{basics}", headers=admin_headers
            ).status_code
            == 409
        )
        assert (
            client.put(
                f"/api/v1/courses/{basics}/prerequisites/{expert}", headers=admin_headers
            ).status_code
            == 409
        )

        # Enrolling with an uncompleted prerequisite is refused; completing
        # the prerequisite course unlocks it
        blocked = client.post(
            "/api/v1/enrollments",
            headers=admin_headers,
            json={"user_id": learner["user_id"], "course_id": advanced},
        )
        assert blocked.status_code == 409
        assert str(basics) in blocked.json()["detail"]

        basics_enrollment = client.post(
            "/api/v1/enrollments",
            headers=admin_headers,
            json={"user_id": learner["user_id"], "course_id": basics},
        )
        assert basics_enrollment.status_code == 201
        # Active is not enough: the prerequisite must be completed
        assert (
            client.post(
                "/api/v1/enrollments",
                headers=admin_headers,
                json={"user_id": learner["user_id"], "course_id": advanced},
            ).status_code
            == 409
        )
        client.post(
            f"/api/v1/enrollments/{basics_enrollment.json()['id']}/complete",
            headers=admin_headers,
        )
        assert (
            client.post(
                "/api/v1/enrollments",
                headers=admin_headers,
                json={"user_id": learner["user_id"], "course_id": advanced},
            ).status_code
            == 201
        )

        # The expert course still requires the advanced one completed
        assert (
            client.post(
                "/api/v1/enrollments",
                headers=admin_headers,
                json={"user_id": learner["user_id"], "course_id": expert},
            ).status_code
            == 409
        )

        # Removing the edge lifts the gate; removing a non-set edge 404s
        removed = client.delete(
            f"/api/v1/courses/{expert}/prerequisites/{advanced}", headers=admin_headers
        )
        assert removed.status_code == 200
        assert removed.json()["prerequisite_ids"] == []
        assert (
            client.delete(
                f"/api/v1/courses/{expert}/prerequisites/{advanced}", headers=admin_headers
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/v1/enrollments",
                headers=admin_headers,
                json={"user_id": learner["user_id"], "course_id": expert},
            ).status_code
            == 201
        )

        # Deleting a course drops the edges of courses that required it
        client.put(f"/api/v1/courses/{expert}/prerequisites/{advanced}", headers=admin_headers)
        deleted = client.delete(f"/api/v1/courses/{advanced}", headers=admin_headers)
        assert deleted.status_code == 204
        expert_course = client.get(f"/api/v1/courses/{expert}", headers=admin_headers).json()
        assert expert_course["prerequisite_ids"] == []


def test_sequential_course_locks_activities(tmp_path: Path, database_url: str) -> None:

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

    def import_activity(client: TestClient, course_id: int) -> dict:
        return client.post(
            "/api/v1/scorm/import",
            headers=admin_headers,
            files={"file": ("pkg.zip", scorm_zip(SCORM_MANIFEST), "application/zip")},
            data={"course_id": str(course_id), "lang": "en"},
        ).json()

    def complete_sco(client: TestClient, sco_id: int, headers: dict[str, str]) -> None:
        launch = client.post(f"/api/v1/scorm/scos/{sco_id}/launch", headers=headers)
        assert launch.status_code == 200
        token = launch.json()["player_url"].rsplit("/", 1)[-1]
        payload = {
            "completion_status": "completed",
            "success_status": "passed",
            "score_scaled": 1.0,
            "score_raw": 100.0,
            "progress_measure": None,
            "location": None,
            "suspend_data": None,
            "interactions": [],
        }
        assert client.post(f"/api/v1/scorm/runtime/{token}/commit", json=payload).status_code == 204

    with TestClient(app) as client:
        # Courses are free-order unless created sequential
        course = client.post(
            "/api/v1/courses",
            headers=admin_headers,
            json={
                "owner_org_id": None,
                "slug": "induction",
                "title": "Induction",
                "description": "",
            },
        ).json()
        course_id = course["id"]
        assert course["sequential"] is False

        first = import_activity(client, course_id)
        second = import_activity(client, course_id)
        client.post(f"/api/v1/courses/{course_id}/publish", headers=admin_headers)

        learner = client.post("/auth/register").json()
        learner_headers = {"Authorization": f"Bearer {learner['token']}"}
        client.post(
            "/api/v1/enrollments",
            headers=admin_headers,
            json={"user_id": learner["user_id"], "course_id": course_id},
        )

        # A free-order course locks nothing
        activities = client.get(
            f"/api/v1/courses/{course_id}/activities", headers=learner_headers
        ).json()
        assert [a["locked"] for a in activities] == [False, False]

        # Turning the course sequential locks everything after the first
        # unsatisfied activity
        edited = client.put(
            f"/api/v1/courses/{course_id}",
            headers=admin_headers,
            json={
                "slug": "induction",
                "title": "Induction",
                "description": "",
                "sequential": True,
            },
        )
        assert edited.json()["sequential"] is True
        activities = client.get(
            f"/api/v1/courses/{course_id}/activities", headers=learner_headers
        ).json()
        assert [a["locked"] for a in activities] == [False, True]

        # Launching a locked activity's content is refused
        locked_launch = client.post(
            f"/api/v1/scorm/scos/{second['scos'][0]['id']}/launch", headers=learner_headers
        )
        assert locked_launch.status_code == 409
        assert "locked" in locked_launch.json()["detail"]

        # Satisfying the first activity unlocks the second
        complete_sco(client, first["scos"][0]["id"], learner_headers)
        activities = client.get(
            f"/api/v1/courses/{course_id}/activities", headers=learner_headers
        ).json()
        assert [a["satisfied"] for a in activities] == [True, False]
        assert [a["locked"] for a in activities] == [False, False]

        complete_sco(client, second["scos"][0]["id"], learner_headers)
        enrollment = client.get("/api/v1/enrollments/me", headers=learner_headers).json()[0]
        assert enrollment["status"] == "completed"

    assert len(hooks.completed) == 1
