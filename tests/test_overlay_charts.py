"""Tests for the map's optional regional climate overlays (``crop_explorer.overlay_charts``).

Checks the rules the overlays follow (SPEC §6-§7 and the dataviz method): an overlay is the
map's only fill, with one scale legend; reports become one count label per state that has any,
placed by Plotly from the state's code (never a coordinate); suppressed cells never show their
count; the zone layer is passed exactly as loaded and carries the "not the official map"
disclaimer; the viewer's zone is emphasised on the map and in the legend; light and dark tokens
come from validated ramps; every text is escaped; and the regional reference describes only
the viewer's chosen region.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Mapping
from itertools import pairwise
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import pytest

from crop_explorer import overlay_charts as oc
from crop_explorer.config import load_config
from crop_explorer.geography import TABLE_COLUMNS, RegionCounts
from crop_explorer.overlays import (
    HALF_ZONES,
    NORMAL_ELEMENTS,
    NORMALS_PERIOD,
    NORMALS_SOURCE,
    SERVED_ZONES_NAME,
    OVERLAY_LABEL,
    ZONE_DISCLAIMER,
    ZONE_PERIOD,
    ZONE_SOURCE,
    OverlayDataError,
    Overlays,
    load_overlays,
    zone_rank,
)
from crop_explorer.overview_charts import DARK, LIGHT, SEQUENTIAL_BLUE, ChartTheme
from crop_explorer.regions import US_STATES, region_name, us_postal

THEMES = pytest.mark.parametrize("theme", [LIGHT, DARK], ids=["light", "dark"])
ELEMENTS = pytest.mark.parametrize("element", list(NORMAL_ELEMENTS))
NOAA_STATES = sorted(set(US_STATES) - {"US-DC"})
CONTIGUOUS = sorted(set(NOAA_STATES) - {"US-AK", "US-HI"})
TRANSPARENT = "rgba(0,0,0,0)"
MAP_CFG = load_config().map
HEIGHT = MAP_CFG.height_px
MUTE = MAP_CFG.zone_mute
SIZE: dict[str, Any] = {"height": HEIGHT, "mute": MUTE}
"""The configured map height and zone fade (``config.yaml`` ``map``)."""


@pytest.fixture(scope="module")
def refs() -> Overlays:
    return load_overlays()


def counts(
    cells: Mapping[str, int], *, suppressed: frozenset[str] = frozenset(), min_cell: int = 1
) -> RegionCounts:
    """A ``RegionCounts`` with the given displayed counts (suppressed cells show 0)."""
    rows = [
        (
            code,
            region_name(code),
            us_postal(code),
            0 if code in suppressed else n,
            code in suppressed,
        )
        for code, n in cells.items()
    ]
    table = pd.DataFrame(rows, columns=list(TABLE_COLUMNS)).astype(
        {"n_comments": "int64", "suppressed": "bool"}
    )
    return RegionCounts(
        table, not_reported=2, outside_us=1, us_state_not_reported=0, min_cell_count=min_cell
    )


SOME = counts({"US-CT": 4, "US-TX": 1, "US-AK": 2})


def oklab_lightness(color: str) -> float:
    """OKLab L of an ``#rrggbb`` colour (an independent re-implementation for the tests)."""

    def linear(channel: int) -> float:
        c = channel / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(int(color[i : i + 2], 16)) for i in (1, 3, 5))
    lms = (
        0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b,
        0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b,
        0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b,
    )
    l_, m_, s_ = (math.cbrt(value) for value in lms)
    return 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_


def fill_traces(figure: go.Figure) -> list[Any]:
    """Traces that paint area: choropleths whose colour scale is not fully transparent."""
    return [
        trace
        for trace in figure.data
        if isinstance(trace, go.Choropleth)
        and any(color != TRANSPARENT for _, color in trace.colorscale)
    ]


def label_trace(figure: go.Figure) -> go.Scattergeo:
    (trace,) = [t for t in figure.data if isinstance(t, go.Scattergeo)]
    return trace


def all_overlays(refs: Overlays, theme: ChartTheme) -> Iterator[tuple[str, go.Figure]]:
    for key in oc.OVERLAY_KEYS[1:]:
        yield key, oc.overlay_figure(key, refs, SOME, theme, **SIZE)


def figure_strings(figure: go.Figure) -> list[str]:
    found: list[str] = []
    for trace in figure.data:
        found += [str(t) for t in (trace.text or [])]
        colorbar = getattr(trace, "colorbar", None)
        if colorbar is not None and colorbar.title.text:
            found.append(colorbar.title.text)
    return found


# --------------------------------------------------------------------------- choices and copy


def test_overlay_choices_are_off_by_default_and_named_as_specified() -> None:
    assert oc.OVERLAY_KEYS[0] == "none"
    assert dict(oc.OVERLAY_LABELS) == {
        "none": "None",
        "zones": "USDA hardiness zones (2023)",
        "july_avg_high_f": "Average July high (NOAA 1991-2020)",
        "jan_avg_low_f": "Average January low (NOAA 1991-2020)",
        "annual_precip_in": "Annual precipitation (NOAA 1991-2020)",
    }
    assert set(oc.OVERLAY_KEYS[2:]) == set(NORMAL_ELEMENTS)


def test_none_has_no_overlay(refs: Overlays) -> None:
    with pytest.raises(ValueError, match="no overlay"):
        oc.overlay_view("none")
    with pytest.raises(ValueError, match="no overlay"):
        oc.overlay_figure("none", refs, SOME, LIGHT, **SIZE)


def test_zone_view_carries_label_source_period_and_the_disclaimer() -> None:
    view = oc.overlay_view("zones")
    assert view.title == "USDA hardiness zones (2023)"
    assert view.disclaimer == ZONE_DISCLAIMER
    assert view.caption.startswith(
        f"{OVERLAY_LABEL.capitalize()} · {ZONE_SOURCE} · {ZONE_PERIOD}."
    )
    assert "never assigned to the reports" in view.caption
    assert "aggregates, not garden locations" in view.caption
    assert "logo" not in view.caption.lower()


def test_zone_view_names_the_viewers_zone() -> None:
    assert oc.overlay_view("zones", "6b").title == "Where zone 6b is (2023 USDA zones)"
    assert oc.overlay_view("zones", "6").title == "Where zone 6 is (2023 USDA zones)"


@ELEMENTS
def test_noaa_views_say_what_the_averages_are_not(element: str) -> None:
    view = oc.overlay_view(element)  # type: ignore[arg-type]
    assert view.disclaimer is None
    assert NORMALS_SOURCE in view.caption
    assert view.caption.count(NORMALS_PERIOD) == 1  # the source already names the period
    assert view.caption.startswith(f"{OVERLAY_LABEL.capitalize()} · {NORMALS_SOURCE}. ")
    assert "not the weather of any season or garden" in view.caption
    assert "not NOAA's station-based Climate Normals" in view.caption
    assert "never attached to the reports here" in view.caption


def test_emphasis_zones() -> None:
    assert oc.emphasis_zones(None) == ()
    assert oc.emphasis_zones("6b") == ("6b",)
    assert oc.emphasis_zones("6") == ("6a", "6b")
    with pytest.raises(OverlayDataError):
        oc.emphasis_zones("14a")


# --------------------------------------------------------------------------- zone figure


@THEMES
def test_zone_figure_is_one_fill_under_state_outlines_with_count_labels(
    refs: Overlays, theme: ChartTheme
) -> None:
    figure = oc.zone_figure(refs, SOME, theme, **SIZE)
    zones, states, labels = figure.data
    assert isinstance(zones, go.Choropleth)
    assert zones.featureidkey == "properties.zone"
    assert zones.hoverinfo == "skip"
    assert list(zones.locations) == list(refs.zones.table["zone"])
    assert list(zones.z) == [zone_rank(z) for z in zones.locations]
    assert fill_traces(figure) == [zones]
    assert isinstance(states, go.Choropleth)
    assert states.locationmode == "USA-states"
    assert states.showscale is False
    assert states.marker.line.color == theme.surface
    assert isinstance(labels, go.Scattergeo)


def test_zone_geojson_is_the_app_served_copy_by_url(refs: Overlays) -> None:
    """The figure names the layer's URL, so the 0.9 MB GeoJSON is never copied into a figure.

    The browser fetches and caches the app-served copy (byte-identical to the packaged file,
    which is passed as is: Plotly does not rewind rings).
    """
    figure = oc.zone_figure(refs, SOME, LIGHT, **SIZE)
    assert figure.data[0].geojson == oc.ZONE_LAYER_URL
    assert oc.ZONE_LAYER_URL == f"app/static/overlays/{SERVED_ZONES_NAME}"
    assert len(figure.to_json()) < 50_000


def test_zone_figure_hover_is_per_state_with_its_range_and_reports(refs: Overlays) -> None:
    figure = oc.zone_figure(refs, SOME, LIGHT, **SIZE)
    states = figure.data[1]
    assert sorted(states.locations) == sorted(us_postal(code) for code in US_STATES)
    hover = dict(zip(states.locations, states.text, strict=True))
    assert hover["CT"] == (
        "Connecticut<br>Main USDA zones by area: 6a-7b (2023, simplified)"
        "<br>Reports naming this state: 4 (zones are not assigned to reports)"
    )
    assert hover["VT"].endswith("Reports naming this state: none shown (zones are not assigned to reports)")
    assert states.hovertemplate == "%{text}<extra></extra>"


@THEMES
def test_zone_scale_legend_runs_cold_to_warm_with_one_tick_per_zone(
    refs: Overlays, theme: ChartTheme
) -> None:
    zones = oc.zone_figure(refs, SOME, theme, **SIZE).data[0]
    bar = zones.colorbar
    assert bar.orientation == "h"
    assert "colder" in bar.title.text and "warmer" in bar.title.text
    assert list(bar.ticktext) == [str(n) for n in range(1, 14)]
    assert list(bar.tickvals) == [2 * n - 1.5 for n in range(1, 14)]
    assert (zones.zmin, zones.zmax) == (-0.5, len(HALF_ZONES) - 0.5)
    ramp = oc.ZONE_RAMP_LIGHT if theme is LIGHT else oc.ZONE_RAMP_DARK
    bands = [color for _, color in zones.colorscale][::2]
    assert bands == list(ramp)
    assert bar.title.font.color == theme.text_secondary


def test_zone_outlines_match_each_fill_so_no_seams_show(refs: Overlays) -> None:
    zones = oc.zone_figure(refs, SOME, LIGHT, **SIZE).data[0]
    assert list(zones.marker.line.color) == [
        oc.ZONE_RAMP_LIGHT[zone_rank(zone)] for zone in zones.locations
    ]


@THEMES
def test_viewer_zone_is_emphasised_on_the_map_and_in_the_legend(
    refs: Overlays, theme: ChartTheme
) -> None:
    zones = oc.zone_figure(refs, SOME, theme, emphasis=("6b",), **SIZE).data[0]
    ramp = oc.ZONE_RAMP_LIGHT if theme is LIGHT else oc.ZONE_RAMP_DARK
    bands = [color for _, color in zones.colorscale][::2]
    kept = [
        zone for zone, band, color in zip(HALF_ZONES, bands, ramp, strict=True) if band == color
    ]
    assert kept == ["6b"]
    six_b = zone_rank("6b")
    assert (
        dict(zip(zones.colorbar.tickvals, zones.colorbar.ticktext, strict=True))[six_b]
        == "<b>6b</b>"
    )
    assert "6" not in zones.colorbar.ticktext  # 6b's tick replaces zone 6's
    assert fill_traces(oc.zone_figure(refs, SOME, theme, emphasis=("6b",), **SIZE)) == [
        oc.zone_figure(refs, SOME, theme, emphasis=("6b",), **SIZE).data[0]
    ]


def test_muted_zones_blend_toward_the_surface(refs: Overlays) -> None:
    for theme, ramp in ((LIGHT, oc.ZONE_RAMP_LIGHT), (DARK, oc.ZONE_RAMP_DARK)):
        bands = [
            c for _, c in oc.zone_figure(refs, SOME, theme, emphasis=("6b",), **SIZE).data[0].colorscale
        ][::2]
        surface = oklab_lightness(theme.surface)
        for zone, band, color in zip(HALF_ZONES, bands, ramp, strict=True):
            if zone != "6b":
                assert abs(oklab_lightness(band) - surface) < abs(oklab_lightness(color) - surface)


def test_a_whole_zone_emphasises_both_halves(refs: Overlays) -> None:
    figure = oc.overlay_figure("zones", refs, SOME, LIGHT, zone="6", **SIZE)
    zones = figure.data[0]
    bands = [color for _, color in zones.colorscale][::2]
    kept = [z for z, b, c in zip(HALF_ZONES, bands, oc.ZONE_RAMP_LIGHT, strict=True) if b == c]
    assert kept == ["6a", "6b"]
    assert "<b>6</b>" in zones.colorbar.ticktext


# --------------------------------------------------------------------------- count labels


def test_count_labels_only_for_states_with_reports_and_no_coordinates(refs: Overlays) -> None:
    for _, figure in all_overlays(refs, LIGHT):
        labels = label_trace(figure)
        assert labels.locationmode == "USA-states"
        assert list(labels.locations) == ["CT", "TX", "AK"]
        assert list(labels.text) == ["4", "1", "2"]
        assert labels.mode == "text"
        assert labels.lat is None and labels.lon is None  # placed by Plotly from the code
        assert labels.hoverinfo == "skip"


def test_no_reports_means_no_labels(refs: Overlays) -> None:
    figure = oc.overlay_figure("zones", refs, counts({}), LIGHT, **SIZE)
    assert list(label_trace(figure).locations) == []


def test_suppressed_cells_never_show_their_count(refs: Overlays) -> None:
    small = counts({"US-CT": 4, "US-TX": 0}, suppressed=frozenset({"US-TX"}), min_cell=3)
    for key in oc.OVERLAY_KEYS[1:]:
        figure = oc.overlay_figure(key, refs, small, LIGHT, **SIZE)
        labels = dict(zip(label_trace(figure).locations, label_trace(figure).text, strict=True))
        assert labels == {"CT": "4", "TX": "&lt;3"}  # displayed as "<3"
        hover = [
            t for trace in figure.data[:-1] for t in (trace.text or ()) if t.startswith("Texas")
        ]
        assert hover and all("Reports naming this state: fewer than 3 (" in t for t in hover)


@THEMES
def test_count_labels_use_text_tokens_with_a_surface_halo(
    refs: Overlays, theme: ChartTheme
) -> None:
    for _, figure in all_overlays(refs, theme):
        font = label_trace(figure).textfont
        assert font.color == theme.text_primary
        assert theme.surface in font.shadow


# --------------------------------------------------------------------------- NOAA figures


@THEMES
@ELEMENTS
def test_noaa_figure_is_one_state_fill_with_a_legend(
    refs: Overlays, theme: ChartTheme, element: str
) -> None:
    figure = oc.normals_figure(refs.normals, element, SOME, theme, height=HEIGHT)  # type: ignore[arg-type]
    fill, labels = figure.data
    assert fill_traces(figure) == [fill]
    assert fill.locationmode == "USA-states"
    assert sorted(fill.locations) == sorted(us_postal(code) for code in NOAA_STATES)
    rows = refs.normals[refs.normals["element"] == element]
    assert list(fill.z) == list(rows["value"])
    contiguous = rows[rows["region_code"].isin(CONTIGUOUS)]["value"]
    assert (fill.zmin, fill.zmax) == (min(contiguous), max(contiguous))
    anchors = oc.NORMALS_ANCHORS_LIGHT if theme is LIGHT else oc.NORMALS_ANCHORS_DARK
    assert [color for _, color in fill.colorscale] == list(anchors)
    assert fill.colorbar.orientation == "h"
    assert fill.colorbar.title.text in {
        "July average high (°F)",
        "January average low (°F)",
        "Annual precipitation (inches)",
    }
    assert fill.marker.line.color == theme.surface
    assert isinstance(labels, go.Scattergeo)


def test_noaa_hover_names_the_average_and_the_reports(refs: Overlays) -> None:
    fill = oc.normals_figure(refs.normals, "july_avg_high_f", SOME, LIGHT, height=HEIGHT).data[0]
    hover = dict(zip(fill.locations, fill.text, strict=True))
    assert hover["CT"] == (
        "Connecticut<br>July average high: 82.9°F<br>statewide 1991-2020 average (NOAA)"
        "<br>Reports naming this state: 4 (the average is not attached to reports)"
    )


def test_noaa_scale_spans_the_contiguous_states_and_says_who_is_off_it(refs: Overlays) -> None:
    """Alaska's July (62.3°F) would squeeze the 48 states into the dark half of the ramp."""
    july = oc.scale_note("july_avg_high_f", refs)
    assert july == (
        "The colour scale spans the 48 contiguous states (77.2°F to 95.2°F); Alaska (62.3°F) "
        "and Hawaii (76.3°F) are below it and drawn in its lowest colour."
    )
    january = oc.scale_note("jan_avg_low_f", refs)
    assert january is not None
    assert "Alaska (-2.4°F) is below it and drawn in its lowest colour" in january
    assert "Hawaii (55.4°F) is above it and drawn in its highest colour" in january
    rain = oc.scale_note("annual_precip_in", refs)
    assert rain is not None and "Hawaii (61.7 in) is above it" in rain
    assert oc.scale_note("zones", refs) is None


def test_unknown_noaa_element_is_rejected(refs: Overlays) -> None:
    with pytest.raises(ValueError, match="Unknown NOAA statewide average"):
        oc.normals_figure(refs.normals, "snowfall", SOME, LIGHT, height=HEIGHT)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- shared chrome


@THEMES
def test_overlay_maps_are_static_transparent_and_us_only(
    refs: Overlays, theme: ChartTheme
) -> None:
    for _, figure in all_overlays(refs, theme):
        layout = figure.layout
        assert layout.geo.scope == "usa"
        assert layout.geo.projection.type == "albers usa"
        assert layout.geo.showland is False  # no base-map lens above the 49th parallel
        assert layout.dragmode is False
        assert layout.paper_bgcolor == TRANSPARENT
        assert layout.height == HEIGHT
        assert layout.hoverlabel.bgcolor == theme.surface
        assert layout.font.color == theme.text_secondary
        assert len(fill_traces(figure)) == 1


def test_no_markup_can_form_in_overlay_text(refs: Overlays) -> None:
    """Only the line breaks and the bold emphasis tick are markup; everything else is text."""
    allowed = re.compile(r"<br>|</?b>")
    for _, figure in all_overlays(refs, LIGHT):
        for text in figure_strings(figure):
            assert "<" not in allowed.sub("", text), text


# --------------------------------------------------------------------------- colour tokens


def test_zone_ramps_are_monotone_semantic_heat_between_validated_anchors() -> None:
    for ramp, anchors, falling in (
        (oc.ZONE_RAMP_LIGHT, oc.ZONE_ANCHORS_LIGHT, True),
        (oc.ZONE_RAMP_DARK, oc.ZONE_ANCHORS_DARK, False),
    ):
        assert len(ramp) == len(HALF_ZONES)
        assert len(set(ramp)) == len(ramp)
        lightness = [oklab_lightness(color) for color in ramp]
        assert lightness == sorted(lightness, reverse=falling)  # cold light (light mode)
        assert (ramp[0], ramp[-1]) == (anchors[0], anchors[-1])
        steps = [oklab_lightness(c) for c in anchors]
        assert min(abs(a - b) for a, b in pairwise(steps)) >= 0.06


def test_orange_ramp_mirrors_the_blue_ramp_and_anchors_are_its_steps() -> None:
    assert len(oc.SEQUENTIAL_ORANGE) == len(SEQUENTIAL_BLUE)
    for orange, blue in zip(oc.SEQUENTIAL_ORANGE, SEQUENTIAL_BLUE, strict=True):
        assert abs(oklab_lightness(orange) - oklab_lightness(blue)) < 0.005
    assert oc.SEQUENTIAL_ORANGE[5] == "#eb6834"  # categorical slot 2
    steps = dict(zip(range(100, 701, 50), oc.SEQUENTIAL_ORANGE, strict=True))
    # Sequential (a magnitude): the low end may recede toward the surface, as on the topic grid.
    assert oc.NORMALS_ANCHORS_LIGHT == tuple(steps[s] for s in (100, 200, 300, 400, 500, 600))
    assert oc.NORMALS_ANCHORS_DARK == tuple(steps[s] for s in (700, 600, 500, 400, 300, 200))
    light = [oklab_lightness(c) for c in oc.NORMALS_ANCHORS_LIGHT]
    dark = [oklab_lightness(c) for c in oc.NORMALS_ANCHORS_DARK]
    assert light == sorted(light, reverse=True)  # light mode: more is darker
    assert dark == sorted(dark)  # dark mode: more is lighter
    for ramp in (light, dark):
        assert min(abs(a - b) for a, b in pairwise(ramp)) >= 0.06  # visible anchor steps


# --------------------------------------------------------------------------- tables


def test_zone_range_table(refs: Overlays) -> None:
    table = oc.zone_range_table(refs.state_zones)
    assert list(table.columns) == ["State", "Main zones by area", "Largest area"]
    assert len(table) == len(US_STATES)
    assert list(table["State"]) == sorted(US_STATES.values())
    ct = table[table["State"] == "Connecticut"].iloc[0]
    assert (ct["Main zones by area"], ct["Largest area"]) == ("6a-7b", "6b")


def test_zone_share_table_is_tidy_with_numeric_shares(refs: Overlays) -> None:
    table = oc.zone_share_table(refs.state_zones)
    assert list(table.columns) == ["State", "Zone", "Share of the state's area (%)"]
    assert len(table) == len(refs.state_zones)
    assert table["Share of the state's area (%)"].dtype == "float64"
    ct = table[table["State"] == "Connecticut"]
    assert list(ct["Zone"]) == ["6a", "6b", "7a", "7b"]  # cold to warm
    assert list(ct["Share of the state's area (%)"]) == [18.7, 65.0, 16.0, 0.3]
    assert list(table["State"]) == sorted(table["State"])
    assert table["Share of the state's area (%)"].min() >= 0.1  # main zones only


def test_zone_tables_carry_the_disclaimer_and_the_share_threshold(refs: Overlays) -> None:
    (states, shares, key) = oc.overlay_tables("zones", refs)
    assert states[0] == (
        "Main zones by area: the half-zones covering at least 0.1% of each state "
        "(2023, simplified)"
    )
    assert shares[0] == "Share of each state's area by half-zone (2023, simplified)"
    assert key[0] == "Zone key"
    assert oc.overlay_view("zones").disclaimer == ZONE_DISCLAIMER  # shown with the tables


def test_zone_key_table(refs: Overlays) -> None:
    table = oc.zone_key_table(refs.zones)
    assert list(table["Zone"]) == list(refs.zones.table["zone"])
    assert table.set_index("Zone").loc["6b"].iloc[0] == "-5 to 0"


@ELEMENTS
def test_normals_table(refs: Overlays, element: str) -> None:
    table = oc.normals_table(refs.normals, element)  # type: ignore[arg-type]
    assert len(table) == len(NOAA_STATES)
    assert list(table["State"]) == sorted(table["State"])
    assert "District of Columbia" not in set(table["State"])
    assert table.columns[0] == "State"


def test_normals_table_values() -> None:
    refs = load_overlays()
    table = oc.normals_table(refs.normals, "july_avg_high_f").set_index("State")
    assert table.loc["Connecticut", "July average high (°F)"] == 82.9


# --------------------------------------------------------------------------- regional reference


def test_no_region_no_reference(refs: Overlays) -> None:
    assert oc.regional_reference(None, refs) is None


def test_us_state_reference(refs: Overlays) -> None:
    reference = oc.regional_reference("US-CT", refs)
    assert reference is not None
    assert reference.summary == (
        "Connecticut — main USDA zones by area 6a-7b (2023, simplified; largest area 6b); "
        "July average high 83°F, January average low 18°F, annual precipitation 49 in "
        "(NOAA 1991-2020 statewide averages)"
    )
    assert reference.disclaimer == ZONE_DISCLAIMER
    assert ZONE_DISCLAIMER not in reference.sources  # shown on its own, prominently
    assert reference.sources.startswith(
        "Main zones: the half-zones covering at least 0.1% of the state's area, so a few "
        "places can be in a zone not listed; for an exact zone, look up a ZIP code at "
        "planthardiness.ars.usda.gov."
    )
    assert "not a reading from any report's garden" in reference.sources
    assert "never used to sort or match reports" in reference.sources
    assert "NOAA NCEI Climate at a Glance (nClimDiv)" in reference.sources
    assert "not observed weather" in reference.sources


def test_negative_averages_never_show_minus_zero(refs: Overlays) -> None:
    reference = oc.regional_reference("US-AK", refs)
    assert reference is not None
    assert "January average low -2°F" in reference.summary
    assert "-0°F" not in reference.summary


def test_dc_says_noaa_averages_are_missing(refs: Overlays) -> None:
    reference = oc.regional_reference("US-DC", refs)
    assert reference is not None
    assert reference.summary.startswith("District of Columbia — main USDA zones by area 7b-8a")
    assert reference.summary.endswith(
        "NOAA statewide averages are not available for District of Columbia."
    )
    assert reference.disclaimer == ZONE_DISCLAIMER
    assert "NOAA" not in reference.sources  # no averages shown, so no NOAA source
    assert reference.sources.startswith("Main zones:")


@pytest.mark.parametrize(("code", "name"), [("CA-ON", "Ontario"), ("GB", "United Kingdom")])
def test_outside_the_us_says_what_is_unavailable(refs: Overlays, code: str, name: str) -> None:
    reference = oc.regional_reference(code, refs)
    assert reference is not None
    assert reference.summary.startswith(f"No regional climate reference for {name}")
    assert reference.disclaimer is None
    assert reference.sources == ""
