"""LTI 1.3 message and service security (IMS Security Framework 1.0).

The platform signs every message it sends (the launch ``id_token``) and every
access token it issues with one RSA key (RS256, the framework's minimum and
default), configured as ``MICROLMS_LTI_PRIVATE_KEY``. Its public half is
published as a JWK Set whose ``kid`` is the key's RFC 7638 thumbprint, so a
new key always gets a new ``kid`` and tools caching the old key refetch.

Tools authenticate to the token endpoint with a client assertion (RFC 7523)
signed by their own key, verified against the JWK Set they registered; the
access token the platform returns is itself a signed JWT, so services verify
it without a token table.
"""

import base64
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Annotated

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from fastapi import Depends, HTTPException, status
from jwt.algorithms import RSAAlgorithm

from microlms.api.deps import SettingsDep

logger = logging.getLogger(__name__)

SIGNING_ALGORITHM = "RS256"

# Lifetimes: the id_token only needs to survive the browser auto-post; access
# tokens follow the framework's recommended 3600 seconds
ID_TOKEN_LIFETIME = timedelta(minutes=5)
ACCESS_TOKEN_LIFETIME = timedelta(hours=1)
# Tolerated clock skew between platform and tool on assertion timestamps
CLOCK_SKEW = timedelta(minutes=1)

CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"

# AGS scopes the platform grants: publish scores and read the link's line item
SCOPE_SCORE = "https://purl.imsglobal.org/spec/lti-ags/scope/score"
SCOPE_LINEITEM_READONLY = "https://purl.imsglobal.org/spec/lti-ags/scope/lineitem.readonly"
GRANTABLE_SCOPES = frozenset((SCOPE_SCORE, SCOPE_LINEITEM_READONLY))

KEY_SET_FETCH_ATTEMPTS = 3


class ClientAssertionError(Exception):
    """Raised when a tool's client assertion fails validation."""


class AccessTokenError(Exception):
    """Raised when a service request carries no valid platform access token."""


class ToolKeySetError(Exception):
    """Raised when a tool's key set URL answers with no usable JWK Set."""


@dataclass(frozen=True, slots=True)
class PlatformKey:
    """The platform's signing key, its public JWK and key id."""

    private_key: RSAPrivateKey
    kid: str
    public_jwk: dict[str, str]


@dataclass(frozen=True, slots=True)
class PlatformConfig:
    """What the platform needs to sign messages and tokens."""

    issuer: str
    key: PlatformKey


@dataclass(frozen=True, slots=True)
class ClientAssertion:
    """The replay-relevant fields of a validated client assertion."""

    jti: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AccessGrant:
    """The tool and scopes an access token was issued for."""

    client_id: str
    scopes: frozenset[str]


def _thumbprint(public_jwk: dict[str, str]) -> str:
    """RFC 7638 JWK thumbprint of an RSA public key (members e, kty, n in
    lexicographic order, no whitespace), base64url without padding."""
    canonical = json.dumps(
        {"e": public_jwk["e"], "kty": public_jwk["kty"], "n": public_jwk["n"]},
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@cache
def load_platform_key(private_key_pem: str) -> PlatformKey:
    """Parse the configured PEM private key into the signing key and its
    public JWK. Cached per PEM value, so the key is parsed once per process."""
    private_key = load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    if not isinstance(private_key, RSAPrivateKey):
        raise ValueError(
            "MICROLMS_LTI_PRIVATE_KEY must be an RSA private key: LTI 1.3 signs with RS256"
        )
    jwk = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    public_jwk = {"kty": jwk["kty"], "n": jwk["n"], "e": jwk["e"]}
    kid = _thumbprint(public_jwk)
    return PlatformKey(
        private_key=private_key,
        kid=kid,
        public_jwk={**public_jwk, "kid": kid, "alg": SIGNING_ALGORITHM, "use": "sig"},
    )


def get_platform_config(settings: SettingsDep) -> PlatformConfig:
    """The platform's issuer and signing key, or fail clearly if unset."""
    if not (settings.lti_issuer and settings.lti_private_key):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The lti module requires MICROLMS_LTI_ISSUER and MICROLMS_LTI_PRIVATE_KEY "
            "to be set",
        )
    return PlatformConfig(
        issuer=settings.lti_issuer, key=load_platform_key(settings.lti_private_key)
    )


PlatformConfigDep = Annotated[PlatformConfig, Depends(get_platform_config)]


def public_key_set(platform: PlatformConfig) -> dict[str, list[dict[str, str]]]:
    """The platform's JWK Set, as served at its key set URL."""
    return {"keys": [platform.key.public_jwk]}


def sign_message(platform: PlatformConfig, claims: dict[str, object]) -> str:
    """Sign a platform-originating message (e.g. the launch id_token)."""
    return jwt.encode(
        claims,
        platform.key.private_key,
        algorithm=SIGNING_ALGORITHM,
        headers={"kid": platform.key.kid, "typ": "JWT"},
    )


def issue_access_token(
    platform: PlatformConfig, client_id: str, scopes: frozenset[str], now: datetime
) -> str:
    """A signed access token for the tool, carrying its granted scopes."""
    claims = {
        "iss": platform.issuer,
        "sub": client_id,
        "iat": int(now.timestamp()),
        "exp": int((now + ACCESS_TOKEN_LIFETIME).timestamp()),
        "jti": secrets.token_urlsafe(16),
        "scope": " ".join(sorted(scopes)),
    }
    return sign_message(platform, claims)


def verify_access_token(platform: PlatformConfig, token: str) -> AccessGrant:
    """Validate an access token this platform issued: signature, issuer and
    expiry. Raises AccessTokenError otherwise."""
    try:
        claims = jwt.decode(
            token,
            platform.key.private_key.public_key(),
            algorithms=[SIGNING_ALGORITHM],
            issuer=platform.issuer,
            options={"require": ["iss", "sub", "exp", "scope"]},
        )
    except jwt.PyJWTError as error:
        raise AccessTokenError(f"Invalid access token: {error}") from error
    return AccessGrant(client_id=claims["sub"], scopes=frozenset(claims["scope"].split()))


def unverified_client_id(assertion: str) -> str:
    """The ``iss`` of a client assertion before its signature is checked, to
    find the registered tool whose key set verifies it."""
    try:
        claims = jwt.decode(assertion, options={"verify_signature": False})
    except jwt.PyJWTError as error:
        raise ClientAssertionError(f"Client assertion is not a JWT: {error}") from error
    issuer = claims.get("iss")
    if not isinstance(issuer, str) or not issuer:
        raise ClientAssertionError("Client assertion has no 'iss' claim")
    return issuer


def verify_client_assertion(
    assertion: str, client_id: str, key_set: jwt.PyJWKSet, token_url: str
) -> ClientAssertion:
    """Validate a tool's client assertion (Security Framework 4.1.1).

    Checks the signature against the tool's key set (by ``kid``), that
    ``iss`` and ``sub`` both are the tool's client id, that ``aud`` names the
    token endpoint, and that ``exp``, ``iat`` and ``jti`` are present with the
    token unexpired. Replay of ``jti`` is the caller's check.
    """
    try:
        kid = jwt.get_unverified_header(assertion).get("kid")
    except jwt.PyJWTError as error:
        raise ClientAssertionError(f"Client assertion is not a JWT: {error}") from error
    if not isinstance(kid, str):
        raise ClientAssertionError("Client assertion header has no 'kid'")
    try:
        key = key_set[kid]
    except KeyError as error:
        raise ClientAssertionError(f"The tool's key set has no key with kid {kid!r}") from error
    try:
        claims = jwt.decode(
            assertion,
            key,
            algorithms=[SIGNING_ALGORITHM],
            audience=token_url,
            issuer=client_id,
            subject=client_id,
            leeway=CLOCK_SKEW,
            options={"require": ["iss", "sub", "aud", "iat", "exp", "jti"]},
        )
    except jwt.PyJWTError as error:
        raise ClientAssertionError(f"Invalid client assertion: {error}") from error
    return ClientAssertion(
        jti=str(claims["jti"]),
        expires_at=datetime.fromtimestamp(claims["exp"], UTC),
    )


class ToolKeySetClient:
    """Connector fetching a registered tool's JWK Set (external system)."""

    def fetch(self, url: str) -> jwt.PyJWKSet:
        """Fetch and parse the key set, retrying transient failures."""
        response = self._get(url)
        try:
            return jwt.PyJWKSet.from_dict(response.json())
        except (ValueError, jwt.PyJWTError) as error:
            raise ToolKeySetError(
                f"Tool key set at {url} is not a usable JWK Set: {error}; "
                f"body: {response.text[:500]!r}"
            ) from error

    def _get(self, url: str) -> httpx.Response:
        last_error: httpx.HTTPError | None = None
        for attempt in range(1, KEY_SET_FETCH_ATTEMPTS + 1):
            try:
                response = httpx.get(url, timeout=10.0)
                response.raise_for_status()
                return response
            except httpx.HTTPError as error:
                last_error = error
                logger.warning(
                    "LTI tool key set fetch failed",
                    extra={
                        "attempt": attempt,
                        "max_attempts": KEY_SET_FETCH_ATTEMPTS,
                        "url": url,
                        "error": str(error),
                    },
                )
        raise last_error  # loop always ran, so this is set


def get_tool_key_set_client() -> ToolKeySetClient:
    return ToolKeySetClient()


ToolKeySetClientDep = Annotated[ToolKeySetClient, Depends(get_tool_key_set_client)]
