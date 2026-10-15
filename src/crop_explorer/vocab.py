"""Small controlled vocabulary for growing-environment facts and viewer conditions.

Keeps normalized values, viewer-selectable values and human-readable labels in one place.
Synonym mapping from free text lives in :mod:`crop_explorer.normalization`.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import Final, Literal, get_args

from crop_explorer.regions import REGION_NAMES
from crop_explorer.schemas import EnvKey

ComparableKey = Literal[
    "zone",
    "method",
    "shelter",
    "summer_heat",
    "humidity",
    "precipitation",
    "season_length",
    "sun",
    "soil",
    "irrigation",
]
"""Environment dimensions compared against the viewer's garden to form relevance tiers.

``region`` is deliberately absent: a location is a preference/filter, never a climate
comparison. ``growing_year`` is a filter, not a garden property.
"""
COMPARABLE_KEYS: tuple[ComparableKey, ...] = get_args(ComparableKey)

CATEGORICAL_VALUES: Final = MappingProxyType(
    {
        "method": ("container", "raised_bed", "in_ground", "hydroponic"),
        "shelter": ("outdoors", "greenhouse", "indoors"),
        "summer_heat": ("hot", "mild", "cool", "heatwave"),
        "humidity": ("humid", "dry_air"),
        "precipitation": ("rainy", "dry_season"),
        "season_length": ("short_season", "long_season"),
        "sun": ("full_sun", "part_shade", "shade"),
        "soil": ("clay", "sandy", "loam", "potting_mix"),
        "irrigation": ("rain_fed", "drip", "hand_watered"),
    }
)
"""Allowed normalized values for categorical keys (``heatwave`` is an event, not a climate)."""

ZONE_PATTERN: Final = re.compile(r"^(?:[1-9]|1[0-3])[ab]?$")
"""USDA hardiness zone: ``1``-``13`` optionally followed by subzone ``a``/``b``."""

YEAR_PATTERN: Final = re.compile(r"^(?:19|20)\d{2}$")

ZONE_VALUES: tuple[str, ...] = tuple(z for n in range(1, 14) for z in (str(n), f"{n}a", f"{n}b"))

VIEWER_VALUES: Final = MappingProxyType(
    {
        "zone": ZONE_VALUES,
        **{k: tuple(v for v in vals if v != "heatwave") for k, vals in CATEGORICAL_VALUES.items()},
    }
)
"""Values a viewer may select per comparable key (events like ``heatwave`` excluded)."""

SELF_DESCRIBED_KEYS: tuple[ComparableKey, ...] = (
    "summer_heat",
    "humidity",
    "precipitation",
    "season_length",
)
"""Comparable keys whose reported values are the gardener's own, uncalibrated words.

"Hot", "humid", "rainy" or "a short season" are relative to what the writer is used to: a
Minnesotan's hot summer is not a Phoenician's. A reported value equal to the viewer's still
matches, but an opposite value is ``described_differently`` and is never counted as a
mismatch (never the ``different`` tier). Zone, method, shelter, sun, soil and irrigation are
checkable facts and keep strict comparison; ``region`` is a location preference only.
"""

DESCRIBED_AS: Final = MappingProxyType(
    {
        "hot": ("summers", "hot"),
        "mild": ("summers", "mild"),
        "cool": ("summers", "cool"),
        "humid": ("it", "humid"),
        "dry_air": ("the air", "dry"),
        "rainy": ("it", "rainy"),
        "dry_season": ("it", "having a dry spell or dry season"),
        "short_season": ("the season", "short"),
        "long_season": ("the season", "long"),
    }
)
"""``(subject, description)`` per selectable value of a self-described key, read as "they
described <subject> as <description>" (e.g. "they described summers as mild")."""

PROMINENT_KEYS: tuple[ComparableKey, ...] = ("zone", "method", "shelter")
"""Comparable keys always visible besides region (SPEC §2 "Which controls are prominent?").

Heat, season length, humidity, soil, sunlight and watering are one tap away.
"""

KEY_LABELS: Final = MappingProxyType(
    {
        "region": "region",
        "zone": "hardiness zone",
        "method": "growing method",
        "shelter": "setting",
        "summer_heat": "summer heat",
        "humidity": "humidity",
        "precipitation": "rainfall",
        "season_length": "season length",
        "sun": "sun exposure",
        "soil": "soil",
        "irrigation": "watering",
        "growing_year": "growing year",
    }
)

VALUE_LABELS: Final = MappingProxyType(
    {
        "container": "containers",
        "raised_bed": "raised beds",
        "in_ground": "in the ground",
        "hydroponic": "hydroponics",
        "outdoors": "outdoors",
        "greenhouse": "greenhouse / tunnel",
        "indoors": "indoors",
        "hot": "hot summers",
        "mild": "mild summers",
        "cool": "cool summers",
        "heatwave": "a heatwave",
        "humid": "humid",
        "dry_air": "dry air",
        "rainy": "rainy",
        "dry_season": "dry spell / dry season",
        "short_season": "short season",
        "long_season": "long season",
        "full_sun": "full sun",
        "part_shade": "part shade",
        "shade": "shade",
        "clay": "clay soil",
        "sandy": "sandy soil",
        "loam": "loam",
        "potting_mix": "potting mix",
        "rain_fed": "rain-fed",
        "drip": "drip irrigation",
        "hand_watered": "hand watering",
    }
)


def is_valid_normalized(key: EnvKey, value: str) -> bool:
    """Return whether ``value`` is an allowed normalized value for ``key``."""
    if key == "region":
        return value in REGION_NAMES
    if key == "zone":
        return ZONE_PATTERN.fullmatch(value) is not None
    if key == "growing_year":
        return YEAR_PATTERN.fullmatch(value) is not None
    return value in CATEGORICAL_VALUES[key]


def value_label(key: EnvKey, value: str) -> str:
    """Return a short human-readable label for a normalized value."""
    if key == "region":
        return REGION_NAMES[value]
    if key == "zone":
        return f"zone {value}"
    return VALUE_LABELS.get(value, value)
