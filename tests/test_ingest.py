"""Tests for :mod:`crop_explorer.ingest` (reading user-supplied CSV/JSONL comment exports).

Covers SPEC §8 "Initial flow" steps 1-2: column mappings for id, body, optional permalink and
date; validation of required fields, input encoding and id collisions; dataset hash and source
permission state. Text must be kept byte-exact so evidence quotes stay exact substrings, and no
error message or log line may echo comment text.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from loguru import logger
from pydantic import Field

from crop_explorer import db
from crop_explorer import ingest as ingest_module
from crop_explorer.config import load_config
from crop_explorer.extraction import load_extractions
from crop_explorer.ingest import (
    IngestError,
    build_source,
    dataset_hash,
    import_file,
    read_comments,
)
from crop_explorer.schemas import (
    RIGHTS_STATUSES,
    ColumnMapping,
    CommentRecord,
    ImportStats,
    RightsStatus,
    SourceRecord,
)

DEFAULT = ColumnMapping()
HEADER = ("comment_id", "text", "permalink", "created_at")
URL1 = "https://www.youtube.com/watch?v=abc&lc=c1"
TEXT1 = "Sun Gold in 5 gallon pots, zone 6b. So sweet!"
TEXT2 = "Cherokee Purple split after the rain."
IMPORTED = "2026-10-15T12:00:00Z"

# Comment text that stresses byte-exact preservation in both formats.
TRICKY_TEXTS = (
    "  leading and trailing spaces  ",
    "\ttab-led text\t",
    "line one\nline two",
    "windows\r\nline break",
    "bare\rcarriage return",
    "ends with newline\n",
    'quotes "inside", and, commas',
    "unicode: café, naïve, 東京, Ελληνικά",
    "emoji 🍅🌶\ufe0f👩\u200d🌾",
    "line\u2028separator and\u2029paragraph separator",
    "next-line\x85char",
    "zero\u200bwidth and \ufeffinner BOM",
    "nul\x00byte",
)

IDS = st.from_regex(r"[A-Za-z0-9_-]{1,12}", fullmatch=True)
TEXTS = st.text(st.characters(codec="utf-8"), min_size=1, max_size=40).filter(
    lambda t: t.strip() != ""
)
ROWS = st.lists(st.tuples(IDS, TEXTS), min_size=1, max_size=8, unique_by=lambda r: r[0])
SECRET = "SECRET"
SECRETS = st.text(st.characters(codec="utf-8", exclude_characters="\r\n"), max_size=20).map(
    lambda t: f"{SECRET}{t}"
)


# --------------------------------------------------------------------------- helpers


def write_csv(path: Path, header: Sequence[str], rows: Iterable[Sequence[str]]) -> Path:
    """Write a UTF-8 CSV exactly as the csv module would export it."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def write_jsonl(path: Path, objects: Iterable[object], *, newline: str = "\n") -> Path:
    """Write one JSON document per line (non-ASCII kept raw, like most exporters)."""
    lines = (json.dumps(obj, ensure_ascii=False) + newline for obj in objects)
    path.write_bytes("".join(lines).encode("utf-8"))
    return path


def record(
    comment_id: str,
    text: str,
    order: int,
    permalink: str | None = None,
    created_at: str | None = None,
) -> CommentRecord:
    return CommentRecord(
        comment_id=comment_id,
        text=text,
        permalink=permalink,
        created_at=created_at,
        source_order=order,
    )


def ingest_error(path: Path, mapping: ColumnMapping = DEFAULT) -> str:
    """Read ``path`` expecting failure; return the error message."""
    with pytest.raises(IngestError) as excinfo:
        read_comments(path, mapping)
    return str(excinfo.value)


@pytest.fixture
def two_row_csv(tmp_path: Path) -> Path:
    return write_csv(
        tmp_path / "comments.csv",
        HEADER,
        [("c1", TEXT1, URL1, "2024-07-01T12:00:00+02:00"), ("c2", TEXT2, "", "")],
    )


@pytest.fixture
def captured_logs() -> Iterator[list[str]]:
    messages: list[str] = []
    handler_id = logger.add(messages.append, format="{message}", level="DEBUG")
    yield messages
    logger.remove(handler_id)


# --------------------------------------------------------------------------- happy paths


def test_ingest_error_is_a_value_error() -> None:
    # The CLI maps ValueError subclasses to a one-line error and exit code 2.
    assert issubclass(IngestError, ValueError)


def test_csv_with_default_mapping(two_row_csv: Path) -> None:
    assert read_comments(two_row_csv, DEFAULT) == [
        record("c1", TEXT1, 0, URL1, "2024-07-01T10:00:00Z"),
        record("c2", TEXT2, 1),
    ]


def test_csv_with_custom_mapping_ignores_unmapped_columns(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "export.csv",
        ("likes", "body", "url", "id", "posted", "text"),
        [
            ("12", TEXT1, URL1, "yt-1", "2024-06-30", "not this column"),
            ("0", TEXT2, "", "yt-2", "", "nor this"),
        ],
    )
    mapping = ColumnMapping(id_col="id", text_col="body", permalink_col="url", date_col="posted")

    assert read_comments(path, mapping) == [
        record("yt-1", TEXT1, 0, URL1, "2024-06-30T00:00:00Z"),
        record("yt-2", TEXT2, 1),
    ]


def test_unmapped_optional_fields_stay_none_even_if_columns_exist(two_row_csv: Path) -> None:
    mapping = ColumnMapping(permalink_col=None, date_col=None)

    records = read_comments(two_row_csv, mapping)

    assert [(r.permalink, r.created_at) for r in records] == [(None, None), (None, None)]
    assert [r.text for r in records] == [TEXT1, TEXT2]


def test_jsonl_with_default_mapping(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path / "comments.jsonl",
        [
            {"comment_id": "c1", "text": TEXT1, "permalink": URL1, "created_at": "2024-07-01"},
            {"comment_id": "c2", "text": TEXT2, "permalink": None, "created_at": None},
        ],
    )

    assert read_comments(path, DEFAULT) == [
        record("c1", TEXT1, 0, URL1, "2024-07-01T00:00:00Z"),
        record("c2", TEXT2, 1),
    ]


def test_jsonl_with_custom_mapping_ignores_other_keys(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path / "export.jsonl",
        [
            {"cid": "a", "body": TEXT1, "link": URL1, "when": "2024-07-01T08:00:00-05:00",
             "author": {"name": "x"}, "likes": 3},
            {"cid": "b", "body": TEXT2},
        ],
    )  # fmt: skip
    mapping = ColumnMapping(id_col="cid", text_col="body", permalink_col="link", date_col="when")

    assert read_comments(path, mapping) == [
        record("a", TEXT1, 0, URL1, "2024-07-01T13:00:00Z"),
        record("b", TEXT2, 1),
    ]


def test_uppercase_suffix_is_recognised(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "EXPORT.CSV", ("comment_id", "text"), [("c1", TEXT1)])

    assert read_comments(path, DEFAULT) == [record("c1", TEXT1, 0)]


def test_jsonl_skips_blank_lines_without_gaps_in_source_order(tmp_path: Path) -> None:
    path = tmp_path / "gaps.jsonl"
    lines = [
        '{"comment_id": "c1", "text": "first"}',
        "",
        "   ",
        '{"comment_id": "c2", "text": "second"}',
    ]
    path.write_text("\n" + "\n".join(lines) + "\n\n", encoding="utf-8")

    assert read_comments(path, DEFAULT) == [record("c1", "first", 0), record("c2", "second", 1)]


def test_jsonl_tolerates_crlf_line_endings(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path / "crlf.jsonl",
        [{"comment_id": "c1", "text": TEXT1}, {"comment_id": "c2", "text": TEXT2}],
        newline="\r\n",
    )

    assert [r.text for r in read_comments(path, DEFAULT)] == [TEXT1, TEXT2]


# --------------------------------------------------------------------------- exact text


@pytest.mark.parametrize("text", TRICKY_TEXTS)
def test_csv_preserves_text_exactly(tmp_path: Path, text: str) -> None:
    path = write_csv(tmp_path / "t.csv", ("comment_id", "text"), [("c1", text), ("c2", "after")])

    assert [r.text for r in read_comments(path, DEFAULT)] == [text, "after"]


@pytest.mark.parametrize("text", TRICKY_TEXTS)
def test_jsonl_preserves_text_exactly(tmp_path: Path, text: str) -> None:
    path = write_jsonl(
        tmp_path / "t.jsonl",
        [{"comment_id": "c1", "text": text}, {"comment_id": "c2", "text": "after"}],
    )

    assert [r.text for r in read_comments(path, DEFAULT)] == [text, "after"]


@settings(deadline=None)
@given(rows=ROWS)
def test_csv_round_trip_preserves_ids_text_and_order(
    tmp_path_factory: pytest.TempPathFactory, rows: list[tuple[str, str]]
) -> None:
    path = write_csv(tmp_path_factory.mktemp("csv") / "rt.csv", ("comment_id", "text"), rows)

    assert read_comments(path, DEFAULT) == [record(i, t, n) for n, (i, t) in enumerate(rows)]


@settings(deadline=None)
@given(rows=ROWS, newline=st.sampled_from(["\n", "\r\n"]))
def test_jsonl_round_trip_preserves_ids_text_and_order(
    tmp_path_factory: pytest.TempPathFactory, rows: list[tuple[str, str]], newline: str
) -> None:
    path = write_jsonl(
        tmp_path_factory.mktemp("jsonl") / "rt.jsonl",
        [{"comment_id": i, "text": t} for i, t in rows],
        newline=newline,
    )

    assert read_comments(path, DEFAULT) == [record(i, t, n) for n, (i, t) in enumerate(rows)]


# --------------------------------------------------------------------------- encoding & format


def test_utf8_bom_is_tolerated_in_csv(tmp_path: Path) -> None:
    path = tmp_path / "bom.csv"
    path.write_bytes(b"\xef\xbb\xbfcomment_id,text\r\nc1,caf\xc3\xa9 tomatoes\r\n")

    assert read_comments(path, DEFAULT) == [record("c1", "café tomatoes", 0)]


def test_utf8_bom_is_tolerated_in_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "bom.jsonl"
    path.write_bytes(b'\xef\xbb\xbf{"comment_id": "c1", "text": "hello"}\n')

    assert read_comments(path, DEFAULT) == [record("c1", "hello", 0)]


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("latin1.csv", b"comment_id,text\nc1,caf\xe9 tomatoes\n"),
        ("latin1.jsonl", b'{"comment_id": "c0", "text": "ok"}\n{"text": "\xff"}\n'),
    ],
)
def test_invalid_utf8_is_rejected_with_line_number(tmp_path: Path, name: str, data: bytes) -> None:
    path = tmp_path / name
    path.write_bytes(data)

    message = ingest_error(path)

    assert "UTF-8" in message
    assert "line 2" in message


@pytest.mark.parametrize("name", ["comments.txt", "comments.json", "comments.tsv", "comments"])
def test_unsupported_suffix_is_rejected(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.write_text("comment_id,text\nc1,hello\n", encoding="utf-8")

    message = ingest_error(path)

    assert "unsupported" in message
    assert ".csv" in message
    assert ".jsonl" in message


def test_missing_file_is_an_ingest_error(tmp_path: Path) -> None:
    message = ingest_error(tmp_path / "nope.csv")

    assert "nope.csv" in message


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("zero.csv", b""),
        ("zero.jsonl", b""),
        ("blank.csv", b"\n\n  \n"),
        ("blank.jsonl", b"\n \n\t\n"),
        ("bom_only.csv", b"\xef\xbb\xbf"),
        ("header_only.csv", b"comment_id,text,permalink,created_at\n"),
    ],
)
def test_empty_file_is_rejected(tmp_path: Path, name: str, data: bytes) -> None:
    path = tmp_path / name
    path.write_bytes(data)

    message = ingest_error(path)

    assert name in message
    assert "no comments" in message or "empty" in message


# --------------------------------------------------------------------------- required fields


@pytest.mark.parametrize(
    ("header", "missing"), [(("text", "permalink"), "comment_id"), (("comment_id",), "text")]
)
def test_csv_missing_required_column_is_rejected(
    tmp_path: Path, header: tuple[str, ...], missing: str
) -> None:
    path = write_csv(tmp_path / "c.csv", header, [tuple("v" for _ in header)])

    message = ingest_error(path)

    assert f"'{missing}'" in message
    assert "column" in message


def test_csv_custom_mapping_requires_the_mapped_columns(two_row_csv: Path) -> None:
    message = ingest_error(two_row_csv, ColumnMapping(text_col="body"))

    assert "'body'" in message


@pytest.mark.parametrize("missing", ["comment_id", "text"])
def test_jsonl_missing_required_key_is_rejected_with_line_number(
    tmp_path: Path, missing: str
) -> None:
    second = {"comment_id": "c2", "text": TEXT2}
    del second[missing]
    path = write_jsonl(tmp_path / "c.jsonl", [{"comment_id": "c1", "text": TEXT1}, second])

    message = ingest_error(path)

    assert "line 2" in message
    assert f"'{missing}'" in message


@pytest.mark.parametrize(
    ("row", "column"),
    [
        (("", "some text"), "comment_id"),
        (("   ", "some text"), "comment_id"),
        (("c2", ""), "text"),
        (("c2", " \n\t "), "text"),
    ],
)
def test_csv_blank_id_or_text_is_rejected_with_row_number(
    tmp_path: Path, row: tuple[str, str], column: str
) -> None:
    path = write_csv(tmp_path / "b.csv", ("comment_id", "text"), [("c1", TEXT1), row])

    message = ingest_error(path)

    assert "row 2" in message
    assert f"'{column}'" in message


@pytest.mark.parametrize(
    ("obj", "column"),
    [
        ({"comment_id": "", "text": "x"}, "comment_id"),
        ({"comment_id": None, "text": "x"}, "comment_id"),
        ({"comment_id": "c2", "text": "   "}, "text"),
        ({"comment_id": "c2", "text": None}, "text"),
    ],
)
def test_jsonl_blank_id_or_text_is_rejected_with_line_number(
    tmp_path: Path, obj: dict[str, object], column: str
) -> None:
    path = write_jsonl(tmp_path / "b.jsonl", [{"comment_id": "c1", "text": TEXT1}, obj])

    message = ingest_error(path)

    assert "line 2" in message
    assert f"'{column}'" in message


@pytest.mark.parametrize("bad_id", [" c1", "c1 ", "\tc1", "c1\n"])
def test_id_with_surrounding_whitespace_is_rejected(tmp_path: Path, bad_id: str) -> None:
    # " c1" and "c1" would silently be two different comments; refuse the ambiguity.
    path = write_csv(tmp_path / "w.csv", ("comment_id", "text"), [(bad_id, TEXT1)])

    message = ingest_error(path)

    assert "row 1" in message
    assert "whitespace" in message


@pytest.mark.parametrize(
    ("obj", "column"),
    [
        ({"comment_id": 7, "text": "x"}, "comment_id"),
        ({"comment_id": "c1", "text": ["x"]}, "text"),
        ({"comment_id": "c1", "text": "x", "permalink": 5}, "permalink"),
        ({"comment_id": "c1", "text": "x", "created_at": 1719792000}, "created_at"),
        ({"comment_id": "c1", "text": True}, "text"),
    ],
)
def test_jsonl_non_string_values_are_rejected(
    tmp_path: Path, obj: dict[str, object], column: str
) -> None:
    path = write_jsonl(tmp_path / "types.jsonl", [obj])

    message = ingest_error(path)

    assert "line 1" in message
    assert f"'{column}'" in message
    assert "string" in message


# --------------------------------------------------------------------------- duplicates


def test_csv_duplicate_ids_name_the_column_and_rows_but_never_the_ids(tmp_path: Path) -> None:
    """F41: with a swapped mapping the "ids" are comment bodies, so they are never printed."""
    rows = [("c1", "a"), ("c2", "b"), ("c1", "c"), ("c3", "d"), ("c2", "e"), ("c1", "f")]
    path = write_csv(tmp_path / "dup.csv", ("comment_id", "text"), rows)

    message = ingest_error(path)

    assert "duplicate comment ids in column 'comment_id'" in message
    assert "rows 1, 3, 6; rows 2, 5" in message
    assert "c1" not in message and "c2" not in message and "c3" not in message


def test_jsonl_duplicate_ids_report_line_numbers(tmp_path: Path) -> None:
    path = tmp_path / "dup.jsonl"
    path.write_text(
        '{"comment_id": "xid", "text": "a"}\n\n{"comment_id": "xid", "text": "b"}\n',
        encoding="utf-8",
    )

    message = ingest_error(path)

    assert "lines 1, 3" in message
    assert "xid" not in message


def test_swapped_mapping_with_identical_bodies_never_echoes_the_body(tmp_path: Path) -> None:
    body = "SECRETBODY my kohlrabi died in Cheyenne"
    path = write_csv(tmp_path / "swapped.csv", ("cid", "body"), [("a", body), ("b", body)])

    message = ingest_error(path, ColumnMapping(id_col="body", text_col="cid"))

    assert "rows 1, 2" in message
    assert "'body'" in message
    assert SECRET not in message


def test_many_duplicate_groups_are_listed_briefly(tmp_path: Path) -> None:
    rows = [(f"c{i}", "t") for i in range(15)] * 2
    path = write_csv(tmp_path / "many.csv", ("comment_id", "text"), rows)

    message = ingest_error(path)

    assert "rows 1, 16; rows 2, 17" in message
    assert "(+5 more)" in message


# --------------------------------------------------------------------------- optional fields


def test_absent_optional_columns_give_none(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "min.csv", ("comment_id", "text"), [("c1", TEXT1)])

    assert read_comments(path, DEFAULT) == [record("c1", TEXT1, 0)]


@pytest.mark.parametrize("blank", ["", " ", "\t \t"])
def test_blank_optional_cells_give_none(tmp_path: Path, blank: str) -> None:
    path = write_csv(tmp_path / "blank.csv", HEADER, [("c1", TEXT1, blank, blank)])

    assert read_comments(path, DEFAULT) == [record("c1", TEXT1, 0)]


@pytest.mark.parametrize(
    ("written", "canonical"),
    [
        ("2024-07-01", "2024-07-01T00:00:00Z"),
        ("2024-07-01T12:30:00Z", "2024-07-01T12:30:00Z"),
        ("2024-07-01T12:30:00", "2024-07-01T12:30:00Z"),
        ("2024-07-01T12:30:00+02:00", "2024-07-01T10:30:00Z"),
        ("2024-07-01 20:15:00-05:00", "2024-07-02T01:15:00Z"),
        ("2024-07-01T12:30:00.123456Z", "2024-07-01T12:30:00Z"),
        ("  2024-07-01  ", "2024-07-01T00:00:00Z"),
    ],
)
def test_dates_are_normalised_to_canonical_utc(
    tmp_path: Path, written: str, canonical: str
) -> None:
    path = write_csv(
        tmp_path / "d.csv", ("comment_id", "text", "created_at"), [("c", "t", written)]
    )

    assert read_comments(path, DEFAULT)[0].created_at == canonical


@pytest.mark.parametrize(
    "bad", ["yesterday", "07/01/2024", "2024-13-01", "2024-02-30", "0001-01-01T00:00:00+05:00"]
)
def test_unparseable_date_is_rejected_with_row_number(tmp_path: Path, bad: str) -> None:
    path = write_csv(
        tmp_path / "d.csv",
        ("comment_id", "text", "created_at"),
        [("c1", "t", "2024-01-01"), ("c2", "t", bad)],
    )

    message = ingest_error(path)

    assert "row 2" in message
    assert "'created_at'" in message


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc&lc=Ugx",
        "http://example.org/thread/1#c2",
        "HTTPS://EXAMPLE.ORG/upper",
    ],
)
def test_http_permalinks_are_kept(tmp_path: Path, url: str) -> None:
    path = write_csv(tmp_path / "p.csv", ("comment_id", "text", "permalink"), [("c1", "t", url)])

    assert read_comments(path, DEFAULT)[0].permalink == url


def test_permalink_surrounding_whitespace_is_trimmed(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "p.csv", ("comment_id", "text", "permalink"), [("c1", "t", f"  {URL1}\t")]
    )

    assert read_comments(path, DEFAULT)[0].permalink == URL1


@pytest.mark.parametrize(
    "bad",
    [
        "ftp://example.org/x",
        "javascript:alert(1)",
        "www.youtube.com/watch?v=abc",
        "/relative/path",
        "https://",
        "https://exa mple.org",
        "data:text/html,hi",
    ],
)
def test_non_http_permalink_is_rejected_with_row_number(tmp_path: Path, bad: str) -> None:
    path = write_csv(
        tmp_path / "p.csv",
        ("comment_id", "text", "permalink"),
        [("c1", "t", URL1), ("c2", "t", bad)],
    )

    message = ingest_error(path)

    assert "row 2" in message
    assert "'permalink'" in message


# --------------------------------------------------------------------------- malformed input


@pytest.mark.parametrize(
    "line",
    [
        '{"comment_id": "c2", "text": ',
        '{"comment_id": "c2" "text": "x"}',
        "not json at all",
        '{"comment_id": "c2", "text": "x"} trailing',
    ],
)
def test_malformed_jsonl_line_is_rejected_with_line_number(tmp_path: Path, line: str) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(f'{{"comment_id": "c1", "text": "ok"}}\n{line}\n', encoding="utf-8")

    message = ingest_error(path)

    assert "line 2" in message
    assert "JSON" in message


@pytest.mark.parametrize("line", ['["c2", "text"]', '"just a string"', "42", "null", "true"])
def test_non_object_jsonl_line_is_rejected_with_line_number(tmp_path: Path, line: str) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(f'{{"comment_id": "c1", "text": "ok"}}\n\n{line}\n', encoding="utf-8")

    message = ingest_error(path)

    assert "line 3" in message
    assert "object" in message


def test_jsonl_duplicate_keys_in_one_object_are_rejected(tmp_path: Path) -> None:
    # json.loads would silently keep the last "text"; refuse the ambiguity instead.
    path = tmp_path / "dupkey.jsonl"
    path.write_text('{"comment_id": "c1", "text": "a", "text": "b"}\n', encoding="utf-8")

    message = ingest_error(path)

    assert "line 1" in message
    assert "duplicate" in message


@pytest.mark.parametrize(
    "row", [("c2", "unquoted, comma", "", "", ""), ("c2", "too few"), ("c2",)]
)
def test_csv_row_with_wrong_field_count_is_rejected(tmp_path: Path, row: tuple[str, ...]) -> None:
    # An unquoted comma would otherwise silently truncate the comment text.
    path = write_csv(tmp_path / "f.csv", HEADER, [("c1", "fine", "", ""), row])

    message = ingest_error(path)

    assert "row 2" in message
    assert "fields" in message


def test_csv_mapped_column_repeated_in_header_is_rejected(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "h.csv", ("comment_id", "text", "text"), [("c1", "a", "b")])

    message = ingest_error(path)

    assert "'text'" in message
    assert "header" in message


def test_csv_unterminated_quote_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "q.csv"
    path.write_text('comment_id,text\nc1,"never closed\n', encoding="utf-8")

    assert "CSV" in ingest_error(path)


@pytest.mark.parametrize(
    "mapping",
    [
        ColumnMapping(id_col="text"),
        ColumnMapping(permalink_col="text"),
        ColumnMapping(date_col="comment_id"),
        ColumnMapping(permalink_col="created_at"),
    ],
)
def test_mapping_must_use_distinct_columns(two_row_csv: Path, mapping: ColumnMapping) -> None:
    assert "distinct" in ingest_error(two_row_csv, mapping)


# --------------------------------------------------------------------------- explicit columns


def test_explicit_optional_csv_column_must_exist(two_row_csv: Path) -> None:
    """F36: a mistyped --permalink-col/--date-col fails instead of dropping every value."""
    for mapping, column in (
        (ColumnMapping(permalink_col="link", permalink_required=True), "link"),
        (ColumnMapping(date_col="posted", date_required=True), "posted"),
    ):
        message = ingest_error(two_row_csv, mapping)
        assert f"'{column}'" in message
        assert "header" in message


def test_explicit_default_named_column_must_exist_too(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "min.csv", ("comment_id", "text"), [("c1", TEXT1)])

    message = ingest_error(path, ColumnMapping(permalink_required=True))

    assert "'permalink'" in message


def test_explicit_optional_jsonl_key_must_appear_in_some_object(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path / "keys.jsonl",
        [{"comment_id": "c1", "text": TEXT1, "url": URL1}, {"comment_id": "c2", "text": TEXT2}],
    )

    message = ingest_error(path, ColumnMapping(permalink_col="link", permalink_required=True))

    assert "'link'" in message
    assert path.name in message
    found = read_comments(path, ColumnMapping(permalink_col="url", permalink_required=True))
    assert [r.permalink for r in found] == [URL1, None]  # absent in one object is fine


def test_implicit_optional_columns_may_be_absent(tmp_path: Path) -> None:
    path = write_jsonl(tmp_path / "plain.jsonl", [{"comment_id": "c1", "text": TEXT1}])

    assert read_comments(path, DEFAULT) == [record("c1", TEXT1, 0)]


# --------------------------------------------------------------------------- invalid Unicode


@pytest.mark.parametrize("column", ["comment_id", "text", "permalink", "created_at"])
def test_lone_surrogate_is_rejected_with_file_line_and_column(tmp_path: Path, column: str) -> None:
    """F28: a truncated emoji (escaped lone surrogate) never puts the cell into the error."""
    cells = {"comment_id": "yt-77", "text": f"{SECRET} Jane at 12 Elm St", "permalink": None}
    cells[column] = f"{SECRET} grew Sungold \ud83c"
    path = tmp_path / "surrogate.jsonl"
    path.write_text(
        '{"comment_id": "ok", "text": "fine"}\n' + json.dumps(cells) + "\n", encoding="utf-8"
    )

    message = ingest_error(path)

    assert path.name in message
    assert "line 2" in message
    assert f"'{column}'" in message
    assert SECRET not in message and "Elm" not in message


def test_any_record_validation_error_is_reported_without_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backstop: a pydantic error (whose text quotes the input) never reaches the message."""

    class TinyRecord(CommentRecord):
        text: str = Field(min_length=1, max_length=3)

    monkeypatch.setattr(ingest_module, "CommentRecord", TinyRecord)
    path = write_csv(tmp_path / "long.csv", ("comment_id", "text"), [("c1", f"{SECRET} long")])

    message = ingest_error(path)

    assert "row 1" in message
    assert "'text'" in message
    assert path.name in message
    assert SECRET not in message


# --------------------------------------------------------------------------- privacy


@settings(deadline=None)
@given(secret=SECRETS, column=st.sampled_from(["permalink", "created_at"]))
def test_bad_optional_cell_errors_never_echo_the_cell(
    tmp_path_factory: pytest.TempPathFactory, secret: str, column: str
) -> None:
    # Swapped columns put comment text in metadata cells; errors must not repeat it.
    cells = {"comment_id": "c1", "text": "fine", "permalink": "", "created_at": ""}
    cells[column] = secret
    path = write_csv(tmp_path_factory.mktemp("p") / "s.csv", HEADER, [tuple(cells.values())])

    assert SECRET not in ingest_error(path)


@settings(deadline=None)
@given(secret=SECRETS)
def test_structural_errors_never_echo_comment_text(
    tmp_path_factory: pytest.TempPathFactory, secret: str
) -> None:
    folder = tmp_path_factory.mktemp("s")
    paths = [
        write_csv(folder / "extra.csv", ("comment_id", "text"), [("c1", secret, secret)]),
        write_csv(folder / "pad.csv", ("comment_id", "text"), [(f" {secret}", "t")]),
        write_csv(folder / "noheader.csv", ("c0", secret), [("c1", secret)]),
        write_jsonl(folder / "types.jsonl", [{"comment_id": "c1", "text": [secret]}]),
    ]
    trunc = folder / "trunc.jsonl"
    trunc.write_text(json.dumps({"comment_id": "c1", "text": secret})[:-2], encoding="utf-8")

    for path in [*paths, trunc]:
        assert SECRET not in ingest_error(path)


def test_reading_logs_counts_but_never_text(two_row_csv: Path, captured_logs: list[str]) -> None:
    read_comments(two_row_csv, DEFAULT)

    assert any("2 comments" in message for message in captured_logs)
    assert not any(TEXT1 in m or TEXT2 in m for m in captured_logs)


# --------------------------------------------------------------------------- dataset hash


def test_dataset_hash_is_sha256_of_file_bytes(two_row_csv: Path) -> None:
    assert dataset_hash(two_row_csv) == hashlib.sha256(two_row_csv.read_bytes()).hexdigest()


@settings(deadline=None)
@given(data=st.binary(max_size=200), flip=st.integers(min_value=0, max_value=199))
def test_dataset_hash_is_stable_and_content_sensitive(
    tmp_path_factory: pytest.TempPathFactory, data: bytes, flip: int
) -> None:
    folder = tmp_path_factory.mktemp("h")
    first, copy, changed = folder / "a.csv", folder / "b.jsonl", folder / "c.csv"
    first.write_bytes(data)
    copy.write_bytes(data)
    mutated = bytearray(data or b"\x00")
    mutated[flip % len(mutated)] ^= 0xFF
    changed.write_bytes(bytes(mutated))

    assert dataset_hash(first) == dataset_hash(first) == dataset_hash(copy)
    assert dataset_hash(changed) != dataset_hash(first)
    assert len(dataset_hash(first)) == 64


def test_dataset_hash_of_missing_file_is_an_ingest_error(tmp_path: Path) -> None:
    with pytest.raises(IngestError, match=r"gone\.csv"):
        dataset_hash(tmp_path / "gone.csv")


# --------------------------------------------------------------------------- build_source


def make_source(path: Path, **overrides: Any) -> SourceRecord:
    params: dict[str, Any] = {
        "source_id": "export-2024",
        "kind": "comments_export",
        "rights_status": "private_unverified",
        "source_url": None,
        "expires_at": None,
        "imported_at": IMPORTED,
    }
    params.update(overrides)
    return build_source(path, **params)


@pytest.mark.parametrize("rights", RIGHTS_STATUSES)
def test_build_source_records_provenance_and_rights(
    two_row_csv: Path, rights: RightsStatus
) -> None:
    source = build_source(
        two_row_csv,
        source_id="export-2024",
        kind="comments_export",
        rights_status=rights,
        source_url="https://www.youtube.com/watch?v=abc",
        expires_at=None,
        imported_at=IMPORTED,
    )

    assert source.source_id == "export-2024"
    assert source.kind == "comments_export"
    assert source.rights_status == rights
    assert source.source_url == "https://www.youtube.com/watch?v=abc"
    assert source.imported_at == IMPORTED
    assert source.expires_at is None
    assert source.dataset_hash == dataset_hash(two_row_csv)


@pytest.mark.parametrize(
    ("written", "canonical"),
    [
        ("2027-01-01", "2027-01-01T00:00:00Z"),
        ("2027-01-01T05:00:00+05:00", "2027-01-01T00:00:00Z"),
        ("2027-01-01T00:00:00Z", "2027-01-01T00:00:00Z"),
    ],
)
def test_build_source_normalises_expires_at(
    two_row_csv: Path, written: str, canonical: str
) -> None:
    assert make_source(two_row_csv, expires_at=written).expires_at == canonical


def test_build_source_normalises_imported_at(two_row_csv: Path) -> None:
    source = make_source(two_row_csv, imported_at="2026-10-15T14:00:00+02:00")

    assert source.imported_at == IMPORTED


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"expires_at": "next spring"}, "expires_at"),
        ({"imported_at": "today"}, "imported_at"),
        ({"rights_status": "public"}, "rights_status"),
        ({"rights_status": "private"}, "rights_status"),
        ({"source_url": "youtube.com/watch?v=abc"}, "source_url"),
        ({"source_url": ""}, "source_url"),
        ({"source_id": ""}, "source_id"),
        ({"source_id": "  "}, "source_id"),
        ({"source_id": " padded"}, "source_id"),
        ({"kind": ""}, "kind"),
    ],
)
def test_build_source_rejects_invalid_arguments(
    two_row_csv: Path, overrides: dict[str, Any], fragment: str
) -> None:
    with pytest.raises(IngestError, match=fragment):
        make_source(two_row_csv, **overrides)


def test_build_source_requires_a_readable_file(tmp_path: Path) -> None:
    with pytest.raises(IngestError, match=r"missing\.csv"):
        make_source(tmp_path / "missing.csv")


# --------------------------------------------------------------------------- import_file
# End-to-end import into SQLite and idempotent re-import (SPEC §8 flow 5, §11 #14).

EXTRACTION_CFG = load_config().extraction
TABLES = ("sources", "comments", "parses", "mentions", "environment_facts")
TEXT2_CHANGED = "Cherokee Purple cracked after the downpour."


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


C1_EXTRACTION = {
    "comment_id": "c1",
    "text_hash": sha256_hex(TEXT1),
    "extractor_version": "test-v1",
    "status": "parsed",
    "mentions": [
        {
            "crop": "tomatoes",
            "variety_as_written": "Sun Gold",
            "topics": ["flavor"],
            "evidence_quote": "Sun Gold in 5 gallon pots",
            "environment": [
                {
                    "key": "method",
                    "value_as_written": "5 gallon pots",
                    "normalized_value": "container",
                    "evidence_quote": "Sun Gold in 5 gallon pots",
                    "applicability": "this_mention",
                    "basis": "reported_garden_context",
                },
                {
                    "key": "zone",
                    "value_as_written": "zone 6b",
                    "normalized_value": "6b",
                    "evidence_quote": "zone 6b",
                    "applicability": "whole_comment",
                    "basis": "reported_garden_context",
                },
            ],
        }
    ],
}
C2_EXTRACTION = {
    "comment_id": "c2",
    "text_hash": sha256_hex(TEXT2),
    "extractor_version": "test-v1",
    "status": "parsed",
    "mentions": [
        {
            "crop": "tomato",
            "variety_as_written": "Cherokee Purple",
            "topics": ["disease"],
            "evidence_quote": "Cherokee Purple split",
        }
    ],
}


@pytest.fixture
def memory_db() -> Iterator[sqlite3.Connection]:
    conn = db.connect(db.MEMORY_PATH)
    yield conn
    conn.close()


def private_stats(
    *, inserted: int = 0, updated: int = 0, unchanged: int = 0, deleted: int = 0
) -> ImportStats:
    return ImportStats(
        source_id="export-2024",
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        deleted=deleted,
        rights_status="private_unverified",
        expires_at=None,
    )


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in TABLES
    }


def private_views(conn: sqlite3.Connection) -> list[Any]:
    return db.load_comment_views(conn, "private", IMPORTED, None)


def text_hits(conn: sqlite3.Connection, text: str) -> list[str]:
    return db.visible_comment_ids(conn, "private", IMPORTED, text)


def import_csv(
    conn: sqlite3.Connection, path: Path, rows: Iterable[Sequence[str]] | None = None
) -> ImportStats:
    """(Re)write ``path`` when ``rows`` is given, then import it as source ``export-2024``."""
    if rows is not None:
        write_csv(path, HEADER, rows)
    return import_file(conn, path, DEFAULT, make_source(path))


def load_extraction_lines(conn: sqlite3.Connection, path: Path, *lines: object) -> None:
    write_jsonl(path, lines)
    report = load_extractions(conn, path, EXTRACTION_CFG, IMPORTED)
    assert report.rejections == ()
    assert report.comments_loaded == len(lines)


@pytest.fixture
def extracted_db(
    memory_db: sqlite3.Connection, two_row_csv: Path, tmp_path: Path
) -> sqlite3.Connection:
    """Both comments of ``two_row_csv`` imported and extracted."""
    import_csv(memory_db, two_row_csv)
    load_extraction_lines(memory_db, tmp_path / "parsed.jsonl", C1_EXTRACTION, C2_EXTRACTION)
    return memory_db


def test_import_file_stores_exact_records_under_the_source(
    memory_db: sqlite3.Connection, two_row_csv: Path
) -> None:
    stats = import_csv(memory_db, two_row_csv)

    assert stats == private_stats(inserted=2)
    assert [
        (v.comment_id, v.text, v.permalink, v.created_at, v.source_order, v.parse_status)
        for v in private_views(memory_db)
    ] == [
        ("c1", TEXT1, URL1, "2024-07-01T10:00:00Z", 0, None),
        ("c2", TEXT2, None, None, 1, None),
    ]
    source = memory_db.execute("SELECT * FROM sources").fetchone()
    assert source["rights_status"] == "private_unverified"
    assert source["dataset_hash"] == dataset_hash(two_row_csv)
    assert db.load_comment_views(memory_db, "public", IMPORTED, None) == []


def test_reimporting_the_identical_file_changes_nothing_and_keeps_extractions(
    extracted_db: sqlite3.Connection, two_row_csv: Path
) -> None:
    before_counts, before_views = table_counts(extracted_db), private_views(extracted_db)

    stats = import_csv(extracted_db, two_row_csv)

    assert stats == private_stats(unchanged=2)
    assert table_counts(extracted_db) == before_counts
    assert private_views(extracted_db) == before_views
    assert before_counts["environment_facts"] == 2
    assert text_hits(extracted_db, "Cherokee") == ["c2"]  # no duplicate FTS rows


def test_changed_comment_text_drops_only_that_comments_extraction(
    extracted_db: sqlite3.Connection, two_row_csv: Path
) -> None:
    stats = import_csv(
        extracted_db,
        two_row_csv,
        [("c1", TEXT1, URL1, "2024-07-01T12:00:00+02:00"), ("c2", TEXT2_CHANGED, "", "")],
    )

    assert (stats.updated, stats.unchanged, stats.inserted, stats.deleted) == (1, 1, 0, 0)
    c1, c2 = private_views(extracted_db)
    assert (c1.parse_status, len(c1.mentions), len(c1.mentions[0].facts)) == ("parsed", 1, 2)
    assert (c2.text, c2.parse_status, c2.mentions) == (TEXT2_CHANGED, None, ())
    assert text_hits(extracted_db, "downpour") == ["c2"]
    assert text_hits(extracted_db, "split") == []
    assert db.iter_comments_for_extraction(extracted_db, only_unparsed=True, now=IMPORTED) == [
        ("c2", TEXT2_CHANGED, sha256_hex(TEXT2_CHANGED))
    ]


def test_rows_missing_from_a_reimport_are_deleted_with_their_extraction(
    extracted_db: sqlite3.Connection, two_row_csv: Path
) -> None:
    stats = import_csv(extracted_db, two_row_csv, [("c1", TEXT1, URL1, "2024-07-01")])

    # c1 keeps its text, so it is "unchanged" (and keeps its extraction) but its metadata is
    # refreshed from the new file.
    assert (stats.deleted, stats.unchanged, stats.updated) == (1, 1, 0)
    assert [(v.comment_id, v.created_at) for v in private_views(extracted_db)] == [
        ("c1", "2024-07-01T00:00:00Z")
    ]
    assert db.stored_texts(extracted_db, ["c1", "c2"]) == {"c1": TEXT1}
    assert text_hits(extracted_db, "Cherokee") == []
    assert table_counts(extracted_db) == {
        "sources": 1,
        "comments": 1,
        "parses": 1,
        "mentions": 1,
        "environment_facts": 2,
    }


def test_invalid_reimport_writes_nothing(
    extracted_db: sqlite3.Connection, two_row_csv: Path
) -> None:
    before_counts, before_views = table_counts(extracted_db), private_views(extracted_db)
    old_hash = extracted_db.execute("SELECT dataset_hash FROM sources").fetchone()[0]

    with pytest.raises(IngestError, match="duplicate comment ids"):
        import_csv(extracted_db, two_row_csv, [("c1", TEXT1, "", ""), ("c1", TEXT2, "", "")])

    assert table_counts(extracted_db) == before_counts
    assert private_views(extracted_db) == before_views
    assert extracted_db.execute("SELECT dataset_hash FROM sources").fetchone()[0] == old_hash


def test_comment_id_owned_by_another_source_is_rejected_atomically(
    extracted_db: sqlite3.Connection, tmp_path: Path
) -> None:
    before_views = private_views(extracted_db)
    other = write_csv(tmp_path / "other.csv", HEADER, [("c9", "new", "", ""), ("c1", "x", "", "")])

    with pytest.raises(ValueError, match="c1"):
        import_file(extracted_db, other, DEFAULT, make_source(other, source_id="other-export"))

    sources = [row[0] for row in extracted_db.execute("SELECT source_id FROM sources")]
    assert sources == ["export-2024"]
    assert private_views(extracted_db) == before_views


def test_import_refuses_a_file_that_changed_after_its_source_was_built(
    extracted_db: sqlite3.Connection, two_row_csv: Path
) -> None:
    """The stored dataset_hash must describe exactly the bytes whose comments are stored."""
    before_counts, before_views = table_counts(extracted_db), private_views(extracted_db)
    stale = make_source(two_row_csv)
    write_csv(two_row_csv, HEADER, [("c1", TEXT1, URL1, "2024-07-01"), ("c3", "New.", "", "")])

    with pytest.raises(IngestError, match="dataset_hash") as caught:
        import_file(extracted_db, two_row_csv, DEFAULT, stale)

    assert TEXT1 not in str(caught.value)
    assert table_counts(extracted_db) == before_counts
    assert private_views(extracted_db) == before_views
    fresh = import_file(extracted_db, two_row_csv, DEFAULT, make_source(two_row_csv))
    assert (fresh.inserted, fresh.deleted) == (1, 1)


def test_a_different_export_under_the_same_source_id_needs_replace(
    extracted_db: sqlite3.Connection, tmp_path: Path
) -> None:
    """F06: an unrelated file with the same source id cannot silently replace the source."""
    other = write_csv(tmp_path / "other.csv", HEADER, [("v2-1", "Okra.", "", "")])
    before = table_counts(extracted_db)

    with pytest.raises(ValueError, match="export-2024"):
        import_file(extracted_db, other, DEFAULT, make_source(other))
    assert table_counts(extracted_db) == before

    stats = import_file(extracted_db, other, DEFAULT, make_source(other), replace=True)
    assert stats == private_stats(inserted=1, deleted=2)


def test_import_file_keeps_or_clears_the_stored_expiry(
    memory_db: sqlite3.Connection, two_row_csv: Path
) -> None:
    expiry = "2030-01-01T00:00:00Z"
    first = import_file(
        memory_db, two_row_csv, DEFAULT, make_source(two_row_csv, expires_at=expiry)
    )
    kept = import_file(memory_db, two_row_csv, DEFAULT, make_source(two_row_csv))
    cleared = import_file(
        memory_db, two_row_csv, DEFAULT, make_source(two_row_csv), clear_expiry=True
    )

    assert (first.expires_at, kept.expires_at, cleared.expires_at) == (expiry, expiry, None)


SMALL_IDS = st.sampled_from(["a", "b", "c", "d"])
SMALL_TEXTS = st.sampled_from(["Sungold in pots.", "Juliet in the ground.", "Brandywine!"])
EXPORTS = st.lists(st.tuples(SMALL_IDS, SMALL_TEXTS), max_size=4, unique_by=lambda r: r[0])


@settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(first=EXPORTS.filter(bool), second=EXPORTS.filter(bool))
def test_reimport_leaves_exactly_the_latest_export(
    tmp_path: Path, first: list[tuple[str, str]], second: list[tuple[str, str]]
) -> None:
    """Any import sequence stores exactly the latest file, with exact change counts."""
    path = tmp_path / "export.jsonl"
    old, new = dict(first), dict(second)
    replace = old.keys().isdisjoint(new)  # an unrelated export needs an explicit replace
    conn = db.connect(db.MEMORY_PATH)
    try:
        for rows in (first, second):
            write_jsonl(path, [{"comment_id": cid, "text": text} for cid, text in rows])
            stats = import_file(conn, path, DEFAULT, make_source(path), replace=replace)
        again = import_file(conn, path, DEFAULT, make_source(path))
        stored = db.stored_texts(conn, sorted(old | new))
    finally:
        conn.close()

    assert stored == dict(sorted(new.items()))
    assert stats.inserted == len(new.keys() - old.keys())
    assert stats.deleted == len(old.keys() - new.keys())
    assert stats.updated == sum(old[k] != new[k] for k in old.keys() & new.keys())
    assert stats.unchanged == sum(old[k] == new[k] for k in old.keys() & new.keys())
    assert again == private_stats(unchanged=len(new))
