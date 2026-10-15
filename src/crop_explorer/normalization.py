"""Deterministic mapping of as-written phrases onto the controlled vocabulary.

Every mapping is an explicit, module-level synonym table. Free text is matched
case-insensitively (two-letter postal codes and dotted state abbreviations excepted), with
whitespace collapsed and every phrase bounded by non-word characters. Overlapping candidates
are resolved **longest phrase first** over non-overlapping spans, so "heat wave" is a heatwave
rather than "hot" and "West Virginia" is not Virginia. A phrase preceded by a negation or
low-quantity cue in the same clause ("not humid", "no greenhouse", "less humid") is discarded,
and look-alike phrases ("hot peppers", "clay pots", "started under lights", "Lake Michigan",
"Perth, WA", "Sunset zone 9") are blocked outright. A bare dimension noun ("humidity",
"heat", "rain") names no value. If two different values remain, the phrase is ambiguous and
nothing is normalized; the only exception is a region subdivision named with its own country
("Ontario, Canada"). Region, zone and year are never inferred from one another or from any
other condition.

:func:`resolve_spans` returns the resolved spans themselves, with offsets into the original
text, so a caller can check what a phrase means at its own position inside a longer quote.
:func:`normalize_written` is computed from those spans, so the two never disagree.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import partial
from types import MappingProxyType
from typing import Final, Literal

from crop_explorer.regions import CA_PROVINCES, REGION_NAMES, US_STATES
from crop_explorer.schemas import ENV_KEYS, EnvKey
from crop_explorer.vocab import CATEGORICAL_VALUES, ZONE_PATTERN, is_valid_normalized

STRICT_KEYS: Final[frozenset[EnvKey]] = frozenset({"region", "zone", "growing_year"})
"""Keys whose stored value must always equal ``normalize_written`` of the as-written phrase."""

SpanKind = Literal["value", "negated", "blocked"]
"""What a resolved span does: names a value, names a negated value, or blocks a look-alike."""

_SynonymTable = Mapping[str, tuple[str, ...]]
"""Normalized value -> the phrases that express it."""

# --- phrase builders


def _combine(heads: Iterable[str], tails: Iterable[str]) -> tuple[str, ...]:
    """Return every ``"<head> <tail>"`` phrase (an explicit cross product of two lists)."""
    tail_phrases = tuple(tails)
    return tuple(f"{head} {tail}" for head in heads for tail in tail_phrases)


_DETERMINERS: Final = ("the", "a", "my", "our")


def _relative_to(preposition: str, structures: Iterable[str]) -> tuple[str, ...]:
    """Return phrases like ``"inside the greenhouse"`` for every determiner and structure."""
    return _combine((f"{preposition} {d}" for d in _DETERMINERS), structures)


# --- synonym tables: categorical keys

_GREENHOUSE_WORDS: Final = ("greenhouse", "glasshouse", "hoop house", "high tunnel", "poly tunnel")
_SEASON_EXTENDERS: Final = ("cold frame", "cold frames", "low tunnel", "low tunnels")
_PART_SHADE_QUALIFIERS: Final = (
    "part",
    "partial",
    "some",
    "a bit of",
    "a little",
    "light",
    "filtered",
    "half",
    "semi",
    "dappled",
    "morning",
    "afternoon",
)
_SHADE_COVERS: Final = ("shade cloth", "shade cloths", "shade netting")

_METHOD: Final[_SynonymTable] = MappingProxyType(
    {
        "container": (
            "container",
            "containers",
            "pot",
            "pots",
            "potted",
            "grow bag",
            "grow bags",
            "fabric pot",
            "fabric pots",
            "bucket",
            "buckets",
            "five gallon",
            "5 gallon",
            "5 gal",
            "planter",
            "planters",
            "tub",
            "tubs",
            "window box",
            "window boxes",
            "earthbox",
        ),
        "raised_bed": ("raised bed", "raised beds", "raised garden bed", "raised garden beds"),
        "in_ground": ("in the ground", "in ground", "in the dirt", "in the field"),
        "hydroponic": (
            "hydroponic",
            "hydroponics",
            "hydroponically",
            "kratky",
            "dwc",
            "dwc bucket",
            "dwc buckets",
            "deep water culture",
            "hydroponic bucket",
            "hydroponic buckets",
            "aeroponic",
            "aeroponics",
            "aquaponics",
        ),
    }
)

_SHELTER: Final[_SynonymTable] = MappingProxyType(
    {
        "outdoors": (
            "outdoors",
            "outdoor",
            "outside",
            "open air",
            "in the open",
            *_relative_to("outside", _GREENHOUSE_WORDS),
        ),
        "greenhouse": (
            *_GREENHOUSE_WORDS,
            "greenhouses",
            "hoop houses",
            "high tunnels",
            "poly tunnels",
            *_relative_to("inside", _GREENHOUSE_WORDS),
        ),
        "indoors": (
            "indoors",
            "indoor",
            "inside",
            "grow tent",
            "grow tents",
            "grow room",
            "under lights",
            "grow light",
            "grow lights",
            "window sill",
        ),
    }
)

_SUMMER_HEAT: Final[_SynonymTable] = MappingProxyType(
    {
        "hot": (
            "hot",
            "very hot",
            "hot summer",
            "hot summers",
            "high heat",
            "brutal heat",
            "extreme heat",
            "intense heat",
            "scorching",
            "sweltering",
            "blazing",
        ),
        "mild": ("mild", "mild summer", "mild summers", "moderate summers", "temperate"),
        "cool": ("cool", "cool summer", "cool summers", "cold summer", "cold summers", "chilly"),
        "heatwave": ("heat wave", "heat waves", "heat dome", "heat domes"),
    }
)

_HUMIDITY: Final[_SynonymTable] = MappingProxyType(
    {
        "humid": ("humid", "high humidity", "muggy", "sticky", "steamy"),
        "dry_air": (
            "dry air",
            "arid",
            "semi arid",
            "low humidity",
            "dry heat",
            "desert",
            "dry climate",
        ),
    }
)

_PRECIPITATION: Final[_SynonymTable] = MappingProxyType(
    {
        "rainy": (
            "rainy",
            "rained",
            "raining",
            "lots of rain",
            "a lot of rain",
            "plenty of rain",
            "so much rain",
            "too much rain",
            "heavy rain",
            "heavy rains",
            "constant rain",
            "high rainfall",
            "wet summer",
            "wet spring",
            "wet season",
            "wet year",
            "downpour",
            "downpours",
            "monsoon",
            "torrential",
        ),
        "dry_season": (
            "drought",
            "droughts",
            "dry spell",
            "dry season",
            "dry summer",
            "dry year",
            "no rain",
            "little rain",
            "lack of rain",
            "not much rain",
            "not enough rain",
            "low rainfall",
        ),
    }
)

_SEASON_LENGTH: Final[_SynonymTable] = MappingProxyType(
    {
        "short_season": (
            "short season",
            "short seasons",
            "short growing season",
            "short summer",
            "short summers",
        ),
        "long_season": (
            "long season",
            "long seasons",
            "long growing season",
            "long summer",
            "long summers",
        ),
    }
)

_SUN: Final[_SynonymTable] = MappingProxyType(
    {
        "full_sun": ("full sun", "all day sun", "direct sun"),
        "part_shade": (
            *_combine(_PART_SHADE_QUALIFIERS, ("shade",)),
            "part sun",
            "partial sun",
            "morning sun",
            "shade in the morning",
            "shade in the afternoon",
            "shade part of the day",
            "shade for part of the day",
            "shade after",
            "shade until",
            "part shaded",
            "partly shaded",
            "partially shaded",
        ),
        "shade": ("shade", "shady", "full shade", "deep shade"),
    }
)

_SOIL: Final[_SynonymTable] = MappingProxyType(
    {
        "clay": ("clay", "heavy clay"),
        "sandy": ("sand", "sandy"),
        "loam": ("loam", "loamy"),
        "potting_mix": (
            "potting mix",
            "potting soil",
            "pro mix",
            "soilless mix",
            "container mix",
            "potting compost",
        ),
    }
)

_IRRIGATION: Final[_SynonymTable] = MappingProxyType(
    {
        "rain_fed": (
            "rain fed",
            "only rain",
            "never watered",
            "no irrigation",
            "without irrigation",
            "dry farmed",
        ),
        "drip": (
            "drip",
            "drip irrigation",
            "drip irrigated",
            "drip line",
            "drip lines",
            "drip tape",
        ),
        "hand_watered": (
            "hand watered",
            "hand watering",
            "hand water",
            "watering can",
            "by hand",
            "watered by hand",
        ),
    }
)

_CATEGORICAL_SYNONYMS: Final[Mapping[str, _SynonymTable]] = MappingProxyType(
    {
        "method": _METHOD,
        "shelter": _SHELTER,
        "summer_heat": _SUMMER_HEAT,
        "humidity": _HUMIDITY,
        "precipitation": _PRECIPITATION,
        "season_length": _SEASON_LENGTH,
        "sun": _SUN,
        "soil": _SOIL,
        "irrigation": _IRRIGATION,
    }
)

# --- look-alike blockers: categorical keys

_SEED_STARTING: Final = _combine(
    (
        "start",
        "started",
        "starting",
        "started them",
        "started seeds",
        "started my seeds",
        "seedlings",
    ),
    (
        "indoors",
        "inside",
        "under lights",
        "under grow lights",
        "under the lights",
        "under the grow lights",
        "under a grow light",
        "indoors under lights",
        "indoors under grow lights",
        "inside under lights",
    ),
)
"""Seed starting is not where the crop grew ("started indoors, then planted outside")."""

_MOVED_INDOORS: Final = _combine(
    (
        "overwinter",
        "overwintered",
        "overwintering",
        "overwinter them",
        "overwintered them",
        "overwintered it",
        "bring them",
        "bring it",
        "brought",
        "brought them",
        "brought it",
        "bringing them",
        "moved them",
        "moved it",
    ),
    ("indoors", "inside"),
)
"""Overwintering a plant indoors is not growing it indoors."""

_NEAR_A_GREENHOUSE: Final = tuple(
    phrase
    for preposition in ("next to", "beside", "behind", "near", "in front of")
    for phrase in _relative_to(preposition, _GREENHOUSE_WORDS)
)

_CATEGORICAL_BLOCKERS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "method": ("container mix", "container soil"),
        "shelter": (
            *_SEED_STARTING,
            *_MOVED_INDOORS,
            *_SEASON_EXTENDERS,
            *_relative_to("inside", _SEASON_EXTENDERS),
            *_NEAR_A_GREENHOUSE,
        ),
        "summer_heat": (
            "hot pepper",
            "hot peppers",
            "hot sauce",
            "heat mat",
            "heat mats",
            "heat lamp",
            "bottom heat",
            "heat tolerant",
            "heat tolerance",
            "heat resistant",
            "heat loving",
            "mild flavor",
            "cool season",
            "cool weather crop",
            "cool weather crops",
            *_combine(
                ("cool", "mild"),
                ("spring", "springs", "fall", "falls", "autumn", "autumns", "winter", "winters"),
            ),
        ),
        "humidity": ("humidity dome", "humidity domes", "humidity tray", "humidity trays"),
        "precipitation": (
            "rain barrel",
            "rain barrels",
            "rain garden",
            "rain gauge",
            "rain fed",
            "only rain",
            "drought tolerant",
            "drought tolerance",
            "drought resistant",
            "drought hardy",
        ),
        "season_length": (),
        "sun": (*_SHADE_COVERS, *_combine(_PART_SHADE_QUALIFIERS, _SHADE_COVERS)),
        "soil": ("clay pot", "clay pots", "clay pebbles", "clay balls"),
        "irrigation": _combine(
            (
                "pollinated",
                "picked",
                "weeded",
                "pulled",
                "planted",
                "seeded",
                "sown",
                "sowed",
                "thinned",
                "transplanted",
                "harvested",
                "removed",
                "picked off",
                "pulled off",
            ),
            ("by hand",),
        ),
    }
)
"""Look-alike phrases that consume their text without yielding a value."""

# --- synonym tables: regions

_REGION_ALIASES: Final[_SynonymTable] = MappingProxyType(
    {
        "US": ("USA", "U.S.", "U.S.A.", "United States of America", "New England"),
        "GB": (
            "UK",
            "U.K.",
            "Great Britain",
            "Britain",
            "England",
            "Scotland",
            "Wales",
            "Northern Ireland",
        ),
        "AU": ("New South Wales", "Queensland", "Tasmania"),
        "NL": ("Holland",),
        "CZ": ("Czech Republic",),
        "KR": ("Republic of Korea",),
        "TR": ("Türkiye", "Turkiye"),
        "MX": ("México",),
        "US-WA": ("Washington State",),
        "US-DC": (
            "Washington, D.C.",
            "Washington D.C.",
            "Washington, DC",
            "Washington DC",
            "D.C.",
        ),
        "US-NY": ("NYC",),
        "US-HI": ("Hawai'i", "Hawai\u02bbi"),
        "CA-QC": ("Québec",),
        "CA-NL": ("Newfoundland",),
        "CA-PE": ("PEI", "P.E.I."),
    }
)
"""Case-insensitive alternative names (full names come from ``regions.REGION_NAMES``)."""

_REGION_BLOCKERS: Final[tuple[str, ...]] = (
    # Cities named after a region they are not (only) in.
    "Kansas City",
    "Nevada City",
    "Michigan City",
    "Virginia City",
    "Missouri City",
    "Arkansas City",
    "Washington Heights",
    "Jamaica Plain",
    "Jamaica Plains",
    *_combine(("Tbilisi", "Tbilisi,", "Batumi", "Batumi,", "Kutaisi", "Kutaisi,"), ("Georgia",)),
    # Lakes, rivers, valleys and waters that span several regions or lie outside the one named.
    *_combine(("Lake",), ("Michigan", "Ontario", "Erie", "Huron", "Superior", "Champlain")),
    "Lake Tahoe",
    *_combine(("Ohio", "Tennessee", "Delaware", "Mississippi", "Missouri", "Hudson"), ("Valley",)),
    *_combine(
        (
            "Ohio",
            "Tennessee",
            "Delaware",
            "Mississippi",
            "Missouri",
            "Colorado",
            "Connecticut",
            "Hudson",
        ),
        ("River",),
    ),
    "Gulf of Mexico",
    "Georgia Strait",
    "Strait of Georgia",
    "Colorado Desert",
    # "West VA" usually means West Virginia, not western Virginia.
    "West VA",
)
"""Places that contain another region's name: they consume their text without yielding a
value, so "Kansas City, Missouri" is Missouri and "Chicago, Illinois near Lake Michigan" is
Illinois, while a bare "Kansas City" or "Lake Michigan" stays unresolved. "New York City",
"Oklahoma City", "Mexico City" and similar cities that lie in the region they name still
resolve."""

_FOREIGN_CITY_CODES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "WA": (
            "Perth",
            "Fremantle",
            "Bunbury",
            "Broome",
            "Geraldton",
            "Mandurah",
            "Kalgoorlie",
            "Busselton",
            "Margaret River",
        ),
        "NT": ("Darwin", "Alice Springs", "Katherine", "Palmerston"),
        "CT": ("Catania",),
        "PA": ("Palermo", "Belém", "Belem"),
        "MI": ("Milano", "Milan"),
        "VA": ("Varese",),
        "CO": ("Como",),
        "ME": ("Messina",),
        "TN": ("Chennai", "Coimbatore", "Madurai", "Trento"),
        "NL": ("Monterrey",),
        "CA": ("Cagliari",),
        "SC": ("Florianópolis", "Florianopolis"),
        "PE": ("Recife", "Pescara"),
        "BC": ("Tijuana", "Ensenada", "Mexicali"),
        "MN": ("Mantova", "Mantua"),
        "MO": ("Modena",),
        "MT": ("Matera", "Cuiabá", "Cuiaba"),
        "AL": ("Alessandria", "Maceió", "Maceio"),
    }
)
"""Non-North-American "City, XX" forms whose subdivision code is also a U.S./Canadian postal
code (Western Australia, Northern Territory, Italian provinces, Brazilian, Indian and Mexican
states). They are blocked (comma optional, city case-insensitive): "Perth, WA" stays
unresolved and "Perth, WA, Australia" is Australia."""

_REGION_ABBREVIATIONS: Final[_SynonymTable] = MappingProxyType(
    {
        "US-AL": ("Ala.",),
        "US-AZ": ("Ariz.",),
        "US-AR": ("Ark.",),
        "US-CA": ("Calif.",),
        "US-CO": ("Colo.",),
        "US-CT": ("Conn.",),
        "US-DE": ("Del.",),
        "US-FL": ("Fla.",),
        "US-GA": ("Ga.",),
        "US-IL": ("Ill.",),
        "US-IN": ("Ind.",),
        "US-KS": ("Kan.", "Kans."),
        "US-KY": ("Ky.",),
        "US-MD": ("Md.",),
        "US-MA": ("Mass.",),
        "US-MI": ("Mich.",),
        "US-MN": ("Minn.",),
        "US-MT": ("Mont.",),
        "US-NE": ("Neb.", "Nebr."),
        "US-NV": ("Nev.",),
        "US-NH": ("N.H.",),
        "US-NJ": ("N.J.",),
        "US-NM": ("N.M.",),
        "US-NY": ("N.Y.", "N.Y.C."),
        "US-NC": ("N.C.",),
        "US-ND": ("N.D.",),
        "US-OK": ("Okla.",),
        "US-OR": ("Ore.", "Oreg."),
        "US-PA": ("Penn.", "Penna."),
        "US-RI": ("R.I.",),
        "US-SC": ("S.C.",),
        "US-SD": ("S.D.",),
        "US-TN": ("Tenn.",),
        "US-TX": ("Tex.",),
        "US-VT": ("Vt.",),
        "US-VA": ("Va.",),
        "US-WA": ("Wash.",),
        "US-WV": ("W. Va.",),
        "US-WI": ("Wis.", "Wisc."),
        "US-WY": ("Wyo.",),
        "CA-AB": ("Alta.",),
        "CA-BC": ("B.C.",),
        "CA-NS": ("N.S.",),
        "CA-ON": ("Ont.",),
        "CA-QC": ("Que.",),
        "CA-SK": ("Sask.",),
    }
)
"""Traditional dotted abbreviations, matched case-sensitively ("Mass." but not "mass.")."""

_POSTAL_CODES: Final = MappingProxyType(
    {code.split("-", 1)[1]: code for code in (*US_STATES, *CA_PROVINCES)}
)
"""Two-letter postal code -> region code, for U.S. states (incl. DC) and Canadian provinces."""

# --- matching primitives

_TYPOGRAPHIC: Final = str.maketrans(
    {"\u2018": "'", "\u2019": "'", "\u02bc": "'", "\u2010": "-", "\u2011": "-"}
)
_NON_SPACE: Final = re.compile(r"\S+")
_TOKEN_SPLIT: Final = re.compile(r"[\s-]+")
_SEPARATOR: Final = r"[\s-]*"
_LEFT_EDGE: Final = r"(?<![\w.-])"
_RIGHT_EDGE: Final = r"(?:(?![\w-])|(?=-grown(?![\w-])))"

_NEGATION_CUES: Final = frozenset(
    {
        "no",
        "not",
        "never",
        "without",
        "nor",
        "neither",
        "hardly",
        "barely",
        "didn't",
        "don't",
        "doesn't",
        "isn't",
        "wasn't",
        "aren't",
        "weren't",
        "haven't",
        "hasn't",
        "hadn't",
        "can't",
        "cannot",
        "couldn't",
        # Low-quantity cues: "less humid", "a lack of humid days", "it rarely rained".
        "less",
        "lack",
        "lacked",
        "lacking",
        "insufficient",
        "rarely",
        "seldom",
        "zero",
    }
)
_NEGATION_WINDOW: Final = 3
"""How many words before a phrase (within its clause) are searched for a negation cue."""
_CLAUSE_BREAK: Final = re.compile(r"[,;:.!?()\[\]{}]")
_WORD: Final = re.compile(r"[\w'-]+")

_SEVERAL: Final = "<several>"
"""Placeholder for a range or list ("zones 6-7", "2022-2023"); never a valid value."""

_ZONE_ITEM: Final = r"\d{1,2}[ab]?(?!\w)"
_ZONE_JOINER: Final = r"\s*(?:[-/&,+\u2013\u2014]|\bto\b|\bor\b|\band\b)\s*"
_ZONE_ITEMS: Final = rf"{_ZONE_ITEM}(?:{_ZONE_JOINER}{_ZONE_ITEM}|/[ab](?!\w))*"
_ZONE_PUNCT: Final = r"\s*(?:[:\-\u2013\u2014]\s*)?"
_ZONE_WORD: Final = r"(?:zones?|zn)"
_ZONE_MENTION: Final = re.compile(
    rf"(?<![\w-])(?:(?:usda\s+)?(?:hardiness\s+)?{_ZONE_WORD}{_ZONE_PUNCT}|usda{_ZONE_PUNCT}"
    rf"|z(?=\d))(?P<zones>{_ZONE_ITEMS})",
    re.IGNORECASE,
)
_OTHER_ZONE_SYSTEMS: Final = (
    r"sunset(?:[\s-]+western[\s-]+garden(?:[\s-]+book)?)?(?:[\s-]+climate)?",
    r"(?:ahs[\s-]+)?(?:plant[\s-]+)?heat",
    r"ahs",
    r"canad(?:ian|a(?:'s)?)(?:[\s-]+(?:plant[\s-]+)?hardiness)?",
    r"australian(?:[\s-]+[a-z]+){0,2}?",
    r"climate",
    r"rhs(?:[\s-]+hardiness)?",
)
_OTHER_ZONE_SYSTEM: Final = re.compile(
    rf"(?<![\w-])(?:{'|'.join(_OTHER_ZONE_SYSTEMS)})[\s-]+{_ZONE_WORD}"
    rf"(?:{_ZONE_PUNCT}{_ZONE_ITEMS})?",
    re.IGNORECASE,
)
"""Zones of other systems (Sunset, AHS heat, Canadian, Australian, climate, RHS): blocked, so
"Sunset zone 9" is never stored as USDA zone 9."""
_BARE_ZONE: Final = re.compile(_ZONE_ITEM, re.IGNORECASE)
_YEAR: Final = re.compile(
    r"(?<!\w)(?P<year>(?:19|20)\d{2})(?!\w)"
    r"(?P<range>\s*[-/\u2013\u2014]\s*\d{2}(?:\d{2})?(?!\w))?"
)
_POSTAL_WHOLE: Final = re.compile(r"(?P<code>[A-Z]{2})")
_POSTAL_AFTER_COMMA: Final = re.compile(
    r"(?:(?<=,)|(?<=, ))(?P<code>[A-Z]{2})"
    r"(?=\s*(?:\Z|[^\w\s-]|\d|(?:USA|U\.S\.|Canada)(?![\w-])))"
)
_DIRECTION: Final = (
    r"(?:north|south)(?:[\s-]?(?:east|west))?(?:ern)?|(?:east|west)(?:ern)?|[ns][ew]"
)
_AREA_QUALIFIER: Final = (
    rf"(?:up|down)state|(?:(?:{_DIRECTION})[\s-]?)?central|middle|mid|coastal|{_DIRECTION}"
)
_POSTAL_AFTER_QUALIFIER: Final = re.compile(
    rf"{_LEFT_EDGE}(?i:{_AREA_QUALIFIER})[\s-]+(?P<code>[A-Z]{{2}})(?![\w-])"
    rf"(?!(?<=OR)\s+(?i:{_DIRECTION})(?![\w-]))"
)
"""An uppercase postal code right after a directional/area qualifier ("upstate NY",
"SE PA", "middle TN"); a bare "Austin TX" without a comma is never read as a state, and the
conjunction in "north OR south" is not Oregon."""


@dataclass(frozen=True, slots=True)
class _Phrase:
    """A compiled synonym; ``value`` is ``None`` for a blocker."""

    pattern: re.Pattern[str]
    value: str | None


@dataclass(frozen=True, slots=True)
class _Span:
    """Where a phrase matched in the canonical text, and the value it stands for."""

    start: int
    end: int
    value: str | None


@dataclass(frozen=True, slots=True)
class _Canonical:
    """Canonical text plus, for each of its characters, the index in the original text."""

    text: str
    origin: tuple[int, ...]

    def original(self, span: _Span) -> tuple[int, int]:
        """Map a span of the canonical text back to ``(start, end)`` in the original text."""
        return self.origin[span.start], self.origin[span.end - 1] + 1


def _canonicalize(text: str) -> _Canonical:
    """Unify typographic apostrophes/hyphens and collapse whitespace, keeping an offset map."""
    tokens = list(_NON_SPACE.finditer(text.translate(_TYPOGRAPHIC)))
    origin: list[int] = []
    for token in tokens:
        if origin:
            origin.append(token.start() - 1)
        origin.extend(range(token.start(), token.end()))
    return _Canonical(" ".join(token[0] for token in tokens), tuple(origin))


def _phrase_key(phrase: str) -> str:
    return " ".join(_TOKEN_SPLIT.split(phrase.strip().casefold()))


def _phrase_body(phrase: str) -> str:
    """Regex for a phrase whose tokens may be joined by spaces, hyphens or nothing."""
    return _SEPARATOR.join(re.escape(token) for token in _TOKEN_SPLIT.split(phrase.strip()))


def _compile_phrase(phrase: str, flags: int) -> re.Pattern[str]:
    return re.compile(f"{_LEFT_EDGE}{_phrase_body(phrase)}{_RIGHT_EDGE}", flags)


def _compile_table(
    table: _SynonymTable, flags: int, blockers: Iterable[str] = ()
) -> tuple[_Phrase, ...]:
    entries: list[tuple[str, str | None]] = [(p, v) for v, ps in table.items() for p in ps]
    entries += [(p, None) for p in blockers]
    return tuple(_Phrase(_compile_phrase(p, flags), v) for p, v in entries)


def _check_table(
    name: str, table: _SynonymTable, allowed: Iterable[str], blockers: Iterable[str] = ()
) -> None:
    """Fail at import if a table names unknown values or lists a phrase twice."""
    unknown = sorted(set(table) - set(allowed))
    if unknown:
        raise ValueError(f"Synonym table {name!r} maps to unknown values: {unknown}")
    phrases = [*(p for ps in table.values() for p in ps), *blockers]
    duplicated = sorted(k for k, n in Counter(map(_phrase_key, phrases)).items() if n > 1)
    if duplicated:
        raise ValueError(f"Synonym table {name!r} lists phrases more than once: {duplicated}")


def _build_categorical(key: str) -> tuple[_Phrase, ...]:
    table, blockers, allowed = (
        _CATEGORICAL_SYNONYMS[key],
        _CATEGORICAL_BLOCKERS[key],
        CATEGORICAL_VALUES[key],
    )
    _check_table(key, table, allowed, blockers)
    missing = sorted(set(allowed) - set(table))
    if missing:
        raise ValueError(f"Synonym table {key!r} has no phrases for: {missing}")
    return _compile_table(table, re.IGNORECASE, blockers)


def _compile_foreign_city(code: str, cities: Iterable[str]) -> _Phrase:
    """Compile a blocker for ``"<city>, <code>"`` (comma optional, city case-insensitive)."""
    names = "|".join(_phrase_body(city) for city in sorted(cities, key=len, reverse=True))
    pattern = rf"{_LEFT_EDGE}(?i:{names})\s*,?\s*{code}{_RIGHT_EDGE}"
    return _Phrase(re.compile(pattern), None)


def _build_foreign_cities() -> tuple[_Phrase, ...]:
    unknown = sorted(set(_FOREIGN_CITY_CODES) - set(_POSTAL_CODES))
    if unknown:
        raise ValueError(f"Foreign city blockers name unknown postal codes: {unknown}")
    return tuple(_compile_foreign_city(c, cities) for c, cities in _FOREIGN_CITY_CODES.items())


def _build_region() -> tuple[_Phrase, ...]:
    _check_table("region aliases", _REGION_ALIASES, REGION_NAMES)
    _check_table("region abbreviations", _REGION_ABBREVIATIONS, REGION_NAMES)
    insensitive = {
        code: (name, *_REGION_ALIASES.get(code, ())) for code, name in REGION_NAMES.items()
    }
    _check_table("region names and aliases", insensitive, REGION_NAMES, _REGION_BLOCKERS)
    return (
        *_compile_table(insensitive, re.IGNORECASE, _REGION_BLOCKERS),
        *_compile_table(_REGION_ABBREVIATIONS, 0),
        *_build_foreign_cities(),
    )


_REGION_PHRASES: Final = _build_region()
_CATEGORICAL_PHRASES: Final = MappingProxyType(
    {key: _build_categorical(key) for key in CATEGORICAL_VALUES}
)

# --- span finders


def _phrase_spans(text: str, phrases: Iterable[_Phrase]) -> list[_Span]:
    return [_Span(m.start(), m.end(), p.value) for p in phrases for m in p.pattern.finditer(text)]


def _postal_spans(text: str) -> list[_Span]:
    """Find postal codes: whole phrase, after a comma ("Hartford, CT") or a qualifier ("SE PA")."""
    whole = _POSTAL_WHOLE.fullmatch(text)
    if whole:
        matches = [whole]
    else:
        matches = [*_POSTAL_AFTER_COMMA.finditer(text), *_POSTAL_AFTER_QUALIFIER.finditer(text)]
    return [
        _Span(m.start(), m.end(), _POSTAL_CODES[m["code"]])
        for m in matches
        if m["code"] in _POSTAL_CODES
    ]


def _region_spans(text: str) -> list[_Span]:
    return [*_phrase_spans(text, _REGION_PHRASES), *_postal_spans(text)]


def _zone_value(zones: str) -> str:
    zones = zones.casefold()
    return zones if _BARE_ZONE.fullmatch(zones) else _SEVERAL


def _zone_spans(text: str) -> list[_Span]:
    """Find zones: prefixed ("zone 6b", "zn 5", "USDA 7a") or the whole phrase ("6b").

    Zones of other systems ("Sunset zone 9", "AHS heat zone 7") are blocked.
    """
    if _BARE_ZONE.fullmatch(text):
        return [_Span(0, len(text), text.casefold())]
    mentions = [
        _Span(m.start(), m.end(), _zone_value(m["zones"])) for m in _ZONE_MENTION.finditer(text)
    ]
    blocked = [_Span(m.start(), m.end(), None) for m in _OTHER_ZONE_SYSTEM.finditer(text)]
    return [*mentions, *blocked]


def _year_spans(text: str) -> list[_Span]:
    return [
        _Span(m.start(), m.end(), m["year"] if m["range"] is None else _SEVERAL)
        for m in _YEAR.finditer(text)
    ]


_SPAN_FINDERS: Final[Mapping[str, Callable[[str], list[_Span]]]] = MappingProxyType(
    {
        "region": _region_spans,
        "zone": _zone_spans,
        "growing_year": _year_spans,
        **{k: partial(_phrase_spans, phrases=v) for k, v in _CATEGORICAL_PHRASES.items()},
    }
)
_KEYS_WITHOUT_RULES: Final = sorted(set(ENV_KEYS) - set(_SPAN_FINDERS))
if _KEYS_WITHOUT_RULES:  # pragma: no cover - guards future vocabulary edits
    raise ValueError(f"No normalization rule for environment keys: {_KEYS_WITHOUT_RULES}")

# --- resolution


def _priority(span: _Span) -> tuple[int, int, str]:
    return (span.start - span.end, span.start, span.value or "")


def _longest_first(spans: Iterable[_Span]) -> list[_Span]:
    """Greedily keep the longest spans that do not overlap an already kept span.

    Kept spans are disjoint, so each candidate is checked only against its neighbours in
    start order (found by bisection): O(k log k) rather than O(k^2) for k candidates. The
    result is in text order.
    """
    starts: list[int] = []
    kept: list[_Span] = []
    for span in sorted(spans, key=_priority):
        index = bisect_right(starts, span.start)
        before_ok = index == 0 or kept[index - 1].end <= span.start
        if before_ok and (index == len(kept) or span.end <= kept[index].start):
            starts.insert(index, span.start)
            kept.insert(index, span)
    return kept


@dataclass(frozen=True, slots=True)
class _Clauses:
    """Clause breaks and word positions of a text, computed once per text."""

    text: str
    breaks: tuple[int, ...]
    word_starts: tuple[int, ...]
    word_ends: tuple[int, ...]


def _clauses(text: str) -> _Clauses:
    words = list(_WORD.finditer(text))
    breaks = tuple(match.start() for match in _CLAUSE_BREAK.finditer(text))
    return _Clauses(text, breaks, tuple(w.start() for w in words), tuple(w.end() for w in words))


def _is_negated(clauses: _Clauses, span: _Span) -> bool:
    """Whether one of the last few words before ``span`` in its clause is a negation cue.

    Uses bisection over the precomputed positions, so checking every span of a long text
    stays O(k log n) instead of rescanning the text before each span.
    """
    n_breaks = bisect_left(clauses.breaks, span.start)
    clause_start = clauses.breaks[n_breaks - 1] + 1 if n_breaks else 0
    last = bisect_left(clauses.word_starts, span.start)
    first = max(bisect_left(clauses.word_starts, clause_start), last - _NEGATION_WINDOW)
    preceding = {
        clauses.text[clauses.word_starts[i] : min(clauses.word_ends[i], span.start)].casefold()
        for i in range(first, last)
    }
    return not _NEGATION_CUES.isdisjoint(preceding)


def _single(values: set[str]) -> str | None:
    return next(iter(values)) if len(values) == 1 else None


def _resolve_region(values: set[str]) -> str | None:
    """Resolve one region, letting a subdivision absorb its own country ("Maine, USA")."""
    subdivisions = {v for v in values if "-" in v}
    if len(subdivisions) != 1:
        return None if subdivisions else _single(values)
    (subdivision,) = subdivisions
    country = subdivision.split("-", 1)[0]
    return subdivision if values - subdivisions <= {country} else None


# --- public API


@dataclass(frozen=True, slots=True)
class WrittenSpan:
    """One resolved phrase of an as-written text.

    Attributes
    ----------
    start, end
        Offsets into the **original** text (before whitespace collapsing), so
        ``text[start:end]`` is exactly the matched phrase region.
    value
        The normalized value the phrase names, or ``None`` when it names nothing in the
        vocabulary: always for a blocker, and for a range, list or out-of-range value
        ("zones 6-7", "2022-2023", "zone 14").
    kind
        ``"value"`` for a phrase that states its value, ``"negated"`` for one preceded by a
        negation or low-quantity cue in its clause ("not humid", "less humid"), and
        ``"blocked"`` for a look-alike that names nothing ("clay pots", "Kansas City").
    """

    start: int
    end: int
    value: str | None
    kind: SpanKind


def _written_span(
    key: EnvKey, canonical: _Canonical, clauses: _Clauses, span: _Span
) -> WrittenSpan:
    start, end = canonical.original(span)
    if span.value is None:
        return WrittenSpan(start, end, None, "blocked")
    value = span.value if is_valid_normalized(key, span.value) else None
    kind: SpanKind = "negated" if _is_negated(clauses, span) else "value"
    return WrittenSpan(start, end, value, kind)


def resolve_spans(key: EnvKey, text: str) -> tuple[WrittenSpan, ...]:
    """Return the resolved phrases of ``text`` for one environment key.

    Candidates from the key's synonym tables, blockers and patterns are resolved longest
    phrase first into non-overlapping spans, exactly as :func:`normalize_written` does. Use it
    to check what a short phrase means at its own position inside a longer quote: "humid"
    inside "it wasn't humid" is ``"negated"``, "Virginia" inside "West Virginia" lies within a
    ``US-WV`` span, and "zone 6b" inside "zone 6b/7a" lies within a range span.

    Parameters
    ----------
    key
        Environment key whose rules apply.
    text
        Any text, e.g. a fact's evidence quote.

    Returns
    -------
    tuple of WrittenSpan
        The kept spans in text order, with offsets into ``text``; blockers included.

    Raises
    ------
    ValueError
        If ``key`` is not an environment key.
    """
    finder = _SPAN_FINDERS.get(key)
    if finder is None:
        raise ValueError(f"Unknown environment key: {key!r}")
    canonical = _canonicalize(text)
    clauses = _clauses(canonical.text)
    kept = _longest_first(finder(canonical.text))
    return tuple(_written_span(key, canonical, clauses, span) for span in kept)


def normalize_written(key: EnvKey, written: str) -> str | None:
    """Map an as-written phrase to a normalized vocabulary value, if unambiguous.

    Computed from :func:`resolve_spans`: the result is the single value named by the
    ``"value"`` spans (a region subdivision may absorb its own country).

    Parameters
    ----------
    key
        Environment key the phrase describes (selects the synonym table).
    written
        The phrase as written by the commenter, e.g. ``"muggy summers"`` or
        ``"Hartford, CT"``.

    Returns
    -------
    str or None
        A value for which ``vocab.is_valid_normalized(key, value)`` holds, or ``None`` when
        nothing is recognised, the only match is negated or blocked, or the phrase names two
        different values or a range (e.g. ``"zones 6-7"``, ``"Connecticut and Virginia"``).

    Raises
    ------
    ValueError
        If ``key`` is not an environment key.
    """
    named = [span.value for span in resolve_spans(key, written) if span.kind == "value"]
    if None in named:
        return None
    values = {value for value in named if value is not None}
    return _resolve_region(values) if key == "region" else _single(values)


_SINGULAR_RULES: Final = (("sses", "ss", 4), ("ches", "ch", 4), ("shes", "sh", 4))
_SINGULAR_RULES_LONG: Final = (("oes", "o", 4), ("ies", "y", 5))
_INVARIANT_ENDINGS: Final = ("ss", "us")
_INVARIANT_WORDS: Final = frozenset(
    {
        "arabis",
        "berberis",
        "cannabis",
        "clematis",
        "coreopsis",
        "digitalis",
        "hemerocallis",
        "iberis",
        "iris",
        "lychnis",
        "myosotis",
        "orris",
        "oxalis",
        "physalis",
        "pieris",
    }
)
"""Singular words ending in ``-is``; other ``-is`` words are plurals ("zucchinis", "kiwis")."""
_IRREGULAR_PLURALS: Final = MappingProxyType(
    {"chilies": "chili", "chillies": "chilli", "irises": "iris"}
)
"""Plurals the suffix rules would get wrong ("chilies" is not "chily")."""
_MIN_PLURAL_LENGTH: Final = 4
_POSSESSIVE: Final = "'s"


def _without_possessive(word: str) -> str:
    """Strip trailing ``'s`` (``tomato's`` -> ``tomato``), keeping at least one character."""
    while word.endswith(_POSSESSIVE) and len(word) > len(_POSSESSIVE):
        word = word.removesuffix(_POSSESSIVE)
    return word


def _singular(word: str) -> str:
    """Deterministically singularise one casefolded word (``tomatoes`` -> ``tomato``)."""
    word = _without_possessive(word)
    if word in _IRREGULAR_PLURALS:
        return _IRREGULAR_PLURALS[word]
    if (
        len(word) < _MIN_PLURAL_LENGTH
        or not word.endswith("s")
        or word.endswith(_INVARIANT_ENDINGS)
        or word in _INVARIANT_WORDS
    ):
        return word
    for suffix, replacement, min_length in (*_SINGULAR_RULES, *_SINGULAR_RULES_LONG):
        if word.endswith(suffix) and len(word) >= min_length:
            return word.removesuffix(suffix) + replacement
    return word.removesuffix("s")


def normalize_crop(crop: str) -> str:
    """Normalize a crop name for grouping and filtering.

    Casefolds, unifies typographic apostrophes/hyphens, strips and collapses whitespace, then
    singularises the last word: a trailing ``'s`` is dropped, ``chilies``/``chillies``/
    ``irises`` map to ``chili``/``chilli``/``iris``, and the usual suffix rules apply
    (``"Cherry Tomatoes"`` -> ``"cherry tomato"``, ``"zucchinis"`` -> ``"zucchini"``). Words
    ending in ``ss`` or ``us`` and a few singular ``-is`` words (``iris``, ``cannabis``) are
    unchanged.

    Parameters
    ----------
    crop
        Crop name as extracted, e.g. ``"Tomatoes"``.

    Returns
    -------
    str
        The normalized crop name.

    Raises
    ------
    ValueError
        If the name is empty or whitespace only.
    """
    words = crop.translate(_TYPOGRAPHIC).casefold().split()
    if not words:
        raise ValueError("Crop name is empty")
    return " ".join([*words[:-1], _singular(words[-1])])


CROP_SYNONYMS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "cuke": "cucumber",
        "cukes": "cucumber",
        "courgette": "zucchini",
        "courgettes": "zucchini",
        "aubergine": "eggplant",
        "aubergines": "eggplant",
        "spud": "potato",
        "spuds": "potato",
        "toms": "tomato",
        "tomatos": "tomato",
        "capsicum": "pepper",
        "capsicums": "pepper",
        "chile": "pepper",
        "chiles": "pepper",
        "chili": "pepper",
        "chilis": "pepper",
        "chilies": "pepper",
        "chilli": "pepper",
        "chillis": "pepper",
        "chillies": "pepper",
    }
)
"""Casefolded as-written crop words -> the normalized crop they also name, for
:func:`mentions_crop` ("cukes" names a cucumber; "chillies" names a pepper as well as a
chilli)."""

_CROP_WORD: Final = re.compile(r"[^\W_]+(?:'[^\W_]+)*")
"""A word of a crop name: letters/digits, with inner apostrophes; hyphens separate words."""
_MAX_CROP_WORDS: Final = 3


def _check_crop_synonyms() -> None:
    """Fail at import if a synonym is not a casefolded word or names a non-normalized crop."""
    bad = sorted(
        k for k, v in CROP_SYNONYMS.items() if k != _phrase_key(k) or normalize_crop(v) != v
    )
    if bad:
        raise ValueError(f"Crop synonyms are not casefolded or not normalized: {bad}")


_check_crop_synonyms()


def _crop_words(text: str) -> list[str]:
    return _CROP_WORD.findall(text.translate(_TYPOGRAPHIC))


def _crops_of(window: str) -> set[str]:
    forms = {window.casefold(), normalize_crop(window)}
    return forms | {CROP_SYNONYMS[form] for form in forms if form in CROP_SYNONYMS}


def crops_named(text: str) -> frozenset[str]:
    """Return every normalized crop that ``text`` names as whole words.

    A crop is named when a window of 1-3 consecutive words of ``text`` (words split on
    whitespace, hyphens and punctuation), casefolded, after :func:`normalize_crop` or via
    :data:`CROP_SYNONYMS`, equals it. Compute this once per text and test membership to check
    many crops against one text (``mentions_crop`` for each would rescan the text).

    Parameters
    ----------
    text
        Text to search, e.g. a search box query or an evidence quote.

    Returns
    -------
    frozenset of str
        The crops named (it also holds the casefolded windows themselves).
    """
    words = _crop_words(text)
    windows = {
        " ".join(words[i : i + n])
        for n in range(1, _MAX_CROP_WORDS + 1)
        for i in range(len(words) - n + 1)
    }
    return frozenset(crop for window in windows for crop in _crops_of(window))


def mentions_crop(text: str, crop: str) -> bool:
    """Return whether ``text`` names ``crop`` as whole words.

    True iff some window of 1-3 consecutive words of ``text`` (words split on whitespace,
    hyphens and punctuation), after :func:`normalize_crop` or via :data:`CROP_SYNONYMS`, equals
    the normalized crop: ``"My peppers were..."`` names ``pepper``, ``"the cukes"`` names
    ``cucumber``, while ``"pepperoni"`` does not name ``pepper``.

    Parameters
    ----------
    text
        Text to search, e.g. a mention's evidence quote.
    crop
        Crop name; normalized the same way (``"Tomatoes"`` and ``"tomato"`` are equal).

    Returns
    -------
    bool
        Whether the crop is named.

    Raises
    ------
    ValueError
        If ``crop`` has no letters or digits.
    """
    return normalize_crop(" ".join(_crop_words(crop))) in crops_named(text)


_NON_NAME_CHARACTERS: Final = re.compile(r"[^\w\s'-]|_")
_LOOSE_JOINERS: Final = re.compile(r"(?<!\w)['-]|['-](?!\w)")


def variety_key(variety: str) -> str:
    """Return the grouping key of a variety name.

    Casefolds, removes quotes and punctuation (keeping hyphens and apostrophes only between
    two word characters, as in ``"early-girl"`` or ``"aunt ruby's"``) and collapses whitespace:
    ``'"Sun Gold"'`` -> ``"sun gold"``.

    Parameters
    ----------
    variety
        Variety name as written.

    Returns
    -------
    str
        The variety key.

    Raises
    ------
    ValueError
        If nothing but punctuation and whitespace remains.
    """
    text = _NON_NAME_CHARACTERS.sub(" ", variety.translate(_TYPOGRAPHIC).casefold())
    key = " ".join(_LOOSE_JOINERS.sub(" ", text).split())
    if not key:
        raise ValueError("Variety name is empty after removing quotes and punctuation")
    return key


def parse_zone(value: str) -> tuple[int, str | None]:
    """Split a normalized hardiness zone into its number and optional subzone.

    Parameters
    ----------
    value
        A normalized zone such as ``"6b"`` or ``"6"``.

    Returns
    -------
    tuple of (int, str or None)
        ``(6, "b")`` for ``"6b"``; ``(6, None)`` for ``"6"``.

    Raises
    ------
    ValueError
        If ``value`` is not a normalized zone (``1``-``13`` with optional ``a``/``b``).
    """
    if ZONE_PATTERN.fullmatch(value) is None:
        raise ValueError(f"Not a normalized hardiness zone (1-13, optional a/b): {value!r}")
    number = value.rstrip("ab")
    return int(number), value.removeprefix(number) or None
