"""Authentication extension point and bundled backends."""

from microlms.auth.anonymous import AnonymousAuthBackend
from microlms.auth.base import (
    AuthBackend,
    AuthenticatedUser,
    AuthenticationError,
    authenticated_user_from,
    bearer_token,
    ensure_active_user,
)
from microlms.auth.local import LocalAuthBackend

__all__ = [
    "AnonymousAuthBackend",
    "AuthBackend",
    "AuthenticatedUser",
    "AuthenticationError",
    "LocalAuthBackend",
    "authenticated_user_from",
    "bearer_token",
    "ensure_active_user",
]
