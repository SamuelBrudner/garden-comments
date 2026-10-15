"""Shared fixtures for end-to-end tests: a demo database built by the real CLI, plus helpers.

* ``demo_template`` (session): a database built once by ``python -m crop_explorer demo``
  (through :func:`crop_explorer.__main__.main`, exactly as a user runs it).
* ``demo_env`` / ``demo_conn``: a private per-test copy of that database with its own config
  file, so a test may run CLI commands or write to it freely.
* ``cli_env`` / ``verbose_cli_env``: an empty database and config in the test's temporary
  directory (log level ``WARNING`` / ``INFO``); never touches the repository's ``data/``.
* ``add_private_source`` / ``source_importer``: import extra comments (``private_unverified``
  by default) and their extractions through the real ingest and extraction code.

No fixture here is autouse, so other test modules are unaffected.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from loguru import logger

from crop_explorer import db
from crop_explorer.__main__ import main
from crop_explorer.config import DEFAULT_CONFIG_PATH, load_config
from crop_explorer.extraction import ExtractionReport, load_extractions
from crop_explorer.ingest import build_source, import_file
from crop_explorer.schemas import ColumnMapping, ImportStats, RightsStatus

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "fixtures"
IMPORTED_AT = "2026-10-15T12:00:00Z"


# --------------------------------------------------------------------------- CLI runner


@dataclass(frozen=True)
class CliRun:
    """Outcome of one in-process CLI invocation."""

    code: int
    out: str
    err: str

    def json(self) -> Any:
        """Parse stdout, which must be exactly one JSON document."""
        return json.loads(self.out)

    @property
    def err_lines(self) -> list[str]:
        return self.err.splitlines()


def _restore_loguru() -> None:
    """Undo ``configure_logging``: drop the CLI's sinks (closing its log file).

    The replacement sink resolves ``sys.stderr`` at write time, so it never holds on to a
    capture stream that pytest has already closed.
    """
    logger.remove()
    logger.add(_write_to_current_stderr, level="DEBUG")


def _write_to_current_stderr(message: str) -> None:
    sys.stderr.write(message)


def invoke_cli(argv: Sequence[str]) -> CliRun:
    """Run ``main(argv)`` capturing stdout/stderr (including the CLI's own log sink)."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
    finally:
        _restore_loguru()
    return CliRun(code=code, out=out.getvalue(), err=err.getvalue())


@dataclass(frozen=True)
class CliEnv:
    """A config file whose database and JSON log live under ``root``."""

    root: Path
    config: Path
    db_path: Path
    log_path: Path

    def run(self, *argv: str | Path) -> CliRun:
        """Run the CLI with ``--config`` pointing at this environment."""
        return invoke_cli(["--config", str(self.config), *(str(arg) for arg in argv)])

    def connect(self) -> sqlite3.Connection:
        return db.connect(self.db_path)

    def log_text(self) -> str:
        return self.log_path.read_text(encoding="utf-8") if self.log_path.exists() else ""


def write_cli_env(root: Path, *, level: str = "WARNING") -> CliEnv:
    """Copy the repository config, redirecting the database and log file into ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    db_path = root / "db" / "crop_explorer.sqlite"
    log_path = root / "logs" / "crop_explorer.jsonl"
    raw["database"]["path"] = str(db_path)
    raw["logging"] = {"level": level, "json_path": str(log_path)}
    config = root / "config.yaml"
    config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return CliEnv(root=root, config=config, db_path=db_path, log_path=log_path)


@pytest.fixture
def cli_env(tmp_path: Path) -> CliEnv:
    """An empty CLI environment logging at WARNING (stderr carries only errors)."""
    return write_cli_env(tmp_path / "cli")


@pytest.fixture
def verbose_cli_env(tmp_path: Path) -> CliEnv:
    """An empty CLI environment logging at INFO, for checks on what gets logged."""
    return write_cli_env(tmp_path / "cli-verbose", level="INFO")


# --------------------------------------------------------------------------- demo database


@dataclass(frozen=True)
class DemoBuild:
    """The database built once by the ``demo`` command, and what the command printed."""

    db_path: Path
    payload: dict[str, Any]

    def copy_to(self, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.db_path.read_bytes())
        return target


@pytest.fixture(scope="session")
def demo_template(tmp_path_factory: pytest.TempPathFactory) -> DemoBuild:
    """Run ``python -m crop_explorer demo`` once; tests only ever read copies of the result."""
    env = write_cli_env(tmp_path_factory.mktemp("demo-template"))
    run = env.run("demo")
    assert run.code == 0, run.err
    return DemoBuild(db_path=env.db_path, payload=run.json())


@pytest.fixture
def demo_env(tmp_path: Path, demo_template: DemoBuild) -> CliEnv:
    """A CLI environment whose database is a fresh copy of the demo database."""
    env = write_cli_env(tmp_path / "demo")
    demo_template.copy_to(env.db_path)
    return env


@pytest.fixture
def demo_conn(demo_env: CliEnv) -> Iterator[sqlite3.Connection]:
    """An open connection to this test's copy of the demo database."""
    conn = demo_env.connect()
    yield conn
    conn.close()


# --------------------------------------------------------------------------- extra sources


@dataclass(frozen=True)
class SourceImport:
    """What importing one extra source produced."""

    stats: ImportStats
    report: ExtractionReport | None


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> Path:
    lines = (json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    path.write_text("".join(lines), encoding="utf-8")
    return path


def text_hash(text: str) -> str:
    """The lowercase sha256 hex of ``text`` encoded as UTF-8, as the export writes it."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def with_text_hashes(
    extractions: Sequence[Mapping[str, Any]], comments: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Give each extraction line the ``text_hash`` of its comment unless it already has one.

    This is what an extractor does when it copies ``text_hash`` from the export line.
    """
    texts = {row["comment_id"]: row["text"] for row in comments}
    return [
        {"text_hash": text_hash(texts[line["comment_id"]]), **line}
        if "text_hash" not in line and line.get("comment_id") in texts
        else dict(line)
        for line in extractions
    ]


def import_source(
    conn: sqlite3.Connection,
    directory: Path,
    *,
    source_id: str,
    comments: Sequence[Mapping[str, Any]],
    extractions: Sequence[Mapping[str, Any]] = (),
    rights_status: RightsStatus = "private_unverified",
    expires_at: str | None = None,
) -> SourceImport:
    """Import ``comments`` (JSONL rows) as one source, then load ``extractions`` for them.

    Both go through the production code (``ingest.import_file``, then
    ``extraction.load_extractions`` with the repository's validation config). Each extraction
    line gets its comment's ``text_hash`` unless it already carries one. Any extraction
    rejection fails the calling test, so helper data can never silently lose facts.
    """
    directory.mkdir(parents=True, exist_ok=True)
    rows = [{"permalink": None, "created_at": None, **row} for row in comments]
    comments_path = _write_jsonl(directory / f"{source_id}.comments.jsonl", rows)
    source = build_source(
        comments_path,
        source_id=source_id,
        kind="comments_export",
        rights_status=rights_status,
        source_url=None,
        expires_at=expires_at,
        imported_at=IMPORTED_AT,
    )
    stats = import_file(conn, comments_path, ColumnMapping(), source)
    if not extractions:
        return SourceImport(stats=stats, report=None)
    extraction_path = _write_jsonl(
        directory / f"{source_id}.extractions.jsonl", with_text_hashes(extractions, rows)
    )
    report = load_extractions(conn, extraction_path, load_config().extraction, IMPORTED_AT)
    assert report.rejections == (), report.rejections
    return SourceImport(stats=stats, report=report)


SourceImporter = Callable[..., SourceImport]


@pytest.fixture(scope="session")
def source_importer() -> SourceImporter:
    """``import_source(conn, directory, *, source_id, comments, extractions=(), ...)``."""
    return import_source


@pytest.fixture
def add_private_source(tmp_path: Path) -> SourceImporter:
    """``add(conn, *, source_id, comments, extractions=(), rights_status=..., expires_at=None)``.

    Rights default to ``private_unverified``; files are written under the test's tmp dir.
    """

    def add(conn: sqlite3.Connection, **kwargs: Any) -> SourceImport:
        return import_source(conn, tmp_path / "extra-sources", **kwargs)

    return add
