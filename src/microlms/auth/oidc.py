"""OpenID Connect sign-in through an external identity provider.

Enabled from the optional ``oidc`` extra (``pip install microlms-core[oidc]``)
and passed to ``create_app``::

    import httpx
    from microlms.auth.oidc import OidcAuthBackend, OidcProvider, OidcRoleGrant

    auth = OidcAuthBackend(
        provider=OidcProvider(
            issuer="https://auth.example.com/application/o/microlms/",
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            redirect_uri="https://lms.example.com/auth/oidc/callback",
            scope="openid profile",
            http_client=httpx.Client(timeout=10.0),
        ),
        groups_claim="groups",
        group_roles={
            "lms-admins": [OidcRoleGrant(role="admin", org_slug=None)],
            "acme-learners": [OidcRoleGrant(role="learner", org_slug="acme")],
        },
        post_login_url="https://lms.example.com/",
        secret_key=settings.oidc_session_secret_key,
        algorithm="HS256",
        access_token_expire_minutes=30,
        refresh_token_expire_minutes=8 * 60,
        session_max_minutes=24 * 60,
        refresh_cookie_secure=True,
    )

The LMS is a confidential client of the provider, using the authorization
code flow with PKCE (S256), a ``state`` bound to the browser and a ``nonce``
bound to the ID token. ``GET /auth/oidc/login?remember=`` redirects the
browser to the provider; the provider redirects back to
``GET /auth/oidc/callback`` (register it as the client's redirect URI), which
exchanges the code, validates the ID token against the provider's key set,
reads the user's profile from the userinfo endpoint, then signs the user in
and redirects to ``post_login_url``. The provider's tokens are dropped once
the user is known: the user continues with an
LMS session (:mod:`microlms.auth.sessions`), its refresh token set as the
session cookie, so the frontend renews it with ``POST /auth/session/refresh``
and ends it with ``DELETE /auth/session`` like after a password login.

The provider owns the users it signs in. A user is known by the ID token's
issuer and ``sub`` (``lms_oidc_identity``); their name follows the ``name``
claim, their email the ``email`` claim (None when the provider sends none;
request the email scope), and their roles the groups in ``groups_claim``, all
read from the ID token or the userinfo answer, the groups mapped through
``group_roles`` (a platform role when ``org_slug`` is None, otherwise an org
role in that organization). Every sign-in replaces the user's roles with the
mapped ones, so a role edited in the LMS lasts until the next sign-in, and
changes at the provider reach the LMS at the next sign-in, not before. The
first sign-in creates the user. Groups mapping to no role are ignored; a
user whose groups map to none is refused.

A refused sign-in redirects to ``post_login_url`` with an ``error`` query
parameter (:class:`OidcLoginRefusal`). A misconfiguration (a mapped role the
access policy does not define, an organization slug that does not exist,
claims without ``name`` or ``groups_claim``) fails the callback with an
error naming it.

This backend is not exported from ``microlms.auth`` so the optional
dependencies load only when it is imported.
"""

import enum
import hmac
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated

import httpx
import jwt
from authlib.common.security import generate_token
from authlib.common.urls import add_params_to_uri
from authlib.oauth2.auth import ClientAuth
from authlib.oauth2.rfc6749.parameters import prepare_grant_uri, prepare_token_request
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from authlib.oidc.core import CodeIDToken
from fastapi import APIRouter, Cookie, Request, Response, status
from fastapi.openapi.models import HTTPBearer
from fastapi.responses import RedirectResponse
from joserfc import jwt as jose_jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.orm import Session

from microlms.auth.base import AuthenticatedUser
from microlms.auth.sessions import JwtAlgorithm, SessionIssuer
from microlms.authz import AccessPolicy
from microlms.domain.audit import record_audit
from microlms.domain.oidc_identities import (
    AssignedRoles,
    assigned_roles_of,
    create_user_with_oidc_identity,
    find_identity_user,
    sync_user_roles,
)
from microlms.domain.orgs import OrgNotFoundError, get_org_by_slug
from microlms.domain.users import EmailAlreadyRegisteredError, User, set_user_email, update_user
from microlms.events import commit_with_events

__all__ = [
    "JwtAlgorithm",
    "OidcAuthBackend",
    "OidcConfigurationError",
    "OidcLoginRefusal",
    "OidcProvider",
    "OidcProviderError",
    "OidcRoleGrant",
]

logger = logging.getLogger(__name__)

# Attempts per provider request before its last error is raised; only
# transport failures and 5xx answers are retried
PROVIDER_REQUEST_ATTEMPTS = 3

# Clock skew tolerated on the ID token's time claims, in seconds
_ID_TOKEN_LEEWAY_SECONDS = 60

# The cookie binding a sign-in to the browser that started it, sent back
# only to the callback. Lax, since the provider's redirect back is a
# cross-site top-level navigation.
_CALLBACK_PATH = "/auth/oidc/callback"
_LOGIN_COOKIE = "microlms_oidc_login"
_LOGIN_MAX_MINUTES = 10
# Audience of the login cookie's JWT, so it never passes for an access token
# signed with the same key (nor an access token for it)
_LOGIN_AUDIENCE = "microlms:oidc-login"


class OidcProviderError(Exception):
    """The provider could not be reached or answered with an error."""


class InvalidIdTokenError(Exception):
    """The ID token failed validation (signature, issuer, audience, nonce,
    expiry)."""


class OidcConfigurationError(Exception):
    """The deployment's OIDC configuration does not match the provider or the
    LMS (a mapped role or organization that does not exist, a missing
    claim)."""


class OidcLoginRefusal(enum.StrEnum):
    """Why a sign-in was refused, sent as the ``error`` query parameter of
    the redirect to ``post_login_url``."""

    # The sign-in was not started by this browser, or took too long
    INVALID_LOGIN_STATE = "invalid_login_state"
    # The provider refused or failed the sign-in (e.g. the user cancelled)
    PROVIDER_ERROR = "provider_error"
    # The ID token failed validation
    INVALID_ID_TOKEN = "invalid_id_token"
    # The user's groups map to no role
    NO_ROLE = "no_role"
    # Another user of the LMS has the email the provider sends
    EMAIL_TAKEN = "email_taken"
    ACCOUNT_DEACTIVATED = "account_deactivated"


class OidcRoleGrant(BaseModel):
    """A role a provider group grants: a platform role when ``org_slug`` is
    None, otherwise an org role in that organization."""

    model_config = ConfigDict(frozen=True)

    role: str
    org_slug: str | None


class _ProviderMetadata(BaseModel):
    """The parts of the provider's discovery document the LMS uses."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str
    jwks_uri: str
    id_token_signing_alg_values_supported: list[str]


class _TokenResponse(BaseModel):
    """The parts of the token endpoint's answer the LMS uses."""

    access_token: str
    id_token: str


class _IdentityClaims(BaseModel):
    """The claims identifying and describing the user."""

    iss: str
    sub: str
    name: str
    # Optional claim: present when the email scope was granted
    email: str | None = None


class _LoginState(BaseModel):
    """What the callback needs from the sign-in it completes."""

    state: str
    nonce: str
    code_verifier: str
    remember: bool


class OidcProvider:
    """Connector to an OpenID Connect provider (external system).

    The discovery document is fetched once, on first use. The key set is
    fetched at every sign-in, so key rotation at the provider needs nothing
    on the LMS side.
    """

    def __init__(
        self,
        issuer: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scope: str,
        http_client: httpx.Client,
    ) -> None:
        self.issuer = issuer
        self._client_auth = ClientAuth(client_id, client_secret, "client_secret_basic")
        self._client_id = client_id
        self._redirect_uri = redirect_uri
        self._scope = scope
        self._http = http_client
        self._metadata: _ProviderMetadata | None = None

    def authorization_url(self, state: str, nonce: str, code_verifier: str) -> str:
        """Where to send the browser to sign in."""
        return prepare_grant_uri(
            self._discover().authorization_endpoint,
            client_id=self._client_id,
            response_type="code",
            redirect_uri=self._redirect_uri,
            scope=self._scope,
            state=state,
            nonce=nonce,
            code_challenge=create_s256_code_challenge(code_verifier),
            code_challenge_method="S256",
        )

    def fetch_user_claims(self, code: str, code_verifier: str, nonce: str) -> Mapping[str, object]:
        """Exchange an authorization code and return the signed-in user's
        claims: the validated ID token's, plus the userinfo endpoint's.

        Providers may keep profile claims (name, groups) out of the ID token
        and serve them from the userinfo endpoint only (OpenID Connect Core
        section 5.4), so both are read; the ID token's win where both carry
        a claim.

        Raises OidcProviderError when a request fails or the userinfo is not
        about the ID token's subject, InvalidIdTokenError when the ID token
        is not valid for this client and sign-in.
        """
        metadata = self._discover()
        body = prepare_token_request(
            "authorization_code",
            code=code,
            redirect_uri=self._redirect_uri,
            code_verifier=code_verifier,
        )
        url, headers, body = self._client_auth.prepare(
            "POST",
            metadata.token_endpoint,
            {"Content-Type": "application/x-www-form-urlencoded"},
            body,
        )
        response = self._send("POST", url, headers, body)
        try:
            tokens = _TokenResponse.model_validate_json(response.content)
        except ValidationError as error:
            raise OidcProviderError(
                f"Token endpoint {url} answered without an access and ID token: {error}"
            ) from error
        key_set = KeySet.import_key_set(self._send("GET", metadata.jwks_uri, {}, None).json())
        try:
            token = jose_jwt.decode(
                tokens.id_token, key_set, algorithms=metadata.id_token_signing_alg_values_supported
            )
            claims = CodeIDToken(
                token.claims,
                token.header,
                {
                    "iss": {"essential": True, "value": self.issuer},
                    "aud": {"essential": True, "value": self._client_id},
                },
                {"nonce": nonce, "client_id": self._client_id, "access_token": tokens.access_token},
            )
            claims.validate(leeway=_ID_TOKEN_LEEWAY_SECONDS)
        except JoseError as error:
            raise InvalidIdTokenError(f"ID token from {self.issuer} is invalid: {error}") from error
        userinfo = self._send(
            "GET",
            metadata.userinfo_endpoint,
            {"Authorization": f"Bearer {tokens.access_token}"},
            None,
        ).json()
        # OpenID Connect Core section 5.3.2: userinfo about another subject
        # must not be used
        if not isinstance(userinfo, dict) or userinfo.get("sub") != claims["sub"]:
            raise OidcProviderError(
                f"Userinfo endpoint {metadata.userinfo_endpoint} did not answer for the "
                f"ID token's subject {claims['sub']!r}: {userinfo!r}"
            )
        return {**userinfo, **claims}

    def _discover(self) -> _ProviderMetadata:
        if self._metadata is None:
            url = f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"
            response = self._send("GET", url, {}, None)
            try:
                metadata = _ProviderMetadata.model_validate_json(response.content)
            except ValidationError as error:
                raise OidcProviderError(
                    f"Discovery document at {url} is not usable: {error}"
                ) from error
            # OpenID Connect Discovery 1.0 section 4.3
            if metadata.issuer != self.issuer:
                raise OidcConfigurationError(
                    f"Discovery document at {url} names the issuer {metadata.issuer!r}, "
                    f"not the configured {self.issuer!r}"
                )
            self._metadata = metadata
        return self._metadata

    def _send(
        self, method: str, url: str, headers: Mapping[str, str], body: str | None
    ) -> httpx.Response:
        """Send a request, retrying transport failures and 5xx answers with a
        warning each; raises OidcProviderError with the last failure."""
        last_error = ""
        for attempt in range(1, PROVIDER_REQUEST_ATTEMPTS + 1):
            try:
                response = self._http.request(method, url, headers=headers, content=body)
            except httpx.TransportError as error:
                last_error = f"{type(error).__name__}: {error}"
            else:
                if response.is_success:
                    return response
                if response.status_code < 500:
                    raise OidcProviderError(
                        f"{method} {url} answered {response.status_code}: {response.text[:500]!r}"
                    )
                last_error = f"status {response.status_code}: {response.text[:500]!r}"
            logger.warning(
                "OIDC provider request failed",
                extra={
                    "attempt": attempt,
                    "max_attempts": PROVIDER_REQUEST_ATTEMPTS,
                    "method": method,
                    "url": url,
                    "error": last_error,
                },
            )
        raise OidcProviderError(
            f"{method} {url} failed after {PROVIDER_REQUEST_ATTEMPTS} attempts: {last_error}"
        )


class OidcAuthBackend:
    """Sign users in through an OpenID Connect provider into LMS sessions."""

    def __init__(
        self,
        provider: OidcProvider,
        groups_claim: str,
        group_roles: Mapping[str, Sequence[OidcRoleGrant]],
        post_login_url: str,
        secret_key: str,
        algorithm: JwtAlgorithm,
        access_token_expire_minutes: int,
        refresh_token_expire_minutes: int,
        session_max_minutes: int,
        refresh_cookie_secure: bool,
    ) -> None:
        self._provider = provider
        self._groups_claim = groups_claim
        self._group_roles = group_roles
        self._post_login_url = post_login_url
        self._secret_key = secret_key
        self._algorithm = algorithm
        self._sessions = SessionIssuer(
            secret_key=secret_key,
            algorithm=algorithm,
            access_token_expire_minutes=access_token_expire_minutes,
            refresh_token_expire_minutes=refresh_token_expire_minutes,
            session_max_minutes=session_max_minutes,
            refresh_cookie_secure=refresh_cookie_secure,
        )

    def authenticate(self, session: Session, request: Request) -> AuthenticatedUser:
        return self._sessions.authenticate(session, request)

    def router(self) -> APIRouter:
        # Imported here to avoid a cycle: api.deps imports the auth package.
        from microlms.api.deps import AccessPolicyDep, HooksDep, SessionDep

        router = APIRouter()

        @router.get("/oidc/login", status_code=status.HTTP_303_SEE_OTHER)
        def start_login(remember: bool) -> Response:
            """Send the browser to the provider to sign in; ``remember`` keeps
            the LMS session across browser restarts."""
            login = _LoginState(
                state=generate_token(32),
                nonce=generate_token(32),
                code_verifier=generate_token(48),
                remember=remember,
            )
            response = RedirectResponse(
                self._provider.authorization_url(login.state, login.nonce, login.code_verifier),
                status_code=status.HTTP_303_SEE_OTHER,
            )
            response.set_cookie(
                _LOGIN_COOKIE,
                self._encode_login_state(login),
                max_age=_LOGIN_MAX_MINUTES * 60,
                path=_CALLBACK_PATH,
                secure=self._sessions.refresh_cookie_secure,
                httponly=True,
                samesite="lax",
            )
            return response

        @router.get("/oidc/callback", status_code=status.HTTP_303_SEE_OTHER)
        def complete_login(
            session: SessionDep,
            hooks: HooksDep,
            policy: AccessPolicyDep,
            state: str | None = None,
            code: str | None = None,
            error: str | None = None,
            error_description: str | None = None,
            login_cookie: Annotated[str | None, Cookie(alias=_LOGIN_COOKIE)] = None,
        ) -> Response:
            """Complete a sign-in the provider redirected back: start an LMS
            session and redirect to ``post_login_url``, or redirect there
            with an ``error`` (:class:`OidcLoginRefusal`)."""
            login = self._decode_login_state(login_cookie)
            if login is None or state is None or not hmac.compare_digest(state, login.state):
                return self._refused(OidcLoginRefusal.INVALID_LOGIN_STATE)
            if error is not None or code is None:
                logger.info(
                    "OIDC sign-in refused by the provider",
                    extra={"error": error, "error_description": error_description},
                )
                return self._refused(OidcLoginRefusal.PROVIDER_ERROR)
            try:
                claims = self._provider.fetch_user_claims(code, login.code_verifier, login.nonce)
            except OidcProviderError as provider_error:
                logger.warning("OIDC code exchange failed", extra={"error": str(provider_error)})
                return self._refused(OidcLoginRefusal.PROVIDER_ERROR)
            except InvalidIdTokenError as token_error:
                logger.warning("OIDC ID token refused", extra={"error": str(token_error)})
                return self._refused(OidcLoginRefusal.INVALID_ID_TOKEN)
            identity = self._identity_claims(claims)
            roles = self._assigned_roles(session, policy, self._groups(claims))
            if roles.is_empty():
                logger.info(
                    "OIDC sign-in refused: no role mapped",
                    extra={"issuer": identity.iss, "subject": identity.sub},
                )
                return self._refused(OidcLoginRefusal.NO_ROLE)
            try:
                user = self._signed_in_user(session, identity, roles)
            except EmailAlreadyRegisteredError:
                logger.info(
                    "OIDC sign-in refused: email taken by another user",
                    extra={"issuer": identity.iss, "subject": identity.sub},
                )
                return self._refused(OidcLoginRefusal.EMAIL_TAKEN)
            if not user.is_active:
                return self._refused(OidcLoginRefusal.ACCOUNT_DEACTIVATED)
            issued = self._sessions.start(session, user.id)
            commit_with_events(session, hooks)
            response = self._redirect(self._post_login_url)
            self._sessions.set_refresh_cookie(response, issued, login.remember)
            return response

        self._sessions.add_session_routes(router)
        return router

    def openapi_security(self) -> HTTPBearer:
        return HTTPBearer(
            description="Access token from POST /auth/session/refresh, after signing in "
            "through GET /auth/oidc/login"
        )

    def _signed_in_user(
        self, session: Session, identity: _IdentityClaims, roles: AssignedRoles
    ) -> User:
        """The user signing in, created on their first sign-in, with the
        provider's name, email and roles; the caller commits.

        Raises EmailAlreadyRegisteredError when another user has the email.
        """
        user = find_identity_user(session, identity.iss, identity.sub)
        if user is None:
            return create_user_with_oidc_identity(
                session, identity.iss, identity.sub, identity.name, identity.email, roles
            )
        if user.full_name != identity.name:
            update_user(session, user.id, identity.name, user.lang)
        if user.email != identity.email:
            set_user_email(session, user.id, identity.email)
        if assigned_roles_of(user) != roles:
            user = sync_user_roles(session, user.id, roles)
            # The provider changed the roles, not an actor in the LMS
            record_audit(
                session,
                actor_id=None,
                impersonated_id=None,
                action="user.roles_synced",
                target_type="user",
                target_id=str(user.id),
                detail={
                    "issuer": identity.iss,
                    "platform": sorted(roles.platform),
                    "orgs": {
                        str(org_id): sorted(org_roles) for org_id, org_roles in roles.orgs.items()
                    },
                },
            )
        return user

    def _identity_claims(self, claims: Mapping[str, object]) -> _IdentityClaims:
        try:
            return _IdentityClaims.model_validate(claims)
        except ValidationError as error:
            raise OidcConfigurationError(
                f"Claims from {self._provider.issuer} lack a usable name; "
                f"request the profile scope: {error}"
            ) from error

    def _groups(self, claims: Mapping[str, object]) -> list[str]:
        groups = claims.get(self._groups_claim)
        if not isinstance(groups, list) or not all(isinstance(group, str) for group in groups):
            raise OidcConfigurationError(
                f"Claims from {self._provider.issuer} have no {self._groups_claim!r} claim "
                f"listing group names (got {groups!r}); configure the provider to include it"
            )
        return groups

    def _assigned_roles(
        self, session: Session, policy: AccessPolicy, groups: Sequence[str]
    ) -> AssignedRoles:
        """The roles ``groups`` map to."""
        grants = [grant for group in groups for grant in self._group_roles.get(group, ())]
        platform: set[str] = set()
        orgs: dict[int, set[str]] = {}
        for grant in grants:
            if grant.role not in policy.roles:
                raise OidcConfigurationError(
                    f"OIDC group mapping grants the role {grant.role!r}, "
                    "which the access policy does not define"
                )
            if grant.org_slug is None:
                platform.add(grant.role)
                continue
            try:
                org_id = get_org_by_slug(session, grant.org_slug).id
            except OrgNotFoundError as error:
                raise OidcConfigurationError(
                    f"OIDC group mapping names the organization {grant.org_slug!r}, "
                    "which does not exist"
                ) from error
            orgs.setdefault(org_id, set()).add(grant.role)
        return AssignedRoles(
            platform=frozenset(platform),
            orgs={org_id: frozenset(roles) for org_id, roles in orgs.items()},
        )

    def _encode_login_state(self, login: _LoginState) -> str:
        expires_at = datetime.now(UTC) + timedelta(minutes=_LOGIN_MAX_MINUTES)
        payload = {**login.model_dump(), "aud": _LOGIN_AUDIENCE, "exp": expires_at}
        return jwt.encode(payload, self._secret_key, algorithm=self._algorithm)

    def _decode_login_state(self, cookie: str | None) -> _LoginState | None:
        """The sign-in this browser started, or None when it has none, it
        expired or the cookie was tampered with."""
        if cookie is None:
            return None
        try:
            payload = jwt.decode(
                cookie,
                self._secret_key,
                algorithms=[self._algorithm],
                audience=_LOGIN_AUDIENCE,
                options={"require": ["exp", "aud"]},
            )
        except jwt.InvalidTokenError:
            return None
        return _LoginState.model_validate(payload)

    def _refused(self, refusal: OidcLoginRefusal) -> RedirectResponse:
        return self._redirect(add_params_to_uri(self._post_login_url, [("error", refusal)]))

    def _redirect(self, url: str) -> RedirectResponse:
        """A redirect that also ends the sign-in's login cookie."""
        response = RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(
            _LOGIN_COOKIE,
            path=_CALLBACK_PATH,
            secure=self._sessions.refresh_cookie_secure,
            httponly=True,
            samesite="lax",
        )
        return response
