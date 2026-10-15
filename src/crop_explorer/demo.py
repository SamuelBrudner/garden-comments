"""The synthetic demo database, built by the ``demo`` CLI command and by the hosted demo.

Both build it the same way (:func:`build_demo_database`): the 69 fictional comments in
``fixtures/synthetic_comments.jsonl`` are imported as source ``synthetic-demo`` (kind
``synthetic_fixture``, rights ``synthetic``, no expiry) and
``fixtures/synthetic_extractions.jsonl`` is loaded for them. The fixtures are written to load
cleanly, so any rejection is an error. They are found relative to the source tree, so this
needs a checkout (or an editable install).

The hosted public demo (``streamlit_app.py``) calls :func:`prepare_hosted_demo`, which builds
the database in a fresh directory next to a configuration file that points only at it.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Final

import yaml
from pydantic import BaseModel, ConfigDict

from crop_explorer import db
from crop_explorer.config import AppConfig
from crop_explorer.extraction import ExtractionReport, load_extractions
from crop_explorer.ingest import build_source, import_file
from crop_explorer.schemas import ColumnMapping, ImportStats, Rejection, SourceRecord

__all__ = [
    "DEMO_COMMENTS",
    "DEMO_EXTRACTIONS",
    "DEMO_KIND",
    "DEMO_SOURCE_ID",
    "FIXTURES_DIR",
    "HOSTED_CONFIG_NAME",
    "HOSTED_DATABASE_NAME",
    "DemoBuild",
    "HostedDemo",
    "build_demo_database",
    "prepare_hosted_demo",
]

DEMO_SOURCE_ID: Final = "synthetic-demo"
DEMO_KIND: Final = "synthetic_fixture"
FIXTURES_DIR: Final = Path(__file__).resolve().parents[2] / "fixtures"
DEMO_COMMENTS: Final = FIXTURES_DIR / "synthetic_comments.jsonl"
DEMO_EXTRACTIONS: Final = FIXTURES_DIR / "synthetic_extractions.jsonl"
HOSTED_DATABASE_NAME: Final = "demo.sqlite"
HOSTED_CONFIG_NAME: Final = "config.yaml"

_SHOWN_REJECTIONS: Final = 5
"""How many rejections the error message names (ids and reason codes only)."""

_FROZEN = ConfigDict(extra="forbid", frozen=True)


class DemoBuild(BaseModel):
    """What building the demo database did.

    Attributes
    ----------
    stats
        Outcome of importing the synthetic comments (the source's effective rights included).
    report
        Outcome of loading their extractions; its ``rejections`` are always empty.
    """

    model_config = _FROZEN

    stats: ImportStats
    report: ExtractionReport


class HostedDemo(BaseModel):
    """Files of one hosted public demo, both inside the directory it was prepared in.

    Attributes
    ----------
    config
        Configuration file for the viewer (``--config``): the demo database, public scope and
        no JSON log file.
    database
        The demo database (``--db``).
    """

    model_config = _FROZEN

    config: Path
    database: Path


def build_demo_database(path: Path, cfg: AppConfig, now: str) -> DemoBuild:
    """Import the synthetic fixture comments and load their extractions into ``path``.

    Re-running it on the same database is idempotent: the comments come back ``unchanged`` and
    the extractions are replaced by identical ones.

    Parameters
    ----------
    path
        Database file; it and missing parent directories are created (see :func:`db.connect`).
    cfg
        Configuration; its ``extraction`` rules validate the fixture extractions.
    now
        Canonical UTC time recorded as the import and load time.

    Returns
    -------
    DemoBuild
        The import counts and the extraction report.

    Raises
    ------
    ValueError
        If any extraction line, mention or fact is rejected: the fixtures must load cleanly.
    """
    with closing(db.connect(path)) as conn:
        stats = import_file(conn, DEMO_COMMENTS, ColumnMapping(), _demo_source(now))
        report = load_extractions(conn, DEMO_EXTRACTIONS, cfg.extraction, now)
    if report.rejections:
        raise ValueError(_rejections_message(report.rejections))
    return DemoBuild(stats=stats, report=report)


def _demo_source(now: str) -> SourceRecord:
    return build_source(
        DEMO_COMMENTS,
        source_id=DEMO_SOURCE_ID,
        kind=DEMO_KIND,
        rights_status="synthetic",
        source_url=None,
        expires_at=None,
        imported_at=now,
    )


def _rejections_message(rejections: tuple[Rejection, ...]) -> str:
    """Name the first rejections by comment id (or line) and reason; never any comment text."""
    shown = "; ".join(
        f"{item.comment_id or f'line {item.line_number}'}: {item.reason}"
        for item in rejections[:_SHOWN_REJECTIONS]
    )
    more = "; ..." if len(rejections) > _SHOWN_REJECTIONS else ""
    return (
        f"{len(rejections)} rejection(s) loading the synthetic demo extractions "
        f"{DEMO_EXTRACTIONS} ({shown}{more}); the demo fixtures must load without rejections"
    )


def prepare_hosted_demo(directory: Path, cfg: AppConfig, now: str) -> HostedDemo:
    """Build the demo database and a configuration that points only at it, in ``directory``.

    The configuration is ``cfg`` with the database set to the new demo database, the viewer
    scope set to ``public`` and the JSON log file turned off (the host collects stderr), so the
    hosted demo never opens or writes anything outside ``directory`` (such as ``data/``).

    Parameters
    ----------
    directory
        An empty directory, created if missing (normally a fresh temporary directory).
    cfg
        Base configuration (the repository's ``config.yaml``).
    now
        Canonical UTC time recorded as the import and load time.

    Returns
    -------
    HostedDemo
        The configuration file and the database.

    Raises
    ------
    ValueError
        If ``directory`` is not empty (an existing database must never be reused), or if the
        fixture extractions are rejected.
    """
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError(f"the hosted demo needs an empty directory, but {directory} is not")
    hosted = HostedDemo(
        config=directory / HOSTED_CONFIG_NAME, database=directory / HOSTED_DATABASE_NAME
    )
    hosted_cfg = _hosted_config(cfg, hosted.database)
    build_demo_database(hosted.database, hosted_cfg, now)
    text = yaml.safe_dump(hosted_cfg.model_dump(mode="json"), sort_keys=False)
    hosted.config.write_text(text, encoding="utf-8")
    return hosted


def _hosted_config(cfg: AppConfig, database: Path) -> AppConfig:
    updates = {
        "database": cfg.database.model_copy(update={"path": database}),
        "viewer": cfg.viewer.model_copy(update={"scope": "public"}),
        "logging": cfg.logging.model_copy(update={"json_path": None}),
    }
    return cfg.model_copy(update=updates)
