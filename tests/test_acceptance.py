"""SPEC §11 acceptance tests, run end to end over the demo database built by the CLI.

The database comes from ``python -m crop_explorer demo`` (see ``conftest.py``): the synthetic
fixtures are ingested with rights ``synthetic`` and their offline extractions are validated and
loaded. Comment ids and expectations come from ``fixtures/SCENARIOS.md``. One test per SPEC §11
item 1-16 and 18; item 17 (phone-width UI, resets, source links) belongs to the Streamlit app
and is covered in ``tests/test_app.py``.

Passing these synthetic tests is not evidence of real-world extraction accuracy (SPEC §11).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from crop_explorer import db
from crop_explorer.config import load_config
from crop_explorer.environment_match import applicable_facts
from crop_explorer.geography import choropleth, region_counts
from crop_explorer.search import chip_support, search
from crop_explorer.views import (
    CommentResult,
    CommentView,
    GardenContext,
    MentionView,
    SearchQuery,
    SearchResult,
)
from crop_explorer.vocab import COMPARABLE_KEYS, VIEWER_VALUES

if TYPE_CHECKING:
    from conftest import CliEnv, SourceImporter

NOW = "2026-10-15T12:00:00Z"
CFG = load_config()
DEMO_SOURCE = "synthetic-demo"
EXTRACTIONS = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic_extractions.jsonl"
EXAMPLE_GARDEN = GardenContext(zone="6b", method="container", humidity="humid")
CONNECTICUT = ("syn-0006", "syn-0013", "syn-0036", "syn-0063")
REPORT_BASES = {"observed_experience", "reported_garden_context"}
OUTSIDE_LABELS = {"regional climate reference", "published external trial"}


# --------------------------------------------------------------------------- helpers


def run(conn: sqlite3.Connection, scope: Any = "public", **query: Any) -> SearchResult:
    return search(conn, scope, SearchQuery(**query), NOW)


def results(result: SearchResult) -> list[CommentResult]:
    return [item for group in result.groups for item in group.results]


def ids(result: SearchResult) -> list[str]:
    return [item.comment.comment_id for item in results(result)]


def by_id(result: SearchResult, comment_id: str) -> CommentResult:
    (found,) = [item for item in results(result) if item.comment.comment_id == comment_id]
    return found


def group_of(result: SearchResult, comment_id: str) -> str:
    (key,) = [
        group.key
        for group in result.groups
        for item in group.results
        if item.comment.comment_id == comment_id
    ]
    return key


def group_ids(result: SearchResult, key: str) -> list[str]:
    (group,) = [group for group in result.groups if group.key == key]
    return [item.comment.comment_id for item in group.results]


def dimension_statuses(item: CommentResult) -> dict[str, str]:
    """Per-dimension status of the comment's best relevant mention (the first of its tier)."""
    matches = [m.match for m in item.mention_matches if m.match is not None]
    best = next(match for match in matches if match.tier == item.tier)
    return {d.key: d.status for d in best.dimensions}


def view(conn: sqlite3.Connection, comment_id: str) -> CommentView:
    (found,) = db.load_comment_views(conn, "public", NOW, [comment_id])
    return found


def mention_named(comment: CommentView, variety: str) -> MentionView:
    (found,) = [m for m in comment.mentions if m.variety_as_written == variety]
    return found


def normalized(facts: Sequence[Any], key: str) -> set[str | None]:
    return {f.normalized_value for f in facts if f.key == key}


def state_counts(result: SearchResult) -> dict[str, int]:
    table = region_counts(results(result), 1).table
    return dict(zip(table["region_code"], table["n_comments"], strict=True))


# --------------------------------------------------------------------------- §11 #1


def test_11_01_search_preservation_finds_raw_text_without_extraction(
    demo_conn: sqlite3.Connection,
) -> None:
    unresolved = run(demo_conn, text="trellis")
    never_extracted = run(demo_conn, text="video")
    no_facts = run(demo_conn, text="Terrific", garden=EXAMPLE_GARDEN)

    assert "syn-0033" in ids(unresolved)
    assert by_id(unresolved, "syn-0033").comment.parse_status == "unresolved"
    assert by_id(unresolved, "syn-0033").comment.mentions == ()
    assert "syn-0012" in ids(run(demo_conn, text="pots"))  # unresolved question about pots
    assert by_id(never_extracted, "syn-0004").comment.parse_status is None
    assert ids(no_facts) == ["syn-0002"]
    assert group_of(no_facts, "syn-0002") == "not_stated"


# --------------------------------------------------------------------------- §11 #2


def test_11_02_environment_association_keeps_greenhouse_and_outdoor_crops_apart(
    demo_conn: sqlite3.Connection,
) -> None:
    comment = view(demo_conn, "syn-0007")
    peppers = mention_named(comment, "Jalapeño")
    tomatoes = mention_named(comment, "Roma")

    assert normalized(applicable_facts(peppers, comment.mentions), "shelter") == {"greenhouse"}
    assert normalized(applicable_facts(tomatoes, comment.mentions), "shelter") == {"outdoors"}
    greenhouse = GardenContext(shelter="greenhouse")
    assert group_of(run(demo_conn, crop="pepper", garden=greenhouse), "syn-0007") == "match"
    assert group_of(run(demo_conn, crop="tomato", garden=greenhouse), "syn-0007") == "different"


# --------------------------------------------------------------------------- §11 #3


def test_11_03_zone_only_report_invents_no_geography_or_climate(
    demo_conn: sqlite3.Connection,
) -> None:
    comment = view(demo_conn, "syn-0010")
    facts = [f for m in comment.mentions for f in m.facts]

    assert {(f.key, f.normalized_value) for f in facts} == {("zone", "7a")}
    only_zone = run(demo_conn, text="Celebrity")
    regions = region_counts(results(only_zone), 1)
    assert (len(regions.table), regions.not_reported) == (0, 1)
    figure = json.loads(choropleth(regions).to_json())
    assert not any(key in trace for trace in figure["data"] for key in ("lat", "lon"))
    columns = {
        row["name"]
        for table in ("sources", "comments", "mentions", "environment_facts")
        for row in demo_conn.execute(f"PRAGMA table_info({table})")
    }
    assert not columns & {"lat", "lon", "latitude", "longitude", "coordinates"}
    assert normalized(view(demo_conn, "syn-0014").mentions[0].facts, "zone") == set()


# --------------------------------------------------------------------------- §11 #4


def test_11_04_location_semantics_map_cultivation_not_residence(
    demo_conn: sqlite3.Connection,
) -> None:
    comment = view(demo_conn, "syn-0008")

    assert normalized(comment.mentions[0].facts, "region") == {"US-ME"}
    assert ids(run(demo_conn, region_filter="US-ME")) == ["syn-0008"]
    assert run(demo_conn, region_filter="US-FL").n_candidates == 0
    states = state_counts(run(demo_conn))
    assert states["US-ME"] == 1
    assert "US-FL" not in states and "US-KS" not in states  # residence and hearsay
    assert "syn-0060" not in ids(run(demo_conn, region_filter="US-TX"))  # a greeting


# --------------------------------------------------------------------------- §11 #5


def test_11_05_hypothetical_zone_is_never_a_growing_report(demo_conn: sqlite3.Connection) -> None:
    result = run(demo_conn, garden=GardenContext(zone="5"))

    assert normalized(view(demo_conn, "syn-0009").mentions[0].facts, "zone") == set()
    assert result.tier_counts["match"] == result.tier_counts["partial"] == 0
    assert group_of(result, "syn-0009") == "not_stated"
    assert by_id(result, "syn-0009").explanation == "None of your selected conditions were stated."


# --------------------------------------------------------------------------- §11 #6

GARDENS = st.builds(
    GardenContext,
    **{key: st.none() | st.sampled_from(VIEWER_VALUES[key]) for key in COMPARABLE_KEYS},
).filter(lambda garden: bool(garden.selected()))


@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(garden=GARDENS, strict=st.booleans())
def test_11_06_missing_conditions_are_never_a_mismatch(
    demo_conn: sqlite3.Connection, garden: GardenContext, strict: bool
) -> None:
    """A flavor-only report is findable under any garden, labelled "not stated"."""
    result = run(demo_conn, garden=garden, only_stated_matches=strict, text="Terrific")

    if strict:
        assert result.n_hidden_by_strict == 1  # hidden only on request, and counted
        assert result.groups == ()
    else:
        assert group_of(result, "syn-0002") == "not_stated"
        item = by_id(result, "syn-0002")
        assert set(dimension_statuses(item).values()) == {"not_stated"}
        assert item.explanation == "None of your selected conditions were stated."


# --------------------------------------------------------------------------- §11 #7


def test_11_07_explainable_match_is_prioritized_with_its_dimensions(
    demo_conn: sqlite3.Connection,
) -> None:
    result = run(demo_conn, garden=EXAMPLE_GARDEN)

    assert result.groups[0].key == "match"
    assert group_ids(result, "match") == ["syn-0001", "syn-0003", "syn-0006"]
    for comment_id in group_ids(result, "match"):
        item = by_id(result, comment_id)
        assert dimension_statuses(item) == {
            "zone": "match",
            "method": "match",
            "humidity": "match",
        }
        assert item.explanation.startswith("Matches your ")
        for label in ("zone 6b", "containers", "humid"):
            assert label in item.explanation
    partial = by_id(result, "syn-0005").explanation
    assert "hardiness zone and humidity not stated" in partial  # unknowns are not filled in


# --------------------------------------------------------------------------- §11 #8


def test_11_08_partial_overlap_is_partial(demo_conn: sqlite3.Connection) -> None:
    result = run(demo_conn, garden=EXAMPLE_GARDEN)
    item = by_id(result, "syn-0005")

    assert (group_of(result, "syn-0005"), item.tier) == ("partial", "partial")
    assert dimension_statuses(item) == {
        "zone": "not_stated",
        "method": "match",
        "humidity": "not_stated",
    }


# --------------------------------------------------------------------------- §11 #9


def test_11_09_different_practice_is_shown_as_different_and_stays_accessible(
    demo_conn: sqlite3.Connection,
) -> None:
    shown = run(demo_conn, garden=EXAMPLE_GARDEN)
    strict = run(demo_conn, garden=EXAMPLE_GARDEN, only_stated_matches=True)

    (different,) = [group for group in shown.groups if group.key == "different"]
    assert different.collapsed_by_default
    assert "syn-0019" in group_ids(shown, "different")
    assert by_id(shown, "syn-0019").explanation.startswith(
        "Different: in the ground (you: containers)"
    )
    assert dimension_statuses(by_id(shown, "syn-0019"))["method"] == "different"
    assert "syn-0019" not in ids(strict)
    hidden = shown.tier_counts["not_stated"] + shown.tier_counts["different"]
    assert strict.n_hidden_by_strict == hidden
    assert strict.tier_counts == shown.tier_counts  # the counts stay honest when hiding


# --------------------------------------------------------------------------- §11 #10


def test_11_10_coarse_zone_is_not_an_exact_match(demo_conn: sqlite3.Connection) -> None:
    result = run(demo_conn, garden=EXAMPLE_GARDEN)
    coarse = by_id(result, "syn-0011")

    assert dimension_statuses(coarse)["zone"] == "imprecise"
    assert coarse.tier == "partial"
    assert "zone 6 reported (subzone unknown)" in coarse.explanation
    assert dimension_statuses(by_id(result, "syn-0057"))["zone"] == "different"  # zone 6a
    assert dimension_statuses(by_id(result, "syn-0010"))["zone"] == "different"  # zone 7a


# --------------------------------------------------------------------------- §11 #11


def test_11_11_failure_and_success_under_matching_conditions_share_a_tier(
    demo_conn: sqlite3.Connection,
) -> None:
    result = run(demo_conn, garden=EXAMPLE_GARDEN)
    success, failure = by_id(result, "syn-0001"), by_id(result, "syn-0003")

    assert success.tier == failure.tier == "match"
    assert group_of(result, "syn-0001") == group_of(result, "syn-0003")
    ordered = group_ids(result, "match")
    assert ordered.index("syn-0001") < ordered.index("syn-0003")  # source order, not outcome


# --------------------------------------------------------------------------- §11 #12


def test_11_12_several_mentions_count_as_one_comment(demo_conn: sqlite3.Connection) -> None:
    result = run(demo_conn, crop="tomato", garden=EXAMPLE_GARDEN)
    found = ids(result)

    assert len(view(demo_conn, "syn-0013").mentions) == 3
    assert found.count("syn-0013") == 1
    assert len(found) == len(set(found)) == result.n_candidates
    assert sum(result.tier_counts.values()) == result.n_candidates
    summary = db.summary_counts(demo_conn, "public", NOW)
    assert (summary["comments"], summary["mentions"]) == (69, 71)
    assert state_counts(run(demo_conn))["US-CT"] == len(CONNECTICUT)  # syn-0013 counted once


# --------------------------------------------------------------------------- §11 #13


def test_11_13_map_counts_reported_states_and_a_state_filters_reports(
    demo_conn: sqlite3.Connection,
) -> None:
    everything = run(demo_conn)
    regions = region_counts(results(everything), CFG.map.public_min_cell_count)
    states = state_counts(everything)

    assert states["US-CT"] == 4
    assert len(states) == 18
    assert "CT" in json.loads(choropleth(regions).to_json())["data"][0]["locations"]
    unmapped = regions.not_reported + regions.outside_us + regions.us_state_not_reported
    assert unmapped + sum(states.values()) == 69  # no fixture comment names two states
    assert regions.not_reported == 45  # includes the zone-only syn-0010
    clicked = run(demo_conn, region_filter="US-CT", garden=EXAMPLE_GARDEN)
    assert sorted(ids(clicked)) == list(CONNECTICUT)
    assert group_of(clicked, "syn-0006") == "match"  # the other conditions are preserved
    zone_only = run(demo_conn, text="Celebrity")
    assert region_counts(results(zone_only), 1).not_reported == 1


# --------------------------------------------------------------------------- §11 #14


def test_11_14_reimport_and_reextraction_are_idempotent_and_deletion_cascades(
    demo_env: CliEnv,
) -> None:
    def table_counts() -> dict[str, int]:
        conn = demo_env.connect()
        try:
            tables = ("comments", "parses", "mentions", "environment_facts")
            return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        finally:
            conn.close()

    before = table_counts()
    rerun = demo_env.run("demo")
    reload = demo_env.run("load-extractions", EXTRACTIONS)

    assert (rerun.code, reload.code) == (0, 0)
    assert rerun.json()["import"]["unchanged"] == 69
    assert reload.json()["comments_loaded"] == 66
    assert (
        table_counts()
        == before
        == {
            "comments": 69,
            "parses": 66,
            "mentions": 71,
            "environment_facts": 82,
        }
    )
    assert demo_env.run("delete-source", DEMO_SOURCE).json()["comments_deleted"] == 69
    assert set(table_counts().values()) == {0}
    conn = demo_env.connect()
    try:
        for scope in ("public", "private"):
            everything = search(conn, scope, SearchQuery(text="Sungold"), NOW)
            assert everything.n_candidates == 0
            assert region_counts(results(run(conn, scope)), 1).table.empty
    finally:
        conn.close()


# --------------------------------------------------------------------------- §11 #15


def test_11_15_public_mode_reveals_no_private_records(
    demo_conn: sqlite3.Connection, add_private_source: SourceImporter
) -> None:
    """Compact end-to-end check; tests/test_visibility.py covers every filter and aggregate."""

    def public_view() -> tuple[Any, ...]:
        everything = run(demo_conn)
        return (
            everything,
            run(demo_conn, text="Zyzzyvaroot"),
            chip_support(demo_conn, "public", NOW, CFG.quick_chips),
            db.summary_counts(demo_conn, "public", NOW),
            region_counts(results(everything), 1).table.to_dict("records"),
        )

    before = public_view()
    add_private_source(
        demo_conn,
        source_id="private-export",
        comments=[{"comment_id": "p1", "text": "Zyzzyvaroot tomatoes in grow bags in Idaho."}],
        extractions=[
            {
                "comment_id": "p1",
                "extractor_version": "acceptance-v1",
                "status": "parsed",
                "mentions": [
                    {
                        "crop": "tomato",
                        "variety_as_written": "Zyzzyvaroot",
                        "evidence_quote": "Zyzzyvaroot tomatoes in grow bags",
                        "environment": [
                            {
                                "key": "method",
                                "value_as_written": "grow bags",
                                "evidence_quote": "tomatoes in grow bags",
                                "applicability": "this_mention",
                                "basis": "reported_garden_context",
                            },
                            {
                                "key": "region",
                                "value_as_written": "Idaho",
                                "evidence_quote": "in Idaho",
                                "applicability": "this_mention",
                                "basis": "reported_garden_context",
                            },
                        ],
                    }
                ],
            }
        ],
    )

    assert public_view() == before
    private = run(demo_conn, "private", text="Zyzzyvaroot")
    assert ids(private) == ["p1"]
    assert chip_support(demo_conn, "private", NOW, CFG.quick_chips)["Containers"] == 9
    assert state_counts(run(demo_conn, "private"))["US-ID"] == 1


# --------------------------------------------------------------------------- §11 #16


def test_11_16_sparse_conditions_still_offer_search_and_explain_the_limitation(
    demo_conn: sqlite3.Connection, add_private_source: SourceImporter
) -> None:
    thanks = run(demo_conn, text="thanks", garden=EXAMPLE_GARDEN)

    assert thanks.n_candidates >= 1 and thanks.n_with_conditions == 0
    assert [group.key for group in thanks.groups] == ["not_stated"]
    assert thanks.limitation_message is not None
    assert thanks.limitation_message.startswith(f"None of these {thanks.n_candidates} report")
    overall = run(demo_conn, garden=EXAMPLE_GARDEN)
    assert overall.limitation_message == "Only 35 of 69 reports mention growing conditions."

    # A corpus where *no* comment states a condition (and none was ever extracted).
    db.delete_source(demo_conn, DEMO_SOURCE)
    add_private_source(
        demo_conn,
        source_id="no-conditions",
        rights_status="synthetic",
        comments=[
            {"comment_id": "n1", "text": "Great video, thanks!"},
            {"comment_id": "n2", "text": "Which tomato should I start with?"},
            {"comment_id": "n3", "text": "Loved the tomato tasting segment."},
        ],
    )
    sparse = run(demo_conn, garden=EXAMPLE_GARDEN)
    assert (sparse.n_candidates, sparse.n_with_conditions) == (3, 0)
    assert sparse.limitation_message == (
        "None of these 3 reports mention growing conditions, so none can be compared with "
        "your garden. All are shown below."
    )
    assert [(group.key, len(group.results)) for group in sparse.groups] == [("not_stated", 3)]
    assert ids(run(demo_conn, text="tomato", garden=EXAMPLE_GARDEN)) == ["n2", "n3"]
    assert run(demo_conn, text="tomato").limitation_message is not None


# --------------------------------------------------------------------------- §11 #17
# Mobile layout, clean resets and source links are properties of the Streamlit viewer; they
# are tested in tests/test_app.py.


# --------------------------------------------------------------------------- §11 #18


def test_11_18_every_chip_is_an_exact_quote_labelled_as_a_community_report(
    demo_conn: sqlite3.Connection,
) -> None:
    every = run(demo_conn, "private", garden=EXAMPLE_GARDEN)
    chips = 0

    for item in results(every):
        comment = item.comment
        assert (comment.source_kind, comment.rights_status) == ("synthetic_fixture", "synthetic")
        for mention_match in item.mention_matches:
            assert mention_match.mention.evidence_quote in comment.text
            for fact in mention_match.facts:
                assert fact.evidence_quote in comment.text
                assert fact.basis in REPORT_BASES
                chips += 1
            evidence = [] if mention_match.match is None else mention_match.match.dimensions
            assert all(f.evidence_quote in comment.text for d in evidence for f in d.evidence)
    assert chips >= 82  # every stored fact (inherited facts are counted per mention)
    assert {link.label for link in CFG.outside_context} <= OUTSIDE_LABELS


@pytest.mark.parametrize("comment_id", ["syn-0014", "syn-0020", "syn-0039", "syn-0058"])
def test_ambiguous_or_untrusted_statements_stay_text_only(
    demo_conn: sqlite3.Connection, comment_id: str
) -> None:
    """Ambiguous scope, injected instructions, names and hearsay never become location chips."""
    comment = view(demo_conn, comment_id)

    assert not [f for m in comment.mentions for f in m.facts if f.key in {"region", "zone"}]
    assert comment_id in db.visible_comment_ids(demo_conn, "public", NOW, None)
