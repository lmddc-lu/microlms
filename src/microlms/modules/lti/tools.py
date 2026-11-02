"""LTI tool registration: the out-of-band security contract per tool.

Registering a tool records what the tool supplies (its OIDC login URL,
redirect URIs and JWK Set URL) and generates what the platform owes it: the
OAuth2 ``client_id`` and an immutable ``deployment_id``. Each registration is
deployed exactly once (the LTI single tenant model), so the two ids pair up
one to one.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from microlms.modules.lti.models import LtiClientAssertion, LtiTool


class LtiToolNotFoundError(Exception):
    """Raised when no registered tool matches the identifier."""


class ClientAssertionReplayedError(Exception):
    """Raised when a client assertion ``jti`` was already accepted."""


def register_tool(
    session: Session,
    name: str,
    oidc_login_url: str,
    redirect_uris: Sequence[str],
    jwks_url: str,
    share_identity: bool,
) -> LtiTool:
    """Register a tool, generating its client and deployment ids."""
    tool = LtiTool(
        name=name,
        client_id=str(uuid.uuid4()),
        deployment_id=str(uuid.uuid4()),
        oidc_login_url=oidc_login_url,
        redirect_uris=list(redirect_uris),
        jwks_url=jwks_url,
        share_identity=share_identity,
    )
    session.add(tool)
    session.flush()
    return tool


def list_tools(session: Session) -> list[LtiTool]:
    """Every registered tool, newest first."""
    return list(session.scalars(select(LtiTool).order_by(LtiTool.id.desc())))


def get_tool(session: Session, tool_id: int) -> LtiTool:
    tool = session.get(LtiTool, tool_id)
    if tool is None:
        raise LtiToolNotFoundError(f"No LTI tool with id {tool_id}")
    return tool


def set_tool_identity_sharing(session: Session, tool_id: int, share_identity: bool) -> LtiTool:
    """Decide whether the tool's launches share the learner's name and email;
    applies from the next launch on."""
    tool = get_tool(session, tool_id)
    tool.share_identity = share_identity
    session.flush()
    return tool


def get_tool_by_client_id(session: Session, client_id: str) -> LtiTool:
    tool = session.scalars(select(LtiTool).where(LtiTool.client_id == client_id)).first()
    if tool is None:
        raise LtiToolNotFoundError(f"No LTI tool with client_id {client_id!r}")
    return tool


def record_client_assertion(
    session: Session, tool_id: int, jti: str, expires_at: datetime, now: datetime
) -> None:
    """Remember an accepted assertion ``jti`` until it expires, refusing one
    already seen (Security Framework 4.1.1: the authorization server
    validates ``jti``). Expired entries are dropped first, so the table only
    holds assertions that could still be replayed.

    Unlike the other services this one owns its transaction: the unique
    constraint is the replay check, so the insert must commit (or roll back
    on conflict) right here - two concurrent requests with the same ``jti``
    cannot both pass. Callers invoke it with no other uncommitted work.
    """
    session.execute(delete(LtiClientAssertion).where(LtiClientAssertion.expires_at < now))
    session.add(LtiClientAssertion(tool_id=tool_id, jti=jti, expires_at=expires_at))
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise ClientAssertionReplayedError(
            f"Client assertion jti {jti!r} was already used by tool {tool_id}"
        ) from error
