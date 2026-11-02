"""Anonymous bearer-token backend for projects that need no login.

Learners register with no credentials at all and receive a uuid4 token. That
token is the sole credential: send it as ``Authorization: Bearer <token>``.
"""

import uuid

from fastapi import APIRouter, Request, status
from fastapi.openapi.models import HTTPBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from microlms.auth.base import (
    AuthenticatedUser,
    AuthenticationError,
    authenticated_user_from,
    bearer_token,
    ensure_active_user,
)
from microlms.domain.auth_tokens import (
    TokenNotFoundError,
    register_anonymous_learner,
    resolve_anonymous_token,
)
from microlms.events import commit_with_events


class AnonymousRegistration(BaseModel):
    """Response of anonymous registration: the token and the user it created."""

    token: uuid.UUID
    user_id: uuid.UUID


class AnonymousAuthBackend:
    """Issue and resolve opaque uuid4 tokens backed by the users table."""

    def authenticate(self, session: Session, request: Request) -> AuthenticatedUser:
        token = bearer_token(request)
        try:
            token_uuid = uuid.UUID(token)
        except ValueError as error:
            raise AuthenticationError("Token is not a valid uuid") from error
        try:
            user = resolve_anonymous_token(session, token_uuid)
        except TokenNotFoundError as error:
            raise AuthenticationError(str(error)) from error
        ensure_active_user(user)
        return authenticated_user_from(user)

    def router(self) -> APIRouter:
        # Imported here to avoid a cycle: api.deps imports the auth package.
        from microlms.api.deps import HooksDep, RegistrationOrgDep, SessionDep

        router = APIRouter()

        @router.post("/register", status_code=status.HTTP_201_CREATED)
        def register(
            session: SessionDep, hooks: HooksDep, org_id: RegistrationOrgDep
        ) -> AnonymousRegistration:
            """Register a nameless learner in the registration organization."""
            user, token = register_anonymous_learner(session, org_id)
            commit_with_events(session, hooks)
            return AnonymousRegistration(token=token, user_id=user.id)

        return router

    def openapi_security(self) -> HTTPBearer:
        return HTTPBearer(description="Token from POST /auth/register")
