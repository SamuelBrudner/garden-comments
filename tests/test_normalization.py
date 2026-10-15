"""Tests for :mod:`crop_explorer.normalization` (synonym mapping onto the vocabulary)."""

from __future__ import annotations

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from crop_explorer.normalization import (
    STRICT_KEYS,
    normalize_crop,
    normalize_written,
    parse_zone,
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
)
fragment_lists = st.lists(st.sampled_from(FRAGMENTS), max_size=6)
env_keys = st.sampled_from(ENV_KEYS)


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
    ("humidity", "humidity", "humid"),
    ("humidity", "muggy summers", "humid"),
    ("humidity", "sticky", "humid"),
    ("humidity", "steamy", "humid"),
    ("humidity", "dry air", "dry_air"),
    ("humidity", "arid", "dry_air"),
    ("humidity", "semi-arid", "dry_air"),
    ("humidity", "low humidity", "dry_air"),
    ("humidity", "dry heat", "dry_air"),
    ("humidity", "desert", "dry_air"),
    ("precipitation", "rain", "rainy"),
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
