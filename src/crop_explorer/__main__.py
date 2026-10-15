"""Command-line interface: ``uv run python -m crop_explorer [--config PATH] [--db PATH] COMMAND``.

Commands (docs/ARCHITECTURE.md, "CLI"):

``ingest PATH --scope {private,synthetic,cleared} [--expires-at ISO | --clear-expiry] [--replace]``
    Import a CSV/JSONL comments export as one source (idempotent re-import; the stored expiry
    is kept unless replaced or cleared; replacing a source wholesale or changing its rights
    status needs ``--replace``).
``load-extractions PATH [--rejections-out PATH]``
    Validate extractor JSONL against stored comments and store it.
``export-for-extraction --out PATH [--only-unparsed]``
    Write ``{comment_id, text, text_hash}`` JSONL for the offline extractor (private data).
``delete-source SOURCE_ID`` / ``purge-expired``
    Remove a source, or every expired source, with everything derived from it.
``summary [--scope {public,private}]``
    Count what a viewer in that scope can see.
``demo``
    Ingest the synthetic fixtures (rights ``synthetic``) and load their extractions
    (:func:`crop_explorer.demo.build_demo_database`; any rejection is an error).

``--config`` and ``--db`` may be given before or after the command. On success exactly one line
of JSON (ids and counts only) goes to stdout and the exit status is 0. On invalid input
(``IngestError``, ``ValueError``, ``KeyError``), an unreadable file (``OSError``), malformed
configuration YAML (``yaml.YAMLError``) or a database that SQLite cannot use
(``sqlite3.Error``, e.g. a file that is not a database), one ``error: ...`` line goes to stderr
and the exit status is 2. Comment text is never printed or
logged; only ``export-for-extraction`` writes it, to an owner-only file.

Output files (``--out``, ``--rejections-out``) are checked and opened before any work: a
directory, a symlink, the database (or its ``-wal``/``-journal``/``-shm`` files) and the
command's own input are refused; another regular file is overwritten.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, closing, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, TextIO, get_args

import yaml

from crop_explorer import db
from crop_explorer.config import DEFAULT_CONFIG_PATH, AppConfig, load_config
from crop_explorer.demo import (
    DEMO_COMMENTS,
    DEMO_EXTRACTIONS,
    DEMO_KIND,
    DEMO_SOURCE_ID,
    build_demo_database,
)
from crop_explorer.extraction import ExtractionReport, load_extractions
from crop_explorer.ingest import build_source, import_file
from crop_explorer.logging_setup import configure_logging
from crop_explorer.schemas import (
    SCOPE_TO_RIGHTS,
    ColumnMapping,
    ImportScope,
    SourceRecord,
    utc_now_iso,
)
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

_HANDLED_ERRORS: Final = (ValueError, KeyError, OSError, yaml.YAMLError, sqlite3.Error)
_OWNER_ONLY: Final = 0o600
_DEFAULT_FILE_MODE: Final = 0o666
"""Mode of other new output files before the umask applies (as for ``open()``)."""
_OUTPUT_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
_DATABASE_FILE_SUFFIXES: Final = ("", "-wal", "-journal", "-shm")
_REPLACE_HELP: Final = (
    "allow replacing every stored comment of an existing source (a different export under "
    "the same source id) or changing its rights status"
)
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
    sub.add_argument("--replace", action="store_true", help=_REPLACE_HELP)
    _expiry_arguments(sub)
    _column_arguments(sub)


def _expiry_arguments(sub: argparse.ArgumentParser) -> None:
    expiry = sub.add_mutually_exclusive_group()
    expiry.add_argument(
        "--expires-at",
        help="ISO-8601 time after which the source is hidden (default: keep the stored expiry)",
    )
    expiry.add_argument("--clear-expiry", action="store_true", help="remove the stored expiry")


def _column_arguments(sub: argparse.ArgumentParser) -> None:
    defaults = ColumnMapping()
    sub.add_argument("--id-col", default=defaults.id_col)
    sub.add_argument("--text-col", default=defaults.text_col)
    for flag, default in (
        ("--permalink-col", defaults.permalink_col),
        ("--date-col", defaults.date_col),
    ):
        sub.add_argument(
            flag, help=f"(default: {default!r} if present; a column named here must exist)"
        )


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


def _database_files(cfg: AppConfig) -> tuple[Path, ...]:
    """Return the database file and the side files SQLite may create next to it."""
    path = cfg.database.path
    return tuple(path.with_name(path.name + suffix) for suffix in _DATABASE_FILE_SUFFIXES)


def _ingest(cfg: AppConfig, args: argparse.Namespace) -> Payload:
    source = _source_from_args(args)
    mapping = _mapping_from_args(args)
    with _open_db(cfg) as conn:
        stats = import_file(
            conn, args.path, mapping, source, replace=args.replace, clear_expiry=args.clear_expiry
        )
    return stats.model_dump()


def _source_from_args(args: argparse.Namespace) -> SourceRecord:
    path: Path = args.path
    return build_source(
        path,
        source_id=path.stem if args.source_id is None else args.source_id,
        kind=args.kind,
        rights_status=SCOPE_TO_RIGHTS[args.scope],
        source_url=args.source_url,
        expires_at=args.expires_at,
        imported_at=utc_now_iso(),
    )


def _mapping_from_args(args: argparse.Namespace) -> ColumnMapping:
    """Map the column options; an optional column named explicitly becomes required."""
    defaults = ColumnMapping()
    return ColumnMapping(
        id_col=args.id_col,
        text_col=args.text_col,
        permalink_col=defaults.permalink_col if args.permalink_col is None else args.permalink_col,
        permalink_required=args.permalink_col is not None,
        date_col=defaults.date_col if args.date_col is None else args.date_col,
        date_required=args.date_col is not None,
    )


def _load_extractions(cfg: AppConfig, args: argparse.Namespace) -> Payload:
    protected = (*_database_files(cfg), args.path)
    with _optional_output(args.rejections_out, protected) as out:
        with _open_db(cfg) as conn:
            report = load_extractions(conn, args.path, cfg.extraction, utc_now_iso())
        if out is not None:
            _write_rows(out, (item.model_dump(mode="json") for item in report.rejections))
    return _report_payload(report)


def _report_payload(report: ExtractionReport) -> Payload:
    """Report counts; ``rejections`` is the number of rejections, never their content."""
    payload = report.model_dump(mode="json", exclude={"rejections"})
    payload["rejections"] = len(report.rejections)
    return payload


def _export(cfg: AppConfig, args: argparse.Namespace) -> Payload:
    with _open_output(args.out, _database_files(cfg), owner_only=True) as out:
        with _open_db(cfg) as conn:
            items = db.iter_comments_for_extraction(conn, args.only_unparsed, utc_now_iso())
        _write_rows(out, (item._asdict() for item in items))
    return {"exported": len(items), "out": str(args.out)}


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
    built = build_demo_database(cfg.database.path, cfg, utc_now_iso())
    return {"import": built.stats.model_dump(), "extraction": _report_payload(built.report)}


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
        "Write {comment_id, text, text_hash} JSONL for the offline extractor (private data).",
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


def _write_rows(handle: TextIO, rows: Iterable[Mapping[str, object]]) -> None:
    handle.write("".join(_jsonl_line(row) for row in rows))


def _optional_output(
    path: Path | None, protected: Sequence[Path]
) -> AbstractContextManager[TextIO | None]:
    if path is None:
        return nullcontext()
    return _open_output(path, protected, owner_only=False)


@contextmanager
def _open_output(path: Path, protected: Sequence[Path], *, owner_only: bool) -> Iterator[TextIO]:
    """Open ``path`` for UTF-8 JSONL output (truncating it), creating parent directories.

    Refused before any side effect: a directory, a ``protected`` file (the database and its
    side files, the command's input) or a symlink (``O_NOFOLLOW``). With ``owner_only`` the
    file is mode 0600 before any data is written, because it holds private comment text.
    """
    _refuse_unsafe_output(path, protected)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, _OUTPUT_FLAGS, _OWNER_ONLY if owner_only else _DEFAULT_FILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        if owner_only:
            os.fchmod(handle.fileno(), _OWNER_ONLY)
        yield handle


def _refuse_unsafe_output(path: Path, protected: Sequence[Path]) -> None:
    if path.is_dir():
        raise ValueError(f"output path {path} is a directory")
    for other in protected:
        if _same_file(path, other):
            raise ValueError(f"refusing to overwrite {other} with the output file {path}")


def _same_file(path: Path, other: Path) -> bool:
    """Whether both name one file (also through symlinks or hard links)."""
    if path.resolve() == other.resolve():
        return True
    return path.exists() and other.exists() and path.samefile(other)


if __name__ == "__main__":
    raise SystemExit(main())
