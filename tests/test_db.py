"""Tests for SQLite persistence, FTS sync and scope-gated reads.

Covers SPEC §8 (storage, idempotent import, transactional re-extraction) and §11 #1 (search
preservation), #14 (idempotency and deletion) and #15 (rights isolation).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from crop_explorer.db import (
    connect,
    delete_source,
    fts_query,
    init_schema,
    iter_comments_for_extraction,
    load_comment_views,
    purge_expired,
    replace_extraction,
    stored_texts,
    summary_counts,
    sync_source_comments,
    visible_comment_ids,
)
from crop_explorer.schemas import (
    RIGHTS_STATUSES,
    CommentRecord,
    EnvKey,
    ImportStats,
    ParseStatus,
    RightsStatus,
    SourceRecord,
    Topic,
    ValidatedComment,
    ValidatedFact,
    ValidatedMention,
)
from crop_explorer.views import CommentView, FactView, MentionView

MEMORY = Path(":memory:")
PAST = "2026-01-01T00:00:00Z"
NOW = "2026-10-15T12:00:00Z"
FUTURE = "2027-01-01T00:00:00Z"
LOADED = "2026-10-14T08:00:00Z"

TEXT_C1 = "Sun Gold tomatoes in containers, zone 6b. Muggy summers here."
TEXT_C2 = "My peppers sulked in the greenhouse."
TEXT_C3 = "Terrific beans this year."

SCOPE_SEES: dict[str, frozenset[str]] = {
    "public": frozenset({"synthetic", "cleared"}),
    "private": frozenset(RIGHTS_STATUSES),
}
EXTRACTION_TABLES = ("parses", "mentions", "environment_facts")

# --------------------------------------------------------------------------- builders


def make_source(
    source_id: str = "garden-a",
    rights: RightsStatus = "synthetic",
    *,
    expires_at: str | None = None,
    imported_at: str = PAST,
    dataset_hash: str = "hash-1",
    kind: str = "comments_export",
    source_url: str | None = None,
) -> SourceRecord:
    return SourceRecord(
        source_id=source_id,
        kind=kind,
        source_url=source_url,
        rights_status=rights,
        imported_at=imported_at,
        expires_at=expires_at,
        dataset_hash=dataset_hash,
    )


def rec(
    comment_id: str,
    text: str,
    order: int = 0,
    *,
    permalink: str | None = None,
    created_at: str | None = None,
) -> CommentRecord:
    return CommentRecord(
        comment_id=comment_id,
        text=text,
        source_order=order,
        permalink=permalink,
        created_at=created_at,
    )


def vfact(
    key: EnvKey,
    written: str,
    normalized: str | None,
    quote: str,
    *,
    applicability: Any = "this_mention",
) -> ValidatedFact:
    return ValidatedFact(
        key=key,
        value_as_written=written,
        normalized_value=normalized,
        evidence_quote=quote,
        applicability=applicability,
        basis="reported_garden_context",
    )


def vmention(
    quote: str,
    *,
    crop: str | None = "tomato",
    variety: str | None = None,
    topics: tuple[Topic, ...] = ("flavor",),
    facts: tuple[ValidatedFact, ...] = (),
) -> ValidatedMention:
    return ValidatedMention(
        crop=crop,
        variety_as_written=variety,
        variety_key=None if variety is None else variety.casefold(),
        topics=topics,
        evidence_quote=quote,
        facts=facts,
    )


def validated(
    comment_id: str,
    mentions: tuple[ValidatedMention, ...] = (),
    *,
    status: ParseStatus = "parsed",
    version: str = "extractor-v1",
    warnings: tuple[str, ...] = (),
) -> ValidatedComment:
    return ValidatedComment(
        comment_id=comment_id,
        extractor_version=version,
        status=status,
        mentions=mentions,
        warnings=warnings,
        rejections=(),
    )


def c1_extraction(status: ParseStatus = "parsed") -> ValidatedComment:
    facts = (
        vfact("method", "containers", "container", "in containers"),
        vfact("zone", "zone 6b", "6b", "zone 6b", applicability="whole_comment"),
    )
    mention = vmention(
        "Sun Gold tomatoes in containers",
        variety="Sun Gold",
        topics=("flavor", "growing_method"),
        facts=facts,
    )
    return validated("c1", (mention,), status=status)


def c2_extraction() -> ValidatedComment:
    facts = (vfact("shelter", "greenhouse", "greenhouse", "in the greenhouse"),)
    return validated("c2", (vmention("My peppers", crop="pepper", facts=facts),))


def import_stats(inserted: int, updated: int, unchanged: int, deleted: int) -> ImportStats:
    return ImportStats(
        source_id="garden-a",
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        deleted=deleted,
    )


def three_records() -> list[CommentRecord]:
    return [rec("c1", TEXT_C1, 0), rec("c2", TEXT_C2, 1), rec("c3", TEXT_C3, 2)]


# --------------------------------------------------------------------------- helpers


def count(conn: sqlite3.Connection, table: str, comment_id: str | None = None) -> int:
    if comment_id is None:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    if table == "environment_facts":
        sql = "SELECT COUNT(*) FROM environment_facts WHERE mention_id LIKE ? || '#m%'"
    else:
        sql = f"SELECT COUNT(*) FROM {table} WHERE comment_id = ?"
    return int(conn.execute(sql, (comment_id,)).fetchone()[0])


def fts_rowids(conn: sqlite3.Connection, word: str) -> list[int]:
    """Query the FTS index directly (no join), so stale index entries would show up."""
    rows = conn.execute(
        "SELECT rowid FROM comments_fts WHERE comments_fts MATCH ?", (fts_query(word),)
    )
    return [row[0] for row in rows]


def assert_fts_in_sync(conn: sqlite3.Connection) -> None:
    """FTS5 raises 'database disk image is malformed' if the index disagrees with comments."""
    conn.execute("INSERT INTO comments_fts(comments_fts, rank) VALUES ('integrity-check', 1)")


def snapshot(conn: sqlite3.Connection) -> list[str]:
    return list(conn.iterdump())


def extraction_rows(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for table in EXTRACTION_TABLES:
        rows.extend(tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1"))
    return rows


def mark_deleted(conn: sqlite3.Connection, comment_id: str) -> None:
    conn.execute("UPDATE comments SET deleted_at = ? WHERE comment_id = ?", (PAST, comment_id))


def view_ids(views: Sequence[CommentView]) -> list[str]:
    return [view.comment_id for view in views]


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect(MEMORY)
    yield connection
    connection.close()


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Synthetic source ``garden-a`` with c1 (extracted), c2 and c3 (never extracted)."""
    sync_source_comments(conn, make_source(), three_records())
    replace_extraction(conn, c1_extraction(), LOADED)
    return conn


@pytest.fixture
def mixed(conn: sqlite3.Connection) -> sqlite3.Connection:
    """One cleared, one synthetic and one private_unverified source, all extracted."""
    sync_source_comments(
        conn, make_source("demo", "synthetic"), [rec("d1", "Sun Gold tomatoes in pots.")]
    )
    sync_source_comments(
        conn, make_source("cleared-src", "cleared"), [rec("k1", "Sun Gold tomatoes, sweet.")]
    )
    sync_source_comments(
        conn,
        make_source("mine", "private_unverified"),
        [rec("p1", "Sun Gold tomatoes, secret plot."), rec("p2", "Secret beans.", 1)],
    )
    for comment_id, quote in (("d1", "in pots"), ("k1", "sweet"), ("p1", "secret plot")):
        fact = vfact("method", quote, None, quote)
        replace_extraction(conn, validated(comment_id, (vmention(quote, facts=(fact,)),)), LOADED)
    return conn


# --------------------------------------------------------------------------- connect / schema


def test_connect_creates_parent_directories_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deeper" / "explorer.sqlite"
    first = connect(path)
    sync_source_comments(first, make_source(), [rec("c1", TEXT_C1)])
    first.close()
    assert path.is_file()
    second = connect(path)
    assert visible_comment_ids(second, "public", NOW, "tomatoes") == ["c1"]
    second.close()


def test_connection_enforces_foreign_keys(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO comments (comment_id, source_id, text, source_order, imported_at,"
            " text_hash) VALUES ('x', 'no-such-source', 't', 0, ?, 'h')",
            (NOW,),
        )


def test_rows_are_addressable_by_column_name(conn: sqlite3.Connection) -> None:
    assert conn.execute("SELECT 7 AS seven").fetchone()["seven"] == 7


def test_init_schema_is_idempotent_and_preserves_data(seeded: sqlite3.Connection) -> None:
    schema_sql = "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    schema_before = [tuple(row) for row in seeded.execute(schema_sql)]
    data_before = snapshot(seeded)
    init_schema(seeded)
    init_schema(seeded)
    assert [tuple(row) for row in seeded.execute(schema_sql)] == schema_before
    assert snapshot(seeded) == data_before
    expected = {"sources", "comments", "parses", "mentions", "environment_facts", "comments_fts"}
    assert expected <= {row[1] for row in schema_before}


# --------------------------------------------------------------------------- sync_source_comments


def test_sync_inserts_new_comments(conn: sqlite3.Connection) -> None:
    stats = sync_source_comments(conn, make_source(), three_records())
    assert stats == import_stats(3, 0, 0, 0)
    row = conn.execute("SELECT * FROM comments WHERE comment_id = 'c1'").fetchone()
    assert row["source_id"] == "garden-a"
    assert row["text"] == TEXT_C1
    assert row["text_hash"] == hashlib.sha256(TEXT_C1.encode("utf-8")).hexdigest()
    assert row["imported_at"] == PAST
    assert row["deleted_at"] is None
    assert visible_comment_ids(conn, "public", NOW, "greenhouse") == ["c2"]
    assert_fts_in_sync(conn)


def test_sync_stores_text_exactly_as_given(conn: sqlite3.Connection) -> None:
    raw = "  Leading spaces,\r\ntrailing tab\t and \u201ccurly\u201d \u2019 e\u0301 \u00e9 "
    sync_source_comments(conn, make_source(), [rec("c1", raw)])
    assert stored_texts(conn, ["c1"]) == {"c1": raw}


def test_sync_unchanged_text_keeps_extraction_and_refreshes_metadata(
    seeded: sqlite3.Connection,
) -> None:
    extraction_before = extraction_rows(seeded)
    records = [
        rec("c1", TEXT_C1, 7, permalink="https://example.org/c1", created_at=PAST),
        rec("c2", TEXT_C2, 1),
        rec("c3", TEXT_C3, 2),
    ]
    stats = sync_source_comments(seeded, make_source(dataset_hash="hash-2"), records)
    assert stats == import_stats(0, 0, 3, 0)
    assert extraction_rows(seeded) == extraction_before
    [view] = load_comment_views(seeded, "public", NOW, ["c1"])
    assert (view.permalink, view.created_at, view.source_order) == (
        "https://example.org/c1",
        PAST,
        7,
    )
    assert view.parse_status == "parsed"
    assert len(view.mentions) == 1


def test_sync_changed_text_updates_comment_drops_its_extraction_and_reindexes(
    seeded: sqlite3.Connection,
) -> None:
    replace_extraction(seeded, c2_extraction(), LOADED)
    new_text = "Cherokee Purple cracked badly."
    records = [rec("c1", new_text, 0), rec("c2", TEXT_C2, 1), rec("c3", TEXT_C3, 2)]
    stats = sync_source_comments(seeded, make_source(dataset_hash="hash-2"), records)
    assert stats == import_stats(0, 1, 2, 0)
    assert stored_texts(seeded, ["c1"]) == {"c1": new_text}
    for table in EXTRACTION_TABLES:
        assert count(seeded, table, "c1") == 0
        assert count(seeded, table, "c2") == 1
    [view] = load_comment_views(seeded, "private", NOW, ["c1"])
    assert (view.parse_status, view.mentions) == (None, ())
    assert fts_rowids(seeded, "muggy") == []
    assert visible_comment_ids(seeded, "public", NOW, "muggy") == []
    assert visible_comment_ids(seeded, "public", NOW, "cherokee") == ["c1"]
    assert_fts_in_sync(seeded)


def test_sync_deletes_comments_missing_from_reimport(seeded: sqlite3.Connection) -> None:
    stats = sync_source_comments(seeded, make_source(), [rec("c2", TEXT_C2, 0)])
    assert stats == import_stats(0, 0, 1, 2)
    assert visible_comment_ids(seeded, "private", NOW, None) == ["c2"]
    for table in EXTRACTION_TABLES:
        assert count(seeded, table) == 0
    assert fts_rowids(seeded, "tomatoes") == []
    assert_fts_in_sync(seeded)


def test_sync_mixed_insert_update_unchanged_delete(seeded: sqlite3.Connection) -> None:
    records = [rec("c1", TEXT_C1, 0), rec("c2", "Rewritten peppers.", 1), rec("c4", "New!", 2)]
    stats = sync_source_comments(seeded, make_source(), records)
    assert stats == import_stats(1, 1, 1, 1)
    assert visible_comment_ids(seeded, "public", NOW, None) == ["c1", "c2", "c4"]


def test_reimporting_identical_data_never_duplicates(seeded: sqlite3.Connection) -> None:
    """SPEC §11 #14: re-import and re-extraction are idempotent."""
    tables = ("sources", "comments", *EXTRACTION_TABLES)
    counts_before = {table: count(seeded, table) for table in tables}
    for _ in range(2):
        stats = sync_source_comments(seeded, make_source(), three_records())
        replace_extraction(seeded, c1_extraction(), LOADED)
    assert stats == import_stats(0, 0, 3, 0)
    assert {table: count(seeded, table) for table in tables} == counts_before
    assert visible_comment_ids(seeded, "public", NOW, "tomatoes") == ["c1"]
    assert len(fts_rowids(seeded, "tomatoes")) == 1


def test_sync_upserts_source_metadata(seeded: sqlite3.Connection) -> None:
    updated = make_source(
        rights="cleared",
        expires_at=FUTURE,
        imported_at=NOW,
        dataset_hash="hash-2",
        kind="survey",
        source_url="https://example.org/export",
    )
    sync_source_comments(seeded, updated, three_records())
    rows = seeded.execute("SELECT * FROM sources").fetchall()
    assert [dict(row) for row in rows] == [
        {
            "source_id": "garden-a",
            "kind": "survey",
            "source_url": "https://example.org/export",
            "rights_status": "cleared",
            "imported_at": NOW,
            "expires_at": FUTURE,
            "dataset_hash": "hash-2",
        }
    ]


def test_cross_source_collision_raises_and_leaves_db_unchanged(
    seeded: sqlite3.Connection,
) -> None:
    before = snapshot(seeded)
    other = make_source("garden-b", "private_unverified", dataset_hash="other")
    with pytest.raises(ValueError, match=r"c2") as excinfo:
        sync_source_comments(seeded, other, [rec("fresh", "Brand new."), rec("c2", TEXT_C2, 1)])
    assert "garden-a" in str(excinfo.value)
    assert TEXT_C2 not in str(excinfo.value)
    assert snapshot(seeded) == before


def test_collision_during_resync_rolls_back_source_upsert(seeded: sqlite3.Connection) -> None:
    sync_source_comments(seeded, make_source("garden-b"), [rec("b1", "Beans in garden b.")])
    before = snapshot(seeded)
    changed_source = make_source(rights="cleared", dataset_hash="hash-2", imported_at=NOW)
    records = [rec("c1", "Changed text.", 0), rec("b1", "Stolen id.", 1)]
    with pytest.raises(ValueError, match=r"b1"):
        sync_source_comments(seeded, changed_source, records)
    assert snapshot(seeded) == before


def test_duplicate_ids_within_one_import_raise(conn: sqlite3.Connection) -> None:
    before = snapshot(conn)
    with pytest.raises(ValueError, match=r"dup"):
        sync_source_comments(conn, make_source(), [rec("dup", "one"), rec("dup", "two", 1)])
    assert snapshot(conn) == before


@pytest.mark.parametrize(
    ("field", "source", "records"),
    [
        ("imported_at", make_source(imported_at="2026-10-15"), [rec("c1", "x")]),
        ("expires_at", make_source(expires_at="tomorrow"), [rec("c1", "x")]),
        ("created_at", make_source(), [rec("c1", "x", created_at="2026-10-15 12:00")]),
    ],
)
def test_sync_rejects_non_canonical_timestamps(
    conn: sqlite3.Connection, field: str, source: SourceRecord, records: list[CommentRecord]
) -> None:
    with pytest.raises(ValueError, match=field):
        sync_source_comments(conn, source, records)
    assert count(conn, "sources") == 0


# --------------------------------------------------------------------------- replace_extraction


def test_replace_extraction_stores_rows_with_deterministic_ids(conn: sqlite3.Connection) -> None:
    text = "Sun Gold tomatoes in containers and peppers in the greenhouse."
    sync_source_comments(conn, make_source(), [rec("c1", text)])
    tomato_facts = (vfact("method", "containers", "container", "in containers"),)
    pepper_facts = (
        vfact("shelter", "greenhouse", "greenhouse", "in the greenhouse"),
        vfact("soil", "greenhouse", None, "greenhouse"),
    )
    extraction = validated(
        "c1",
        (
            vmention("Sun Gold tomatoes in containers", facts=tomato_facts),
            vmention("peppers in the greenhouse", crop="pepper", facts=pepper_facts),
        ),
        warnings=("w1",),
    )
    replace_extraction(conn, extraction, LOADED)
    mentions = conn.execute("SELECT mention_id, mention_index, status FROM mentions ORDER BY 1")
    assert [tuple(r) for r in mentions] == [("c1#m0", 0, "parsed"), ("c1#m1", 1, "parsed")]
    facts = conn.execute(
        "SELECT fact_id, mention_id, fact_index FROM environment_facts ORDER BY 1"
    )
    assert [tuple(r) for r in facts] == [
        ("c1#m0#f0", "c1#m0", 0),
        ("c1#m1#f0", "c1#m1", 0),
        ("c1#m1#f1", "c1#m1", 1),
    ]
    parse = conn.execute("SELECT * FROM parses").fetchone()
    assert dict(parse) == {
        "comment_id": "c1",
        "status": "parsed",
        "extractor_version": "extractor-v1",
        "warnings_json": '["w1"]',
        "loaded_at": LOADED,
    }


def test_replace_extraction_is_idempotent(seeded: sqlite3.Connection) -> None:
    first = extraction_rows(seeded)
    replace_extraction(seeded, c1_extraction(), LOADED)
    replace_extraction(seeded, c1_extraction(), LOADED)
    assert extraction_rows(seeded) == first


def test_replace_extraction_replaces_previous_mentions_and_facts(
    seeded: sqlite3.Connection,
) -> None:
    replacement = validated("c1", (vmention("Muggy summers", crop=None),), status="partial")
    replace_extraction(seeded, replacement, NOW)
    [view] = load_comment_views(seeded, "public", NOW, ["c1"])
    assert view.parse_status == "partial"
    assert [(m.mention_id, m.crop, m.facts) for m in view.mentions] == [("c1#m0", None, ())]
    assert count(seeded, "environment_facts") == 0
    assert count(seeded, "parses") == 1


def test_replace_extraction_with_zero_mentions_records_status(seeded: sqlite3.Connection) -> None:
    replace_extraction(seeded, validated("c3", (), status="unresolved"), LOADED)
    [view] = load_comment_views(seeded, "public", NOW, ["c3"])
    assert (view.parse_status, view.mentions) == ("unresolved", ())


def test_replace_extraction_unknown_comment_raises_key_error(seeded: sqlite3.Connection) -> None:
    before = snapshot(seeded)
    with pytest.raises(KeyError, match="nope"):
        replace_extraction(seeded, validated("nope"), LOADED)
    assert snapshot(seeded) == before


@pytest.mark.parametrize(
    "bad",
    [
        validated("c1", (vmention("Sun Gold tomatoes in a field"),)),
        validated("c1", (vmention("Sun Gold", facts=(vfact("soil", "clay", "clay", "clay"),)),)),
        validated("c1", (vmention("sun gold tomatoes"),)),  # case differs: not an exact substring
    ],
)
def test_replace_extraction_rejects_quotes_not_in_stored_text(
    seeded: sqlite3.Connection, bad: ValidatedComment
) -> None:
    before = snapshot(seeded)
    with pytest.raises(ValueError, match="c1") as excinfo:
        replace_extraction(seeded, bad, NOW)
    assert "field" not in str(excinfo.value)
    assert snapshot(seeded) == before


def test_replace_extraction_rejects_non_canonical_loaded_at(seeded: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="loaded_at"):
        replace_extraction(seeded, c1_extraction(), "2026-10-15")


# --------------------------------------------------------------------------- deletion and expiry


def test_delete_source_cascades_to_every_derived_record(conn: sqlite3.Connection) -> None:
    """SPEC §11 #14: deleting a source removes it from search, extraction tables and FTS."""
    sync_source_comments(conn, make_source("garden-a"), three_records())
    replace_extraction(conn, c1_extraction(), LOADED)
    replace_extraction(conn, c2_extraction(), LOADED)
    sync_source_comments(conn, make_source("garden-b"), [rec("b1", "Bush beans in clay.")])
    b1_facts = (vfact("soil", "clay", "clay", "in clay"),)
    replace_extraction(conn, validated("b1", (vmention("Bush beans", facts=b1_facts),)), LOADED)

    assert delete_source(conn, "garden-a") == 3

    assert {table: count(conn, table) for table in ("sources", "comments")} == {
        "sources": 1,
        "comments": 1,
    }
    for table in EXTRACTION_TABLES:
        assert count(conn, table) == 1
        assert count(conn, table, "b1") == 1
    for word in ("tomatoes", "greenhouse", "terrific"):
        assert fts_rowids(conn, word) == []
        assert visible_comment_ids(conn, "private", NOW, word) == []
    assert visible_comment_ids(conn, "private", NOW, "beans") == ["b1"]
    assert summary_counts(conn, "private", NOW)["sources"] == 1
    assert_fts_in_sync(conn)


def test_delete_unknown_source_raises_key_error(seeded: sqlite3.Connection) -> None:
    before = snapshot(seeded)
    with pytest.raises(KeyError, match="nope"):
        delete_source(seeded, "nope")
    assert snapshot(seeded) == before


def test_delete_source_without_comments_returns_zero(conn: sqlite3.Connection) -> None:
    sync_source_comments(conn, make_source("empty"), [])
    assert delete_source(conn, "empty") == 0
    assert count(conn, "sources") == 0


def test_purge_expired_deletes_sources_expiring_at_or_before_now(
    conn: sqlite3.Connection,
) -> None:
    expiries = {"old": PAST, "edge": NOW, "later": FUTURE, "forever": None}
    for source_id, expires_at in expiries.items():
        records = [rec(f"{source_id}-1", f"Tomatoes from {source_id}orchard.")]
        sync_source_comments(conn, make_source(source_id, expires_at=expires_at), records)
    replace_extraction(conn, validated("old-1", (vmention("Tomatoes"),)), LOADED)

    assert purge_expired(conn, NOW) == ["edge", "old"]

    remaining = [row[0] for row in conn.execute("SELECT source_id FROM sources ORDER BY 1")]
    assert remaining == ["forever", "later"]
    assert count(conn, "mentions") == 0
    assert fts_rowids(conn, "oldorchard") == []
    assert fts_rowids(conn, "edgeorchard") == []
    assert_fts_in_sync(conn)
    assert purge_expired(conn, NOW) == []


def test_purge_expired_rejects_non_canonical_now(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="now"):
        purge_expired(conn, "2026-10-15")


# --------------------------------------------------------------------------- pipeline-internal


def test_stored_texts_returns_known_ids_regardless_of_scope(mixed: sqlite3.Connection) -> None:
    texts = stored_texts(mixed, ["p1", "d1", "missing"])
    assert texts == {"d1": "Sun Gold tomatoes in pots.", "p1": "Sun Gold tomatoes, secret plot."}
    assert stored_texts(mixed, []) == {}


def test_id_sequences_must_not_be_bare_strings(mixed: sqlite3.Connection) -> None:
    with pytest.raises(TypeError):
        stored_texts(mixed, "d1")
    with pytest.raises(TypeError):
        load_comment_views(mixed, "public", NOW, "d1")


def test_iter_comments_for_extraction(mixed: sqlite3.Connection) -> None:
    every = iter_comments_for_extraction(mixed, only_unparsed=False)
    assert [comment_id for comment_id, _ in every] == ["k1", "d1", "p1", "p2"]
    assert dict(every)["p2"] == "Secret beans."
    assert iter_comments_for_extraction(mixed, only_unparsed=True) == [("p2", "Secret beans.")]
    mark_deleted(mixed, "p2")
    assert iter_comments_for_extraction(mixed, only_unparsed=True) == []


# --------------------------------------------------------------------------- fts_query


@pytest.mark.parametrize("text", ["", "   ", "!!!", "--- *** ---", '"', "()", "…", "'", "\n\t"])
def test_fts_query_without_word_characters_is_none(text: str) -> None:
    assert fts_query(text) is None


@given(
    text=st.text(
        alphabet=st.characters(blacklist_categories=("L", "N", "Cs"), blacklist_characters="_")
    )
)
def test_fts_query_is_none_for_any_punctuation_only_text(text: str) -> None:
    assume(re.search(r"\w", text) is None)
    assert fts_query(text) is None


def test_fts_query_quotes_each_token_as_a_prefix_term() -> None:
    assert fts_query("Sun Gold!") == '"Sun"* "Gold"*'
    assert fts_query('tomato" OR text:foo*') == '"tomato"* "OR"* "text"* "foo"*'


@given(text=st.text())
def test_fts_query_output_contains_only_quoted_prefix_terms(text: str) -> None:
    query = fts_query(text)
    if query is not None:
        assert re.fullmatch(r'"\w+"\*( "\w+"\*)*', query)
        assert re.findall(r'"(\w+)"', query) == re.findall(r"\w+", text)


@pytest.fixture(scope="module")
def search_db() -> Iterator[sqlite3.Connection]:
    connection = connect(MEMORY)
    records = [
        rec("c1", TEXT_C1, 0),
        rec("c2", TEXT_C2, 1),
        rec("c3", TEXT_C3, 2),
        rec("c4", "Jalapeño plants loved the heat.", 3),
    ]
    sync_source_comments(connection, make_source(), records)
    yield connection
    connection.close()


@settings(max_examples=300)
@given(text=st.text())
def test_any_user_text_is_safe_to_match(search_db: sqlite3.Connection, text: str) -> None:
    hits = visible_comment_ids(search_db, "public", NOW, text)
    assert set(hits) <= {"c1", "c2", "c3", "c4"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('"tomatoes', ["c1"]),  # unbalanced quote
        ('tomatoes"', ["c1"]),
        ("tomat*", ["c1"]),
        ("*", ["c1", "c2", "c3", "c4"]),  # no word characters: no text filter
        ("-tomatoes", ["c1"]),  # not interpreted as NOT
        ("tomatoes OR beans", []),  # OR is a literal token, terms are ANDed
        ("tomatoes AND", []),
        ("NEAR(sun gold)", []),
        ("NOT beans", []),
        ("text:tomatoes", []),  # not a column filter
        ("{text}: beans", []),
        ("^terrific", ["c3"]),
        ("beans)", ["c3"]),
        ("sun + gold", ["c1"]),
    ],
)
def test_fts_operators_in_user_text_are_neutralised(
    search_db: sqlite3.Connection, text: str, expected: list[str]
) -> None:
    assert visible_comment_ids(search_db, "public", NOW, text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("tom", ["c1"]),  # prefix
        ("Sun Go", ["c1"]),
        ("contain", ["c1"]),
        ("tomato", ["c1"]),  # porter stemming: tomato finds tomatoes
        ("summer", ["c1"]),
        ("pepper", ["c2"]),
        ("TERRIFIC", ["c3"]),  # case-insensitive
        ("jalapeno", ["c4"]),  # diacritics removed
        ("jalapeño plant", ["c4"]),
        ("sun beans", []),  # implicit AND across comments
    ],
)
def test_fts_prefix_stemming_and_folding(
    search_db: sqlite3.Connection, text: str, expected: list[str]
) -> None:
    assert visible_comment_ids(search_db, "public", NOW, text) == expected


# --------------------------------------------------------------------------- visible_comment_ids


def test_visible_ids_order_by_source_then_source_order_then_id(conn: sqlite3.Connection) -> None:
    sync_source_comments(
        conn, make_source("zeta"), [rec("z-late", "tomato", 5), rec("z-early", "tomato", 1)]
    )
    sync_source_comments(
        conn,
        make_source("alpha"),
        [rec("a2", "tomato", 0), rec("a1", "tomato", 0), rec("a0", "bean", 3)],
    )
    assert visible_comment_ids(conn, "public", NOW, None) == [
        "a1",
        "a2",
        "a0",
        "z-early",
        "z-late",
    ]
    assert visible_comment_ids(conn, "public", NOW, "tomato") == ["a1", "a2", "z-early", "z-late"]
    assert visible_comment_ids(conn, "public", NOW, "...") == visible_comment_ids(
        conn, "public", NOW, None
    )
    shuffled = ["z-late", "a0", "a1", "a1", "missing", "z-early", "a2"]
    assert view_ids(load_comment_views(conn, "public", NOW, shuffled)) == [
        "a1",
        "a2",
        "a0",
        "z-early",
        "z-late",
    ]


def test_text_search_finds_comments_without_extraction(conn: sqlite3.Connection) -> None:
    """SPEC §11 #1: raw keyword matches work for unextracted and unresolved comments."""
    records = [
        rec("c1", "Brandywine split after rain.", 0),
        rec("c2", "Brandywine, never again.", 1),
        rec("c3", "Peas.", 2),
    ]
    sync_source_comments(conn, make_source(), records)
    replace_extraction(conn, validated("c2", (), status="unresolved"), LOADED)
    assert visible_comment_ids(conn, "public", NOW, "brandywine") == ["c1", "c2"]
    views = load_comment_views(conn, "public", NOW, ["c1", "c2"])
    assert [(v.comment_id, v.parse_status, v.mentions) for v in views] == [
        ("c1", None, ()),
        ("c2", "unresolved", ()),
    ]


# --------------------------------------------------------------------------- scope gating


def test_public_scope_never_returns_private_rows(mixed: sqlite3.Connection) -> None:
    assert visible_comment_ids(mixed, "public", NOW, None) == ["k1", "d1"]
    assert visible_comment_ids(mixed, "public", NOW, "secret") == []
    assert visible_comment_ids(mixed, "public", NOW, "tomatoes") == ["k1", "d1"]
    assert load_comment_views(mixed, "public", NOW, ["p1", "p2"]) == []
    public_views = load_comment_views(mixed, "public", NOW, None)
    assert view_ids(public_views) == ["k1", "d1"]
    assert {view.rights_status for view in public_views} == {"cleared", "synthetic"}
    assert summary_counts(mixed, "public", NOW) == {
        "sources": 2,
        "comments": 2,
        "parsed_comments": 2,
        "mentions": 2,
        "facts": 2,
    }


def test_private_scope_sees_every_rights_status(mixed: sqlite3.Connection) -> None:
    assert visible_comment_ids(mixed, "private", NOW, None) == ["k1", "d1", "p1", "p2"]
    assert visible_comment_ids(mixed, "private", NOW, "secret") == ["p1", "p2"]
    assert view_ids(load_comment_views(mixed, "private", NOW, ["p2", "p1"])) == ["p1", "p2"]
    assert summary_counts(mixed, "private", NOW) == {
        "sources": 3,
        "comments": 4,
        "parsed_comments": 3,
        "mentions": 3,
        "facts": 3,
    }


@pytest.mark.parametrize("scope", ["public", "private"])
def test_expired_sources_are_invisible_before_purge(conn: sqlite3.Connection, scope: Any) -> None:
    for source_id, expires_at in (("gone", PAST), ("edge", NOW), ("live", FUTURE)):
        records = [rec(f"{source_id}-1", "Tomatoes everywhere.")]
        sync_source_comments(
            conn, make_source(source_id, "cleared", expires_at=expires_at), records
        )
        replace_extraction(conn, validated(f"{source_id}-1", (vmention("Tomatoes"),)), LOADED)
    assert visible_comment_ids(conn, scope, NOW, None) == ["live-1"]
    assert visible_comment_ids(conn, scope, NOW, "tomatoes") == ["live-1"]
    assert view_ids(load_comment_views(conn, scope, NOW, ["gone-1", "edge-1", "live-1"])) == [
        "live-1"
    ]
    assert summary_counts(conn, scope, NOW) == {
        "sources": 1,
        "comments": 1,
        "parsed_comments": 1,
        "mentions": 1,
        "facts": 0,
    }
    assert visible_comment_ids(conn, scope, PAST, None) == ["edge-1", "live-1"]


@pytest.mark.parametrize("scope", ["public", "private"])
def test_soft_deleted_comments_are_invisible(seeded: sqlite3.Connection, scope: Any) -> None:
    mark_deleted(seeded, "c1")
    assert visible_comment_ids(seeded, scope, NOW, None) == ["c2", "c3"]
    assert visible_comment_ids(seeded, scope, NOW, "tomatoes") == []
    assert load_comment_views(seeded, scope, NOW, ["c1"]) == []
    counts = summary_counts(seeded, scope, NOW)
    assert (counts["comments"], counts["parsed_comments"], counts["mentions"]) == (2, 0, 0)


def test_soft_deletion_survives_reimport(seeded: sqlite3.Connection) -> None:
    mark_deleted(seeded, "c1")
    sync_source_comments(seeded, make_source(), three_records())
    assert visible_comment_ids(seeded, "private", NOW, None) == ["c2", "c3"]


source_specs = st.lists(
    st.tuples(
        st.sampled_from(RIGHTS_STATUSES),
        st.sampled_from([None, PAST, NOW, FUTURE]),
        st.lists(st.booleans(), min_size=1, max_size=3),  # one soft-deleted flag per comment
    ),
    min_size=1,
    max_size=4,
)


@settings(max_examples=40, deadline=None)
@given(specs=source_specs, scope=st.sampled_from(["public", "private"]))
def test_every_viewer_read_agrees_with_the_rights_oracle(
    specs: list[tuple[RightsStatus, str | None, list[bool]]], scope: Any
) -> None:
    conn = connect(MEMORY)
    expected: list[str] = []
    for i, (rights, expires_at, deleted_flags) in enumerate(specs):
        ids = [f"s{i}-c{j}" for j in range(len(deleted_flags))]
        source = make_source(f"s{i}", rights, expires_at=expires_at)
        sync_source_comments(
            conn, source, [rec(cid, "Okra pods.", j) for j, cid in enumerate(ids)]
        )
        for cid, deleted in zip(ids, deleted_flags, strict=True):
            replace_extraction(conn, validated(cid, (vmention("Okra"),)), LOADED)
            if deleted:
                mark_deleted(conn, cid)
            elif rights in SCOPE_SEES[scope] and (expires_at is None or expires_at > NOW):
                expected.append(cid)
    every_id = [row[0] for row in conn.execute("SELECT comment_id FROM comments")]
    assert visible_comment_ids(conn, scope, NOW, None) == expected
    assert visible_comment_ids(conn, scope, NOW, "okra") == expected
    assert view_ids(load_comment_views(conn, scope, NOW, every_id)) == expected
    assert view_ids(load_comment_views(conn, scope, NOW, None)) == expected
    counts = summary_counts(conn, scope, NOW)
    assert (counts["comments"], counts["mentions"]) == (len(expected), len(expected))
    assert counts["sources"] == len({cid.split("-")[0] for cid in expected})
    conn.close()


# --------------------------------------------------------------------------- load_comment_views


def test_views_carry_provenance_and_extraction(conn: sqlite3.Connection) -> None:
    source = make_source("demo", "synthetic", kind="synthetic_fixture")
    record = rec(
        "c1", TEXT_C1, 4, permalink="https://example.org/c1", created_at="2025-07-01T00:00:00Z"
    )
    sync_source_comments(conn, source, [record])
    replace_extraction(conn, c1_extraction(status="partial"), LOADED)
    assert load_comment_views(conn, "public", NOW, None) == [
        CommentView(
            comment_id="c1",
            source_id="demo",
            source_kind="synthetic_fixture",
            rights_status="synthetic",
            text=TEXT_C1,
            permalink="https://example.org/c1",
            source_order=4,
            created_at="2025-07-01T00:00:00Z",
            parse_status="partial",
            mentions=(
                MentionView(
                    mention_id="c1#m0",
                    crop="tomato",
                    variety_as_written="Sun Gold",
                    variety_key="sun gold",
                    topics=("flavor", "growing_method"),
                    evidence_quote="Sun Gold tomatoes in containers",
                    extractor_version="extractor-v1",
                    facts=(
                        FactView(
                            fact_id="c1#m0#f0",
                            key="method",
                            value_as_written="containers",
                            normalized_value="container",
                            evidence_quote="in containers",
                            applicability="this_mention",
                            basis="reported_garden_context",
                        ),
                        FactView(
                            fact_id="c1#m0#f1",
                            key="zone",
                            value_as_written="zone 6b",
                            normalized_value="6b",
                            evidence_quote="zone 6b",
                            applicability="whole_comment",
                            basis="reported_garden_context",
                        ),
                    ),
                ),
            ),
        )
    ]


def test_views_order_mentions_and_facts_by_index_not_by_id(conn: sqlite3.Connection) -> None:
    """With 12 entries, ``#m10`` sorts before ``#m2`` as text; index order must win."""
    words = [f"word{i}" for i in range(12)]
    sync_source_comments(conn, make_source(), [rec("c1", " ".join(words))])
    facts = tuple(vfact("soil", word, None, word) for word in words)
    mentions = tuple(
        vmention(word, crop=f"crop{i}", facts=facts if i == 0 else ())
        for i, word in enumerate(words)
    )
    replace_extraction(conn, validated("c1", mentions), LOADED)
    [view] = load_comment_views(conn, "public", NOW, None)
    assert [m.crop for m in view.mentions] == [f"crop{i}" for i in range(12)]
    assert [m.mention_id for m in view.mentions] == [f"c1#m{i}" for i in range(12)]
    assert [f.fact_id for f in view.mentions[0].facts] == [f"c1#m0#f{j}" for j in range(12)]
    assert [f.value_as_written for f in view.mentions[0].facts] == words


def test_views_for_empty_id_list_are_empty(seeded: sqlite3.Connection) -> None:
    assert load_comment_views(seeded, "private", NOW, []) == []


def test_viewer_reads_reject_unknown_scope(seeded: sqlite3.Connection) -> None:
    bad: Any = "admin"
    with pytest.raises(ValueError, match="scope"):
        visible_comment_ids(seeded, bad, NOW, None)
    with pytest.raises(ValueError, match="scope"):
        load_comment_views(seeded, bad, NOW, None)
    with pytest.raises(ValueError, match="scope"):
        summary_counts(seeded, bad, NOW)


# --------------------------------------------------------------------------- summary_counts


def test_summary_counts_count_distinct_comments_not_mentions(seeded: sqlite3.Connection) -> None:
    """SPEC §11 #12: multiple mentions in one comment do not inflate comment counts."""
    replace_extraction(seeded, c2_extraction(), LOADED)
    assert summary_counts(seeded, "public", NOW) == {
        "sources": 1,
        "comments": 3,
        "parsed_comments": 2,
        "mentions": 2,
        "facts": 3,
    }


def test_summary_counts_on_empty_database(conn: sqlite3.Connection) -> None:
    assert summary_counts(conn, "private", NOW) == {
        "sources": 0,
        "comments": 0,
        "parsed_comments": 0,
        "mentions": 0,
        "facts": 0,
    }
