"""Retry policy, error shape and the rate-limit snapshot.

Sleeps are stubbed out, so a case that waits out a 60-second rate-limit window
still runs in milliseconds — and the recorded delays are themselves asserted on,
which is the only way to tell "honoured the server's hint" from "backed off on
its own schedule and got lucky".
"""
from __future__ import annotations

import io

import httpx
import pytest

import anona.client as ac
from anona.client import AnonaClient, AnonaError, _ErrorTranslatingTransport, _RetryPolicy

BASE = "http://test.anona.local"
KEY = "anona_live_testkey"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def slept(monkeypatch):
    """Record what the client asked to wait for, instead of waiting."""
    waits: list[float] = []
    monkeypatch.setattr(ac.time, "sleep", lambda s: waits.append(s))
    return waits


def make_client(handler, *, max_retries: int = 2, max_wait: float = 60.0):
    client = AnonaClient(api_key=KEY, base_url=BASE)
    client._client = httpx.Client(
        transport=_ErrorTranslatingTransport(
            httpx.MockTransport(handler),
            _RetryPolicy(max_retries, max_wait),
            client.rate_limit,
        ),
        headers={"Authorization": f"Bearer {KEY}"},
    )
    return client


def responder(*responses):
    """Answer each call with the next response, repeating the last one."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        index = min(len(seen) - 1, len(responses) - 1)
        return responses[index]()

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


def rate_limited(**kwargs) -> httpx.Response:
    return httpx.Response(
        429, json={"error": {"code": "rate_limited", **kwargs}}, headers=kwargs.pop("headers", {})
    )


# ── Retry ─────────────────────────────────────────────────────────────────────


def test_429_is_retried_and_honours_retry_after_header(slept):
    handler = responder(
        lambda: httpx.Response(429, headers={"Retry-After": "17"}, json={}),
        lambda: httpx.Response(200, json={"results": []}),
    )
    make_client(handler).retrieve(space_id="s1", query="q")

    assert len(handler.seen) == 2
    assert slept == [17.0]


def test_429_reads_the_wait_out_of_the_body_when_there_is_no_header(slept):
    # The API carried window_seconds in the payload before it grew the header;
    # a client that only understood the header would ignore the one number the
    # server was already sending, and back off ~1.5s against a 60s window.
    handler = responder(
        lambda: httpx.Response(
            429, json={"error": {"code": "rate_limited", "limit": 60, "window_seconds": 60}}
        ),
        lambda: httpx.Response(200, json={"results": []}),
    )
    make_client(handler).retrieve(space_id="s1", query="q")

    assert len(handler.seen) == 2
    assert slept == [60.0]


def test_retry_after_is_capped_by_retry_max_wait(slept):
    handler = responder(
        lambda: httpx.Response(429, headers={"Retry-After": "600"}, json={}),
        lambda: httpx.Response(200, json={"results": []}),
    )
    make_client(handler, max_wait=30.0).retrieve(space_id="s1", query="q")

    assert slept == [30.0]


def test_429_on_a_write_is_retried_because_nothing_was_stored(slept):
    handler = responder(
        lambda: httpx.Response(429, headers={"Retry-After": "1"}, json={}),
        lambda: httpx.Response(200, json={"memory_id": "m1"}),
    )
    out = make_client(handler).record(space_id="s1", content="hi")

    assert len(handler.seen) == 2
    assert out["memory_id"] == "m1"


def test_5xx_on_a_write_is_never_retried(slept):
    # A 5xx can arrive after the write landed; replaying it stores twice.
    handler = responder(lambda: httpx.Response(503, json={"error": {"code": "engine_unavailable"}}))

    with pytest.raises(AnonaError) as exc:
        make_client(handler).record(space_id="s1", content="hi")

    assert exc.value.status_code == 503
    assert len(handler.seen) == 1
    assert slept == []


def test_5xx_on_a_read_is_retried(slept):
    handler = responder(
        lambda: httpx.Response(503, json={}),
        lambda: httpx.Response(503, json={}),
        lambda: httpx.Response(200, json={"items": [], "total": 0}),
    )
    make_client(handler).list_memories("s1")

    assert len(handler.seen) == 3
    assert len(slept) == 2 and all(0 < s < 5 for s in slept)


def test_uploads_are_not_retried_because_the_file_is_not_rewound():
    handler = responder(lambda: httpx.Response(429, headers={"Retry-After": "1"}, json={}))

    with pytest.raises(AnonaError) as exc:
        make_client(handler).upload_file("s1", io.BytesIO(b"hello"), filename="a.txt")

    assert exc.value.status_code == 429
    assert len(handler.seen) == 1


def test_max_retries_zero_turns_retrying_off():
    handler = responder(lambda: httpx.Response(429, headers={"Retry-After": "1"}, json={}))

    with pytest.raises(AnonaError):
        make_client(handler, max_retries=0).retrieve(space_id="s1", query="q")

    assert len(handler.seen) == 1


def test_retries_are_bounded_and_the_last_response_is_raised(slept):
    handler = responder(
        lambda: httpx.Response(
            429, headers={"Retry-After": "2"}, json={"error": {"code": "rate_limited"}}
        )
    )

    with pytest.raises(AnonaError) as exc:
        make_client(handler, max_retries=2).retrieve(space_id="s1", query="q")

    assert exc.value.status_code == 429
    assert exc.value.code == "rate_limited"
    assert len(handler.seen) == 3  # initial + 2 retries


# ── Error shape ───────────────────────────────────────────────────────────────


def test_error_carries_code_request_id_and_retry_after():
    handler = responder(
        lambda: httpx.Response(
            429,
            headers={"Retry-After": "9", "X-Request-ID": "req-123"},
            json={"error": {"code": "rate_limited", "message": "slow down"}},
        )
    )

    with pytest.raises(AnonaError) as exc:
        make_client(handler, max_retries=0).retrieve(space_id="s1", query="q")

    err = exc.value
    assert err.code == "rate_limited"
    assert err.request_id == "req-123"
    assert err.retry_after == 9
    # The original surface is unchanged.
    assert err.status_code == 429
    assert err.detail["error"]["message"] == "slow down"


def test_error_code_is_none_when_the_body_is_not_the_envelope():
    # Cloudflare strips the body of 502/504 and the API rewrites those to 503,
    # so a bodiless failure is a real shape the SDK has to survive.
    handler = responder(
        lambda: httpx.Response(503, headers={"X-Request-ID": "req-9"}, text="error code: 502")
    )

    with pytest.raises(AnonaError) as exc:
        make_client(handler, max_retries=0).list_spaces()

    assert exc.value.code is None
    assert exc.value.retry_after is None
    assert exc.value.request_id == "req-9"


# ── Rate-limit snapshot ───────────────────────────────────────────────────────


def test_rate_limit_snapshot_is_populated_from_response_headers():
    handler = responder(
        lambda: httpx.Response(
            200,
            json={"results": []},
            headers={
                "X-RateLimit-Limit": "60",
                "X-RateLimit-Remaining": "7",
                "X-RateLimit-Window": "60",
                "X-Credits-Remaining": "4321",
            },
        )
    )
    client = make_client(handler)
    client.retrieve(space_id="s1", query="q")

    assert client.rate_limit.limit == 60
    assert client.rate_limit.remaining == 7
    assert client.rate_limit.window_seconds == 60
    assert client.rate_limit.credits_remaining == 4321
    assert client.rate_limit.retry_after is None


def test_snapshot_starts_empty_and_survives_an_unmetered_call():
    # The config routes report no budget; a call to one must not blank out what
    # the last metered call reported.
    handler = responder(
        lambda: httpx.Response(
            200,
            json={"results": []},
            headers={"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "5"},
        ),
        lambda: httpx.Response(200, json={"space_id": "s1"}),
    )
    client = make_client(handler)
    assert client.rate_limit.limit is None

    client.retrieve(space_id="s1", query="q")
    client.get_chat_settings("s1")

    assert client.rate_limit.limit == 60
    assert client.rate_limit.remaining == 5
