"""Default email/password backend for projects without an external IdP."""

from fastapi import APIRouter, Request
from sqlalchemy.orm import Session

from microlms.auth.base import AuthenticatedUser


class LocalAuthBackend:
    """Email/password authentication against the core users table.

    Skeleton stub: token issuing/validation and password hashing are not
    implemented yet.
    """

    def authenticate(self, session: Session, request: Request) -> AuthenticatedUser:
        raise NotImplementedError(
            "LocalAuthBackend.authenticate is not implemented yet: "
            "token validation against the users table is pending"
        )

    def router(self) -> APIRouter | None:
        # No login/logout endpoints yet; None means nothing gets mounted
        return None

    def openapi_security(self) -> None:
        # Nothing to advertise while the backend is a stub
        return None
