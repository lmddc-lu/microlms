"""Bundled JWT backend and custom credential transports."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI, Request
from fastapi.openapi.models import APIKey, APIKeyIn
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session as OrmSession

from microlms import (
    DEFAULT_ACCESS_POLICY,
    Settings,
    create_app,
    create_db_engine,
    create_session_factory,
)
from microlms.auth import AuthenticatedUser, AuthenticationError, authenticated_user_from
from microlms.domain.auth_tokens import TokenNotFoundError, resolve_anonymous_token
from microlms.domain.refresh_tokens import RefreshToken
from microlms.domain.users import UserRoleAssignment
from microlms.events import NullEventHooks
from microlms.storage import FilesystemStorage
from tests.helpers import (
    ALL_CORE_ROUTERS,
    DEFAULT_ORG_SLUG,
    RecordingHooks,
    default_org_id,
    seed_admin,
    seed_default_org,
)

if TYPE_CHECKING:
    from microlms.auth.jwt import JwtAuthBackend


def _jwt_backend(secret_key: str) -> "JwtAuthBackend":
    """The JWT backend under test, locking an account after 3 failed logins."""
    # Imported here so the rest of the suite does not require the optional
    # `jwt` extra (pyjwt + pwdlib) to be installed; callers skip without it.
    from microlms.auth.jwt import JwtAuthBackend
    from microlms.auth.password_policy import PasswordPolicy

    return JwtAuthBackend(
        secret_key=secret_key,
        algorithm="HS256",
        access_token_expire_minutes=30,
        refresh_token_expire_minutes=14 * 24 * 60,
        session_max_minutes=30 * 24 * 60,
        # The test client talks plain HTTP, which never carries Secure cookies
        refresh_cookie_secure=False,
        max_failed_logins=3,
        lockout_minutes=15,
        reset_token_expire_minutes=60,
        password_policy=PasswordPolicy(
            min_length=8,
            require_uppercase=True,
            require_lowercase=True,
            require_digit=True,
            require_special=True,
        ),
    )


def _jwt_app(tmp_path: Path, database_url: str, hooks: RecordingHooks) -> FastAPI:
    return create_app(
        settings=Settings(database_url=database_url, registration_org_slug=DEFAULT_ORG_SLUG),
        engine=create_db_engine(database_url, echo=False),
        auth=_jwt_backend("0123456789abcdef0123456789abcdef"),
        storage=FilesystemStorage(tmp_path / "storage"),
        hooks=hooks,
        routers=ALL_CORE_ROUTERS,
        modules=[],
        schedules=[],
        access_policy=DEFAULT_ACCESS_POLICY,
    )


def test_jwt_auth_login_loop(tmp_path: Path, database_url: str) -> None:
    pytest.importorskip("jwt")
    hooks = RecordingHooks()
    app = _jwt_app(tmp_path, database_url, hooks)

    with TestClient(app) as client:
        # Registration lands learners in the default organization, so it
        # fails loudly until that organization exists
        credentials = {"email": "jane@example.com", "password": "S3cret-pass", "full_name": "Jane"}
        missing_org = client.post("/auth/register", json=credentials)
        assert missing_org.status_code == 503
        assert DEFAULT_ORG_SLUG in missing_org.json()["detail"]
        seed_default_org(database_url)

        # A password breaking the policy is refused with every broken rule
        weak = client.post("/auth/register", json={**credentials, "password": "short"})
        assert weak.status_code == 422
        assert weak.json()["detail"]["unmet_rules"] == [
            "min_length",
            "uppercase",
            "digit",
            "special",
        ]

        registration = client.post("/auth/register", json=credentials)
        assert registration.status_code == 201
        user_id = registration.json()["user_id"]

        # The same email cannot register twice
        duplicate = client.post(
            "/auth/register",
            json={"email": "jane@example.com", "password": "Other-pass1", "full_name": "Jane"},
        )
        assert duplicate.status_code == 409

        # OAuth 2.0 token endpoint, password grant: username is the email
        def login(email: str, password: str):
            return client.post(
                "/auth/token",
                data={"grant_type": "password", "username": email, "password": password},
            )

        def refresh(refresh_token: str):
            return client.post(
                "/auth/token",
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            )

        def oauth_error(response) -> str:
            assert response.status_code == 400
            return response.json()["error"]

        def refusal(response) -> str:
            # Why a password login was refused
            assert oauth_error(response) == "invalid_grant"
            return response.json()["reason"]

        token_response = login("jane@example.com", "S3cret-pass")
        assert token_response.status_code == 200
        assert token_response.headers["cache-control"] == "no-store"
        assert token_response.json()["token_type"] == "bearer"
        assert token_response.json()["expires_in"] == 30 * 60
        access_token = token_response.json()["access_token"]

        # Wrong password is refused
        assert refusal(login("jane@example.com", "wrong")) == "invalid_credentials"

        # Malformed requests get the RFC 6749 error codes
        def token_error(data: dict[str, str]) -> str:
            return oauth_error(client.post("/auth/token", data=data))

        jane_credentials = {"username": "jane@example.com", "password": "S3cret-pass"}
        assert token_error(jane_credentials) == "invalid_request"
        assert token_error({"grant_type": "client_credentials"}) == "unsupported_grant_type"
        assert token_error({"grant_type": "refresh_token"}) == "invalid_request"
        assert (
            token_error({"grant_type": "password", "scope": "admin", **jane_credentials})
            == "invalid_scope"
        )

        # --- Refresh tokens ---
        # A refresh renews the access token and rotates the refresh token
        first_refresh_token = token_response.json()["refresh_token"]
        renewed = refresh(first_refresh_token)
        assert renewed.status_code == 200
        assert renewed.headers["cache-control"] == "no-store"
        renewed_tokens = renewed.json()
        assert renewed_tokens["refresh_token"] != first_refresh_token
        renewed_me = client.get(
            "/api/v1/users/me",
            headers={"Authorization": f"Bearer {renewed_tokens['access_token']}"},
        )
        assert renewed_me.status_code == 200
        # A refresh token is not an access token
        refresh_as_bearer = client.get(
            "/api/v1/users/me",
            headers={"Authorization": f"Bearer {renewed_tokens['refresh_token']}"},
        )
        assert refresh_as_bearer.status_code == 401

        # Presenting a used refresh token again revokes the whole session
        assert oauth_error(refresh(first_refresh_token)) == "invalid_grant"
        assert oauth_error(refresh(renewed_tokens["refresh_token"])) == "invalid_grant"
        assert oauth_error(refresh("not-a-token")) == "invalid_grant"

        # An expired refresh token is refused
        expiring = login("jane@example.com", "S3cret-pass").json()
        engine = create_db_engine(database_url, echo=False)
        with create_session_factory(engine)() as session:
            session.execute(
                update(RefreshToken).values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
            )
            session.commit()
        engine.dispose()
        assert oauth_error(refresh(expiring["refresh_token"])) == "invalid_grant"

        # Revocation (RFC 7009) ends one session, leaves the others, and
        # answers 200 for unknown tokens; access tokens cannot be revoked
        revoked_session = login("jane@example.com", "S3cret-pass").json()
        other_session = login("jane@example.com", "S3cret-pass").json()
        revocation = client.post("/auth/revoke", data={"token": revoked_session["refresh_token"]})
        assert revocation.status_code == 200
        assert oauth_error(refresh(revoked_session["refresh_token"])) == "invalid_grant"
        jane_session = refresh(other_session["refresh_token"]).json()
        assert client.post("/auth/revoke", data={"token": "not-a-token"}).status_code == 200
        access_revocation = client.post(
            "/auth/revoke", data={"token": revoked_session["access_token"]}
        )
        assert oauth_error(access_revocation) == "unsupported_token_type"
        assert oauth_error(client.post("/auth/revoke", data={})) == "invalid_request"

        # --- Browser sessions: the refresh token travels in an HttpOnly cookie ---
        def cookie_header(response) -> str:
            return response.headers["set-cookie"].lower()

        remembered = client.post(
            "/auth/session",
            json={"email": "jane@example.com", "password": "S3cret-pass", "remember": True},
        )
        assert remembered.status_code == 200
        assert remembered.headers["cache-control"] == "no-store"
        assert "refresh_token" not in remembered.json()
        assert remembered.json()["expires_in"] == 30 * 60
        set_cookie = cookie_header(remembered)
        for attribute in ("httponly", "samesite=strict", "path=/auth/session", "max-age="):
            assert attribute in set_cookie
        first_cookie = client.cookies["microlms_refresh_token"]

        # The cookie renews the session and is rotated
        renewed_session = client.post("/auth/session/refresh", json={"remember": True})
        assert renewed_session.status_code == 200
        renewed_me = client.get(
            "/api/v1/users/me",
            headers={"Authorization": f"Bearer {renewed_session.json()['access_token']}"},
        )
        assert renewed_me.status_code == 200
        assert client.cookies["microlms_refresh_token"] != first_cookie

        # Logout revokes the session and clears the cookie
        assert client.delete("/auth/session").status_code == 204
        assert "microlms_refresh_token" not in client.cookies
        # Present the revoked cookie again, as a browser that kept it would
        client.cookies.set(
            "microlms_refresh_token",
            renewed_session.cookies["microlms_refresh_token"],
            domain="testserver.local",
            path="/auth/session",
        )
        ended = client.post("/auth/session/refresh", json={"remember": True})
        assert oauth_error(ended) == "invalid_grant"
        # A refused renewal clears the cookie too
        assert "microlms_refresh_token" not in client.cookies
        assert oauth_error(client.post("/auth/session/refresh", json={"remember": True})) == (
            "invalid_request"
        )

        # Without remember, the cookie lasts until the browser closes
        browser_only = client.post(
            "/auth/session",
            json={"email": "jane@example.com", "password": "S3cret-pass", "remember": False},
        )
        assert "max-age=" not in cookie_header(browser_only)
        assert client.delete("/auth/session").status_code == 204

        # Refused logins answer like the password grant, and only JSON is
        # accepted, which another site cannot send without a CORS preflight
        refused_session = client.post(
            "/auth/session",
            json={"email": "jane@example.com", "password": "wrong", "remember": False},
        )
        assert refusal(refused_session) == "invalid_credentials"
        form_session = client.post(
            "/auth/session",
            data={"email": "jane@example.com", "password": "S3cret-pass", "remember": "true"},
        )
        assert form_session.status_code == 422

        me = client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {access_token}"})
        assert me.status_code == 200
        assert me.json()["id"] == user_id
        assert me.json()["email"] == "jane@example.com"
        assert me.json()["orgs"] == [
            {
                "org_id": default_org_id(database_url),
                "name": DEFAULT_ORG_SLUG,
                "roles": ["learner"],
            }
        ]

        # Malformed and missing tokens are rejected
        assert client.get("/api/v1/users/me").status_code == 401
        garbage = client.get("/api/v1/users/me", headers={"Authorization": "Bearer not-a-jwt"})
        assert garbage.status_code == 401

        # --- Self-service account management ---
        jane_headers = {"Authorization": f"Bearer {access_token}"}
        assert me.json()["full_name"] == "Jane"

        # Own profile edit; the sign-in email is not part of it
        renamed = client.put(
            "/api/v1/users/me",
            headers=jane_headers,
            json={"email": "thief@example.com", "full_name": "Jane Doe", "lang": "en"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["full_name"] == "Jane Doe"
        assert renamed.json()["lang"] == "en"
        assert renamed.json()["email"] == "jane@example.com"

        # Own email change requires the current password; an email already
        # registered elsewhere is refused
        assert (
            client.post(
                "/auth/register",
                json={
                    "email": "taken@example.com",
                    "password": "S3cret-pass",
                    "full_name": "Other",
                },
            ).status_code
            == 201
        )

        def change_email(current_password: str, new_email: str):
            return client.put(
                "/auth/me/email",
                headers=jane_headers,
                json={"current_password": current_password, "new_email": new_email},
            )

        wrong_password = change_email("wrong", "thief@example.com")
        assert wrong_password.status_code == 403
        assert wrong_password.json()["detail"]["reason"] == "invalid_credentials"
        assert change_email("S3cret-pass", "taken@example.com").status_code == 409
        assert change_email("S3cret-pass", "jane.doe@example.com").status_code == 204
        assert client.get("/api/v1/users/me", headers=jane_headers).json()["email"] == (
            "jane.doe@example.com"
        )
        assert login("jane.doe@example.com", "S3cret-pass").status_code == 200
        assert change_email("S3cret-pass", "jane@example.com").status_code == 204

        # Own password change requires the current password
        assert (
            client.put(
                "/auth/me/password",
                headers=jane_headers,
                json={"current_password": "wrong", "new_password": "N3w-secret"},
            ).status_code
            == 403
        )
        assert (
            client.put(
                "/auth/me/password",
                headers=jane_headers,
                json={"current_password": "S3cret-pass", "new_password": "N3w-secret"},
            ).status_code
            == 204
        )
        assert refusal(login("jane@example.com", "S3cret-pass")) == "invalid_credentials"
        assert login("jane@example.com", "N3w-secret").status_code == 200
        # A new password ends every session opened with the old one
        assert oauth_error(refresh(jane_session["refresh_token"])) == "invalid_grant"

        # --- Credential management and lockout ---
        # Bootstrap an admin: registration only creates learners, so the role
        # is granted directly against the database (like a real deployment)
        admin_id = client.post(
            "/auth/register",
            json={"email": "root@example.com", "password": "S3cret-pass", "full_name": "Root"},
        ).json()["user_id"]
        engine = create_db_engine(database_url, echo=False)
        with create_session_factory(engine)() as session:
            session.add(UserRoleAssignment(user_id=uuid.UUID(admin_id), role="admin"))
            session.commit()
        engine.dispose()

        def bearer(email: str, password: str) -> dict:
            return {"Authorization": f"Bearer {login(email, password).json()['access_token']}"}

        admin_headers = bearer("root@example.com", "S3cret-pass")
        org_id = client.post(
            "/api/v1/orgs", headers=admin_headers, json={"slug": "acme", "name": "Acme"}
        ).json()["id"]

        def new_user(email: str, roles: list[str], user_org: int | None) -> str:
            return client.post(
                "/api/v1/users",
                headers=admin_headers,
                json={"email": email, "full_name": email, "roles": roles, "org_id": user_org},
            ).json()["id"]

        manager_id = new_user("mgr@acme.example", ["manager"], org_id)
        employee_id = new_user("emp@acme.example", ["learner"], org_id)
        author_id = new_user("author@acme.example", ["instructor"], org_id)
        outsider_id = new_user("out@example.com", ["learner"], default_org_id(database_url))

        # A sign-in email belongs to one account only
        duplicate = client.post(
            "/api/v1/users",
            headers=admin_headers,
            json={
                "email": "emp@acme.example",
                "full_name": "Dup",
                "roles": ["learner"],
                "org_id": org_id,
            },
        )
        assert duplicate.status_code == 409

        # Management-created accounts have no password until one is set
        assert refusal(login("mgr@acme.example", "anything")) == "invalid_credentials"
        provisioned = client.put(
            f"/auth/users/{manager_id}/password",
            headers=admin_headers,
            json={"password": "Mgr-pass-1"},
        )
        assert provisioned.status_code == 204
        manager_headers = bearer("mgr@acme.example", "Mgr-pass-1")

        # A manager provisions their org's users, but nobody outside the org
        # and no account holding a permission the manager lacks
        assert (
            client.put(
                f"/auth/users/{employee_id}/password",
                headers=manager_headers,
                json={"password": "Emp-pass-1"},
            ).status_code
            == 204
        )
        assert login("emp@acme.example", "Emp-pass-1").status_code == 200
        assert (
            client.put(
                f"/auth/users/{outsider_id}/password",
                headers=manager_headers,
                json={"password": "x"},
            ).status_code
            == 403
        )
        assert (
            client.put(
                f"/auth/users/{author_id}/password",
                headers=manager_headers,
                json={"password": "x"},
            ).status_code
            == 403
        )

        # Lockout: after max_failed_logins wrong passwords, even the correct
        # one is refused until the lockout expires or the password is reset
        for _ in range(3):
            assert refusal(login("emp@acme.example", "wrong")) == "invalid_credentials"
        assert refusal(login("emp@acme.example", "Emp-pass-1")) == "account_locked"
        assert (
            client.put(
                f"/auth/users/{employee_id}/password",
                headers=manager_headers,
                json={"password": "Emp-pass-2"},
            ).status_code
            == 204
        )
        employee_session = login("emp@acme.example", "Emp-pass-2").json()
        employee_headers = {"Authorization": f"Bearer {employee_session['access_token']}"}

        # Deactivation kills logins, already-issued access tokens and the
        # sessions; reactivation restores logins and the access tokens still
        # running, not the ended sessions
        assert (
            client.post(
                f"/api/v1/users/{employee_id}/deactivate", headers=manager_headers
            ).status_code
            == 200
        )
        assert refusal(login("emp@acme.example", "Emp-pass-2")) == "account_deactivated"
        assert client.get("/api/v1/users/me", headers=employee_headers).status_code == 401
        assert (
            client.post(
                f"/api/v1/users/{employee_id}/activate", headers=manager_headers
            ).status_code
            == 200
        )
        assert client.get("/api/v1/users/me", headers=employee_headers).status_code == 200
        assert oauth_error(refresh(employee_session["refresh_token"])) == "invalid_grant"

        # --- Self-service password recovery ---
        # A known active email issues a reset token through the hooks
        recovery = client.post("/auth/password-recovery", json={"email": "emp@acme.example"})
        assert recovery.status_code == 200
        assert len(hooks.reset_requests) == 1
        reset_request = hooks.reset_requests[0]
        assert reset_request.user_id == uuid.UUID(employee_id)
        assert reset_request.email == "emp@acme.example"
        assert reset_request.lang is None

        # An unknown email gets the identical response and emits nothing
        unknown = client.post("/auth/password-recovery", json={"email": "ghost@example.com"})
        assert unknown.status_code == 200
        assert unknown.json() == recovery.json()
        assert len(hooks.reset_requests) == 1

        # The reset token is not an access token
        stolen = client.get(
            "/api/v1/users/me",
            headers={"Authorization": f"Bearer {reset_request.token}"},
        )
        assert stolen.status_code == 401

        # A garbage token is refused without confirming anything
        assert (
            client.post(
                "/auth/reset-password",
                json={"token": "not-a-token", "new_password": "Emp-pass-9"},
            ).status_code
            == 400
        )

        # An access token cannot reset the password either
        assert (
            client.post(
                "/auth/reset-password",
                json={"token": access_token, "new_password": "Emp-pass-9"},
            ).status_code
            == 400
        )

        # A refused password leaves the token usable
        assert (
            client.post(
                "/auth/reset-password",
                json={"token": reset_request.token, "new_password": "weak"},
            ).status_code
            == 422
        )

        # Consuming the real token sets the new password and unlocks logins
        for _ in range(3):
            assert refusal(login("emp@acme.example", "wrong")) == "invalid_credentials"
        assert (
            client.post(
                "/auth/reset-password",
                json={"token": reset_request.token, "new_password": "Emp-pass-3"},
            ).status_code
            == 204
        )
        assert refusal(login("emp@acme.example", "Emp-pass-2")) == "invalid_credentials"
        assert login("emp@acme.example", "Emp-pass-3").status_code == 200

        # The token works once
        assert (
            client.post(
                "/auth/reset-password",
                json={"token": reset_request.token, "new_password": "Emp-pass-4"},
            ).status_code
            == 400
        )
        assert login("emp@acme.example", "Emp-pass-3").status_code == 200

        # A newer request revokes the older link, and a password change
        # revokes every link still out there
        for _ in range(2):
            client.post("/auth/password-recovery", json={"email": "emp@acme.example"})
        older_token, newer_token = (event.token for event in hooks.reset_requests[-2:])
        assert (
            client.post(
                "/auth/reset-password",
                json={"token": older_token, "new_password": "Emp-pass-4"},
            ).status_code
            == 400
        )
        emp_token = login("emp@acme.example", "Emp-pass-3").json()["access_token"]
        assert (
            client.put(
                "/auth/me/password",
                headers={"Authorization": f"Bearer {emp_token}"},
                json={"current_password": "Emp-pass-3", "new_password": "Emp-pass-5"},
            ).status_code
            == 204
        )
        assert (
            client.post(
                "/auth/reset-password",
                json={"token": newer_token, "new_password": "Emp-pass-4"},
            ).status_code
            == 400
        )
        assert login("emp@acme.example", "Emp-pass-5").status_code == 200

        # --- Logins are stamped on the account ---
        employee = client.get(f"/api/v1/users/{employee_id}", headers=manager_headers).json()
        assert employee["created_at"] is not None
        assert employee["last_login_at"] is not None
        outsider = client.get(f"/api/v1/users/{outsider_id}", headers=admin_headers).json()
        assert outsider["last_login_at"] is None

        # --- Manager-sent password reset ---
        # A manager mails a reset link to their org's user without learning
        # the password; the link is the same single-use recovery token
        sent = client.post(f"/auth/users/{employee_id}/password-reset", headers=manager_headers)
        assert sent.status_code == 204
        manager_reset = hooks.reset_requests[-1]
        assert manager_reset.user_id == uuid.UUID(employee_id)
        assert manager_reset.email == "emp@acme.example"
        assert (
            client.post(
                "/auth/reset-password",
                json={"token": manager_reset.token, "new_password": "Emp-pass-6"},
            ).status_code
            == 204
        )
        assert login("emp@acme.example", "Emp-pass-6").status_code == 200
        audit = client.get(
            "/api/v1/audit?action=user.password_reset_requested", headers=admin_headers
        ).json()
        assert [(entry["actor_id"], entry["target_id"]) for entry in audit] == [
            (manager_id, employee_id)
        ]

        # Scoped like the management password set: nobody outside the org,
        # no account holding a permission the manager lacks
        sent_requests = len(hooks.reset_requests)
        outside = client.post(f"/auth/users/{outsider_id}/password-reset", headers=manager_headers)
        assert outside.status_code == 403
        escalating = client.post(f"/auth/users/{author_id}/password-reset", headers=manager_headers)
        assert escalating.status_code == 403

        # Nothing is sent to a deactivated account or one without an email
        client.post(f"/api/v1/users/{employee_id}/deactivate", headers=manager_headers)
        deactivated = client.post(
            f"/auth/users/{employee_id}/password-reset", headers=manager_headers
        )
        assert deactivated.status_code == 409
        client.post(f"/api/v1/users/{employee_id}/activate", headers=manager_headers)
        no_email_id = client.post(
            "/api/v1/users",
            headers=manager_headers,
            json={"email": None, "full_name": "No Mail", "roles": ["learner"], "org_id": org_id},
        ).json()["id"]
        no_email = client.post(f"/auth/users/{no_email_id}/password-reset", headers=manager_headers)
        assert no_email.status_code == 409
        assert len(hooks.reset_requests) == sent_requests

        # Without a sign-in email there is no password to set either, and
        # taking the email away removes the password sign-in
        no_email_password = client.put(
            f"/auth/users/{no_email_id}/password",
            headers=manager_headers,
            json={"password": "No-mail-1"},
        )
        assert no_email_password.status_code == 409
        leaver_id = new_user("leaver@acme.example", ["learner"], org_id)
        client.put(
            f"/auth/users/{leaver_id}/password",
            headers=admin_headers,
            json={"password": "Leave-pass-1"},
        )
        assert login("leaver@acme.example", "Leave-pass-1").status_code == 200
        unmailed = client.put(
            f"/api/v1/users/{leaver_id}",
            headers=admin_headers,
            json={"email": None, "full_name": "Leaver", "lang": None},
        )
        assert unmailed.json()["email"] is None
        assert refusal(login("leaver@acme.example", "Leave-pass-1")) == "invalid_credentials"


def test_jwt_lockout_bounds_password_guesses(tmp_path: Path, database_url: str) -> None:
    pytest.importorskip("jwt")
    from jwt.exceptions import InvalidKeyError

    # An HMAC secret shorter than the hash output is refused at startup
    with pytest.raises(InvalidKeyError):
        _jwt_backend("too-short")

    app = _jwt_app(tmp_path, database_url, RecordingHooks())
    with TestClient(app) as client:
        seed_default_org(database_url)
        for email in ("jane@example.com", "bob@example.com"):
            registration = client.post(
                "/auth/register",
                json={"email": email, "password": "S3cret-pass", "full_name": email},
            )
            assert registration.status_code == 201

        def login_refusal(email: str, password: str) -> str:
            response = client.post(
                "/auth/token",
                data={"grant_type": "password", "username": email, "password": password},
            )
            assert response.status_code == 400
            return response.json()["reason"]

        # Wrong current passwords on a password change count toward the
        # lockout, so a stolen access token cannot guess the password freely
        login = client.post(
            "/auth/token",
            data={
                "grant_type": "password",
                "username": "jane@example.com",
                "password": "S3cret-pass",
            },
        )
        jane_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        for _ in range(3):
            changed = client.put(
                "/auth/me/password",
                headers=jane_headers,
                json={"current_password": "wrong", "new_password": "N3w-secret"},
            )
            assert changed.status_code == 403
            assert changed.json()["detail"]["reason"] == "invalid_credentials"
        assert login_refusal("jane@example.com", "S3cret-pass") == "account_locked"
        locked_change = client.put(
            "/auth/me/password",
            headers=jane_headers,
            json={"current_password": "S3cret-pass", "new_password": "N3w-secret"},
        )
        assert locked_change.status_code == 403
        assert locked_change.json()["detail"]["reason"] == "account_locked"

        # Guesses sent all at once still get only max_failed_logins password
        # checks before the lockout refuses the rest
        with ThreadPoolExecutor(max_workers=20) as pool:
            reasons = list(pool.map(lambda _: login_refusal("bob@example.com", "wrong"), range(20)))
        assert reasons.count("invalid_credentials") == 3
        assert reasons.count("account_locked") == 17


def test_cookie_transport_auth_backend(tmp_path: Path, database_url: str) -> None:
    """The auth backend owns credential extraction, so a cookie transport fits."""
    admin_token = seed_admin(database_url)

    class CookieAuthBackend:
        """Resolve the anonymous token from a session cookie, not a bearer header."""

        def authenticate(self, session: OrmSession, request: Request) -> AuthenticatedUser:
            cookie = request.cookies.get("lms_session")
            if cookie is None:
                raise AuthenticationError("Request has no lms_session cookie")
            try:
                user = resolve_anonymous_token(session, uuid.UUID(cookie))
            except (ValueError, TokenNotFoundError) as error:
                raise AuthenticationError(str(error)) from error
            return authenticated_user_from(user)

        def router(self) -> None:
            return None

        def openapi_security(self) -> APIKey:
            return APIKey(**{"in": APIKeyIn.cookie}, name="lms_session")

    app = create_app(
        settings=Settings(database_url=database_url, registration_org_slug=DEFAULT_ORG_SLUG),
        engine=create_db_engine(database_url, echo=False),
        auth=CookieAuthBackend(),
        storage=FilesystemStorage(tmp_path / "storage"),
        hooks=NullEventHooks(),
        routers=ALL_CORE_ROUTERS,
        modules=[],
        schedules=[],
        access_policy=DEFAULT_ACCESS_POLICY,
    )

    with TestClient(app) as client:
        # The backend's scheme reaches the OpenAPI docs: cookie transport on
        # the authenticated endpoints, nothing on the public ones
        openapi = client.get("/openapi.json").json()
        scheme = openapi["components"]["securitySchemes"]["AuthBackend"]
        assert scheme == {"type": "apiKey", "in": "cookie", "name": "lms_session"}
        assert openapi["paths"]["/api/v1/users/me"]["get"]["security"] == [{"AuthBackend": []}]
        # The org-scoped course catalog resolves the current user, so the
        # security requirement is attached there too
        assert openapi["paths"]["/api/v1/courses"]["get"]["security"] == [{"AuthBackend": []}]
        assert "security" not in openapi["paths"]["/health"]["get"]

        # Without the cookie the request is rejected, even with a bearer
        # header: this backend's transport is the cookie alone.
        assert client.get("/api/v1/users/me").status_code == 401
        assert (
            client.get(
                "/api/v1/users/me",
                headers={"Authorization": f"Bearer {admin_token}"},
            ).status_code
            == 401
        )

        client.cookies.set("lms_session", str(admin_token))
        me = client.get("/api/v1/users/me")
        assert me.status_code == 200
        assert me.json()["email"] == "admin@example.com"
