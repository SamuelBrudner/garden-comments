"""Synthetic YouTube Data API v3 pages and fake transports for the ``fetch-youtube`` tests.

Every comment, id and key here is invented for the tests: none is a real YouTube comment and
nothing touches the network. :class:`FakeApi` serves ``commentThreads`` and ``comments``
pages chained by ``pageToken`` and records each request; :class:`FakeHttpsHandler` puts it
behind urllib, so the real transport (``crop_explorer.youtube_http``) runs without a socket.
"""

from __future__ import annotations

import email.message
import io
import json
import urllib.request
import urllib.response
from collections.abc import Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qsl, urlsplit

VIDEO = "Syn_Video-1"
"""An invented 11-character video id."""
API_KEY = "synthetic-test-key-0123456789-not-a-real-key"
BASE_URL = "https://www.googleapis.com/youtube/v3"
PUBLISHED = "2026-09-01T10:00:00Z"
AUTHOR = "Synthetic Author Name"
AUTHOR_CHANNEL = "UCsyntheticAuthorChannel"
AVATAR = "https://example.invalid/synthetic-avatar.png"
LIKES = 4242
UNREGISTERED_CALLERS = (
    "Method doesn't allow unregistered callers (callers without established identity). "
    "Please use API Key or other form of API consumer identity to call this API."
)
"""The API's message for a request that carried no key (with reason ``forbidden``)."""

Page = list[dict[str, Any]]


def comment(
    comment_id: str, text: str, published: str = PUBLISHED, **snippet: Any
) -> dict[str, Any]:
    """A ``youtube#comment`` resource, with the author and metric fields the API also sends."""
    return {
        "kind": "youtube#comment",
        "etag": "synthetic-etag",
        "id": comment_id,
        "snippet": {
            "channelId": "UCsyntheticVideoChannel",
            "videoId": VIDEO,
            "textDisplay": text,
            "textOriginal": text,
            "authorDisplayName": AUTHOR,
            "authorProfileImageUrl": AVATAR,
            "authorChannelUrl": "https://example.invalid/synthetic-channel",
            "authorChannelId": {"value": AUTHOR_CHANNEL},
            "canRate": True,
            "viewerRating": "none",
            "likeCount": LIKES,
            "publishedAt": published,
            "updatedAt": published,
            **snippet,
        },
    }


def reply(parent_id: str, suffix: str, text: str, published: str = PUBLISHED) -> dict[str, Any]:
    """A reply resource: its id is ``<parent id>.<suffix>`` and it names its parent."""
    return comment(f"{parent_id}.{suffix}", text, published, parentId=parent_id)


def thread(
    top: dict[str, Any], *inline: dict[str, Any], total: int | None = None
) -> dict[str, Any]:
    """A ``youtube#commentThread``; ``total`` (``totalReplyCount``) defaults to inline count."""
    item: dict[str, Any] = {
        "kind": "youtube#commentThread",
        "etag": "synthetic-etag",
        "id": top["id"],
        "snippet": {
            "channelId": "UCsyntheticVideoChannel",
            "videoId": VIDEO,
            "topLevelComment": top,
            "canReply": True,
            "totalReplyCount": len(inline) if total is None else total,
            "isPublic": True,
        },
    }
    if inline:
        item["replies"] = {"comments": list(inline)}
    return item


@dataclass(frozen=True)
class ApiRequest:
    """One request the fake API received."""

    url: str
    endpoint: str
    params: dict[str, str]
    headers: dict[str, str]  # names lower-cased (urllib capitalises them its own way)


def parse_request(url: str, headers: Mapping[str, str], base_url: str) -> ApiRequest:
    """Split a request URL into endpoint and query parameters (each parameter given once)."""
    assert url.startswith(base_url + "/"), url
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
    params = dict(pairs)
    assert len(params) == len(pairs), f"repeated query parameter in {url}"
    endpoint = parts.path.rsplit("/", 1)[1]
    assert endpoint in {"commentThreads", "comments"}, url
    lowered = {name.lower(): value for name, value in headers.items()}
    return ApiRequest(url=url, endpoint=endpoint, params=params, headers=lowered)


ERROR_INFO_TYPE = "type.googleapis.com/google.rpc.ErrorInfo"


def error_body(
    status: int,
    reason: str,
    message: str = "Synthetic API message",
    *,
    detail: str | None = None,
) -> bytes:
    """An API error document as Google sends it (``error.errors[0].reason``).

    ``detail`` adds a ``google.rpc.ErrorInfo`` entry to ``error.details`` with that reason, the
    shape in which Google reports API-key problems (``API_KEY_INVALID``,
    ``API_KEY_HTTP_REFERRER_BLOCKED``, ...), after an unrelated detail entry.
    """
    error: dict[str, Any] = {
        "code": status,
        "message": message,
        "errors": [{"message": message, "domain": "youtube.commentThread", "reason": reason}],
    }
    if detail is not None:
        error["details"] = [
            {"@type": "type.googleapis.com/google.rpc.LocalizedMessage", "message": message},
            {
                "@type": ERROR_INFO_TYPE,
                "reason": detail,
                "domain": "googleapis.com",
                "metadata": {"service": "youtube.googleapis.com", "consumer": "projects/0"},
            },
        ]
    return json.dumps({"error": error}).encode("utf-8")


@dataclass
class FakeApi:
    """Serve synthetic pages: ``thread_pages`` for ``commentThreads``, ``reply_pages`` per parent.

    Page ``n`` (0-based) is requested with ``pageToken=token-n``; every page but the last
    carries ``nextPageToken``. ``failure`` (status, body) answers every request with an error.
    """

    thread_pages: list[Page] = field(default_factory=lambda: [[]])
    reply_pages: dict[str, list[Page]] = field(default_factory=dict)
    base_url: str = BASE_URL
    failure: tuple[int, bytes] | None = None
    requests: list[ApiRequest] = field(default_factory=list)
    handlers: list[FakeHttpsHandler] = field(default_factory=list)
    """The urllib handlers serving this API (CLI tests read each request's ``timeout``)."""

    def __call__(self, url: str, headers: Mapping[str, str]) -> dict[str, Any]:
        """``fetch_json`` for the pure module: decode the page this request asks for."""
        status, body = self.respond(url, headers)
        assert status == HTTPStatus.OK, status
        page: dict[str, Any] = json.loads(body)
        return page

    def respond(self, url: str, headers: Mapping[str, str]) -> tuple[int, bytes]:
        """Record the request and return ``(status, body)``."""
        request = parse_request(url, headers, self.base_url)
        self.requests.append(request)
        if self.failure is not None:
            return self.failure
        if request.endpoint == "commentThreads":
            pages = self.thread_pages
        else:
            pages = self.reply_pages[request.params["parentId"]]
        index = int(request.params.get("pageToken", "token-0").removeprefix("token-"))
        page: dict[str, Any] = {"kind": "youtube#listResponse", "items": pages[index]}
        if index + 1 < len(pages):
            page["nextPageToken"] = f"token-{index + 1}"
        return HTTPStatus.OK, json.dumps(page).encode("utf-8")

    @property
    def urls(self) -> list[str]:
        return [request.url for request in self.requests]

    def https_handler(self) -> FakeHttpsHandler:
        """A new in-process handler answering from :meth:`respond`, kept in ``handlers``."""
        handler = FakeHttpsHandler(self.respond)
        self.handlers.append(handler)
        return handler

    @property
    def timeouts(self) -> list[float | None]:
        """The ``timeout`` urllib gave each request, in order."""
        return [request.timeout for handler in self.handlers for request in handler.seen]


class FakeHttpsHandler(urllib.request.HTTPSHandler):
    """Answer every https request in-process from ``respond(url, headers)``; no socket opens.

    Subclassing ``HTTPSHandler`` makes ``build_opener`` drop the real one.
    """

    def __init__(self, respond: Any, extra_headers: Mapping[str, str] | None = None) -> None:
        super().__init__()
        self.respond = respond
        self.extra_headers = dict(extra_headers or {})
        self.seen: list[urllib.request.Request] = []

    def https_open(self, req: urllib.request.Request) -> urllib.response.addinfourl:
        self.seen.append(req)
        status, body = self.respond(req.full_url, dict(req.header_items()))
        headers = email.message.Message()
        for name, value in self.extra_headers.items():
            headers[name] = value
        response = urllib.response.addinfourl(io.BytesIO(body), headers, req.full_url, status)
        response.msg = HTTPStatus(status).phrase
        return response
