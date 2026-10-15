"""Tests for rights and expiry gating of viewer-facing reads (SPEC §9, §11 #15).

The visibility clause is exercised against a tiny independent SQLite schema (not ``db.py``) so
its semantics are pinned down on their own: rights, expiry and soft deletion.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from crop_explorer import db
from crop_explorer.config import load_config
from crop_explorer.geography import choropleth, region_counts
from crop_explorer.schemas import RIGHTS_STATUSES, RightsStatus, to_utc_iso
from crop_explorer.search import chip_support, filter_options, search
from crop_explorer.views import (
    ChipSupport,
    CommentResult,
    GardenContext,
    SearchQuery,
    SearchResult,
)
from crop_explorer.visibility import (
    PUBLIC_RIGHTS,
    require_utc_iso,
    visibility_clause,
    visible_rights,
)
from crop_explorer.vocab import VIEWER_VALUES

if TYPE_CHECKING:
    from conftest import DemoBuild, SourceImporter

PAST = "2026-01-01T00:00:00Z"
NOW = "2026-10-15T12:00:00Z"
FUTURE = "2027-01-01T00:00:00Z"

# Independent oracle: what each scope may see (SPEC §9).
SCOPE_SEES: dict[str, frozenset[str]] = {
    "public": frozenset({"synthetic", "cleared"}),
    "private": frozenset({"private_unverified", "synthetic", "cleared"}),
}

Row = tuple[RightsStatus, str | None, str | None]  # (rights_status, expires_at, deleted_at)

rows_strategy = st.lists(
    st.tuples(
        st.sampled_from(RIGHTS_STATUSES),
        st.sampled_from([None, PAST, NOW, FUTURE]),
        st.sampled_from([None, PAST]),
    ),
    max_size=12,
)
scopes = st.sampled_from(["public", "private"])


def gate_db(rows: Sequence[Row]) -> sqlite3.Connection:
    """One source per row, each with one comment ``c{i}``; aliases are chosen by the query."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE src (source_id TEXT, rights_status TEXT, expires_at TEXT)")
    conn.execute("CREATE TABLE cm (comment_id TEXT, source_id TEXT, deleted_at TEXT)")
    for i, (rights, expires_at, deleted_at) in enumerate(rows):
        conn.execute("INSERT INTO src VALUES (?, ?, ?)", (f"s{i}", rights, expires_at))
        conn.execute("INSERT INTO cm VALUES (?, ?, ?)", (f"c{i}", f"s{i}", deleted_at))
    return conn


def select_ids(conn: sqlite3.Connection, where: str, params: Sequence[str]) -> set[str]:
    sql = f"SELECT c.comment_id FROM cm c JOIN src s ON s.source_id = c.source_id WHERE {where}"
    return {row[0] for row in conn.execute(sql, params)}


def oracle(rows: Sequence[Row], scope: str, now: str) -> set[str]:
    return {
        f"c{i}"
        for i, (rights, expires_at, deleted_at) in enumerate(rows)
        if rights in SCOPE_SEES[scope]
        and (expires_at is None or expires_at > now)
        and deleted_at is None
    }


@pytest.fixture
def one_row_db() -> Iterator[sqlite3.Connection]:
    conn = gate_db([("synthetic", None, None)])
    yield conn
    conn.close()


# --------------------------------------------------------------------------- visible_rights


def test_public_rights_are_exactly_synthetic_and_cleared() -> None:
    assert frozenset({"synthetic", "cleared"}) == PUBLIC_RIGHTS
    assert visible_rights("public") == PUBLIC_RIGHTS
    assert "private_unverified" not in visible_rights("public")


def test_private_scope_sees_every_rights_status() -> None:
    assert visible_rights("private") == frozenset(RIGHTS_STATUSES)


@pytest.mark.parametrize("scope", ["Public", "PRIVATE", "", "admin", "public "])
def test_unknown_scope_raises_value_error(scope: Any) -> None:
    with pytest.raises(ValueError, match="scope"):
        visible_rights(scope)
    with pytest.raises(ValueError, match="scope"):
        visibility_clause(scope, NOW)


# --------------------------------------------------------------------------- visibility_clause


@given(rows=rows_strategy, scope=scopes)
def test_clause_selects_exactly_visible_rows(rows: list[Row], scope: Any) -> None:
    where, params = visibility_clause(scope, NOW)
    with closing(gate_db(rows)) as conn:
        assert select_ids(conn, where, params) == oracle(rows, scope, NOW)


@given(rows=rows_strategy, scope=scopes)
def test_negated_clause_selects_exactly_invisible_rows(rows: list[Row], scope: Any) -> None:
    """The fragment composes as a single boolean term (safe under NOT / OR)."""
    where, params = visibility_clause(scope, NOW)
    everything = {f"c{i}" for i in range(len(rows))}
    with closing(gate_db(rows)) as conn:
        invisible = select_ids(conn, f"NOT {where}", params)
    assert invisible == everything - oracle(rows, scope, NOW)


@given(rows=rows_strategy)
def test_public_visible_rows_are_a_subset_of_private(rows: list[Row]) -> None:
    with closing(gate_db(rows)) as conn:
        public = select_ids(conn, *visibility_clause("public", NOW))
        private = select_ids(conn, *visibility_clause("private", NOW))
    assert public <= private


def test_source_expiring_exactly_now_is_invisible(one_row_db: sqlite3.Connection) -> None:
    one_row_db.execute("UPDATE src SET expires_at = ?", (NOW,))
    for scope in ("public", "private"):
        assert select_ids(one_row_db, *visibility_clause(scope, NOW)) == set()
    assert select_ids(one_row_db, *visibility_clause("public", PAST)) == {"c0"}


def test_custom_aliases_are_honoured() -> None:
    where, params = visibility_clause("public", NOW, source_alias="srcx", comment_alias="cmx")
    sql = f"SELECT cmx.comment_id FROM cm cmx JOIN src srcx USING (source_id) WHERE {where}"
    with closing(gate_db([("cleared", FUTURE, None), ("private_unverified", None, None)])) as conn:
        assert [row[0] for row in conn.execute(sql, params)] == ["c0"]


def test_values_are_bound_parameters_never_interpolated() -> None:
    where, params = visibility_clause("public", NOW)
    assert NOW not in where
    assert not any(status in where for status in RIGHTS_STATUSES)
    assert where.count("?") == len(params)
    assert params == ["cleared", "synthetic", NOW]


@pytest.mark.parametrize(
    "alias", ["", "1s", "s.x", "s c", "s; DROP TABLE src; --", "s'", '"s"', "s\n"]
)
def test_unsafe_alias_raises_value_error(alias: str) -> None:
    with pytest.raises(ValueError, match="alias"):
        visibility_clause("public", NOW, source_alias=alias)
    with pytest.raises(ValueError, match="alias"):
        visibility_clause("public", NOW, comment_alias=alias)


# --------------------------------------------------------------------------- timestamps

NON_CANONICAL = [
    "",
    "now",
    "2026-10-15",
    "2026-10-15 12:00:00",
    "2026-10-15T12:00:00",
    "2026-10-15T12:00:00+00:00",
    "2026-10-15T12:00:00.000Z",
    "2026-10-15t12:00:00z",
    "2026-02-30T00:00:00Z",
    " 2026-10-15T12:00:00Z",
]


@pytest.mark.parametrize("now", NON_CANONICAL)
def test_non_canonical_now_raises_value_error(now: str) -> None:
    with pytest.raises(ValueError, match="now"):
        visibility_clause("private", now)
    with pytest.raises(ValueError, match="now"):
        require_utc_iso(now, "now")


@given(moment=st.datetimes(min_value=datetime(1000, 1, 1), timezones=st.just(UTC)))
def test_canonical_timestamps_are_accepted_unchanged(moment: datetime) -> None:
    canonical = to_utc_iso(moment)
    assert require_utc_iso(canonical, "now") == canonical
    _, params = visibility_clause("private", canonical)
    assert params[-1] == canonical


def test_require_utc_iso_names_the_offending_field() -> None:
    with pytest.raises(ValueError, match="expires_at"):
        require_utc_iso("tomorrow", "expires_at")


# --------------------------------------------------------------------------- end to end (#15)
# A demo database (synthetic source, built by the CLI) plus a private_unverified source whose
# comments carry unique words, crops, varieties, states and conditions, plus a cleared source
# that has expired. Public results, counts, chips, filter options, map data and summaries must
# be *identical* to those of the demo database alone; the private scope must see the private
# rows; deleting or expiring a source must remove it everywhere.

CFG = load_config()
PRIVATE_SOURCE = "private-export"
EXPIRED_SOURCE = "expired-export"
PRIVATE_WORDS = (
    "Zanzibarquux",
    "Quetzalmoon",
    "Blorptastic",
    "Wonderbean",
    "Glimmerquash",
    "Vermiculitopia",
    "Xylophonica",
)
EXPIRED_WORD = "Gloriousexpiry"
PRIVATE_CROPS = ("okra", "kohlrabi", "pumpkin")
PRIVATE_REGIONS = ("US-WY", "US-NV", "CA-BC")
PRIVATE_TOKENS = (
    *PRIVATE_WORDS,
    EXPIRED_WORD,
    *PRIVATE_CROPS,
    *PRIVATE_REGIONS,
    "Wyoming",
    "Nevada",
    "British Columbia",
    '"WY"',
    '"NV"',
)
DEMO_CHIPS = {
    "Containers": ChipSupport(n_any=8, n_all=8),
    "Hot & humid": ChipSupport(n_any=6, n_all=2),
    "Short season": ChipSupport(n_any=4, n_all=4),
    "Greenhouse": ChipSupport(n_any=5, n_all=5),
}

PRIVATE_COMMENTS = [
    {
        "comment_id": "prv-0001",
        "text": "Zanzibarquux okra loved the grow bags on my porch in Wyoming. "
        "Zone 4a, cool summers, loam in every bag.",
    },
    {
        "comment_id": "prv-0002",
        "text": "Quetzalmoon kohlrabi bolted in the greenhouse here in Nevada during 2019. "
        "Short season, hand watered, deep shade.",
    },
    {
        "comment_id": "prv-0003",
        "text": "Hartford, CT again: Blorptastic Wonderbean beans in containers, muggy as ever.",
    },
    {
        "comment_id": "prv-0004",
        "text": "Glimmerquash pumpkins sprawled across raised beds in British Columbia.",
    },
    {"comment_id": "prv-0005", "text": "Vermiculitopia question: which trellis netting lasts?"},
    {"comment_id": "prv-0006", "text": "Xylophonica greetings from my kitchen table."},
]
EXPIRED_COMMENTS = [
    {
        "comment_id": "exp-0001",
        "text": "Gloriousexpiry Sungold tomatoes thrived in pots in Oregon.",
    }
]


def fact(
    key: str, written: str, normalized: str, quote: str, applicability: str = "this_mention"
) -> dict[str, Any]:
    """One fact; the loader never derives a missing ``normalized_value``, so it is given."""
    return {
        "key": key,
        "value_as_written": written,
        "normalized_value": normalized,
        "evidence_quote": quote,
        "applicability": applicability,
        "basis": "reported_garden_context",
    }


def parsed_line(comment_id: str, *mentions: dict[str, Any]) -> dict[str, Any]:
    status = "parsed" if mentions else "unresolved"
    return {
        "comment_id": comment_id,
        "extractor_version": "isolation-test-v1",
        "status": status,
        "mentions": list(mentions),
    }


def mention(crop: str, variety: str, quote: str, *facts: dict[str, Any]) -> dict[str, Any]:
    return {
        "crop": crop,
        "variety_as_written": variety,
        "topics": ["productivity"],
        "evidence_quote": quote,
        "environment": list(facts),
    }


PRIVATE_EXTRACTIONS = [
    parsed_line(
        "prv-0001",
        mention(
            "okra",
            "Zanzibarquux",
            "Zanzibarquux okra loved the grow bags on my porch in Wyoming",
            fact("region", "Wyoming", "US-WY", "on my porch in Wyoming", "whole_comment"),
            fact("zone", "Zone 4a", "4a", "Zone 4a", "whole_comment"),
            fact("method", "grow bags", "container", "loved the grow bags"),
            fact("summer_heat", "cool summers", "cool", "cool summers", "whole_comment"),
            fact("soil", "loam", "loam", "loam in every bag"),
        ),
    ),
    parsed_line(
        "prv-0002",
        mention(
            "kohlrabi",
            "Quetzalmoon",
            "Quetzalmoon kohlrabi bolted in the greenhouse",
            fact("shelter", "greenhouse", "greenhouse", "bolted in the greenhouse"),
            fact("region", "Nevada", "US-NV", "here in Nevada"),
            fact("growing_year", "2019", "2019", "during 2019"),
            fact("season_length", "Short season", "short_season", "Short season"),
            fact("irrigation", "hand watered", "hand_watered", "hand watered"),
            fact("sun", "deep shade", "shade", "deep shade"),
        ),
    ),
    parsed_line(
        "prv-0003",
        mention(
            "beans",
            "Blorptastic Wonderbean",
            "Blorptastic Wonderbean beans in containers",
            fact("region", "Hartford, CT", "US-CT", "Hartford, CT"),
            fact("method", "containers", "container", "beans in containers"),
            fact("humidity", "muggy", "humid", "muggy as ever"),
        ),
    ),
    parsed_line(
        "prv-0004",
        mention(
            "pumpkins",
            "Glimmerquash",
            "Glimmerquash pumpkins sprawled across raised beds",
            fact("method", "raised beds", "raised_bed", "across raised beds"),
            fact("region", "British Columbia", "CA-BC", "in British Columbia"),
        ),
    ),
    parsed_line("prv-0005"),  # unresolved, no mentions; prv-0006 is never extracted
]
EXPIRED_EXTRACTIONS = [
    parsed_line(
        "exp-0001",
        mention(
            "tomatoes",
            "Sungold",
            "Gloriousexpiry Sungold tomatoes thrived in pots",
            fact("method", "pots", "container", "thrived in pots"),
            fact("region", "Oregon", "US-OR", "in Oregon"),
        ),
    )
]


def add_private(importer: SourceImporter, conn: sqlite3.Connection, *args: Any) -> None:
    importer(
        conn,
        *args,
        source_id=PRIVATE_SOURCE,
        comments=PRIVATE_COMMENTS,
        extractions=PRIVATE_EXTRACTIONS,
    )


def add_expired(
    importer: SourceImporter, conn: sqlite3.Connection, *args: Any, expires_at: str = PAST
) -> None:
    """Import and extract while permission holds, then re-import recording the expiry.

    Extractions can only be loaded for comments whose permission has not ended, so the
    expiry is set by a second (identical) import, as an operator would.
    """
    common: dict[str, Any] = {
        "source_id": EXPIRED_SOURCE,
        "comments": EXPIRED_COMMENTS,
        "rights_status": "cleared",
    }
    importer(conn, *args, **common, extractions=EXPIRED_EXTRACTIONS)
    importer(conn, *args, **common, expires_at=expires_at)


def flatten(result: SearchResult) -> list[CommentResult]:
    return [item for group in result.groups for item in group.results]


def result_ids(result: SearchResult) -> list[str]:
    return sorted(item.comment.comment_id for item in flatten(result))


def snapshot(conn: sqlite3.Connection, scope: Any, now: str = NOW) -> dict[str, Any]:
    """Everything a viewer in ``scope`` can derive: results, counts, chips, options, map."""
    everything = search(conn, scope, SearchQuery(), now)
    min_cell = CFG.map.public_min_cell_count if scope == "public" else 1
    regions = region_counts(flatten(everything), min_cell)
    return {
        "search": everything,
        "chips": chip_support(conn, scope, now, CFG.quick_chips),
        "options": filter_options(conn, scope, now),
        "summary": db.summary_counts(conn, scope, now),
        "map_table": regions.table.to_dict("records"),
        "map_buckets": (regions.not_reported, regions.outside_us, regions.us_state_not_reported),
        "figure": choropleth(regions).to_json(),
    }


def serialized(snap: dict[str, Any]) -> str:
    return "\n".join(
        value.model_dump_json() if hasattr(value, "model_dump_json") else str(value)
        for value in snap.values()
    )


def assert_no_private_tokens(text: str) -> None:
    assert [token for token in PRIVATE_TOKENS if token in text] == []


@dataclass(frozen=True)
class IsolationDbs:
    baseline: sqlite3.Connection  # the demo database alone
    mixed: sqlite3.Connection  # the same plus the private and the expired sources


@pytest.fixture(scope="module")
def isolation(
    demo_template: DemoBuild,
    source_importer: SourceImporter,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[IsolationDbs]:
    root = tmp_path_factory.mktemp("isolation")
    baseline = db.connect(demo_template.copy_to(root / "baseline.sqlite"))
    mixed = db.connect(demo_template.copy_to(root / "mixed.sqlite"))
    add_private(source_importer, mixed, root / "imports")
    add_expired(source_importer, mixed, root / "imports")
    yield IsolationDbs(baseline=baseline, mixed=mixed)
    baseline.close()
    mixed.close()


def _maybe(values: Sequence[str]) -> st.SearchStrategy[str | None]:
    return st.one_of(st.none(), st.sampled_from(values))


GARDENS = st.builds(
    GardenContext,
    region=_maybe(("US-CT", "US-WY", "US-NV", "CA-BC")),
    zone=_maybe(("4a", "4", "6b")),
    method=_maybe(VIEWER_VALUES["method"]),
    shelter=_maybe(VIEWER_VALUES["shelter"]),
    summer_heat=_maybe(VIEWER_VALUES["summer_heat"]),
    humidity=_maybe(VIEWER_VALUES["humidity"]),
    season_length=_maybe(VIEWER_VALUES["season_length"]),
    sun=_maybe(VIEWER_VALUES["sun"]),
    soil=_maybe(VIEWER_VALUES["soil"]),
    irrigation=_maybe(VIEWER_VALUES["irrigation"]),
)
QUERIES = st.builds(
    SearchQuery,
    text=_maybe((*PRIVATE_WORDS, EXPIRED_WORD, "trellis", "pots", "beans", "greenhouse")),
    crop=_maybe((*PRIVATE_CROPS, "bean", "tomato")),
    topics=st.sampled_from([(), ("productivity",), ("flavor", "productivity")]),
    year=_maybe(("2019", "2023")),
    region_filter=_maybe((*PRIVATE_REGIONS, "US-CT", "US-OR")),
    garden=GARDENS,
    only_stated_matches=st.booleans(),
)


@settings(max_examples=150, deadline=None)
@given(query=QUERIES)
def test_public_search_is_identical_with_or_without_private_sources(
    isolation: IsolationDbs, query: SearchQuery
) -> None:
    """Text, every filter, tiers, counts and the limitation message ignore private rows."""
    public = search(isolation.mixed, "public", query, NOW)

    assert public == search(isolation.baseline, "public", query, NOW)
    assert_no_private_tokens(public.model_dump_json())


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        *((SearchQuery(text=word), "prv-") for word in PRIVATE_WORDS),
        (SearchQuery(crop="okra"), "prv-0001"),
        (SearchQuery(crop="kohlrabi"), "prv-0002"),
        (SearchQuery(crop="pumpkins"), "prv-0004"),
        (SearchQuery(year="2019"), "prv-0002"),
        (SearchQuery(region_filter="US-WY"), "prv-0001"),
        (SearchQuery(region_filter="US-NV"), "prv-0002"),
        (SearchQuery(region_filter="CA-BC"), "prv-0004"),
        (SearchQuery(text="Wonderbean", crop="bean", topics=("productivity",)), "prv-0003"),
    ],
)
def test_private_only_queries_find_nothing_publicly_but_match_privately(
    isolation: IsolationDbs, query: SearchQuery, expected: str
) -> None:
    public = search(isolation.mixed, "public", query, NOW)
    private = search(isolation.mixed, "private", query, NOW)

    assert (public.n_candidates, public.groups, public.limitation_message) == (0, (), None)
    assert result_ids(private)
    assert all(cid.startswith(expected) for cid in result_ids(private))


def test_public_tiers_ignore_private_condition_matches(isolation: IsolationDbs) -> None:
    query = SearchQuery(garden=GardenContext(zone="4a", summer_heat="cool", soil="loam"))

    public = search(isolation.mixed, "public", query, NOW)
    private = search(isolation.mixed, "private", query, NOW)

    assert public.tier_counts["match"] == 0
    assert public.limitation_message == (
        "Only 33 of 69 reports mention comparable growing conditions (3 not yet analysed)."
    )
    assert [item.comment.comment_id for item in private.groups[0].results] == ["prv-0001"]
    assert private.groups[0].key == "match"
    assert private.limitation_message == (
        "Only 37 of 75 reports mention comparable growing conditions (4 not yet analysed)."
    )


def test_public_counts_chips_options_map_and_summary_ignore_private_sources(
    isolation: IsolationDbs,
) -> None:
    public = snapshot(isolation.mixed, "public")

    assert public == snapshot(isolation.baseline, "public")
    assert public["chips"] == DEMO_CHIPS
    assert public["summary"] == {
        "sources": 1,
        "comments": 69,
        "parsed_comments": 66,
        "mentions": 71,
        "facts": 82,
    }
    connecticut = [row for row in public["map_table"] if row["region_code"] == "US-CT"]
    assert connecticut[0]["n_comments"] == 4
    assert_no_private_tokens(serialized(public))


def test_private_scope_sees_private_counts_chips_options_and_map(isolation: IsolationDbs) -> None:
    base = snapshot(isolation.baseline, "private")

    private = snapshot(isolation.mixed, "private")

    assert private["chips"] == {
        "Containers": ChipSupport(n_any=10, n_all=10),
        "Hot & humid": ChipSupport(n_any=7, n_all=2),  # prv-0003 is muggy but states no heat
        "Short season": ChipSupport(n_any=5, n_all=5),
        "Greenhouse": ChipSupport(n_any=6, n_all=6),
    }
    assert private["summary"] == {
        "sources": 2,
        "comments": 75,
        "parsed_comments": 71,
        "mentions": 75,
        "facts": 98,
    }
    assert set(PRIVATE_CROPS) <= set(private["options"].crops)
    assert "2019" in private["options"].years
    states = {row["region_code"]: row["n_comments"] for row in private["map_table"]}
    assert (states["US-WY"], states["US-NV"], states["US-CT"]) == (1, 1, 5)
    assert "US-OR" in states and states["US-OR"] == 1  # the expired Oregon report is not added
    assert private["map_buckets"][1] == base["map_buckets"][1] + 1  # British Columbia


def test_deleting_the_private_source_removes_it_from_private_results_and_maps(
    demo_conn: sqlite3.Connection, add_private_source: SourceImporter
) -> None:
    before = snapshot(demo_conn, "private")
    add_private_source(
        demo_conn,
        source_id=PRIVATE_SOURCE,
        comments=PRIVATE_COMMENTS,
        extractions=PRIVATE_EXTRACTIONS,
    )
    assert snapshot(demo_conn, "private") != before

    removed = db.delete_source(demo_conn, PRIVATE_SOURCE)

    assert removed == len(PRIVATE_COMMENTS)
    after = snapshot(demo_conn, "private")
    assert after == before
    assert_no_private_tokens(serialized(after))
    for word in PRIVATE_WORDS:
        assert db.visible_comment_ids(demo_conn, "private", NOW, word) == []
    orphans = demo_conn.execute(
        "SELECT COUNT(*) FROM mentions WHERE comment_id LIKE 'prv-%'"
    ).fetchone()[0]
    assert orphans == 0


def test_expired_source_is_invisible_in_every_scope_and_purged(
    demo_conn: sqlite3.Connection, add_private_source: SourceImporter, tmp_path: Path
) -> None:
    before = {scope: snapshot(demo_conn, scope) for scope in ("public", "private")}
    add_expired(add_private_source, demo_conn)

    for scope in ("public", "private"):
        assert snapshot(demo_conn, scope) == before[scope]
        assert search(demo_conn, scope, SearchQuery(text=EXPIRED_WORD), NOW).n_candidates == 0
    earlier = "2025-06-01T00:00:00Z"  # before expiry the cleared source *is* public
    assert search(demo_conn, "public", SearchQuery(text=EXPIRED_WORD), earlier).n_candidates == 1

    assert db.extractable_texts(demo_conn, ["exp-0001"], NOW) == {}  # permission has ended
    assert list(db.extractable_texts(demo_conn, ["exp-0001"], earlier)) == ["exp-0001"]
    assert db.purge_expired(demo_conn, NOW) == [EXPIRED_SOURCE]
    assert db.stored_texts(demo_conn, ["exp-0001"]) == {}
    assert search(demo_conn, "public", SearchQuery(text=EXPIRED_WORD), earlier).n_candidates == 0
