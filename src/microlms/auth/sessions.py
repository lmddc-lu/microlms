"""LMS sessions shared by the backends that sign users in themselves.

However a user signs in (email/password in :mod:`microlms.auth.jwt`, an
OpenID Connect provider in :mod:`microlms.auth.oidc`), the backend then
starts the same kind of session through :class:`SessionIssuer`: a short-lived
signed JWT access token whose ``sub`` claim is the user id, plus an opaque
refresh token rotated on every use (see :mod:`microlms.domain.refresh_tokens`).
Each refresh token expires ``refresh_token_expire_minutes`` after it was
issued, and none outlives ``session_max_minutes`` after the sign-in.

Browser apps keep the refresh token out of reach of the page's scripts: it
travels in an ``HttpOnly``, ``SameSite=Strict`` cookie scoped to
``/auth/session``. ``POST /auth/session/refresh`` (JSON ``{remember}``) renews
the access token from that cookie, rotating it; ``DELETE /auth/session`` ends
the session and clears the cookie. With ``remember`` the cookie lasts until
its refresh token expires, otherwise until the browser closes. The cookie is
``Secure`` when ``refresh_cookie_secure`` is set, which every HTTPS deployment
should do.

Internal to the bundled backends: needs ``pyjwt``, which the ``jwt`` and
``oidc`` extras both install.
"""

import enum
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, get_args

import jwt
from fastapi import APIRouter, Cookie, Request, Response, status
from fastapi.responses import JSONResponse
from jwt.algorithms import get_default_algorithms
from jwt.exceptions import InvalidKeyError, InvalidTokenError
from pydantic import BaseModel
from sqlalchemy.orm import Session

from microlms.auth.base import (
    AuthenticatedUser,
    AuthenticationError,
    authenticated_user_from,
    bearer_token,
    ensure_active_user,
)
from microlms.domain.refresh_tokens import (
    InvalidRefreshTokenError,
    RefreshTokenReusedError,
    consume_refresh_token,
    issue_refresh_token,
    revoke_refresh_token,
)
from microlms.domain.users import UserNotFoundError, get_user, record_login
from microlms.events import EventHooks, commit_with_events

__all__: list[str] = []

# The HMAC algorithms signing access tokens with the shared secret key
JwtAlgorithm = Literal["HS256", "HS384", "HS512"]

# Claims every access token carries; a token missing one is refused
_REQUIRED_CLAIMS = ["exp", "sub"]

logger = logging.getLogger(__name__)

# Token responses carry credentials, so no cache may keep them (RFC 6749 5.1)
NO_STORE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}

# The browser session endpoints and the cookie carrying their refresh token,
# sent back only to them
SESSION_PATH = "/auth/session"
REFRESH_COOKIE = "microlms_refresh_token"


class Token(BaseModel):
    """OAuth 2.0 access token response (RFC 6749 section 5.1)."""

    access_token: str
    token_type: str
    # Lifetime of the access token in seconds
    expires_in: int
    refresh_token: str


class SessionToken(BaseModel):
    """Access token of a browser session; its refresh token is in a cookie."""

    access_token: str
    token_type: str
    # Lifetime of the access token in seconds
    expires_in: int


class SessionRefresh(BaseModel):
    """Payload renewing a browser session."""

    # Keep the session across browser restarts
    remember: bool


@dataclass(frozen=True)
class IssuedTokens:
    """The tokens of a started or renewed session."""

    token: Token
    refresh_expires_at: datetime


class LoginRefusal(enum.StrEnum):
    """Why a password login was refused, sent as the ``reason`` extension
    member of the ``invalid_grant`` error."""

    INVALID_CREDENTIALS = "invalid_credentials"
    ACCOUNT_LOCKED = "account_locked"
    ACCOUNT_DEACTIVATED = "account_deactivated"


class OAuthErrorBody(BaseModel):
    """OAuth 2.0 error response (RFC 6749 section 5.2, RFC 7009 2.2.1)."""

    error: str
    error_description: str
    reason: LoginRefusal | None


class OAuthError(Exception):
    """A refused token or revocation request, answered as a 400 error body."""

    def __init__(self, error: str, description: str, reason: LoginRefusal | None) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.reason = reason

    def response(self) -> JSONResponse:
        body: dict[str, str] = {"error": self.error, "error_description": self.description}
        if self.reason is not None:
            body["reason"] = self.reason
        return JSONResponse(body, status_code=status.HTTP_400_BAD_REQUEST, headers=NO_STORE_HEADERS)


def required_parameter(name: str, value: str | None) -> str:
    """A request parameter the grant needs, refused when missing or empty."""
    if not value:
        raise OAuthError("invalid_request", f"Missing required parameter: {name}", None)
    return value


class SessionIssuer:
    """Start, renew and validate LMS sessions for a signing-in backend."""

    def __init__(
        self,
        secret_key: str,
        algorithm: JwtAlgorithm,
        access_token_expire_minutes: int,
        refresh_token_expire_minutes: int,
        session_max_minutes: int,
        refresh_cookie_secure: bool,
    ) -> None:
        # Settings come from the environment, so the type alone does not
        # guarantee the value
        supported = get_args(JwtAlgorithm)
        if algorithm not in supported:
            raise ValueError(
                f"Unsupported JWT algorithm {algorithm!r}; supported: {', '.join(supported)}"
            )
        key_length_error = get_default_algorithms()[algorithm].check_key_length(secret_key.encode())
        if key_length_error is not None:
            raise InvalidKeyError(f"{key_length_error} Generate one with `openssl rand -hex 32`.")
        self._secret_key = secret_key
        self._algorithm = algorithm
        self._access_token_expire_minutes = access_token_expire_minutes
        self._refresh_token_expire_minutes = refresh_token_expire_minutes
        self._session_max_minutes = session_max_minutes
        self._refresh_cookie_secure = refresh_cookie_secure

    @property
    def refresh_cookie_secure(self) -> bool:
        """Whether cookies are sent over HTTPS only; backends setting cookies
        of their own follow it too."""
        return self._refresh_cookie_secure

    def create_access_token(self, user_id: uuid.UUID) -> str:
        now = datetime.now(UTC)
        expire = now + timedelta(minutes=self._access_token_expire_minutes)
        payload = {"sub": str(user_id), "iat": now, "exp": expire}
        return jwt.encode(payload, self._secret_key, algorithm=self._algorithm)

    def authenticate(self, session: Session, request: Request) -> AuthenticatedUser:
        """Resolve the request's bearer access token to an active user."""
        token = bearer_token(request)
        try:
            subject = self._access_token_subject(token)
        except InvalidTokenError as error:
            raise AuthenticationError("Token is invalid or expired") from error
        try:
            user_id = uuid.UUID(subject)
        except ValueError as error:
            raise AuthenticationError("Token subject is not a user id") from error
        try:
            user = get_user(session, user_id)
        except UserNotFoundError as error:
            raise AuthenticationError(str(error)) from error
        ensure_active_user(user)
        return authenticated_user_from(user)

    def start(self, session: Session, user_id: uuid.UUID) -> IssuedTokens:
        """Start a session for a user who just signed in, stamping their
        ``last_login_at``; the caller commits."""
        now = datetime.now(UTC)
        record_login(session, user_id, now)
        return self._issue_tokens(
            session, user_id, uuid.uuid4(), now + timedelta(minutes=self._session_max_minutes)
        )

    def refresh(self, session: Session, hooks: EventHooks, refresh_token: str) -> IssuedTokens:
        """Continue the session of a refresh token, rotating it; commits."""
        try:
            grant = consume_refresh_token(session, refresh_token)
        except RefreshTokenReusedError as error:
            # The family revocation must survive the refused request
            commit_with_events(session, hooks)
            logger.warning(
                "Refresh token reused, session revoked",
                extra={"user_id": str(error.user_id), "family_id": str(error.family_id)},
            )
            raise OAuthError("invalid_grant", str(error), None) from error
        except InvalidRefreshTokenError as error:
            raise OAuthError("invalid_grant", str(error), None) from error
        try:
            user = get_user(session, grant.user_id)
        except UserNotFoundError as error:
            raise OAuthError("invalid_grant", str(error), None) from error
        if not user.is_active:
            raise OAuthError("invalid_grant", "Account is deactivated", None)
        token = self._issue_tokens(session, user.id, grant.family_id, grant.family_expires_at)
        commit_with_events(session, hooks)
        return token

    def is_access_token(self, token: str) -> bool:
        """Whether ``token`` is an access token this issuer signed."""
        try:
            self._access_token_subject(token)
        except InvalidTokenError:
            return False
        return True

    def set_refresh_cookie(self, response: Response, issued: IssuedTokens, remember: bool) -> None:
        """Set the session's refresh token as its cookie: persistent until the
        refresh token expires when ``remember``, otherwise gone when the
        browser closes."""
        max_age = (
            int((issued.refresh_expires_at - datetime.now(UTC)).total_seconds())
            if remember
            else None
        )
        response.set_cookie(
            REFRESH_COOKIE,
            issued.token.refresh_token,
            max_age=max_age,
            path=SESSION_PATH,
            secure=self._refresh_cookie_secure,
            httponly=True,
            samesite="strict",
        )

    def session_response(self, issued: IssuedTokens, remember: bool) -> JSONResponse:
        """The access token of a browser session, with its refresh token set
        as the session cookie."""
        body = SessionToken(
            access_token=issued.token.access_token,
            token_type=issued.token.token_type,
            expires_in=issued.token.expires_in,
        )
        response = JSONResponse(body.model_dump(), headers=NO_STORE_HEADERS)
        self.set_refresh_cookie(response, issued, remember)
        return response

    def add_session_routes(self, router: APIRouter) -> None:
        """Mount the browser session renewal and logout endpoints."""
        # Imported here to avoid a cycle: api.deps imports the auth package.
        from microlms.api.deps import HooksDep, SessionDep

        @router.post(
            "/session/refresh",
            response_model=SessionToken,
            responses={status.HTTP_400_BAD_REQUEST: {"model": OAuthErrorBody}},
        )
        def refresh_session(
            session: SessionDep,
            hooks: HooksDep,
            payload: SessionRefresh,
            refresh_cookie: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
        ) -> Response:
            """Renew a browser session from its cookie, rotating the refresh
            token. Refused like the refresh token grant of ``/auth/token``;
            a refused renewal also clears the cookie."""
            try:
                issued = self.refresh(
                    session, hooks, required_parameter("refresh token cookie", refresh_cookie)
                )
            except OAuthError as error:
                refused = error.response()
                self._clear_refresh_cookie(refused)
                return refused
            return self.session_response(issued, payload.remember)

        @router.delete("/session", status_code=status.HTTP_204_NO_CONTENT)
        def end_session(
            session: SessionDep,
            hooks: HooksDep,
            refresh_cookie: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
        ) -> Response:
            """End a browser session and clear its cookie; a request without
            the cookie only clears it."""
            if refresh_cookie is not None:
                revoke_refresh_token(session, refresh_cookie)
                commit_with_events(session, hooks)
            ended = Response(status_code=status.HTTP_204_NO_CONTENT)
            self._clear_refresh_cookie(ended)
            return ended

    def _issue_tokens(
        self,
        session: Session,
        user_id: uuid.UUID,
        family_id: uuid.UUID,
        family_expires_at: datetime,
    ) -> IssuedTokens:
        """An access token plus the next refresh token of the session; the
        caller commits."""
        refresh_expires_at = min(
            datetime.now(UTC) + timedelta(minutes=self._refresh_token_expire_minutes),
            family_expires_at,
        )
        token = Token(
            access_token=self.create_access_token(user_id),
            token_type="bearer",
            expires_in=self._access_token_expire_minutes * 60,
            refresh_token=issue_refresh_token(
                session, user_id, family_id, refresh_expires_at, family_expires_at
            ),
        )
        return IssuedTokens(token=token, refresh_expires_at=refresh_expires_at)

    def _clear_refresh_cookie(self, response: Response) -> None:
        response.delete_cookie(
            REFRESH_COOKIE,
            path=SESSION_PATH,
            secure=self._refresh_cookie_secure,
            httponly=True,
            samesite="strict",
        )

    def _access_token_subject(self, token: str) -> str:
        """The ``sub`` claim of an access token this issuer signed; raises
        InvalidTokenError for a bad signature, an expired token or a missing
        claim."""
        payload = jwt.decode(
            token,
            self._secret_key,
            algorithms=[self._algorithm],
            options={"require": _REQUIRED_CLAIMS},
        )
        return payload["sub"]
