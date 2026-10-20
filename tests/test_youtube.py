"""Tests for ``crop_explorer.youtube``: video references, the API key, pagination and records.

``fetch_comments`` runs over :class:`synthetic_youtube.FakeApi`, an injected ``fetch_json``
serving invented API pages, so nothing here touches the network. Records carry exactly the
four ``ingest`` fields; author names, channel ids, avatars and like counts are never written.
"""

from __future__ import annotations

import json
import string
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import quote

import pytest
from hypothesis import given
from hypothesis import strategies as st
from loguru import logger

from crop_explorer.config import YouTubeConfig, load_config
from crop_explorer.youtube import (
    API_KEY_HEADER,
    KEY_VIA_PROXY_FLAG,
    SOURCE_KIND,
    FetchResult,
    YouTubeComment,
    YouTubeError,
    api_error,
    api_key_from_env,
    decode_json,
    fetch_comments,
    parse_video_id,
    permalink,
    resolve_api_key,
    retention_expiry,
    source_id,
    watch_url,
)
from synthetic_youtube import (
    API_KEY,
    AUTHOR,
    AUTHOR_CHANNEL,
    AVATAR,
    LIKES,
    PUBLISHED,
    UNREGISTERED_CALLERS,
    VIDEO,
    FakeApi,
    comment,
    error_body,
    reply,
    thread,
)

SEED_VIDEO = "GUmvL7s6b2w"
ID_ALPHABET = string.ascii_letters + string.digits + "_-"
VIDEO_IDS = st.text(st.sampled_from(ID_ALPHABET), min_size=11, max_size=11)
ENV_NAME = "YOUTUBE_API_KEY"


def youtube_cfg(**updates: Any) -> YouTubeConfig:
    """The shipped ``youtube`` section with ``updates`` applied (validated)."""
    raw = {**load_config().youtube.model_dump(), "page_size": 2, **updates}
    return YouTubeConfig.model_validate(raw)


def fetch(api: FakeApi, *, api_key: str | None = API_KEY, **updates: Any) -> FetchResult:
    """Fetch ``VIDEO`` from ``api``; ``api_key=None`` is ``--key-via-proxy`` (no key header)."""
    return fetch_comments(VIDEO, api_key, youtube_cfg(**updates), api)


def ids(result: FetchResult) -> list[str]:
    return [row.comment_id for row in result.comments]


def top(number: int, text: str | None = None) -> dict[str, Any]:
    return comment(f"UgSynThread{number}", text or f"Synthetic thread {number} about okra.")


@pytest.fixture
def log_lines() -> Iterator[list[str]]:
    """Every loguru message emitted during the test (at any level)."""
    lines: list[str] = []
    sink = logger.add(lines.append, level="DEBUG", format="{message}")
    yield lines
    logger.remove(sink)


# --------------------------------------------------------------------------- video references


@pytest.mark.parametrize(
    "reference",
    [
        SEED_VIDEO,
        f"https://www.youtube.com/watch?v={SEED_VIDEO}",
        f"https://youtube.com/watch?v={SEED_VIDEO}&t=42s",
        f"http://m.youtube.com/watch?feature=share&v={SEED_VIDEO}",
        f"HTTPS://WWW.YOUTUBE.COM/watch?v={SEED_VIDEO}#comments",
        f"https://youtu.be/{SEED_VIDEO}",
        f"https://youtu.be/{SEED_VIDEO}?si=SyntheticShareId&t=7",
    ],
)
def test_parse_video_id_accepts_ids_and_watch_urls(reference: str) -> None:
    assert parse_video_id(reference) == SEED_VIDEO


@pytest.mark.parametrize(
    "reference",
    [
        "",
        f"{SEED_VIDEO}0",  # 12 characters
        SEED_VIDEO[:-1],  # 10 characters
        "GUmvL7s6b2!",
        f" {SEED_VIDEO}",
        f"https://youtu.be/{SEED_VIDEO} ",
        f"www.youtube.com/watch?v={SEED_VIDEO}",  # no scheme
        f"ftp://youtu.be/{SEED_VIDEO}",
        f"https://example.org/watch?v={SEED_VIDEO}",
        f"https://youtube.com.example.org/watch?v={SEED_VIDEO}",
        f"https://user@youtu.be/{SEED_VIDEO}",
        f"https://youtu.be:8443/{SEED_VIDEO}",
        f"https://www.youtube.com/watch?v={SEED_VIDEO}&v=abcdefghijk",  # ambiguous
        "https://www.youtube.com/watch",
        f"https://www.youtube.com/watch?v={SEED_VIDEO}0",
        f"https://www.youtube.com/shorts/{SEED_VIDEO}",
        f"https://www.youtube.com/embed/{SEED_VIDEO}",
        f"https://youtu.be/{SEED_VIDEO}/",
        "https://youtu.be/",
        "http://[",  # urlsplit itself raises on this
    ],
)
def test_parse_video_id_rejects_anything_else(reference: str) -> None:
    with pytest.raises(YouTubeError, match="video id"):
        parse_video_id(reference)


@given(video=VIDEO_IDS, extra=st.sampled_from(["", "&t=1s", "&list=PLsynthetic&index=2"]))
def test_every_video_id_round_trips_through_each_reference_form(video: str, extra: str) -> None:
    for reference in (
        video,
        f"https://www.youtube.com/watch?v={video}{extra}",
        f"https://youtube.com/watch?feature=share&v={video}{extra}",
        f"https://youtu.be/{video}{extra.replace('&', '?', 1)}",
    ):
        assert parse_video_id(reference) == video


@given(reference=st.text(max_size=80))
def test_any_text_is_a_valid_video_id_or_a_clear_error(reference: str) -> None:
    try:
        video = parse_video_id(reference)
    except YouTubeError as exc:
        assert "video id" in str(exc)
    else:
        assert len(video) == 11
        assert set(video) <= set(ID_ALPHABET)


def test_a_rejected_reference_is_never_echoed() -> None:
    """A key pasted in place of the video by mistake must not reach the terminal or logs."""
    with pytest.raises(YouTubeError) as caught:
        parse_video_id(API_KEY)

    assert API_KEY not in str(caught.value)


# --------------------------------------------------------------------------- API key


class UnreadableEnviron(Mapping[str, str]):
    """An environment that fails the test as soon as anything reads it."""

    def __getitem__(self, name: str) -> str:
        raise AssertionError(f"the environment was read ({name})")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("the environment was read")

    def __len__(self) -> int:
        raise AssertionError("the environment was read")


@pytest.mark.parametrize("environ", [{}, {ENV_NAME: ""}])
def test_a_missing_or_empty_key_fails_naming_the_variable_and_the_alternative(
    environ: dict[str, str],
) -> None:
    with pytest.raises(YouTubeError, match=ENV_NAME) as caught:
        api_key_from_env(environ, ENV_NAME)

    assert KEY_VIA_PROXY_FLAG == "--key-via-proxy"
    assert KEY_VIA_PROXY_FLAG in str(caught.value)


@pytest.mark.parametrize("value", ["   ", "key with spaces", f"{API_KEY}\n", "kéy"])
def test_a_malformed_key_fails_without_echoing_it(value: str) -> None:
    with pytest.raises(YouTubeError, match=ENV_NAME) as caught:
        api_key_from_env({ENV_NAME: value}, ENV_NAME)

    if value.strip():
        assert value.strip() not in str(caught.value)


def test_the_key_is_read_from_the_named_variable() -> None:
    environ = {ENV_NAME: API_KEY, "OTHER": "unused"}

    assert api_key_from_env(environ, ENV_NAME) == API_KEY


def test_key_via_proxy_reads_no_environment_variable() -> None:
    assert resolve_api_key(UnreadableEnviron(), ENV_NAME, via_proxy=True) is None


def test_without_key_via_proxy_the_key_must_come_from_the_environment() -> None:
    assert resolve_api_key({ENV_NAME: API_KEY}, ENV_NAME, via_proxy=False) == API_KEY
    with pytest.raises(YouTubeError, match=ENV_NAME):  # never a silent switch to the proxy
        resolve_api_key({}, ENV_NAME, via_proxy=False)


# --------------------------------------------------------------------------- pagination


def test_fetch_follows_next_page_token_to_the_end() -> None:
    api = FakeApi(thread_pages=[[thread(top(1)), thread(top(2))], [thread(top(3))], []])

    result = fetch(api)

    assert ids(result) == ["UgSynThread1", "UgSynThread2", "UgSynThread3"]
    assert (result.threads, result.replies, result.pages) == (3, 0, 3)
    expected = {
        "part": "snippet,replies",
        "videoId": VIDEO,
        "maxResults": "2",
        "textFormat": "plainText",
    }
    assert [request.endpoint for request in api.requests] == ["commentThreads"] * 3
    assert [request.params for request in api.requests] == [
        expected,
        {**expected, "pageToken": "token-1"},
        {**expected, "pageToken": "token-2"},
    ]


def test_a_video_without_comments_gives_no_rows() -> None:
    result = fetch(FakeApi())

    assert result == FetchResult(video_id=VIDEO, comments=(), threads=0, replies=0, pages=1)


def test_truncated_replies_are_paged_and_follow_their_thread() -> None:
    parent = "UgSynThread1"
    r1, r2, r3, r4 = (reply(parent, f"R{n}", f"Synthetic reply {n}.") for n in range(1, 5))
    api = FakeApi(
        thread_pages=[[thread(top(1), r1, r2, total=4), thread(top(2))]],
        reply_pages={parent: [[r1, r2], [r3, r4]]},
    )

    result = fetch(api)

    replies = [f"{parent}.R{n}" for n in range(1, 5)]
    assert ids(result) == [parent, *replies, "UgSynThread2"]
    assert (result.threads, result.replies, result.pages) == (2, 4, 3)
    reply_requests = [r for r in api.requests if r.endpoint == "comments"]
    expected = {
        "part": "snippet",
        "parentId": parent,
        "maxResults": "2",
        "textFormat": "plainText",
    }
    assert [r.params for r in reply_requests] == [expected, {**expected, "pageToken": "token-1"}]


def test_complete_inline_replies_need_no_reply_request() -> None:
    parent = "UgSynThread1"
    api = FakeApi(thread_pages=[[thread(top(1), reply(parent, "R1", "Synthetic reply."))]])

    result = fetch(api)

    assert ids(result) == [parent, f"{parent}.R1"]
    assert [request.endpoint for request in api.requests] == ["commentThreads"]


def test_each_threads_replies_come_right_after_it_in_api_order() -> None:
    a, b = "UgSynThread1", "UgSynThread2"
    api = FakeApi(
        thread_pages=[
            [thread(top(1), reply(a, "R1", "first reply to a"), total=2)],
            [thread(top(2), reply(b, "R1", "only reply to b"))],
        ],
        reply_pages={a: [[reply(a, "R1", "first reply to a"), reply(a, "R2", "second")]]},
    )

    assert ids(fetch(api)) == [a, f"{a}.R1", f"{a}.R2", b, f"{b}.R1"]


def test_duplicates_are_dropped_and_the_first_seen_copy_wins() -> None:
    first = top(1, "Synthetic first copy.")
    again = top(1, "Synthetic later copy with edited text.")
    api = FakeApi(thread_pages=[[thread(first), thread(top(2))], [thread(again)]])

    result = fetch(api)

    assert ids(result) == ["UgSynThread1", "UgSynThread2"]
    assert result.comments[0].text == "Synthetic first copy."
    assert (result.threads, result.replies) == (2, 0)


def test_exactly_max_pages_pages_are_allowed() -> None:
    api = FakeApi(thread_pages=[[thread(top(1))], [thread(top(2))], [thread(top(3))]])

    assert len(fetch(api, max_pages=3).comments) == 3


def test_more_pages_than_max_pages_fail_loudly_instead_of_truncating() -> None:
    api = FakeApi(thread_pages=[[thread(top(1))], [thread(top(2))], [thread(top(3))]])

    with pytest.raises(YouTubeError, match=r"max_pages.*2"):
        fetch(api, max_pages=2)
    assert len(api.requests) == 2  # the third page is never requested


def test_reply_pages_count_towards_max_pages() -> None:
    parent = "UgSynThread1"
    api = FakeApi(
        thread_pages=[[thread(top(1), total=3)]],
        reply_pages={parent: [[reply(parent, "R1", "a")], [reply(parent, "R2", "b")]]},
    )

    with pytest.raises(YouTubeError, match="max_pages"):
        fetch(api, max_pages=2)
    assert len(fetch(FakeApi(api.thread_pages, api.reply_pages), max_pages=3).comments) == 3


# --------------------------------------------------------------------------- the API key header


def threads_and_reply_pages() -> FakeApi:
    """Two thread pages and one reply page: three requests, one of each kind at least."""
    parent = "UgSynThread1"
    return FakeApi(
        thread_pages=[[thread(top(1), total=1)], [thread(top(2))]],
        reply_pages={parent: [[reply(parent, "R1", "Synthetic reply.")]]},
    )


def test_the_key_is_sent_only_in_the_header() -> None:
    api = threads_and_reply_pages()

    fetch(api)

    assert [request.endpoint for request in api.requests] == [
        "commentThreads",
        "comments",
        "commentThreads",
    ]
    for request in api.requests:
        assert request.headers == {API_KEY_HEADER.lower(): API_KEY}
        assert API_KEY not in request.url
        assert quote(API_KEY, safe="") not in request.url
        assert "key" not in {name.lower() for name in request.params}


def test_with_the_key_via_the_proxy_no_key_is_sent_at_all() -> None:
    api = threads_and_reply_pages()

    result = fetch(api, api_key=None)

    assert len(result.comments) == 3
    assert len(api.requests) == 3
    for request in api.requests:
        assert request.headers == {}  # the network proxy adds X-Goog-Api-Key on its way out
        assert API_KEY not in request.url
        assert "key" not in {name.lower() for name in request.params}


# --------------------------------------------------------------------------- records


def test_records_are_exactly_the_four_ingest_fields() -> None:
    parent = "UgSynThread1"
    api = FakeApi(
        thread_pages=[
            [thread(top(1), reply(parent, "R1", "Synthetic reply.", "2026-09-02T08:30:00Z"))]
        ]
    )

    result = fetch(api)

    assert result.comments == (
        YouTubeComment(
            comment_id=parent,
            text="Synthetic thread 1 about okra.",
            permalink=f"https://www.youtube.com/watch?v={VIDEO}&lc={parent}",
            created_at=PUBLISHED,
        ),
        YouTubeComment(
            comment_id=f"{parent}.R1",
            text="Synthetic reply.",
            permalink=f"https://www.youtube.com/watch?v={VIDEO}&lc={parent}.R1",
            created_at="2026-09-02T08:30:00Z",
        ),
    )
    rows = [row._asdict() for row in result.comments]
    assert all(list(row) == ["comment_id", "text", "permalink", "created_at"] for row in rows)
    written = json.dumps(rows)
    for private in (AUTHOR, AUTHOR_CHANNEL, AVATAR, str(LIKES), "UCsyntheticVideoChannel"):
        assert private not in written


def test_text_is_kept_exactly_as_the_api_sends_it() -> None:
    text = "  Synthetic: Sungold\nin grow bags \u2028 zone 6b 🍅  "
    api = FakeApi(thread_pages=[[thread(top(1, text))]])

    assert fetch(api).comments[0].text == text


def broken(field: str, value: Any = None) -> dict[str, Any]:
    """A top-level comment whose snippet lacks ``field`` (or holds ``value`` there)."""
    resource = top(1, "Synthetic secret text that must never be echoed.")
    if value is None:
        del resource["snippet"][field]
    else:
        resource["snippet"][field] = value
    return resource


@pytest.mark.parametrize(
    ("resource", "fragment"),
    [
        (broken("textDisplay"), "textDisplay"),
        (broken("textDisplay", 42), "textDisplay"),
        (broken("textDisplay", "   "), "textDisplay"),
        (broken("textDisplay", "Synthetic \ud83c half emoji"), "textDisplay"),
        (broken("publishedAt"), "publishedAt"),
        (broken("publishedAt", "last Tuesday"), "publishedAt"),
    ],
)
def test_a_comment_without_usable_text_or_date_fails_loudly(
    resource: dict[str, Any], fragment: str
) -> None:
    api = FakeApi(thread_pages=[[thread(top(2)), thread(resource)]])

    with pytest.raises(YouTubeError, match=fragment) as caught:
        fetch(api)

    message = str(caught.value)
    assert "thread 2" in message and "UgSynThread1" in message
    assert "secret" not in message


def mangled(path: tuple[str | int, ...], value: Any) -> dict[str, Any]:
    """A one-thread page with the value at ``path`` replaced (``...`` deletes it)."""
    page: dict[str, Any] = {"items": [thread(top(1), reply("UgSynThread1", "R1", "x"))]}
    target: Any = page
    for step in path[:-1]:
        target = target[step]
    if value is ...:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return page


@pytest.mark.parametrize(
    ("path", "value", "fragment"),
    [
        (("items",), ..., "items"),
        (("items",), {"0": "not a list"}, "items"),
        (("items", 0), "not an object", "object"),
        (("items", 0, "snippet"), ..., "snippet"),
        (("items", 0, "snippet", "topLevelComment"), ..., "topLevelComment"),
        (("items", 0, "snippet", "totalReplyCount"), ..., "totalReplyCount"),
        (("items", 0, "snippet", "totalReplyCount"), -1, "totalReplyCount"),
        (("items", 0, "snippet", "totalReplyCount"), True, "totalReplyCount"),
        (("items", 0, "snippet", "totalReplyCount"), "1", "totalReplyCount"),
        (("items", 0, "replies", "comments"), ..., "comments"),
        (("items", 0, "replies", "comments"), "x", "comments"),
        (("items", 0, "snippet", "topLevelComment", "id"), ..., "'id'"),
        (("items", 0, "snippet", "topLevelComment", "id"), "", "comment id"),
        (("items", 0, "snippet", "topLevelComment", "id"), "bad id&x=1", "comment id"),
        (("items", 0, "snippet", "topLevelComment", "snippet"), ..., "snippet"),
        (("nextPageToken",), 7, "nextPageToken"),
        (("nextPageToken",), "", "nextPageToken"),
    ],
)
def test_malformed_api_pages_fail_loudly(
    path: tuple[str | int, ...], value: Any, fragment: str
) -> None:
    page = mangled(path, value)

    def fetch_json(url: str, headers: Any) -> dict[str, Any]:
        return page

    with pytest.raises(YouTubeError, match=fragment):
        fetch_comments(VIDEO, API_KEY, youtube_cfg(), fetch_json)


def test_fetch_logs_counts_but_never_comment_text(log_lines: list[str]) -> None:
    text = "Synthetic private remark about the neighbour's zucchini."
    api = FakeApi(thread_pages=[[thread(top(1, text)), thread(top(2))]])

    fetch(api)

    logged = "\n".join(log_lines)
    assert "2 threads" in logged and VIDEO in logged
    assert text not in logged and "zucchini" not in logged
    assert API_KEY not in logged


# --------------------------------------------------------------------------- API errors


@pytest.mark.parametrize(
    ("status", "reason", "fragment"),
    [
        (403, "commentsDisabled", "comments are turned off"),
        (404, "videoNotFound", "no public video"),
        (403, "quotaExceeded", "quota"),
        (400, "keyInvalid", "API key is not valid"),
        (400, "badRequest", "API key"),
        (403, "forbidden", "refused"),
        (403, "accessNotConfigured", "not enabled"),
        (429, "rateLimitExceeded", "too many requests"),
    ],
)
def test_api_errors_map_to_a_clear_message(status: int, reason: str, fragment: str) -> None:
    secret = "Synthetic server message with request details"

    error = api_error(status, error_body(status, reason, secret))

    assert isinstance(error, YouTubeError)
    message = str(error)
    assert message.startswith(f"YouTube Data API error (HTTP {status}, {reason}): ")
    assert fragment in message
    assert secret not in message  # the API's own message text is never echoed


def test_an_unknown_reason_is_named_but_not_explained() -> None:
    message = str(api_error(409, error_body(409, "somethingNew")))

    assert message.startswith("YouTube Data API error (HTTP 409, somethingNew): ")


def test_a_request_without_a_key_points_at_the_missing_network_secret() -> None:
    """With ``--key-via-proxy``, 403 "unregistered callers" means the proxy added no key."""
    message = str(api_error(403, error_body(403, "forbidden", UNREGISTERED_CALLERS)))

    assert message.startswith("YouTube Data API error (HTTP 403, forbidden): ")
    for fragment in ("unregistered callers", KEY_VIA_PROXY_FLAG, "network secret"):
        assert fragment in message
    assert API_KEY_HEADER in message
    assert UNREGISTERED_CALLERS not in message  # explained in our words, not echoed


@pytest.mark.parametrize(
    "error",
    [
        {"message": 42, "errors": [{"reason": "forbidden"}]},
        {"message": "Synthetic other refusal", "errors": [{"reason": "forbidden"}]},
    ],
)
def test_other_forbidden_errors_keep_their_own_explanation(error: dict[str, Any]) -> None:
    message = str(api_error(403, json.dumps({"error": error}).encode("utf-8")))

    assert message == (
        "YouTube Data API error (HTTP 403, forbidden): the API refused access (the video or "
        "its comments may not be public)"
    )


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"<html>Synthetic gateway page</html>",
        b'{"error": "flat"}',
        b'{"error": {"errors": []}}',
        error_body(500, "has spaces and Synthetic secret text"),
        error_body(500, "x" * 200),
    ],
)
def test_an_error_without_a_usable_reason_names_only_the_status(body: bytes) -> None:
    message = str(api_error(500, body))

    assert message.startswith("YouTube Data API error (HTTP 500): ")
    assert "Synthetic" not in message


def test_a_redirect_is_reported_as_refused() -> None:
    assert "redirect" in str(api_error(302, b""))


def test_decode_json_needs_one_json_object() -> None:
    assert decode_json(b'{"items": []}') == {"items": []}
    for body in (b"[1, 2]", b"not json", b"\xff\xfe\x00", b""):
        with pytest.raises(YouTubeError, match="JSON"):
            decode_json(body)


# --------------------------------------------------------------------------- source description


def test_source_description_for_ingest() -> None:
    assert SOURCE_KIND == "youtube_comments"
    assert source_id(SEED_VIDEO) == f"youtube-{SEED_VIDEO}"
    assert watch_url(SEED_VIDEO) == f"https://www.youtube.com/watch?v={SEED_VIDEO}"
    assert (
        permalink(SEED_VIDEO, "Ugz1.2")
        == f"https://www.youtube.com/watch?v={SEED_VIDEO}&lc=Ugz1.2"
    )


@pytest.mark.parametrize(
    ("fetched_at", "days", "expected"),
    [
        ("2026-10-20T12:00:00Z", 30, "2026-11-19T12:00:00Z"),
        ("2026-12-31T23:59:59Z", 1, "2027-01-01T23:59:59Z"),
    ],
)
def test_retention_expiry_is_fetch_time_plus_retention_days(
    fetched_at: str, days: int, expected: str
) -> None:
    assert retention_expiry(fetched_at, days) == expected


def test_retention_expiry_needs_a_canonical_fetch_time() -> None:
    with pytest.raises(ValueError, match="fetched_at"):
        retention_expiry("2026-10-20", 30)
