"""Tests for deterministic, explainable environment matching (SPEC §5, §11 #2, #6-#11)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, get_args

import pytest
from hypothesis import given
from hypothesis import strategies as st

from crop_explorer.environment_match import (
    applicable_facts,
    compare_dimension,
    explain,
    match_context,
    region_status,
    tier_of,
    values_with,
)
from crop_explorer.regions import REGION_NAMES
from crop_explorer.schemas import ENV_KEYS, Applicability, Basis, EnvKey, Topic
from crop_explorer.views import (
    DimensionMatch,
    FactView,
    GardenContext,
    MatchResult,
    MatchStatus,
    MentionView,
)
from crop_explorer.vocab import (
    CATEGORICAL_VALUES,
    COMPARABLE_KEYS,
    VIEWER_VALUES,
    ZONE_VALUES,
    ComparableKey,
)

# --------------------------------------------------------------------------- builders


def fact(
    key: EnvKey,
    value: str | None,
    *,
    written: str | None = None,
    applicability: Applicability = "this_mention",
    fact_id: str | None = None,
    quote: str | None = None,
    basis: Basis = "reported_garden_context",
) -> FactView:
    """Build a FactView; ``written``/``quote`` default to text derived from the value."""
    as_written = written if written is not None else (value or "something unusual")
    return FactView(
        fact_id=fact_id or f"f:{key}:{value}:{as_written}:{applicability}",
        key=key,
        value_as_written=as_written,
        normalized_value=value,
        evidence_quote=quote if quote is not None else f"... {as_written} ...",
        applicability=applicability,
        basis=basis,
    )


def mention(
    mention_id: str,
    *facts: FactView,
    crop: str | None = "tomato",
    variety: str | None = None,
    topics: tuple[Topic, ...] = (),
    quote: str = "a mention",
) -> MentionView:
    """Build a MentionView carrying ``facts`` in the given order."""
    return MentionView(
        mention_id=mention_id,
        crop=crop,
        variety_as_written=variety,
        variety_key=variety.casefold() if variety else None,
        topics=topics,
        evidence_quote=quote,
        extractor_version="test-1",
        facts=facts,
    )


def statuses(result: MatchResult | None) -> dict[str, MatchStatus]:
    """Return ``{key: status}`` of a match result (empty for ``None``)."""
    return {} if result is None else {d.key: d.status for d in result.dimensions}


SPEC_GARDEN = GardenContext(zone="6b", method="container", humidity="humid")
"""The viewer selection used in SPEC §5's illustrative example table."""

# --------------------------------------------------------------------------- tier_of


@pytest.mark.parametrize(
    ("given_statuses", "expected"),
    [
        (["match"], "match"),
        (["match", "match", "match"], "match"),
        (["match", "not_stated"], "partial"),
        (["imprecise"], "partial"),
        (["imprecise", "match"], "partial"),
        (["imprecise", "not_stated"], "partial"),
        (["not_stated"], "not_stated"),
        (["not_stated", "not_stated"], "not_stated"),
        (["different"], "different"),
        (["match", "match", "different"], "different"),
        (["not_stated", "different"], "different"),
    ],
)
def test_tier_of_follows_contract(given_statuses: list[MatchStatus], expected: str) -> None:
    assert tier_of(given_statuses) == expected


def test_tier_of_empty_input_raises() -> None:
    with pytest.raises(ValueError, match="at least one"):
        tier_of([])


def test_tier_of_unknown_status_raises() -> None:
    bogus: Any = ["match", "great"]
    with pytest.raises(ValueError, match="great"):
        tier_of(bogus)


# --------------------------------------------------------------------------- compare_dimension


def test_compare_dimension_without_facts_is_not_stated() -> None:
    result = compare_dimension("method", "container", [])
    assert result == DimensionMatch(
        key="method",
        status="not_stated",
        viewer_value="container",
        reported_values=(),
        written_values=(),
        evidence=(),
    )


def test_compare_dimension_ignores_other_keys() -> None:
    facts = [fact("zone", "6b"), fact("region", "US-CT"), fact("humidity", "humid")]
    result = compare_dimension("method", "container", facts)
    assert result.status == "not_stated"
    assert result.evidence == ()


def test_unnormalized_fact_is_not_stated_never_different() -> None:
    weird = fact("method", None, written="a hugelkultur mound")
    result = compare_dimension("method", "container", [weird])
    assert result.status == "not_stated"
    assert result.reported_values == ()
    assert result.written_values == ("a hugelkultur mound",)
    assert result.evidence == ()
    assert match_context(GardenContext(method="container"), [weird]) == MatchResult(
        tier="not_stated", dimensions=(result,)
    )


def test_categorical_equal_matches_and_records_evidence() -> None:
    pots = fact("method", "container", written="pots")
    result = compare_dimension("method", "container", [pots])
    assert result.status == "match"
    assert result.reported_values == ("container",)
    assert result.evidence == (pots,)


def test_categorical_unequal_is_different() -> None:
    ground = fact("method", "in_ground", written="in the ground")
    result = compare_dimension("method", "container", [ground])
    assert result.status == "different"
    assert result.reported_values == ("in_ground",)


def test_multiple_values_any_match_wins() -> None:
    both = [
        fact("method", "container", written="pots"),
        fact("method", "in_ground", written="in-ground"),
    ]
    result = compare_dimension("method", "container", both)
    assert result.status == "match"
    assert result.reported_values == ("container", "in_ground")
    assert result.evidence == tuple(both)


def test_reported_values_distinct_in_first_seen_order() -> None:
    facts = [
        fact("method", "raised_bed", written="raised beds"),
        fact("method", "container", written="pots"),
        fact("method", "raised_bed", written="beds"),
        fact("method", None, written="hay bales"),
    ]
    result = compare_dimension("method", "in_ground", facts)
    assert result.reported_values == ("raised_bed", "container")
    assert result.written_values == ("hay bales",)
    assert result.evidence == tuple(facts[:3])


def test_display_only_phrases_are_written_values_in_first_seen_order() -> None:
    """F19: phrases the vocabulary cannot map are kept for display, never compared."""
    facts = [
        fact("soil", None, written="rocky hillside dirt"),
        fact("sun", None, written="dappled light"),
        fact("soil", None, written="clay-ish", fact_id="f2"),
        fact("soil", None, written="rocky hillside dirt", fact_id="f3"),
    ]
    result = compare_dimension("soil", "loam", facts)
    assert result.status == "not_stated"
    assert result.reported_values == ()
    assert result.written_values == ("rocky hillside dirt", "clay-ish")
    assert result.evidence == ()


def test_written_values_never_change_the_status() -> None:
    with_phrase = [fact("method", "container"), fact("method", None, written="hay bales")]
    without = [fact("method", "container")]
    for viewer in ("container", "in_ground"):
        assert compare_dimension("method", viewer, with_phrase).status == (
            compare_dimension("method", viewer, without).status
        )


@pytest.mark.parametrize(
    ("reported", "viewer", "expected"),
    [
        ("6b", "6b", "match"),
        ("6", "6", "match"),
        ("6", "6b", "imprecise"),
        ("6", "6a", "imprecise"),
        ("6b", "6", "imprecise"),
        ("6a", "6b", "different"),
        ("7a", "6b", "different"),
        ("5b", "6a", "different"),
        ("7", "6b", "different"),
        ("6b", "7", "different"),
        ("7", "6", "different"),
        ("10a", "1a", "different"),
        ("1", "10", "different"),
        ("11", "1", "different"),
    ],
)
def test_zone_precision(reported: str, viewer: str, expected: MatchStatus) -> None:
    assert compare_dimension("zone", viewer, [fact("zone", reported)]).status == expected


def test_coarse_zone_never_elevated_to_match() -> None:
    result = match_context(GardenContext(zone="6b"), [fact("zone", "6", written="zone 6")])
    assert result is not None
    assert result.tier == "partial"
    assert statuses(result) == {"zone": "imprecise"}


def test_zone_any_exact_value_beats_imprecise() -> None:
    facts = [fact("zone", "6"), fact("zone", "6b")]
    assert compare_dimension("zone", "6b", facts).status == "match"


def test_zone_imprecise_beats_different() -> None:
    facts = [fact("zone", "7a"), fact("zone", "6")]
    assert compare_dimension("zone", "6b", facts).status == "imprecise"


@pytest.mark.parametrize(
    ("reported", "viewer", "expected"),
    [
        ("hot", "hot", "match"),
        ("mild", "hot", "different"),
        ("cool", "mild", "different"),
        ("heatwave", "hot", "imprecise"),
        ("heatwave", "cool", "not_stated"),
        ("heatwave", "mild", "not_stated"),
    ],
)
def test_summer_heat_heatwave_is_an_event(
    reported: str, viewer: str, expected: MatchStatus
) -> None:
    assert compare_dimension("summer_heat", viewer, [fact("summer_heat", reported)]).status == (
        expected
    )


def test_ignored_heatwave_does_not_mask_a_difference() -> None:
    facts = [fact("summer_heat", "heatwave"), fact("summer_heat", "mild")]
    assert compare_dimension("summer_heat", "cool", facts).status == "different"


MILD_AND_HEATWAVE = [fact("summer_heat", "mild"), fact("summer_heat", "heatwave")]
HOT_AND_HEATWAVE = [fact("summer_heat", "hot"), fact("summer_heat", "heatwave")]


@pytest.mark.parametrize(
    ("facts", "viewer", "status", "expected"),
    [
        pytest.param(MILD_AND_HEATWAVE, "hot", "imprecise", ("heatwave",), id="hot-imprecise"),
        pytest.param(MILD_AND_HEATWAVE, "hot", "different", ("mild",), id="hot-different"),
        pytest.param(MILD_AND_HEATWAVE, "hot", "match", (), id="hot-match"),
        pytest.param(MILD_AND_HEATWAVE, "cool", "different", ("mild",), id="cool-different"),
        pytest.param(MILD_AND_HEATWAVE, "cool", "not_stated", ("heatwave",), id="cool-ignored"),
        pytest.param(HOT_AND_HEATWAVE, "mild", "different", ("hot",), id="mild-different"),
        pytest.param(HOT_AND_HEATWAVE, "mild", "not_stated", ("heatwave",), id="mild-ignored"),
        pytest.param(HOT_AND_HEATWAVE, "hot", "match", ("hot",), id="hot-match-only"),
        pytest.param(
            [fact("zone", "7a"), fact("zone", "6")], "6b", "imprecise", ("6",), id="zone-coarse"
        ),
        pytest.param(
            [fact("zone", "7a"), fact("zone", "6")], "6b", "different", ("7a",), id="zone-other"
        ),
    ],
)
def test_values_with_names_only_the_values_that_gave_a_status(
    facts: list[FactView], viewer: str, status: MatchStatus, expected: tuple[str, ...]
) -> None:
    """F21: a card labels only the reported values whose own comparison gave the status."""
    key: ComparableKey = "zone" if facts[0].key == "zone" else "summer_heat"
    assert values_with(compare_dimension(key, viewer, facts), status) == expected


def test_mild_summer_with_a_heatwave_stays_imprecise_for_a_hot_viewer() -> None:
    """F21: tier precedence is unchanged (match > imprecise > different)."""
    dimension = compare_dimension("summer_heat", "hot", MILD_AND_HEATWAVE)
    assert dimension.status == "imprecise"
    assert values_with(dimension, dimension.status) == ("heatwave",)


def test_values_with_rejects_an_unknown_status() -> None:
    dimension = compare_dimension("summer_heat", "hot", MILD_AND_HEATWAVE)
    bogus: Any = "great"
    with pytest.raises(ValueError, match="great"):
        values_with(dimension, bogus)


def test_compare_dimension_rejects_non_comparable_key() -> None:
    bad_key: Any = "region"
    with pytest.raises(ValueError, match="region"):
        compare_dimension(bad_key, "US-CT", [fact("region", "US-CT")])


@pytest.mark.parametrize(
    ("key", "viewer_value"),
    [("method", "pots"), ("zone", "6c"), ("summer_heat", "heatwave"), ("humidity", "")],
)
def test_compare_dimension_rejects_unselectable_viewer_value(
    key: ComparableKey, viewer_value: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(repr(viewer_value))):
        compare_dimension(key, viewer_value, [])


def test_compare_dimension_rejects_invalid_normalized_fact_value() -> None:
    corrupt = fact("zone", "zone 6b")
    with pytest.raises(ValueError, match="zone 6b"):
        compare_dimension("zone", "6b", [corrupt])


# --------------------------------------------------------------------------- match_context


def test_no_selection_gives_no_match_result() -> None:
    assert match_context(GardenContext(), [fact("zone", "6b")]) is None


def test_region_only_selection_gives_no_match_result() -> None:
    assert match_context(GardenContext(region="US-CT"), [fact("region", "US-CT")]) is None


def test_missing_is_not_mismatch() -> None:
    """SPEC §11 #6: a flavor-only comment is 'not stated' under any environment search."""
    garden = GardenContext(
        zone="6b", method="container", shelter="outdoors", summer_heat="hot", humidity="humid"
    )
    result = match_context(garden, [])
    assert result is not None
    assert result.tier == "not_stated"
    assert set(statuses(result).values()) == {"not_stated"}
    assert all(d.reported_values == () and d.evidence == () for d in result.dimensions)


def test_explainable_match() -> None:
    """SPEC §11 #7: container + zone 6b + humid reported → match on exactly those three."""
    facts = [
        fact("zone", "6b", written="zone 6b"),
        fact("method", "container", written="containers"),
        fact("humidity", "humid", written="muggy summers"),
    ]
    result = match_context(SPEC_GARDEN, facts)
    assert result is not None
    assert result.tier == "match"
    assert result.keys_with("match") == ("zone", "method", "humidity")
    text = explain(result)
    for label in ("zone 6b", "containers", "humid"):
        assert label in text
    assert "not stated" not in text


def test_unknowns_are_never_filled() -> None:
    """A garden dimension the report is silent on stays not_stated even when others match."""
    garden = GardenContext(zone="6b", method="container", humidity="humid", sun="full_sun")
    facts = [fact("zone", "6b"), fact("method", "container"), fact("humidity", "humid")]
    result = match_context(garden, facts)
    assert result is not None
    assert statuses(result)["sun"] == "not_stated"
    assert result.tier == "partial"
    assert "sun exposure not stated" in explain(result)


def test_partial_overlap() -> None:
    """SPEC §11 #8: matching containers with humidity missing → partial."""
    garden = GardenContext(method="container", humidity="humid")
    result = match_context(garden, [fact("method", "container", written="pots")])
    assert result is not None
    assert result.tier == "partial"
    assert statuses(result) == {"method": "match", "humidity": "not_stated"}
    assert explain(result) == "Matches your containers; humidity not stated."


def test_different_practice() -> None:
    """SPEC §11 #9: in-ground vs selected containers is different, and says so."""
    garden = GardenContext(method="container", humidity="humid")
    result = match_context(garden, [fact("method", "in_ground", written="in the ground")])
    assert result is not None
    assert result.tier == "different"
    assert explain(result) == "Different: in the ground (you: containers). Humidity not stated."


def test_dimensions_follow_canonical_key_order() -> None:
    garden = GardenContext(irrigation="drip", zone="5a", humidity="dry_air", method="container")
    result = match_context(garden, [])
    assert result is not None
    assert tuple(d.key for d in result.dimensions) == ("zone", "method", "humidity", "irrigation")
    assert tuple(d.viewer_value for d in result.dimensions) == (
        "5a",
        "container",
        "dry_air",
        "drip",
    )


def test_region_facts_never_change_the_tier() -> None:
    garden = GardenContext(region="US-CT", zone="6b")
    base = [fact("zone", "6b")]
    with_regions = [*base, fact("region", "US-VA"), fact("region", "GB")]
    assert match_context(garden, base) == match_context(garden, with_regions)
    result = match_context(garden, with_regions)
    assert result is not None
    assert result.tier == "match"


def test_growing_year_facts_never_change_the_tier() -> None:
    garden = GardenContext(method="container")
    base = [fact("method", "container")]
    assert match_context(garden, base) == match_context(
        garden, [*base, fact("growing_year", "2023")]
    )


def test_outcome_and_topic_words_never_influence_tier() -> None:
    """SPEC §11 #11: a failure report under matching conditions ranks like a success report."""
    failure = mention(
        "c1#m0",
        fact("method", "container", written="pots", quote="all died in pots, total failure"),
        fact("zone", "6b", written="zone 6b", quote="zone 6b, worst year ever"),
        topics=("disease", "weather_tolerance"),
        quote="Sungold died, total failure",
    )
    success = mention(
        "c2#m0",
        fact("method", "container", written="containers", quote="great in containers"),
        fact("zone", "6b", written="6b", quote="best ever here in 6b"),
        topics=("flavor", "productivity"),
        quote="Sungold was a great success",
    )
    garden = GardenContext(zone="6b", method="container")
    bad = match_context(garden, applicable_facts(failure, [failure]))
    good = match_context(garden, applicable_facts(success, [success]))
    assert bad is not None
    assert good is not None
    assert bad.tier == good.tier == "match"
    assert statuses(bad) == statuses(good)
    assert explain(bad) == explain(good)


@pytest.mark.parametrize(
    ("facts", "tier", "explanation"),
    [
        pytest.param(
            [
                fact("zone", "6b", written="Zone 6b"),
                fact("method", "container", written="containers"),
                fact("humidity", "humid", written="muggy summers"),
            ],
            "match",
            "Matches your zone 6b, containers and humid.",
            id="all-three-match",
        ),
        pytest.param(
            [fact("method", "container", written="pots")],
            "partial",
            "Matches your containers; hardiness zone and humidity not stated.",
            id="pots-only",
        ),
        pytest.param(
            [],
            "not_stated",
            "None of your selected conditions were stated.",
            id="terrific-tomatoes",
        ),
        pytest.param(
            [
                fact("method", "in_ground", written="in the ground"),
                fact("humidity", "dry_air", written="dry conditions"),
            ],
            "different",
            "Different: in the ground (you: containers), dry air (you: humid). "
            "Hardiness zone not stated.",
            id="ground-and-dry",
        ),
    ],
)
def test_spec_section_5_example_table(facts: list[FactView], tier: str, explanation: str) -> None:
    result = match_context(SPEC_GARDEN, facts)
    assert result is not None
    assert result.tier == tier
    assert explain(result) == explanation


def test_spec_example_different_row_lists_both_differences() -> None:
    facts = [fact("method", "in_ground"), fact("humidity", "dry_air")]
    result = match_context(SPEC_GARDEN, facts)
    assert result is not None
    assert result.keys_with("different") == ("method", "humidity")
    assert result.keys_with("not_stated") == ("zone",)


# --------------------------------------------------------------------------- explain


def test_explain_none_is_empty() -> None:
    assert explain(None) == ""


def test_explain_coarse_reported_zone() -> None:
    result = match_context(GardenContext(zone="6b", method="container"), [fact("zone", "6")])
    assert explain(result) == "Zone 6 reported (subzone unknown); growing method not stated."


def test_explain_reported_subzone_finer_than_viewer() -> None:
    result = match_context(GardenContext(zone="6"), [fact("zone", "6b")])
    assert result is not None
    assert result.tier == "partial"
    assert explain(result) == "Zone 6b reported (you: zone 6)."


def test_explain_heatwave_against_hot() -> None:
    result = match_context(GardenContext(summer_heat="hot"), [fact("summer_heat", "heatwave")])
    assert explain(result) == "A heatwave reported (you: hot summers)."


def test_explain_heatwave_not_comparable_with_cool() -> None:
    garden = GardenContext(zone="6b", summer_heat="cool")
    result = match_context(garden, [fact("summer_heat", "heatwave")])
    assert result is not None
    assert result.tier == "not_stated"
    assert explain(result) == (
        "Summer heat not comparable (a heatwave reported); hardiness zone not stated."
    )


def test_explain_different_plus_match() -> None:
    garden = GardenContext(zone="6b", method="container", humidity="humid")
    facts = [fact("zone", "6b"), fact("method", "raised_bed")]
    assert explain(match_context(garden, facts)) == (
        "Different: raised beds (you: containers). Matches your zone 6b; humidity not stated."
    )


def test_explain_different_only() -> None:
    result = match_context(GardenContext(shelter="outdoors"), [fact("shelter", "greenhouse")])
    assert explain(result) == "Different: greenhouse / tunnel (you: outdoors)."


def test_explain_lists_every_differing_value() -> None:
    facts = [fact("method", "in_ground"), fact("method", "raised_bed")]
    result = match_context(GardenContext(method="container"), facts)
    assert explain(result) == "Different: in the ground and raised beds (you: containers)."


def test_explain_two_matches_and_two_unknowns() -> None:
    garden = GardenContext(zone="7a", method="raised_bed", sun="full_sun", soil="clay")
    facts = [fact("zone", "7a"), fact("method", "raised_bed")]
    assert explain(match_context(garden, facts)) == (
        "Matches your zone 7a and raised beds; sun exposure and soil not stated."
    )


def test_explain_names_display_only_phrases_instead_of_not_stated() -> None:
    """F19: a condition stated in words the app cannot map is mentioned, not "not stated"."""
    garden = GardenContext(method="container", humidity="humid", soil="loam")
    facts = [fact("method", "container"), fact("soil", None, written="rocky hillside dirt")]
    result = match_context(garden, facts)
    assert result is not None
    assert result.tier == "partial"
    assert explain(result) == (
        'Matches your containers; soil mentioned as "rocky hillside dirt" (not comparable); '
        "humidity not stated."
    )


def test_explain_display_only_phrases_alone_are_not_nothing_stated() -> None:
    garden = GardenContext(zone="6b", humidity="humid", soil="loam")
    facts = [
        fact("zone", None, written="zones 6-7"),
        fact("humidity", None, written="not humid"),
        fact("soil", None, written="sandy loam"),
        fact("soil", None, written="a bit\n  of  clay"),
    ]
    result = match_context(garden, facts)
    assert result is not None
    assert result.tier == "not_stated"
    assert explain(result) == (
        'Hardiness zone mentioned as "zones 6-7" (not comparable), humidity mentioned as '
        '"not humid" (not comparable), soil mentioned as "sandy loam" and "a bit of clay" '
        "(not comparable)."
    )


def test_explain_values_with_their_own_status_only() -> None:
    """F21: the explanation already names only the values that produced each status."""
    hot = match_context(GardenContext(summer_heat="hot"), MILD_AND_HEATWAVE)
    assert explain(hot) == "A heatwave reported (you: hot summers)."
    mild = match_context(GardenContext(summer_heat="mild"), HOT_AND_HEATWAVE)
    assert explain(mild) == "Different: hot summers (you: mild summers)."


# --------------------------------------------------------------------------- region_status


def test_region_status_none_without_garden_region() -> None:
    assert region_status(None, [fact("region", "US-CT")]) is None


def test_region_status_same() -> None:
    facts = [fact("region", "US-VA"), fact("region", "US-CT")]
    assert region_status("US-CT", facts) == "same"


def test_region_status_elsewhere() -> None:
    assert region_status("US-CT", [fact("region", "US-VA")]) == "elsewhere"


def test_region_status_not_stated() -> None:
    facts = [fact("zone", "6b"), fact("region", None, written="up north")]
    assert region_status("US-CT", facts) == "not_stated"


@pytest.mark.parametrize(
    ("garden_region", "reported", "expected"),
    [
        pytest.param("US", ["US-CT"], "same_country", id="us-state"),
        pytest.param("CA", ["CA-ON", "US-MN"], "same_country", id="ca-province"),
        pytest.param("US", ["US-CT", "US"], "same", id="country-report-is-same"),
        pytest.param("GB", ["GB"], "same", id="other-country"),
        pytest.param("US", ["CA-ON"], "elsewhere", id="other-country-subdivision"),
        pytest.param("CA", ["US"], "elsewhere", id="other-country-code"),
        pytest.param("US-CT", ["US"], "elsewhere", id="state-viewer-country-report"),
        pytest.param("US-CT", ["US-NY"], "elsewhere", id="neighbouring-state"),
    ],
)
def test_region_status_same_country(
    garden_region: str, reported: list[str], expected: str
) -> None:
    """F34: a viewer who picks a country prefers reports from its states or provinces."""
    facts = [fact("region", code) for code in reported]
    assert region_status(garden_region, facts) == expected


def test_region_status_unknown_garden_region_raises() -> None:
    with pytest.raises(ValueError, match="Connecticut"):
        region_status("Connecticut", [])


def test_region_status_rejects_invalid_region_fact() -> None:
    with pytest.raises(ValueError, match="Atlantis"):
        region_status("US-CT", [fact("region", "Atlantis")])


# --------------------------------------------------------------------------- applicable_facts


def test_this_mention_fact_never_leaks_to_sibling() -> None:
    """SPEC §11 #2: greenhouse peppers and outdoor tomatoes don't inherit each other's setting."""
    peppers = mention("c#m0", fact("shelter", "greenhouse"), crop="pepper")
    tomatoes = mention("c#m1", fact("shelter", "outdoors"), crop="tomato")
    siblings = [peppers, tomatoes]
    assert applicable_facts(tomatoes, siblings) == tomatoes.facts
    assert applicable_facts(peppers, siblings) == peppers.facts


def test_this_mention_fact_does_not_fill_an_unstated_sibling_key() -> None:
    peppers = mention("c#m0", fact("shelter", "greenhouse"), crop="pepper")
    beans = mention("c#m1", crop="bean")
    assert applicable_facts(beans, [peppers, beans]) == ()


def test_whole_comment_fact_flows_to_sibling_lacking_the_key() -> None:
    zone = fact("zone", "6b", applicability="whole_comment")
    sungold = mention("c#m0", zone, fact("method", "container"))
    cherokee = mention("c#m1", fact("method", "in_ground"))
    assert applicable_facts(cherokee, [sungold, cherokee]) == (*cherokee.facts, zone)


def test_own_fact_wins_over_sibling_whole_comment_fact() -> None:
    sibling_zone = fact("zone", "6b", applicability="whole_comment")
    own_zone = fact("zone", "7a")
    first = mention("c#m0", sibling_zone)
    second = mention("c#m1", own_zone)
    assert applicable_facts(second, [first, second]) == (own_zone,)


def test_own_unnormalized_fact_still_states_the_key() -> None:
    sibling_zone = fact("zone", "6b", applicability="whole_comment")
    own_vague = fact("zone", None, written="a cold pocket")
    first = mention("c#m0", sibling_zone)
    second = mention("c#m1", own_vague)
    assert applicable_facts(second, [first, second]) == (own_vague,)


def test_applicable_facts_deduplicates_and_orders() -> None:
    zone_a = fact("zone", "6b", written="zone 6b", applicability="whole_comment", fact_id="a")
    zone_b = fact("zone", "6b", written="zone 6b", applicability="whole_comment", fact_id="b")
    humid = fact("humidity", "humid", applicability="whole_comment", fact_id="h")
    region = fact("region", "US-CT", applicability="whole_comment", fact_id="r")
    own = fact("method", "container", fact_id="own")
    target = mention("c#m2", own)
    siblings = [mention("c#m0", zone_a, humid), mention("c#m1", zone_b, region), target]
    assert applicable_facts(target, siblings) == (own, zone_a, humid, region)


def test_applicable_facts_deduplicates_own_facts() -> None:
    first = fact("method", "container", written="pots", fact_id="1")
    again = fact("method", "container", written="pots", fact_id="2")
    other = fact("method", "container", written="containers", fact_id="3")
    target = mention("c#m0", first, again, other)
    assert applicable_facts(target, [target]) == (first, other)


def test_applicable_facts_works_without_self_in_siblings() -> None:
    zone = fact("zone", "6b", applicability="whole_comment")
    sungold = mention("c#m0", zone)
    cherokee = mention("c#m1")
    assert applicable_facts(cherokee, [sungold]) == (zone,)
    assert applicable_facts(cherokee, []) == ()


def test_own_whole_comment_fact_is_not_duplicated_via_self_in_siblings() -> None:
    zone = fact("zone", "6b", applicability="whole_comment")
    sungold = mention("c#m0", zone)
    assert applicable_facts(sungold, [sungold]) == (zone,)


def test_inherited_facts_feed_matching() -> None:
    """Whole-comment context reaches a sibling mention's match; this-mention context does not."""
    peppers = mention(
        "c#m0",
        fact("zone", "6b", applicability="whole_comment"),
        fact("shelter", "greenhouse"),
        crop="pepper",
    )
    tomatoes = mention("c#m1", crop="tomato")
    garden = GardenContext(zone="6b", shelter="outdoors")
    result = match_context(garden, applicable_facts(tomatoes, [peppers, tomatoes]))
    assert statuses(result) == {"zone": "match", "shelter": "not_stated"}


# --------------------------------------------------------------------------- strategies


def _normalized_values(key: EnvKey) -> st.SearchStrategy[str]:
    if key == "region":
        return st.sampled_from(sorted(REGION_NAMES))
    if key == "zone":
        return st.sampled_from(ZONE_VALUES)
    if key == "growing_year":
        return st.integers(min_value=1990, max_value=2030).map(str)
    return st.sampled_from(CATEGORICAL_VALUES[key])


_TEXT = st.text(min_size=1, max_size=20)


@st.composite
def fact_views(draw: st.DrawFn, keys: Sequence[EnvKey] = ENV_KEYS) -> FactView:
    key = draw(st.sampled_from(keys))
    return FactView(
        fact_id=draw(_TEXT),
        key=key,
        value_as_written=draw(_TEXT),
        normalized_value=draw(st.none() | _normalized_values(key)),
        evidence_quote=draw(_TEXT),
        applicability=draw(st.sampled_from(get_args(Applicability))),
        basis=draw(st.sampled_from(get_args(Basis))),
    )


@st.composite
def gardens(draw: st.DrawFn, min_selected: int = 0) -> GardenContext:
    keys = draw(st.sets(st.sampled_from(COMPARABLE_KEYS), min_size=min_selected))
    values = {key: draw(st.sampled_from(VIEWER_VALUES[key])) for key in sorted(keys)}
    region = draw(st.none() | st.sampled_from(sorted(REGION_NAMES)))
    return GardenContext(region=region, **values)


@st.composite
def mention_views(draw: st.DrawFn, mention_id: str) -> MentionView:
    facts = draw(st.lists(fact_views(), max_size=6))
    return mention(mention_id, *facts)


FACT_LISTS = st.lists(fact_views(), max_size=10)


def _order_free(result: MatchResult | None) -> object:
    """Project a result onto everything that must not depend on fact order."""
    if result is None:
        return None
    dims = tuple(
        (
            d.key,
            d.status,
            d.viewer_value,
            frozenset(d.reported_values),
            frozenset(d.written_values),
            len(d.evidence),
        )
        for d in result.dimensions
    )
    return result.tier, dims


# --------------------------------------------------------------------------- properties


@given(garden=gardens(), facts=FACT_LISTS, data=st.data())
def test_property_fact_order_never_changes_the_comparison(
    garden: GardenContext, facts: list[FactView], data: st.DataObject
) -> None:
    shuffled = data.draw(st.permutations(facts))
    assert _order_free(match_context(garden, shuffled)) == _order_free(
        match_context(garden, facts)
    )


@given(garden=gardens(), facts=FACT_LISTS, extra=FACT_LISTS)
def test_property_facts_of_unselected_keys_never_change_the_result(
    garden: GardenContext, facts: list[FactView], extra: list[FactView]
) -> None:
    selected = garden.selected()
    unrelated = [f for f in extra if f.key not in selected]
    assert match_context(garden, [*facts, *unrelated]) == match_context(garden, facts)


@given(garden=gardens(min_selected=1), facts=FACT_LISTS)
def test_property_match_tier_iff_every_dimension_matches(
    garden: GardenContext, facts: list[FactView]
) -> None:
    result = match_context(garden, facts)
    assert result is not None
    every_match = all(d.status == "match" for d in result.dimensions)
    assert (result.tier == "match") == every_match


@given(garden=gardens(min_selected=1), facts=FACT_LISTS)
def test_property_tier_is_tier_of_dimension_statuses(
    garden: GardenContext, facts: list[FactView]
) -> None:
    result = match_context(garden, facts)
    assert result is not None
    assert tuple(d.key for d in result.dimensions) == tuple(garden.selected())
    assert result.tier == tier_of([d.status for d in result.dimensions])


@given(
    garden=gardens(),
    facts=FACT_LISTS,
    regions=st.lists(fact_views(keys=("region",)), max_size=4),
)
def test_property_region_facts_are_irrelevant_to_matching(
    garden: GardenContext, facts: list[FactView], regions: list[FactView]
) -> None:
    without = [f for f in facts if f.key != "region"]
    assert match_context(garden, [*facts, *regions]) == match_context(garden, without)


@given(garden=gardens(min_selected=1), facts=FACT_LISTS, data=st.data())
def test_property_only_key_and_normalized_value_matter(
    garden: GardenContext, facts: list[FactView], data: st.DataObject
) -> None:
    """Outcome words in quotes/as-written text never move a report between tiers.

    Only a display-only fact's own words are quoted by ``explain`` (F19), so rewording a
    normalized fact never changes the explanation either.
    """
    outcome_words = st.sampled_from(["died", "great", "failure"])
    reworded = [
        f.model_copy(
            update={
                "value_as_written": data.draw(outcome_words),
                "evidence_quote": data.draw(_TEXT),
                "basis": data.draw(st.sampled_from(get_args(Basis))),
            }
        )
        for f in facts
    ]
    before = match_context(garden, facts)
    after = match_context(garden, reworded)
    assert before is not None
    assert after is not None
    assert after.tier == before.tier
    assert statuses(after) == statuses(before)
    normalized_reworded = [
        new if old.normalized_value is not None else old.model_copy(update={"basis": new.basis})
        for old, new in zip(facts, reworded, strict=True)
    ]
    assert explain(match_context(garden, normalized_reworded)) == explain(before)


_FORBIDDEN = re.compile(r"best|recommend|score|%|success|quality|worse|better", re.IGNORECASE)


@given(garden=gardens(), facts=FACT_LISTS)
def test_property_explain_never_ranks_or_scores(
    garden: GardenContext, facts: list[FactView]
) -> None:
    text = explain(match_context(garden, _neutral_words(facts)))
    assert _FORBIDDEN.search(text) is None
    assert (text == "") == (not garden.selected())


def _neutral_words(facts: Sequence[FactView]) -> list[FactView]:
    """Give every fact the same neutral as-written words (explain quotes display-only ones)."""
    return [f.model_copy(update={"value_as_written": "as written"}) for f in facts]


_PHRASES = st.text(alphabet="ab ;.\n\t", min_size=1, max_size=12).filter(str.strip)


@given(garden=gardens(min_selected=1), facts=FACT_LISTS)
def test_property_explain_is_one_or_two_sentences(
    garden: GardenContext, facts: list[FactView]
) -> None:
    text = explain(match_context(garden, _neutral_words(facts)))
    assert text[0].isupper()
    assert text.endswith(".")
    assert "  " not in text
    assert ";." not in text


@given(garden=gardens(min_selected=1), facts=FACT_LISTS, data=st.data())
def test_property_explain_quotes_display_only_phrases_of_unstated_keys(
    garden: GardenContext, facts: list[FactView], data: st.DataObject
) -> None:
    """F19: every display-only phrase of a selected, otherwise unstated key is quoted."""
    phrased = [f.model_copy(update={"value_as_written": data.draw(_PHRASES)}) for f in facts]
    result = match_context(garden, phrased)
    assert result is not None
    text = explain(result)
    assert text.endswith(".")
    assert "  " not in text
    assert "\n" not in text
    for dimension in result.dimensions:
        if dimension.status == "not_stated" and not dimension.reported_values:
            for phrase in dimension.written_values:
                assert f'"{" ".join(phrase.split())}"' in text


@given(garden=gardens(min_selected=1), facts=FACT_LISTS)
def test_property_values_with_partitions_the_reported_values(
    garden: GardenContext, facts: list[FactView]
) -> None:
    """F21: each reported value has exactly one own status; the dimension status has values."""
    result = match_context(garden, facts)
    assert result is not None
    for dimension in result.dimensions:
        groups = {status: values_with(dimension, status) for status in get_args(MatchStatus)}
        flat = [value for values in groups.values() for value in values]
        assert sorted(flat) == sorted(dimension.reported_values)
        if dimension.status != "not_stated":
            assert groups[dimension.status]


@given(target=mention_views("c#m0"), others=st.lists(mention_views("c#mX"), max_size=3))
def test_property_applicable_facts_scoping(target: MentionView, others: list[MentionView]) -> None:
    siblings = [mention(f"c#m{i + 1}", *m.facts) for i, m in enumerate(others)]
    result = applicable_facts(target, [target, *siblings])
    own_keys = {f.key for f in target.facts}
    sibling_facts = [f for s in siblings for f in s.facts]
    for f in result:
        if f in target.facts:
            continue
        assert f.applicability == "whole_comment"
        assert f.key not in own_keys
        assert f in sibling_facts
    identities = [(f.key, f.normalized_value, f.value_as_written) for f in result]
    assert len(identities) == len(set(identities))
    assert applicable_facts(target, siblings) == result


@given(target=mention_views("c#m0"), others=st.lists(mention_views("c#mX"), max_size=3))
def test_property_every_own_identity_is_kept_first(
    target: MentionView, others: list[MentionView]
) -> None:
    siblings = [mention(f"c#m{i + 1}", *m.facts) for i, m in enumerate(others)]
    result = applicable_facts(target, siblings)
    own_identities = list(
        dict.fromkeys((f.key, f.normalized_value, f.value_as_written) for f in target.facts)
    )
    head = [(f.key, f.normalized_value, f.value_as_written) for f in result[: len(own_identities)]]
    assert head == own_identities


@given(garden_region=st.sampled_from(sorted(REGION_NAMES)), facts=FACT_LISTS)
def test_property_region_status_consistent(garden_region: str, facts: list[FactView]) -> None:
    reported = {f.normalized_value for f in facts if f.key == "region"} - {None}
    in_country = any(code.startswith(f"{garden_region}-") for code in reported if code)
    expected = (
        "same"
        if garden_region in reported
        else "same_country"
        if in_country
        else "elsewhere"
        if reported
        else "not_stated"
    )
    assert region_status(garden_region, facts) == expected


@given(
    garden=gardens(min_selected=1),
    facts=FACT_LISTS,
    country=st.sampled_from(["US", "CA", "GB"]),
)
def test_property_region_never_changes_the_tier(
    garden: GardenContext, facts: list[FactView], country: str
) -> None:
    """F34: a country, state or no region at all never changes a comparison."""
    without = garden.model_copy(update={"region": None})
    with_country = garden.model_copy(update={"region": country})
    assert match_context(with_country, facts) == match_context(without, facts)
