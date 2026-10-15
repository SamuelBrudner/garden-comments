"""SQLite persistence, full-text search and scope-gated reads (SPEC §8, §9).

Every write runs in one savepoint: on any error it rolls back completely and re-raises. Every
viewer-facing read (ids, views, counts) joins ``comments c JOIN sources s`` and applies
:func:`crop_explorer.visibility.visibility_clause`, so private, expired or soft-deleted rows never
reach a viewer who may not see them. Comment bodies and evidence quotes are never logged nor put
into error messages; only ids and counts are.

The database file is created owner-only (it holds comment text verbatim), carries a schema
version, and deletes leave no readable residue: SQLite zeroes freed content
(``PRAGMA secure_delete``), the FTS index removes deleted postings (FTS5 ``secure-delete``) and
is optimised after every deletion or text change.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from itertools import groupby
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, NamedTuple

from loguru import logger

from crop_explorer.schemas import (
    CommentRecord,
    ImportStats,
    SourceRecord,
    ValidatedComment,
    ValidatedFact,
    ValidatedMention,
)
from crop_explorer.views import CommentView, FactView, MentionView, ViewerScope
from crop_explorer.visibility import require_utc_iso, visibility_clause

__all__ = [
    "MAX_FTS_TOKENS",
    "MEMORY_PATH",
    "SCHEMA_VERSION",
    "SUMMARY_KEYS",
    "ExtractableComment",
    "connect",
    "delete_source",
    "extractable_texts",
    "fts_query",
    "init_schema",
    "iter_comments_for_extraction",
    "load_comment_views",
    "purge_expired",
    "replace_extraction",
    "stored_texts",
    "summary_counts",
    "sync_source_comments",
    "visible_comment_ids",
]

MEMORY_PATH: Final = Path(":memory:")
"""Pass to :func:`connect` for a private in-memory database."""

SUMMARY_KEYS: Final = ("sources", "comments", "parsed_comments", "mentions", "facts")
"""Keys of :func:`summary_counts`, in display order."""

SCHEMA_VERSION: Final = 1
"""``PRAGMA user_version`` of a database created by this module; checked on every connect."""

MAX_FTS_TOKENS: Final = 32
"""Most distinct words :func:`fts_query` keeps from one search text.

FTS5 work grows faster than linearly with the number of prefix terms in one query, so a pasted
wall of text could tie up the server on every rerun. De-duplicating and then capping the terms
bounds the cost of any query; 32 words is far more than a real search uses.
"""


class ExtractableComment(NamedTuple):
    """One comment as handed to the offline extractor (pipeline-internal; private text)."""

    comment_id: str
    text: str
    text_hash: str
    """SHA-256 hex digest of the UTF-8 text, as stored in ``comments.text_hash``."""


_SCHEMA: Final[tuple[str, ...]] = (
    """CREATE TABLE IF NOT EXISTS sources (
        source_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        source_url TEXT,
        rights_status TEXT NOT NULL
            CHECK (rights_status IN ('private_unverified', 'synthetic', 'cleared')),
        imported_at TEXT NOT NULL,
        expires_at TEXT,
        dataset_hash TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY,
        comment_id TEXT NOT NULL UNIQUE,
        source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
        text TEXT NOT NULL,
        permalink TEXT,
        source_order INTEGER NOT NULL,
        created_at TEXT,
        imported_at TEXT NOT NULL,
        deleted_at TEXT,
        text_hash TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS parses (
        comment_id TEXT PRIMARY KEY REFERENCES comments(comment_id) ON DELETE CASCADE,
        status TEXT NOT NULL,
        extractor_version TEXT NOT NULL,
        warnings_json TEXT NOT NULL,
        loaded_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS mentions (
        mention_id TEXT PRIMARY KEY,
        comment_id TEXT NOT NULL REFERENCES comments(comment_id) ON DELETE CASCADE,
        mention_index INTEGER NOT NULL,
        crop TEXT,
        variety_as_written TEXT,
        variety_key TEXT,
        topics_json TEXT NOT NULL,
        evidence_quote TEXT NOT NULL,
        extractor_version TEXT NOT NULL,
        status TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS environment_facts (
        fact_id TEXT PRIMARY KEY,
        mention_id TEXT NOT NULL REFERENCES mentions(mention_id) ON DELETE CASCADE,
        fact_index INTEGER NOT NULL,
        key TEXT NOT NULL,
        value_as_written TEXT NOT NULL,
        normalized_value TEXT,
        evidence_quote TEXT NOT NULL,
        applicability TEXT NOT NULL,
        basis TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS comments_source_idx ON comments(source_id)",
    "CREATE INDEX IF NOT EXISTS mentions_comment_idx ON mentions(comment_id)",
    "CREATE INDEX IF NOT EXISTS facts_mention_idx ON environment_facts(mention_id)",
    """CREATE VIRTUAL TABLE IF NOT EXISTS comments_fts USING fts5(
        text, content='comments', content_rowid='id',
        tokenize='porter unicode61 remove_diacritics 2')""",
    """CREATE TRIGGER IF NOT EXISTS comments_fts_insert AFTER INSERT ON comments BEGIN
        INSERT INTO comments_fts (rowid, text) VALUES (new.id, new.text);
    END""",
    """CREATE TRIGGER IF NOT EXISTS comments_fts_delete AFTER DELETE ON comments BEGIN
        INSERT INTO comments_fts (comments_fts, rowid, text) VALUES ('delete', old.id, old.text);
    END""",
    """CREATE TRIGGER IF NOT EXISTS comments_fts_update AFTER UPDATE OF id, text ON comments BEGIN
        INSERT INTO comments_fts (comments_fts, rowid, text) VALUES ('delete', old.id, old.text);
        INSERT INTO comments_fts (rowid, text) VALUES (new.id, new.text);
    END""",
)

_FRESH_SCHEMA_SETTINGS: Final = (
    # Deleting a row removes its postings from the FTS index instead of appending delete
    # markers that still hold the words (FTS5 'secure-delete', SQLite >= 3.42).
    "INSERT INTO comments_fts (comments_fts, rank) VALUES ('secure-delete', 1)",
    f"PRAGMA user_version = {SCHEMA_VERSION}",
)
_CASCADES: Final = frozenset(
    {
        ("comments", "source_id", "sources", "CASCADE"),
        ("parses", "comment_id", "comments", "CASCADE"),
        ("mentions", "comment_id", "comments", "CASCADE"),
        ("environment_facts", "mention_id", "mentions", "CASCADE"),
    }
)
"""Foreign keys deletion relies on: ``(table, column, parent table, ON DELETE action)``."""

_REQUIRED_PRAGMAS: Final = ("foreign_keys", "secure_delete")
_PRIVATE_DIR_MODE: Final = 0o700
_PRIVATE_FILE_MODE: Final = 0o600
_HAS_TABLES: Final = "SELECT EXISTS (SELECT 1 FROM sqlite_master WHERE type = 'table')"
_SELECT_FOREIGN_KEYS: Final = (
    'SELECT m.name, f."from", f."table", f.on_delete'
    " FROM sqlite_master AS m, pragma_foreign_key_list(m.name) AS f WHERE m.type = 'table'"
)
_OPTIMIZE_FTS: Final = "INSERT INTO comments_fts (comments_fts) VALUES ('optimize')"

_UPSERT_SOURCE: Final = (
    "INSERT INTO sources (source_id, kind, source_url, rights_status, imported_at,"
    " expires_at, dataset_hash)"
    " VALUES (:source_id, :kind, :source_url, :rights_status, :imported_at,"
    " :expires_at, :dataset_hash)"
    " ON CONFLICT (source_id) DO UPDATE SET kind = excluded.kind,"
    " source_url = excluded.source_url, rights_status = excluded.rights_status,"
    " imported_at = excluded.imported_at, dataset_hash = excluded.dataset_hash,"
    " expires_at = CASE WHEN :clear_expiry THEN NULL"
    " ELSE COALESCE(excluded.expires_at, sources.expires_at) END"
)
_SELECT_SOURCE_STATE: Final = "SELECT rights_status, expires_at FROM sources WHERE source_id = ?"
_SELECT_STORED_HASHES: Final = "SELECT comment_id, text_hash FROM comments WHERE source_id = ?"
_SAVEPOINT: Final = "crop_explorer_tx"
_ORDER: Final = "c.source_id, c.source_order, c.comment_id"
_VISIBLE_FROM: Final = "comments c JOIN sources s ON s.source_id = c.source_id"
_IDS_PARAM: Final = "(SELECT value FROM json_each(?))"
_SELECT_OWNERS: Final = (
    f"SELECT comment_id, source_id FROM comments WHERE comment_id IN {_IDS_PARAM}"
    " ORDER BY comment_id"
)
_INSERT_COMMENT: Final = (
    "INSERT INTO comments (comment_id, source_id, text, permalink, source_order,"
    " created_at, imported_at, text_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)
_UPDATE_COMMENT: Final = (
    "UPDATE comments SET text = ?, text_hash = ?, permalink = ?, source_order = ?,"
    " created_at = ?, imported_at = ? WHERE comment_id = ?"
)
_REFRESH_COMMENT: Final = (
    "UPDATE comments SET permalink = ?, source_order = ?, created_at = ? WHERE comment_id = ?"
)
_INSERT_PARSE: Final = (
    "INSERT INTO parses (comment_id, status, extractor_version, warnings_json, loaded_at)"
    " VALUES (?, ?, ?, ?, ?)"
)
_INSERT_MENTION: Final = (
    "INSERT INTO mentions (mention_id, comment_id, mention_index, crop, variety_as_written,"
    " variety_key, topics_json, evidence_quote, extractor_version, status)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_INSERT_FACT: Final = (
    "INSERT INTO environment_facts (fact_id, mention_id, fact_index, key, value_as_written,"
    " normalized_value, evidence_quote, applicability, basis)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_SELECT_EXPIRED: Final = (
    "SELECT source_id FROM sources WHERE expires_at IS NOT NULL AND expires_at <= ?"
    " ORDER BY source_id"
)
_EXTRACTABLE: Final = "c.deleted_at IS NULL AND (s.expires_at IS NULL OR s.expires_at > :now)"
"""Comments the pipeline may still process: not soft-deleted, source permission not ended."""
_SELECT_FOR_EXTRACTION: Final = (
    f"SELECT c.comment_id, c.text, c.text_hash FROM {_VISIBLE_FROM}"
    " LEFT JOIN parses p ON p.comment_id = c.comment_id"
    f" WHERE {_EXTRACTABLE} AND (:only_unparsed = 0 OR p.comment_id IS NULL)"
    f" ORDER BY {_ORDER}"
)
_SELECT_EXTRACTABLE_TEXTS: Final = (
    f"SELECT c.comment_id, c.text, c.text_hash FROM {_VISIBLE_FROM}"
    f" WHERE c.comment_id IN (SELECT value FROM json_each(:ids)) AND {_EXTRACTABLE}"
    " ORDER BY c.comment_id"
)
_SELECT_STORED_TEXTS: Final = (
    f"SELECT comment_id, text FROM comments WHERE comment_id IN {_IDS_PARAM} ORDER BY comment_id"
)
_SELECT_VIEW_COMMENTS: Final = (
    "SELECT c.comment_id, c.source_id, s.kind AS source_kind, s.rights_status, c.text,"
    " c.permalink, c.source_order, c.created_at, p.status AS parse_status"
    f" FROM {_VISIBLE_FROM} LEFT JOIN parses p ON p.comment_id = c.comment_id"
)
_SELECT_SUMMARY: Final = (
    "SELECT COUNT(DISTINCT c.source_id) AS sources, COUNT(DISTINCT c.comment_id) AS comments,"
    " COUNT(DISTINCT p.comment_id) AS parsed_comments,"
    " COUNT(DISTINCT m.mention_id) AS mentions, COUNT(DISTINCT f.fact_id) AS facts"
    f" FROM {_VISIBLE_FROM}"
    " LEFT JOIN parses p ON p.comment_id = c.comment_id"
    " LEFT JOIN mentions m ON m.comment_id = c.comment_id"
    " LEFT JOIN environment_facts f ON f.mention_id = m.mention_id"
)
_SELECT_VIEW_FACTS: Final = (
    "SELECT f.fact_id, f.mention_id, f.key, f.value_as_written, f.normalized_value,"
    " f.evidence_quote, f.applicability, f.basis FROM environment_facts f"
    " JOIN mentions m ON m.mention_id = f.mention_id"
    " JOIN comments c ON c.comment_id = m.comment_id"
    " JOIN sources s ON s.source_id = c.source_id"
)
_SELECT_VIEW_MENTIONS: Final = (
    "SELECT m.mention_id, m.comment_id, m.crop, m.variety_as_written, m.variety_key,"
    " m.topics_json, m.evidence_quote, m.extractor_version FROM mentions m"
    " JOIN comments c ON c.comment_id = m.comment_id"
    " JOIN sources s ON s.source_id = c.source_id"
)
_WORD_CHAR: Final = re.compile(r"\w")
_MAX_IDS_IN_MESSAGE: Final = 10
_REPLACE_HINT: Final = "without replace=True (CLI: ingest --replace)"

Outcome = Literal["inserted", "updated", "unchanged"]

# --------------------------------------------------------------------------- connection


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the database at ``path`` with the schema initialised.

    A missing database file is created owner-only (mode 0600) before SQLite opens it, in a
    parent directory created owner-only (0700) when missing; existing files and directories
    keep their modes. The connection enforces foreign keys, zeroes deleted content
    (``PRAGMA secure_delete``), returns rows addressable by column name (:class:`sqlite3.Row`)
    and uses SQLite-level autocommit; this module wraps each write in its own transaction.

    Parameters
    ----------
    path
        Database file, or :data:`MEMORY_PATH` (``Path(":memory:")``) for an in-memory database.

    Returns
    -------
    sqlite3.Connection
        The open connection.

    Raises
    ------
    RuntimeError
        If SQLite cannot enforce foreign keys or secure deletion.
    sqlite3.DatabaseError
        If the file is not a database, or not one created by this module (see
        :func:`init_schema`).
    """
    if path != MEMORY_PATH:
        _create_private_file(path)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        _configure(conn)
        init_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create the schema in an empty database, or check that an existing one is ours.

    An empty database gets every table, index, the FTS index with its sync triggers, FTS5
    secure deletion and ``PRAGMA user_version`` :data:`SCHEMA_VERSION`. A database of this
    version is left as it is (idempotent). Nothing is written to any other database.

    Parameters
    ----------
    conn
        Open connection.

    Raises
    ------
    sqlite3.DatabaseError
        If the database has tables but another schema version (including 0, i.e. not created
        by this module), or lacks the ``ON DELETE CASCADE`` foreign keys that deletion relies on.
    """
    with _transaction(conn):
        fresh = _check_schema(conn)
        for statement in _SCHEMA:
            conn.execute(statement)
        if fresh:
            for setting in _FRESH_SCHEMA_SETTINGS:
                conn.execute(setting)


def _create_private_file(path: Path) -> None:
    """Create a missing parent directory (0700) and a missing database file (0600)."""
    path.parent.mkdir(mode=_PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _PRIVATE_FILE_MODE)
    except FileExistsError:
        return
    os.close(descriptor)


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    for pragma in _REQUIRED_PRAGMAS:
        conn.execute(f"PRAGMA {pragma} = ON")
        if conn.execute(f"PRAGMA {pragma}").fetchone()[0] != 1:
            raise RuntimeError(f"SQLite refused to enable PRAGMA {pragma}")


def _check_schema(conn: sqlite3.Connection) -> bool:
    """Return whether the database is empty; raise unless it was created by this module."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version == 0 and not conn.execute(_HAS_TABLES).fetchone()[0]:
        return True
    missing = _CASCADES - {tuple(row) for row in conn.execute(_SELECT_FOREIGN_KEYS)}
    if version != SCHEMA_VERSION or missing:
        detail = "; it lacks the cascading foreign keys deletion relies on" if missing else ""
        raise sqlite3.DatabaseError(
            f"unsupported database schema (version {version}, expected {SCHEMA_VERSION}{detail})"
            ": it was not created by this version of crop_explorer; use a new database file"
        )
    return False


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Run the block atomically: commit on success, roll back and re-raise on any error."""
    conn.execute(f"SAVEPOINT {_SAVEPOINT}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO {_SAVEPOINT}")
        conn.execute(f"RELEASE {_SAVEPOINT}")
        raise
    conn.execute(f"RELEASE {_SAVEPOINT}")


# --------------------------------------------------------------------------- import


def sync_source_comments(
    conn: sqlite3.Connection,
    source: SourceRecord,
    comments: Sequence[CommentRecord],
    *,
    replace: bool = False,
    clear_expiry: bool = False,
) -> ImportStats:
    """Make the stored comments of ``source`` equal ``comments`` (idempotent re-import).

    The ``sources`` row is upserted; a ``source.expires_at`` of ``None`` keeps the stored
    expiry unless ``clear_expiry``. New ids are inserted; ids whose text hash is unchanged keep
    their extraction (permalink, ``created_at`` and ``source_order`` are refreshed); ids whose
    text changed are updated and lose their extraction, because its quotes may no longer be
    substrings; stored ids absent from ``comments`` are deleted with everything derived from
    them. A soft-deleted comment stays soft-deleted.

    Re-importing an existing source is refused when its stored expiry is at or before
    ``source.imported_at`` (purge or delete it first), and, unless ``replace``, when it would
    change the stored ``rights_status`` or when the source has stored comments and ``comments``
    shares none of their ids (a different dataset under the same source id).

    Parameters
    ----------
    conn
        Open connection.
    source
        Provenance and rights state of the dataset; ``imported_at`` is the current time.
    comments
        The complete current contents of the source.
    replace
        Allow replacing every stored comment and changing the rights status.
    clear_expiry
        Remove the stored expiry (``source.expires_at`` must then be ``None``).

    Returns
    -------
    ImportStats
        Counts of inserted, updated, unchanged and deleted comments, plus the effective rights
        status and expiry stored for the source.

    Raises
    ------
    ValueError
        If ``comments`` repeats an id, an id already belongs to another source, a timestamp is
        not canonical UTC, ``clear_expiry`` is combined with an expiry, or the re-import is
        refused as described above. Messages carry ids and counts, never text. Nothing is
        written in that case.
    """
    _check_source(source, clear_expiry=clear_expiry)
    _check_records(comments)
    with _transaction(conn):
        existing = _stored_hashes(conn, source.source_id)
        _check_reimport(conn, source, existing, comments, replace=replace)
        _upsert_source(conn, source, clear_expiry=clear_expiry)
        _check_collisions(conn, source.source_id, comments, existing)
        outcomes = _apply_records(conn, source, comments, existing)
        deleted = _delete_absent(conn, existing, comments)
        if deleted or outcomes["updated"]:
            conn.execute(_OPTIMIZE_FTS)
        stats = _import_stats(conn, source.source_id, outcomes, deleted)
    _log_import(stats)
    return stats


def _check_source(source: SourceRecord, *, clear_expiry: bool) -> None:
    require_utc_iso(source.imported_at, "imported_at")
    if source.expires_at is None:
        return
    require_utc_iso(source.expires_at, "expires_at")
    if clear_expiry:
        raise ValueError("clear_expiry and a new expires_at are mutually exclusive")


def _check_records(records: Sequence[CommentRecord]) -> None:
    counts = Counter(record.comment_id for record in records)
    duplicates = sorted(comment_id for comment_id, n in counts.items() if n > 1)
    if duplicates:
        raise ValueError(f"Duplicate comment_id(s) within one import: {_id_list(duplicates)}")
    for record in records:
        if record.created_at is not None:
            require_utc_iso(record.created_at, f"created_at of comment {record.comment_id!r}")


def _check_reimport(
    conn: sqlite3.Connection,
    source: SourceRecord,
    existing: Mapping[str, str],
    comments: Sequence[CommentRecord],
    *,
    replace: bool,
) -> None:
    """Refuse a re-import that would revive expired content or silently replace a source."""
    stored = conn.execute(_SELECT_SOURCE_STATE, (source.source_id,)).fetchone()
    if stored is None:
        return
    _refuse_expired(source, stored["expires_at"])
    if not replace:
        _refuse_rights_change(source, stored["rights_status"])
        _refuse_disjoint(source.source_id, existing, comments)


def _refuse_expired(source: SourceRecord, stored_expiry: str | None) -> None:
    if stored_expiry is not None and stored_expiry <= source.imported_at:
        raise ValueError(
            f"source {source.source_id!r} expired at {stored_expiry}; its content may not be "
            "re-imported: run purge-expired or delete-source first"
        )


def _refuse_rights_change(source: SourceRecord, stored_rights: str) -> None:
    if stored_rights != source.rights_status:
        raise ValueError(
            f"source {source.source_id!r} is stored with rights_status {stored_rights!r}; "
            f"refusing to change it to {source.rights_status!r} {_REPLACE_HINT}"
        )


def _refuse_disjoint(
    source_id: str, existing: Mapping[str, str], comments: Sequence[CommentRecord]
) -> None:
    if existing and existing.keys().isdisjoint(record.comment_id for record in comments):
        raise ValueError(
            f"source {source_id!r} holds {len(existing)} stored comment(s) and the new import "
            f"of {len(comments)} comment(s) shares none of their ids; refusing to replace them "
            f"{_REPLACE_HINT}"
        )


def _upsert_source(conn: sqlite3.Connection, source: SourceRecord, *, clear_expiry: bool) -> None:
    conn.execute(_UPSERT_SOURCE, {**source.model_dump(), "clear_expiry": int(clear_expiry)})


def _stored_hashes(conn: sqlite3.Connection, source_id: str) -> dict[str, str]:
    rows = conn.execute(_SELECT_STORED_HASHES, (source_id,))
    return {str(row["comment_id"]): str(row["text_hash"]) for row in rows}


def _check_collisions(
    conn: sqlite3.Connection,
    source_id: str,
    comments: Sequence[CommentRecord],
    existing: Mapping[str, str],
) -> None:
    new_ids = [record.comment_id for record in comments if record.comment_id not in existing]
    rows = conn.execute(_SELECT_OWNERS, (_json_ids(new_ids),)).fetchall()
    if rows:
        owners = ", ".join(sorted({str(row["source_id"]) for row in rows}))
        raise ValueError(
            f"comment_id collision while importing source {source_id!r}: "
            f"{_id_list([str(row['comment_id']) for row in rows])} already belong to "
            f"source(s) {owners}"
        )


def _apply_records(
    conn: sqlite3.Connection,
    source: SourceRecord,
    records: Sequence[CommentRecord],
    existing: Mapping[str, str],
) -> dict[Outcome, int]:
    outcomes: dict[Outcome, int] = {"inserted": 0, "updated": 0, "unchanged": 0}
    for record in records:
        outcome = _classify(record, existing.get(record.comment_id))
        _WRITERS[outcome](conn, source, record)
        outcomes[outcome] += 1
    return outcomes


def _delete_absent(
    conn: sqlite3.Connection, existing: Mapping[str, str], records: Sequence[CommentRecord]
) -> int:
    absent = sorted(existing.keys() - {record.comment_id for record in records})
    conn.executemany("DELETE FROM comments WHERE comment_id = ?", [(i,) for i in absent])
    return len(absent)


def _import_stats(
    conn: sqlite3.Connection, source_id: str, outcomes: dict[Outcome, int], deleted: int
) -> ImportStats:
    stored = conn.execute(_SELECT_SOURCE_STATE, (source_id,)).fetchone()
    return ImportStats(
        source_id=source_id,
        deleted=deleted,
        rights_status=stored["rights_status"],
        expires_at=stored["expires_at"],
        **outcomes,
    )


def _log_import(stats: ImportStats) -> None:
    logger.info(
        "Synced source {}: {} inserted, {} updated, {} unchanged, {} deleted; rights {}, "
        "expires {}",
        stats.source_id,
        stats.inserted,
        stats.updated,
        stats.unchanged,
        stats.deleted,
        stats.rights_status,
        stats.expires_at,
    )


def _classify(record: CommentRecord, stored_hash: str | None) -> Outcome:
    if stored_hash is None:
        return "inserted"
    return "unchanged" if stored_hash == _text_hash(record.text) else "updated"


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _insert_comment(conn: sqlite3.Connection, source: SourceRecord, record: CommentRecord) -> None:
    conn.execute(
        _INSERT_COMMENT,
        (
            record.comment_id,
            source.source_id,
            record.text,
            record.permalink,
            record.source_order,
            record.created_at,
            source.imported_at,
            _text_hash(record.text),
        ),
    )


def _update_comment(conn: sqlite3.Connection, source: SourceRecord, record: CommentRecord) -> None:
    conn.execute(
        _UPDATE_COMMENT,
        (
            record.text,
            _text_hash(record.text),
            record.permalink,
            record.source_order,
            record.created_at,
            source.imported_at,
            record.comment_id,
        ),
    )
    _delete_extraction(conn, record.comment_id)


def _refresh_comment(
    conn: sqlite3.Connection, source: SourceRecord, record: CommentRecord
) -> None:
    del source  # unchanged text keeps its original import time
    conn.execute(
        _REFRESH_COMMENT,
        (record.permalink, record.source_order, record.created_at, record.comment_id),
    )


_WRITERS: Final[
    Mapping[Outcome, Callable[[sqlite3.Connection, SourceRecord, CommentRecord], None]]
] = MappingProxyType(
    {"inserted": _insert_comment, "updated": _update_comment, "unchanged": _refresh_comment}
)


# --------------------------------------------------------------------------- extraction


def replace_extraction(
    conn: sqlite3.Connection, validated: ValidatedComment, loaded_at: str
) -> None:
    """Atomically replace a comment's parse status, mentions and facts.

    Ids are deterministic (``{comment_id}#m{i}``, ``{mention_id}#f{j}``), so loading the same
    validated comment again leaves identical rows.

    Parameters
    ----------
    conn
        Open connection.
    validated
        Validation output for one comment.
    loaded_at
        Canonical UTC load time.

    Raises
    ------
    KeyError
        If the comment does not exist.
    ValueError
        If ``loaded_at`` is not canonical UTC or an evidence quote is not an exact substring of
        the stored comment text. Nothing is written in that case.
    """
    require_utc_iso(loaded_at, "loaded_at")
    with _transaction(conn):
        _check_quotes(validated, _stored_text(conn, validated.comment_id))
        _delete_extraction(conn, validated.comment_id)
        _insert_parse(conn, validated, loaded_at)
        for index, mention in enumerate(validated.mentions):
            _insert_mention(conn, validated, index, mention)
    logger.debug(
        "Stored extraction for comment {}: status {}, {} mention(s)",
        validated.comment_id,
        validated.status,
        len(validated.mentions),
    )


def _stored_text(conn: sqlite3.Connection, comment_id: str) -> str:
    row = conn.execute("SELECT text FROM comments WHERE comment_id = ?", (comment_id,)).fetchone()
    if row is None:
        raise KeyError(f"Unknown comment_id {comment_id!r}; import the comment first")
    return str(row["text"])


def _check_quotes(validated: ValidatedComment, text: str) -> None:
    for m_index, mention in enumerate(validated.mentions):
        _require_substring(
            mention.evidence_quote, text, validated.comment_id, f"mention {m_index}"
        )
        for f_index, fact in enumerate(mention.facts):
            where = f"fact {f_index} of mention {m_index}"
            _require_substring(fact.evidence_quote, text, validated.comment_id, where)


def _require_substring(quote: str, text: str, comment_id: str, where: str) -> None:
    if quote not in text:
        raise ValueError(
            f"Evidence quote of {where} is not an exact substring of comment {comment_id!r}"
        )


def _delete_extraction(conn: sqlite3.Connection, comment_id: str) -> None:
    conn.execute("DELETE FROM parses WHERE comment_id = ?", (comment_id,))
    conn.execute("DELETE FROM mentions WHERE comment_id = ?", (comment_id,))


def _insert_parse(conn: sqlite3.Connection, validated: ValidatedComment, loaded_at: str) -> None:
    conn.execute(
        _INSERT_PARSE,
        (
            validated.comment_id,
            validated.status,
            validated.extractor_version,
            json.dumps(list(validated.warnings)),
            loaded_at,
        ),
    )


def _insert_mention(
    conn: sqlite3.Connection, validated: ValidatedComment, index: int, mention: ValidatedMention
) -> None:
    mention_id = f"{validated.comment_id}#m{index}"
    conn.execute(_INSERT_MENTION, _mention_values(validated, mention_id, index, mention))
    conn.executemany(
        _INSERT_FACT,
        [
            (f"{mention_id}#f{j}", mention_id, j, *_fact_values(fact))
            for j, fact in enumerate(mention.facts)
        ],
    )


def _mention_values(
    validated: ValidatedComment, mention_id: str, index: int, mention: ValidatedMention
) -> tuple[str | int | None, ...]:
    return (
        mention_id,
        validated.comment_id,
        index,
        mention.crop,
        mention.variety_as_written,
        mention.variety_key,
        json.dumps(list(mention.topics)),
        mention.evidence_quote,
        validated.extractor_version,
        validated.status,
    )


def _fact_values(fact: ValidatedFact) -> tuple[str | None, ...]:
    return (
        fact.key,
        fact.value_as_written,
        fact.normalized_value,
        fact.evidence_quote,
        fact.applicability,
        fact.basis,
    )


# --------------------------------------------------------------------------- deletion


def delete_source(conn: sqlite3.Connection, source_id: str) -> int:
    """Delete a source and, by cascade, its comments, parses, mentions, facts and FTS entries.

    The FTS index is optimised afterwards, so no deleted word stays readable in it.

    Parameters
    ----------
    conn
        Open connection.
    source_id
        Source to delete.

    Returns
    -------
    int
        Number of comments removed.

    Raises
    ------
    KeyError
        If no such source exists.
    """
    with _transaction(conn):
        _require_source(conn, source_id)
        removed = int(
            conn.execute(
                "SELECT COUNT(*) FROM comments WHERE source_id = ?", (source_id,)
            ).fetchone()[0]
        )
        conn.execute("DELETE FROM sources WHERE source_id = ?", (source_id,))
        conn.execute(_OPTIMIZE_FTS)
    logger.info("Deleted source {} with {} comment(s)", source_id, removed)
    return removed


def _require_source(conn: sqlite3.Connection, source_id: str) -> None:
    row = conn.execute("SELECT 1 FROM sources WHERE source_id = ?", (source_id,)).fetchone()
    if row is None:
        raise KeyError(f"Unknown source_id {source_id!r}")


def purge_expired(conn: sqlite3.Connection, now: str) -> list[str]:
    """Delete every source whose ``expires_at`` is at or before ``now`` (with cascade).

    The FTS index is optimised afterwards when anything was purged.

    Parameters
    ----------
    conn
        Open connection.
    now
        Canonical UTC current time.

    Returns
    -------
    list[str]
        Purged source ids, sorted.

    Raises
    ------
    ValueError
        If ``now`` is not canonical UTC.
    """
    require_utc_iso(now, "now")
    with _transaction(conn):
        rows = conn.execute(_SELECT_EXPIRED, (now,)).fetchall()
        expired = [str(row["source_id"]) for row in rows]
        conn.executemany("DELETE FROM sources WHERE source_id = ?", [(i,) for i in expired])
        if expired:
            conn.execute(_OPTIMIZE_FTS)
    logger.info("Purged {} expired source(s): {}", len(expired), expired)
    return expired


# --------------------------------------------------------------------------- pipeline reads


def stored_texts(conn: sqlite3.Connection, comment_ids: Sequence[str]) -> dict[str, str]:
    """Return the stored text of each known id (pipeline-internal; ignores scope and expiry).

    Use :func:`extractable_texts` to decide what may still be processed.

    Parameters
    ----------
    conn
        Open connection.
    comment_ids
        Ids to look up; unknown ids are absent from the result.

    Returns
    -------
    dict[str, str]
        ``comment_id -> text``, ordered by id.

    Raises
    ------
    TypeError
        If ``comment_ids`` is a bare string or holds non-string ids.
    """
    rows = conn.execute(_SELECT_STORED_TEXTS, (_json_ids(comment_ids),))
    return {str(row["comment_id"]): str(row["text"]) for row in rows}


def extractable_texts(
    conn: sqlite3.Connection, comment_ids: Sequence[str], now: str
) -> dict[str, tuple[str, str]]:
    """Return text and text hash of each id the pipeline may still process (pipeline-internal).

    Rights status is ignored, as in :func:`iter_comments_for_extraction`, but unknown and
    soft-deleted comments and comments of sources whose ``expires_at`` is at or before ``now``
    are absent: permission to derive anything from them has ended.

    Parameters
    ----------
    conn
        Open connection.
    comment_ids
        Ids to look up.
    now
        Canonical UTC current time.

    Returns
    -------
    dict[str, tuple[str, str]]
        ``comment_id -> (text, text_hash)``, ordered by id; ``text_hash`` is the SHA-256 hex
        digest of the UTF-8 text.

    Raises
    ------
    TypeError
        If ``comment_ids`` is a bare string or holds non-string ids.
    ValueError
        If ``now`` is not canonical UTC.
    """
    require_utc_iso(now, "now")
    rows = conn.execute(_SELECT_EXTRACTABLE_TEXTS, {"ids": _json_ids(comment_ids), "now": now})
    return {item.comment_id: (item.text, item.text_hash) for item in map(_extractable, rows)}


def iter_comments_for_extraction(
    conn: sqlite3.Connection, only_unparsed: bool, now: str
) -> list[ExtractableComment]:
    """List the comments to send to the offline extractor (pipeline-internal).

    Rights status is ignored (extraction runs on private data), but comments of sources whose
    ``expires_at`` is at or before ``now`` are excluded: permission to process them has ended
    even if :func:`purge_expired` has not run yet. Soft-deleted comments are excluded too.
    Ordered like viewer results.

    Parameters
    ----------
    conn
        Open connection.
    only_unparsed
        Restrict to comments that have no stored extraction.
    now
        Canonical UTC current time.

    Returns
    -------
    list[ExtractableComment]
        Comment ids with their exact stored text and its hash.

    Raises
    ------
    ValueError
        If ``now`` is not canonical UTC.
    """
    require_utc_iso(now, "now")
    params = {"now": now, "only_unparsed": int(only_unparsed)}
    return [_extractable(row) for row in conn.execute(_SELECT_FOR_EXTRACTION, params)]


def _extractable(row: sqlite3.Row) -> ExtractableComment:
    return ExtractableComment(str(row["comment_id"]), str(row["text"]), str(row["text_hash"]))


def _json_ids(comment_ids: Sequence[str]) -> str:
    if isinstance(comment_ids, str) or not all(isinstance(i, str) for i in comment_ids):
        raise TypeError("comment_ids must be a sequence of str ids (not a single str)")
    return json.dumps(list(comment_ids))


def _id_list(ids: Sequence[str]) -> str:
    shown = ", ".join(repr(i) for i in ids[:_MAX_IDS_IN_MESSAGE])
    hidden = len(ids) - _MAX_IDS_IN_MESSAGE
    return f"{shown} (+{hidden} more)" if hidden > 0 else shown


# --------------------------------------------------------------------------- viewer reads


def fts_query(text: str) -> str | None:
    r"""Turn free user text into a safe FTS5 query: every word a quoted prefix term, ANDed.

    The text is NFC-normalised, then split into words: runs of Unicode word characters
    (``\w``) together with the combining marks inside them (a decomposed ``n`` + U+0303 stays
    one word, as in FTS5's own tokenizer). Repeated words are dropped and at most
    :data:`MAX_FTS_TOKENS` distinct words are kept, in first-seen order. FTS5 syntax in the
    input (quotes, ``*``, ``-``, ``AND``/``OR``/``NOT``/``NEAR``, column filters) is
    neutralised: only words survive, each inside double quotes. Matching is case-insensitive,
    diacritic-insensitive and porter-stemmed.

    Parameters
    ----------
    text
        Raw search text.

    Returns
    -------
    str | None
        The MATCH expression, or ``None`` when ``text`` has no word characters (no text filter).
    """
    tokens = _search_tokens(text)
    if not tokens:
        return None
    return " ".join(f'"{token}"*' for token in tokens)


def _search_tokens(text: str) -> list[str]:
    """Return the distinct words of ``text`` (NFC) in first-seen order, capped."""
    runs = groupby(unicodedata.normalize("NFC", text), key=_is_token_char)
    words = (_from_first_word_char("".join(chars)) for keep, chars in runs if keep)
    return list(dict.fromkeys(word for word in words if word))[:MAX_FTS_TOKENS]


def _is_token_char(char: str) -> bool:
    r"""Word character (``\w``, i.e. alphanumeric or ``_``) or combining mark."""
    return char.isalnum() or char == "_" or unicodedata.category(char).startswith("M")


def _from_first_word_char(run: str) -> str:
    """Drop combining marks before the first word character (a mark cannot start a word)."""
    first = _WORD_CHAR.search(run)
    return "" if first is None else run[first.start() :]


def visible_comment_ids(
    conn: sqlite3.Connection, scope: ViewerScope, now: str, text: str | None
) -> list[str]:
    """Return ids of comments visible in ``scope`` at ``now``, optionally full-text filtered.

    Comments are found by text whether or not they were ever extracted.

    Parameters
    ----------
    conn
        Open connection.
    scope
        Viewer scope.
    now
        Canonical UTC current time.
    text
        Free search text, or ``None``; text without word characters applies no filter.

    Returns
    -------
    list[str]
        Ids ordered by ``(source_id, source_order, comment_id)``.

    Raises
    ------
    ValueError
        If ``scope`` is unknown or ``now`` is not canonical UTC.
    """
    where, params = visibility_clause(scope, now)
    match = None if text is None else fts_query(text)
    if match is not None:
        where += " AND c.id IN (SELECT rowid FROM comments_fts WHERE comments_fts MATCH ?)"
        params.append(match)
    sql = f"SELECT c.comment_id FROM {_VISIBLE_FROM} WHERE {where} ORDER BY {_ORDER}"
    return [str(row["comment_id"]) for row in conn.execute(sql, params)]


def load_comment_views(
    conn: sqlite3.Connection,
    scope: ViewerScope,
    now: str,
    comment_ids: Sequence[str] | None,
) -> list[CommentView]:
    """Load visible comments with provenance, parse status, mentions and facts.

    Visibility is re-applied: requested ids that are not visible in ``scope`` (private, expired,
    soft-deleted or unknown) are silently absent, never returned.

    Parameters
    ----------
    conn
        Open connection.
    scope
        Viewer scope.
    now
        Canonical UTC current time.
    comment_ids
        Ids to load, or ``None`` for every visible comment.

    Returns
    -------
    list[CommentView]
        Ordered by ``(source_id, source_order, comment_id)``; mentions by ``mention_index``,
        facts by ``fact_index``.

    Raises
    ------
    ValueError
        If ``scope`` is unknown or ``now`` is not canonical UTC.
    TypeError
        If ``comment_ids`` is a bare string.
    """
    where, params = _visible_filter(scope, now, comment_ids)
    with _transaction(conn):  # one consistent snapshot across the three reads
        facts = _facts_by_mention(conn, where, params)
        mentions = _mentions_by_comment(conn, where, params, facts)
        sql = f"{_SELECT_VIEW_COMMENTS} WHERE {where} ORDER BY {_ORDER}"
        rows = conn.execute(sql, params).fetchall()
    return [_comment_view(row, mentions.get(str(row["comment_id"]), ())) for row in rows]


def summary_counts(conn: sqlite3.Connection, scope: ViewerScope, now: str) -> dict[str, int]:
    """Count visible sources, comments, extracted comments, mentions and facts.

    Parameters
    ----------
    conn
        Open connection.
    scope
        Viewer scope.
    now
        Canonical UTC current time.

    Returns
    -------
    dict[str, int]
        Keys :data:`SUMMARY_KEYS`. ``sources`` counts sources with at least one visible comment;
        ``parsed_comments`` counts visible comments with a stored extraction of any status.

    Raises
    ------
    ValueError
        If ``scope`` is unknown or ``now`` is not canonical UTC.
    """
    where, params = visibility_clause(scope, now)
    row = conn.execute(f"{_SELECT_SUMMARY} WHERE {where}", params).fetchone()
    return {key: int(row[key]) for key in SUMMARY_KEYS}


def _visible_filter(
    scope: ViewerScope, now: str, comment_ids: Sequence[str] | None
) -> tuple[str, list[str]]:
    where, params = visibility_clause(scope, now)
    if comment_ids is None:
        return where, params
    return f"{where} AND c.comment_id IN {_IDS_PARAM}", [*params, _json_ids(comment_ids)]


def _facts_by_mention(
    conn: sqlite3.Connection, where: str, params: Sequence[str]
) -> dict[str, tuple[FactView, ...]]:
    sql = f"{_SELECT_VIEW_FACTS} WHERE {where} ORDER BY f.mention_id, f.fact_index"
    grouped: defaultdict[str, list[FactView]] = defaultdict(list)
    for row in conn.execute(sql, params):
        grouped[str(row["mention_id"])].append(_fact_view(row))
    return {mention_id: tuple(facts) for mention_id, facts in grouped.items()}


def _mentions_by_comment(
    conn: sqlite3.Connection,
    where: str,
    params: Sequence[str],
    facts: Mapping[str, tuple[FactView, ...]],
) -> dict[str, tuple[MentionView, ...]]:
    sql = f"{_SELECT_VIEW_MENTIONS} WHERE {where} ORDER BY m.comment_id, m.mention_index"
    grouped: defaultdict[str, list[MentionView]] = defaultdict(list)
    for row in conn.execute(sql, params):
        mention_facts = facts.get(str(row["mention_id"]), ())
        grouped[str(row["comment_id"])].append(_mention_view(row, mention_facts))
    return {comment_id: tuple(mentions) for comment_id, mentions in grouped.items()}


def _fact_view(row: sqlite3.Row) -> FactView:
    return FactView(
        fact_id=row["fact_id"],
        key=row["key"],
        value_as_written=row["value_as_written"],
        normalized_value=row["normalized_value"],
        evidence_quote=row["evidence_quote"],
        applicability=row["applicability"],
        basis=row["basis"],
    )


def _mention_view(row: sqlite3.Row, facts: tuple[FactView, ...]) -> MentionView:
    return MentionView(
        mention_id=row["mention_id"],
        crop=row["crop"],
        variety_as_written=row["variety_as_written"],
        variety_key=row["variety_key"],
        topics=tuple(json.loads(row["topics_json"])),
        evidence_quote=row["evidence_quote"],
        extractor_version=row["extractor_version"],
        facts=facts,
    )


def _comment_view(row: sqlite3.Row, mentions: tuple[MentionView, ...]) -> CommentView:
    return CommentView(
        comment_id=row["comment_id"],
        source_id=row["source_id"],
        source_kind=row["source_kind"],
        rights_status=row["rights_status"],
        text=row["text"],
        permalink=row["permalink"],
        source_order=row["source_order"],
        created_at=row["created_at"],
        parse_status=row["parse_status"],
        mentions=mentions,
    )
