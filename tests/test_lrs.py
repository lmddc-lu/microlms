"""The shared LRS client."""

import pytest


def test_lrs_forward_header_allowlist(monkeypatch) -> None:
    # lrsql rejects any forwarded request carrying an Origin header, so the
    # proxy must relay only xAPI-relevant headers and drop browser ones.
    httpx = pytest.importorskip("httpx")
    from microlms.lrs import LrsClient

    captured: dict[str, object] = {}

    def fake_request(method, url, **kwargs):
        captured["headers"] = kwargs["headers"]
        return httpx.Response(200, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", fake_request)

    LrsClient("http://lrs:8080", "k", "s").forward(
        "PUT",
        "activities/state",
        "stateId=x",
        {
            # Starlette yields header names lowercased
            "x-experience-api-version": "1.0.3",
            "origin": "http://localhost:8000",
            "referer": "http://localhost:8000/x",
            "cookie": "a=b",
            "accept-encoding": "gzip",
            "if-match": '"etag"',
            "content-type": "application/json",
        },
        b"{}",
    )
    relayed = [name.lower() for name in captured["headers"]]
    assert "origin" not in relayed
    assert "cookie" not in relayed
    assert "accept-encoding" not in relayed
    assert {"if-match", "content-type", "x-experience-api-version"} <= set(relayed)
    # The version header must appear exactly once (a duplicate is rejected)
    assert relayed.count("x-experience-api-version") == 1
