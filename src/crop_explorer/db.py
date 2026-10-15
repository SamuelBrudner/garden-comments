"""SQLite persistence, full-text search and scope-gated reads (SPEC §8, §9).

Every write runs in one savepoint: on any error it rolls back completely and re-raises. Every
viewer-facing read (ids, views, counts) joins ``comments c JOIN sources s`` and applies
:func:`crop_explorer.visibility.visibility_clause`, so private, expired or soft-deleted rows never
reach a viewer who may not see them. Comment bodies and evidence quotes are never logged nor put
into error messages; only ids and counts are.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

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
    "MEMORY_PATH",
    "SUMMARY_KEYS",
    "connect",
    "delete_source",
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

_SAVEPOINT: Final = "crop_explorer_tx"
_ORDER: Final = "c.source_id, c.source_order, c.comment_id"
_VISIBLE_FROM: Final = "comments c JOIN sources s ON s.source_id = c.source_id"
_IDS_PARAM: Final = "(SELECT value FROM json_each(?))"
_TOKEN: Final = re.compile(r"\w+")
_MAX_IDS_IN_MESSAGE: Final = 10

Outcome = Literal["inserted", "updated", "unchanged"]

# --------------------------------------------------------------------------- connection


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the database at ``path`` with the schema initialised.

    Parent directories are created. The connection has foreign keys enforced, rows addressable
    by column name (:class:`sqlite3.Row`) and SQLite-level autocommit; this module wraps each
    write in its own transaction.

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
        If SQLite cannot enforce foreign keys.
    """
    if path != MEMORY_PATH:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        _configure(conn)
        init_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables, indexes, the FTS index and its sync triggers if absent (idempotent).

    Parameters
    ----------
    conn
        Open connection.
    """
    with _transaction(conn):
        for statement in _SCHEMA:
            conn.execute(statement)


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite refused to enable foreign key enforcement")


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
    conn: sqlite3.Connection, source: SourceRecord, comments: Sequence[CommentRecord]
) -> ImportStats:
    """Make the stored comments of ``source`` equal ``comments`` (idempotent re-import).

    The ``sources`` row is upserted. New ids are inserted; ids whose text hash is unchanged keep
    their extraction (permalink, ``created_at`` and ``source_order`` are refreshed); ids whose
    text changed are updated and lose their extraction, because its quotes may no longer be
    substrings; stored ids absent from ``comments`` are deleted with everything derived from
    them. A soft-deleted comment stays soft-deleted.

    Parameters
    ----------
    conn
        Open connection.
    source
        Provenance and rights state of the dataset.
    comments
        The complete current contents of the source.

    Returns
    -------
    ImportStats
        Counts of inserted, updated, unchanged and deleted comments.

    Raises
    ------
    ValueError
        If ``comments`` repeats an id, an id already belongs to another source, or a timestamp
        is not canonical UTC. Nothing is written in that case.
    """
    _check_source(source)
    _check_records(comments)
    with _transaction(conn):
        _upsert_source(conn, source)
        existing = _stored_hashes(conn, source.source_id)
        new_ids = [record.comment_id for record in comments if record.comment_id not in existing]
        _check_collisions(conn, source.source_id, new_ids)
        outcomes = _apply_records(conn, source, comments, existing)
        absent = sorted(existing.keys() - {record.comment_id for record in comments})
        conn.executemany("DELETE FROM comments WHERE comment_id = ?", [(i,) for i in absent])
    stats = ImportStats(source_id=source.source_id, deleted=len(absent), **outcomes)
    logger.info(
        "Synced source {}: {} inserted, {} updated, {} unchanged, {} deleted",
        stats.source_id,
        stats.inserted,
        stats.updated,
        stats.unchanged,
        stats.deleted,
    )
    return stats


def _check_source(source: SourceRecord) -> None:
    require_utc_iso(source.imported_at, "imported_at")
    if source.expires_at is not None:
        require_utc_iso(source.expires_at, "expires_at")


def _check_records(records: Sequence[CommentRecord]) -> None:
    counts = Counter(record.comment_id for record in records)
    duplicates = sorted(comment_id for comment_id, n in counts.items() if n > 1)
    if duplicates:
        raise ValueError(f"Duplicate comment_id(s) within one import: {_id_list(duplicates)}")
    for record in records:
        if record.created_at is not None:
            require_utc_iso(record.created_at, f"created_at of comment {record.comment_id!r}")


def _upsert_source(conn: sqlite3.Connection, source: SourceRecord) -> None:
    conn.execute(
        "INSERT INTO sources (source_id, kind, source_url, rights_status, imported_at,"
        " expires_at, dataset_hash)"
        " VALUES (:source_id, :kind, :source_url, :rights_status, :imported_at,"
        " :expires_at, :dataset_hash)"
        " ON CONFLICT (source_id) DO UPDATE SET kind = excluded.kind,"
        " source_url = excluded.source_url, rights_status = excluded.rights_status,"
        " imported_at = excluded.imported_at, expires_at = excluded.expires_at,"
        " dataset_hash = excluded.dataset_hash",
        source.model_dump(),
    )


def _stored_hashes(conn: sqlite3.Connection, source_id: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT comment_id, text_hash FROM comments WHERE source_id = ?", (source_id,)
    )
    return {str(row["comment_id"]): str(row["text_hash"]) for row in rows}


def _check_collisions(conn: sqlite3.Connection, source_id: str, new_ids: Sequence[str]) -> None:
    rows = conn.execute(
        f"SELECT comment_id, source_id FROM comments WHERE comment_id IN {_IDS_PARAM}"
        " ORDER BY comment_id",
        (_json_ids(new_ids),),
    ).fetchall()
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


def _classify(record: CommentRecord, stored_hash: str | None) -> Outcome:
    if stored_hash is None:
        return "inserted"
    return "unchanged" if stored_hash == _text_hash(record.text) else "updated"


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _insert_comment(conn: sqlite3.Connection, source: SourceRecord, record: CommentRecord) -> None:
    conn.execute(
        "INSERT INTO comments (comment_id, source_id, text, permalink, source_order,"
        " created_at, imported_at, text_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
        "UPDATE comments SET text = ?, text_hash = ?, permalink = ?, source_order = ?,"
        " created_at = ?, imported_at = ? WHERE comment_id = ?",
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
        "UPDATE comments SET permalink = ?, source_order = ?, created_at = ? WHERE comment_id = ?",
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
        "INSERT INTO parses (comment_id, status, extractor_version, warnings_json, loaded_at)"
        " VALUES (?, ?, ?, ?, ?)",
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
    conn.execute(
        "INSERT INTO mentions (mention_id, comment_id, mention_index, crop, variety_as_written,"
        " variety_key, topics_json, evidence_quote, extractor_version, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
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
        ),
    )
    conn.executemany(
        "INSERT INTO environment_facts (fact_id, mention_id, fact_index, key, value_as_written,"
        " normalized_value, evidence_quote, applicability, basis)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (f"{mention_id}#f{j}", mention_id, j, *_fact_values(fact))
            for j, fact in enumerate(mention.facts)
        ],
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
    logger.info("Deleted source {} with {} comment(s)", source_id, removed)
    return removed


def _require_source(conn: sqlite3.Connection, source_id: str) -> None:
    row = conn.execute("SELECT 1 FROM sources WHERE source_id = ?", (source_id,)).fetchone()
    if row is None:
        raise KeyError(f"Unknown source_id {source_id!r}")


def purge_expired(conn: sqlite3.Connection, now: str) -> list[str]:
    """Delete every source whose ``expires_at`` is at or before ``now`` (with cascade).

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
        rows = conn.execute(
            "SELECT source_id FROM sources WHERE expires_at IS NOT NULL AND expires_at <= ?"
            " ORDER BY source_id",
            (now,),
        ).fetchall()
        expired = [str(row["source_id"]) for row in rows]
        conn.executemany("DELETE FROM sources WHERE source_id = ?", [(i,) for i in expired])
    logger.info("Purged {} expired source(s): {}", len(expired), expired)
    return expired


# --------------------------------------------------------------------------- pipeline reads


def stored_texts(conn: sqlite3.Connection, comment_ids: Sequence[str]) -> dict[str, str]:
    """Return the stored text of each known id (pipeline-internal; ignores viewer scope).

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
    rows = conn.execute(
        f"SELECT comment_id, text FROM comments WHERE comment_id IN {_IDS_PARAM}"
        " ORDER BY comment_id",
        (_json_ids(comment_ids),),
    )
    return {str(row["comment_id"]): str(row["text"]) for row in rows}


def iter_comments_for_extraction(
    conn: sqlite3.Connection, only_unparsed: bool, now: str
) -> list[tuple[str, str]]:
    """List ``(comment_id, text)`` to send to the offline extractor (pipeline-internal).

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
    list[tuple[str, str]]
        Comment ids with their exact stored text.

    Raises
    ------
    ValueError
        If ``now`` is not canonical UTC.
    """
    require_utc_iso(now, "now")
    rows = conn.execute(
        f"SELECT c.comment_id, c.text FROM {_VISIBLE_FROM}"
        " LEFT JOIN parses p ON p.comment_id = c.comment_id"
        " WHERE c.deleted_at IS NULL AND (s.expires_at IS NULL OR s.expires_at > ?)"
        " AND (? = 0 OR p.comment_id IS NULL)"
        f" ORDER BY {_ORDER}",
        (now, int(only_unparsed)),
    )
    return [(str(row["comment_id"]), str(row["text"])) for row in rows]


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
    """Turn free user text into a safe FTS5 query: every word a quoted prefix term, ANDed.

    FTS5 syntax in the input (quotes, ``*``, ``-``, ``AND``/``OR``/``NOT``/``NEAR``, column
    filters) is neutralised: only runs of Unicode word characters survive, each inside double
    quotes. Matching is case-insensitive, diacritic-insensitive and porter-stemmed.

    Parameters
    ----------
    text
        Raw search text.

    Returns
    -------
    str | None
        The MATCH expression, or ``None`` when ``text`` has no word characters (no text filter).
    """
    tokens = _TOKEN.findall(text)
    if not tokens:
        return None
    return " ".join(f'"{token}"*' for token in tokens)


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
        rows = conn.execute(
            "SELECT c.comment_id, c.source_id, s.kind AS source_kind, s.rights_status, c.text,"
            " c.permalink, c.source_order, c.created_at, p.status AS parse_status"
            f" FROM {_VISIBLE_FROM} LEFT JOIN parses p ON p.comment_id = c.comment_id"
            f" WHERE {where} ORDER BY {_ORDER}",
            params,
        ).fetchall()
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
    row = conn.execute(
        "SELECT COUNT(DISTINCT c.source_id) AS sources, COUNT(DISTINCT c.comment_id) AS comments,"
        " COUNT(DISTINCT p.comment_id) AS parsed_comments,"
        " COUNT(DISTINCT m.mention_id) AS mentions, COUNT(DISTINCT f.fact_id) AS facts"
        f" FROM {_VISIBLE_FROM}"
        " LEFT JOIN parses p ON p.comment_id = c.comment_id"
        " LEFT JOIN mentions m ON m.comment_id = c.comment_id"
        " LEFT JOIN environment_facts f ON f.mention_id = m.mention_id"
        f" WHERE {where}",
        params,
    ).fetchone()
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
    rows = conn.execute(
        "SELECT f.fact_id, f.mention_id, f.key, f.value_as_written, f.normalized_value,"
        " f.evidence_quote, f.applicability, f.basis FROM environment_facts f"
        " JOIN mentions m ON m.mention_id = f.mention_id"
        " JOIN comments c ON c.comment_id = m.comment_id"
        " JOIN sources s ON s.source_id = c.source_id"
        f" WHERE {where} ORDER BY f.mention_id, f.fact_index",
        params,
    )
    grouped: defaultdict[str, list[FactView]] = defaultdict(list)
    for row in rows:
        grouped[str(row["mention_id"])].append(_fact_view(row))
    return {mention_id: tuple(facts) for mention_id, facts in grouped.items()}


def _mentions_by_comment(
    conn: sqlite3.Connection,
    where: str,
    params: Sequence[str],
    facts: Mapping[str, tuple[FactView, ...]],
) -> dict[str, tuple[MentionView, ...]]:
    rows = conn.execute(
        "SELECT m.mention_id, m.comment_id, m.crop, m.variety_as_written, m.variety_key,"
        " m.topics_json, m.evidence_quote, m.extractor_version FROM mentions m"
        " JOIN comments c ON c.comment_id = m.comment_id"
        " JOIN sources s ON s.source_id = c.source_id"
        f" WHERE {where} ORDER BY m.comment_id, m.mention_index",
        params,
    )
    grouped: defaultdict[str, list[MentionView]] = defaultdict(list)
    for row in rows:
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
