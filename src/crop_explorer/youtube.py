"""Fetch one video's public comments through the official YouTube Data API v3 (no I/O here).

The network is reached only through an injected ``fetch_json(url, headers)`` callable
(:func:`crop_explorer.youtube_http.urllib_transport` in production, a fake in tests), so
everything in this module is deterministic. ``commentThreads`` pages are followed to the end;
a thread whose ``snippet.totalReplyCount`` exceeds the replies included inline has its replies
paged through ``comments``. Every request counts towards ``youtube.max_pages``: needing more is
an error, never a silently truncated result.

The API key comes from one of two explicit modes (:func:`resolve_api_key`), never a fallback
from one to the other: the environment variable named by ``youtube.api_key_env``, sent only in
the ``X-Goog-Api-Key`` request header (never in a URL, a log line or an error message); or,
with ``--key-via-proxy``, no key at all, because the cloud environment's network proxy attaches
the header (a network secret for the API host).

Records hold exactly the four ``ingest`` fields (``comment_id``, ``text``, ``permalink``,
``created_at``), never author names, channel ids, avatars or like counts. Rows are
de-duplicated by comment id (the first one seen wins) in API order: each thread, then its
replies (inline ones first, then the paged ones); a thread seen again on a later page is
skipped with its replies. Errors name the thread number and comment id, never comment text;
only counts are logged.
"""

from __future__ import annotations

import itertools
import json
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Final, NamedTuple
from urllib.parse import parse_qs, urlencode, urlsplit

from loguru import logger

from crop_explorer.config import YouTubeConfig
from crop_explorer.schemas import to_utc_iso
from crop_explorer.visibility import require_utc_iso

__all__ = [
    "API_KEY_HEADER",
    "KEY_VIA_PROXY_FLAG",
    "SOURCE_KIND",
    "FetchJson",
    "FetchResult",
    "YouTubeComment",
    "YouTubeError",
    "api_error",
    "api_key_from_env",
    "decode_json",
    "fetch_comments",
    "parse_video_id",
    "permalink",
    "resolve_api_key",
    "retention_expiry",
    "source_id",
    "watch_url",
]

API_KEY_HEADER: Final = "X-Goog-Api-Key"
KEY_VIA_PROXY_FLAG: Final = "--key-via-proxy"
"""``fetch-youtube`` option: send no key, the network proxy attaches it (a network secret)."""
SOURCE_KIND: Final = "youtube_comments"
"""``ingest --kind`` of a fetched video's comments."""
_SOURCE_ID_PREFIX: Final = "youtube-"
_WATCH_URL: Final = "https://www.youtube.com/watch"
_THREADS_ENDPOINT: Final = "commentThreads"
_REPLIES_ENDPOINT: Final = "comments"
_PLAIN_TEXT: Final = "plainText"

_VIDEO_ID: Final = re.compile(r"[A-Za-z0-9_-]{11}")
_COMMENT_ID: Final = re.compile(r"[A-Za-z0-9_.-]+")
_VISIBLE_ASCII: Final = re.compile(r"[!-~]+")
"""Printable ASCII without spaces: what a pasted URL or an API key consists of."""
_REASON_TOKEN: Final = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_WEB_SCHEMES: Final = frozenset({"http", "https"})
_WATCH_HOSTS: Final = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com"})
_SHORT_HOST: Final = "youtu.be"
_BAD_VIDEO: Final = (
    "expected a YouTube video id (11 characters: letters, digits, _ or -), a "
    "https://www.youtube.com/watch?v=ID URL or a https://youtu.be/ID URL"
)

_KEY_INVALID: Final = (
    "the API key is not valid; check the key in the environment variable (or, with "
    f"{KEY_VIA_PROXY_FLAG}, in the network secret)"
)
_API_NOT_ENABLED: Final = (
    "the YouTube Data API v3 is not enabled for the API key's Google Cloud project"
)
_APPLICATION_RESTRICTED: Final = (
    "the API key's application restrictions block this request (use no application "
    "restriction, or an IP address restriction, for a command-line key)"
)
_REASONS: Final = MappingProxyType(
    {
        "commentsDisabled": "comments are turned off for this video",
        "videoNotFound": "no public video has this id",
        "forbidden": (
            "the API refused access: the video's comments may not be public, or the API key's "
            "restrictions block this request"
        ),
        "quotaExceeded": (
            "the API key's daily quota is used up; try again after the daily reset (midnight "
            "Pacific Time)"
        ),
        "dailyLimitExceeded": "the API key's daily limit is used up; try again tomorrow",
        "rateLimitExceeded": "too many requests in a short time; wait and try again",
        "keyInvalid": _KEY_INVALID,
        "keyExpired": "the API key has expired; create a new one",
        "badRequest": "the API rejected the request (most often an invalid API key)",
        "accessNotConfigured": _API_NOT_ENABLED,
        "ipRefererBlocked": "the API key's restrictions do not allow this request",
        "commentNotFound": "a comment thread disappeared while fetching; try again",
        "processingFailure": "the API could not process the request; try again later",
        "backendError": "the API had an internal error; try again later",
    }
)
"""Explanations of ``error.errors[0].reason`` values (the API's own messages are not shown)."""
_ERROR_INFO_TYPE: Final = "type.googleapis.com/google.rpc.ErrorInfo"
_DETAIL_REASONS: Final = MappingProxyType(
    {
        "API_KEY_INVALID": _KEY_INVALID,
        "API_KEY_HTTP_REFERRER_BLOCKED": _APPLICATION_RESTRICTED,
        "API_KEY_IP_ADDRESS_BLOCKED": _APPLICATION_RESTRICTED,
        "API_KEY_ANDROID_APP_BLOCKED": _APPLICATION_RESTRICTED,
        "API_KEY_IOS_APP_BLOCKED": _APPLICATION_RESTRICTED,
        "API_KEY_SERVICE_BLOCKED": (
            "the API key's API restrictions do not include YouTube Data API v3"
        ),
        "SERVICE_DISABLED": _API_NOT_ENABLED,
    }
)
"""Explanations of ``google.rpc.ErrorInfo`` reasons in ``error.details``.

Google reports API-key problems this way while ``errors[0].reason`` says only ``badRequest``
or ``forbidden``, so these are checked first.
"""
_UNREGISTERED_MARKER: Final = "unregistered callers"
"""Matched (never shown) in ``error.message`` of a request that reached the API without a key."""
_UNREGISTERED: Final = (
    'the request reached the API without an API key ("unregistered callers"): with '
    f"{KEY_VIA_PROXY_FLAG} this means the network proxy has no network secret that adds the "
    f"{API_KEY_HEADER} header for the API host (www.googleapis.com)"
)
_REDIRECT: Final = range(300, 400)
_REDIRECT_REFUSED: Final = (
    "the API answered with a redirect, which is refused so that the API key is never sent to "
    "another address"
)
_REQUEST_FAILED: Final = "the request failed"

_KIND_NAMES: Final[Mapping[type, str]] = MappingProxyType(
    {str: "a string", int: "an integer", list: "a list", dict: "an object"}
)

FetchJson = Callable[[str, Mapping[str, str]], Mapping[str, object]]
"""``fetch_json(url, headers)``: GET ``url`` and return the decoded JSON object.

Raises :class:`YouTubeError` for an API error response (see :func:`api_error`).
"""


class YouTubeError(ValueError):
    """A video reference, API key or API response is unusable.

    Messages never contain the API key, request headers or comment text.
    """


class YouTubeComment(NamedTuple):
    """One output row: exactly the fields ``ingest`` reads from a JSONL export."""

    comment_id: str
    text: str
    permalink: str
    created_at: str


@dataclass(frozen=True)
class FetchResult:
    """Every comment of one video, in output order, with counts.

    ``threads`` and ``replies`` count distinct top-level comments and replies (``threads +
    replies == len(comments)``); ``pages`` is the number of API requests made (quota units).
    """

    video_id: str
    comments: tuple[YouTubeComment, ...]
    threads: int
    replies: int
    pages: int


# --------------------------------------------------------------------------- public API


def parse_video_id(reference: str) -> str:
    """Return the video id of a bare id, a ``youtube.com/watch?v=`` or a ``youtu.be/`` URL.

    Parameters
    ----------
    reference
        An 11-character video id, or an http(s) URL on ``youtube.com``, ``www.youtube.com``,
        ``m.youtube.com`` (path ``/watch`` with exactly one ``v``) or ``youtu.be`` (path
        ``/ID``). Scheme and host are case-insensitive; other query parameters are ignored.

    Returns
    -------
    str
        The 11-character video id.

    Raises
    ------
    YouTubeError
        For anything else (the reference is not echoed: it may be a mistakenly pasted key).
    """
    candidate = reference if _VIDEO_ID.fullmatch(reference) else _id_from_url(reference)
    if candidate is None or not _VIDEO_ID.fullmatch(candidate):
        raise YouTubeError(_BAD_VIDEO)
    return candidate


def api_key_from_env(environ: Mapping[str, str], name: str) -> str:
    """Return the API key held by the environment variable ``name``.

    Parameters
    ----------
    environ
        The process environment (``os.environ``).
    name
        The variable's name (``youtube.api_key_env``).

    Returns
    -------
    str
        The key: printable ASCII without spaces.

    Raises
    ------
    YouTubeError
        If the variable is missing, empty or holds anything but one key (the message names the
        variable, never its value).
    """
    key = environ.get(name, "")
    if not key:
        raise YouTubeError(
            f"environment variable {name} is missing or empty: set it to your YouTube Data API "
            "key in your own terminal (never on the command line, in a file or in a chat), or "
            f"pass {KEY_VIA_PROXY_FLAG} if the network proxy attaches the key (a network "
            f"secret adding the {API_KEY_HEADER} header for the API host)"
        )
    if not _VISIBLE_ASCII.fullmatch(key):
        raise YouTubeError(
            f"environment variable {name} must hold only the API key (printable ASCII without "
            "spaces or line breaks)"
        )
    return key


def resolve_api_key(environ: Mapping[str, str], name: str, *, via_proxy: bool) -> str | None:
    """Return the API key to send, or ``None`` when the network proxy attaches it.

    Parameters
    ----------
    environ
        The process environment (``os.environ``); not read at all with ``via_proxy``.
    name
        The variable holding the key (``youtube.api_key_env``).
    via_proxy
        ``--key-via-proxy``: the cloud environment's network proxy adds the ``X-Goog-Api-Key``
        header to requests for the API host (a network secret), so no key is read or sent.

    Returns
    -------
    str or None
        The key from :func:`api_key_from_env`, or ``None`` with ``via_proxy``.

    Raises
    ------
    YouTubeError
        Without ``via_proxy``, if the variable is missing, empty or malformed: there is never a
        silent switch to the proxy.
    """
    return None if via_proxy else api_key_from_env(environ, name)


def fetch_comments(
    video_id: str, api_key: str | None, cfg: YouTubeConfig, fetch_json: FetchJson
) -> FetchResult:
    """Fetch every public comment and reply under ``video_id``.

    Parameters
    ----------
    video_id
        A video id from :func:`parse_video_id`.
    api_key
        The API key, sent only as the ``X-Goog-Api-Key`` header; ``None`` (``--key-via-proxy``)
        sends no key header, because the network proxy attaches it.
    cfg
        Base URL, page size and the page budget (``max_pages``).
    fetch_json
        The transport; called once per API page.

    Returns
    -------
    FetchResult
        The de-duplicated rows in API order, each thread followed by its replies.

    Raises
    ------
    YouTubeError
        If more than ``cfg.max_pages`` pages would be needed, a page or comment is malformed
        (e.g. a comment without ``snippet.textDisplay``) or the API answers with an error.
    """
    pager = _Pager(fetch_json, _key_headers(api_key), cfg)
    collector = _Collector(video_id)
    threads = pager.items(_THREADS_ENDPOINT, _thread_params(video_id, cfg), "comment threads")
    for number, item in enumerate(threads, 1):
        _collect_thread(pager, collector, item, f"thread {number}")
    result = collector.result(pager.pages)
    logger.info(
        "Fetched {} threads and {} replies ({} API pages) for video {}",
        result.threads,
        result.replies,
        result.pages,
        video_id,
    )
    return result


def api_error(status: int, body: bytes) -> YouTubeError:
    """Describe an API error response from its status and its reason tokens.

    Parameters
    ----------
    status
        The HTTP status code.
    body
        The response body. Only short reason tokens are read from it: ``error.errors[0].reason``
        and the first ``google.rpc.ErrorInfo`` reason in ``error.details`` (how Google reports
        API-key problems such as ``API_KEY_INVALID`` or ``API_KEY_HTTP_REFERRER_BLOCKED``; it
        is explained first). ``error.message`` is only matched for "unregistered callers" (a
        request without a key: ``--key-via-proxy`` without a network secret). The API's own
        message text is never shown, and headers are never passed in.

    Returns
    -------
    YouTubeError
        ``YouTube Data API error (HTTP <status>[, <reason>][, <detail>]): <explanation>``.
    """
    error = _error_object(body)
    reason, detail = _error_reason(error), _detail_reason(error)
    label = ", ".join([f"HTTP {status}", *(token for token in (reason, detail) if token)])
    explanation = _explain(status, reason, detail, error)
    return YouTubeError(f"YouTube Data API error ({label}): {explanation}")


def decode_json(body: bytes) -> dict[str, object]:
    """Decode an API response body that must be one JSON object.

    Raises
    ------
    YouTubeError
        If the body is not JSON or not an object.
    """
    try:
        value: object = json.loads(body)
    except ValueError:  # also UnicodeDecodeError
        raise YouTubeError(
            "the YouTube Data API answered with something that is not JSON"
        ) from None
    if not isinstance(value, dict):
        raise YouTubeError("the YouTube Data API answered with JSON that is not an object")
    return value


def source_id(video_id: str) -> str:
    """Return the ``ingest --source-id`` of a video's comments: ``youtube-<video id>``."""
    return f"{_SOURCE_ID_PREFIX}{video_id}"


def watch_url(video_id: str) -> str:
    """Return the video's watch page (``ingest --source-url``)."""
    return f"{_WATCH_URL}?v={video_id}"


def permalink(video_id: str, comment_id: str) -> str:
    """Return the link that opens the video with ``comment_id`` highlighted."""
    return f"{watch_url(video_id)}&lc={comment_id}"


def retention_expiry(fetched_at: str, retention_days: int) -> str:
    """Return ``fetched_at`` plus ``retention_days`` days, canonical UTC.

    Raises
    ------
    ValueError
        If ``fetched_at`` is not a canonical UTC timestamp.
    """
    fetched = datetime.fromisoformat(require_utc_iso(fetched_at, "fetched_at"))
    return to_utc_iso(fetched + timedelta(days=retention_days))


# --------------------------------------------------------------------------- video references


def _id_from_url(reference: str) -> str | None:
    """Return the id candidate of a watch or short URL (validated by the caller)."""
    if not _VISIBLE_ASCII.fullmatch(reference):
        return None
    try:
        parts = urlsplit(reference)
    except ValueError:  # e.g. an unbalanced "[" in the host
        return None
    host = parts.netloc.lower()
    if parts.scheme.lower() not in _WEB_SCHEMES:
        return None
    if host == _SHORT_HOST:
        return parts.path.removeprefix("/")
    if host in _WATCH_HOSTS and parts.path == "/watch":
        values = parse_qs(parts.query).get("v", [])
        return values[0] if len(values) == 1 else None
    return None


# --------------------------------------------------------------------------- pagination


def _key_headers(api_key: str | None) -> Mapping[str, str]:
    """Return the key header, or no header at all when the network proxy attaches the key."""
    return MappingProxyType({} if api_key is None else {API_KEY_HEADER: api_key})


def _thread_params(video_id: str, cfg: YouTubeConfig) -> dict[str, str]:
    return {
        "part": "snippet,replies",
        "videoId": video_id,
        "maxResults": str(cfg.page_size),
        "textFormat": _PLAIN_TEXT,
    }


def _reply_params(parent_id: str, cfg: YouTubeConfig) -> dict[str, str]:
    return {
        "part": "snippet",
        "parentId": parent_id,
        "maxResults": str(cfg.page_size),
        "textFormat": _PLAIN_TEXT,
    }


@dataclass
class _Pager:
    """Issue the GET requests for one video, counting every page against ``max_pages``."""

    fetch_json: FetchJson
    headers: Mapping[str, str] = field(repr=False)  # holds the API key
    cfg: YouTubeConfig
    pages: int = 0

    def items(self, endpoint: str, params: Mapping[str, str], what: str) -> Iterator[object]:
        """Yield every item of every page of ``endpoint``, following ``nextPageToken``."""
        token: str | None = None
        for number in itertools.count(1):
            page = self._get(endpoint, params if token is None else {**params, "pageToken": token})
            where = f"{what}, page {number}"
            yield from _field(page, "items", list, where)
            token = _next_page_token(page, where)
            if token is None:
                return

    def _get(self, endpoint: str, params: Mapping[str, str]) -> Mapping[str, object]:
        if self.pages >= self.cfg.max_pages:
            raise YouTubeError(
                f"this video needs more than youtube.max_pages = {self.cfg.max_pages} API pages "
                "(comment threads plus reply pages); stopped instead of returning a truncated "
                "result: raise max_pages in the configuration to fetch every comment"
            )
        self.pages += 1
        url = f"{self.cfg.api_base_url}/{endpoint}?{urlencode(params, safe=',')}"
        return self.fetch_json(url, self.headers)


def _next_page_token(page: Mapping[str, object], where: str) -> str | None:
    token = page.get("nextPageToken")
    if token is None:
        return None
    if not isinstance(token, str) or not token:
        raise YouTubeError(f"{where}: 'nextPageToken' in the API response is not a token")
    return token


# --------------------------------------------------------------------------- records


@dataclass
class _Collector:
    """De-duplicated rows of one video in the order first seen, with their counts."""

    video_id: str
    rows: dict[str, YouTubeComment] = field(default_factory=dict)
    threads: int = 0
    replies: int = 0

    def add(self, resource: object, where: str, *, is_reply: bool) -> YouTubeComment | None:
        """Validate a comment resource; keep and return it, or ``None`` if its id was seen."""
        row = _comment(resource, self.video_id, where)
        if row.comment_id in self.rows:
            return None
        self.rows[row.comment_id] = row
        if is_reply:
            self.replies += 1
        else:
            self.threads += 1
        return row

    def result(self, pages: int) -> FetchResult:
        """Freeze the rows and counts collected so far."""
        rows = tuple(self.rows.values())
        return FetchResult(self.video_id, rows, self.threads, self.replies, pages)


def _collect_thread(pager: _Pager, collector: _Collector, item: object, where: str) -> None:
    """Add a thread's top-level comment, its inline replies and, if truncated, the rest.

    A thread already collected from an earlier page (pages shift when a comment is posted
    during the fetch) is skipped whole: its replies were collected with its first copy, and
    paging them again would spend quota and could place a reply after an unrelated thread.
    """
    snippet = _field(item, "snippet", dict, where)
    top = collector.add(_member(snippet, "topLevelComment", where), where, is_reply=False)
    if top is None:
        return
    inline = _inline_replies(item, where)
    for reply in inline:
        collector.add(reply, f"a reply in {where}", is_reply=True)
    if _count(snippet, "totalReplyCount", where) > len(inline):
        params = _reply_params(top.comment_id, pager.cfg)
        for reply in pager.items(_REPLIES_ENDPOINT, params, f"replies to {where}"):
            collector.add(reply, f"a reply to {where}", is_reply=True)


def _inline_replies(item: object, where: str) -> list[object]:
    """Return ``replies.comments`` of a thread (absent when it has no replies)."""
    if not isinstance(item, dict) or "replies" not in item:
        return []
    return _field(item["replies"], "comments", list, where)


def _comment(resource: object, video_id: str, where: str) -> YouTubeComment:
    comment_id = _field(resource, "id", str, where)
    if not _COMMENT_ID.fullmatch(comment_id):
        raise YouTubeError(f"{where}: the comment id is not a YouTube comment id")
    where = f"{where} (comment {comment_id})"
    snippet = _field(resource, "snippet", dict, where)
    return YouTubeComment(
        comment_id=comment_id,
        text=_text(snippet, where),
        permalink=permalink(video_id, comment_id),
        created_at=_published_at(snippet, where),
    )


def _text(snippet: object, where: str) -> str:
    """Return ``snippet.textDisplay`` (plain text) exactly as sent; it must not be blank."""
    text = _field(snippet, "textDisplay", str, where)
    if not text.strip():
        raise YouTubeError(f"{where}: 'textDisplay' in the API response is blank")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise YouTubeError(
            f"{where}: 'textDisplay' holds invalid Unicode (a lone surrogate)"
        ) from None
    return text


def _published_at(snippet: object, where: str) -> str:
    """Return ``snippet.publishedAt`` as sent once it parses as an ISO-8601 timestamp."""
    value = _field(snippet, "publishedAt", str, where)
    try:
        to_utc_iso(value)
    except (ValueError, OverflowError):
        raise YouTubeError(f"{where}: 'publishedAt' is not an ISO-8601 timestamp") from None
    return value


# --------------------------------------------------------------------------- response fields


def _member(value: object, key: str, where: str) -> object:
    if not isinstance(value, dict):
        raise YouTubeError(f"{where}: expected a JSON object in the API response")
    if key not in value:
        raise YouTubeError(f"{where}: the API response has no {key!r}")
    found: object = value[key]
    return found


def _field[T](value: object, key: str, kind: type[T], where: str) -> T:
    """Return ``value[key]`` if it is a ``kind`` (``bool`` never counts as an integer)."""
    found = _member(value, key, where)
    if not isinstance(found, kind) or isinstance(found, bool):
        raise YouTubeError(f"{where}: {key!r} in the API response is not {_KIND_NAMES[kind]}")
    return found


def _count(value: object, key: str, where: str) -> int:
    number = _field(value, key, int, where)
    if number < 0:
        raise YouTubeError(f"{where}: {key!r} in the API response is negative")
    return number


def _error_object(body: bytes) -> dict[str, Any]:
    """Return the ``error`` object of an error body, or ``{}`` when there is none."""
    try:
        error = json.loads(body)["error"]
    except (ValueError, LookupError, TypeError):  # ValueError includes UnicodeDecodeError
        return {}
    return error if isinstance(error, dict) else {}


def _token(value: object) -> str | None:
    """Return ``value`` when it is a short reason token, else ``None`` (it is never shown)."""
    return value if isinstance(value, str) and _REASON_TOKEN.fullmatch(value) else None


def _error_reason(error: Mapping[str, Any]) -> str | None:
    """Return ``errors[0].reason`` when it is a short token, else ``None``."""
    try:
        reason: object = error["errors"][0]["reason"]
    except (LookupError, TypeError):
        return None
    return _token(reason)


def _detail_reason(error: Mapping[str, Any]) -> str | None:
    """Return the first ``google.rpc.ErrorInfo`` reason in ``details`` when it is a token."""
    details = error.get("details")
    infos = [
        detail
        for detail in (details if isinstance(details, list) else [])
        if isinstance(detail, dict) and detail.get("@type") == _ERROR_INFO_TYPE
    ]
    return _token(infos[0].get("reason")) if infos else None


def _explain(status: int, reason: str | None, detail: str | None, error: Mapping[str, Any]) -> str:
    """Return the fixed explanation of an API error (the API's own text is never shown)."""
    message = error.get("message")
    if isinstance(message, str) and _UNREGISTERED_MARKER in message.lower():
        return _UNREGISTERED
    explanation = _DETAIL_REASONS.get(detail or "") or _REASONS.get(reason or "")
    if explanation is not None:
        return explanation
    return _REDIRECT_REFUSED if status in _REDIRECT else _REQUEST_FAILED
