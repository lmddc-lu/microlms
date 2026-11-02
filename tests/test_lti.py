"""lti module: the LTI 1.3 platform launch loop."""

import time
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
from microlms.storage import FilesystemStorage
from tests.helpers import ALL_CORE_ROUTERS, DEFAULT_ORG_SLUG, FakeLrs, RecordingHooks, seed_admin


def _auto_posted_fields(page: str) -> dict[str, str]:
    """The hidden fields of an auto-posting form page."""
    import html
    import re

    return {
        name: html.unescape(value)
        for name, value in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', page)
    }


def test_lti_platform_launch_loop(tmp_path: Path, database_url: str) -> None:
    """The test plays the tool: register, launch through the OIDC login,
    obtain an access token with a signed client assertion, and publish
    scores that complete the enrollment."""
    from urllib.parse import parse_qs, urlsplit

    # The lti module needs the optional `lti` extra; skip without it
    jwt = pytest.importorskip("jwt")
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    from microlms.lrs import get_lrs_client
    from microlms.modules.lti.security import get_tool_key_set_client

    admin_token = seed_admin(database_url)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    platform_pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    # The tool's own key, published through its (faked) key set URL
    tool_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    tool_jwk = {**RSAAlgorithm.to_jwk(tool_key.public_key(), as_dict=True), "kid": "tool-key-1"}
    fetched_key_sets: list[str] = []

    class FakeToolKeySetClient:
        def fetch(self, url: str) -> jwt.PyJWKSet:
            fetched_key_sets.append(url)
            return jwt.PyJWKSet.from_dict({"keys": [tool_jwk]})

    issuer = "https://lms.example.com"
    hooks = RecordingHooks()
    app = create_app(
        settings=Settings(
            database_url=database_url,
            registration_org_slug=DEFAULT_ORG_SLUG,
            lti_issuer=issuer,
            lti_private_key=platform_pem,
        ),
        engine=create_db_engine(database_url, echo=False),
        auth=AnonymousAuthBackend(),
        storage=FilesystemStorage(tmp_path / "storage"),
        hooks=hooks,
        routers=ALL_CORE_ROUTERS,
        modules=["lti"],
        schedules=[],
        access_policy=DEFAULT_ACCESS_POLICY,
    )
    app.dependency_overrides[get_tool_key_set_client] = lambda: FakeToolKeySetClient()
    fake_lrs = FakeLrs()
    app.dependency_overrides[get_lrs_client] = lambda: fake_lrs

    with TestClient(app) as client:
        assert client.get("/api/v1/lti/status").json()["status"] == "ready"

        # Registration is platform administration; learners are refused
        learner = client.post("/auth/register").json()
        learner_headers = {"Authorization": f"Bearer {learner['token']}"}
        tool_payload = {
            "name": "Quiz tool",
            "oidc_login_url": "https://tool.example.com/login",
            "redirect_uris": ["https://tool.example.com/launch"],
            "jwks_url": "https://tool.example.com/jwks",
            "share_identity": False,
        }
        assert (
            client.post("/api/v1/lti/tools", headers=learner_headers, json=tool_payload).status_code
            == 403
        )
        registered = client.post("/api/v1/lti/tools", headers=admin_headers, json=tool_payload)
        assert registered.status_code == 201
        platform = registered.json()["platform"]
        client_id = platform["client_id"]
        assert platform["issuer"] == issuer
        assert platform["token_url"] == "http://testserver/api/v1/lti/token"
        tool_id = registered.json()["id"]

        course_id = client.post(
            "/api/v1/courses",
            headers=admin_headers,
            json={"owner_org_id": None, "slug": "lti", "title": "LTI course", "description": ""},
        ).json()["id"]
        created = client.post(
            "/api/v1/lti/links",
            headers=admin_headers,
            json={
                "course_id": course_id,
                "lang": "en",
                "tool_id": tool_id,
                "title": "Chapter quiz",
                "target_link_uri": "https://tool.example.com/quiz/1",
                "custom": {"quiz": "1"},
                "score_maximum": 10,
            },
        )
        assert created.status_code == 201
        link = created.json()["link"]
        client.post(f"/api/v1/courses/{course_id}/publish", headers=admin_headers)
        variant_link = client.get(
            f"/api/v1/lti/variants/{created.json()['variant_id']}/link", headers=learner_headers
        )
        assert variant_link.json()["id"] == link["id"]

        # Launch needs an enrollment
        launch_path = f"/api/v1/lti/links/{link['id']}/launch"
        assert client.post(launch_path, headers=learner_headers).status_code == 403
        enrollment = client.post(
            "/api/v1/enrollments",
            headers=admin_headers,
            json={"user_id": learner["user_id"], "course_id": course_id},
        ).json()

        # Step 1: third-party initiated login at the tool's login URL
        launch_url = client.post(launch_path, headers=learner_headers).json()["launch_url"]
        assert launch_url.startswith("https://tool.example.com/login?")
        login = {name: values[0] for name, values in parse_qs(urlsplit(launch_url).query).items()}
        assert login["iss"] == issuer
        assert login["login_hint"] == learner["user_id"]
        assert login["target_link_uri"] == "https://tool.example.com/quiz/1"
        assert login["client_id"] == client_id
        assert login["lti_deployment_id"] == platform["deployment_id"]

        # Step 2: the tool's authentication request. An unregistered redirect
        # URI is refused outright, never posted to
        auth_request = {
            "scope": "openid",
            "response_type": "id_token",
            "client_id": client_id,
            "redirect_uri": "https://tool.example.com/launch",
            "login_hint": login["login_hint"],
            "lti_message_hint": login["lti_message_hint"],
            "state": "tool-state",
            "response_mode": "form_post",
            "nonce": "tool-nonce",
            "prompt": "none",
        }
        stray = client.get(
            "/api/v1/lti/auth", params={**auth_request, "redirect_uri": "https://evil.example"}
        )
        assert stray.status_code == 400

        # Step 3: the signed id_token is auto-posted to the redirect URI
        answer = client.get("/api/v1/lti/auth", params=auth_request)
        assert answer.status_code == 200
        assert 'action="https://tool.example.com/launch"' in answer.text
        fields = _auto_posted_fields(answer.text)
        assert fields["state"] == "tool-state"
        platform_keys = jwt.PyJWKSet.from_dict(client.get("/api/v1/lti/jwks").json())
        kid = jwt.get_unverified_header(fields["id_token"])["kid"]
        claims = jwt.decode(
            fields["id_token"],
            platform_keys[kid],
            algorithms=["RS256"],
            audience=client_id,
            issuer=issuer,
        )
        lti_claim = "https://purl.imsglobal.org/spec/lti/claim"
        assert claims["sub"] == learner["user_id"]
        # The tool was registered without identity sharing: sub only
        assert not {"name", "given_name", "family_name", "email"} & claims.keys()
        assert claims["nonce"] == "tool-nonce"
        assert claims[f"{lti_claim}/message_type"] == "LtiResourceLinkRequest"
        assert claims[f"{lti_claim}/version"] == "1.3.0"
        assert claims[f"{lti_claim}/deployment_id"] == platform["deployment_id"]
        assert claims[f"{lti_claim}/resource_link"]["id"] == link["resource_link_id"]
        assert claims[f"{lti_claim}/custom"] == {"quiz": "1"}
        ags = claims["https://purl.imsglobal.org/spec/lti-ags/claim/endpoint"]
        lineitem_url = ags["lineitem"]
        assert lineitem_url == f"http://testserver/api/v1/lti/lineitems/{link['id']}"

        # The completed launch is recorded in the LRS with the canonical actor
        # and the enrollment registration, against the link's stable IRI
        link_iri = f"urn:microlms:lti:{link['resource_link_id']}"
        launched = fake_lrs.statements[-1]
        assert launched["verb"]["id"] == "http://adlnet.gov/expapi/verbs/launched"
        assert launched["actor"]["account"]["name"] == learner["user_id"]
        assert launched["context"]["registration"] == enrollment["registration"]
        assert launched["object"]["id"] == link_iri
        assert launched["object"]["definition"]["name"] == {"en": "Chapter quiz"}

        # The message hint is single use: replaying the login posts an error
        replayed = _auto_posted_fields(client.get("/api/v1/lti/auth", params=auth_request).text)
        assert replayed["error"] == "login_required"
        assert "id_token" not in replayed
        assert len(fake_lrs.statements) == 1

        # With identity sharing turned on, the next launch carries the
        # learner's name (split into given and family names) and email
        client.put(
            f"/api/v1/users/{learner['user_id']}",
            headers=admin_headers,
            json={"email": "jane@example.com", "full_name": "Jane  Marie Doe", "lang": None},
        )
        shared = client.patch(
            f"/api/v1/lti/tools/{tool_id}", headers=admin_headers, json={"share_identity": True}
        )
        assert shared.status_code == 200
        assert shared.json()["share_identity"] is True
        assert shared.json()["platform"]["client_id"] == client_id
        relaunch_url = client.post(launch_path, headers=learner_headers).json()["launch_url"]
        relaunch_hint = parse_qs(urlsplit(relaunch_url).query)["lti_message_hint"][0]
        relaunched = _auto_posted_fields(
            client.get(
                "/api/v1/lti/auth", params={**auth_request, "lti_message_hint": relaunch_hint}
            ).text
        )
        identity = jwt.decode(
            relaunched["id_token"],
            platform_keys[kid],
            algorithms=["RS256"],
            audience=client_id,
            issuer=issuer,
        )
        assert identity["name"] == "Jane Marie Doe"
        assert identity["given_name"] == "Jane"
        assert identity["family_name"] == "Marie Doe"
        assert identity["email"] == "jane@example.com"

        # Services: a client assertion signed by the tool buys an access token
        def client_assertion(jti: str) -> str:
            now = int(time.time())
            return jwt.encode(
                {
                    "iss": client_id,
                    "sub": client_id,
                    "aud": platform["token_url"],
                    "iat": now,
                    "exp": now + 300,
                    "jti": jti,
                },
                tool_key,
                algorithm="RS256",
                headers={"kid": "tool-key-1"},
            )

        token_request = {
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": client_assertion("assertion-1"),
            "scope": "https://purl.imsglobal.org/spec/lti-ags/scope/score "
            "https://purl.imsglobal.org/spec/lti-ags/scope/lineitem.readonly "
            "https://purl.imsglobal.org/spec/lti-ags/scope/lineitem",
        }
        token = client.post("/api/v1/lti/token", data=token_request)
        assert token.status_code == 200
        assert fetched_key_sets == ["https://tool.example.com/jwks"]
        # Only the offered scopes are granted
        assert token.json()["scope"].split() == [
            "https://purl.imsglobal.org/spec/lti-ags/scope/lineitem.readonly",
            "https://purl.imsglobal.org/spec/lti-ags/scope/score",
        ]
        service_headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
        # A replayed assertion is refused
        replayed_token = client.post("/api/v1/lti/token", data=token_request)
        assert replayed_token.status_code == 400
        assert replayed_token.json()["error"] == "invalid_client"

        lineitem_path = urlsplit(lineitem_url).path
        lineitem = client.get(lineitem_path, headers=service_headers)
        assert lineitem.status_code == 200
        assert lineitem.headers["content-type"].startswith("application/vnd.ims.lis.v2.lineitem")
        assert lineitem.json() == {
            "id": lineitem_url,
            "label": "Chapter quiz",
            "scoreMaximum": 10,
            "resourceLinkId": link["resource_link_id"],
        }
        assert client.get(lineitem_path).status_code == 401

        score_headers = {
            **service_headers,
            "Content-Type": "application/vnd.ims.lis.v1.score+json",
        }

        def publish(score: dict[str, object]) -> int:
            return client.post(
                f"{lineitem_path}/scores", headers=score_headers, json=score
            ).status_code

        started = {
            "userId": learner["user_id"],
            "timestamp": "2026-09-28T10:00:00.000Z",
            "activityProgress": "Started",
            "gradingProgress": "NotReady",
        }
        statements_before_scores = len(fake_lrs.statements)
        assert publish(started) == 204
        assert hooks.completed == []
        # A progress-only update (no score, not completed) writes no statement
        assert len(fake_lrs.statements) == statements_before_scores
        # scoreMaximum must accompany scoreGiven
        assert publish({**started, "scoreGiven": 3}) == 422

        # The tool reporting the activity completed satisfies it, which
        # completes the enrollment (the course has this one activity)
        completed = {
            **started,
            "timestamp": "2026-09-28T10:05:00.000Z",
            "scoreGiven": 8,
            "scoreMaximum": 10,
            "activityProgress": "Completed",
            "gradingProgress": "FullyGraded",
        }
        # When the LRS refuses the statement, the score is not stored either
        # and the request fails, so the tool retries it later
        import httpx

        class FailingLrs(FakeLrs):
            def post_statement(self, statement) -> None:
                raise httpx.ConnectError("LRS unreachable")

        app.dependency_overrides[get_lrs_client] = lambda: FailingLrs()
        with pytest.raises(httpx.ConnectError):
            publish(completed)
        assert hooks.completed == []
        app.dependency_overrides[get_lrs_client] = lambda: fake_lrs

        assert publish(completed) == 204
        scored = fake_lrs.statements[-1]
        assert scored["verb"]["id"] == "http://adlnet.gov/expapi/verbs/completed"
        assert scored["object"]["id"] == link_iri
        assert scored["context"]["registration"] == enrollment["registration"]
        # Dated by the tool's score timestamp, not by when it arrived
        assert scored["timestamp"] == "2026-09-28T10:05:00+00:00"
        assert scored["result"] == {
            "score": {"raw": 8, "min": 0, "max": 10, "scaled": 0.8},
            "completion": True,
        }
        # An out-of-order older update is accepted but ignored, and writes
        # no statement
        assert publish(started) == 204
        assert fake_lrs.statements[-1] is scored
        # A score for a user outside the course is refused
        assert publish({**completed, "userId": str(uuid.uuid4())}) == 404

    assert len(hooks.completed) == 1
    assert hooks.completed[0].course_id == course_id
    # The latest result on record is the completed one, not the stale update
    from microlms.modules.lti.models import LtiResult

    engine = create_db_engine(database_url, echo=False)
    with create_session_factory(engine)() as session:
        result = session.query(LtiResult).one()
        assert (result.activity_progress, result.score_given) == ("Completed", 8)
    engine.dispose()
