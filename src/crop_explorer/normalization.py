"""Deterministic mapping of as-written phrases onto the controlled vocabulary.

Every mapping is an explicit, module-level synonym table. Free text is matched
case-insensitively (two-letter postal codes and dotted state abbreviations excepted), with
whitespace collapsed and every phrase bounded by non-word characters. Overlapping candidates
are resolved **longest phrase first** over non-overlapping spans, so "heat wave" is a heatwave
rather than "hot" and "West Virginia" is not Virginia. A phrase preceded by a negation cue in
the same clause ("not humid", "no greenhouse") is discarded, and some look-alike phrases
("hot peppers", "clay pots") are blocked outright. If two different values remain, the phrase
is ambiguous and nothing is normalized; the only exception is a region subdivision named with
its own country ("Ontario, Canada"). Region, zone and year are never inferred from one another
or from any other condition.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import partial
from types import MappingProxyType
from typing import Final

from crop_explorer.regions import CA_PROVINCES, REGION_NAMES, US_STATES
from crop_explorer.schemas import ENV_KEYS, EnvKey
from crop_explorer.vocab import CATEGORICAL_VALUES, ZONE_PATTERN, is_valid_normalized

STRICT_KEYS: Final[frozenset[EnvKey]] = frozenset({"region", "zone", "growing_year"})
"""Keys whose stored value must always equal ``normalize_written`` of the as-written phrase."""

_SynonymTable = Mapping[str, tuple[str, ...]]
"""Normalized value -> the phrases that express it."""

# --- synonym tables: categorical keys

_GREENHOUSE_WORDS: Final = ("greenhouse", "glasshouse", "hoop house", "high tunnel", "poly tunnel")
_DETERMINERS: Final = ("the", "a", "my", "our")


def _relative_to_greenhouse(preposition: str) -> tuple[str, ...]:
    """Return phrases like ``"inside the greenhouse"`` for every determiner and structure."""
    return tuple(f"{preposition} {d} {w}" for d in _DETERMINERS for w in _GREENHOUSE_WORDS)


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
            *_relative_to_greenhouse("outside"),
        ),
        "greenhouse": (
            *_GREENHOUSE_WORDS,
            "greenhouses",
            "hoop houses",
            "high tunnels",
            "poly tunnels",
            *_relative_to_greenhouse("inside"),
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
            "heat",
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
        "humid": ("humid", "humidity", "high humidity", "muggy", "sticky", "steamy"),
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
            "rain",
            "rains",
            "rainy",
            "rained",
            "raining",
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
            "part shade",
            "partial shade",
            "part sun",
            "partial sun",
            "afternoon shade",
            "morning sun",
            "dappled shade",
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

_CATEGORICAL_BLOCKERS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "method": (),
        "shelter": (
            "started indoors",
            "start indoors",
            "starting indoors",
            "started inside",
            "start inside",
            "starting inside",
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
            "mild winter",
            "mild winters",
            "mild flavor",
            "cool season",
            "cool weather crop",
            "cool weather crops",
        ),
        "humidity": ("humidity dome", "humidity domes", "humidity tray", "humidity trays"),
        "precipitation": (
            "rain barrel",
            "rain barrels",
            "rain garden",
            "rain gauge",
            "rain fed",
            "only rain",
        ),
        "season_length": (),
        "sun": ("shade cloth", "shade cloths", "shade netting"),
        "soil": ("clay pot", "clay pots", "clay pebbles", "clay balls"),
        "irrigation": ("pollinated by hand", "picked by hand", "weeded by hand", "pulled by hand"),
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
    }
)
_NEGATION_WINDOW: Final = 3
"""How many words before a phrase (within its clause) are searched for a negation cue."""
_CLAUSE_BREAK: Final = re.compile(r"[,;:.!?()\[\]{}]")
_WORD: Final = re.compile(r"[\w'-]+")

_SEVERAL: Final = "<several>"
"""Placeholder value for a range or list ("zones 6-7", "2022-2023"); never a valid value."""

_ZONE_ITEM: Final = r"\d{1,2}[ab]?(?!\w)"
_ZONE_JOINER: Final = r"\s*(?:[-/&,+\u2013\u2014]|\bto\b|\bor\b|\band\b)\s*"
_ZONE_MENTION: Final = re.compile(
    r"(?<![\w-])(?:(?:usda\s+)?(?:hardiness\s+)?zones?\s*|usda\s+|z(?=\d))"
    rf"(?P<zones>{_ZONE_ITEM}(?:{_ZONE_JOINER}{_ZONE_ITEM}|/[ab](?!\w))*)",
    re.IGNORECASE,
)
_BARE_ZONE: Final = re.compile(_ZONE_ITEM, re.IGNORECASE)
_YEAR: Final = re.compile(
    r"(?<!\w)(?P<year>(?:19|20)\d{2})(?!\w)"
    r"(?P<range>\s*[-/\u2013\u2014]\s*\d{2}(?:\d{2})?(?!\w))?"
)
_POSTAL_WHOLE: Final = re.compile(r"[A-Z]{2}")
_POSTAL_AFTER_COMMA: Final = re.compile(
    r"(?:(?<=,)|(?<=, ))[A-Z]{2}"
    r"(?=\s*(?:\Z|[^\w\s-]|\d|(?:USA|U\.S\.|Canada)(?![\w-])))"
)


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


def _canonical(text: str) -> str:
    """Unify typographic apostrophes/hyphens and collapse whitespace."""
    return " ".join(text.translate(_TYPOGRAPHIC).split())


def _phrase_key(phrase: str) -> str:
    return " ".join(_TOKEN_SPLIT.split(phrase.strip().casefold()))


def _compile_phrase(phrase: str, flags: int) -> re.Pattern[str]:
    """Compile a phrase whose tokens may be joined by spaces, hyphens or nothing."""
    body = _SEPARATOR.join(re.escape(token) for token in _TOKEN_SPLIT.split(phrase.strip()))
    return re.compile(f"{_LEFT_EDGE}{body}{_RIGHT_EDGE}", flags)


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


def _build_region() -> tuple[_Phrase, ...]:
    _check_table("region aliases", _REGION_ALIASES, REGION_NAMES)
    _check_table("region abbreviations", _REGION_ABBREVIATIONS, REGION_NAMES)
    insensitive = {
        code: (name, *_REGION_ALIASES.get(code, ())) for code, name in REGION_NAMES.items()
    }
    _check_table("region names and aliases", insensitive, REGION_NAMES)
    return (
        *_compile_table(insensitive, re.IGNORECASE),
        *_compile_table(_REGION_ABBREVIATIONS, 0),
    )


_REGION_PHRASES: Final = _build_region()
_CATEGORICAL_PHRASES: Final = MappingProxyType(
    {key: _build_categorical(key) for key in CATEGORICAL_VALUES}
)

# --- span finders


def _phrase_spans(text: str, phrases: Iterable[_Phrase]) -> list[_Span]:
    return [_Span(m.start(), m.end(), p.value) for p in phrases for m in p.pattern.finditer(text)]


def _postal_spans(text: str) -> list[_Span]:
    """Find postal codes: only the whole phrase ("CT") or right after a comma ("Hartford, CT")."""
    whole = _POSTAL_WHOLE.fullmatch(text)
    matches = [whole] if whole else list(_POSTAL_AFTER_COMMA.finditer(text))
    return [
        _Span(m.start(), m.end(), _POSTAL_CODES[m[0]]) for m in matches if m[0] in _POSTAL_CODES
    ]


def _region_spans(text: str) -> list[_Span]:
    return [*_phrase_spans(text, _REGION_PHRASES), *_postal_spans(text)]


def _zone_value(zones: str) -> str:
    zones = zones.casefold()
    return zones if _BARE_ZONE.fullmatch(zones) else _SEVERAL


def _zone_spans(text: str) -> list[_Span]:
    """Zones need a zone prefix ("zone 6b", "z6b", "USDA 7a") unless they are the whole phrase."""
    if _BARE_ZONE.fullmatch(text):
        return [_Span(0, len(text), text.casefold())]
    return [
        _Span(m.start(), m.end(), _zone_value(m["zones"])) for m in _ZONE_MENTION.finditer(text)
    ]


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
    """Greedily keep the longest spans that do not overlap an already kept span."""
    kept: list[_Span] = []
    for span in sorted(spans, key=_priority):
        if all(span.end <= other.start or other.end <= span.start for other in kept):
            kept.append(span)
    return kept


def _is_negated(text: str, span: _Span) -> bool:
    clause = _CLAUSE_BREAK.split(text[: span.start])[-1]
    preceding = _WORD.findall(clause.casefold())[-_NEGATION_WINDOW:]
    return not _NEGATION_CUES.isdisjoint(preceding)


def _surviving_values(text: str, spans: Iterable[_Span]) -> set[str]:
    kept = _longest_first(spans)
    return {s.value for s in kept if s.value is not None and not _is_negated(text, s)}


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


def normalize_written(key: EnvKey, written: str) -> str | None:
    """Map an as-written phrase to a normalized vocabulary value, if unambiguous.

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
        nothing is recognised, the only match is negated, or the phrase names two different
        values (e.g. ``"zones 6-7"``, ``"Connecticut and Virginia"``).

    Raises
    ------
    ValueError
        If ``key`` is not an environment key.
    """
    finder = _SPAN_FINDERS.get(key)
    if finder is None:
        raise ValueError(f"Unknown environment key: {key!r}")
    text = _canonical(written)
    values = _surviving_values(text, finder(text))
    value = _resolve_region(values) if key == "region" else _single(values)
    if value is None or not is_valid_normalized(key, value):
        return None
    return value


_SINGULAR_RULES: Final = (("sses", "ss", 4), ("ches", "ch", 4), ("shes", "sh", 4))
_SINGULAR_RULES_LONG: Final = (("oes", "o", 4), ("ies", "y", 5))
_INVARIANT_ENDINGS: Final = ("ss", "us", "is")
_MIN_PLURAL_LENGTH: Final = 4


def _singular(word: str) -> str:
    """Deterministically singularise one casefolded word (``tomatoes`` -> ``tomato``)."""
    if (
        len(word) < _MIN_PLURAL_LENGTH
        or not word.endswith("s")
        or word.endswith(_INVARIANT_ENDINGS)
    ):
        return word
    for suffix, replacement, min_length in (*_SINGULAR_RULES, *_SINGULAR_RULES_LONG):
        if word.endswith(suffix) and len(word) >= min_length:
            return word.removesuffix(suffix) + replacement
    return word.removesuffix("s")


def normalize_crop(crop: str) -> str:
    """Normalize a crop name for grouping and filtering.

    Casefolds, strips and collapses whitespace, then singularises the last word
    (``"Cherry Tomatoes"`` -> ``"cherry tomato"``; words ending in ``ss``, ``us`` or ``is``
    are unchanged).

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
    words = crop.casefold().split()
    if not words:
        raise ValueError("Crop name is empty")
    return " ".join([*words[:-1], _singular(words[-1])])


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
