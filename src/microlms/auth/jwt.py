"""OAuth2 password flow issuing signed JWT access tokens.

Enabled from the optional ``jwt`` extra (``pip install microlms-core[jwt]``) and
passed to ``create_app``::

    from microlms.auth.jwt import JwtAuthBackend
    from microlms.auth.password_policy import PasswordPolicy

    auth = JwtAuthBackend(
        secret_key=settings.jwt_secret_key,
        algorithm="HS256",
        access_token_expire_minutes=30,
        refresh_token_expire_minutes=14 * 24 * 60,
        session_max_minutes=30 * 24 * 60,
        refresh_cookie_secure=True,
        max_failed_logins=5,
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

Access tokens are signed with HMAC (:data:`JwtAlgorithm`). ``secret_key`` must
be at least as long as the algorithm's hash output (32 bytes for HS256, RFC
7518 section 3.2): construction refuses a shorter one with
``jwt.exceptions.InvalidKeyError``. Generate one with ``openssl rand -hex 32``.

Every endpoint that sets a password (registration, management set/reset,
own change, recovery reset) refuses one breaking the policy with 422 and
``detail = {"message": ..., "unmet_rules": [...]}``, listing the broken
:class:`microlms.auth.password_policy.PasswordRule` values.

``POST /auth/token`` is an OAuth 2.0 token endpoint (RFC 6749) for public
clients, taking form-encoded requests:

- ``grant_type=password`` with ``username`` (the sign-in email) and ``password``
  starts a session (resource owner password credentials grant, section 4.3);
- ``grant_type=refresh_token`` with ``refresh_token`` continues it (section 6).

Both answer ``{access_token, token_type, expires_in, refresh_token}`` with
``Cache-Control: no-store``. The access token is a JWT whose ``sub`` claim is
the user id, valid ``access_token_expire_minutes``; validation checks the
signature and expiry without a database round-trip, then loads the user to
attach roles, so deactivation and role changes apply on the next
request. The refresh token is opaque and rotated on every use (see
:mod:`microlms.domain.refresh_tokens`): each one expires
``refresh_token_expire_minutes`` after it was issued, and none outlives
``session_max_minutes`` after the password login. A refused request is a 400
``{error, error_description}`` (section 5.2); a refused password login also
carries ``reason`` (:class:`LoginRefusal`), so a client can tell the user why.
``POST /auth/revoke`` (RFC 7009) ends the session of a refresh token, for a
logout. Setting a password by any path and deactivating the account end every
session of the user.

Browser apps use the session endpoints instead, which keep the refresh token
out of reach of the page's scripts: ``POST /auth/session`` (JSON ``{email,
password, remember}``) answers ``{access_token, token_type, expires_in}`` and
sets the refresh token as an ``HttpOnly``, ``SameSite=Strict`` cookie scoped to
``/auth/session``; ``POST /auth/session/refresh`` (JSON ``{remember}``) renews
the access token from that cookie, rotating it; ``DELETE /auth/session`` ends
the session and clears the cookie. With ``remember`` the cookie lasts until its
refresh token expires, otherwise until the browser closes. Sessions, rotation,
reuse detection and errors are the same as on ``/auth/token``. The cookie is
``Secure`` (sent over HTTPS only) when ``refresh_cookie_secure`` is set, which
every HTTPS deployment should do. The JSON bodies are a CSRF guard on top of
``SameSite``: another site cannot send them without a CORS preflight.

This backend is not exported from ``microlms.auth`` so the optional
hashing/JWT dependencies load only when it is imported.

Forgotten passwords: ``POST /auth/password-recovery`` issues a short-lived
single-use reset token and emits :class:`microlms.events.PasswordResetRequested`
for the client project to deliver (the core sends no mail);
``POST /auth/reset-password`` consumes the token and sets the new password.
Managers start the same flow for a user with
``POST /auth/users/{user_id}/password-reset``.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.openapi.models import OAuth2, OAuthFlowPassword, OAuthFlows
from fastapi.responses import JSONResponse
from pwdlib import PasswordHash
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from microlms.auth.base import AuthenticatedUser
from microlms.auth.password_policy import PasswordPolicy, unmet_password_rules
from microlms.auth.sessions import (
    NO_STORE_HEADERS,
    IssuedTokens,
    JwtAlgorithm,
    LoginRefusal,
    OAuthError,
    OAuthErrorBody,
    SessionIssuer,
    SessionToken,
    Token,
    required_parameter,
)
from microlms.domain.audit import record_audit
from microlms.domain.password_credentials import (
    InvalidPasswordResetTokenError,
    NoSignInEmailError,
    clear_login_failures,
    consume_password_reset_token,
    create_user_with_password,
    get_password_credential,
    issue_password_reset_token,
    set_password,
    start_login_attempt,
)
from microlms.domain.refresh_tokens import revoke_refresh_token
from microlms.domain.users import (
    EmailAlreadyRegisteredError,
    User,
    UserNotFoundError,
    UserRole,
    find_user_by_email,
    get_user,
    set_user_email,
)
from microlms.events import (
    EventHooks,
    PasswordResetRequested,
    commit_with_events,
    queue_event,
)

__all__ = [
    "JwtAlgorithm",
    "JwtAuthBackend",
]

_password_hash = PasswordHash.recommended()
# Verified against when no user matches, so login timing does not reveal whether
# an email exists.
_DUMMY_HASH = _password_hash.hash("dummy-password-for-timing")

# Identical whether the email is registered or not, so the recovery endpoint
# does not reveal which emails exist.
_RECOVERY_MESSAGE = "If that email is registered, a password reset was sent to it"


class SessionLogin(BaseModel):
    """Payload starting a browser session."""

    email: str
    password: str
    # Keep the session across browser restarts
    remember: bool


class PasswordRegistration(BaseModel):
    """Self-service registration payload; always creates a learner."""

    email: EmailStr
    password: str
    full_name: str


class RegisteredUser(BaseModel):
    """Response of a successful registration."""

    user_id: uuid.UUID
    email: EmailStr


class PasswordSet(BaseModel):
    """Payload of a management password set/reset."""

    password: str


class PasswordChange(BaseModel):
    """Payload of a self-service password change."""

    current_password: str
    new_password: str


class EmailChange(BaseModel):
    """Payload of a self-service sign-in email change."""

    current_password: str
    new_email: EmailStr


class PasswordRecoveryRequest(BaseModel):
    """Payload starting a self-service password recovery."""

    email: EmailStr


class PasswordRecoveryResponse(BaseModel):
    """Anti-enumeration acknowledgement of a recovery request."""

    message: str


class PasswordReset(BaseModel):
    """Payload consuming a recovery token to set a new password."""

    token: str
    new_password: str


class JwtAuthBackend:
    """Issue and validate signed JWT access tokens for email/password users."""

    def __init__(
        self,
        secret_key: str,
        algorithm: JwtAlgorithm,
        access_token_expire_minutes: int,
        refresh_token_expire_minutes: int,
        session_max_minutes: int,
        refresh_cookie_secure: bool,
        max_failed_logins: int,
        lockout_minutes: int,
        reset_token_expire_minutes: int,
        password_policy: PasswordPolicy,
    ) -> None:
        self._sessions = SessionIssuer(
            secret_key=secret_key,
            algorithm=algorithm,
            access_token_expire_minutes=access_token_expire_minutes,
            refresh_token_expire_minutes=refresh_token_expire_minutes,
            session_max_minutes=session_max_minutes,
            refresh_cookie_secure=refresh_cookie_secure,
        )
        self._max_failed_logins = max_failed_logins
        self._lockout_minutes = lockout_minutes
        self._reset_token_expire_minutes = reset_token_expire_minutes
        self._password_policy = password_policy

    def create_access_token(self, user_id: uuid.UUID) -> str:
        return self._sessions.create_access_token(user_id)

    def create_password_reset_token(
        self, session: Session, user_id: uuid.UUID
    ) -> tuple[str, datetime]:
        """Issue a single-use reset token and its expiry; the caller commits.

        The token is random and stored server-side (as a digest), not a JWT:
        it works once, only the latest one issued for a user works, and
        setting the password by any path revokes it. Not being a JWT, it can
        never authenticate a request either.
        """
        expires_at = datetime.now(UTC) + timedelta(minutes=self._reset_token_expire_minutes)
        return issue_password_reset_token(session, user_id, expires_at), expires_at

    def authenticate(self, session: Session, request: Request) -> AuthenticatedUser:
        return self._sessions.authenticate(session, request)

    def router(self) -> APIRouter:
        # Imported here to avoid a cycle: api.deps imports the auth package.
        from microlms.api.deps import (
            AccessPolicyDep,
            AuditDep,
            CurrentUserDep,
            HooksDep,
            ManagementScope,
            RegistrationOrgDep,
            SessionDep,
            forbid_unmanageable_target,
            get_account_in_scope,
            require_scoped_permission,
        )
        from microlms.authz import Permission

        router = APIRouter()

        @router.post("/register", status_code=status.HTTP_201_CREATED)
        def register(
            session: SessionDep,
            hooks: HooksDep,
            org_id: RegistrationOrgDep,
            registration: PasswordRegistration,
        ) -> RegisteredUser:
            """Register a learner in the registration organization."""
            self._ensure_password_meets_policy(registration.password)
            try:
                user = create_user_with_password(
                    session,
                    email=registration.email,
                    full_name=registration.full_name,
                    org_id=org_id,
                    roles={UserRole.LEARNER},
                    hashed_password=_password_hash.hash(registration.password),
                )
            except EmailAlreadyRegisteredError as error:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(error)
                ) from error
            commit_with_events(session, hooks)
            return RegisteredUser(user_id=user.id, email=registration.email)

        @router.post(
            "/token",
            response_model=Token,
            responses={status.HTTP_400_BAD_REQUEST: {"model": OAuthErrorBody}},
        )
        def issue_token(
            session: SessionDep,
            hooks: HooksDep,
            grant_type: Annotated[str | None, Form()] = None,
            username: Annotated[str | None, Form()] = None,
            password: Annotated[str | None, Form()] = None,
            refresh_token: Annotated[str | None, Form()] = None,
            scope: Annotated[str | None, Form()] = None,
        ) -> JSONResponse:
            """OAuth 2.0 token endpoint: the ``password`` grant starts a
            session (and stamps the account's ``last_login_at``), the
            ``refresh_token`` grant continues one with a rotated refresh
            token. Scopes are not supported, so a non-empty ``scope`` is
            refused."""
            try:
                if scope:
                    raise OAuthError("invalid_scope", "This server defines no scopes", None)
                match required_parameter("grant_type", grant_type):
                    case "password":
                        issued = self._password_grant(
                            session,
                            hooks,
                            required_parameter("username", username),
                            required_parameter("password", password),
                        )
                    case "refresh_token":
                        issued = self._sessions.refresh(
                            session, hooks, required_parameter("refresh_token", refresh_token)
                        )
                    case unsupported:
                        raise OAuthError(
                            "unsupported_grant_type",
                            f"Unsupported grant_type {unsupported!r}; "
                            "supported: password, refresh_token",
                            None,
                        )
            except OAuthError as error:
                return error.response()
            return JSONResponse(issued.token.model_dump(), headers=NO_STORE_HEADERS)

        @router.post(
            "/session",
            response_model=SessionToken,
            responses={status.HTTP_400_BAD_REQUEST: {"model": OAuthErrorBody}},
        )
        def start_session(session: SessionDep, hooks: HooksDep, login: SessionLogin) -> Response:
            """Start a browser session: the access token in the body, the
            refresh token in an HttpOnly cookie. Refused like the password
            grant of ``/auth/token``."""
            try:
                issued = self._password_grant(session, hooks, login.email, login.password)
            except OAuthError as error:
                return error.response()
            return self._sessions.session_response(issued, login.remember)

        self._sessions.add_session_routes(router)

        @router.post("/revoke", responses={status.HTTP_400_BAD_REQUEST: {"model": OAuthErrorBody}})
        def revoke_token(
            session: SessionDep,
            hooks: HooksDep,
            token: Annotated[str | None, Form()] = None,
            token_type_hint: Annotated[str | None, Form()] = None,
        ) -> Response:
            """OAuth 2.0 token revocation (RFC 7009): ends the session of a
            refresh token, for a logout.

            Answers 200 for an unknown or already revoked token too, so it
            confirms nothing about which tokens exist. Access tokens are
            self-contained and expire on their own; presenting one is refused
            as ``unsupported_token_type``. ``token_type_hint`` is accepted
            and not needed: refresh tokens are the only revocable type.
            """
            try:
                presented = required_parameter("token", token)
                if self._sessions.is_access_token(presented):
                    raise OAuthError(
                        "unsupported_token_type",
                        "Access tokens cannot be revoked; they expire on their own",
                        None,
                    )
            except OAuthError as error:
                return error.response()
            revoke_refresh_token(session, presented)
            commit_with_events(session, hooks)
            return Response(status_code=status.HTTP_200_OK)

        @router.post("/users/{user_id}/password-reset", status_code=status.HTTP_204_NO_CONTENT)
        def send_user_password_reset(
            session: SessionDep,
            scope: Annotated[
                ManagementScope,
                Depends(require_scoped_permission(Permission.USER_RESET_PASSWORD)),
            ],
            policy: AccessPolicyDep,
            audit: AuditDep,
            user_id: uuid.UUID,
        ) -> None:
            """Send a user a password reset on their behalf, so the manager
            never learns the new password.

            Issues the same single-use token as the self-service recovery and
            emits :class:`microlms.events.PasswordResetRequested` for the
            client project to deliver. Scoped like the management password
            set: org-scoped actors only for accounts whose every organization
            is in their reach, never accounts holding a permission the actor
            lacks. 409 when the account is
            deactivated or has no email to send to.
            """
            target = get_account_in_scope(session, scope, user_id)
            forbid_unmanageable_target(scope, target, policy)
            if not target.is_active:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Account is deactivated; activate it before resetting its password",
                )
            email = target.email
            if email is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Account has no email to send a password reset to",
                )
            token, expires_at = self.create_password_reset_token(session, target.id)
            queue_event(
                session,
                PasswordResetRequested(
                    user_id=target.id,
                    email=email,
                    full_name=target.full_name,
                    lang=target.lang,
                    token=token,
                    expires_at=expires_at,
                ),
            )
            # Commits the token with the entry, then dispatches the event
            audit.record("user.password_reset_requested", "user", str(target.id), None)

        @router.put("/users/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
        def set_user_password(
            session: SessionDep,
            scope: Annotated[
                ManagementScope,
                Depends(require_scoped_permission(Permission.USER_RESET_PASSWORD)),
            ],
            policy: AccessPolicyDep,
            audit: AuditDep,
            user_id: uuid.UUID,
            payload: PasswordSet,
        ) -> None:
            """Set or reset a user's password; org-scoped actors only for
            accounts whose every organization is in their reach.

            One endpoint covers provisioning a management-created account with
            its first password and resetting a forgotten one; it also clears
            any login lockout. Nobody touches an account holding a permission
            they lack. 409 when the account has no sign-in email to log in
            with.
            """
            target = get_account_in_scope(session, scope, user_id)
            forbid_unmanageable_target(scope, target, policy)
            self._ensure_password_meets_policy(payload.password)
            try:
                set_password(session, target.id, _password_hash.hash(payload.password))
            except NoSignInEmailError as error:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(error)
                ) from error
            audit.record("user.password_set", "user", str(target.id), None)

        @router.put("/me/password", status_code=status.HTTP_204_NO_CONTENT)
        def change_own_password(
            session: SessionDep,
            current_user: CurrentUserDep,
            audit: AuditDep,
            payload: PasswordChange,
        ) -> None:
            """Change the authenticated user's own password.

            Requires the current password, so a stolen bearer token alone
            cannot take over the account. Wrong current passwords count toward
            the login lockout, so that token cannot guess the password freely
            either, and a locked account changes its password only once the
            lockout ends. A successful change clears the lockout state.

            A refusal is a 403 with ``detail = {"message": ..., "reason": ...}``,
            ``reason`` being ``invalid_credentials`` or ``account_locked``
            (:class:`LoginRefusal`), so a client can tell the user why.
            """
            # Checked first, so a request bound to fail spends no attempt
            self._ensure_password_meets_policy(payload.new_password)
            self._ensure_current_password(session, current_user.id, payload.current_password)
            set_password(session, current_user.id, _password_hash.hash(payload.new_password))
            audit.record("user.password_changed", "user", str(current_user.id), None)

        @router.put("/me/email", status_code=status.HTTP_204_NO_CONTENT)
        def change_own_email(
            session: SessionDep,
            current_user: CurrentUserDep,
            audit: AuditDep,
            payload: EmailChange,
        ) -> None:
            """Change the authenticated user's own sign-in email.

            Requires the current password, like the password change: otherwise
            a stolen bearer token or session could point the email at an
            address its holder reads, then take the account over for good
            through password recovery. Wrong current passwords count toward
            the login lockout. The password is checked before the email, so
            only the account owner learns whether an email is already taken.

            A wrong password or a locked account is a 403 shaped like the
            password change's; an email another account signs in with is a
            409.
            """
            self._ensure_current_password(session, current_user.id, payload.current_password)
            try:
                set_user_email(session, current_user.id, payload.new_email)
            except EmailAlreadyRegisteredError as error:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(error)
                ) from error
            audit.record(
                "user.email_changed", "user", str(current_user.id), {"email": payload.new_email}
            )

        @router.post("/password-recovery")
        def request_password_recovery(
            session: SessionDep, hooks: HooksDep, payload: PasswordRecoveryRequest
        ) -> PasswordRecoveryResponse:
            """Start a self-service password reset for a forgotten password.

            When an active account has the email, a reset
            token is issued and handed to the client project's hooks for
            delivery (typically an emailed link). The response is the same
            either way so it never reveals whether an email is registered.
            """
            user = find_user_by_email(session, payload.email)
            if user is not None and user.is_active:
                token, expires_at = self.create_password_reset_token(session, user.id)
                queue_event(
                    session,
                    PasswordResetRequested(
                        user_id=user.id,
                        email=payload.email,
                        full_name=user.full_name,
                        lang=user.lang,
                        token=token,
                        expires_at=expires_at,
                    ),
                )
                commit_with_events(session, hooks)
            return PasswordRecoveryResponse(message=_RECOVERY_MESSAGE)

        @router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
        def reset_password(session: SessionDep, hooks: HooksDep, payload: PasswordReset) -> None:
            """Consume a recovery token and set the new password.

            The token works once: consuming it and setting the password
            commit together. Also clears any login lockout, like a
            management reset. Every failure is the same 400 so the endpoint
            confirms nothing about which tokens or accounts exist.
            """
            invalid_token_error = HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Password reset token is invalid or expired",
            )
            # Checked first, so a refused password does not burn the token
            self._ensure_password_meets_policy(payload.new_password)
            try:
                user_id = consume_password_reset_token(session, payload.token)
            except InvalidPasswordResetTokenError as error:
                raise invalid_token_error from error
            try:
                user = get_user(session, user_id)
            except UserNotFoundError as error:
                raise invalid_token_error from error
            if not user.is_active:
                raise invalid_token_error
            # The user proved account ownership via the token, so they are the
            # actor; entry and password change commit atomically below
            record_audit(
                session,
                actor_id=user.id,
                impersonated_id=None,
                action="user.password_reset",
                target_type="user",
                target_id=str(user.id),
                detail=None,
            )
            try:
                set_password(session, user.id, _password_hash.hash(payload.new_password))
            except NoSignInEmailError as error:
                raise invalid_token_error from error
            commit_with_events(session, hooks)

        return router

    def openapi_security(self) -> OAuth2:
        # Password flow against this backend's login endpoint, so Swagger UI's
        # Authorize button performs the whole email/password login.
        return OAuth2(
            flows=OAuthFlows(
                password=OAuthFlowPassword(tokenUrl="/auth/token", refreshUrl="/auth/token")
            )
        )

    def _ensure_password_meets_policy(self, password: str) -> None:
        """Refuse a new password breaking the policy, naming every broken rule."""
        unmet_rules = unmet_password_rules(self._password_policy, password)
        if unmet_rules:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "message": f"Password breaks the password policy: {', '.join(unmet_rules)}",
                    "unmet_rules": unmet_rules,
                },
            )

    def _ensure_current_password(self, session: Session, user_id: uuid.UUID, password: str) -> None:
        """Refuse a self-service change unless ``password`` is the user's
        current password, counting a wrong one toward the login lockout.

        A refusal is a 403 with ``detail = {"message": ..., "reason": ...}``,
        ``reason`` being ``invalid_credentials`` or ``account_locked``
        (:class:`LoginRefusal`), so a client can tell the user why.
        """
        credential = get_password_credential(session, user_id)
        refusal = (
            LoginRefusal.INVALID_CREDENTIALS
            if credential is None
            else self._check_password(session, user_id, credential.hashed_password, password)
        )
        if refusal is not None:
            message = (
                "Account is locked after repeated failed logins; try again later"
                if refusal is LoginRefusal.ACCOUNT_LOCKED
                else "Current password is incorrect"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"message": message, "reason": refusal},
            )

    def _password_grant(
        self, session: Session, hooks: EventHooks, email: str, password: str
    ) -> IssuedTokens:
        """Start a session for valid credentials."""
        user = self._authenticate(session, email, password)
        issued = self._sessions.start(session, user.id)
        commit_with_events(session, hooks)
        return issued

    def _check_password(
        self, session: Session, user_id: uuid.UUID, hashed_password: str, password: str
    ) -> LoginRefusal | None:
        """Check a password under the login lockout; None when correct,
        otherwise why it was refused.

        The attempt is counted before the hash is verified, so concurrent
        guesses cannot outrun the lockout, and a correct password clears the
        count. Locked means locked: not even the right password gets through.
        """
        if not start_login_attempt(
            session, user_id, self._max_failed_logins, self._lockout_minutes
        ):
            return LoginRefusal.ACCOUNT_LOCKED
        if not _password_hash.verify(password, hashed_password):
            return LoginRefusal.INVALID_CREDENTIALS
        clear_login_failures(session, user_id)
        return None

    def _authenticate(self, session: Session, email: str, password: str) -> User:
        credentials_error = OAuthError(
            "invalid_grant", "Incorrect email or password", LoginRefusal.INVALID_CREDENTIALS
        )
        user = find_user_by_email(session, email)
        credential = None if user is None else get_password_credential(session, user.id)
        if user is None or credential is None:
            # Spend the same work as a real verify to keep login timing
            # uniform; an account with no password yet cannot log in either
            _password_hash.verify(password, _DUMMY_HASH)
            raise credentials_error
        refusal = self._check_password(
            session, credential.user_id, credential.hashed_password, password
        )
        if refusal is LoginRefusal.ACCOUNT_LOCKED:
            raise OAuthError(
                "invalid_grant",
                "Account is locked after repeated failed logins; try again later",
                LoginRefusal.ACCOUNT_LOCKED,
            )
        if refusal is LoginRefusal.INVALID_CREDENTIALS:
            raise credentials_error
        if not user.is_active:
            raise OAuthError(
                "invalid_grant", "Account is deactivated", LoginRefusal.ACCOUNT_DEACTIVATED
            )
        return user
