"""OIDC auth backend: sign-in through an external OpenID Connect provider."""

import base64
import hashlib
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import update

from microlms import (
    DEFAULT_ACCESS_POLICY,
    Settings,
    create_app,
    create_db_engine,
    create_session_factory,
)
from microlms.domain.users import User
from microlms.storage import FilesystemStorage
from tests.helpers import ALL_CORE_ROUTERS, RecordingHooks, org_id_of, seed_org

if TYPE_CHECKING:
    import httpx
    from joserfc.jwk import RSAKey

ISSUER = "https://idp.example.com/application/o/microlms/"
CLIENT_ID = "microlms-client"
CLIENT_SECRET = "client-secret"
REDIRECT_URI = "http://testserver/auth/oidc/callback"
POST_LOGIN_URL = "http://frontend.example.com/after-login"


@dataclass
class FakeProvider:
    """An OpenID Connect provider answering the LMS's back-channel requests.

    ``sign_in`` plays the browser's visit to the authorization endpoint: it
    records the user's claims under a fresh code, as the provider would after
    the user signed in. Like some providers, it keeps the profile claims
    out of the ID token and serves them from the userinfo endpoint only.
    """

    key: "RSAKey"
    # Signs ID tokens instead of the published key when set, like a forger
    forging_key: "RSAKey | None" = None
    claims_by_code: dict[str, dict[str, object]] = field(default_factory=dict)
    challenge_by_code: dict[str, str] = field(default_factory=dict)
    claims_by_access_token: dict[str, dict[str, object]] = field(default_factory=dict)

    def sign_in(self, authorization_url: str, claims: Mapping[str, object]) -> tuple[str, str]:
        """The ``(code, state)`` the provider redirects back with."""
        query = {
            name: values[0] for name, values in parse_qs(urlsplit(authorization_url).query).items()
        }
        assert query["client_id"] == CLIENT_ID
        assert query["redirect_uri"] == REDIRECT_URI
        assert query["code_challenge_method"] == "S256"
        code = secrets.token_urlsafe(16)
        self.claims_by_code[code] = {**claims, "nonce": query["nonce"]}
        self.challenge_by_code[code] = query["code_challenge"]
        return code, query["state"]

    def handle(self, request: "httpx.Request") -> "httpx.Response":
        import httpx
        from joserfc import jwt as jose_jwt
        from joserfc.jwk import KeySet

        url = str(request.url)
        if url == f"{ISSUER.rstrip('/')}/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": "https://idp.example.com/authorize",
                    "token_endpoint": "https://idp.example.com/token",
                    "userinfo_endpoint": "https://idp.example.com/userinfo",
                    "jwks_uri": "https://idp.example.com/jwks",
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        if url == "https://idp.example.com/jwks":
            return httpx.Response(200, json=KeySet([self.key]).as_dict(private=False))
        if url == "https://idp.example.com/userinfo":
            access_token = request.headers["Authorization"].removeprefix("Bearer ")
            return httpx.Response(200, json=self.claims_by_access_token[access_token])
        assert url == "https://idp.example.com/token"
        expected_auth = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        assert request.headers["Authorization"] == f"Basic {expected_auth}"
        form = {name: values[0] for name, values in parse_qs(request.content.decode()).items()}
        code = form["code"]
        if code not in self.claims_by_code:
            return httpx.Response(400, json={"error": "invalid_grant"})
        verifier_digest = hashlib.sha256(form["code_verifier"].encode()).digest()
        challenge = base64.urlsafe_b64encode(verifier_digest).rstrip(b"=").decode()
        assert challenge == self.challenge_by_code.pop(code)
        claims = self.claims_by_code.pop(code)
        access_token = secrets.token_urlsafe(16)
        self.claims_by_access_token[access_token] = {
            name: value for name, value in claims.items() if name != "nonce"
        }
        now = int(time.time())
        signing_key = self.key if self.forging_key is None else self.forging_key
        id_token = jose_jwt.encode(
            {"alg": "RS256", "kid": signing_key.kid},
            {
                "iss": ISSUER,
                "aud": CLIENT_ID,
                "iat": now,
                "exp": now + 300,
                "sub": claims["sub"],
                "nonce": claims["nonce"],
            },
            signing_key,
        )
        return httpx.Response(
            200,
            json={"access_token": access_token, "token_type": "Bearer", "id_token": id_token},
        )


def _oidc_app(tmp_path: Path, database_url: str, provider: FakeProvider) -> FastAPI:
    import httpx

    from microlms.auth.oidc import OidcAuthBackend, OidcProvider, OidcRoleGrant

    return create_app(
        settings=Settings(database_url=database_url),
        engine=create_db_engine(database_url, echo=False),
        auth=OidcAuthBackend(
            provider=OidcProvider(
                issuer=ISSUER,
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
                redirect_uri=REDIRECT_URI,
                scope="openid profile",
                http_client=httpx.Client(transport=httpx.MockTransport(provider.handle)),
            ),
            groups_claim="groups",
            group_roles={
                "lms-admins": [OidcRoleGrant(role="admin", org_slug=None)],
                "acme-learners": [OidcRoleGrant(role="learner", org_slug="acme")],
                "acme-managers": [
                    OidcRoleGrant(role="manager", org_slug="acme"),
                    OidcRoleGrant(role="learner", org_slug="acme"),
                ],
            },
            post_login_url=POST_LOGIN_URL,
            secret_key="0123456789abcdef0123456789abcdef",
            algorithm="HS256",
            access_token_expire_minutes=30,
            refresh_token_expire_minutes=8 * 60,
            session_max_minutes=24 * 60,
            # The test client talks plain HTTP, which never carries Secure cookies
            refresh_cookie_secure=False,
        ),
        storage=FilesystemStorage(tmp_path / "storage"),
        hooks=RecordingHooks(),
        routers=ALL_CORE_ROUTERS,
        modules=[],
        schedules=[],
        access_policy=DEFAULT_ACCESS_POLICY,
    )


def _sign_in(
    client: TestClient, provider: FakeProvider, claims: Mapping[str, object]
) -> "httpx.Response":
    """Run a browser sign-in through the provider; the callback's redirect."""
    login = client.get("/auth/oidc/login", params={"remember": "false"}, follow_redirects=False)
    assert login.status_code == 303
    code, state = provider.sign_in(login.headers["location"], claims)
    return client.get(
        "/auth/oidc/callback", params={"code": code, "state": state}, follow_redirects=False
    )


def _me(client: TestClient) -> dict[str, object]:
    """The signed-in user's profile, through a renewed session access token."""
    renewed = client.post("/auth/session/refresh", json={"remember": False})
    assert renewed.status_code == 200, renewed.text
    me = client.get(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {renewed.json()['access_token']}"},
    )
    assert me.status_code == 200, me.text
    return me.json()


def test_oidc_sign_in_loop(tmp_path: Path, database_url: str) -> None:
    """First sign-in creates the user from the provider's claims, later
    sign-ins follow the provider's name, email and groups, and refusals
    redirect with their reason."""
    pytest.importorskip("authlib")
    from joserfc.jwk import RSAKey

    seed_org(database_url, "acme")
    acme_id = org_id_of(database_url, "acme")
    provider = FakeProvider(key=RSAKey.generate_key(2048, parameters={"kid": "idp-key-1"}))

    with TestClient(_oidc_app(tmp_path, database_url, provider)) as client:
        # First sign-in: the user is created with the mapped org role
        signed_in = _sign_in(
            client,
            provider,
            {
                "sub": "user-1",
                "name": "Jane Doe",
                "email": "jane@example.com",
                "groups": ["acme-learners"],
            },
        )
        assert signed_in.status_code == 303, signed_in.text
        assert signed_in.headers["location"] == POST_LOGIN_URL
        me = _me(client)
        assert me["full_name"] == "Jane Doe"
        assert me["email"] == "jane@example.com"
        assert me["roles"] == []
        assert me["orgs"] == [{"org_id": acme_id, "name": "acme", "roles": ["learner"]}]

        # Next sign-in: name, email and roles follow the provider, unmapped
        # groups are ignored
        signed_in = _sign_in(
            client,
            provider,
            {
                "sub": "user-1",
                "name": "Jane Smith",
                "email": "jane.smith@example.com",
                "groups": ["lms-admins", "unrelated"],
            },
        )
        assert signed_in.headers["location"] == POST_LOGIN_URL
        me = _me(client)
        user_id = me["id"]
        assert me["full_name"] == "Jane Smith"
        assert me["email"] == "jane.smith@example.com"
        assert me["roles"] == ["admin"]
        assert me["orgs"] == []

        # The provider's role change is on the audit trail
        token = client.post("/auth/session/refresh", json={"remember": False}).json()
        audit = client.get(
            "/api/v1/audit",
            params={"action": "user.roles_synced"},
            headers={"Authorization": f"Bearer {token['access_token']}"},
        )
        assert [entry["target_id"] for entry in audit.json()] == [user_id]

        # Logging out ends the LMS session
        assert client.delete("/auth/session").status_code == 204
        assert client.post("/auth/session/refresh", json={"remember": False}).status_code == 400

        # Another user already has the email the provider sends: refused
        taken = _sign_in(
            client,
            provider,
            {
                "sub": "user-3",
                "name": "Impostor",
                "email": "jane.smith@example.com",
                "groups": ["acme-learners"],
            },
        )
        assert taken.headers["location"] == f"{POST_LOGIN_URL}?error=email_taken"

        # A provider sending no email leaves the user without one
        no_email = _sign_in(
            client, provider, {"sub": "user-4", "name": "No Mail", "groups": ["acme-learners"]}
        )
        assert no_email.headers["location"] == POST_LOGIN_URL
        assert _me(client)["email"] is None
        assert client.delete("/auth/session").status_code == 204

        # Groups mapping to no role: refused, and no user is created
        refused = _sign_in(client, provider, {"sub": "user-2", "name": "Nobody", "groups": []})
        assert refused.headers["location"] == f"{POST_LOGIN_URL}?error=no_role"

        # A callback this browser did not start
        login = client.get("/auth/oidc/login", params={"remember": "false"}, follow_redirects=False)
        code, _ = provider.sign_in(
            login.headers["location"], {"sub": "user-1", "name": "Jane Smith", "groups": []}
        )
        forged = client.get(
            "/auth/oidc/callback",
            params={"code": code, "state": "forged"},
            follow_redirects=False,
        )
        assert forged.headers["location"] == f"{POST_LOGIN_URL}?error=invalid_login_state"

        # The user cancelled at the provider
        login = client.get("/auth/oidc/login", params={"remember": "false"}, follow_redirects=False)
        _, state = provider.sign_in(login.headers["location"], {})
        cancelled = client.get(
            "/auth/oidc/callback",
            params={"error": "access_denied", "state": state},
            follow_redirects=False,
        )
        assert cancelled.headers["location"] == f"{POST_LOGIN_URL}?error=provider_error"

        # An ID token signed by another key than the published one
        provider.forging_key = RSAKey.generate_key(2048, parameters={"kid": "idp-key-1"})
        login = client.get("/auth/oidc/login", params={"remember": "false"}, follow_redirects=False)
        code, state = provider.sign_in(
            login.headers["location"], {"sub": "user-1", "name": "Jane Smith", "groups": []}
        )
        invalid = client.get(
            "/auth/oidc/callback", params={"code": code, "state": state}, follow_redirects=False
        )
        assert invalid.headers["location"] == f"{POST_LOGIN_URL}?error=invalid_id_token"

    # A deactivated account is refused even though the provider signs it in
    engine = create_db_engine(database_url, echo=False)
    with create_session_factory(engine)() as session:
        session.execute(update(User).where(User.id == user_id).values(is_active=False))
        session.commit()
    engine.dispose()
    provider = FakeProvider(key=provider.key)
    with TestClient(_oidc_app(tmp_path, database_url, provider)) as client:
        refused = _sign_in(
            client, provider, {"sub": "user-1", "name": "Jane Smith", "groups": ["lms-admins"]}
        )
        assert refused.headers["location"] == f"{POST_LOGIN_URL}?error=account_deactivated"
