"""Command-line interface: ``uv run python -m crop_explorer [--config PATH] [--db PATH] COMMAND``.

Commands (docs/ARCHITECTURE.md, "CLI"):

``ingest PATH --scope {private,synthetic,cleared}``
    Import a CSV/JSONL comments export as one source (idempotent re-import).
``load-extractions PATH [--rejections-out PATH]``
    Validate extractor JSONL against stored comments and store it.
``export-for-extraction --out PATH [--only-unparsed]``
    Write ``{comment_id, text}`` JSONL for the offline extractor (private data).
``delete-source SOURCE_ID`` / ``purge-expired``
    Remove a source, or every expired source, with everything derived from it.
``summary [--scope {public,private}]``
    Count what a viewer in that scope can see.
``demo``
    Ingest the synthetic fixtures (rights ``synthetic``) and load their extractions.

``--config`` and ``--db`` may be given before or after the command. On success exactly one line
of JSON (ids and counts only) goes to stdout and the exit status is 0. On invalid input
(``IngestError``, ``ValueError``, ``KeyError``), an unreadable file (``OSError``), malformed
configuration YAML (``yaml.YAMLError``) or a database that SQLite cannot use
(``sqlite3.Error``, e.g. a file that is not a database), one ``error: ...`` line goes to stderr
and the exit status is 2. Comment text is never printed or
logged; only ``export-for-extraction`` writes it, to an owner-only file.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, get_args

import yaml

from crop_explorer import db
from crop_explorer.config import DEFAULT_CONFIG_PATH, AppConfig, load_config
from crop_explorer.extraction import ExtractionReport, load_extractions
from crop_explorer.ingest import build_source, import_file
from crop_explorer.logging_setup import configure_logging
from crop_explorer.schemas import SCOPE_TO_RIGHTS, ColumnMapping, ImportScope, utc_now_iso
from crop_explorer.views import ViewerScope

__all__ = [
    "DEFAULT_KIND",
    "DEMO_COMMENTS",
    "DEMO_EXTRACTIONS",
    "DEMO_KIND",
    "DEMO_SOURCE_ID",
    "EXIT_ERROR",
    "EXIT_OK",
    "build_parser",
    "main",
]

EXIT_OK: Final = 0
EXIT_ERROR: Final = 2
"""Exit status for invalid input (the same status argparse uses for usage errors)."""

DEFAULT_KIND: Final = "comments_export"
DEMO_SOURCE_ID: Final = "synthetic-demo"
DEMO_KIND: Final = "synthetic_fixture"
_FIXTURES_DIR: Final = Path(__file__).resolve().parents[2] / "fixtures"
DEMO_COMMENTS: Final = _FIXTURES_DIR / "synthetic_comments.jsonl"
DEMO_EXTRACTIONS: Final = _FIXTURES_DIR / "synthetic_extractions.jsonl"

_HANDLED_ERRORS: Final = (ValueError, KeyError, OSError, yaml.YAMLError, sqlite3.Error)
_OWNER_ONLY: Final = 0o600
_RAW_LINE_BREAKS: Final = MappingProxyType(
    {chr(code): f"\\u{code:04x}" for code in (0x85, 0x2028, 0x2029)}
)
"""Line breaks that ``json.dumps(ensure_ascii=False)`` leaves raw but ``str.splitlines`` splits."""

Payload = dict[str, Any]
Handler = Callable[[AppConfig, argparse.Namespace], Payload]


# --------------------------------------------------------------------------- entry point


def main(argv: Sequence[str] | None = None) -> int:
    """Run one CLI command.

    Parameters
    ----------
    argv
        Arguments without the program name; ``None`` reads ``sys.argv[1:]``.

    Returns
    -------
    int
        ``0`` on success; ``2`` on a usage error or invalid input (after printing one
        ``error: ...`` line to stderr); ``0`` for ``--help``.
    """
    args = _parse(argv)
    if isinstance(args, int):
        return args
    try:
        cfg = _config(args)
        configure_logging(cfg.logging)
        handler: Handler = args.handler
        payload = handler(cfg, args)
    except _HANDLED_ERRORS as exc:
        print(f"error: {_one_line(exc)}", file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps(payload, ensure_ascii=False))
    return EXIT_OK


def _parse(argv: Sequence[str] | None) -> argparse.Namespace | int:
    try:
        return build_parser().parse_args(argv)
    except SystemExit as exc:  # argparse exits for --help (0) and usage errors (2)
        return exc.code if isinstance(exc.code, int) else EXIT_ERROR


def _config(args: argparse.Namespace) -> AppConfig:
    cfg = load_config(args.config)
    if args.db is None:
        return cfg
    return cfg.model_copy(update={"database": cfg.database.model_copy(update={"path": args.db})})


def _one_line(exc: BaseException) -> str:
    """Return the error message on one line (``str(KeyError)`` would add quotes)."""
    single_arg = isinstance(exc, KeyError) and len(exc.args) == 1
    message = str(exc.args[0]) if single_arg else str(exc)
    return " ".join(message.split()) or type(exc).__name__


# --------------------------------------------------------------------------- argument parser


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for every command.

    Returns
    -------
    argparse.ArgumentParser
        Parser whose namespace carries ``config``, ``db``, ``command`` and a ``handler``.
    """
    parser = argparse.ArgumentParser(
        prog="python -m crop_explorer",
        description="Import, extract and summarise gardeners' comments (no comment text is "
        "printed; output is one line of JSON).",
        parents=[_global_options(suppress=False)],
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    late_globals = _global_options(suppress=True)
    for spec in _COMMANDS:
        sub = commands.add_parser(
            spec.name, parents=[late_globals], help=spec.summary, description=spec.summary
        )
        sub.set_defaults(handler=spec.handler)
        spec.add_arguments(sub)
    return parser


def _global_options(*, suppress: bool) -> argparse.ArgumentParser:
    """``--config``/``--db``; the suppressed copy lets them also follow the command."""
    options = argparse.ArgumentParser(add_help=False)
    options.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS if suppress else DEFAULT_CONFIG_PATH,
        help="configuration file (default: the repository's config.yaml)",
    )
    options.add_argument(
        "--db",
        type=Path,
        default=argparse.SUPPRESS if suppress else None,
        help="database file, overriding database.path from the configuration",
    )
    return options


def _ingest_arguments(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("path", type=Path, help="export file (.csv or .jsonl)")
    sub.add_argument("--scope", required=True, choices=get_args(ImportScope))
    sub.add_argument("--source-id", help="stable id of this dataset (default: the file stem)")
    sub.add_argument("--kind", default=DEFAULT_KIND, help=f"(default: {DEFAULT_KIND})")
    sub.add_argument("--source-url", help="http(s) URL the export came from")
    sub.add_argument("--expires-at", help="ISO-8601 time after which the source is hidden")
    defaults = ColumnMapping()
    sub.add_argument("--id-col", default=defaults.id_col)
    sub.add_argument("--text-col", default=defaults.text_col)
    sub.add_argument("--permalink-col", default=defaults.permalink_col)
    sub.add_argument("--date-col", default=defaults.date_col)


def _load_extractions_arguments(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("path", type=Path, help="extractor output, one ParsedComment per line")
    sub.add_argument(
        "--rejections-out", type=Path, help="write every rejection (ids and reasons) as JSONL"
    )


def _export_arguments(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--out", type=Path, required=True, help="output JSONL (mode 600)")
    sub.add_argument(
        "--only-unparsed", action="store_true", help="only comments with no stored extraction"
    )


def _delete_source_arguments(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("source_id", help="id of the source to delete")


def _summary_arguments(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--scope", choices=get_args(ViewerScope), help="(default: viewer.scope from the config)"
    )


def _no_arguments(sub: argparse.ArgumentParser) -> None:
    del sub


# --------------------------------------------------------------------------- commands


def _open_db(cfg: AppConfig) -> closing[sqlite3.Connection]:
    return closing(db.connect(cfg.database.path))


def _ingest(cfg: AppConfig, args: argparse.Namespace) -> Payload:
    path: Path = args.path
    source = build_source(
        path,
        source_id=path.stem if args.source_id is None else args.source_id,
        kind=args.kind,
        rights_status=SCOPE_TO_RIGHTS[args.scope],
        source_url=args.source_url,
        expires_at=args.expires_at,
        imported_at=utc_now_iso(),
    )
    mapping = ColumnMapping(
        id_col=args.id_col,
        text_col=args.text_col,
        permalink_col=args.permalink_col,
        date_col=args.date_col,
    )
    with _open_db(cfg) as conn:
        stats = import_file(conn, path, mapping, source)
    return stats.model_dump()


def _load_extractions(cfg: AppConfig, args: argparse.Namespace) -> Payload:
    with _open_db(cfg) as conn:
        report = load_extractions(conn, args.path, cfg.extraction, utc_now_iso())
    if args.rejections_out is not None:
        rows = (rejection.model_dump(mode="json") for rejection in report.rejections)
        _write_jsonl(args.rejections_out, rows, owner_only=False)
    return _report_payload(report)


def _report_payload(report: ExtractionReport) -> Payload:
    """Report counts; ``rejections`` is the number of rejections, never their content."""
    payload = report.model_dump(mode="json", exclude={"rejections"})
    payload["rejections"] = len(report.rejections)
    return payload


def _export(cfg: AppConfig, args: argparse.Namespace) -> Payload:
    with _open_db(cfg) as conn:
        comments = db.iter_comments_for_extraction(conn, args.only_unparsed, utc_now_iso())
    rows = ({"comment_id": comment_id, "text": text} for comment_id, text in comments)
    _write_jsonl(args.out, rows, owner_only=True)
    return {"exported": len(comments), "out": str(args.out)}


def _delete_source(cfg: AppConfig, args: argparse.Namespace) -> Payload:
    with _open_db(cfg) as conn:
        removed = db.delete_source(conn, args.source_id)
    return {"source_id": args.source_id, "comments_deleted": removed}


def _purge_expired(cfg: AppConfig, args: argparse.Namespace) -> Payload:
    del args
    with _open_db(cfg) as conn:
        purged = db.purge_expired(conn, utc_now_iso())
    return {"purged_sources": purged}


def _summary(cfg: AppConfig, args: argparse.Namespace) -> Payload:
    scope: ViewerScope = cfg.viewer.scope if args.scope is None else args.scope
    with _open_db(cfg) as conn:
        counts = db.summary_counts(conn, scope, utc_now_iso())
    return {"scope": scope, **counts}


def _demo(cfg: AppConfig, args: argparse.Namespace) -> Payload:
    del args
    now = utc_now_iso()
    source = build_source(
        DEMO_COMMENTS,
        source_id=DEMO_SOURCE_ID,
        kind=DEMO_KIND,
        rights_status="synthetic",
        source_url=None,
        expires_at=None,
        imported_at=now,
    )
    with _open_db(cfg) as conn:
        stats = import_file(conn, DEMO_COMMENTS, ColumnMapping(), source)
        report = load_extractions(conn, DEMO_EXTRACTIONS, cfg.extraction, now)
    return {"import": stats.model_dump(), "extraction": _report_payload(report)}


@dataclass(frozen=True)
class _CommandSpec:
    name: str
    handler: Handler
    summary: str
    add_arguments: Callable[[argparse.ArgumentParser], None]


_COMMANDS: Final = (
    _CommandSpec("ingest", _ingest, "Import a CSV/JSONL comments export.", _ingest_arguments),
    _CommandSpec(
        "load-extractions",
        _load_extractions,
        "Validate extractor JSONL against stored comments and load it.",
        _load_extractions_arguments,
    ),
    _CommandSpec(
        "export-for-extraction",
        _export,
        "Write {comment_id, text} JSONL for the offline extractor (private data).",
        _export_arguments,
    ),
    _CommandSpec(
        "delete-source",
        _delete_source,
        "Delete a source and everything derived from it.",
        _delete_source_arguments,
    ),
    _CommandSpec("purge-expired", _purge_expired, "Delete every expired source.", _no_arguments),
    _CommandSpec("summary", _summary, "Count what a viewer can see.", _summary_arguments),
    _CommandSpec(
        "demo", _demo, "Load the synthetic demo fixtures and their extractions.", _no_arguments
    ),
)


# --------------------------------------------------------------------------- JSONL output


def _jsonl_line(row: Mapping[str, object]) -> str:
    """Serialise ``row`` as one physical line for any line splitter (text stays exact)."""
    line = json.dumps(row, ensure_ascii=False)
    for raw, escaped in _RAW_LINE_BREAKS.items():
        line = line.replace(raw, escaped)  # these only occur inside JSON strings
    return line + "\n"


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, object]], *, owner_only: bool) -> None:
    """Write ``rows`` as UTF-8 JSONL, creating parent directories.

    With ``owner_only`` the file is made readable by its owner only before any data is
    written, because it holds private comment text.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if owner_only:
        path.touch(mode=_OWNER_ONLY, exist_ok=True)
        path.chmod(_OWNER_ONLY)
    path.write_text("".join(_jsonl_line(row) for row in rows), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
