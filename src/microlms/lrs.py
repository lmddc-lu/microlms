"""Server-side xAPI client shared by content modules (cmi5, scorm).

Connector to the Learning Record Store (external system): modules hold the
LRS credentials and talk to the LRS on the learner's behalf, so any
credentials handed to the browser stay opaque. Requires the optional httpx
dependency, so it is only imported by modules that talk to an LRS.
"""

import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, Request, status

from microlms.api.deps import SettingsDep
from microlms.config import Settings

logger = logging.getLogger(__name__)

XAPI_VERSION = "1.0.3"
SEND_ATTEMPTS = 3
# Only these request headers are relayed upstream. An allowlist
# is required because the LRS rejects browser headers it does not
# expect: lrsql answers 403 to any request carrying an ``Origin`` header, so
# forwarding the browser's Origin/Referer/Cookie/User-Agent would break every
# call. Concurrency headers (If-Match/If-None-Match) must pass through because
# the State API requires them on writes to existing documents.
_FORWARDED_REQUEST_HEADERS = frozenset(
    {
        "content-type",
        "x-experience-api-version",
        "if-match",
        "if-none-match",
        "accept",
        "accept-language",
    }
)


def build_actor(user_id: uuid.UUID, home_page: str) -> dict[str, object]:
    """xAPI Agent for a learner, keyed by account (core users may lack email)."""
    return {
        "objectType": "Agent",
        "account": {"homePage": home_page, "name": str(user_id)},
    }


def resolve_actor_home_page(settings: Settings, request: Request) -> str:
    """The homePage for canonical actors, without a trailing slash.

    The configured ``lrs_actor_home_page`` keeps one identity per user across
    every hostname the API is reachable through; a deployment that leaves it
    unset gets the request's base URL, which only matches statements written
    through the same hostname.
    """
    if settings.lrs_actor_home_page is not None:
        return settings.lrs_actor_home_page.rstrip("/")
    return str(request.base_url).rstrip("/")


class LrsResponseError(Exception):
    """Raised when the LRS answers with a body that is not a valid StatementResult."""


@dataclass(frozen=True, slots=True)
class StatementsPage:
    """One page of an xAPI StatementResult.

    ``more`` is the IRL of the next page, a path relative to the LRS host;
    None when the result is complete.
    """

    statements: list[dict[str, object]]
    more: str | None


class LrsClient:
    """Basic-auth xAPI client against a single Learning Record Store."""

    def __init__(self, lrs_url: str, api_key: str, api_secret: str) -> None:
        self._origin = lrs_url.rstrip("/")
        self._xapi_url = f"{self._origin}/xapi"
        self._auth = (api_key, api_secret)

    def put_state(
        self,
        activity_id: str,
        agent: Mapping[str, object],
        registration: str,
        state_id: str,
        document: Mapping[str, object],
    ) -> None:
        """Store a State API document (e.g. LMS.LaunchData), overwriting any
        existing one. Overwrite matters because LMS.LaunchData is rewritten each
        launch (its session id changes) under a registration reused across
        launches; the LRS requires a concurrency header once the document
        exists, so the current ETag is preflighted and supplied."""
        params = {
            "activityId": activity_id,
            "agent": json.dumps(agent),
            "registration": registration,
            "stateId": state_id,
        }
        etag = self.document_etag("activities/state", params)
        concurrency = {"If-Match": etag} if etag is not None else {"If-None-Match": "*"}
        self._send("put", "activities/state", params, document, concurrency)

    def post_statement(self, statement: Mapping[str, object]) -> None:
        """Store a single xAPI statement."""
        self._send("post", "statements", None, statement, {})

    def post_statements(self, statements: Sequence[Mapping[str, object]]) -> None:
        """Store a batch of xAPI statements in one request."""
        self._send("post", "statements", None, list(statements), {})

    def get_statements(self, params: Mapping[str, object]) -> StatementsPage:
        """Query stored statements with standard xAPI filters (agent, verb,
        activity, since, until, limit, ...)."""
        return self._fetch_statement_result(f"{self._xapi_url}/statements", params)

    def get_more(self, more_path: str) -> StatementsPage:
        """Fetch the next page of a StatementResult via its ``more`` IRL,
        which the LRS returns as a path relative to its host."""
        return self._fetch_statement_result(f"{self._origin}{more_path}", None)

    def _fetch_statement_result(
        self, url: str, params: Mapping[str, object] | None
    ) -> StatementsPage:
        headers = {"X-Experience-API-Version": XAPI_VERSION}
        last_error: httpx.HTTPError | None = None
        for attempt in range(1, SEND_ATTEMPTS + 1):
            try:
                response = httpx.request(
                    "get",
                    url,
                    params=params,
                    auth=self._auth,
                    headers=headers,
                    timeout=10.0,
                )
                response.raise_for_status()
                return _parse_statement_result(response.json())
            except httpx.HTTPError as error:
                last_error = error
                logger.warning(
                    "xAPI statement query failed",
                    extra={
                        "attempt": attempt,
                        "max_attempts": SEND_ATTEMPTS,
                        "url": url,
                        "error": str(error),
                    },
                )
        raise last_error  # loop always ran, so this is set

    def document_etag(self, path: str, params: str | Mapping[str, object]) -> str | None:
        """Return the current ETag of a document resource, or None if absent.

        Used to reconcile unconditional document writes: some AUs PUT State or
        Profile documents without a concurrency header, which the LRS rejects
        once the document exists. The caller preflights the ETag and supplies it.
        """
        response = httpx.request(
            "GET",
            f"{self._xapi_url}/{path}",
            params=params,
            auth=self._auth,
            headers={"X-Experience-API-Version": XAPI_VERSION},
            timeout=10.0,
        )
        if response.status_code == 200:
            return response.headers.get("ETag")
        return None

    def forward(
        self,
        method: str,
        path: str,
        query: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> httpx.Response:
        """Relay a proxied xAPI request to the LRS with server credentials.

        Returns the upstream response unchanged so the caller can pass the LRS
        status, headers and body straight back to the AU.
        """
        url = f"{self._xapi_url}/{path}"
        forwarded = {
            name: value
            for name, value in headers.items()
            if name.lower() in _FORWARDED_REQUEST_HEADERS
        }
        # Case-insensitive default: header names arrive lowercased, so a
        # title-cased setdefault would add a second copy and the LRS rejects a
        # duplicated X-Experience-API-Version.
        if not any(name.lower() == "x-experience-api-version" for name in forwarded):
            forwarded["X-Experience-API-Version"] = XAPI_VERSION

        last_error: httpx.TransportError | None = None
        for attempt in range(1, SEND_ATTEMPTS + 1):
            try:
                return httpx.request(
                    method,
                    url,
                    params=query,
                    content=body,
                    headers=forwarded,
                    auth=self._auth,
                    timeout=30.0,
                )
            except httpx.TransportError as error:
                last_error = error
                logger.warning(
                    "xAPI proxy forward failed",
                    extra={
                        "attempt": attempt,
                        "max_attempts": SEND_ATTEMPTS,
                        "method": method,
                        "url": url,
                        "error": str(error),
                    },
                )
        raise last_error  # loop always ran, so this is set

    def _send(
        self,
        method: str,
        path: str,
        params: Mapping[str, object] | None,
        json_body: Mapping[str, object] | Sequence[Mapping[str, object]],
        extra_headers: Mapping[str, str],
    ) -> None:
        url = f"{self._xapi_url}/{path}"
        headers = {"X-Experience-API-Version": XAPI_VERSION, **extra_headers}
        last_error: httpx.HTTPError | None = None
        for attempt in range(1, SEND_ATTEMPTS + 1):
            try:
                response = httpx.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    auth=self._auth,
                    headers=headers,
                    timeout=10.0,
                )
                response.raise_for_status()
                return
            except httpx.HTTPError as error:
                last_error = error
                logger.warning(
                    "xAPI request failed",
                    extra={
                        "attempt": attempt,
                        "max_attempts": SEND_ATTEMPTS,
                        "method": method,
                        "url": url,
                        "error": str(error),
                    },
                )
        raise last_error  # loop always ran, so this is set


def _parse_statement_result(body: object) -> StatementsPage:
    """Validate an xAPI StatementResult body; ``more`` is absent or an empty
    string when the result is complete."""
    if not isinstance(body, dict) or not isinstance(body.get("statements"), list):
        raise LrsResponseError(
            f"LRS response is not a StatementResult (expected a 'statements' list): {body!r}"
        )
    more = body.get("more")
    if more is not None and not isinstance(more, str):
        raise LrsResponseError(f"LRS StatementResult 'more' is not a string: {more!r}")
    return StatementsPage(statements=list(body["statements"]), more=more or None)


def get_lrs_client(settings: SettingsDep) -> LrsClient:
    """Build the LRS client, or fail clearly if the LRS is not configured."""
    if not (settings.lrs_url and settings.lrs_api_key and settings.lrs_api_secret):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "This module requires MICROLMS_LRS_URL, MICROLMS_LRS_API_KEY "
                "and MICROLMS_LRS_API_SECRET to be set"
            ),
        )
    return LrsClient(settings.lrs_url, settings.lrs_api_key, settings.lrs_api_secret)


LrsClientDep = Annotated[LrsClient, Depends(get_lrs_client)]
