"""Tests for ``crop_explorer.demo``: the synthetic demo database and the hosted demo's files.

``build_demo_database`` is what both the ``demo`` CLI command and the hosted public demo
(``streamlit_app.py``) run: the 69 fictional fixture comments imported with rights
``synthetic`` plus their extractions, which must load with zero rejections (anything else fails
loudly). ``prepare_hosted_demo`` builds that database in a fresh directory next to a config file
that points only at it, forces public scope and writes no JSON log file.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from crop_explorer import __main__ as cli
from crop_explorer import db, demo
from crop_explorer.config import AppConfig, load_config
from crop_explorer.demo import (
    DEMO_COMMENTS,
    DEMO_EXTRACTIONS,
    DEMO_KIND,
    DEMO_SOURCE_ID,
    DemoBuild,
    HostedDemo,
    build_demo_database,
    prepare_hosted_demo,
)
from crop_explorer.views import ViewerScope

REPO = Path(__file__).resolve().parents[1]
NOW = "2026-10-15T12:00:00Z"
LATER = "2026-10-16T08:30:00Z"
DEMO_SUMMARY = {"sources": 1, "comments": 69, "parsed_comments": 66, "mentions": 71, "facts": 82}


@pytest.fixture(scope="module")
def cfg() -> AppConfig:
    return load_config()


@pytest.fixture
def built(tmp_path: Path, cfg: AppConfig) -> tuple[Path, DemoBuild]:
    path = tmp_path / "demo.sqlite"
    return path, build_demo_database(path, cfg, NOW)


@pytest.fixture
def conn(built: tuple[Path, DemoBuild]) -> Iterator[sqlite3.Connection]:
    with closing(db.connect(built[0])) as connection:
        yield connection


def _summary(conn: sqlite3.Connection, scope: ViewerScope) -> dict[str, int]:
    return db.summary_counts(conn, scope, NOW)


# --------------------------------------------------------------------------- fixtures paths


def test_demo_fixtures_are_the_repository_fixtures() -> None:
    assert REPO / "fixtures" / "synthetic_comments.jsonl" == DEMO_COMMENTS
    assert REPO / "fixtures" / "synthetic_extractions.jsonl" == DEMO_EXTRACTIONS
    assert (DEMO_SOURCE_ID, DEMO_KIND) == ("synthetic-demo", "synthetic_fixture")


def test_cli_keeps_exporting_the_demo_constants() -> None:
    assert cli.DEMO_COMMENTS == DEMO_COMMENTS
    assert cli.DEMO_EXTRACTIONS == DEMO_EXTRACTIONS
    assert (cli.DEMO_SOURCE_ID, cli.DEMO_KIND) == (DEMO_SOURCE_ID, DEMO_KIND)


# --------------------------------------------------------------------------- build


def test_build_imports_69_synthetic_comments(built: tuple[Path, DemoBuild]) -> None:
    _, result = built
    assert result.stats.model_dump() == {
        "source_id": DEMO_SOURCE_ID,
        "inserted": 69,
        "updated": 0,
        "unchanged": 0,
        "deleted": 0,
        "rights_status": "synthetic",
        "expires_at": None,
    }


def test_build_loads_every_extraction_without_rejections(built: tuple[Path, DemoBuild]) -> None:
    _, result = built
    report = result.report
    assert report.rejections == ()
    assert report.lines_read == report.comments_loaded == 66
    assert report.status_counts == {"parsed": 61, "partial": 1, "unresolved": 4}
    assert (report.mentions_accepted, report.facts_accepted) == (71, 82)


def test_built_database_holds_only_the_synthetic_source(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT source_id, kind, rights_status, expires_at FROM sources")
    assert [tuple(row) for row in rows] == [(DEMO_SOURCE_ID, DEMO_KIND, "synthetic", None)]
    assert _summary(conn, "public") == DEMO_SUMMARY
    assert _summary(conn, "private") == DEMO_SUMMARY


def test_build_records_the_given_time(conn: sqlite3.Connection) -> None:
    (imported_at,) = conn.execute("SELECT imported_at FROM sources").fetchone()
    assert imported_at == NOW


def test_result_is_frozen(built: tuple[Path, DemoBuild]) -> None:
    _, result = built
    with pytest.raises(ValueError, match="frozen"):
        result.stats = result.stats  # type: ignore[misc]


def test_rebuild_is_idempotent(built: tuple[Path, DemoBuild], cfg: AppConfig) -> None:
    path, first = built
    second = build_demo_database(path, cfg, LATER)

    assert second.stats.model_dump() == {
        **first.stats.model_dump(),
        "inserted": 0,
        "unchanged": 69,
    }
    assert second.report == first.report
    with closing(db.connect(path)) as conn:
        assert _summary(conn, "public") == DEMO_SUMMARY


def test_build_creates_missing_parent_directories(tmp_path: Path, cfg: AppConfig) -> None:
    path = tmp_path / "nested" / "dir" / "demo.sqlite"
    build_demo_database(path, cfg, NOW)
    assert path.is_file()


# --------------------------------------------------------------------------- fail loud


def _with_rejected_line(tmp_path: Path) -> Path:
    """The fixture extractions plus one line naming a comment the database does not hold."""
    bad = json.loads(DEMO_EXTRACTIONS.read_text(encoding="utf-8").splitlines()[0])
    bad["comment_id"] = "no-such-comment"
    path = tmp_path / "extractions.jsonl"
    path.write_text(
        DEMO_EXTRACTIONS.read_text(encoding="utf-8") + json.dumps(bad) + "\n", encoding="utf-8"
    )
    return path


def test_any_rejection_fails_loudly(
    tmp_path: Path, cfg: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(demo, "DEMO_EXTRACTIONS", _with_rejected_line(tmp_path))

    with pytest.raises(ValueError, match=r"1 rejection.*no-such-comment: unknown_comment_id"):
        build_demo_database(tmp_path / "demo.sqlite", cfg, NOW)


def test_rejection_message_carries_no_comment_text(
    tmp_path: Path, cfg: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(demo, "DEMO_EXTRACTIONS", _with_rejected_line(tmp_path))
    texts = [json.loads(line)["text"] for line in DEMO_COMMENTS.read_text().splitlines()]

    with pytest.raises(ValueError) as caught:
        build_demo_database(tmp_path / "demo.sqlite", cfg, NOW)
    assert not any(text in str(caught.value) for text in texts)


# --------------------------------------------------------------------------- hosted demo


def test_prepare_hosted_demo_builds_the_database_and_config_in_the_directory(
    tmp_path: Path, cfg: AppConfig
) -> None:
    hosted = prepare_hosted_demo(tmp_path, cfg, NOW)

    assert hosted == HostedDemo(config=tmp_path / "config.yaml", database=tmp_path / "demo.sqlite")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["config.yaml", "demo.sqlite"]
    with closing(db.connect(hosted.database)) as conn:
        assert _summary(conn, "public") == DEMO_SUMMARY


def test_hosted_config_points_only_at_the_demo_and_writes_no_log_file(
    tmp_path: Path, cfg: AppConfig
) -> None:
    hosted = prepare_hosted_demo(tmp_path / "hosted", cfg, NOW)
    loaded = load_config(hosted.config)

    assert loaded.database.path == hosted.database
    assert loaded.viewer.scope == "public"
    assert loaded.logging.json_path is None
    assert str(REPO / "data") not in hosted.config.read_text(encoding="utf-8")


def test_hosted_config_keeps_every_other_setting(tmp_path: Path, cfg: AppConfig) -> None:
    private_cfg = cfg.model_copy(
        update={"viewer": cfg.viewer.model_copy(update={"scope": "private"})}
    )
    loaded = load_config(prepare_hosted_demo(tmp_path, private_cfg, NOW).config)

    assert loaded.viewer.scope == "public"
    unchanged = ("map", "extraction", "quick_chips", "outside_context", "youtube")
    assert {key: getattr(loaded, key) for key in unchanged} == {
        key: getattr(cfg, key) for key in unchanged
    }
    assert loaded.viewer.model_dump(exclude={"scope"}) == cfg.viewer.model_dump(exclude={"scope"})
    assert loaded.logging.level == cfg.logging.level


def test_prepare_hosted_demo_refuses_a_directory_that_is_not_empty(
    tmp_path: Path, cfg: AppConfig
) -> None:
    (tmp_path / "crop_explorer.sqlite").write_bytes(b"")

    with pytest.raises(ValueError, match="empty"):
        prepare_hosted_demo(tmp_path, cfg, NOW)
    assert not (tmp_path / "demo.sqlite").exists()
