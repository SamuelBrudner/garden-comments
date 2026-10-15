"""Read user-supplied CSV/JSONL comment exports into validated records (SPEC §8, flow 1-2).

The file format comes from the suffix (``.csv`` or ``.jsonl``). Bytes are decoded as strict
UTF-8 (a leading byte-order mark is tolerated). Every row needs a non-blank id and text; ids
must be unique within the file. Optional permalink and date columns become ``None`` when the
column is unmapped, absent or blank. Comment text is kept exactly as given (no stripping or
normalising), so evidence quotes stay exact substrings.

Errors are :class:`IngestError` with the file, the row (CSV data row, 1-based) or line (JSONL
physical line, 1-based) and the column, but never a cell value: a swapped column mapping would
otherwise echo comment text. Only counts are logged.
"""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from loguru import logger

from crop_explorer import db
from crop_explorer.schemas import (
    RIGHTS_STATUSES,
    ColumnMapping,
    CommentRecord,
    ImportStats,
    RightsStatus,
    SourceRecord,
    to_utc_iso,
)

__all__ = [
    "SUPPORTED_SUFFIXES",
    "IngestError",
    "build_source",
    "dataset_hash",
    "import_file",
    "read_comments",
]

SUPPORTED_SUFFIXES: Final = (".csv", ".jsonl")
"""Recognised export file suffixes (matched case-insensitively)."""

_HTTP_URL: Final = re.compile(r"https?://[^\s\x00-\x1f\x7f]+", re.IGNORECASE)
_JSON_TYPE_NAMES: Final[Mapping[type, str]] = {
    dict: "object",
    list: "array",
    str: "string",
    int: "number",
    float: "number",
    bool: "boolean",
    type(None): "null",
}


class IngestError(ValueError):
    """An export file or source description is invalid.

    Messages name the file, row/line and column, never comment text or cell values.
    """


@dataclass(frozen=True, slots=True)
class _Row:
    """One data row of an export with its 1-based position (``unit`` is "row" or "line")."""

    unit: Literal["row", "line"]
    number: int
    cells: Mapping[str, object]

    @property
    def where(self) -> str:
        """Return the human-readable position, e.g. ``"row 3"``."""
        return f"{self.unit} {self.number}"


# --------------------------------------------------------------------------- public API


def read_comments(path: Path, mapping: ColumnMapping) -> list[CommentRecord]:
    """Read and validate every comment of a CSV or JSONL export, in file order.

    Parameters
    ----------
    path
        The export file; its suffix (``.csv`` or ``.jsonl``) selects the format.
    mapping
        Which columns (CSV) or keys (JSONL) hold the id, text, permalink and date.

    Returns
    -------
    list[CommentRecord]
        One record per data row; ``source_order`` is the 0-based row order, text is exact,
        dates are canonical UTC (``to_utc_iso``).

    Raises
    ------
    IngestError
        On an unsupported suffix, unreadable file, invalid UTF-8, malformed CSV/JSON, missing
        required column, blank id or text, non-http(s) permalink, unparseable date, duplicate
        ids, or a file with no comments.
    """
    suffix = _suffix_of(path)
    _check_mapping(mapping)
    text = _decode(path, _read_bytes(path))
    try:
        records = _records(_rows(suffix, text, mapping), mapping)
    except IngestError as exc:
        raise IngestError(f"{path}: {exc}") from exc
    logger.info("Read {} comments from {}", len(records), path.name)
    return records


def dataset_hash(path: Path) -> str:
    """Return the SHA-256 hex digest of the file's bytes.

    Parameters
    ----------
    path
        The export file.

    Returns
    -------
    str
        64 lowercase hex characters; identical bytes always give the identical hash.

    Raises
    ------
    IngestError
        If the file cannot be read.
    """
    return hashlib.sha256(_read_bytes(path)).hexdigest()


def build_source(  # noqa: PLR0913 - keyword-only signature fixed by docs/ARCHITECTURE.md
    path: Path,
    *,
    source_id: str,
    kind: str,
    rights_status: RightsStatus,
    source_url: str | None,
    expires_at: str | None,
    imported_at: str,
) -> SourceRecord:
    """Describe the provenance and rights state of one export file.

    Parameters
    ----------
    path
        The export file; its bytes determine ``dataset_hash``.
    source_id
        Stable id of the dataset (non-blank, no surrounding whitespace).
    kind
        Kind of dataset, e.g. ``"comments_export"`` (non-blank).
    rights_status
        Publication state; one of ``schemas.RIGHTS_STATUSES``.
    source_url
        Optional http(s) URL of where the dataset came from.
    expires_at
        Optional ISO-8601 expiry; normalised to canonical UTC.
    imported_at
        ISO-8601 import time; normalised to canonical UTC.

    Returns
    -------
    SourceRecord
        The validated source description.

    Raises
    ------
    IngestError
        On any invalid argument or an unreadable file.
    """
    return SourceRecord(
        source_id=_identifier("source_id", source_id),
        kind=_identifier("kind", kind),
        source_url=_source_url(source_url),
        rights_status=_rights_status(rights_status),
        imported_at=_timestamp("imported_at", imported_at),
        expires_at=None if expires_at is None else _timestamp("expires_at", expires_at),
        dataset_hash=dataset_hash(path),
    )


def import_file(
    conn: sqlite3.Connection, path: Path, mapping: ColumnMapping, source: SourceRecord
) -> ImportStats:
    """Read an export and make it the complete stored contents of ``source``.

    Parameters
    ----------
    conn
        Open database connection (see :func:`crop_explorer.db.connect`).
    path
        The export file.
    mapping
        Column mapping of the export.
    source
        Provenance of the export, usually from :func:`build_source` on the same ``path``.

    Returns
    -------
    ImportStats
        Inserted/updated/unchanged/deleted counts from :func:`db.sync_source_comments`.

    Raises
    ------
    IngestError
        If the file is invalid; nothing is written in that case.
    ValueError
        If a comment id already belongs to another source (raised by the database layer).
    """
    records = read_comments(path, mapping)
    return db.sync_source_comments(conn, source, records)


# --------------------------------------------------------------------------- file level


def _suffix_of(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        expected = ", ".join(SUPPORTED_SUFFIXES)
        raise IngestError(f"{path}: unsupported file type {path.suffix!r}; expected {expected}")
    return suffix


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise IngestError(f"cannot read {path}: {exc.strerror or type(exc).__name__}") from exc


def _decode(path: Path, data: bytes) -> str:
    body = data.removeprefix(codecs.BOM_UTF8)
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        line = body.count(b"\n", 0, exc.start) + 1
        message = f"{path}: not valid UTF-8 (line {line}, byte {exc.start}: {exc.reason})"
        raise IngestError(message) from None


def _check_mapping(mapping: ColumnMapping) -> None:
    columns = _mapped_columns(mapping)
    if len(set(columns)) != len(columns):
        raise IngestError(f"column mapping must name distinct columns, got {list(columns)}")


def _mapped_columns(mapping: ColumnMapping) -> tuple[str, ...]:
    optional = (mapping.permalink_col, mapping.date_col)
    return (mapping.id_col, mapping.text_col, *(c for c in optional if c is not None))


def _rows(suffix: str, text: str, mapping: ColumnMapping) -> list[_Row]:
    if not text.strip():
        raise IngestError("file is empty")
    rows = _csv_rows(text, mapping) if suffix == ".csv" else _jsonl_rows(text)
    if not rows:
        raise IngestError("file contains no comments")
    return rows


def _records(rows: Sequence[_Row], mapping: ColumnMapping) -> list[CommentRecord]:
    records = [_to_record(row, order, mapping) for order, row in enumerate(rows)]
    _check_unique_ids(rows, records)
    return records


def _check_unique_ids(rows: Sequence[_Row], records: Sequence[CommentRecord]) -> None:
    positions: defaultdict[str, list[int]] = defaultdict(list)
    for row, rec in zip(rows, records, strict=True):
        positions[rec.comment_id].append(row.number)
    duplicates = [(cid, numbers) for cid, numbers in positions.items() if len(numbers) > 1]
    if duplicates:
        unit = rows[0].unit
        listed = "; ".join(
            f"{cid!r} ({unit}s {', '.join(map(str, numbers))})" for cid, numbers in duplicates
        )
        raise IngestError(f"duplicate comment ids: {listed}")


# --------------------------------------------------------------------------- CSV


def _csv_rows(text: str, mapping: ColumnMapping) -> list[_Row]:
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    try:
        header = list(reader.fieldnames or ())
        _check_header(header, mapping)
        return [_csv_row(number, cells, len(header)) for number, cells in enumerate(reader, 1)]
    except csv.Error as exc:
        raise IngestError(f"malformed CSV near line {reader.line_num}: {exc}") from None


def _check_header(header: Sequence[str], mapping: ColumnMapping) -> None:
    for column in (mapping.id_col, mapping.text_col):
        if column not in header:
            raise IngestError(f"CSV header has no required column {column!r}")
    for column in _mapped_columns(mapping):
        if header.count(column) > 1:
            raise IngestError(f"column {column!r} appears more than once in the CSV header")


def _csv_row(number: int, cells: Mapping[str | None, object], width: int) -> _Row:
    if None in cells or None in cells.values():
        raise IngestError(f"row {number}: number of fields differs from the {width}-column header")
    return _Row("row", number, {str(key): value for key, value in cells.items()})


# --------------------------------------------------------------------------- JSONL


def _jsonl_rows(text: str) -> list[_Row]:
    # Split on "\n" only: raw U+2028 and friends are legal inside JSON strings.
    lines = enumerate(text.split("\n"), 1)
    return [_jsonl_row(number, line) for number, line in lines if line.strip()]


def _jsonl_row(number: int, line: str) -> _Row:
    try:
        value: object = json.loads(line, object_pairs_hook=_unique_keys)
    except json.JSONDecodeError as exc:
        raise IngestError(f"line {number}: invalid JSON ({exc.msg}, column {exc.colno})") from None
    except IngestError as exc:
        raise IngestError(f"line {number}: {exc}") from None
    if not isinstance(value, dict):
        raise IngestError(f"line {number}: expected a JSON object, got {_json_type(value)}")
    return _Row("line", number, value)


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise IngestError("JSON object has duplicate keys")
    return obj


def _json_type(value: object) -> str:
    return _JSON_TYPE_NAMES.get(type(value), type(value).__name__)


# --------------------------------------------------------------------------- cells


def _to_record(row: _Row, order: int, mapping: ColumnMapping) -> CommentRecord:
    try:
        return CommentRecord(
            comment_id=_comment_id(row.cells, mapping.id_col),
            text=_required(row.cells, mapping.text_col),
            permalink=_permalink(row.cells, mapping.permalink_col),
            created_at=_created_at(row.cells, mapping.date_col),
            source_order=order,
        )
    except IngestError as exc:
        raise IngestError(f"{row.where}: {exc}") from None


def _cell(cells: Mapping[str, object], column: str | None) -> str | None:
    """Return the cell as given; ``None`` when unmapped, absent or JSON null."""
    if column is None:
        return None
    value = cells.get(column)
    if value is None or isinstance(value, str):
        return value
    raise IngestError(f"column {column!r} must be a string, got {_json_type(value)}")


def _required(cells: Mapping[str, object], column: str) -> str:
    value = _cell(cells, column)
    if value is None or not value.strip():
        raise IngestError(f"missing or blank value in required column {column!r}")
    return value


def _optional(cells: Mapping[str, object], column: str | None) -> str | None:
    value = _cell(cells, column)
    return value.strip() if value is not None and value.strip() else None


def _comment_id(cells: Mapping[str, object], column: str) -> str:
    value = _required(cells, column)
    if value != value.strip():
        raise IngestError(f"comment id in column {column!r} has leading/trailing whitespace")
    return value


def _permalink(cells: Mapping[str, object], column: str | None) -> str | None:
    value = _optional(cells, column)
    if value is not None and not _HTTP_URL.fullmatch(value):
        raise IngestError(f"column {column!r} is not an http(s) URL")
    return value


def _created_at(cells: Mapping[str, object], column: str | None) -> str | None:
    value = _optional(cells, column)
    if value is None:
        return None
    try:
        return to_utc_iso(value)
    except (ValueError, OverflowError):
        raise IngestError(f"column {column!r} is not an ISO-8601 date") from None


# --------------------------------------------------------------------------- source fields


def _identifier(name: str, value: str) -> str:
    if not value.strip() or value != value.strip():
        raise IngestError(f"{name} must be non-blank without surrounding whitespace: {value!r}")
    return value


def _source_url(value: str | None) -> str | None:
    if value is not None and not _HTTP_URL.fullmatch(value):
        raise IngestError(f"source_url must be an http(s) URL: {value!r}")
    return value


def _rights_status(value: str) -> RightsStatus:
    matches = [status for status in RIGHTS_STATUSES if status == value]
    if not matches:
        raise IngestError(f"rights_status must be one of {list(RIGHTS_STATUSES)}: {value!r}")
    return matches[0]


def _timestamp(name: str, value: str) -> str:
    try:
        return to_utc_iso(value)
    except (ValueError, OverflowError):
        raise IngestError(f"{name} is not an ISO-8601 timestamp: {value!r}") from None
