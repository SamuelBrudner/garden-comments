"""Packaged regional climate-reference overlays: shipped files, contract checks, provenance.

The overlay files are built offline by ``scripts/build_overlays.py``; these tests pin the
shipped data (size budget, coverage, sanity values), every fail-loud check of the runtime
loader, and the attribution the 2023 PHZM terms require (disclaimer, verbatim terms).
"""

from __future__ import annotations

import ast
import copy
import json
import re
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml

from crop_explorer import overlays
from crop_explorer.overlays import OverlayDataError
from crop_explorer.regions import US_STATES

ROOT = Path(__file__).resolve().parents[1]
DATA = overlays.OVERLAY_DIR
PHZM_TERMS_VERBATIM = (
    "Under the terms of a Specific Cooperative Agreement between the US Department of "
    "Agriculture and Oregon State University (OSU), OSU retains ownership of the GIS datasets "
    "underlying the official 2023 and 2012 USDA Plant Hardiness Zone Maps. These datasets may "
    "be freely reproduced and redistributed, subject to the following conditions: (1) Both "
    "the USDA-ARS logo and OSU logo must be prominently displayed on any maps derived from "
    "the GIS datasets. (2) The data may not be altered in any way unless (a) there is an "
    "explicit and prominently displayed disclaimer that the map is not the official USDA "
    "Plant Hardiness Zone Map, and (b) the USDA-ARS and OSU logos are eliminated."
)
"""Copied independently from https://prism.oregonstate.edu/phzm/ ("Terms of Use")."""
NOAA_STATES = sorted(set(US_STATES) - {"US-DC"})
GEOMETRY_TOOLS = ("shapely", "shapefile", "geopandas", "pyogrio", "fiona", "pyproj", "osgeo")


# --------------------------------------------------------------------------- helpers


def _square(zone: str, *, clockwise: bool = True) -> dict[str, Any]:
    ring = [[0.0, 0.0], [0.0, 1.0], [1.0, 1.0], [1.0, 0.0], [0.0, 0.0]]
    if not clockwise:
        ring.reverse()
    return {
        "type": "Feature",
        "properties": {
            "zone": zone,
            "zone_number": int(zone[:-1]),
            "temp_range_f": overlays.zone_temp_range_f(zone),
        },
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


def _collection(*zones: str) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "disclaimer": overlays.ZONE_DISCLAIMER,
        "features": [_square(zone) for zone in zones],
    }


def _write_json(tmp_path: Path, data: Any) -> Path:
    path = tmp_path / overlays.ZONES_FILE
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _rewrite(tmp_path: Path, name: str, edit: Callable[[list[list[str]]], None]) -> Path:
    """Copy a shipped CSV to ``tmp_path`` after applying ``edit`` to its rows (header first)."""
    lines = (DATA / name).read_text(encoding="utf-8").splitlines()
    rows = [line.split(",") for line in lines]
    edit(rows)
    path = tmp_path / name
    path.write_text("\n".join(",".join(row) for row in rows) + "\n", encoding="utf-8")
    return path


def _zone_range(table: pd.DataFrame, code: str) -> tuple[str, str]:
    rows = table[table["region_code"] == code]
    coldest = rows.loc[rows["is_coldest"], "zone"].item()
    warmest = rows.loc[rows["is_warmest"], "zone"].item()
    return coldest, warmest


# --------------------------------------------------------------------------- zone helpers


def test_zone_rank_runs_cold_to_warm() -> None:
    assert overlays.zone_rank("1a") == 0
    assert overlays.zone_rank("1b") == 1
    assert overlays.zone_rank("6a") == 10
    assert overlays.zone_rank("13b") == 25
    assert list(overlays.HALF_ZONES) == sorted(overlays.HALF_ZONES, key=overlays.zone_rank)


@pytest.mark.parametrize("zone", ["6", "6c", "14a", "0a", "", " 6a", "6A", "13c"])
def test_zone_rank_rejects_non_half_zones(zone: str) -> None:
    with pytest.raises(OverlayDataError):
        overlays.zone_rank(zone)


@pytest.mark.parametrize(
    ("zone", "expected"),
    [("1a", "-60 to -55"), ("3a", "-40 to -35"), ("6a", "-10 to -5"), ("7a", "0 to 5"),
     ("13b", "65 to 70")],
)  # fmt: skip
def test_zone_temp_range_matches_the_usda_bands(zone: str, expected: str) -> None:
    assert overlays.zone_temp_range_f(zone) == expected


# --------------------------------------------------------------------------- shipped files


def test_shipped_overlays_load_and_validate() -> None:
    loaded = overlays.load_overlays()
    assert len(loaded.zones.table) == len(loaded.zones.geojson["features"])
    assert set(loaded.state_zones["region_code"]) == set(US_STATES)
    assert sorted(set(loaded.normals["region_code"])) == NOAA_STATES


def test_zone_geojson_fits_the_phone_size_budget() -> None:
    size = (DATA / overlays.ZONES_FILE).stat().st_size
    assert size <= overlays.ZONE_GEOJSON_MAX_BYTES, f"{size:,} bytes"


def test_zone_layer_covers_contiguous_us_alaska_and_hawaii_but_not_puerto_rico() -> None:
    zones = overlays.load_zone_layer()
    table = zones.table
    assert list(table.columns) == list(overlays.ZONE_TABLE_COLUMNS)
    assert table["rank"].is_monotonic_increasing
    shipped = set(table["zone"])
    assert {"1a", "2b"} <= shipped  # Alaska only
    assert {"6b", "7a", "9b"} <= shipped  # contiguous U.S.
    assert {"12b", "13a"} <= shipped  # Hawaii
    assert "13b" not in shipped  # only Puerto Rico has 13b, and it is left out
    assert zones.geojson["disclaimer"] == overlays.ZONE_DISCLAIMER


def test_zone_layer_table_twin_matches_the_feature_properties() -> None:
    zones = overlays.load_zone_layer()
    properties = [feature["properties"] for feature in zones.geojson["features"]]
    assert zones.table[["zone", "zone_number", "temp_range_f"]].to_dict("records") == properties
    assert (zones.table["zone_number"] == zones.table["zone"].str[:-1].astype(int)).all()


def test_zone_layer_coordinates_are_in_the_us_and_rounded() -> None:
    features = overlays.load_zone_layer().geojson["features"]
    positions = [
        position
        for feature in features
        for polygon in _polygons(feature["geometry"])
        for ring in polygon
        for position in ring
    ]
    lats = [lat for _, lat in positions]
    assert min(lats) > 18.5  # Hawaii's southern tip
    assert max(lats) < 72  # Alaska's north slope
    assert all(
        len(repr(value).split(".")[-1]) <= 3 for position in positions for value in position
    )


def _polygons(geometry: dict[str, Any]) -> list[Any]:
    coordinates = geometry["coordinates"]
    return [coordinates] if geometry["type"] == "Polygon" else list(coordinates)


def test_state_zone_ranges_match_known_2023_values() -> None:
    table = overlays.load_state_zone_ranges()
    assert _zone_range(table, "US-CT") == ("6a", "7b")
    assert _zone_range(table, "US-FL") == ("8b", "11b")
    assert _zone_range(table, "US-MN") == ("3a", "5a")
    assert _zone_range(table, "US-ME") == ("3b", "6b")
    assert _zone_range(table, "US-AK") == ("1a", "8b")
    assert _zone_range(table, "US-TX") == ("6b", "10a")
    assert _zone_range(table, "US-HI") == ("9a", "13a")


def test_state_zone_shares_cover_almost_all_of_each_state() -> None:
    table = overlays.load_state_zone_ranges()
    totals = table.groupby("region_code")["area_share"].sum()
    assert (totals > 0.97).all() and (totals <= 1.0001).all()
    assert (table["area_share"] >= overlays.MIN_STATE_ZONE_SHARE).all()


def test_state_normals_are_thirty_year_means_for_fifty_states() -> None:
    table = overlays.load_state_normals()
    assert list(table.columns) == list(overlays.NORMALS_COLUMNS)
    assert len(table) == len(NOAA_STATES) * len(overlays.NORMAL_ELEMENTS)
    assert (table["n_years"] == 30).all()
    assert not table["value"].isna().any()
    assert "US-DC" not in set(table["region_code"])


def test_state_normals_sanity_values() -> None:
    table = overlays.load_state_normals().set_index(["region_code", "element"])["value"]
    assert 80 < table["US-CT", "july_avg_high_f"] < 86
    assert 12 < table["US-CT", "jan_avg_low_f"] < 22
    assert 40 < table["US-CT", "annual_precip_in"] < 55
    assert table["US-FL", "jan_avg_low_f"] > table["US-MN", "jan_avg_low_f"] + 30
    assert table["US-NV", "annual_precip_in"] < table["US-LA", "annual_precip_in"] / 3
    assert table["US-HI", "jan_avg_low_f"] > 50


# --------------------------------------------------------------------------- GeoJSON checks


def test_minimal_zone_collection_loads(tmp_path: Path) -> None:
    layer = overlays.load_zone_layer(_write_json(tmp_path, _collection("6a", "6b")))
    assert layer.table["zone"].tolist() == ["6a", "6b"]
    assert layer.table["rank"].tolist() == [10, 11]


def _break_disclaimer(data: dict[str, Any]) -> None:
    data["disclaimer"] = "Simplified USDA zones."


def _drop_disclaimer(data: dict[str, Any]) -> None:
    del data["disclaimer"]


def _not_a_collection(data: dict[str, Any]) -> None:
    data["type"] = "Feature"


def _no_features(data: dict[str, Any]) -> None:
    data["features"] = []


def _wrong_zone_number(data: dict[str, Any]) -> None:
    data["features"][0]["properties"]["zone_number"] = 7


def _bool_zone_number(data: dict[str, Any]) -> None:
    data["features"][0]["properties"]["zone_number"] = True
    data["features"][0]["properties"]["zone"] = "1a"


def _wrong_temp_range(data: dict[str, Any]) -> None:
    data["features"][0]["properties"]["temp_range_f"] = "-5 to 0"


def _extra_property(data: dict[str, Any]) -> None:
    data["features"][0]["properties"]["state"] = "US-CT"


def _bad_zone(data: dict[str, Any]) -> None:
    data["features"][0]["properties"]["zone"] = "6c"


def _counter_clockwise(data: dict[str, Any]) -> None:
    data["features"][0] = _square("6a", clockwise=False)


def _unclosed(data: dict[str, Any]) -> None:
    data["features"][0]["geometry"]["coordinates"][0].pop()


def _off_the_globe(data: dict[str, Any]) -> None:
    data["features"][0]["geometry"]["coordinates"][0][1] = [0.0, 91.0]


def _not_a_number(data: dict[str, Any]) -> None:
    data["features"][0]["geometry"]["coordinates"][0][1] = [0.0, float("nan")]


def _point(data: dict[str, Any]) -> None:
    data["features"][0]["geometry"] = {"type": "Point", "coordinates": [0.0, 0.0]}


def _duplicate(data: dict[str, Any]) -> None:
    data["features"].append(copy.deepcopy(data["features"][0]))


def _warm_first(data: dict[str, Any]) -> None:
    data["features"].reverse()


@pytest.mark.parametrize(
    "corrupt",
    [
        _break_disclaimer,
        _drop_disclaimer,
        _not_a_collection,
        _no_features,
        _wrong_zone_number,
        _bool_zone_number,
        _wrong_temp_range,
        _extra_property,
        _bad_zone,
        _counter_clockwise,
        _unclosed,
        _off_the_globe,
        _not_a_number,
        _point,
        _duplicate,
        _warm_first,
    ],
)
def test_zone_layer_fails_loud(tmp_path: Path, corrupt: Callable[[dict[str, Any]], None]) -> None:
    data = _collection("6a", "6b")
    corrupt(data)
    with pytest.raises(OverlayDataError):
        overlays.load_zone_layer(_write_json(tmp_path, data))


def test_zone_layer_hole_must_be_counter_clockwise(tmp_path: Path) -> None:
    data = _collection("6a")
    outer = [[0.0, 0.0], [0.0, 3.0], [3.0, 3.0], [3.0, 0.0], [0.0, 0.0]]
    hole = [[1.0, 1.0], [2.0, 1.0], [2.0, 2.0], [1.0, 2.0], [1.0, 1.0]]
    data["features"][0]["geometry"]["coordinates"] = [outer, hole]
    assert len(overlays.load_zone_layer(_write_json(tmp_path, data)).table) == 1
    data["features"][0]["geometry"]["coordinates"] = [outer, list(reversed(hole))]
    with pytest.raises(OverlayDataError, match="counter-clockwise"):
        overlays.load_zone_layer(_write_json(tmp_path, data))


def test_missing_or_invalid_files_fail_loud(tmp_path: Path) -> None:
    with pytest.raises(OverlayDataError):
        overlays.load_zone_layer(tmp_path / "absent.geojson")
    bad = tmp_path / "bad.geojson"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(OverlayDataError):
        overlays.load_zone_layer(bad)
    with pytest.raises(OverlayDataError):
        overlays.load_state_zone_ranges(tmp_path / "absent.csv")
    with pytest.raises(OverlayDataError):
        overlays.load_state_normals(tmp_path / "absent.csv")


# --------------------------------------------------------------------------- CSV checks


def _first_data_row(rows: list[list[str]]) -> list[str]:
    return rows[1]


def _set_cell(column: int, value: str) -> Callable[[list[list[str]]], None]:
    def edit(rows: list[list[str]]) -> None:
        _first_data_row(rows)[column] = value

    return edit


def _drop_state(code: str) -> Callable[[list[list[str]]], None]:
    def edit(rows: list[list[str]]) -> None:
        rows[1:] = [row for row in rows[1:] if row[0] != code]

    return edit


def _duplicate_first(rows: list[list[str]]) -> None:
    rows.insert(1, list(rows[1]))


def _swap_first_two(rows: list[list[str]]) -> None:
    rows[1], rows[2] = rows[2], rows[1]


def _rename_column(rows: list[list[str]]) -> None:
    rows[0][0] = "state"


def _header_only(rows: list[list[str]]) -> None:
    del rows[1:]


@pytest.mark.parametrize(
    "edit",
    [
        _set_cell(0, "US-XX"),
        _set_cell(0, "CA-ON"),  # a region, but not a U.S. state
        _set_cell(1, "6c"),
        _set_cell(1, "6"),
        _set_cell(2, "0.0005"),  # below the minimum share
        _set_cell(2, "1.5"),
        _set_cell(2, "nan"),
        _set_cell(2, "many"),
        _set_cell(2, ""),
        _set_cell(3, "yes"),
        _set_cell(3, "false"),  # the coldest row loses its flag
        _set_cell(4, "true"),  # a second warmest row
        _drop_state("US-WY"),
        _duplicate_first,
        _swap_first_two,
        _rename_column,
        _header_only,
    ],
)
def test_state_zone_ranges_fail_loud(
    tmp_path: Path, edit: Callable[[list[list[str]]], None]
) -> None:
    path = _rewrite(tmp_path, overlays.STATE_ZONES_FILE, edit)
    with pytest.raises(OverlayDataError):
        overlays.load_state_zone_ranges(path)


def test_state_zone_ranges_reject_shares_summing_past_one(tmp_path: Path) -> None:
    def inflate(rows: list[list[str]]) -> None:
        for row in rows[1:]:
            if row[0] == "US-CT":
                row[2] = "0.9"

    path = _rewrite(tmp_path, overlays.STATE_ZONES_FILE, inflate)
    with pytest.raises(OverlayDataError, match="sum"):
        overlays.load_state_zone_ranges(path)


@pytest.mark.parametrize(
    "edit",
    [
        _set_cell(0, "US-XX"),
        _set_cell(0, "GB"),
        _set_cell(1, "july_avg_temp_f"),
        _set_cell(2, "nan"),
        _set_cell(2, "inf"),
        _set_cell(2, "warm"),
        _set_cell(3, "29"),  # not a 30-year mean
        _set_cell(3, "30.0"),
        _set_cell(3, "-30"),
        _duplicate_first,
        _swap_first_two,
        _rename_column,
        _header_only,
    ],
)
def test_state_normals_fail_loud(tmp_path: Path, edit: Callable[[list[list[str]]], None]) -> None:
    path = _rewrite(tmp_path, overlays.NORMALS_FILE, edit)
    with pytest.raises(OverlayDataError):
        overlays.load_state_normals(path)


def test_state_normals_may_omit_a_whole_state(tmp_path: Path) -> None:
    path = _rewrite(tmp_path, overlays.NORMALS_FILE, _drop_state("US-WY"))
    assert "US-WY" not in set(overlays.load_state_normals(path)["region_code"])


def test_state_normals_reject_a_state_missing_an_element(tmp_path: Path) -> None:
    def drop_one(rows: list[list[str]]) -> None:
        rows[1:] = [r for r in rows[1:] if not (r[0] == "US-CT" and r[1] == "jan_avg_low_f")]

    path = _rewrite(tmp_path, overlays.NORMALS_FILE, drop_one)
    with pytest.raises(OverlayDataError, match="US-CT"):
        overlays.load_state_normals(path)


# --------------------------------------------------------------------------- provenance


def test_notice_carries_the_disclaimer_and_the_terms_verbatim() -> None:
    notice = (DATA / overlays.NOTICE_FILE).read_text(encoding="utf-8")
    flat = re.sub(r"\s+", " ", notice)
    assert overlays.ZONE_DISCLAIMER in flat
    assert PHZM_TERMS_VERBATIM in flat
    assert "no USDA-ARS or OSU logo is used anywhere" in flat
    for needle in (
        "https://prism.oregonstate.edu/phzm/data/2023/phzm_us_zones_shp_2023.zip",
        "https://prism.oregonstate.edu/phzm/data/2023/phzm_ak_zones_shp_2023.zip",
        "https://prism.oregonstate.edu/phzm/data/2023/phzm_hi_zones_shp_2023.zip",
        "cb_2023_us_state_500k",
        "climate-at-a-glance/statewide/time-series",
        overlays.NORMALS_SOURCE,
        "Puerto Rico",
        "not NOAA's official station-based U.S. Climate Normals",
        overlays.OVERLAY_LABEL,
    ):
        assert needle in flat, needle


def test_sources_yaml_is_machine_readable_and_consistent() -> None:
    document = yaml.safe_load((DATA / overlays.SOURCES_FILE).read_text(encoding="utf-8"))
    sources = {source["id"]: source for source in document["sources"]}
    assert set(sources) == {"phzm_2023", "census_states_2023", "noaa_cag_statewide"}
    phzm = sources["phzm_2023"]
    assert phzm["required_disclaimer"] == overlays.ZONE_DISCLAIMER
    assert phzm["terms"] == PHZM_TERMS_VERBATIM
    assert sources["noaa_cag_statewide"]["title"] == overlays.NORMALS_SOURCE
    files = [item for source in sources.values() for item in source["files"]]
    assert len(files) == 3 + 1 + len(NOAA_STATES) * len(overlays.NORMAL_ELEMENTS)
    for item in files:
        assert re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", item["retrieved"])
        assert item["url"].startswith("https://")
    outputs = document["outputs"]
    assert set(outputs) == {overlays.ZONES_FILE, overlays.STATE_ZONES_FILE, overlays.NORMALS_FILE}
    zone_bytes = (DATA / overlays.ZONES_FILE).stat().st_size
    assert outputs[overlays.ZONES_FILE]["bytes"] == zone_bytes
    assert outputs[overlays.STATE_ZONES_FILE]["min_area_share"] == overlays.MIN_STATE_ZONE_SHARE


def test_noaa_state_codes_were_verified_against_alphabetical_order() -> None:
    document = yaml.safe_load((DATA / overlays.SOURCES_FILE).read_text(encoding="utf-8"))
    codes = next(s for s in document["sources"] if s["id"] == "noaa_cag_statewide")["state_codes"]
    contiguous = sorted(
        (US_STATES[code], code) for code in NOAA_STATES if code not in {"US-AK", "US-HI"}
    )
    assert {code: codes[code] for _, code in contiguous} == {
        code: index for index, (_, code) in enumerate(contiguous, start=1)
    }
    assert (codes["US-AK"], codes["US-HI"]) == (50, 51)


# --------------------------------------------------------------------------- runtime purity


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_overlay_loader_uses_only_stdlib_json_csv_and_pandas() -> None:
    allowed = {
        "__future__", "csv", "json", "math", "collections", "dataclasses", "itertools",
        "pathlib", "types", "typing", "pandas", "crop_explorer",
    }  # fmt: skip
    assert _imported_modules(ROOT / "src" / "crop_explorer" / "overlays.py") <= allowed


def test_runtime_code_never_imports_the_geometry_build_tools() -> None:
    sources = [*(ROOT / "src").rglob("*.py"), ROOT / "app.py", ROOT / "streamlit_app.py"]
    for path in sources:
        assert not _imported_modules(path) & set(GEOMETRY_TOOLS), path


def test_geometry_tools_are_an_optional_group_only() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    runtime = " ".join(project["project"]["dependencies"])
    assert not any(tool in runtime for tool in (*GEOMETRY_TOOLS, "pyshp"))
    assert {"pyshp", "shapely"} <= {
        re.split(r"[<>=!~ ]", spec)[0] for spec in project["dependency-groups"]["overlays"]
    }
    default_groups = project.get("tool", {}).get("uv", {}).get("default-groups", ["dev"])
    assert "overlays" not in default_groups


# --------------------------------------------------------------------------- browser-served copy


SERVED = ROOT / "static" / "overlays" / overlays.SERVED_ZONES_NAME


def test_the_browser_served_zone_layer_is_the_packaged_file() -> None:
    """Browsers fetch the zone layer from the app's static folder by URL (cached there), so the
    figure never carries 0.9 MB of GeoJSON; it must be byte-identical to the packaged file."""
    assert SERVED.read_bytes() == (DATA / overlays.ZONES_FILE).read_bytes()
    overlays.check_served_copy(SERVED)
    notice = (SERVED.parent / "NOTICE.md").read_text(encoding="utf-8")
    assert overlays.ZONE_DISCLAIMER in notice
    assert "src/crop_explorer/data/overlays/NOTICE.md" in notice


def test_a_missing_or_different_served_copy_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(OverlayDataError, match="missing"):
        overlays.check_served_copy(tmp_path / overlays.SERVED_ZONES_NAME)
    stale = tmp_path / overlays.SERVED_ZONES_NAME
    stale.write_bytes((DATA / overlays.ZONES_FILE).read_bytes() + b"\n")
    with pytest.raises(OverlayDataError, match="differs from the packaged"):
        overlays.check_served_copy(stale)
