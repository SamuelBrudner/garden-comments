"""Packaged regional climate references for the map's optional overlays (pure loaders).

The files under ``data/overlays/`` are built offline by ``scripts/build_overlays.py`` from
public sources; ``NOTICE.md`` and ``sources.yaml`` next to them record the exact URLs,
retrieval dates, terms and every alteration. They are **regional climate references**
(SPEC §7): context for the region a viewer picks, never evidence about a variety, a report
or a commenter. Nothing here reads comments: zones are never assigned to reports by
geography, and no climate value is ever attached to a comment (SPEC §6).

- :func:`load_zone_layer`: simplified 2023 USDA Plant Hardiness Zone half-zone polygons
  (contiguous U.S., Alaska and Hawaii), plus their legend/table twin. Not the official map:
  every display must show :data:`ZONE_DISCLAIMER`.
- :func:`load_state_zone_ranges`: the half-zones covering at least
  :data:`MIN_STATE_ZONE_SHARE` of each state's area, with its coldest and warmest.
- :func:`load_state_normals`: statewide 1991-2020 averages of three NOAA nClimDiv series.
  Statistical averages over a whole state, not observed weather in any season or garden.
- :func:`check_served_copy`: the viewer's browser-served copy of the zone layer must be
  byte-identical to the packaged file.

Only stdlib ``json``/``csv`` and pandas are used: the geometry tooling that builds the files
is not a runtime dependency. Every loader fails loud with :class:`OverlayDataError`.
"""

from __future__ import annotations

import csv
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import pandas as pd

from crop_explorer.regions import US_STATES, is_us_state
from crop_explorer.vocab import ZONE_PATTERN

OVERLAY_DIR: Final = Path(__file__).resolve().parent / "data" / "overlays"
"""Directory of the packaged overlay files."""
ZONES_FILE: Final = "phzm_2023_simplified.geojson"
STATE_ZONES_FILE: Final = "state_zone_ranges.csv"
NORMALS_FILE: Final = "noaa_state_normals_1991_2020.csv"
NOTICE_FILE: Final = "NOTICE.md"
SOURCES_FILE: Final = "sources.yaml"
SERVED_ZONES_NAME: Final = "phzm_2023_simplified.json"
"""Name of the zone layer's byte-identical copy that the viewer serves to browsers from its
``static/overlays/`` folder (the zone figure names its URL instead of embedding 0.9 MB)."""

OVERLAY_LABEL: Final = "regional climate reference"
"""The evidence label every overlay panel carries (SPEC §7)."""
ZONE_DISCLAIMER: Final = (
    "Simplified from the 2023 USDA Plant Hardiness Zone Map GIS data (PRISM Climate Group, "
    "Oregon State University). This is not the official USDA Plant Hardiness Zone Map — see "
    "planthardiness.ars.usda.gov."
)
"""Required wherever the zone layer or a value derived from it is shown (OSU terms, (2a))."""
ZONE_SOURCE: Final = (
    "2023 USDA Plant Hardiness Zone Map GIS data, PRISM Climate Group, Oregon State University"
)
ZONE_PERIOD: Final = "1991-2020 average annual extreme minimum winter temperature"
ZONE_COVERAGE: Final = "Contiguous U.S., Alaska and Hawaii (Puerto Rico not included)"
NORMALS_SOURCE: Final = (
    "NOAA NCEI Climate at a Glance: Statewide Time Series, 1991-2020 average (nClimDiv)"
)
NORMALS_PERIOD: Final = "1991-2020"
NORMALS_FIRST_YEAR: Final = 1991
NORMALS_LAST_YEAR: Final = 2020
NORMALS_N_YEARS: Final = NORMALS_LAST_YEAR - NORMALS_FIRST_YEAR + 1
"""Every statewide average is the mean of exactly this many annual values."""
NORMAL_ELEMENTS: Final = MappingProxyType(
    {
        "july_avg_high_f": "July average high (°F)",
        "jan_avg_low_f": "January average low (°F)",
        "annual_precip_in": "Annual precipitation (inches)",
    }
)
"""Statewide average elements, in display order, with their labels."""
MIN_STATE_ZONE_SHARE: Final = 0.001
"""A half-zone is listed for a state when it covers at least this share (0.1 %) of the
state's zone-mapped area; smaller slivers are mostly boundary misalignment."""
ZONE_GEOJSON_MAX_BYTES: Final = 1_500_000
"""Size budget of the zone GeoJSON, so the layer renders quickly on a phone."""

HALF_ZONES: Final = tuple(f"{number}{half}" for number in range(1, 14) for half in "ab")
"""Every USDA half-zone, coldest (``1a``) to warmest (``13b``)."""
ZONE_TABLE_COLUMNS: Final = ("zone", "zone_number", "temp_range_f", "rank")
STATE_ZONE_COLUMNS: Final = ("region_code", "zone", "area_share", "is_coldest", "is_warmest")
NORMALS_COLUMNS: Final = ("region_code", "element", "value", "n_years")

_RANK: Final = MappingProxyType({zone: rank for rank, zone in enumerate(HALF_ZONES)})
_COLDEST_LOW_F: Final = -60
"""Lower bound of half-zone ``1a`` (degrees Fahrenheit)."""
_HALF_ZONE_STEP_F: Final = 5
_PROPERTY_KEYS: Final = frozenset({"zone", "zone_number", "temp_range_f"})
_POLYGONAL: Final = frozenset({"Polygon", "MultiPolygon"})
_MIN_RING_POSITIONS: Final = 4
_MAX_LON: Final = 180.0
_MAX_LAT: Final = 90.0
_SHARE_SUM_SLACK: Final = 1e-4
"""Rounded shares of one state may sum to slightly more than 1."""
_BOOLS: Final = MappingProxyType({"true": True, "false": False})


class OverlayDataError(ValueError):
    """A packaged overlay file is missing, malformed, or breaks its documented contract."""


@dataclass(frozen=True, eq=False)
class ZoneLayer:
    """The simplified half-zone polygons and their legend/table twin.

    Attributes
    ----------
    geojson : Mapping[str, Any]
        A GeoJSON ``FeatureCollection`` with one feature per half-zone, ordered coldest to
        warmest, properties ``zone``, ``zone_number`` and ``temp_range_f``, and the top-level
        ``disclaimer`` :data:`ZONE_DISCLAIMER`. Exterior rings are clockwise and holes
        counter-clockwise (the d3-geo convention Plotly draws; RFC 7946's opposite winding
        would fill the whole globe).
    table : pandas.DataFrame
        One row per feature in the same order, columns :data:`ZONE_TABLE_COLUMNS`.
    """

    geojson: Mapping[str, Any]
    table: pd.DataFrame


@dataclass(frozen=True, eq=False)
class Overlays:
    """All three packaged references, validated.

    Attributes
    ----------
    zones : ZoneLayer
        Output of :func:`load_zone_layer`.
    state_zones : pandas.DataFrame
        Output of :func:`load_state_zone_ranges`.
    normals : pandas.DataFrame
        Output of :func:`load_state_normals`.
    """

    zones: ZoneLayer
    state_zones: pd.DataFrame
    normals: pd.DataFrame


def zone_rank(zone: str) -> int:
    """Return the cold-to-warm rank of a half-zone (``1a`` -> 0, ``13b`` -> 25).

    Raises
    ------
    OverlayDataError
        If ``zone`` is not a USDA half-zone such as ``6b``.
    """
    if ZONE_PATTERN.fullmatch(zone) is None or zone not in _RANK:
        raise OverlayDataError(f"Not a USDA half-zone: {zone!r}")
    return _RANK[zone]


def zone_temp_range_f(zone: str) -> str:
    """Return a half-zone's defining temperature range, e.g. ``"-10 to -5"`` for ``6a``.

    The range is of the average annual extreme minimum winter temperature, in °F.
    """
    low = _COLDEST_LOW_F + _HALF_ZONE_STEP_F * zone_rank(zone)
    return f"{low} to {low + _HALF_ZONE_STEP_F}"


def load_overlays(directory: Path = OVERLAY_DIR) -> Overlays:
    """Load and validate the three packaged overlay files from ``directory``."""
    return Overlays(
        zones=load_zone_layer(directory / ZONES_FILE),
        state_zones=load_state_zone_ranges(directory / STATE_ZONES_FILE),
        normals=load_state_normals(directory / NORMALS_FILE),
    )


def check_served_copy(path: Path, packaged: Path = OVERLAY_DIR / ZONES_FILE) -> None:
    """Require the browser-served copy of the zone layer to equal the packaged file, byte for byte.

    Browsers draw the zone layer from that copy (by URL), while every check here runs on the
    packaged file, so a stale or missing copy would show unchecked data or an empty map.

    Raises
    ------
    OverlayDataError
        If either file cannot be read, or the copy differs from the packaged file.
    """
    try:
        served, expected = path.read_bytes(), packaged.read_bytes()
    except OSError as error:
        raise OverlayDataError(f"The browser copy of the zone layer is missing: {error}") from error
    if served != expected:
        raise OverlayDataError(
            f"{path.name} differs from the packaged {packaged.name}: copy it again"
        )


def load_zone_layer(path: Path = OVERLAY_DIR / ZONES_FILE) -> ZoneLayer:
    """Load and validate the simplified half-zone GeoJSON.

    Raises
    ------
    OverlayDataError
        If the file is missing or not JSON, is not a ``FeatureCollection`` carrying
        :data:`ZONE_DISCLAIMER`, a feature's properties disagree with its zone, a geometry
        is not a well-formed lon/lat polygon with clockwise exteriors, or the features are not
        distinct half-zones ordered coldest to warmest.
    """
    data = _read_json(path)
    _check_collection(data)
    rows = [_check_feature(feature) for feature in data["features"]]
    ranks = [row[3] for row in rows]
    if ranks != sorted(set(ranks)):
        raise OverlayDataError(f"{path.name}: features must be distinct, coldest to warmest")
    table = pd.DataFrame(rows, columns=list(ZONE_TABLE_COLUMNS))
    return ZoneLayer(geojson=data, table=table)


def load_state_zone_ranges(path: Path = OVERLAY_DIR / STATE_ZONES_FILE) -> pd.DataFrame:
    """Load and validate the per-state half-zone shares.

    Returns
    -------
    pandas.DataFrame
        Columns :data:`STATE_ZONE_COLUMNS`, one row per (U.S. state, half-zone covering at
        least :data:`MIN_STATE_ZONE_SHARE` of it), sorted by ``region_code`` then cold to
        warm. Exactly one row per state is ``is_coldest`` and one ``is_warmest``.

    Raises
    ------
    OverlayDataError
        If a column, region code, zone, share or flag is invalid, a (state, zone) pair
        repeats, the rows are out of order, or a U.S. state (or DC) is missing.
    """
    raw = _read_csv(path, STATE_ZONE_COLUMNS)
    for code in raw["region_code"]:
        _check_us_state(code, path)
    ranks = [zone_rank(zone) for zone in raw["zone"]]
    table = raw.assign(
        area_share=_floats(raw["area_share"], path),
        is_coldest=_flags(raw["is_coldest"], path),
        is_warmest=_flags(raw["is_warmest"], path),
    )
    _check_order(list(zip(table["region_code"], ranks, strict=True)), path)
    _check_shares(table, path)
    _check_extremes(table.assign(rank=ranks), path)
    return table


def load_state_normals(path: Path = OVERLAY_DIR / NORMALS_FILE) -> pd.DataFrame:
    """Load and validate the statewide 1991-2020 averages.

    Returns
    -------
    pandas.DataFrame
        Columns :data:`NORMALS_COLUMNS` (``value`` float, ``n_years`` int), one row per
        (U.S. state, element of :data:`NORMAL_ELEMENTS`), sorted by ``region_code`` then
        element order. States without NOAA statewide series (DC) have no rows.

    Raises
    ------
    OverlayDataError
        If a column, region code, element or value is invalid, ``n_years`` is not
        :data:`NORMALS_N_YEARS`, a state lacks an element, or the rows are out of order.
    """
    raw = _read_csv(path, NORMALS_COLUMNS)
    for code in raw["region_code"]:
        _check_us_state(code, path)
    order = list(NORMAL_ELEMENTS)
    bad = sorted(set(raw["element"]) - set(order))
    if bad:
        raise OverlayDataError(f"{path.name}: unknown elements {bad}")
    table = raw.assign(value=_floats(raw["value"], path), n_years=_ints(raw["n_years"], path))
    if (table["n_years"] != NORMALS_N_YEARS).any():
        raise OverlayDataError(f"{path.name}: every value must average {NORMALS_N_YEARS} years")
    keys = list(zip(table["region_code"], table["element"].map(order.index), strict=True))
    _check_order(keys, path)
    _check_complete(table, path)
    return table


# --------------------------------------------------------------------------- file readers


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise OverlayDataError(f"Cannot read overlay file {path.name}: {error}") from error
    if not isinstance(data, dict):
        raise OverlayDataError(f"{path.name}: expected a JSON object")
    return data


def _read_csv(path: Path, columns: Sequence[str]) -> pd.DataFrame:
    """Read a CSV as text cells, requiring exactly ``columns`` and no blank cell."""
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, [])
            rows = list(reader)
    except OSError as error:
        raise OverlayDataError(f"Cannot read overlay file {path.name}: {error}") from error
    if tuple(header) != tuple(columns):
        raise OverlayDataError(f"{path.name}: columns must be exactly {tuple(columns)}")
    if not rows or any(len(row) != len(columns) or "" in row for row in rows):
        raise OverlayDataError(f"{path.name}: needs rows of {len(columns)} non-blank cells")
    return pd.DataFrame(rows, columns=list(columns), dtype="str")


def _floats(cells: pd.Series[str], path: Path) -> pd.Series[float]:
    try:
        values = cells.astype("float64")
    except ValueError as error:
        raise OverlayDataError(f"{path.name}: non-numeric value") from error
    if not all(math.isfinite(value) for value in values):
        raise OverlayDataError(f"{path.name}: values must be finite numbers (no NaN)")
    return values


def _ints(cells: pd.Series[str], path: Path) -> pd.Series[int]:
    if not all(cell.isdigit() for cell in cells):
        raise OverlayDataError(f"{path.name}: n_years must be a whole number")
    return cells.astype("int64")


def _flags(cells: pd.Series[str], path: Path) -> pd.Series[bool]:
    if not set(cells) <= set(_BOOLS):
        raise OverlayDataError(f"{path.name}: flags must be 'true' or 'false'")
    return cells.map(_BOOLS).astype("bool")


# --------------------------------------------------------------------------- table checks


def _check_us_state(code: str, path: Path) -> None:
    if not is_us_state(code):
        raise OverlayDataError(f"{path.name}: {code!r} is not a U.S. state code like 'US-CT'")


def _check_order(keys: list[tuple[str, int]], path: Path) -> None:
    """Rows must be sorted by (region, rank) with no repeated key."""
    if keys != sorted(set(keys)):
        raise OverlayDataError(f"{path.name}: rows must be unique and sorted by region code")


def _check_shares(table: pd.DataFrame, path: Path) -> None:
    shares = table["area_share"]
    if ((shares < MIN_STATE_ZONE_SHARE) | (shares > 1)).any():
        raise OverlayDataError(f"{path.name}: area_share must be in [{MIN_STATE_ZONE_SHARE}, 1]")
    if (table.groupby("region_code")["area_share"].sum() > 1 + _SHARE_SUM_SLACK).any():
        raise OverlayDataError(f"{path.name}: a state's area shares sum to more than 1")
    missing = sorted(set(US_STATES) - set(table["region_code"]))
    if missing:
        raise OverlayDataError(f"{path.name}: missing states {missing}")


def _check_extremes(table: pd.DataFrame, path: Path) -> None:
    """Exactly the coldest and the warmest listed half-zone of each state carry the flags."""
    by_state = table.groupby("region_code")["rank"]
    coldest = table["rank"] == by_state.transform("min")
    warmest = table["rank"] == by_state.transform("max")
    if not (coldest.equals(table["is_coldest"]) and warmest.equals(table["is_warmest"])):
        raise OverlayDataError(f"{path.name}: is_coldest/is_warmest must mark each extreme")


def _check_complete(table: pd.DataFrame, path: Path) -> None:
    counts = table.groupby("region_code")["element"].nunique()
    incomplete = sorted(counts[counts != len(NORMAL_ELEMENTS)].index)
    if incomplete:
        raise OverlayDataError(f"{path.name}: states missing an element: {incomplete}")


# --------------------------------------------------------------------------- GeoJSON checks


def _check_collection(data: Mapping[str, Any]) -> None:
    if data.get("type") != "FeatureCollection":
        raise OverlayDataError("Zone layer must be a GeoJSON FeatureCollection")
    if data.get("disclaimer") != ZONE_DISCLAIMER:
        raise OverlayDataError("Zone layer must carry the 'not the official map' disclaimer")
    features = data.get("features")
    if not isinstance(features, list) or not features:
        raise OverlayDataError("Zone layer has no features")


def _check_feature(feature: Any) -> tuple[str, int, str, int]:
    """Validate one feature; return its table row (zone, zone_number, temp_range_f, rank)."""
    if not isinstance(feature, dict) or feature.get("type") != "Feature":
        raise OverlayDataError("Zone layer: every item must be a GeoJSON Feature")
    properties = feature.get("properties")
    if not isinstance(properties, dict) or set(properties) != _PROPERTY_KEYS:
        raise OverlayDataError(f"Zone layer: properties must be exactly {sorted(_PROPERTY_KEYS)}")
    zone = str(properties["zone"])
    rank = zone_rank(zone)
    number, temp_range = properties["zone_number"], properties["temp_range_f"]
    if type(number) is not int or number != int(zone[:-1]):
        raise OverlayDataError(f"Zone layer: zone {zone} has the wrong zone_number")
    if temp_range != zone_temp_range_f(zone):
        raise OverlayDataError(f"Zone layer: zone {zone} has the wrong temp_range_f")
    _check_geometry(feature.get("geometry"), zone)
    return (zone, number, temp_range, rank)


def _check_geometry(geometry: Any, zone: str) -> None:
    if not isinstance(geometry, dict) or geometry.get("type") not in _POLYGONAL:
        raise OverlayDataError(f"Zone layer: zone {zone} must be a Polygon or MultiPolygon")
    coordinates = geometry.get("coordinates")
    polygons = [coordinates] if geometry["type"] == "Polygon" else coordinates
    if not isinstance(polygons, list) or not polygons:
        raise OverlayDataError(f"Zone layer: zone {zone} has no polygons")
    for rings in polygons:
        if not isinstance(rings, list) or not rings:
            raise OverlayDataError(f"Zone layer: zone {zone} has a polygon without rings")
        for index, ring in enumerate(rings):
            _check_ring(ring, zone, exterior=index == 0)


def _check_ring(ring: Any, zone: str, *, exterior: bool) -> None:
    """Require a closed lon/lat ring, clockwise when ``exterior`` else counter-clockwise."""
    if not isinstance(ring, list) or len(ring) < _MIN_RING_POSITIONS or ring[0] != ring[-1]:
        raise OverlayDataError(f"Zone layer: zone {zone} has an unclosed or degenerate ring")
    if not all(_is_lon_lat(position) for position in ring):
        raise OverlayDataError(f"Zone layer: zone {zone} has a position outside lon/lat")
    twice_area = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in pairwise(ring))
    if (twice_area < 0) != exterior:
        winding = "clockwise" if exterior else "counter-clockwise"
        raise OverlayDataError(f"Zone layer: zone {zone} needs {winding} rings")


def _is_lon_lat(position: Any) -> bool:
    if not isinstance(position, list) or len(position) != 2:
        return False
    if not all(type(value) in (int, float) and math.isfinite(value) for value in position):
        return False
    lon, lat = position
    return bool(abs(lon) <= _MAX_LON and abs(lat) <= _MAX_LAT)
