"""Tests for reported-region counts and the U.S. state map (SPEC §6, §11 #3, #4, #12, #13)."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
import pytest
from hypothesis import given
from hypothesis import strategies as st

from crop_explorer.geography import (
    TABLE_COLUMNS,
    RegionCounts,
    choropleth,
    count_label,
    region_counts,
)
from crop_explorer.schemas import Applicability, EnvKey
from crop_explorer.views import CommentResult, CommentView, FactView, MentionMatch, MentionView
from crop_explorer.vocab import COMPARABLE_KEYS

COMMENT_TEXT = "SECRET-BODY grew Sungold in pots in Hartford"
QUOTE_PREFIX = "SECRET-QUOTE"

# --------------------------------------------------------------------------- builders


def fact(
    key: EnvKey,
    value: str | None,
    *,
    fact_id: str | None = None,
    applicability: Applicability = "this_mention",
) -> FactView:
    """Build a FactView whose quote is recognisable so tests can check it never leaks."""
    written = value or "somewhere vague"
    return FactView(
        fact_id=fact_id or f"f:{key}:{value}",
        key=key,
        value_as_written=written,
        normalized_value=value,
        evidence_quote=f"{QUOTE_PREFIX} {written}",
        applicability=applicability,
        basis="reported_garden_context",
    )


def region(code: str | None, **kwargs: Any) -> FactView:
    return fact("region", code, **kwargs)


def mention(mention_id: str, *facts: FactView) -> MentionView:
    return MentionView(
        mention_id=mention_id,
        crop="tomato",
        variety_as_written=None,
        variety_key=None,
        topics=(),
        evidence_quote=f"{QUOTE_PREFIX} tomato",
        extractor_version="test-v1",
        facts=facts,
    )


def comment_view(comment_id: str, mentions: Sequence[MentionView]) -> CommentView:
    return CommentView(
        comment_id=comment_id,
        source_id="src",
        source_kind="synthetic_fixture",
        rights_status="synthetic",
        text=COMMENT_TEXT,
        permalink=None,
        source_order=0,
        created_at=None,
        parse_status="parsed",
        mentions=tuple(mentions),
    )


def result(comment_id: str, *mention_facts: Sequence[FactView]) -> CommentResult:
    """Build a result whose relevant mentions carry ``mention_facts`` as applicable facts."""
    mentions = [mention(f"{comment_id}:m{i}", *facts) for i, facts in enumerate(mention_facts)]
    return result_from(comment_id, mentions, [(m, tuple(m.facts)) for m in mentions])


def result_from(
    comment_id: str,
    all_mentions: Sequence[MentionView],
    relevant: Sequence[tuple[MentionView, tuple[FactView, ...]]],
) -> CommentResult:
    """Build a result whose comment has ``all_mentions`` but only ``relevant`` are matched."""
    return CommentResult(
        comment=comment_view(comment_id, all_mentions),
        tier=None,
        mention_matches=tuple(
            MentionMatch(mention=m, facts=facts, match=None) for m, facts in relevant
        ),
        region_status=None,
        explanation="",
        has_conditions=any(
            f.normalized_value is not None and f.key in COMPARABLE_KEYS
            for _, fs in relevant
            for f in fs
        ),
    )


def rows(counts: RegionCounts) -> list[tuple[str, str, str, int, bool]]:
    """Return the table as plain tuples for exact comparison."""
    return [
        (r.region_code, r.region_name, r.postal, int(r.n_comments), bool(r.suppressed))
        for r in counts.table.itertuples(index=False)
    ]


def cell(counts: RegionCounts, code: str) -> tuple[int, bool]:
    match = counts.table[counts.table["region_code"] == code]
    assert len(match) == 1, f"expected exactly one row for {code}"
    return int(match["n_comments"].iloc[0]), bool(match["suppressed"].iloc[0])


def walk_keys(node: object) -> Iterator[str]:
    """Yield every dict key anywhere in a nested JSON-like structure."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            yield from walk_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk_keys(item)


# --------------------------------------------------------------------------- region_counts


def test_unambiguous_state_fact_maps_to_that_state() -> None:
    counts = region_counts([result("c1", [region("US-CT")])], min_cell_count=1)

    assert rows(counts) == [("US-CT", "Connecticut", "CT", 1, False)]
    assert (counts.not_reported, counts.outside_us, counts.us_state_not_reported) == (0, 0, 0)
    assert counts.min_cell_count == 1


def test_zone_only_comment_never_maps_and_counts_as_not_reported() -> None:
    counts = region_counts([result("c1", [fact("zone", "6b")])], min_cell_count=1)

    assert counts.table.empty
    assert counts.not_reported == 1
    assert counts.outside_us == 0


def test_comment_without_relevant_mentions_counts_as_not_reported() -> None:
    counts = region_counts([result("c1")], min_cell_count=1)

    assert counts.table.empty
    assert counts.not_reported == 1


def test_display_only_region_without_normalized_value_is_not_reported() -> None:
    counts = region_counts([result("c1", [region(None)])], min_cell_count=1)

    assert counts.table.empty
    assert counts.not_reported == 1


def test_same_state_on_two_mentions_counts_the_comment_once() -> None:
    counts = region_counts(
        [result("c1", [region("US-CT", fact_id="a")], [region("US-CT", fact_id="b")])],
        min_cell_count=1,
    )

    assert rows(counts) == [("US-CT", "Connecticut", "CT", 1, False)]


def test_states_on_different_mentions_count_the_comment_once_for_each() -> None:
    counts = region_counts([result("c1", [region("US-CT")], [region("US-ME")])], min_cell_count=1)

    assert rows(counts) == [
        ("US-CT", "Connecticut", "CT", 1, False),
        ("US-ME", "Maine", "ME", 1, False),
    ]
    assert counts.not_reported == 0


def test_counts_are_distinct_comments_not_mentions() -> None:
    results = [
        result("c1", [region("US-CT")], [region("US-CT")], [region("US-CT")]),
        result("c2", [region("US-CT")]),
        result("c3", [region("US-CT")], [fact("zone", "6b")]),
    ]

    counts = region_counts(results, min_cell_count=1)

    assert cell(counts, "US-CT") == (3, False)


def test_only_relevant_mentions_applicable_facts_count() -> None:
    """SPEC §11 #4: a region on a non-relevant mention never places this report."""
    pepper = mention("c1:pepper", region("US-FL"))
    tomato = mention("c1:tomato", region("US-ME"))

    counts = region_counts(
        [result_from("c1", [pepper, tomato], [(tomato, tomato.facts)])], min_cell_count=1
    )

    assert rows(counts) == [("US-ME", "Maine", "ME", 1, False)]


def test_inherited_whole_comment_region_in_applicable_facts_counts() -> None:
    inherited = region("US-VT", applicability="whole_comment")
    owner = mention("c1:m0", inherited)
    sibling = mention("c1:m1")

    counts = region_counts(
        [result_from("c1", [owner, sibling], [(sibling, (inherited,))])], min_cell_count=1
    )

    assert rows(counts) == [("US-VT", "Vermont", "VT", 1, False)]


def test_non_us_regions_go_to_outside_us_and_never_on_the_map() -> None:
    results = [
        result("c1", [region("GB")]),
        result("c2", [region("CA-ON")], [region("CA-AB")]),
    ]

    counts = region_counts(results, min_cell_count=1)

    assert counts.table.empty
    assert counts.outside_us == 2
    assert counts.not_reported == 0
    assert choropleth(counts).data[0].locations == ()


def test_comment_with_a_us_state_and_a_foreign_region_is_not_outside_us() -> None:
    counts = region_counts([result("c1", [region("US-CT")], [region("GB")])], min_cell_count=1)

    assert rows(counts) == [("US-CT", "Connecticut", "CT", 1, False)]
    assert counts.outside_us == 0


def test_country_level_us_has_no_state_and_is_neither_outside_nor_unreported() -> None:
    results = [result("c1", [region("US")]), result("c2", [region("US")], [region("CA-ON")])]

    counts = region_counts(results, min_cell_count=1)

    assert counts.table.empty
    assert counts.us_state_not_reported == 2
    assert (counts.outside_us, counts.not_reported) == (0, 0)


def test_small_cells_are_suppressed_with_zero_count() -> None:
    results = [
        result("a1", [region("US-CT")]),
        result("a2", [region("US-CT")]),
        result("b1", [region("US-ME")]),
        result("b2", [region("US-ME")]),
        result("b3", [region("US-ME")]),
    ]

    counts = region_counts(results, min_cell_count=3)

    assert rows(counts) == [
        ("US-ME", "Maine", "ME", 3, False),
        ("US-CT", "Connecticut", "CT", 0, True),
    ]
    assert counts.min_cell_count == 3


def test_min_cell_count_one_suppresses_nothing() -> None:
    counts = region_counts([result("c1", [region("US-CT")])], min_cell_count=1)

    assert not counts.table["suppressed"].any()
    assert cell(counts, "US-CT") == (1, False)


def test_suppressed_rows_do_not_reveal_their_relative_sizes() -> None:
    """Rows sort on the displayed count, so hidden counts cannot be read from row order."""
    results = [result(f"tx{i}", [region("US-TX")]) for i in range(4)]
    results.append(result("az", [region("US-AZ")]))

    counts = region_counts(results, min_cell_count=5)

    assert rows(counts) == [
        ("US-AZ", "Arizona", "AZ", 0, True),
        ("US-TX", "Texas", "TX", 0, True),
    ]


def test_table_has_exact_columns_and_deterministic_sort() -> None:
    results = [
        result("c1", [region("US-ME")]),
        result("c2", [region("US-CT")]),
        result("c3", [region("US-TX")]),
        result("c4", [region("US-TX")]),
    ]

    counts = region_counts(results, min_cell_count=1)

    assert tuple(counts.table.columns) == TABLE_COLUMNS
    assert TABLE_COLUMNS == ("region_code", "region_name", "postal", "n_comments", "suppressed")
    assert [r[0] for r in rows(counts)] == ["US-TX", "US-CT", "US-ME"]
    assert list(counts.table.index) == [0, 1, 2]


def test_empty_input_gives_empty_typed_table_and_zero_counts() -> None:
    empty = region_counts([], min_cell_count=3)
    filled = region_counts([result("c1", [region("US-CT")])], min_cell_count=3)

    assert empty.table.empty
    assert tuple(empty.table.columns) == TABLE_COLUMNS
    assert empty.table.dtypes.to_dict() == filled.table.dtypes.to_dict()
    assert (empty.not_reported, empty.outside_us, empty.us_state_not_reported) == (0, 0, 0)


def test_district_of_columbia_maps_with_its_postal_code() -> None:
    counts = region_counts([result("c1", [region("US-DC")])], min_cell_count=1)

    assert rows(counts) == [("US-DC", "District of Columbia", "DC", 1, False)]


@pytest.mark.parametrize("bad", [0, -1])
def test_min_cell_count_below_one_raises(bad: int) -> None:
    with pytest.raises(ValueError, match="min_cell_count"):
        region_counts([], min_cell_count=bad)


def test_unknown_region_code_raises_without_echoing_the_value() -> None:
    bad = region("Hartford-ish SECRET", fact_id="f-bad")

    with pytest.raises(ValueError, match="f-bad") as excinfo:
        region_counts([result("c1", [bad])], min_cell_count=1)

    assert "SECRET" not in str(excinfo.value)
    assert "c1" in str(excinfo.value)


def test_duplicate_comment_ids_raise() -> None:
    with pytest.raises(ValueError, match="c1"):
        region_counts(
            [result("c1", [region("US-CT")]), result("c1", [region("US-CT")])], min_cell_count=1
        )


def test_region_counts_rejects_a_table_with_wrong_columns() -> None:
    with pytest.raises(ValueError, match="columns"):
        RegionCounts(
            table=pd.DataFrame({"state": ["CT"], "n": [1]}),
            not_reported=0,
            outside_us=0,
            us_state_not_reported=0,
            min_cell_count=1,
        )


def test_region_counts_compare_by_value() -> None:
    def counts(*results: CommentResult, k: int = 1) -> RegionCounts:
        return region_counts(list(results), min_cell_count=k)

    ct, me = result("c1", [region("US-CT")]), result("c2", [region("US-ME")])
    assert counts(ct, me) == counts(me, ct)
    assert counts(ct, me) != counts(ct)
    assert counts(ct, me) != counts(ct, me, k=2)
    assert counts() != "not counts"
    with pytest.raises(TypeError):
        hash(counts())


def test_region_counts_rejects_negative_bucket_counts() -> None:
    table = region_counts([], min_cell_count=1).table
    with pytest.raises(ValueError, match="non-negative"):
        RegionCounts(
            table=table,
            not_reported=-1,
            outside_us=0,
            us_state_not_reported=0,
            min_cell_count=1,
        )


# --------------------------------------------------------------------------- count_label


@pytest.mark.parametrize(
    ("n", "suppressed", "k", "expected"),
    [
        (1, False, 1, "1 report"),
        (3, False, 3, "3 reports"),
        (0, True, 3, "fewer than 3 reports"),
        (0, True, 2, "fewer than 2 reports"),
    ],
)
def test_count_label(n: int, suppressed: bool, k: int, expected: str) -> None:
    assert count_label(n, suppressed=suppressed, min_cell_count=k) == expected


# --------------------------------------------------------------------------- choropleth


def mixed_counts() -> RegionCounts:
    results = [
        result("a1", [region("US-CT")], [region("US-CT")]),
        result("a2", [region("US-CT")]),
        result("a3", [region("US-CT")]),
        result("b1", [region("US-ME")]),
        result("z1", [fact("zone", "6b")]),
        result("g1", [region("GB")]),
    ]
    return region_counts(results, min_cell_count=2)


def test_choropleth_has_a_single_usa_states_choropleth_trace() -> None:
    counts = mixed_counts()

    figure = choropleth(counts)

    assert isinstance(figure, go.Figure)
    assert [trace.type for trace in figure.data] == ["choropleth"]
    trace = figure.data[0]
    assert trace.locationmode == "USA-states"
    assert list(trace.locations) == ["CT", "ME"]
    assert list(trace.z) == [3, 0]
    assert figure.layout.geo.scope == "usa"


def test_choropleth_hover_shows_counts_and_hides_suppressed_values() -> None:
    trace = choropleth(mixed_counts()).data[0]

    assert list(trace.text) == ["Connecticut: 3 reports", "Maine: fewer than 2 reports"]
    assert "%{text}" in trace.hovertemplate
    assert "%{z}" not in trace.hovertemplate


def test_choropleth_never_plots_points_or_coordinates() -> None:
    figure = choropleth(mixed_counts())
    as_dict = json.loads(figure.to_json())

    trace_types = {trace["type"] for trace in as_dict["data"]}
    assert trace_types == {"choropleth"}
    keys = set(walk_keys(as_dict))
    assert not keys & {"lat", "lon", "latitude", "longitude"}
    # The default template styles every trace type; only actual traces matter here.
    assert "scattergeo" not in json.dumps(as_dict["data"]).lower()


def test_choropleth_never_contains_comment_text_or_quotes() -> None:
    serialized = choropleth(mixed_counts()).to_json()

    assert "SECRET" not in serialized


def test_choropleth_of_empty_input_still_renders() -> None:
    figure = choropleth(region_counts([], min_cell_count=3))

    assert [trace.type for trace in figure.data] == ["choropleth"]
    assert figure.data[0].locations == ()
    html = pio.to_html(figure, include_plotlyjs=False, full_html=False)
    assert "choropleth" in html
    assert pio.from_json(figure.to_json()).data[0].locationmode == "USA-states"


def test_choropleth_layout_is_static_and_borderless() -> None:
    layout = choropleth(mixed_counts()).layout

    assert layout.dragmode is False
    assert (layout.margin.l, layout.margin.r, layout.margin.t, layout.margin.b) == (0, 0, 0, 0)
    assert layout.geo.bgcolor == "rgba(0,0,0,0)"


def test_choropleth_color_range_starts_at_zero() -> None:
    trace = choropleth(mixed_counts()).data[0]

    assert trace.zmin == 0
    assert trace.zmax >= 3


# --------------------------------------------------------------------------- properties

_CODES = ("US-CT", "US-ME", "US-TX", "US-DC", "US", "GB", "CA-ON", None)
_KEYS: tuple[EnvKey, ...] = ("region", "region", "region", "zone")

_facts = st.lists(
    st.builds(
        lambda key, code, i: (
            fact("zone", "6b", fact_id=f"z{i}") if key == "zone" else region(code, fact_id=f"r{i}")
        ),
        st.sampled_from(_KEYS),
        st.sampled_from(_CODES),
        st.integers(0, 3),
    ),
    max_size=3,
)
_mention_fact_lists = st.lists(_facts, max_size=3)
_results = st.lists(_mention_fact_lists, max_size=8).map(
    lambda comments: [result(f"c{i}", *mfs) for i, mfs in enumerate(comments)]
)


def _codes(res: CommentResult) -> set[str]:
    return {
        f.normalized_value
        for mm in res.mention_matches
        for f in mm.facts
        if f.key == "region" and f.normalized_value is not None
    }


@given(_results)
def test_property_cells_equal_distinct_comments_naming_each_state(
    results: list[CommentResult],
) -> None:
    counts = region_counts(results, min_cell_count=1)

    expected = {
        code: sum(code in _codes(r) for r in results)
        for code in ("US-CT", "US-ME", "US-TX", "US-DC")
    }
    assert {code: n for code, n in expected.items() if n} == dict(
        zip(counts.table["region_code"], counts.table["n_comments"].tolist(), strict=True)
    )


@given(_results)
def test_property_every_comment_lands_in_exactly_one_bucket(results: list[CommentResult]) -> None:
    counts = region_counts(results, min_cell_count=1)

    mapped = sum(any(c.startswith("US-") for c in _codes(r)) for r in results)
    total = mapped + counts.not_reported + counts.outside_us + counts.us_state_not_reported
    assert total == len(results)


@given(_results, st.integers(1, 6))
def test_property_suppression_only_hides_small_cells(results: list[CommentResult], k: int) -> None:
    raw = region_counts(results, min_cell_count=1)
    true_n = dict(zip(raw.table["region_code"], raw.table["n_comments"].tolist(), strict=True))

    counts = region_counts(results, min_cell_count=k)

    assert set(counts.table["region_code"]) == set(true_n)
    for code, n, suppressed in zip(
        counts.table["region_code"],
        counts.table["n_comments"].tolist(),
        counts.table["suppressed"].tolist(),
        strict=True,
    ):
        assert suppressed == (true_n[code] < k)
        assert n == (0 if suppressed else true_n[code])
    assert counts.not_reported == raw.not_reported
    assert counts.outside_us == raw.outside_us


@given(_results, st.integers(1, 4))
def test_property_table_is_sorted_by_displayed_count_then_code(
    results: list[CommentResult], k: int
) -> None:
    table = region_counts(results, min_cell_count=k).table

    keys = list(zip((-n for n in table["n_comments"].tolist()), table["region_code"], strict=True))
    assert keys == sorted(keys)


@given(_results, st.randoms(use_true_random=False), st.integers(1, 4))
def test_property_result_order_does_not_change_counts(
    results: list[CommentResult], rnd: Any, k: int
) -> None:
    shuffled = list(results)
    rnd.shuffle(shuffled)

    first = region_counts(results, min_cell_count=k)
    second = region_counts(shuffled, min_cell_count=k)

    pd.testing.assert_frame_equal(first.table, second.table)
    assert (first.not_reported, first.outside_us, first.us_state_not_reported) == (
        second.not_reported,
        second.outside_us,
        second.us_state_not_reported,
    )


@given(_results)
def test_property_repeating_mentions_never_inflates_counts(results: list[CommentResult]) -> None:
    doubled = [r.model_copy(update={"mention_matches": r.mention_matches * 2}) for r in results]

    first = region_counts(results, min_cell_count=1)
    second = region_counts(doubled, min_cell_count=1)

    pd.testing.assert_frame_equal(first.table, second.table)
    assert first.not_reported == second.not_reported


@given(_results, st.integers(1, 4))
def test_property_figure_matches_table(results: list[CommentResult], k: int) -> None:
    counts = region_counts(results, min_cell_count=k)

    trace = choropleth(counts).data[0]

    assert list(trace.locations) == counts.table["postal"].tolist()
    assert list(trace.z) == counts.table["n_comments"].tolist()
    assert len(trace.text) == len(counts.table)
