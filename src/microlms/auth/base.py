"""Authentication contract implemented by client projects (SSO, SAML, OIDC, ...).

The core only needs one thing per request: turning the incoming request
into an authenticated user. The backend owns both credential extraction
(bearer header, cookie, ...) and validation, so the transport is as pluggable
as the credential scheme. Login/logout flows are backend-specific and exposed
as an optional router mounted under ``/auth``.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from fastapi import APIRouter, Request
from fastapi.openapi.models import SecurityScheme
from sqlalchemy.orm import Session

from microlms.domain.users import User


class AuthenticationError(Exception):
    """Raised when a request carries no credential resolvable to a user."""


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """Identity attached to a request after authentication.

    The stable identifier is ``id``. ``roles`` are the platform roles, held
    outside any organization; ``org_roles`` maps each organization the user
    belongs to onto the org roles held there (free-form labels; permissions
    are derived from both via the policy in :mod:`microlms.authz`). Build it
    from a loaded user with :func:`authenticated_user_from`.
    """

    id: uuid.UUID
    roles: frozenset[str]
    org_roles: Mapping[int, frozenset[str]]

    @property
    def org_ids(self) -> frozenset[int]:
        """The organizations the user belongs to."""
        return frozenset(self.org_roles)


def authenticated_user_from(user: User) -> AuthenticatedUser:
    """The request identity of a loaded user, with its platform and org roles."""
    return AuthenticatedUser(id=user.id, roles=user.roles, org_roles=user.org_roles)


def ensure_active_user(user: User) -> None:
    """Refuse authentication for a deactivated account.

    Backends call this after loading the user, so deactivation takes effect
    immediately: credentials issued earlier stop resolving on the next request.
    """
    if not user.is_active:
        raise AuthenticationError("Account is deactivated")


def bearer_token(request: Request) -> str:
    """Extract the bearer token from the request's Authorization header.

    Shared by backends whose credential is a bearer token. Raises
    AuthenticationError when the header is missing or uses another scheme.
    """
    header = request.headers.get("Authorization")
    if header is None:
        raise AuthenticationError("Request has no Authorization header")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthenticationError("Authorization header is not a bearer token")
    return token


class AuthBackend(Protocol):
    """Authentication extension point passed to ``create_app``."""

    def authenticate(self, session: Session, request: Request) -> AuthenticatedUser:
        """Authenticate the incoming request and return the user.

        The backend extracts its credential from the request (use the
        ``bearer_token`` helper for Authorization-header tokens, or read a
        cookie / assertion for other transports) and validates it. The
        request-scoped session lets database-backed backends look credentials
        up; stateless backends (e.g. signed JWTs) may ignore it. Raises
        AuthenticationError when the credential is missing, invalid or
        expired.
        """
        ...

    def router(self) -> APIRouter | None:
        """Backend-specific login/logout endpoints, mounted under ``/auth``.

        Return None when the backend has no HTTP surface (e.g. tokens are
        issued by an external identity provider).
        """
        ...

    def openapi_security(self) -> SecurityScheme | None:
        """Security scheme advertised in the OpenAPI schema, or None for none.

        Purely documentation: ``create_app`` attaches it to every operation
        that resolves the current user, which gives Swagger UI its Authorize
        button. Return a ``fastapi.openapi.models`` scheme matching the
        transport, e.g. ``HTTPBearer()`` for Authorization-header tokens or an
        ``APIKey`` in a cookie.
        """
        ...
