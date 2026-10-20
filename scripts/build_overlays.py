"""Build the packaged regional climate-reference overlays (offline, reproducible).

Run from the repository root (the geometry tools live in the optional ``overlays`` group and
are never imported by the app)::

    uv run --group overlays python -I scripts/build_overlays.py --download-dir DIR

Downloads are cached in ``--download-dir`` (each with a ``.retrieved`` date stamp), so a
rerun reuses them and reproduces the same outputs; ``--refresh`` downloads again. Outputs go
to ``--out-dir`` (default ``src/crop_explorer/data/overlays``):

``phzm_2023_simplified.geojson``
    2023 USDA Plant Hardiness Zone half-zone polygons from the PRISM Climate Group (Oregon
    State University) shapefiles for the contiguous U.S., Alaska and Hawaii, merged into one
    feature per half-zone and simplified (see :data:`REGIONS`): polygon parts and holes
    smaller than a minimum area are dropped, the shared edges are simplified together with
    GEOS coverage simplification (Visvalingam-Whyatt, so neighbouring zones keep matching
    edges), coordinates are snapped to a grid, and rings are wound clockwise (the d3-geo
    convention Plotly draws). Puerto Rico is left out: Plotly's ``albers usa`` projection
    does not draw it (checked by rendering), and it is not a region the viewer offers.
``state_zone_ranges.csv``
    For every U.S. state and DC, the half-zones covering at least
    ``overlays.MIN_STATE_ZONE_SHARE`` of its area, from the *unsimplified* zone polygons
    intersected with the U.S. Census Bureau 2023 cartographic boundary file (1:500,000) on a
    spherical equal-area projection. Alaska's and Hawaii's shares come from their own zone
    files, which each cover exactly that state.
``noaa_state_normals_1991_2020.csv``
    Mean of the 30 annual 1991-2020 values of three NOAA NCEI Climate at a Glance statewide
    (nClimDiv) series per state; each response's title is checked against the state name.
``NOTICE.md`` and ``sources.yaml``
    Sources, exact URLs, retrieval dates, checksums, terms (verbatim), the required
    disclaimer and every alteration, human- and machine-readable.

With the default ``--out-dir`` (or an explicit ``--served-dir``) the zone layer is also copied,
byte for byte, to ``static/overlays/phzm_2023_simplified.json``: the viewer's browsers fetch it
from there by URL (:data:`crop_explorer.overlays.SERVED_ZONES_NAME`).

The script fails loud on any unexpected input and validates its outputs with the app's own
loader (:func:`crop_explorer.overlays.load_overlays`) before finishing.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import shutil
import statistics
import sys
import urllib.request
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

import numpy as np
import numpy.typing as npt
import shapefile  # type: ignore[import-untyped]
import shapely  # type: ignore[import-untyped]
import yaml
from loguru import logger
from shapely.geometry import mapping, shape  # type: ignore[import-untyped]

from crop_explorer import overlays
from crop_explorer.regions import US_STATES

DEFAULT_OUT_DIR: Final = overlays.OVERLAY_DIR
DEFAULT_SERVED_DIR: Final = Path(__file__).resolve().parents[1] / "static" / "overlays"
"""Where the viewer serves the zone layer to browsers (only written for the default
``--out-dir``, so a scratch build never replaces the served copy)."""
DEFAULT_DOWNLOAD_DIR: Final = Path.home() / ".cache" / "crop_explorer" / "overlay-downloads"

PHZM_PAGE: Final = "https://prism.oregonstate.edu/phzm/"
PHZM_ZIP: Final = "https://prism.oregonstate.edu/phzm/data/2023/{stem}.zip"
USDA_PHZM: Final = "https://planthardiness.ars.usda.gov/"
CENSUS_STEM: Final = "cb_2023_us_state_500k"
CENSUS_ZIP: Final = f"https://www2.census.gov/geo/tiger/GENZ2023/shp/{CENSUS_STEM}.zip"
CENSUS_PAGE: Final = (
    "https://www.census.gov/geographies/mapping-files/time-series/geo/cartographic-boundary.html"
)
CAG_PAGE: Final = "https://www.ncei.noaa.gov/access/monitoring/climate-at-a-glance/"
CAG_URL: Final = (
    "https://www.ncei.noaa.gov/access/monitoring/climate-at-a-glance/statewide/time-series/"
    "{code}/{param}/{timescale}/{month}/{first}-{last}/data.json"
)
NAD83_PRJ: Final = 'GEOGCS["GCS_North_American_1983"'
USER_AGENT: Final = "crop-explorer-overlay-build/1.0 (offline data build)"
HTTP_TIMEOUT_S: Final = 120
HTTP_OK: Final = 200

EARTH_RADIUS_KM: Final = 6371.0088
"""Mean Earth radius for the spherical Lambert cylindrical equal-area projection (areas)."""
MIN_STATE_COVERAGE: Final = 0.95
"""Fail if the zone polygons cover less than this share of a contiguous state's area."""
SANITY_STATES: Final = ("US-CT", "US-FL", "US-MN", "US-ME", "US-TX", "US-AK", "US-HI")

PHZM_TERMS: Final = (
    "Under the terms of a Specific Cooperative Agreement between the US Department of "
    "Agriculture and Oregon State University (OSU), OSU retains ownership of the GIS datasets "
    "underlying the official 2023 and 2012 USDA Plant Hardiness Zone Maps. These datasets may "
    "be freely reproduced and redistributed, subject to the following conditions: (1) Both "
    "the USDA-ARS logo and OSU logo must be prominently displayed on any maps derived from "
    "the GIS datasets. (2) The data may not be altered in any way unless (a) there is an "
    "explicit and prominently displayed disclaimer that the map is not the official USDA "
    "Plant Hardiness Zone Map, and (b) the USDA-ARS and OSU logos are eliminated."
)
"""Verbatim from the "Terms of Use" section of :data:`PHZM_PAGE`."""
PHZM_CITATION: Final = (
    "PRISM Climate Group, Oregon State University. 2023 USDA Plant Hardiness Zone Map GIS "
    "data (1991-2020 mean annual extreme minimum temperature, half-zone polygon shapefiles). "
    "https://prism.oregonstate.edu/phzm/. Official map: USDA Agricultural Research Service, "
    "2023 USDA Plant Hardiness Zone Map, https://planthardiness.ars.usda.gov/."
)
PHZM_REFERENCE: Final = (
    "Daly, C., M.P. Widrlechner, M.D. Halbleib, J.I. Smith, and W.P. Gibson. 2012. "
    "Development of a new USDA plant hardiness zone map for the United States. Journal of "
    "Applied Meteorology and Climatology 51(2): 242-264. doi:10.1175/2010JAMC2536.1."
)
"""The method reference listed in the shapefiles' own metadata."""
CENSUS_CITATION: Final = (
    "U.S. Census Bureau, 2023 Cartographic Boundary File, States, 1:500,000 "
    f"({CENSUS_STEM}). {CENSUS_ZIP}"
)
CAG_CITATION: Final = (
    "NOAA National Centers for Environmental Information, Climate at a Glance: Statewide "
    "Time Series (nClimDiv). https://www.ncei.noaa.gov/access/monitoring/climate-at-a-glance/"
)
US_GOV_TERMS: Final = (
    "Work of the U.S. federal government: not subject to copyright in the United States "
    "(17 U.S.C. 105). Credited here as a courtesy."
)


@dataclass(frozen=True)
class RegionSpec:
    """How one PRISM zone shapefile is generalized for the map.

    Attributes
    ----------
    key : str
        PRISM file key (``us``, ``ak``, ``hi``): the file is ``phzm_{key}_zones_shp_2023``.
    label : str
        Human name of the area the file covers.
    state_code : str or None
        The one state the file covers exactly (its shares need no state boundary), or
        ``None`` for the contiguous U.S. (shares come from Census state boundaries).
    min_area_km2 : float
        Polygon parts smaller than this are merged into the neighbouring half-zone they share
        the longest boundary with (holes this small are filled the same way); isolated
        clusters of them smaller than this (islets) are dropped.
    tolerance_deg : float
        GEOS coverage-simplification tolerance (degrees): roughly the square root of the
        area of the triangles removed (Visvalingam-Whyatt).
    """

    key: str
    label: str
    state_code: str | None
    min_area_km2: float
    tolerance_deg: float

    @property
    def stem(self) -> str:
        """Return the PRISM file stem, e.g. ``phzm_us_zones_shp_2023``."""
        return f"phzm_{self.key}_zones_shp_2023"


REGIONS: Final = (
    RegionSpec("us", "Contiguous U.S.", None, 75.0, 0.07),
    RegionSpec("ak", "Alaska", "US-AK", 400.0, 0.15),
    RegionSpec("hi", "Hawaii", "US-HI", 10.0, 0.01),
)
"""Generalization per region. Plotly's ``albers usa`` draws the contiguous U.S. and Hawaii
at one scale (at most ~0.06 degrees per pixel on a 1000 px wide map, ~0.16 on a phone) and
Alaska at about 0.35 of it, so Alaska gets a coarser tolerance and minimum area for the same
on-screen fidelity; Hawaii's small islands keep a finer one."""
COORD_DECIMALS: Final = 3
"""Output coordinates are rounded to this many decimals (0.001 degrees, about 110 m)."""
GRID_DEG: Final = 10.0**-COORD_DECIMALS
CLIP_SLACK_DEG: Final = 1e-9
"""Bounding-box slack when clipping a neighbour's boundary around a gap."""


@dataclass(frozen=True)
class ElementSpec:
    """One NOAA Climate at a Glance statewide series and the element it becomes."""

    element: str
    param: str
    timescale: int
    month: int
    title_suffix: str
    units: str


ELEMENTS: Final = (
    ElementSpec("july_avg_high_f", "tmax", 1, 7, "July Maximum Temperature", "Degrees Fahrenheit"),
    ElementSpec(
        "jan_avg_low_f", "tmin", 1, 1, "January Minimum Temperature", "Degrees Fahrenheit"
    ),
    ElementSpec("annual_precip_in", "pcp", 12, 12, "January-December Precipitation", "Inches"),
)
"""Same order as :data:`crop_explorer.overlays.NORMAL_ELEMENTS` (checked at start-up)."""
MISSING_SENTINEL: Final = -99.0
"""NCEI marks missing values with -99 or lower; any such value fails the build."""
NCEI_NONCONTIGUOUS: Final = {"US-AK": 50, "US-HI": 51}
"""NCEI statewide codes outside 1-48 (the contiguous states in alphabetical order)."""
NO_NCEI_SERIES: Final = ("US-DC",)


@dataclass(frozen=True)
class Fetched:
    """One downloaded (or cached) file and its provenance."""

    url: str
    path: Path
    sha256: str
    n_bytes: int
    retrieved: str


@dataclass(frozen=True)
class SourceZone:
    """One half-zone record of a PRISM shapefile, geometry made valid."""

    zone: str
    temp_range_f: str
    geometry: Any


@dataclass(frozen=True)
class RegionStats:
    """What generalizing one region did, for NOTICE.md and sources.yaml."""

    label: str
    source_km2: float
    simplified_km2: float
    merged_gaps: int
    dropped_islets_km2: float
    overlap_km2: float
    edge_matched: bool


@dataclass(frozen=True)
class Elimination:
    """One region's half-zones after small parts were merged into their neighbours."""

    geometries: list[Any]
    merged_gaps: int
    dropped_islets_km2: float


@dataclass(frozen=True)
class BuildResult:
    """What the build wrote, for NOTICE.md, sources.yaml and the summary."""

    downloads: dict[str, Fetched]
    noaa: list[Fetched]
    zone_bytes: int
    zone_vertices: int
    zones_dropped: list[str]
    regions: list[RegionStats]


# --------------------------------------------------------------------------- downloads


def fetch(url: str, dest: Path, *, refresh: bool) -> Fetched:
    """Download ``url`` to ``dest`` unless a stamped copy exists; return its provenance."""
    stamp = dest.with_name(dest.name + ".retrieved")
    if refresh or not dest.exists() or not stamp.exists():
        logger.info("Downloading {}", url)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
            if response.status != HTTP_OK:
                raise RuntimeError(f"HTTP {response.status} for {url}")
            body = response.read()
        dest.write_bytes(body)
        stamp.write_text(datetime.now(UTC).date().isoformat() + "\n", encoding="utf-8")
    data = dest.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    return Fetched(url, dest, digest, len(data), stamp.read_text(encoding="utf-8").strip())


def read_shapefile(zip_path: Path, stem: str) -> Any:
    """Open ``stem`` inside ``zip_path`` as a pyshp reader; require NAD83 geographic coords."""
    with zipfile.ZipFile(zip_path) as archive:
        prj = archive.read(f"{stem}.prj").decode("ascii")
        parts = {ext: io.BytesIO(archive.read(f"{stem}.{ext}")) for ext in ("shp", "shx", "dbf")}
    if not prj.startswith(NAD83_PRJ):
        raise ValueError(f"{stem}: expected NAD83 geographic coordinates, got {prj[:60]!r}")
    return shapefile.Reader(**parts)


# --------------------------------------------------------------------------- geometry


def polygonal(geometry: Any) -> Any:
    """Return ``geometry`` made valid, as a MultiPolygon of its polygonal parts."""
    parts = shapely.get_parts(shapely.make_valid(geometry))
    polygons = [p for part in parts for p in shapely.get_parts(part) if p.geom_type == "Polygon"]
    return shapely.multipolygons(polygons) if polygons else shapely.MultiPolygon()


def equal_area(geometry: Any) -> Any:
    """Project lon/lat degrees to a spherical Lambert cylindrical equal-area plane (km)."""

    def project(coords: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        lon, lat = np.radians(coords[:, 0]), np.radians(coords[:, 1])
        return np.column_stack([EARTH_RADIUS_KM * lon, EARTH_RADIUS_KM * np.sin(lat)])

    return shapely.transform(geometry, project)


def area_km2(geometry: Any) -> float:
    """Return the area of a lon/lat geometry in square kilometres."""
    return float(shapely.area(equal_area(geometry)))


def read_zones(zip_path: Path, stem: str) -> list[SourceZone]:
    """Read every half-zone record, checking its zone code and temperature range."""
    zones = []
    for record in read_shapefile(zip_path, stem).iterShapeRecords():
        zone, temp_range = record.record["zone"], record.record["trange"]
        if temp_range != overlays.zone_temp_range_f(zone):
            raise ValueError(f"{stem}: zone {zone} has range {temp_range!r}")
        geometry = polygonal(shape(record.shape.__geo_interface__))
        zones.append(SourceZone(zone, temp_range, geometry))
    logger.info("{}: {} half-zones", stem, len(zones))
    return zones


def explode(zones: Sequence[SourceZone]) -> tuple[npt.NDArray[np.str_], npt.NDArray[Any]]:
    """Return every polygon part of ``zones`` and its half-zone label."""
    labels = np.array([z.zone for z in zones for _ in shapely.get_parts(z.geometry)])
    parts = np.concatenate([shapely.get_parts(z.geometry) for z in zones])
    return labels, parts


def fill_small_holes(part: Any, min_area_km2: float) -> Any:
    """Return ``part`` without its holes smaller than ``min_area_km2``."""
    holes = [r for r in part.interiors if area_km2(shapely.Polygon(r)) >= min_area_km2]
    return shapely.Polygon(part.exterior, holes)


def outside_filled(
    small: npt.NDArray[Any], labels: npt.NDArray[np.str_], tree: Any
) -> tuple[npt.NDArray[Any], npt.NDArray[np.str_]]:
    """Return the small parts (and labels) not inside a filled hole of a large part."""
    inside = np.zeros(len(small), dtype=bool)
    inside[tree.query(small, predicate="within")[0]] = True
    return small[~inside], labels[~inside]


def shared_boundaries(
    gaps: npt.NDArray[Any], large: npt.NDArray[Any], tree: Any
) -> list[tuple[int, int, float]]:
    """Return (gap, large part, length of their shared boundary) for every touching pair."""
    gap_index, part_index = tree.query(gaps, predicate="intersects")
    boundaries = shapely.boundary(large)
    pairs = []
    for gap, part in zip(gap_index.tolist(), part_index.tolist(), strict=True):
        x0, y0, x1, y1 = shapely.bounds(gaps[gap]).tolist()
        slack = CLIP_SLACK_DEG
        near = shapely.clip_by_rect(
            boundaries[part], x0 - slack, y0 - slack, x1 + slack, y1 + slack
        )
        shared = shapely.intersection(near, shapely.boundary(gaps[gap]))
        pairs.append((gap, part, float(shapely.length(shared))))
    return pairs


def longest_boundary_zone(
    pairs: Iterable[tuple[int, int, float]], labels: npt.NDArray[np.str_]
) -> dict[int, str]:
    """Give each gap the half-zone it shares the longest boundary with (ties: the colder)."""
    totals: dict[tuple[int, str], float] = {}
    for gap, part, length in pairs:
        key = (gap, str(labels[part]))
        totals[key] = totals.get(key, 0.0) + length
    best: dict[int, tuple[float, str]] = {}
    for (gap, zone), length in sorted(totals.items(), key=_gap_zone_order):
        if length > best.get(gap, (0.0, ""))[0]:
            best[gap] = (length, zone)
    return {gap: zone for gap, (_, zone) in best.items()}


def _gap_zone_order(item: tuple[tuple[int, str], float]) -> tuple[int, int]:
    (gap, zone), _ = item
    return gap, overlays.zone_rank(zone)


def island_zones(
    gaps: npt.NDArray[Any], small: npt.NDArray[Any], labels: npt.NDArray[np.str_], min_km2: float
) -> dict[int, str]:
    """Keep isolated gaps of at least ``min_km2`` as the half-zone of their largest part."""
    tree = shapely.STRtree(small)
    zones = {}
    for gap, geometry in enumerate(gaps):
        if area_km2(geometry) >= min_km2:
            members = tree.query(geometry, predicate="covers").tolist()
            largest = max(members, key=lambda index: (area_km2(small[index]), -index))
            zones[gap] = str(labels[largest])
    return zones


def eliminate_small(zones: Sequence[SourceZone], min_area_km2: float) -> Elimination:
    """Merge parts and holes below ``min_area_km2`` into their neighbouring half-zone."""
    labels, parts = explode(zones)
    large = shapely.area(equal_area(parts)) >= min_area_km2
    filled = np.array([fill_small_holes(p, min_area_km2) for p in parts[large]], dtype=object)
    tree = shapely.STRtree(filled)
    small, small_labels = outside_filled(parts[~large], labels[~large], tree)
    gaps = shapely.get_parts(shapely.union_all(small))
    assigned = longest_boundary_zone(shared_boundaries(gaps, filled, tree), labels[large])
    isolated = np.array([g for g in range(len(gaps)) if g not in assigned], dtype=int)
    islands = island_zones(gaps[isolated], small, small_labels, min_area_km2)
    assigned |= {int(isolated[index]): zone for index, zone in islands.items()}
    members = zone_members(zones, list(zip(filled, labels[large], strict=True)))
    for gap, zone in assigned.items():
        members[zone].append(gaps[gap])
    dropped = sum(area_km2(gaps[g]) for g in range(len(gaps)) if g not in assigned)
    merged = [polygonal(shapely.union_all(members[z.zone])) for z in zones]
    return Elimination(merged, len(assigned), dropped)


def zone_members(
    zones: Sequence[SourceZone], parts: Iterable[tuple[Any, str]]
) -> dict[str, list[Any]]:
    """Group labelled parts by half-zone (every zone of ``zones`` gets a list)."""
    members: dict[str, list[Any]] = {z.zone: [] for z in zones}
    for part, zone in parts:
        members[str(zone)].append(part)
    return members


def simplify_region(
    zones: Sequence[SourceZone], spec: RegionSpec
) -> tuple[list[Any], RegionStats]:
    """Generalize one region's half-zones together so shared edges stay matched."""
    eliminated = eliminate_small(zones, spec.min_area_km2)
    geometries = np.array(eliminated.geometries, dtype=object)
    simplified = shapely.coverage_simplify(geometries, spec.tolerance_deg)
    result = [shapely.orient_polygons(g, exterior_cw=True) for g in simplified]
    if not all(g.is_valid for g in result):
        raise ValueError(f"{spec.label}: simplification produced an invalid polygon")
    stats = region_stats(spec, zones, result, eliminated)
    logger.info("{}", stats)
    return result, stats


def region_stats(
    spec: RegionSpec, zones: Sequence[SourceZone], result: list[Any], eliminated: Elimination
) -> RegionStats:
    """Measure what generalizing one region changed."""
    shown = [g for g in result if not g.is_empty]
    simplified_km2 = sum(area_km2(g) for g in shown)
    return RegionStats(
        label=spec.label,
        source_km2=sum(area_km2(z.geometry) for z in zones),
        simplified_km2=simplified_km2,
        merged_gaps=eliminated.merged_gaps,
        dropped_islets_km2=eliminated.dropped_islets_km2,
        overlap_km2=max(0.0, simplified_km2 - area_km2(shapely.union_all(shown))),
        edge_matched=bool(shapely.coverage_is_valid(np.array(shown, dtype=object))),
    )


def ring_positions(ring: Sequence[Sequence[float]], *, exterior: bool) -> list[list[float]] | None:
    """Round a ring, drop repeated positions and wind it; ``None`` if it collapses."""
    rounded = [[round(x, COORD_DECIMALS), round(y, COORD_DECIMALS)] for x, y in ring]
    kept = [p for i, p in enumerate(rounded) if i == 0 or p != rounded[i - 1]]
    twice_area = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in pairwise(kept))
    if len(kept) < 4 or twice_area == 0:
        return None
    return kept if (twice_area < 0) == exterior else kept[::-1]


def polygon_positions(polygon: Any) -> list[list[list[float]]] | None:
    """Return a polygon's rounded rings (exterior clockwise), or ``None`` if it collapses."""
    rings = mapping(polygon)["coordinates"]
    exterior = ring_positions(rings[0], exterior=True)
    if exterior is None:
        return None
    holes = [ring_positions(ring, exterior=False) for ring in rings[1:]]
    return [exterior, *(hole for hole in holes if hole is not None)]


def is_valid_positions(positions: list[list[list[float]]]) -> bool:
    """Return whether rounded rings still form a valid polygon."""
    return bool(shapely.Polygon(positions[0], positions[1:]).is_valid)


def polygon_coordinates(geometry: Any) -> list[Any]:
    """Return every polygon's GeoJSON coordinates, rounded and valid.

    Rounding each vertex keeps shared edges identical; a polygon that rounding would make
    invalid (a self-touching sliver) is snap-rounded by GEOS instead.
    """
    coordinates = []
    for polygon in shapely.get_parts(geometry):
        positions = polygon_positions(polygon)
        if positions is not None and is_valid_positions(positions):
            coordinates.append(positions)
            continue
        snapped = polygonal(shapely.set_precision(polygon, GRID_DEG))
        for part in shapely.get_parts(snapped):
            fixed = polygon_positions(part)
            if fixed is None or not is_valid_positions(fixed):
                raise ValueError("Rounding produced an invalid polygon that GEOS cannot repair")
            coordinates.append(fixed)
    return coordinates


def zone_feature(zone: str, polygons: list[Any]) -> dict[str, Any]:
    """Return one half-zone Feature (properties from the zone; Polygon when one part)."""
    geometry = (
        {"type": "Polygon", "coordinates": polygons[0]}
        if len(polygons) == 1
        else {"type": "MultiPolygon", "coordinates": polygons}
    )
    properties = {
        "zone": zone,
        "zone_number": int(zone[:-1]),
        "temp_range_f": overlays.zone_temp_range_f(zone),
    }
    return {"type": "Feature", "properties": properties, "geometry": geometry}


@dataclass(frozen=True)
class ZoneLayerBuild:
    """The zone FeatureCollection plus what building it did."""

    collection: dict[str, Any]
    n_vertices: int
    regions: list[RegionStats]


def zone_collection(by_region: dict[str, list[SourceZone]]) -> ZoneLayerBuild:
    """Generalize every region and merge into one feature per half-zone, cold to warm."""
    polygons: dict[str, list[Any]] = {}
    stats = []
    for spec in REGIONS:
        zones = by_region[spec.key]
        simplified, region = simplify_region(zones, spec)
        stats.append(region)
        for source, geometry in zip(zones, simplified, strict=True):
            polygons.setdefault(source.zone, []).extend(polygon_coordinates(geometry))
    ordered = sorted((z for z in polygons if polygons[z]), key=overlays.zone_rank)
    features = [zone_feature(zone, polygons[zone]) for zone in ordered]
    n_vertices = sum(len(ring) for zone in ordered for rings in polygons[zone] for ring in rings)
    return ZoneLayerBuild(_feature_collection(features), n_vertices, stats)


def _feature_collection(features: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "name": Path(overlays.ZONES_FILE).stem,
        "disclaimer": overlays.ZONE_DISCLAIMER,
        "source": overlays.ZONE_SOURCE,
        "features": features,
    }


# --------------------------------------------------------------------------- state shares


def census_states(zip_path: Path) -> dict[str, Any]:
    """Return every U.S. state and DC boundary from the Census file, keyed ``US-XX``."""
    states = {}
    for record in read_shapefile(zip_path, CENSUS_STEM).iterShapeRecords():
        code = f"US-{record.record['STUSPS']}"
        if code in US_STATES:
            states[code] = polygonal(shape(record.shape.__geo_interface__))
    missing = sorted(set(US_STATES) - set(states))
    if missing:
        raise ValueError(f"{CENSUS_STEM}: missing states {missing}")
    return states


@dataclass(frozen=True)
class ZoneIndex:
    """Every polygon part of one region's half-zones, equal-area projected and indexed."""

    names: npt.NDArray[np.str_]
    parts: npt.NDArray[np.object_]
    tree: Any

    @classmethod
    def build(cls, zones: Sequence[SourceZone]) -> ZoneIndex:
        """Explode, project and index ``zones``."""
        names, parts = explode(zones)
        projected = equal_area(parts)
        return cls(names, projected, shapely.STRtree(projected))

    def areas(self, boundary: Any | None) -> dict[str, float]:
        """Return km2 of each half-zone inside ``boundary`` (everywhere when ``None``)."""
        names, parts = self.names, self.parts
        if boundary is not None:
            region = equal_area(boundary)
            shapely.prepare(region)
            hits = self.tree.query(region, predicate="intersects")
            names, parts = names[hits], shapely.intersection(parts[hits], region)
        areas: dict[str, float] = {}
        for name, area in zip(names, shapely.area(parts), strict=True):
            areas[str(name)] = areas.get(str(name), 0.0) + float(area)
        return areas


def share_rows(code: str, areas: dict[str, float]) -> list[list[str]]:
    """Return the CSV rows of the half-zones covering at least the minimum share."""
    total = sum(areas.values())
    kept = sorted(
        (z for z, a in areas.items() if a / total >= overlays.MIN_STATE_ZONE_SHARE),
        key=overlays.zone_rank,
    )
    dropped = {z: f"{a / total:.5f}" for z, a in areas.items() if z not in kept and a > 0}
    logger.info("{}: {} (below minimum share: {})", code, ", ".join(kept), dropped or "none")
    return [
        [code, z, f"{areas[z] / total:.5f}", _flag(z == kept[0]), _flag(z == kept[-1])]
        for z in kept
    ]


def _flag(value: bool) -> str:
    return "true" if value else "false"


def state_rows(by_region: dict[str, list[SourceZone]], census_zip: Path) -> list[list[str]]:
    """Return every state's share rows, sorted by region code."""
    states = census_states(census_zip)
    indexes = {key: ZoneIndex.build(zones) for key, zones in by_region.items()}
    single = {spec.state_code: spec.key for spec in REGIONS if spec.state_code is not None}
    rows = []
    for code in sorted(states):
        if code in single:
            areas = indexes[single[code]].areas(None)
        else:
            areas = indexes["us"].areas(states[code])
            _check_coverage(code, sum(areas.values()) / area_km2(states[code]))
        rows.extend(share_rows(code, areas))
    return rows


def _check_coverage(code: str, coverage: float) -> None:
    if coverage < MIN_STATE_COVERAGE:
        raise ValueError(f"{code}: zone polygons cover only {coverage:.1%} of the state")


# --------------------------------------------------------------------------- NOAA averages


def ncei_codes() -> dict[str, int]:
    """Return NCEI statewide codes: contiguous states 1-48 alphabetically, AK 50, HI 51."""
    excluded = {*NCEI_NONCONTIGUOUS, *NO_NCEI_SERIES}
    contiguous = sorted((name, code) for code, name in US_STATES.items() if code not in excluded)
    codes = {code: index for index, (_, code) in enumerate(contiguous, start=1)}
    return dict(sorted({**codes, **NCEI_NONCONTIGUOUS}.items()))


def cag_url(code: int, spec: ElementSpec) -> str:
    """Return the Climate at a Glance statewide time-series URL for one state and element."""
    return CAG_URL.format(
        code=code,
        param=spec.param,
        timescale=spec.timescale,
        month=spec.month,
        first=overlays.NORMALS_FIRST_YEAR,
        last=overlays.NORMALS_LAST_YEAR,
    )


def cag_url_template() -> str:
    """Return :data:`CAG_URL` with the reference period filled in."""
    first, last = overlays.NORMALS_FIRST_YEAR, overlays.NORMALS_LAST_YEAR
    return CAG_URL.replace("{first}-{last}", f"{first}-{last}")


def thirty_year_mean(payload: dict[str, Any], state: str, spec: ElementSpec) -> float:
    """Check one response's title, units and years; return the mean of its 30 values."""
    description = payload["description"]
    if description["title"] != f"{state} {spec.title_suffix}":
        raise ValueError(f"Expected '{state} {spec.title_suffix}', got {description['title']!r}")
    if description["units"] != spec.units:
        raise ValueError(f"{state} {spec.element}: units {description['units']!r}")
    first, last = overlays.NORMALS_FIRST_YEAR, overlays.NORMALS_LAST_YEAR
    expected = [f"{year}{spec.month:02d}" for year in range(first, last + 1)]
    if sorted(payload["data"]) != expected:
        raise ValueError(f"{state} {spec.element}: years differ from {first}-{last}")
    values = [float(payload["data"][key]["value"]) for key in expected]
    if len(values) != overlays.NORMALS_N_YEARS or min(values) <= MISSING_SENTINEL:
        raise ValueError(f"{state} {spec.element}: missing annual values")
    return statistics.fmean(values)


def normal_rows(download_dir: Path, *, refresh: bool) -> tuple[list[list[str]], list[Fetched]]:
    """Fetch every state's three series; return tidy CSV rows and the fetched files."""
    rows, fetched = [], []
    for region_code, ncei in ncei_codes().items():
        for spec in ELEMENTS:
            dest = download_dir / f"cag_statewide_{ncei:02d}_{spec.element}.json"
            item = fetch(cag_url(ncei, spec), dest, refresh=refresh)
            payload = json.loads(item.path.read_text(encoding="utf-8"))
            mean = thirty_year_mean(payload, US_STATES[region_code], spec)
            rows.append([region_code, spec.element, f"{mean:.2f}", str(overlays.NORMALS_N_YEARS)])
            fetched.append(item)
    logger.info("NOAA: {} rows for {} states", len(rows), len(rows) // len(ELEMENTS))
    return rows, fetched


# --------------------------------------------------------------------------- writers


def write_csv(path: Path, header: Sequence[str], rows: Iterable[Sequence[str]]) -> None:
    """Write a CSV with Unix line endings."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def write_geojson(path: Path, collection: dict[str, Any]) -> int:
    """Write compact UTF-8 GeoJSON; return its size in bytes."""
    text = json.dumps(collection, ensure_ascii=False, separators=(",", ":")) + "\n"
    path.write_text(text, encoding="utf-8")
    return path.stat().st_size


def _fetched_yaml(item: Fetched) -> dict[str, Any]:
    return {
        "url": item.url,
        "retrieved": item.retrieved,
        "sha256": item.sha256,
        "bytes": item.n_bytes,
    }


def _regions_yaml(stats: Sequence[RegionStats]) -> list[dict[str, Any]]:
    return [
        {
            "region": spec.label,
            "file": f"{spec.stem}.zip",
            "min_area_km2": spec.min_area_km2,
            "coverage_simplify_tolerance_deg": spec.tolerance_deg,
            "coordinate_decimals": COORD_DECIMALS,
            "source_km2": round(region.source_km2),
            "simplified_km2": round(region.simplified_km2),
            "gaps_merged_into_neighbours": region.merged_gaps,
            "islets_dropped_km2": round(region.dropped_islets_km2, 1),
            "overlap_km2": round(region.overlap_km2, 1),
            "shared_edges_matched": region.edge_matched,
        }
        for spec, region in zip(REGIONS, stats, strict=True)
    ]


def _dates(items: Sequence[Fetched]) -> str:
    dates = sorted({item.retrieved for item in items})
    return dates[0] if len(dates) == 1 else f"{dates[0]} to {dates[-1]}"


def sources_document(result: BuildResult) -> dict[str, Any]:
    """Return the machine-readable provenance written to ``sources.yaml``."""
    phzm = [result.downloads[spec.key] for spec in REGIONS]
    return {
        "label": overlays.OVERLAY_LABEL,
        "built_by": "scripts/build_overlays.py",
        "sources": [
            _phzm_source(phzm),
            _census_source(result.downloads["census"]),
            _cag_source(result.noaa),
        ],
        "outputs": _outputs_yaml(result),
    }


def _phzm_source(phzm: Sequence[Fetched]) -> dict[str, Any]:
    return {
        "id": "phzm_2023",
        "title": overlays.ZONE_SOURCE,
        "publisher": "PRISM Climate Group, Oregon State University (for USDA-ARS)",
        "page": PHZM_PAGE,
        "official_map": USDA_PHZM,
        "period": overlays.ZONE_PERIOD,
        "coverage": overlays.ZONE_COVERAGE,
        "citation": PHZM_CITATION,
        "method_reference": PHZM_REFERENCE,
        "terms": PHZM_TERMS,
        "terms_url": PHZM_PAGE,
        "required_disclaimer": overlays.ZONE_DISCLAIMER,
        "logos": "none (the data are altered, so condition (2b) requires no logos)",
        "files": [_fetched_yaml(item) for item in phzm],
    }


def _census_source(census: Fetched) -> dict[str, Any]:
    return {
        "id": "census_states_2023",
        "title": "2023 Cartographic Boundary File, States, 1:500,000",
        "publisher": "U.S. Census Bureau",
        "page": CENSUS_PAGE,
        "citation": CENSUS_CITATION,
        "terms": US_GOV_TERMS,
        "used_for": "state areas for state_zone_ranges.csv only (not shipped)",
        "files": [_fetched_yaml(census)],
    }


def _cag_source(noaa: Sequence[Fetched]) -> dict[str, Any]:
    return {
        "id": "noaa_cag_statewide",
        "title": overlays.NORMALS_SOURCE,
        "publisher": "NOAA National Centers for Environmental Information",
        "page": CAG_PAGE,
        "url_template": cag_url_template(),
        "period": overlays.NORMALS_PERIOD,
        "citation": CAG_CITATION,
        "terms": US_GOV_TERMS,
        "retrieved": _dates(noaa),
        "series": [
            {"element": s.element, "param": s.param, "timescale": s.timescale, "month": s.month}
            for s in ELEMENTS
        ],
        "state_codes": {code: ncei for code, ncei in ncei_codes().items()},
        "files": [_fetched_yaml(item) for item in noaa],
    }


def _outputs_yaml(result: BuildResult) -> dict[str, Any]:
    return {
        overlays.ZONES_FILE: {
            "from": ["phzm_2023"],
            "bytes": result.zone_bytes,
            "vertices": result.zone_vertices,
            "crs": "lon/lat degrees (NAD83 source coordinates, treated as WGS84)",
            "winding": "exterior rings clockwise, holes counter-clockwise (d3-geo / Plotly)",
            "generalization": _regions_yaml(result.regions),
            "half_zones_dropped_entirely": result.zones_dropped,
        },
        overlays.STATE_ZONES_FILE: {
            "from": ["phzm_2023", "census_states_2023"],
            "min_area_share": overlays.MIN_STATE_ZONE_SHARE,
            "area_projection": "spherical Lambert cylindrical equal-area, R = 6371.0088 km",
            "geometry": "unsimplified (validity-repaired) zone polygons",
        },
        overlays.NORMALS_FILE: {
            "from": ["noaa_cag_statewide"],
            "n_years": overlays.NORMALS_N_YEARS,
            "not_covered": list(NO_NCEI_SERIES),
        },
    }


def write_sources(path: Path, result: BuildResult) -> None:
    """Write ``sources.yaml``."""
    text = yaml.safe_dump(sources_document(result), sort_keys=False, allow_unicode=True, width=99)
    path.write_text("# Generated by scripts/build_overlays.py. Do not edit.\n" + text, "utf-8")


# --------------------------------------------------------------------------- NOTICE.md


NOTICE_TEMPLATE: Final = """\
# Regional climate references: sources, terms and alterations

Generated by `scripts/build_overlays.py`; do not edit by hand. Machine-readable twin:
`sources.yaml`.

These files back the viewer's **optional** map overlays. Each is a
**{label}**: background for the region a viewer chooses, never evidence that a variety
will succeed anywhere, and never a reading for any commenter. Hardiness zones are never
assigned to reports by geography, and no climate value is attached to a comment
(SPEC §6-§7). Reports stay state-level counts; no coordinates are derived for them.

## 1. 2023 USDA Plant Hardiness Zone Map GIS data

**Required disclaimer** (shown wherever the zone layer, or a range derived from it, is
displayed):

> {disclaimer}

- Source: PRISM Climate Group, Oregon State University, half-zone polygon shapefiles, for
  the official 2023 USDA Plant Hardiness Zone Map ({usda}).
- Page and terms: {phzm_page}
- Files (retrieved {phzm_dates}):
{phzm_files}
- Period: {zone_period} (zones are 10 °F bands, half-zones 5 °F).
- Coverage: {zone_coverage}.
- Citation: {phzm_citation}
- Method reference (from the shapefiles' metadata): {phzm_reference}

**Terms of use, verbatim** (from the "Terms of Use" section of {phzm_page}):

> {phzm_terms}

**How these terms are met.** The geometry is altered (simplified), so under condition (2)
the derived layer carries the explicit disclaimer above wherever it is shown and **no
USDA-ARS or OSU logo is used anywhere**. This file and `sources.yaml` record the
alterations.

**What was altered** (`{zones_file}`, {zone_bytes:,} bytes, {zone_vertices:,} vertices):

1. Each shapefile's polygons were repaired to valid geometry (GEOS `make_valid`).
2. Raster specks were generalized away: every polygon part smaller than the region's
   minimum area (and every hole that small, including small lakes) was merged into the
   neighbouring half-zone it shares the longest boundary with, so no gaps are left.
   Isolated clusters of such parts (islets with no larger neighbour) at least that large
   keep the half-zone of their largest part; smaller ones were dropped.
3. The half-zones of each region were simplified together with GEOS coverage
   simplification (Visvalingam-Whyatt; neighbouring zones keep identical shared edges;
   the tolerance is roughly the square root of the area of the triangles removed).
4. Coordinates were rounded to {decimals} decimals (about 110 m); the few polygons that
   rounding would make invalid were snap-rounded by GEOS instead.
5. Rings were re-wound clockwise (exterior) / counter-clockwise (holes), the d3-geo
   convention Plotly draws.
6. The contiguous U.S., Alaska and Hawaii were merged into one feature per half-zone with
   properties `zone`, `zone_number` and `temp_range_f` (°F, from the source `trange`).
   The source `Id`, `gridcode` (a per-file index) and `zonetitle` fields were dropped.
7. Source coordinates are NAD83 longitude/latitude, kept as-is and treated as WGS84 (the
   datum shift, about 1-2 m, is far below the simplification tolerance).
8. Puerto Rico was left out: Plotly's `albers usa` projection, which the map uses, does
   not draw it (checked by rendering), and the viewer offers no Puerto Rico region.
   Plotly also clips the westernmost Aleutian Islands out of its Alaska inset.
{dropped_note}
| Region | Min part/hole area | Tolerance | Gaps merged | Islets dropped | Overlap | Area kept |
|---|---|---|---|---|---|---|
{region_table}

Alaska's source polygons do not share exactly matching edges, so its simplified half-zones
overlap slightly (see the table); at map scale this is invisible.

## 2. State zone ranges (`{state_zones_file}`)

Derived from the **unsimplified** (validity-repaired) zone polygons above, so the same
disclaimer applies to any display of these ranges.

- For each U.S. state and DC, the area of each half-zone inside the state was measured on
  a spherical Lambert cylindrical equal-area projection (R = 6371.0088 km). Contiguous
  states use the boundary file below; Alaska and Hawaii use their own zone files, which
  each cover exactly that state.
- A half-zone is listed when it covers at least **{min_share:.1%}** of the state's
  zone-mapped area (`area_share`, a fraction). Smaller slivers are mostly misalignment
  between the 800 m zone raster and the state boundary. `is_coldest` and `is_warmest`
  mark the coldest and warmest listed half-zone.
- State boundaries: {census_citation} (retrieved {census_date}, sha256
  `{census_sha}`). {us_gov_terms} Used for areas only; not shipped.

## 3. NOAA statewide 1991-2020 averages (`{normals_file}`)

- Display label: "{normals_source}".
- Citation: {cag_citation}
- Terms: {us_gov_terms}
- Requests: `{cag_url}` (retrieved {cag_dates}), one per state and series:
{series_list}
- State codes: the contiguous states are 1-48 in alphabetical order, Alaska 50, Hawaii 51;
  every response's title was checked against the state name. DC has no statewide series.
- `value` is the mean of exactly {n_years} annual values ({period}); the build fails on
  any missing year or value.

**What these numbers are.** Statistical averages of NOAA's gridded statewide (nClimDiv)
series, computed by this project. They are not NOAA's official station-based U.S.
Climate Normals product, not observed weather in any particular season, and one number
per state hides large differences within it (elevation, coast, latitude).
"""


def notice_text(result: BuildResult) -> str:
    """Return NOTICE.md."""
    return NOTICE_TEMPLATE.format(**_notice_fields(result))


def _notice_fields(result: BuildResult) -> dict[str, Any]:
    phzm = [result.downloads[spec.key] for spec in REGIONS]
    census = result.downloads["census"]
    return {
        **_notice_constants(),
        "phzm_dates": _dates(phzm),
        "phzm_files": "\n".join(f"  - {i.url} (sha256 `{i.sha256}`)" for i in phzm),
        "zone_bytes": result.zone_bytes,
        "zone_vertices": result.zone_vertices,
        "dropped_note": _dropped_note(result.zones_dropped),
        "region_table": "\n".join(
            _region_row(spec, region) for spec, region in zip(REGIONS, result.regions, strict=True)
        ),
        "census_date": census.retrieved,
        "census_sha": census.sha256,
        "cag_dates": _dates(result.noaa),
    }


def _notice_constants() -> dict[str, Any]:
    return {
        "label": overlays.OVERLAY_LABEL,
        "disclaimer": overlays.ZONE_DISCLAIMER,
        "usda": USDA_PHZM,
        "phzm_page": PHZM_PAGE,
        "zone_period": overlays.ZONE_PERIOD,
        "zone_coverage": overlays.ZONE_COVERAGE,
        "phzm_citation": PHZM_CITATION,
        "phzm_reference": PHZM_REFERENCE,
        "phzm_terms": PHZM_TERMS,
        "zones_file": overlays.ZONES_FILE,
        "state_zones_file": overlays.STATE_ZONES_FILE,
        "normals_file": overlays.NORMALS_FILE,
        "min_share": overlays.MIN_STATE_ZONE_SHARE,
        "census_citation": CENSUS_CITATION,
        "us_gov_terms": US_GOV_TERMS,
        "normals_source": overlays.NORMALS_SOURCE,
        "cag_citation": CAG_CITATION,
        "cag_url": cag_url_template(),
        "series_list": "\n".join(_series_line(spec) for spec in ELEMENTS),
        "n_years": overlays.NORMALS_N_YEARS,
        "period": overlays.NORMALS_PERIOD,
        "decimals": COORD_DECIMALS,
    }


def _series_line(spec: ElementSpec) -> str:
    return (
        f'  - `{spec.element}`: "{spec.title_suffix}" ({spec.units}); param `{spec.param}`, '
        f"timescale {spec.timescale}, month {spec.month}"
    )


def _region_row(spec: RegionSpec, region: RegionStats) -> str:
    kept = region.simplified_km2 / region.source_km2
    return (
        f"| {spec.label} | {spec.min_area_km2:g} km² | {spec.tolerance_deg:g}° | "
        f"{region.merged_gaps:,} | {region.dropped_islets_km2:,.0f} km² | "
        f"{region.overlap_km2:,.1f} km² | {kept:.2%} |"
    )


def _dropped_note(dropped: Sequence[str]) -> str:
    if not dropped:
        return ""
    names = ", ".join(dropped)
    return f"\nHalf-zones left with no area after step 2 (every part merged away): {names}.\n"


# --------------------------------------------------------------------------- main


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--download-dir", type=Path, default=DEFAULT_DOWNLOAD_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--served-dir", type=Path, default=None, help="browser copy folder")
    parser.add_argument("--refresh", action="store_true", help="download every file again")
    args = parser.parse_args(argv)
    if args.served_dir is None and args.out_dir.resolve() == DEFAULT_OUT_DIR.resolve():
        args.served_dir = DEFAULT_SERVED_DIR
    return args


def copy_served(out_dir: Path, served_dir: Path | None) -> None:
    """Copy the zone layer to the folder the viewer serves to browsers, then check the copy."""
    if served_dir is None:
        logger.info("No --served-dir: the browser copy of the zone layer was not updated")
        return
    served_dir.mkdir(parents=True, exist_ok=True)
    served = served_dir / overlays.SERVED_ZONES_NAME
    shutil.copyfile(out_dir / overlays.ZONES_FILE, served)
    overlays.check_served_copy(served, out_dir / overlays.ZONES_FILE)
    logger.info("Browser copy: {}", served)


def download_sources(download_dir: Path, *, refresh: bool) -> dict[str, Fetched]:
    """Fetch the PRISM shapefiles and the Census state boundaries."""
    urls = {spec.key: PHZM_ZIP.format(stem=spec.stem) for spec in REGIONS}
    urls["census"] = CENSUS_ZIP
    return {
        key: fetch(url, download_dir / url.rsplit("/", 1)[1], refresh=refresh)
        for key, url in urls.items()
    }


def dropped_zones(by_region: dict[str, list[SourceZone]], collection: dict[str, Any]) -> list[str]:
    """Return source half-zones that the minimum area removed from the map entirely."""
    shipped = {feature["properties"]["zone"] for feature in collection["features"]}
    source = {zone.zone for zones in by_region.values() for zone in zones}
    return sorted(source - shipped, key=overlays.zone_rank)


def build(download_dir: Path, out_dir: Path, *, refresh: bool) -> BuildResult:
    """Download, build and write every overlay file; return provenance for the notices."""
    downloads = download_sources(download_dir, refresh=refresh)
    by_region = {spec.key: read_zones(downloads[spec.key].path, spec.stem) for spec in REGIONS}
    layer = zone_collection(by_region)
    zone_bytes = write_geojson(out_dir / overlays.ZONES_FILE, layer.collection)
    shares = state_rows(by_region, downloads["census"].path)
    write_csv(out_dir / overlays.STATE_ZONES_FILE, overlays.STATE_ZONE_COLUMNS, shares)
    normals, noaa = normal_rows(download_dir, refresh=refresh)
    write_csv(out_dir / overlays.NORMALS_FILE, overlays.NORMALS_COLUMNS, normals)
    dropped = dropped_zones(by_region, layer.collection)
    return BuildResult(downloads, noaa, zone_bytes, layer.n_vertices, dropped, layer.regions)


def report(out_dir: Path) -> None:
    """Validate the outputs with the app's loader and log the sanity-check values."""
    loaded = overlays.load_overlays(out_dir)
    size = (out_dir / overlays.ZONES_FILE).stat().st_size
    if size > overlays.ZONE_GEOJSON_MAX_BYTES:
        raise ValueError(f"{overlays.ZONES_FILE} is {size:,} bytes (budget exceeded)")
    ranges = loaded.state_zones
    for code in SANITY_STATES:
        zones = ranges.loc[ranges["region_code"] == code, "zone"].tolist()
        logger.info("Sanity {}: {}-{} ({})", code, zones[0], zones[-1], ", ".join(zones))
    ct = loaded.normals[loaded.normals["region_code"] == "US-CT"]
    logger.info("Sanity US-CT normals: {}", dict(zip(ct["element"], ct["value"], strict=True)))
    logger.info(
        "{}: {:,} bytes, {} half-zones", overlays.ZONES_FILE, size, len(loaded.zones.table)
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Build the overlays; return the process exit code."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if tuple(spec.element for spec in ELEMENTS) != tuple(overlays.NORMAL_ELEMENTS):
        raise ValueError("ELEMENTS must match crop_explorer.overlays.NORMAL_ELEMENTS")
    args.download_dir.mkdir(parents=True, exist_ok=True)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    result = build(args.download_dir, args.out_dir, refresh=args.refresh)
    write_sources(args.out_dir / overlays.SOURCES_FILE, result)
    notice = notice_text(result)
    (args.out_dir / overlays.NOTICE_FILE).write_text(notice, encoding="utf-8")
    report(args.out_dir)
    copy_served(args.out_dir, args.served_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
