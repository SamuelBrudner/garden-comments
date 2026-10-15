"""Tests for :mod:`crop_explorer.normalization` (synonym mapping onto the vocabulary)."""

from __future__ import annotations

import itertools
import re
import string

import pytest
from hypothesis import given
from hypothesis import strategies as st

from crop_explorer.normalization import (
    CROP_SYNONYMS,
    STRICT_KEYS,
    WrittenSpan,
    mentions_crop,
    normalize_crop,
    normalize_written,
    parse_zone,
    resolve_spans,
    variety_key,
)
from crop_explorer.regions import CA_PROVINCES, REGION_NAMES, US_STATES
from crop_explorer.schemas import ENV_KEYS, EnvKey
from crop_explorer.vocab import (
    CATEGORICAL_VALUES,
    VALUE_LABELS,
    ZONE_VALUES,
    is_valid_normalized,
)

NON_REGION_KEYS: tuple[EnvKey, ...] = tuple(k for k in ENV_KEYS if k != "region")
CATEGORICAL_KEYS: tuple[EnvKey, ...] = tuple(k for k in ENV_KEYS if k in CATEGORICAL_VALUES)
POSTAL_CODES: dict[str, str] = {
    code.split("-")[1]: code for code in (*US_STATES.keys(), *CA_PROVINCES.keys())
}
ZONE_PREFIXES = ("zone ", "Zone ", "ZONE ", "z", "Z", "USDA zone ", "hardiness zone ", "zone")

# Realistic phrase fragments (including tricky ones) that Hypothesis recombines freely.
FRAGMENTS = (
    "zone 6b",
    "Zone 7A",
    "z5",
    "zones 6-7",
    "6b/7a",
    "2023",
    "1999",
    "2022-2023",
    "Connecticut",
    "Hartford, CT",
    "CT",
    ", OK",
    "it was OK",
    "Washington, D.C.",
    "West Virginia",
    "New Mexico",
    "Ontario, Canada",
    "USA",
    "England",
    "pots",
    "raised beds",
    "in the ground",
    "greenhouse",
    "inside",
    "heat wave",
    "hot",
    "low humidity",
    "humid",
    "no rain",
    "rain",
    "part shade",
    "shade",
    "clay",
    "sandy",
    "loam",
    "drip",
    "by hand",
    "hot peppers",
    "kratky",
    "full sun",
    "not",
    "never",
    "and",
    ",",
    ".",
    "upstate NY",
    "Perth, WA",
    "Lake Michigan",
    "Kansas City",
    "West VA",
    "Sunset zone 9",
    "zn 5",
    "some shade",
    "started under lights",
    "lack of",
    "humidity",
    "heat",
    "it\u2019s",
    "rained",
    "zone 14",
)
fragment_lists = st.lists(st.sampled_from(FRAGMENTS), max_size=6)
env_keys = st.sampled_from(ENV_KEYS)
separators = st.sampled_from([" ", "  ", "\t", " \n ", "\u00a0", ", ", " - "])


@st.composite
def fragment_texts(draw: st.DrawFn) -> str:
    """Realistic fragments joined by assorted whitespace and punctuation runs."""
    parts = draw(fragment_lists)
    text = draw(st.sampled_from(["", " ", "\n"]))
    for part in parts:
        text += part + draw(separators)
    return text


# --- STRICT_KEYS


def test_strict_keys_are_region_zone_and_growing_year() -> None:
    assert frozenset({"region", "zone", "growing_year"}) == STRICT_KEYS


# --- normalize_written: general contract


@pytest.mark.parametrize("key", ENV_KEYS)
@pytest.mark.parametrize("written", ["", "   ", "\n\t", "nothing relevant here"])
def test_unrecognised_text_returns_none_for_every_key(key: EnvKey, written: str) -> None:
    assert normalize_written(key, written) is None


def test_unknown_key_fails_loudly() -> None:
    with pytest.raises(ValueError, match="key"):
        normalize_written("altitude", "zone 6b")  # type: ignore[arg-type]


@given(key=env_keys, written=st.text())
def test_any_text_yields_none_or_a_valid_vocabulary_value(key: EnvKey, written: str) -> None:
    result = normalize_written(key, written)
    assert result is None or is_valid_normalized(key, result)


@given(key=env_keys, parts=fragment_lists)
def test_phrase_soup_yields_none_or_a_valid_vocabulary_value(
    key: EnvKey, parts: list[str]
) -> None:
    result = normalize_written(key, " ".join(parts))
    assert result is None or is_valid_normalized(key, result)


@given(
    key=env_keys,
    parts=fragment_lists,
    separator=st.sampled_from(["  ", "\t", " \n ", "\u00a0 "]),
)
def test_runs_of_whitespace_are_collapsed(key: EnvKey, parts: list[str], separator: str) -> None:
    assert normalize_written(key, separator.join(parts)) == normalize_written(key, " ".join(parts))


@given(key=st.sampled_from(NON_REGION_KEYS), parts=fragment_lists)
def test_non_region_matching_ignores_case(key: EnvKey, parts: list[str]) -> None:
    text = " ".join(parts)
    assert normalize_written(key, text.upper()) == normalize_written(key, text.lower())


@pytest.mark.parametrize("key", NON_REGION_KEYS)
@pytest.mark.parametrize("name", sorted(REGION_NAMES.values()))
def test_region_names_never_imply_other_conditions(key: EnvKey, name: str) -> None:
    # Never infer humidity, heat, soil, zone, ... from a place name.
    assert normalize_written(key, f"in {name}") is None


# --- resolve_spans


def _slices(text: str, spans: tuple[WrittenSpan, ...]) -> list[tuple[str, str | None, str]]:
    return [(text[s.start : s.end], s.value, s.kind) for s in spans]


@pytest.mark.parametrize(
    ("key", "text", "expected"),
    [
        ("humidity", "it wasn't humid", [("humid", "humid", "negated")]),
        (
            "humidity",
            "not humid at all, dry heat",
            [("humid", "humid", "negated"), ("dry heat", "dry_air", "value")],
        ),
        ("shelter", "we had no greenhouse", [("greenhouse", "greenhouse", "negated")]),
        ("soil", "Sungold in clay pots", [("clay pots", None, "blocked")]),
        ("summer_heat", "Hot peppers loved it", [("Hot peppers", None, "blocked")]),
        ("region", "in West Virginia", [("West Virginia", "US-WV", "value")]),
        (
            "region",
            "Kansas City, Missouri",
            [("Kansas City", None, "blocked"), ("Missouri", "US-MO", "value")],
        ),
        ("region", "Here in Perth, WA our summers", [("Perth, WA", None, "blocked")]),
        ("region", "Zone 7a, upstate NY.", [("upstate NY", "US-NY", "value")]),
        ("zone", "I'm right on the zone 6b/7a line", [("zone 6b/7a", None, "value")]),
        ("zone", "zones 6-7", [("zones 6-7", None, "value")]),
        ("zone", "zone 14", [("zone 14", None, "value")]),
        ("zone", "Sunset zone 9 here", [("Sunset zone 9", None, "blocked")]),
        ("zone", "not zone 5", [("zone 5", "5", "negated")]),
        ("growing_year", "in 2022-2023", [("2022-2023", None, "value")]),
        ("growing_year", "summer of 2023", [("2023", "2023", "value")]),
        (
            "method",
            "Sungold in pots and Juliet in raised beds",
            [("pots", "container", "value"), ("raised beds", "raised_bed", "value")],
        ),
        (
            "sun",
            "gets shade in the afternoon",
            [("shade in the afternoon", "part_shade", "value")],
        ),
        ("humidity", "nothing relevant", []),
    ],
)
def test_resolve_spans_examples(
    key: EnvKey, text: str, expected: list[tuple[str, str | None, str]]
) -> None:
    assert _slices(text, resolve_spans(key, text)) == expected


def test_resolve_spans_offsets_point_into_the_original_text() -> None:
    text = (
        "  It was\u00a0 not\n\nhumid at\tall,  but a  heat\n wave  hit \u2018hard\u2019"
        " in Hartford,CT."
    )
    humid = text.index("humid")
    assert resolve_spans("humidity", text) == (WrittenSpan(humid, humid + 5, "humid", "negated"),)
    wave = text.index("heat\n wave")
    assert resolve_spans("summer_heat", text) == (
        WrittenSpan(wave, wave + len("heat\n wave"), "heatwave", "value"),
    )
    ct = text.index("CT")
    assert resolve_spans("region", text) == (WrittenSpan(ct, ct + 2, "US-CT", "value"),)


def test_written_span_is_frozen() -> None:
    span = WrittenSpan(0, 5, "humid", "value")
    with pytest.raises(AttributeError):
        span.start = 1  # type: ignore[misc]


def test_resolve_spans_rejects_an_unknown_key() -> None:
    with pytest.raises(ValueError, match="key"):
        resolve_spans("altitude", "zone 6b")  # type: ignore[arg-type]


@given(key=env_keys, text=st.one_of(fragment_texts(), st.text()))
def test_resolved_spans_are_ordered_disjoint_and_well_formed(key: EnvKey, text: str) -> None:
    spans = resolve_spans(key, text)
    for span in spans:
        assert 0 <= span.start < span.end <= len(text)
        assert span.kind in {"value", "negated", "blocked"}
        assert span.kind != "blocked" or span.value is None
        assert span.value is None or is_valid_normalized(key, span.value)
    for left, right in itertools.pairwise(spans):
        assert left.end <= right.start


@given(key=env_keys, text=fragment_texts())
def test_each_span_is_the_matched_phrase_region_of_the_original_text(
    key: EnvKey, text: str
) -> None:
    for span in resolve_spans(key, text):
        phrase = text[span.start : span.end]
        assert phrase == phrase.strip()
        # On its own, the region resolves to exactly that phrase with the same meaning (a
        # negation cue lies outside the phrase, so a negated span reads as a plain value).
        expected_kind = "value" if span.kind == "negated" else span.kind
        assert resolve_spans(key, phrase) == (
            WrittenSpan(0, len(phrase), span.value, expected_kind),
        )


@given(key=env_keys, text=st.one_of(fragment_texts(), st.text()))
def test_normalize_written_agrees_with_resolve_spans(key: EnvKey, text: str) -> None:
    named = {s.value for s in resolve_spans(key, text) if s.kind == "value"}
    result = normalize_written(key, text)
    if len(named) == 1 and None not in named:
        assert result == next(iter(named))
    elif result is not None:
        # Only a region subdivision may absorb its own country ("Ontario, Canada").
        assert key == "region"
        assert named == {result, result.split("-", 1)[0]}
    if not named or None in named:
        assert result is None


# --- region


@pytest.mark.parametrize(("code", "name"), sorted(REGION_NAMES.items()))
def test_every_full_region_name_maps_to_its_code(code: str, name: str) -> None:
    assert normalize_written("region", name) == code
    assert normalize_written("region", f"I grew these in {name} last summer") == code
    assert normalize_written("region", name.lower()) == code
    assert normalize_written("region", name.upper()) == code


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("in Connecticut", "US-CT"),
        ("Hartford, CT", "US-CT"),
        ("Hartford,CT", "US-CT"),
        ("Hartford, CT 06103", "US-CT"),
        ("Hartford, CT.", "US-CT"),
        ("Hartford, CT, USA", "US-CT"),
        ("Portland, OR", "US-OR"),
        ("Tulsa, OK", "US-OK"),
        ("Toronto, ON", "CA-ON"),
        ("Vancouver, BC, Canada", "CA-BC"),
        ("CT", "US-CT"),
        ("DC", "US-DC"),
        ("Conn.", "US-CT"),
        ("Mass.", "US-MA"),
        ("Calif.", "US-CA"),
        ("Penn.", "US-PA"),
        ("Penna.", "US-PA"),
        ("N.Y.", "US-NY"),
        ("USA", "US"),
        ("U.S.", "US"),
        ("U.S.A.", "US"),
        ("United States of America", "US"),
        ("UK", "GB"),
        ("U.K.", "GB"),
        ("England", "GB"),
        ("Scotland", "GB"),
        ("Wales", "GB"),
        ("Northern Ireland", "GB"),
        ("Belfast, Northern Ireland, UK", "GB"),
        ("Ireland", "IE"),
        ("New South Wales", "AU"),
        ("Washington State", "US-WA"),
        ("Seattle, Washington", "US-WA"),
        ("Washington, D.C.", "US-DC"),
        ("Washington D.C.", "US-DC"),
        ("Washington DC", "US-DC"),
        ("Washington, DC", "US-DC"),
        ("D.C.", "US-DC"),
        ("Georgia", "US-GA"),
        ("West Virginia", "US-WV"),
        ("Virginia", "US-VA"),
        ("New Mexico", "US-NM"),
        ("Mexico", "MX"),
        ("Arkansas", "US-AR"),
        ("Kansas", "US-KS"),
        ("Ontario, Canada", "CA-ON"),
        ("Maine, USA", "US-ME"),
        ("Portland, Maine, New England", "US-ME"),
        ("coastal Maine", "US-ME"),
        ("Maine's coast", "US-ME"),
        ("Québec", "CA-QC"),
        ("Hawai'i", "US-HI"),
        ("Hawai\u2019i", "US-HI"),
        ("Georgia-grown", "US-GA"),
        ("not far from Boston, Massachusetts", "US-MA"),
        ("Kansas City, Missouri", "US-MO"),
        ("Kansas City, Kansas", "US-KS"),
        ("Kansas City, MO", "US-MO"),
        ("Nevada City, California", "US-CA"),
    ],
)
def test_region_phrases(written: str, expected: str) -> None:
    assert normalize_written("region", written) == expected


@pytest.mark.parametrize(
    "written",
    [
        "zone 6b",
        "it was OK",
        "in it",
        "grown in ME",
        "tomatoes OR peppers",
        "pots, OK results",
        "hartford, ct",
        "ct",
        "US",
        "GB",
        "FR",
        "Connecticut and Virginia",
        "Ontario, California",
        "Maine and Florida, USA",
        "I live in Florida but grew these in Maine",
        "Toronto, ON, USA",
        "not in Maine",
        "humid",
        "a huge mass.",
        "conn.",
        "Kansas City",
        "near Kansas City",
    ],
)
def test_region_phrases_that_are_absent_or_ambiguous(written: str) -> None:
    assert normalize_written("region", written) is None


@pytest.mark.parametrize(("postal", "code"), sorted(POSTAL_CODES.items()))
def test_postal_codes_only_as_whole_phrase_or_after_a_comma(postal: str, code: str) -> None:
    assert normalize_written("region", postal) == code
    assert normalize_written("region", f"Springfield, {postal}") == code
    assert normalize_written("region", f"Springfield,{postal}") == code
    assert normalize_written("region", f"grown in {postal} last year") is None
    assert normalize_written("region", f"Springfield, {postal.lower()}") is None
    assert normalize_written("region", f"Springfield, {postal} gardeners say") is None


@given(zone=st.sampled_from(ZONE_VALUES), prefix=st.sampled_from(ZONE_PREFIXES))
def test_a_zone_never_produces_a_region(zone: str, prefix: str) -> None:
    assert normalize_written("region", f"{prefix}{zone}") is None
    assert normalize_written("region", f"{prefix}{zone.upper()}") is None


# Non-North-American "City, XX" forms whose code collides with a U.S./Canadian postal code.
FOREIGN_CITY_CODES = [
    "Perth, WA",
    "Fremantle, WA",
    "Bunbury, WA",
    "Broome, WA",
    "Darwin, NT",
    "Alice Springs, NT",
    "Catania, CT",
    "Palermo, PA",
    "Milano, MI",
    "Milan, MI",
    "Varese, VA",
    "Como, CO",
    "Messina, ME",
    "Chennai, TN",
    "Monterrey, NL",
    "Belém, PA",
    "Belem, PA",
    "Cagliari, CA",
    "Florianópolis, SC",
    "Recife, PE",
    "Tijuana, BC",
]


@pytest.mark.parametrize("written", FOREIGN_CITY_CODES)
def test_foreign_city_code_collisions_are_blocked(written: str) -> None:
    city, code = written.split(", ")
    assert normalize_written("region", written) is None
    assert normalize_written("region", f"{city} {code}") is None
    assert normalize_written("region", f"{city.upper()},{code}") is None
    assert normalize_written("region", f"Here in {written}, our summers are brutal") is None
    assert normalize_written("region", f"{written} 6000") is None


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("Perth, WA, Australia", "AU"),
        ("Darwin, NT Australia", "AU"),
        ("Catania, CT, Italy", "IT"),
        ("Seattle, WA", "US-WA"),
        ("Spokane, WA 99201", "US-WA"),
        ("Hartford, CT", "US-CT"),
        ("Yellowknife, NT", "CA-NT"),
        ("Perth, ON", "CA-ON"),
        ("Milan, Italy", "IT"),
        ("Pittsburgh, PA", "US-PA"),
    ],
)
def test_collision_blockers_leave_other_places_alone(written: str, expected: str) -> None:
    assert normalize_written("region", written) == expected


# Lakes, rivers, valleys, cities and neighbourhoods named after a region they are not in.
REGION_FEATURE_LOOKALIKES = [
    "near Lake Michigan",
    "Lake Michigan",
    "Lake Ontario",
    "Lake Erie",
    "Lake Huron",
    "Lake Superior",
    "Lake Champlain",
    "Lake Tahoe",
    "Gulf of Mexico",
    "near the Gulf of Mexico",
    "Ohio Valley",
    "Tennessee Valley",
    "Delaware Valley",
    "Mississippi Valley",
    "Missouri Valley",
    "Hudson Valley",
    "Ohio River",
    "Mississippi River",
    "Missouri River",
    "Delaware River",
    "Colorado River",
    "Tennessee River",
    "Connecticut River",
    "Michigan City",
    "Virginia City",
    "Missouri City",
    "Arkansas City",
    "Washington Heights",
    "Jamaica Plain",
    "Georgia Strait",
    "Strait of Georgia",
    "Colorado Desert",
    "Tbilisi, Georgia",
    "Tbilisi Georgia",
    "zone 5b near Lake Michigan",
]


@pytest.mark.parametrize("written", REGION_FEATURE_LOOKALIKES)
def test_features_named_after_a_region_are_blocked(written: str) -> None:
    assert normalize_written("region", written) is None


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("Chicago, Illinois near Lake Michigan", "US-IL"),
        ("Lake Michigan shore in Michigan", "US-MI"),
        ("Michigan City, Indiana", "US-IN"),
        ("Huntsville, Alabama in the Tennessee Valley", "US-AL"),
        ("Lake Erie shore of Ohio", "US-OH"),
        ("Houston, Texas near the Gulf of Mexico", "US-TX"),
        ("New York City", "US-NY"),
        ("Oklahoma City", "US-OK"),
        ("Iowa City", "US-IA"),
        ("Mexico City", "MX"),
        ("Quebec City", "CA-QC"),
        ("Oregon City", "US-OR"),
        ("the Texas Gulf Coast", "US-TX"),
        ("Utah Valley", "US-UT"),
        ("Virginia Beach", "US-VA"),
    ],
)
def test_feature_blockers_keep_real_regions(written: str, expected: str) -> None:
    assert normalize_written("region", written) == expected


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("upstate NY", "US-NY"),
        ("Upstate NY", "US-NY"),
        ("downstate IL", "US-IL"),
        ("central NJ", "US-NJ"),
        ("Central PA", "US-PA"),
        ("SE PA", "US-PA"),
        ("se PA", "US-PA"),
        ("NE OH", "US-OH"),
        ("middle TN", "US-TN"),
        ("Middle TN", "US-TN"),
        ("mid-MI", "US-MI"),
        ("western NC", "US-NC"),
        ("West TX", "US-TX"),
        ("northern CA", "US-CA"),
        ("Northern CA", "US-CA"),
        ("southern OR", "US-OR"),
        ("north central PA", "US-PA"),
        ("Southwest MO", "US-MO"),
        ("northeastern OH", "US-OH"),
        ("coastal SC", "US-SC"),
        ("eastern ON", "CA-ON"),
        ("Zone 7a, upstate NY.", "US-NY"),
        ("upstate NY, USA", "US-NY"),
    ],
)
def test_area_qualifier_before_a_postal_code(written: str, expected: str) -> None:
    assert normalize_written("region", written) == expected


@pytest.mark.parametrize(
    "written",
    [
        "Austin TX",
        "Portland OR",
        "upstate ny",
        "central nj",
        "West VA",
        "west VA",
        "W VA",
        "West Va.",
        "upstate NY and central NJ",
        "upstate NYC-ish",
        "central NJs",
        "plant it north OR south of the fence",
        "east OR west facing",
    ],
)
def test_postal_codes_without_comma_or_qualifier_stay_unresolved(written: str) -> None:
    assert normalize_written("region", written) is None


@pytest.mark.parametrize(("postal", "code"), sorted(POSTAL_CODES.items()))
def test_every_postal_code_resolves_after_an_area_qualifier(postal: str, code: str) -> None:
    assert normalize_written("region", f"upstate {postal}") == code
    assert normalize_written("region", f"in central {postal} we grow") == code
    assert normalize_written("region", f"Springfield {postal}") is None


# --- zone


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("zone 6b", "6b"),
        ("Zone 6B", "6b"),
        ("zone6b", "6b"),
        ("z6b", "6b"),
        ("Z6B", "6b"),
        ("USDA zone 7a", "7a"),
        ("USDA 7a", "7a"),
        ("hardiness zone 9", "9"),
        ("USDA hardiness zone 10b", "10b"),
        ("6b", "6b"),
        ("7", "7"),
        ("zone 6", "6"),
        ("zone 13", "13"),
        ("I'm in zone 6b, humid summers", "6b"),
        ("zone 6, 2023", "6"),
        ("zone 6b (2023)", "6b"),
        ("zone 6b, which is zone 6b", "6b"),
        ("zone 7-ish", "7"),
    ],
)
def test_zone_phrases(written: str, expected: str) -> None:
    assert normalize_written("zone", written) == expected


@pytest.mark.parametrize(
    "written",
    [
        "zones 6-7",
        "zone 6-7",
        "zone 6b-7a",
        "zone 6b\u20137a",
        "6b/7a",
        "zone 6b/7a",
        "zone 6a/b",
        "zone 5 to 6",
        "zone 5 or 6",
        "zone 5 & 6",
        "zone 5, 6",
        "zone 6b and zone 7a",
        "zone 14",
        "zone 0",
        "zone 6c",
        "I'm in 6b",
        "ozone 6",
        "Connecticut",
        "2023",
        "not zone 5",
    ],
)
def test_zone_phrases_that_are_absent_ambiguous_or_invalid(written: str) -> None:
    assert normalize_written("zone", written) is None


@given(zone=st.sampled_from(ZONE_VALUES), prefix=st.sampled_from(ZONE_PREFIXES))
def test_every_valid_zone_is_recognised_after_a_zone_prefix(zone: str, prefix: str) -> None:
    assert normalize_written("zone", f"{prefix}{zone.upper()}") == zone


@given(zone=st.sampled_from(ZONE_VALUES))
def test_a_bare_zone_is_recognised_only_as_the_whole_phrase(zone: str) -> None:
    assert normalize_written("zone", zone) == zone
    assert normalize_written("zone", f" {zone.upper()} ") == zone
    assert normalize_written("zone", f"we are {zone} here") is None


SHORTHAND_ZONE_PREFIXES = (
    "zn ",
    "Zn",
    "ZN ",
    "zone: ",
    "Zone: ",
    "zone - ",
    "usda",
    "USDA zone: ",
)


@given(zone=st.sampled_from(ZONE_VALUES), prefix=st.sampled_from(SHORTHAND_ZONE_PREFIXES))
def test_shorthand_zone_prefixes_are_recognised(zone: str, prefix: str) -> None:
    assert normalize_written("zone", f"{prefix}{zone}") == zone
    assert normalize_written("zone", f"I'm in {prefix}{zone.upper()} this year") == zone


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("zn 5", "5"),
        ("Zn5", "5"),
        ("Zone: 6b", "6b"),
        ("zone - 7a", "7a"),
        ("zone-7a", "7a"),
        ("usda7a", "7a"),
        ("USDA zone: 7a", "7a"),
        ("USDA: 7a", "7a"),
    ],
)
def test_shorthand_zone_phrases(written: str, expected: str) -> None:
    assert normalize_written("zone", written) == expected


OTHER_ZONE_SYSTEMS = [
    "Sunset zone 9",
    "Sunset zone 2A",
    "sunset zones 8-9",
    "Sunset Western Garden zone 9",
    "Sunset climate zone 7",
    "Sunset zone 9 here. Early Girl did fine.",
    "heat zone 9",
    "AHS heat zone 7",
    "AHS plant heat zone 7",
    "AHS zone 7",
    "Canadian zone 5a",
    "Canada zone 5",
    "Canadian hardiness zone 4b",
    "Australian climate zone 5",
    "Australian zone 3",
    "climate zone 3",
    "RHS zone 4",
    "RHS hardiness zone 4",
    "Sunset zn 9",
    "Sunset zone: 9",
]


@pytest.mark.parametrize("written", OTHER_ZONE_SYSTEMS)
def test_zones_from_other_systems_are_not_usda_zones(written: str) -> None:
    assert normalize_written("zone", written) is None


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("USDA zone 9b, Sunset zone 15", "9b"),
        ("USDA zone 9b, Sunset 15", "9b"),
        ("zone 7, AHS heat zone 9", "7"),
        ("Sunset zone 9 but USDA 8b", "8b"),
    ],
)
def test_other_zone_systems_do_not_hide_a_usda_zone(written: str, expected: str) -> None:
    assert normalize_written("zone", written) == expected


# --- growing_year


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("2023", "2023"),
        ("summer of 2023", "2023"),
        ("in 1999", "1999"),
        ("2023 season, zone 6b", "2023"),
        ("mid-2021", "2021"),
        ("2023, a great 2023", "2023"),
    ],
)
def test_year_phrases(written: str, expected: str) -> None:
    assert normalize_written("growing_year", written) == expected


@pytest.mark.parametrize(
    "written",
    ["last year", "2022 and 2023", "2022-2023", "2023-24", "2023/24", "1850", "20230", "zone 6b"],
)
def test_year_phrases_that_are_absent_or_ambiguous(written: str) -> None:
    assert normalize_written("growing_year", written) is None


@given(year=st.integers(min_value=1900, max_value=2099))
def test_every_19xx_or_20xx_year_is_recognised(year: int) -> None:
    assert normalize_written("growing_year", f"grown in {year}.") == str(year)


# --- categorical


@pytest.mark.parametrize(
    ("key", "value"),
    [(key, value) for key, values in CATEGORICAL_VALUES.items() for value in values],
)
def test_every_categorical_value_is_reachable_from_its_label_and_name(
    key: EnvKey, value: str
) -> None:
    assert normalize_written(key, VALUE_LABELS[value]) == value
    assert normalize_written(key, value.replace("_", " ")) == value


CATEGORICAL_EXAMPLES: list[tuple[EnvKey, str, str]] = [
    ("method", "pots", "container"),
    ("method", "container-grown", "container"),
    ("shelter", "greenhouse-grown", "greenhouse"),
    ("method", "a pot", "container"),
    ("method", "potted", "container"),
    ("method", "containers", "container"),
    ("method", "grow bags", "container"),
    ("method", "fabric pots", "container"),
    ("method", "5 gallon buckets", "container"),
    ("method", "five-gallon bucket", "container"),
    ("method", "planters", "container"),
    ("method", "tubs", "container"),
    ("method", "window box", "container"),
    ("method", "raised beds", "raised_bed"),
    ("method", "raised-bed", "raised_bed"),
    ("method", "no-till raised beds", "raised_bed"),
    ("method", "in the ground", "in_ground"),
    ("method", "in-ground", "in_ground"),
    ("method", "in ground", "in_ground"),
    ("method", "hydroponics", "hydroponic"),
    ("method", "Kratky", "hydroponic"),
    ("method", "DWC", "hydroponic"),
    ("shelter", "greenhouse", "greenhouse"),
    ("shelter", "glasshouse", "greenhouse"),
    ("shelter", "hoop house", "greenhouse"),
    ("shelter", "hoophouse", "greenhouse"),
    ("shelter", "high tunnel", "greenhouse"),
    ("shelter", "polytunnel", "greenhouse"),
    ("shelter", "poly tunnel", "greenhouse"),
    ("shelter", "inside the greenhouse", "greenhouse"),
    ("shelter", "outdoors", "outdoors"),
    ("shelter", "outside", "outdoors"),
    ("shelter", "outdoor", "outdoors"),
    ("shelter", "outside the greenhouse", "outdoors"),
    ("shelter", "started indoors, then grown outside", "outdoors"),
    ("shelter", "indoors", "indoors"),
    ("shelter", "inside", "indoors"),
    ("shelter", "grow tent", "indoors"),
    ("shelter", "under lights", "indoors"),
    ("shelter", "grow lights", "indoors"),
    ("summer_heat", "hot", "hot"),
    ("summer_heat", "hot summers", "hot"),
    ("summer_heat", "brutal heat", "hot"),
    ("summer_heat", "scorching", "hot"),
    ("summer_heat", "sweltering", "hot"),
    ("summer_heat", "very hot", "hot"),
    ("summer_heat", "mild summers", "mild"),
    ("summer_heat", "cool summer", "cool"),
    ("summer_heat", "cold summers", "cool"),
    ("summer_heat", "chilly", "cool"),
    ("summer_heat", "heat wave", "heatwave"),
    ("summer_heat", "heat-wave", "heatwave"),
    ("summer_heat", "heatwave", "heatwave"),
    ("summer_heat", "heat dome", "heatwave"),
    ("humidity", "humid", "humid"),
    ("humidity", "high humidity", "humid"),
    ("humidity", "muggy summers", "humid"),
    ("humidity", "sticky", "humid"),
    ("humidity", "steamy", "humid"),
    ("humidity", "dry air", "dry_air"),
    ("humidity", "arid", "dry_air"),
    ("humidity", "semi-arid", "dry_air"),
    ("humidity", "low humidity", "dry_air"),
    ("humidity", "dry heat", "dry_air"),
    ("humidity", "desert", "dry_air"),
    ("precipitation", "lots of rain", "rainy"),
    ("precipitation", "rainy", "rainy"),
    ("precipitation", "it rained for weeks", "rainy"),
    ("precipitation", "wet summer", "rainy"),
    ("precipitation", "downpours", "rainy"),
    ("precipitation", "monsoon", "rainy"),
    ("precipitation", "drought", "dry_season"),
    ("precipitation", "dry spell", "dry_season"),
    ("precipitation", "dry season", "dry_season"),
    ("precipitation", "dry summer", "dry_season"),
    ("precipitation", "no rain", "dry_season"),
    ("season_length", "short season", "short_season"),
    ("season_length", "short-season", "short_season"),
    ("season_length", "short growing season", "short_season"),
    ("season_length", "short summers", "short_season"),
    ("season_length", "long season", "long_season"),
    ("season_length", "long growing season", "long_season"),
    ("sun", "full sun", "full_sun"),
    ("sun", "all-day sun", "full_sun"),
    ("sun", "part shade", "part_shade"),
    ("sun", "partial shade", "part_shade"),
    ("sun", "part sun", "part_shade"),
    ("sun", "afternoon shade", "part_shade"),
    ("sun", "shade", "shade"),
    ("sun", "shady", "shade"),
    ("sun", "full shade", "shade"),
    ("soil", "clay", "clay"),
    ("soil", "heavy clay", "clay"),
    ("soil", "sand", "sandy"),
    ("soil", "sandy soil", "sandy"),
    ("soil", "loam", "loam"),
    ("soil", "loamy", "loam"),
    ("soil", "potting mix", "potting_mix"),
    ("soil", "potting soil", "potting_mix"),
    ("soil", "Pro-Mix", "potting_mix"),
    ("irrigation", "rain-fed", "rain_fed"),
    ("irrigation", "rain fed", "rain_fed"),
    ("irrigation", "only rain", "rain_fed"),
    ("irrigation", "never watered", "rain_fed"),
    ("irrigation", "drip", "drip"),
    ("irrigation", "drip irrigation", "drip"),
    ("irrigation", "drip line", "drip"),
    ("irrigation", "hand watered", "hand_watered"),
    ("irrigation", "hand watering", "hand_watered"),
    ("irrigation", "hand-watered", "hand_watered"),
    ("irrigation", "watering can", "hand_watered"),
    ("irrigation", "by hand", "hand_watered"),
    ("irrigation", "watered by hand", "hand_watered"),
]


@pytest.mark.parametrize(("key", "written", "expected"), CATEGORICAL_EXAMPLES)
def test_categorical_synonyms(key: EnvKey, written: str, expected: str) -> None:
    assert normalize_written(key, written) == expected
    assert normalize_written(key, written.upper()) == expected
    assert normalize_written(key, f"I grew them {written} this year") == expected


@pytest.mark.parametrize(
    ("key", "written"),
    [
        # Two different values remain -> ambiguous.
        ("method", "pots and raised beds"),
        ("summer_heat", "hot then mild"),
        ("sun", "full sun with afternoon shade"),
        ("shelter", "greenhouse and outdoors"),
        # Negated mentions are not reports of the condition.
        ("humidity", "not humid"),
        ("humidity", "not very humid"),
        ("humidity", "non-humid"),
        ("shelter", "no greenhouse"),
        ("shelter", "don't have a greenhouse"),
        ("irrigation", "didn't use drip"),
        ("method", "never in containers"),
        # Phrases that only look like a condition.
        ("summer_heat", "hot peppers"),
        ("summer_heat", "heat mat"),
        ("summer_heat", "heat-tolerant"),
        ("summer_heat", "mild winters"),
        ("summer_heat", "cool-season crops"),
        ("soil", "clay pots"),
        ("sun", "shade cloth"),
        ("humidity", "humidity dome"),
        ("precipitation", "rain barrel"),
        ("precipitation", "rain-fed"),
        ("irrigation", "pollinated by hand"),
        # Cross-key leakage.
        ("humidity", "Florida"),
        ("method", "zone 6b"),
        ("soil", "potting"),
    ],
)
def test_categorical_phrases_that_are_absent_ambiguous_or_negated(
    key: EnvKey, written: str
) -> None:
    assert normalize_written(key, written) is None


@pytest.mark.parametrize(
    ("key", "written", "expected"),
    [
        ("method", "not in pots but in the ground", "in_ground"),
        ("shelter", "no greenhouse, just outside", "outdoors"),
        ("humidity", "not humid at all, dry heat", "dry_air"),
        ("precipitation", "hardly any rain, a real drought", "dry_season"),
        ("method", "pots, nothing fancy", "container"),
    ],
)
def test_negation_only_discards_the_negated_phrase(
    key: EnvKey, written: str, expected: str
) -> None:
    assert normalize_written(key, written) == expected


@pytest.mark.parametrize(
    ("key", "written", "expected"),
    [
        ("summer_heat", "heat wave", "heatwave"),
        ("humidity", "low humidity", "dry_air"),
        ("sun", "part shade", "part_shade"),
        ("region", "West Virginia", "US-WV"),
        ("region", "New Mexico", "US-NM"),
        ("region", "Washington, D.C.", "US-DC"),
        ("humidity", "dry heat", "dry_air"),
    ],
)
def test_longest_phrase_wins_over_phrases_it_contains(
    key: EnvKey, written: str, expected: str
) -> None:
    assert normalize_written(key, written) == expected


# Bare dimension nouns and low-quantity wording name no high value.
LOW_QUANTITY_PHRASES: list[tuple[EnvKey, str]] = [
    ("humidity", "humidity"),
    ("humidity", "Humidity was in the single digits"),
    ("humidity", "humidity was low"),
    ("humidity", "the humidity was low"),
    ("humidity", "humidity is usually around 20%"),
    ("humidity", "lower humidity"),
    ("humidity", "lack of humidity"),
    ("humidity", "zero humidity"),
    ("humidity", "0% humidity"),
    ("humidity", "less humid"),
    ("humidity", "rarely humid"),
    ("summer_heat", "heat"),
    ("summer_heat", "lack of heat"),
    ("summer_heat", "Peppers sulked all summer from the lack of heat"),
    ("summer_heat", "too little heat"),
    ("summer_heat", "insufficient heat"),
    ("summer_heat", "lacked heat"),
    ("summer_heat", "needed more heat"),
    ("summer_heat", "less hot than usual"),
    ("summer_heat", "seldom hot"),
    ("precipitation", "rain"),
    ("precipitation", "rains"),
    ("precipitation", "It rarely rains here in summer"),
    ("precipitation", "seldom rains"),
    ("precipitation", "rain was scarce"),
    ("precipitation", "zero rain"),
    ("precipitation", "in the rain shadow"),
    ("precipitation", "rain never came"),
    ("precipitation", "it rarely rained"),
    ("precipitation", "watered with rain water"),
]


@pytest.mark.parametrize(("key", "written"), LOW_QUANTITY_PHRASES)
def test_low_quantity_wording_never_yields_the_high_value(key: EnvKey, written: str) -> None:
    assert normalize_written(key, written) is None


@pytest.mark.parametrize(
    ("key", "written", "expected"),
    [
        ("humidity", "low humidity", "dry_air"),
        ("humidity", "very low humidity", "dry_air"),
        ("humidity", "high humidity", "humid"),
        ("summer_heat", "high heat", "hot"),
        ("summer_heat", "extreme heat", "hot"),
        ("precipitation", "lots of rain", "rainy"),
        ("precipitation", "a lot of rain", "rainy"),
        ("precipitation", "heavy rains", "rainy"),
        ("precipitation", "too much rain", "rainy"),
        ("precipitation", "it rained for weeks", "rainy"),
        ("precipitation", "too little rain", "dry_season"),
        ("precipitation", "not enough rain", "dry_season"),
        ("precipitation", "lack of rain", "dry_season"),
        ("precipitation", "low rainfall", "dry_season"),
    ],
)
def test_explicit_quantity_phrases(key: EnvKey, written: str, expected: str) -> None:
    assert normalize_written(key, written) == expected


# Look-alikes: seed starting, overwintering, season extenders, nearby structures, other
# seasons, plant traits and non-watering hand work.
LOOKALIKE_PHRASES: list[tuple[EnvKey, str]] = [
    ("shelter", "seedlings started under lights"),
    ("shelter", "started them under grow lights"),
    ("shelter", "started under the grow lights"),
    ("shelter", "started seeds indoors under lights"),
    ("shelter", "overwintered indoors"),
    ("shelter", "overwinter them inside"),
    ("shelter", "overwintering indoors"),
    ("shelter", "brought them inside for winter"),
    ("shelter", "brought it indoors"),
    ("shelter", "inside the cold frame"),
    ("shelter", "inside a low tunnel"),
    ("shelter", "in a cold frame"),
    ("shelter", "under a low tunnel"),
    ("shelter", "next to the greenhouse"),
    ("shelter", "beside my greenhouse"),
    ("shelter", "behind the hoop house"),
    ("shelter", "near our polytunnel"),
    ("summer_heat", "cool spring"),
    ("summer_heat", "mild spring"),
    ("summer_heat", "cool fall"),
    ("summer_heat", "mild autumn"),
    ("summer_heat", "cool autumns"),
    ("summer_heat", "mild winter"),
    ("precipitation", "drought tolerant"),
    ("precipitation", "drought-tolerant"),
    ("precipitation", "drought resistant"),
    ("irrigation", "planted by hand"),
    ("irrigation", "seeded by hand"),
    ("irrigation", "thinned by hand"),
    ("irrigation", "picked off by hand"),
    ("irrigation", "harvested by hand"),
    ("method", "container mix"),
    ("sun", "light shade cloth"),
    ("sun", "partial shade cloth"),
    ("sun", "40% shade cloth"),
]


@pytest.mark.parametrize(("key", "written"), LOOKALIKE_PHRASES)
def test_lookalike_phrases_are_blocked(key: EnvKey, written: str) -> None:
    assert normalize_written(key, written) is None


@pytest.mark.parametrize(
    ("key", "written", "expected"),
    [
        ("shelter", "started indoors under lights, then planted outside", "outdoors"),
        ("shelter", "overwintered indoors, grown outside all summer", "outdoors"),
        ("shelter", "inside the greenhouse", "greenhouse"),
        ("shelter", "under grow lights", "indoors"),
        ("summer_heat", "cool summer", "cool"),
        ("summer_heat", "mild summers", "mild"),
        ("irrigation", "watered by hand", "hand_watered"),
        ("precipitation", "drought", "dry_season"),
    ],
)
def test_lookalike_blockers_keep_real_conditions(key: EnvKey, written: str, expected: str) -> None:
    assert normalize_written(key, written) == expected


@pytest.mark.parametrize(
    "written",
    [
        "some shade",
        "a bit of shade",
        "a little shade",
        "light shade",
        "filtered shade",
        "half shade",
        "semi shade",
        "semi-shade",
        "dappled shade",
        "morning shade",
        "afternoon shade",
        "Sungold gets shade in the afternoon from the fence",
        "shade in the morning",
        "shade part of the day",
        "shade for part of the day",
        "partly shaded",
        "part shaded",
        "partially shaded",
        "shade after 2pm",
    ],
)
def test_qualified_shade_is_part_shade(written: str) -> None:
    assert normalize_written("sun", written) == "part_shade"


@pytest.mark.parametrize("written", ["shade", "shady", "full shade", "deep shade", "shady spot"])
def test_unqualified_shade_stays_shade(written: str) -> None:
    assert normalize_written("sun", written) == "shade"


# --- normalize_crop


@pytest.mark.parametrize(
    ("crop", "expected"),
    [
        ("tomatoes", "tomato"),
        ("Tomatoes", "tomato"),
        ("TOMATO", "tomato"),
        ("potatoes", "potato"),
        ("strawberries", "strawberry"),
        ("peppers", "pepper"),
        ("beans", "bean"),
        ("peas", "pea"),
        ("peaches", "peach"),
        ("radishes", "radish"),
        ("grass", "grass"),
        ("asparagus", "asparagus"),
        ("squash", "squash"),
        ("lettuce", "lettuce"),
        ("  Cherry   Tomatoes ", "cherry tomato"),
        ("Brussels Sprouts", "brussels sprout"),
        ("sweet\tpotatoes", "sweet potato"),
        ("zucchinis", "zucchini"),
        ("Zucchinis", "zucchini"),
        ("kiwis", "kiwi"),
        ("chilis", "chili"),
        ("kohlrabis", "kohlrabi"),
        ("broccolis", "broccoli"),
        ("chilies", "chili"),
        ("chillies", "chilli"),
        ("red chillies", "red chilli"),
        ("tomato's", "tomato"),
        ("Tomato\u2019s", "tomato"),
        ("tomatoes's", "tomato"),
        ("iris", "iris"),
        ("irises", "iris"),
        ("japanese iris", "japanese iris"),
        ("cannabis", "cannabis"),
        ("clematis", "clematis"),
        ("physalis", "physalis"),
        ("hibiscus", "hibiscus"),
    ],
)
def test_normalize_crop(crop: str, expected: str) -> None:
    assert normalize_crop(crop) == expected


@pytest.mark.parametrize("crop", ["", "   ", "\n"])
def test_normalize_crop_rejects_empty(crop: str) -> None:
    with pytest.raises(ValueError, match="empty"):
        normalize_crop(crop)


@given(st.text().filter(lambda s: s.strip() != ""))
def test_normalize_crop_is_idempotent_and_collapsed(crop: str) -> None:
    once = normalize_crop(crop)
    assert normalize_crop(once) == once
    assert once == " ".join(once.split())


@given(st.text(alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E)))
def test_normalize_crop_ignores_ascii_case(crop: str) -> None:
    if not crop.strip():
        return
    assert normalize_crop(crop.upper()) == normalize_crop(crop.lower())


# --- CROP_SYNONYMS and mentions_crop

CROPS = ("tomato", "pepper", "cucumber", "zucchini", "potato", "bean", "cherry tomato", "pea")
CROP_VARIANTS = {
    "tomato": ("tomato", "Tomatoes", "TOMATO", "tomato's", "toms", "tomatos"),
    "pepper": ("peppers", "Pepper", "chilies", "chillies", "chile", "chili", "chilis"),
    "cucumber": ("cucumbers", "cukes", "Cuke"),
    "zucchini": ("zucchinis", "courgettes", "Courgette"),
    "potato": ("potatoes", "spuds", "Spud"),
    "bean": ("beans", "Bean"),
    "cherry tomato": ("cherry tomatoes", "Cherry  Tomato", "cherry-tomatoes"),
    "pea": ("peas",),
}
word_texts = st.text(alphabet=string.ascii_letters + string.digits + " ,.!?'()-", max_size=30)


def test_crop_synonyms_map_casefolded_words_to_normalized_crops() -> None:
    assert CROP_SYNONYMS["cukes"] == "cucumber"
    assert CROP_SYNONYMS["courgette"] == "zucchini"
    assert CROP_SYNONYMS["spuds"] == "potato"
    assert CROP_SYNONYMS["toms"] == "tomato"
    assert CROP_SYNONYMS["chillies"] == "pepper"
    for written, crop in CROP_SYNONYMS.items():
        assert written == " ".join(written.casefold().split())
        assert normalize_crop(crop) == crop
        assert mentions_crop(f"My {written}, finally!", crop)


@pytest.mark.parametrize(
    ("text", "crop", "expected"),
    [
        ("My peppers were in the greenhouse", "pepper", True),
        ("My peppers were in the greenhouse", "tomato", False),
        ("Great video, thanks!", "pepper", False),
        ("I grow Sungold in pots", "tomato", False),
        ("Sungold cherry tomatoes did great", "cherry tomato", True),
        ("Sungold cherry tomatoes did great", "tomato", True),
        ("Sungold cherry tomatoes did great", "Tomatoes", True),
        ("pepperoni pizza", "pepper", False),
        ("the peppermint spread", "pepper", False),
        ("Thanks Tom, great tip", "tomato", False),
        ("Tom's tip worked", "tomato", False),
        ("the cukes went crazy", "cucumber", True),
        ("Courgettes everywhere", "zucchini", True),
        ("chillies on the windowsill", "pepper", True),
        ("chillies on the windowsill", "chilli", True),
        ("chilies on the windowsill", "chili", True),
        ("the tomato\u2019s skin split", "tomato", True),
        ("pak-choi bolted", "pak choi", True),
        ("pak choi bolted", "pak-choi", True),
        ("", "tomato", False),
    ],
)
def test_mentions_crop_examples(text: str, crop: str, expected: bool) -> None:
    assert mentions_crop(text, crop) is expected


@pytest.mark.parametrize("crop", ["", "   ", "--", "!!"])
def test_mentions_crop_rejects_an_empty_crop(crop: str) -> None:
    with pytest.raises(ValueError, match="empty"):
        mentions_crop("tomatoes in pots", crop)


@given(
    crop=st.sampled_from(CROPS),
    data=st.data(),
    left=word_texts,
    right=word_texts,
    separator=st.sampled_from([" ", ", ", " (", "\n", "! "]),
)
def test_a_crop_written_anywhere_as_its_own_words_is_mentioned(
    crop: str, data: st.DataObject, left: str, right: str, separator: str
) -> None:
    written = data.draw(st.sampled_from(CROP_VARIANTS[crop]))
    assert mentions_crop(f"{left}{separator}{written}{separator[::-1]}{right}", crop)


@given(
    crop=st.sampled_from(CROPS),
    glued=st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=4),
)
def test_mentions_crop_is_word_bounded(crop: str, glued: str) -> None:
    # Letters glued on in front always make a different word.
    assert not mentions_crop(f"I grew {glued}{crop} here", crop)
    # Letters glued on behind make a different word unless they form a plural or possessive.
    word = f"{crop}{glued}"
    expected = normalize_crop(word) == normalize_crop(crop) or CROP_SYNONYMS.get(word) == crop
    assert mentions_crop(f"I grew {word} here", crop) is expected


@given(text=word_texts, crop=st.sampled_from(CROPS))
def test_mentions_crop_ignores_case_and_crop_spelling(text: str, crop: str) -> None:
    assert mentions_crop(text, crop) == mentions_crop(text.upper(), crop.upper())
    assert mentions_crop(text, crop) == mentions_crop(text, f"{crop}s")


# --- variety_key


@pytest.mark.parametrize(
    ("variety", "expected"),
    [
        ("Sun Gold", "sun gold"),
        ("Cherokee Purple", "cherokee purple"),
        ('"Sun Gold"', "sun gold"),
        ("'Sungold'", "sungold"),
        ("\u2018Sun Gold\u2019", "sun gold"),
        ("\u201cSun Gold\u201d", "sun gold"),
        ("Mr. Stripey", "mr stripey"),
        ("Aunt Ruby's German Green", "aunt ruby's german green"),
        ("Aunt Ruby\u2019s German Green", "aunt ruby's german green"),
        ("Early-Girl", "early-girl"),
        ("Sun - Gold", "sun gold"),
        ("  Black   Krim!! ", "black krim"),
        ("Brandywine (Sudduth's)", "brandywine sudduth's"),
        ("Sweet 100", "sweet 100"),
        ("Rutgers'", "rutgers"),
        ("-Juliet-", "juliet"),
    ],
)
def test_variety_key(variety: str, expected: str) -> None:
    assert variety_key(variety) == expected


@pytest.mark.parametrize("variety", ["", "   ", "!!!", "''", '"-"'])
def test_variety_key_rejects_empty(variety: str) -> None:
    with pytest.raises(ValueError, match="empty"):
        variety_key(variety)


@given(st.text())
def test_variety_key_is_idempotent_and_clean(variety: str) -> None:
    try:
        key = variety_key(variety)
    except ValueError:
        return
    assert variety_key(key) == key
    assert key == " ".join(key.split())
    assert re.search(r"(?<!\w)['-]|['-](?!\w)", key) is None


# --- parse_zone


@pytest.mark.parametrize(
    ("value", "expected"),
    [("6b", (6, "b")), ("6", (6, None)), ("13a", (13, "a")), ("1", (1, None)), ("10b", (10, "b"))],
)
def test_parse_zone(value: str, expected: tuple[int, str | None]) -> None:
    assert parse_zone(value) == expected


@pytest.mark.parametrize("value", ["", "0", "14", "6B", "6c", "zone 6", " 6", "6b ", "b6"])
def test_parse_zone_rejects_non_normalized_values(value: str) -> None:
    with pytest.raises(ValueError, match="zone"):
        parse_zone(value)


@given(st.sampled_from(ZONE_VALUES))
def test_parse_zone_round_trips_every_zone(zone: str) -> None:
    number, subzone = parse_zone(zone)
    assert 1 <= number <= 13
    assert f"{number}{subzone or ''}" == zone
    assert normalize_written("zone", zone) == zone
