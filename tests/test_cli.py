"""Tests for the command-line interface (docs/ARCHITECTURE.md "CLI", SPEC §10).

Every command is run in-process through ``crop_explorer.__main__.main`` against a config file
in the test's temporary directory (see ``conftest.py``). The CLI prints exactly one JSON
document of ids and counts on success; on bad input it prints one ``error: ...`` line to stderr
and exits 2. It never prints or logs comment text.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from crop_explorer import db

if TYPE_CHECKING:
    from conftest import CliEnv, CliRun

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
NOW = "2026-10-15T12:00:00Z"
DEMO_SOURCE = "synthetic-demo"
DEMO_COMMENTS = FIXTURES_DIR / "synthetic_comments.jsonl"
DEMO_EXTRACTIONS = FIXTURES_DIR / "synthetic_extractions.jsonl"
COMMANDS = (
    "ingest",
    "load-extractions",
    "export-for-extraction",
    "delete-source",
    "purge-expired",
    "summary",
    "demo",
)
STATS_KEYS = [
    "source_id",
    "inserted",
    "updated",
    "unchanged",
    "deleted",
    "rights_status",
    "expires_at",
]
REPORT_KEYS = [
    "lines_read",
    "schema_errors",
    "unknown_comment_ids",
    "duplicate_comment_ids",
    "comments_loaded",
    "status_counts",
    "mentions_accepted",
    "mentions_rejected",
    "facts_accepted",
    "facts_rejected",
    "unresolved_identities",
    "rejections",
]
DEMO_SUMMARY = {"sources": 1, "comments": 69, "parsed_comments": 66, "mentions": 71, "facts": 82}
EMPTY_SUMMARY = {"sources": 0, "comments": 0, "parsed_comments": 0, "mentions": 0, "facts": 0}
SECRET_TEXT = "Secretive Sungold notes about my neighbour's garden on Elm Street"
NEVER_EXTRACTED = ["syn-0004", "syn-0025", "syn-0069"]


# --------------------------------------------------------------------------- helpers


def write_csv(path: Path, header: Sequence[str], rows: Iterable[Sequence[str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def write_jsonl(path: Path, rows: Iterable[object]) -> Path:
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    path.write_text(text, encoding="utf-8")
    return path


def comment_rows(*pairs: tuple[str, str]) -> list[dict[str, Any]]:
    return [
        {"comment_id": cid, "text": text, "permalink": None, "created_at": None}
        for cid, text in pairs
    ]


def ok(run: CliRun) -> Any:
    """Assert success with a single JSON document on stdout and return it."""
    assert run.code == 0, run.err
    assert run.out.endswith("\n")
    assert len(run.out.splitlines()) == 1
    return run.json()


def failed(run: CliRun) -> str:
    """Assert the CLI's error contract (exit 2, nothing on stdout, one stderr line)."""
    assert run.code == 2
    assert run.out == ""
    assert len(run.err_lines) == 1, run.err
    (line,) = run.err_lines
    assert line.startswith("error: ")
    return line


def source_row(env: CliEnv, source_id: str) -> sqlite3.Row:
    conn = env.connect()
    try:
        row = conn.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row


def summary(env: CliEnv, scope: str | None = None, *extra: str) -> dict[str, Any]:
    args = ["summary", *(["--scope", scope] if scope else []), *extra]
    payload: dict[str, Any] = ok(env.run(*args))
    return payload


def counts(payload: dict[str, Any]) -> dict[str, int]:
    return {key: value for key, value in payload.items() if key != "scope"}


def fixture_texts() -> dict[str, str]:
    lines = DEMO_COMMENTS.read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines if line.strip()]
    return {row["comment_id"]: row["text"] for row in rows}


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fixture_quotes() -> set[str]:
    lines = DEMO_EXTRACTIONS.read_text(encoding="utf-8").splitlines()
    quotes: set[str] = set()
    for line in filter(str.strip, lines):
        for mention in json.loads(line)["mentions"]:
            quotes.add(mention["evidence_quote"])
            quotes.update(fact["evidence_quote"] for fact in mention["environment"])
    return quotes


@pytest.fixture
def private_csv(tmp_path: Path) -> Path:
    """A CSV export with non-default column names and an extra column."""
    return write_csv(
        tmp_path / "my-export.csv",
        ("id", "body", "url", "posted", "likes"),
        [
            (
                "yt-1",
                "Sun Gold in 5 gallon pots, zone 6b.",
                "https://example.org/c/1",
                "2024-07-01",
                "12",
            ),
            ("yt-2", "  Cherokee Purple split after the rain.\n", "", "", "0"),
        ],
    )


MAPPING_ARGS = ("--id-col", "id", "--text-col", "body", "--permalink-col", "url")
MAPPING_ARGS_WITH_DATE = (*MAPPING_ARGS, "--date-col", "posted")


# --------------------------------------------------------------------------- entry point


def test_module_entry_point_lists_every_command() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "crop_explorer", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    for command in COMMANDS:
        assert command in proc.stdout


def test_missing_command_is_a_usage_error(cli_env: CliEnv) -> None:
    run = cli_env.run()

    assert run.code == 2
    assert run.out == ""
    assert "usage" in run.err


@pytest.mark.parametrize(
    "argv",
    [
        ["frobnicate"],
        ["ingest", "x.csv"],  # --scope is required
        ["ingest", "x.csv", "--scope", "public"],  # not an import scope
        ["summary", "--scope", "synthetic"],  # not a viewer scope
        ["export-for-extraction"],  # --out is required
        ["delete-source"],
    ],
)
def test_invalid_arguments_exit_2_with_usage(cli_env: CliEnv, argv: list[str]) -> None:
    run = cli_env.run(*argv)

    assert run.code == 2
    assert run.out == ""
    assert "usage" in run.err


# --------------------------------------------------------------------------- demo


def test_demo_ingests_fixtures_as_synthetic_and_loads_extractions(cli_env: CliEnv) -> None:
    payload = ok(cli_env.run("demo"))

    assert payload["import"] == {
        "source_id": DEMO_SOURCE,
        "inserted": 69,
        "updated": 0,
        "unchanged": 0,
        "deleted": 0,
        "rights_status": "synthetic",
        "expires_at": None,
    }
    report = payload["extraction"]
    assert list(report) == REPORT_KEYS
    assert report["lines_read"] == report["comments_loaded"] == 66
    assert report["status_counts"] == {"parsed": 61, "partial": 1, "unresolved": 4}
    assert (report["mentions_accepted"], report["facts_accepted"]) == (71, 82)
    assert report["rejections"] == 0
    row = source_row(cli_env, DEMO_SOURCE)
    assert (row["rights_status"], row["kind"]) == ("synthetic", "synthetic_fixture")
    assert row["dataset_hash"] == hashlib.sha256(DEMO_COMMENTS.read_bytes()).hexdigest()
    assert row["expires_at"] is None


def test_demo_rerun_is_idempotent(cli_env: CliEnv) -> None:
    first = ok(cli_env.run("demo"))
    second = ok(cli_env.run("demo"))

    assert second["import"] == {
        "source_id": DEMO_SOURCE,
        "inserted": 0,
        "updated": 0,
        "unchanged": 69,
        "deleted": 0,
        "rights_status": "synthetic",
        "expires_at": None,
    }
    assert second["extraction"] == first["extraction"]
    assert counts(summary(cli_env, "private")) == DEMO_SUMMARY


# --------------------------------------------------------------------------- summary


def test_summary_defaults_to_the_configured_public_scope(demo_env: CliEnv) -> None:
    assert summary(demo_env) == {"scope": "public", **DEMO_SUMMARY}
    assert summary(demo_env, "private") == {"scope": "private", **DEMO_SUMMARY}


def test_summary_public_scope_never_counts_private_imports(
    demo_env: CliEnv, private_csv: Path
) -> None:
    ok(demo_env.run("ingest", private_csv, "--scope", "private", *MAPPING_ARGS))

    assert counts(summary(demo_env)) == DEMO_SUMMARY
    assert counts(summary(demo_env, "private")) == {**DEMO_SUMMARY, "sources": 2, "comments": 71}


# --------------------------------------------------------------------------- ingest


def test_ingest_csv_with_column_mapping_and_defaults(cli_env: CliEnv, private_csv: Path) -> None:
    payload = ok(cli_env.run("ingest", private_csv, "--scope", "private", *MAPPING_ARGS_WITH_DATE))

    assert list(payload) == STATS_KEYS
    assert payload == {
        "source_id": "my-export",  # default: the file stem
        "inserted": 2,
        "updated": 0,
        "unchanged": 0,
        "deleted": 0,
        "rights_status": "private_unverified",
        "expires_at": None,
    }
    row = source_row(cli_env, "my-export")
    assert row["kind"] == "comments_export"
    assert row["rights_status"] == "private_unverified"
    assert row["source_url"] is None
    assert row["expires_at"] is None
    assert row["dataset_hash"] == hashlib.sha256(private_csv.read_bytes()).hexdigest()
    conn = cli_env.connect()
    try:
        views = db.load_comment_views(conn, "private", NOW, None)
    finally:
        conn.close()
    assert [(v.comment_id, v.text, v.permalink, v.created_at) for v in views] == [
        (
            "yt-1",
            "Sun Gold in 5 gallon pots, zone 6b.",
            "https://example.org/c/1",
            "2024-07-01T00:00:00Z",
        ),
        ("yt-2", "  Cherokee Purple split after the rain.\n", None, None),
    ]


def test_ingest_records_source_id_kind_url_and_expiry(cli_env: CliEnv, private_csv: Path) -> None:
    payload = ok(
        cli_env.run(
            "ingest",
            private_csv,
            "--scope",
            "cleared",
            "--source-id",
            "creator-approved-2026",
            "--kind",
            "consented_reports",
            "--source-url",
            "https://example.org/video",
            "--expires-at",
            "2030-01-01T05:00:00+05:00",
            *MAPPING_ARGS,
        )
    )

    assert payload["source_id"] == "creator-approved-2026"
    row = source_row(cli_env, "creator-approved-2026")
    assert row["kind"] == "consented_reports"
    assert row["rights_status"] == "cleared"
    assert row["source_url"] == "https://example.org/video"
    assert row["expires_at"] == "2030-01-01T00:00:00Z"


@pytest.mark.parametrize(
    ("scope", "rights"),
    [("private", "private_unverified"), ("synthetic", "synthetic"), ("cleared", "cleared")],
)
def test_ingest_scope_sets_rights_status(
    cli_env: CliEnv, private_csv: Path, scope: str, rights: str
) -> None:
    ok(cli_env.run("ingest", private_csv, "--scope", scope, *MAPPING_ARGS))

    assert source_row(cli_env, "my-export")["rights_status"] == rights
    public = 0 if rights == "private_unverified" else 2
    assert summary(cli_env)["comments"] == public


def test_ingest_jsonl_with_default_mapping_and_idempotent_reimport(
    cli_env: CliEnv, tmp_path: Path
) -> None:
    path = write_jsonl(tmp_path / "export.jsonl", comment_rows(("a", "one"), ("b", "two")))

    first = ok(cli_env.run("ingest", path, "--scope", "private"))
    second = ok(cli_env.run("ingest", path, "--scope", "private"))

    assert (first["inserted"], first["unchanged"]) == (2, 0)
    assert (second["inserted"], second["unchanged"]) == (0, 2)
    assert counts(summary(cli_env, "private"))["comments"] == 2


def test_global_options_are_accepted_after_the_command(cli_env: CliEnv, tmp_path: Path) -> None:
    other_db = tmp_path / "elsewhere" / "other.sqlite"

    ok(cli_env.run("demo", "--db", other_db))

    assert other_db.exists()
    assert summary(cli_env, None, "--db", str(other_db))["comments"] == 69


def test_db_option_overrides_the_configured_database(cli_env: CliEnv, tmp_path: Path) -> None:
    other_db = tmp_path / "override.sqlite"

    ok(cli_env.run("--db", other_db, "demo"))

    assert other_db.exists()
    assert not cli_env.db_path.exists()
    assert counts(ok(cli_env.run("--db", other_db, "summary"))) == DEMO_SUMMARY
    assert counts(summary(cli_env)) == EMPTY_SUMMARY  # the configured database is separate


# --------------------------------------------------------------------------- rights on re-import


def test_reingest_without_expires_at_keeps_the_expiry(cli_env: CliEnv, tmp_path: Path) -> None:
    """F02: re-running ingest after an update never silently drops the expiry."""
    path = write_jsonl(tmp_path / "clip.jsonl", comment_rows(("a", "one"), ("b", "two")))
    first = ok(cli_env.run("ingest", path, "--scope", "cleared", "--expires-at", "2030-01-01"))

    again = ok(cli_env.run("ingest", path, "--scope", "cleared"))

    assert (first["rights_status"], first["expires_at"]) == ("cleared", "2030-01-01T00:00:00Z")
    assert (again["rights_status"], again["expires_at"]) == ("cleared", "2030-01-01T00:00:00Z")
    assert source_row(cli_env, "clip")["expires_at"] == "2030-01-01T00:00:00Z"


def test_clear_expiry_removes_the_expiry(cli_env: CliEnv, tmp_path: Path) -> None:
    path = write_jsonl(tmp_path / "clip.jsonl", comment_rows(("a", "one")))
    ok(cli_env.run("ingest", path, "--scope", "cleared", "--expires-at", "2030-01-01"))

    payload = ok(cli_env.run("ingest", path, "--scope", "cleared", "--clear-expiry"))

    assert payload["expires_at"] is None
    assert source_row(cli_env, "clip")["expires_at"] is None


def test_clear_expiry_and_expires_at_are_mutually_exclusive(
    cli_env: CliEnv, private_csv: Path
) -> None:
    run = cli_env.run(
        "ingest", private_csv, "--scope", "private", "--expires-at", "2030-01-01", "--clear-expiry"
    )

    assert (run.code, run.out) == (2, "")
    assert "not allowed with" in run.err


def test_reingesting_an_expired_source_is_refused_until_purged(
    cli_env: CliEnv, tmp_path: Path
) -> None:
    path = write_jsonl(tmp_path / "old.jsonl", comment_rows(("o1", SECRET_TEXT)))
    ok(cli_env.run("ingest", path, "--scope", "cleared", "--expires-at", "2000-01-01"))

    line = failed(cli_env.run("ingest", path, "--scope", "cleared", "--clear-expiry"))

    assert "old" in line and "purge-expired" in line
    assert "Secretive" not in line
    assert source_row(cli_env, "old")["expires_at"] == "2000-01-01T00:00:00Z"
    ok(cli_env.run("purge-expired"))
    assert ok(cli_env.run("ingest", path, "--scope", "cleared"))["inserted"] == 1


def test_a_different_export_with_the_same_file_name_needs_replace(
    cli_env: CliEnv, tmp_path: Path
) -> None:
    """F06: video2/comments.jsonl must not silently replace video1/comments.jsonl."""
    (tmp_path / "video1").mkdir()
    (tmp_path / "video2").mkdir()
    first = write_jsonl(
        tmp_path / "video1" / "comments.jsonl", comment_rows(("v1-a", "a"), ("v1-b", "b"))
    )
    second = write_jsonl(
        tmp_path / "video2" / "comments.jsonl", comment_rows(("v2-a", SECRET_TEXT))
    )
    ok(cli_env.run("ingest", first, "--scope", "private"))

    line = failed(cli_env.run("ingest", second, "--scope", "private"))

    assert "'comments'" in line and "2 stored" in line and "--replace" in line
    assert "Secretive" not in line
    assert counts(summary(cli_env, "private"))["comments"] == 2
    payload = ok(cli_env.run("ingest", second, "--scope", "private", "--replace"))
    assert (payload["inserted"], payload["deleted"]) == (1, 2)


def test_changing_the_scope_of_a_stored_source_needs_replace(
    cli_env: CliEnv, private_csv: Path
) -> None:
    ok(cli_env.run("ingest", private_csv, "--scope", "private", *MAPPING_ARGS))

    line = failed(cli_env.run("ingest", private_csv, "--scope", "cleared", *MAPPING_ARGS))

    assert "private_unverified" in line and "cleared" in line and "--replace" in line
    assert summary(cli_env)["comments"] == 0
    args = ("ingest", private_csv, "--scope", "cleared", "--replace", *MAPPING_ARGS)
    assert ok(cli_env.run(*args))["rights_status"] == "cleared"
    assert summary(cli_env)["comments"] == 2


# --------------------------------------------------------------------------- explicit columns


@pytest.mark.parametrize("flag", ["--permalink-col", "--date-col"])
def test_a_mistyped_optional_column_fails_instead_of_dropping_values(
    cli_env: CliEnv, private_csv: Path, flag: str
) -> None:
    """F36: an explicitly named column must exist; nothing is imported otherwise."""
    args = ("--id-col", "id", "--text-col", "body", flag, "urll")

    line = failed(cli_env.run("ingest", private_csv, "--scope", "private", *args))

    assert "'urll'" in line
    assert counts(summary(cli_env, "private")) == EMPTY_SUMMARY


def test_a_mistyped_jsonl_key_fails(cli_env: CliEnv, tmp_path: Path) -> None:
    path = write_jsonl(tmp_path / "keys.jsonl", comment_rows(("a", "one")))

    line = failed(cli_env.run("ingest", path, "--scope", "private", "--date-col", "publishd"))

    assert "'publishd'" in line


def test_implicit_optional_columns_may_be_missing(cli_env: CliEnv, tmp_path: Path) -> None:
    path = write_csv(tmp_path / "bare.csv", ("comment_id", "text"), [("a", "one")])

    assert ok(cli_env.run("ingest", path, "--scope", "private"))["inserted"] == 1


# --------------------------------------------------------------------------- errors


def test_missing_input_file_fails_with_one_line(cli_env: CliEnv, tmp_path: Path) -> None:
    line = failed(cli_env.run("ingest", tmp_path / "nope.csv", "--scope", "private"))

    assert "nope.csv" in line


@pytest.mark.parametrize(
    ("name", "content", "fragment"),
    [
        ("notes.txt", "comment_id,text\na,b\n", "unsupported file type"),
        ("empty.csv", "", "empty"),
        ("dupes.csv", f"comment_id,text\nc7,{SECRET_TEXT}\nc7,{SECRET_TEXT}\n", "rows 1, 2"),
        ("blank.csv", "comment_id,text\nc1,   \n", "row 1"),
    ],
)
def test_invalid_export_fails_with_one_line_and_no_text(
    cli_env: CliEnv, tmp_path: Path, name: str, content: str, fragment: str
) -> None:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")

    line = failed(cli_env.run("ingest", path, "--scope", "private"))

    assert fragment in line
    assert "Secretive" not in line
    assert "c7" not in line  # duplicate ids are never echoed (they may be comment text)
    assert counts(summary(cli_env, "private")) == EMPTY_SUMMARY


def test_comment_id_collision_with_another_source_fails(cli_env: CliEnv, tmp_path: Path) -> None:
    path = write_jsonl(tmp_path / "first.jsonl", comment_rows(("c1", SECRET_TEXT)))
    ok(cli_env.run("ingest", path, "--scope", "private"))
    other = write_jsonl(tmp_path / "second.jsonl", comment_rows(("c1", "different text")))

    line = failed(cli_env.run("ingest", other, "--scope", "private"))

    assert "c1" in line
    assert "Secretive" not in line
    conn = cli_env.connect()
    try:
        assert [r[0] for r in conn.execute("SELECT source_id FROM sources")] == ["first"]
    finally:
        conn.close()


@pytest.mark.parametrize(
    "argv",
    [
        ["--expires-at", "next spring"],
        ["--source-url", "example.org/no-scheme"],
        ["--source-id", " padded"],
    ],
)
def test_invalid_source_description_fails(
    cli_env: CliEnv, private_csv: Path, argv: list[str]
) -> None:
    failed(cli_env.run("ingest", private_csv, "--scope", "private", *MAPPING_ARGS, *argv))

    assert counts(summary(cli_env, "private")) == EMPTY_SUMMARY


def test_unknown_source_deletion_fails(demo_env: CliEnv) -> None:
    line = failed(demo_env.run("delete-source", "no-such-source"))

    assert "no-such-source" in line
    assert counts(summary(demo_env)) == DEMO_SUMMARY


def test_missing_extraction_file_fails(demo_env: CliEnv, tmp_path: Path) -> None:
    line = failed(demo_env.run("load-extractions", tmp_path / "parsed.jsonl"))

    assert "parsed.jsonl" in line


def test_missing_config_file_fails(cli_env: CliEnv, tmp_path: Path) -> None:
    line = failed(cli_env.run("--config", tmp_path / "absent.yaml", "summary"))  # last wins

    assert "absent.yaml" in line


def test_invalid_config_is_reported_on_one_line(cli_env: CliEnv) -> None:
    text = cli_env.config.read_text(encoding="utf-8").replace("scope: public", "scope: everyone")
    cli_env.config.write_text(text, encoding="utf-8")

    line = failed(cli_env.run("summary"))

    assert "scope" in line


def test_malformed_config_yaml_is_reported_on_one_line(cli_env: CliEnv) -> None:
    cli_env.config.write_text("database: [unclosed\n  path: x\n", encoding="utf-8")

    failed(cli_env.run("summary"))


def test_a_file_that_is_not_a_database_is_reported_on_one_line(
    cli_env: CliEnv, tmp_path: Path
) -> None:
    not_a_db = tmp_path / "notes.sqlite"
    not_a_db.write_bytes(b"this is not an SQLite database, just some bytes " * 100)

    failed(cli_env.run("summary", "--db", not_a_db))

    assert not_a_db.read_bytes().startswith(b"this is not an SQLite database")


# --------------------------------------------------------------------------- load-extractions

RAW_TEXT = "Zone 6b here and my Sungold loved the grow bags."
GOOD_LINE = {
    "comment_id": "c1",
    "extractor_version": "test-v1",
    "status": "parsed",
    "mentions": [
        {
            "crop": "tomato",
            "variety_as_written": "Sungold",
            "topics": ["productivity"],
            "evidence_quote": "my Sungold loved the grow bags",
            "environment": [
                {
                    "key": "zone",
                    "value_as_written": "Zone 6b",
                    "evidence_quote": "Zone 6b here",
                    "applicability": "whole_comment",
                    "basis": "reported_garden_context",
                },
                {
                    "key": "method",
                    "value_as_written": "grow bags",
                    "evidence_quote": "a paraphrase that is not in the comment",
                    "applicability": "this_mention",
                    "basis": "reported_garden_context",
                },
            ],
        }
    ],
}


@pytest.fixture
def extraction_file(cli_env: CliEnv, tmp_path: Path) -> Path:
    comments = write_jsonl(tmp_path / "export.jsonl", comment_rows(("c1", RAW_TEXT)))
    ok(cli_env.run("ingest", comments, "--scope", "private"))
    unknown = {**GOOD_LINE, "comment_id": "ghost", "mentions": []}
    path = tmp_path / "parsed.jsonl"
    path.write_text(
        "\n".join([json.dumps(GOOD_LINE), json.dumps(unknown), "{not json", ""]),
        encoding="utf-8",
    )
    return path


def test_load_extractions_prints_counts_only(cli_env: CliEnv, extraction_file: Path) -> None:
    payload = ok(cli_env.run("load-extractions", extraction_file))

    assert list(payload) == REPORT_KEYS
    assert payload == {
        "lines_read": 3,
        "schema_errors": 1,
        "unknown_comment_ids": 1,
        "duplicate_comment_ids": 0,
        "comments_loaded": 1,
        "status_counts": {"parsed": 0, "partial": 1, "unresolved": 0},
        "mentions_accepted": 1,
        "mentions_rejected": 0,
        "facts_accepted": 1,
        "facts_rejected": 1,
        "unresolved_identities": 0,
        "rejections": 3,
    }
    assert counts(summary(cli_env, "private"))["facts"] == 1


def test_load_extractions_writes_rejections_jsonl(
    cli_env: CliEnv, extraction_file: Path, tmp_path: Path
) -> None:
    out = tmp_path / "reports" / "rejections.jsonl"

    ok(cli_env.run("load-extractions", extraction_file, "--rejections-out", out))

    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert rows == [
        {
            "comment_id": "c1",
            "line_number": 1,
            "mention_index": 0,
            "fact_index": 1,
            "reason": "fact_quote_not_substring",
        },
        {
            "comment_id": "ghost",
            "line_number": 2,
            "mention_index": None,
            "fact_index": None,
            "reason": "unknown_comment_id",
        },
        {
            "comment_id": None,
            "line_number": 3,
            "mention_index": None,
            "fact_index": None,
            "reason": "schema_error",
        },
    ]
    assert "Sungold" not in out.read_text(encoding="utf-8")


def test_rejections_out_is_written_even_when_empty(demo_env: CliEnv, tmp_path: Path) -> None:
    out = tmp_path / "rejections.jsonl"

    payload = ok(demo_env.run("load-extractions", DEMO_EXTRACTIONS, "--rejections-out", out))

    assert payload["rejections"] == 0
    assert out.read_text(encoding="utf-8") == ""


def test_reloading_extractions_is_idempotent(demo_env: CliEnv) -> None:
    first = ok(demo_env.run("load-extractions", DEMO_EXTRACTIONS))
    second = ok(demo_env.run("load-extractions", DEMO_EXTRACTIONS))

    assert first == second
    assert counts(summary(demo_env, "private")) == DEMO_SUMMARY


# --------------------------------------------------------------------------- export


def read_export(path: Path) -> list[dict[str, Any]]:
    data = path.read_bytes().decode("utf-8")
    assert data.endswith("\n")
    return [json.loads(line) for line in data.split("\n")[:-1]]


def test_export_for_extraction_writes_ids_and_exact_text(demo_env: CliEnv, tmp_path: Path) -> None:
    out = tmp_path / "to-extract" / "comments.jsonl"

    payload = ok(demo_env.run("export-for-extraction", "--out", out))

    assert payload == {"exported": 69, "out": str(out)}
    rows = read_export(out)
    assert all(list(row) == ["comment_id", "text", "text_hash"] for row in rows)
    assert all(row["text_hash"] == sha256(row["text"]) for row in rows)
    texts = fixture_texts()
    assert [row["comment_id"] for row in rows] == list(texts)  # file (source) order
    assert {row["comment_id"]: row["text"] for row in rows} == texts


def test_export_only_unparsed_lists_never_extracted_comments(
    demo_env: CliEnv, tmp_path: Path
) -> None:
    out = tmp_path / "unparsed.jsonl"

    payload = ok(demo_env.run("export-for-extraction", "--out", out, "--only-unparsed"))

    assert payload["exported"] == 3
    assert [row["comment_id"] for row in read_export(out)] == NEVER_EXTRACTED


def test_export_skips_sources_whose_permission_expired(cli_env: CliEnv, tmp_path: Path) -> None:
    old = write_jsonl(tmp_path / "old.jsonl", comment_rows(("o1", "Lapsed okra notes.")))
    new = write_jsonl(tmp_path / "new.jsonl", comment_rows(("n1", "Current okra notes.")))
    ok(cli_env.run("ingest", old, "--scope", "private", "--expires-at", "2000-01-01"))
    ok(cli_env.run("ingest", new, "--scope", "private", "--expires-at", "2999-01-01"))
    out = tmp_path / "export.jsonl"

    payload = ok(cli_env.run("export-for-extraction", "--out", out))

    assert payload["exported"] == 1
    assert [row["comment_id"] for row in read_export(out)] == ["n1"]


def test_export_file_is_private_to_the_owner(demo_env: CliEnv, tmp_path: Path) -> None:
    out = tmp_path / "comments.jsonl"
    out.write_text("stale", encoding="utf-8")
    out.chmod(0o644)

    ok(demo_env.run("export-for-extraction", "--out", out))

    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert len(read_export(out)) == 69


TRICKY = st.text(st.characters(codec="utf-8"), min_size=1, max_size=30).filter(str.strip)


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(texts=st.lists(TRICKY, min_size=1, max_size=4))
def test_export_round_trips_any_text_one_record_per_line(
    cli_env: CliEnv, tmp_path: Path, texts: list[str]
) -> None:
    """Each record is one physical line for any line splitter (incl. U+2028, NEL, CR)."""
    rows = comment_rows(*((f"c{i}", text) for i, text in enumerate(texts)))
    ok(cli_env.run("ingest", write_jsonl(tmp_path / "prop.jsonl", rows), "--scope", "private"))
    out = tmp_path / "export.jsonl"

    ok(cli_env.run("export-for-extraction", "--out", out))

    data = out.read_text(encoding="utf-8")
    assert len(data.splitlines()) == len(texts)
    assert [json.loads(line)["text"] for line in data.splitlines()] == texts


# --------------------------------------------------------------------------- output safety


def test_export_to_a_directory_fails_without_changing_its_mode(
    demo_env: CliEnv, tmp_path: Path
) -> None:
    """F17: a typo like ``--out data`` must not chmod the directory to 0600."""
    target = tmp_path / "data"
    target.mkdir()
    target.chmod(0o755)

    line = failed(demo_env.run("export-for-extraction", "--out", target))

    assert "directory" in line
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


@pytest.mark.parametrize("suffix", ["", "-wal", "-journal", "-shm"])
def test_export_never_overwrites_the_database(demo_env: CliEnv, suffix: str) -> None:
    target = demo_env.db_path.with_name(demo_env.db_path.name + suffix)
    before = target.read_bytes() if target.exists() else None

    line = failed(demo_env.run("export-for-extraction", "--out", target))

    assert "refusing" in line
    assert (target.read_bytes() if target.exists() else None) == before
    assert counts(summary(demo_env)) == DEMO_SUMMARY


def test_export_never_overwrites_the_database_given_by_db_option(
    demo_env: CliEnv, tmp_path: Path
) -> None:
    other = demo_env.db_path.parent / "copy.sqlite"
    other.write_bytes(demo_env.db_path.read_bytes())
    link = tmp_path / "innocent.jsonl"
    link.symlink_to(other)

    failed(demo_env.run("export-for-extraction", "--db", other, "--out", other))
    failed(demo_env.run("export-for-extraction", "--db", other, "--out", link))

    assert other.read_bytes() == demo_env.db_path.read_bytes()


def test_export_refuses_a_symlinked_output(demo_env: CliEnv, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.txt"
    target.write_text("keep me", encoding="utf-8")
    link = tmp_path / "export.jsonl"
    link.symlink_to(target)

    failed(demo_env.run("export-for-extraction", "--out", link))

    assert target.read_text(encoding="utf-8") == "keep me"


def test_rejections_out_never_overwrites_the_input(cli_env: CliEnv, extraction_file: Path) -> None:
    before = extraction_file.read_bytes()

    line = failed(
        cli_env.run("load-extractions", extraction_file, "--rejections-out", extraction_file)
    )

    assert "refusing" in line
    assert extraction_file.read_bytes() == before
    assert counts(summary(cli_env, "private"))["parsed_comments"] == 0  # nothing was loaded


def test_rejections_out_never_overwrites_the_database(
    cli_env: CliEnv, extraction_file: Path
) -> None:
    failed(cli_env.run("load-extractions", extraction_file, "--rejections-out", cli_env.db_path))

    assert counts(summary(cli_env, "private")) == {**EMPTY_SUMMARY, "sources": 1, "comments": 1}


@pytest.mark.parametrize("kind", ["directory", "under-a-file"])
def test_unwritable_rejections_out_fails_before_loading(
    cli_env: CliEnv, extraction_file: Path, tmp_path: Path, kind: str
) -> None:
    """F49: a bad --rejections-out exits 2 before the database changes."""
    if kind == "directory":
        target = tmp_path / "reports"
        target.mkdir()
    else:
        (tmp_path / "plain-file").write_text("x", encoding="utf-8")
        target = tmp_path / "plain-file" / "rejections.jsonl"

    failed(cli_env.run("load-extractions", extraction_file, "--rejections-out", target))

    assert counts(summary(cli_env, "private"))["parsed_comments"] == 0


def test_output_files_are_plain_regular_files(demo_env: CliEnv, tmp_path: Path) -> None:
    out = tmp_path / "rejections.jsonl"
    out.write_text("stale rejections\n", encoding="utf-8")

    ok(demo_env.run("load-extractions", DEMO_EXTRACTIONS, "--rejections-out", out))

    assert out.read_text(encoding="utf-8") == ""  # overwriting a regular file stays allowed
    assert not out.is_symlink()
    assert os.access(out, os.R_OK)


# --------------------------------------------------------------------------- delete / purge


def test_delete_source_removes_its_comments(demo_env: CliEnv) -> None:
    payload = ok(demo_env.run("delete-source", DEMO_SOURCE))

    assert payload == {"source_id": DEMO_SOURCE, "comments_deleted": 69}
    assert counts(summary(demo_env, "private")) == EMPTY_SUMMARY


def test_purge_expired_deletes_only_expired_sources(cli_env: CliEnv, tmp_path: Path) -> None:
    old = write_jsonl(tmp_path / "old.jsonl", comment_rows(("o1", "old"), ("o2", "older")))
    new = write_jsonl(tmp_path / "new.jsonl", comment_rows(("n1", "new")))
    ok(cli_env.run("ingest", old, "--scope", "cleared", "--expires-at", "2000-01-01"))
    ok(cli_env.run("ingest", new, "--scope", "cleared", "--expires-at", "2999-01-01"))

    assert ok(cli_env.run("purge-expired")) == {"purged_sources": ["old"]}
    assert ok(cli_env.run("purge-expired")) == {"purged_sources": []}
    assert counts(summary(cli_env, "private"))["comments"] == 1


# --------------------------------------------------------------------------- privacy of output


def test_outputs_and_logs_never_contain_comment_text(
    verbose_cli_env: CliEnv, tmp_path: Path
) -> None:
    env = verbose_cli_env
    runs = [
        env.run("demo"),
        env.run("load-extractions", DEMO_EXTRACTIONS, "--rejections-out", tmp_path / "r.jsonl"),
        env.run("export-for-extraction", "--out", tmp_path / "x.jsonl"),
        env.run("summary", "--scope", "private"),
        env.run("ingest", tmp_path / "missing.jsonl", "--scope", "private"),
        env.run("delete-source", DEMO_SOURCE),
    ]

    assert [run.code for run in runs] == [0, 0, 0, 0, 2, 0]
    log_file = env.log_text()
    assert "Read 69 comments" in log_file  # INFO logging went to the configured JSON sink
    assert "Read 69 comments" in runs[0].err  # ... and to stderr
    emitted = "\n".join([*(run.out + run.err for run in runs), log_file])
    leaked = [t for t in [*fixture_texts().values(), *fixture_quotes()] if len(t) >= 12]
    assert [t for t in leaked if t in emitted] == []


def test_warning_level_config_keeps_stderr_quiet(cli_env: CliEnv) -> None:
    run = cli_env.run("demo")

    assert run.code == 0
    assert run.err == ""
    assert "Read 69 comments" not in cli_env.log_text()
