"""Tests for ``crop_explorer.youtube_http``: the thin urllib transport for the YouTube Data API.

Requests go through real ``urllib`` machinery into :class:`synthetic_youtube.FakeHttpsHandler`,
which answers in-process; ``no_network`` makes any real connection fail the test.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

import pytest

from crop_explorer.youtube import API_KEY_HEADER, YouTubeError
from crop_explorer.youtube_http import urllib_transport
from synthetic_youtube import API_KEY, BASE_URL, FakeHttpsHandler, error_body

pytestmark = pytest.mark.usefixtures("no_network")

URL = f"{BASE_URL}/commentThreads?part=snippet%2Creplies&videoId=Syn_Video-1"
HEADERS = {API_KEY_HEADER: API_KEY}
TIMEOUT_S = 7.5


def answering(status: int, body: bytes) -> Callable[[str, Any], tuple[int, bytes]]:
    def respond(url: str, headers: Any) -> tuple[int, bytes]:
        return status, body

    return respond


def raising(exc: BaseException) -> Callable[[str, Any], tuple[int, bytes]]:
    def respond(url: str, headers: Any) -> tuple[int, bytes]:
        raise exc

    return respond


def test_get_sends_the_key_header_and_decodes_the_page() -> None:
    page = {"items": [], "nextPageToken": "token-1"}
    handler = FakeHttpsHandler(answering(200, json.dumps(page).encode("utf-8")))

    assert urllib_transport(TIMEOUT_S, handler)(URL, HEADERS) == page

    (request,) = handler.seen
    assert request.get_method() == "GET"
    assert request.full_url == URL
    assert request.timeout == TIMEOUT_S
    assert request.get_header(API_KEY_HEADER.capitalize()) == API_KEY
    assert API_KEY not in request.full_url
    assert request.data is None


def test_without_key_headers_the_request_carries_no_key() -> None:
    """``--key-via-proxy``: the transport adds nothing; the network proxy attaches the key."""
    handler = FakeHttpsHandler(answering(200, b'{"items": []}'))

    assert urllib_transport(TIMEOUT_S, handler)(URL, {}) == {"items": []}

    (request,) = handler.seen
    sent = {name.lower() for name, _ in request.header_items()}
    assert API_KEY_HEADER.lower() not in sent
    assert "key" not in request.full_url.lower().split("?", 1)[1]


def test_an_api_error_is_mapped_without_echoing_headers_or_body() -> None:
    body = error_body(403, "commentsDisabled", "Synthetic server detail")
    handler = FakeHttpsHandler(answering(403, body), {"X-Synthetic-Header": "header-value"})

    with pytest.raises(YouTubeError) as caught:
        urllib_transport(TIMEOUT_S, handler)(URL, HEADERS)

    message = str(caught.value)
    assert message.startswith("YouTube Data API error (HTTP 403, commentsDisabled): ")
    for leaked in (API_KEY, "header-value", "Synthetic server detail"):
        assert leaked not in message
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def test_a_redirect_is_refused_so_the_key_never_leaves_for_another_host() -> None:
    handler = FakeHttpsHandler(
        answering(302, b""), {"Location": "https://collector.example.invalid/steal"}
    )

    with pytest.raises(YouTubeError, match="redirect"):
        urllib_transport(TIMEOUT_S, handler)(URL, HEADERS)

    assert [request.full_url for request in handler.seen] == [URL]


@pytest.mark.parametrize("body", [b"<html>Synthetic proxy page</html>", b"[]"])
def test_a_success_that_is_not_a_json_object_fails_loudly(body: bytes) -> None:
    handler = FakeHttpsHandler(answering(200, body))

    with pytest.raises(YouTubeError, match="JSON"):
        urllib_transport(TIMEOUT_S, handler)(URL, HEADERS)


@pytest.mark.parametrize(
    "exc",
    [
        urllib.error.URLError("synthetic: name or service not known"),
        TimeoutError("timed out"),
        ConnectionResetError("synthetic: connection reset"),
        http.client.IncompleteRead(b"partial"),
    ],
)
def test_network_failures_become_one_clear_error(exc: BaseException) -> None:
    handler = FakeHttpsHandler(raising(exc))

    with pytest.raises(YouTubeError, match="YouTube Data API") as caught:
        urllib_transport(TIMEOUT_S, handler)(URL, HEADERS)

    assert API_KEY not in str(caught.value)


def test_the_real_https_handler_is_replaced_by_an_injected_one() -> None:
    """Sanity check of the test double: ``no_network`` would fail a real connection."""
    handler = FakeHttpsHandler(answering(200, b"{}"))
    opener = urllib.request.build_opener(handler)

    assert [h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler)] == [handler]
