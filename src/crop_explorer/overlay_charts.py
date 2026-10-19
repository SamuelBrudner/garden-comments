"""The map's optional regional climate overlays: Plotly figures, table twins and copy (pure).

An overlay is **context, not an answer** (SPEC §6-§7). It is off by default; when one is on it
is the map's only fill, the state outlines stay, and the shown reports become one count label
per state that has any (an aggregate placed by Plotly at the state's centre from its own
``USA-states`` shapes: no coordinates are passed or derived for reports). Every overlay names
its evidence label (:data:`~crop_explorer.overlays.OVERLAY_LABEL`), source and period, and the
zone layer, and anything derived from it, carries
:data:`~crop_explorer.overlays.ZONE_DISCLAIMER` (the data are simplified, so no USDA-ARS or OSU
logo may appear). Nothing here reads a comment: the counts arrive as a
:class:`~crop_explorer.geography.RegionCounts`, and no zone or climate value is ever attached to
a report or used in matching. :func:`regional_reference` describes the region the **viewer**
chose, never a commenter's garden.

Colours. The NOAA layers are magnitudes on one hue (:data:`SEQUENTIAL_ORANGE`, the second
sequential hue: blue already means "reports"), drawn as sequential choropleths whose scale spans
the 48 contiguous states (Alaska and Hawaii, when outside it, take its end colours and
:func:`scale_note` says so). The zone layer is the one multi-hue exception the dataviz rules
allow, **semantic heat**: cold zones blue, warm zones red, through violet, with lightness
monotone (light mode: cold is light; dark mode: cold recedes into the surface), always with a
scale legend. Only its seven anchors (:data:`ZONE_ANCHORS_LIGHT`, :data:`ZONE_ANCHORS_DARK`)
pass the ordinal checks; the 26 half-zone steps interpolated between them are, by design, closer
than the ordinal lightness gate (0.06), so neighbouring half-zones cannot be told apart by colour
alone. Which zone a place is in is read with the emphasis (the viewer's zone, or any zone picked
with the map's "Highlight a zone"), not from the colour.

The zone figure names the layer by URL (:data:`ZONE_LAYER_URL`: the app serves a byte-identical
copy of the packaged file), so the 0.9 MB GeoJSON is never copied into a figure; browsers fetch
it once and cache it. Plotly does not rewind GeoJSON, so the file keeps clockwise exteriors (see
:mod:`crop_explorer.overlays`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal, get_args

import pandas as pd
import plotly.graph_objects as go

from crop_explorer.geography import RegionCounts, count_label
from crop_explorer.overlays import (
    HALF_ZONES,
    MIN_STATE_ZONE_SHARE,
    NORMALS_PERIOD,
    NORMALS_SOURCE,
    OVERLAY_LABEL,
    SERVED_ZONES_NAME,
    ZONE_DISCLAIMER,
    ZONE_PERIOD,
    ZONE_SOURCE,
    Overlays,
    ZoneLayer,
    zone_rank,
)
from crop_explorer.overview_charts import FONT_FAMILY, ChartTheme, plotly_text
from crop_explorer.regions import REGION_NAMES, US_STATES, is_us_state, us_postal

__all__ = [
    "NORMALS_ANCHORS_DARK",
    "NORMALS_ANCHORS_LIGHT",
    "OVERLAY_KEYS",
    "OVERLAY_LABELS",
    "SEQUENTIAL_ORANGE",
    "ZONE_ANCHORS_DARK",
    "ZONE_ANCHORS_LIGHT",
    "ZONE_LAYER_URL",
    "ZONE_RAMP_DARK",
    "ZONE_RAMP_LIGHT",
    "NormalElement",
    "OverlayKey",
    "OverlayView",
    "RegionalReference",
    "emphasis_zones",
    "normals_figure",
    "normals_table",
    "overlay_figure",
    "overlay_tables",
    "overlay_view",
    "regional_reference",
    "scale_note",
    "zone_figure",
    "zone_key_table",
    "zone_range_table",
    "zone_share_table",
]

OverlayKey = Literal["none", "zones", "july_avg_high_f", "jan_avg_low_f", "annual_precip_in"]
"""The map's overlay choices; ``"none"`` is the default report-count map."""
NormalElement = Literal["july_avg_high_f", "jan_avg_low_f", "annual_precip_in"]
"""The three NOAA statewide averages (:data:`~crop_explorer.overlays.NORMAL_ELEMENTS`)."""

OVERLAY_KEYS: Final[tuple[OverlayKey, ...]] = get_args(OverlayKey)
OVERLAY_LABELS: Final[Mapping[OverlayKey, str]] = MappingProxyType(
    {
        "none": "None",
        "zones": "USDA hardiness zones (2023)",
        "july_avg_high_f": "Average July high (NOAA 1991-2020)",
        "jan_avg_low_f": "Average January low (NOAA 1991-2020)",
        "annual_precip_in": "Annual precipitation (NOAA 1991-2020)",
    }
)
"""What the overlay picker shows for each choice."""
ZONE_LAYER_URL: Final = f"app/static/overlays/{SERVED_ZONES_NAME}"
"""Where browsers fetch the zone layer: the app's static copy of the packaged GeoJSON
(``static/overlays/``, checked byte-identical by :func:`crop_explorer.overlays.check_served_copy`).
Relative, so it also works under a base URL path, like the base map."""

# --------------------------------------------------------------------------- colour tokens

SEQUENTIAL_ORANGE: Final = (
    "#fbd6c9",
    "#f7c3b0",
    "#f4ae95",
    "#f19979",
    "#ee8159",
    "#eb6834",
    "#d95924",
    "#c54d19",
    "#b04414",
    "#9b3b11",
    "#853310",
    "#702b0f",
    "#5c240e",
)
"""One-hue orange ramp, steps 100 to 700 (OKLCH hue 40.6, the lightness of
:data:`~crop_explorer.overview_charts.SEQUENTIAL_BLUE` step for step; step 350 is categorical
slot 2, ``#eb6834``)."""
NORMALS_ANCHORS_LIGHT: Final = tuple(SEQUENTIAL_ORANGE[i] for i in (0, 2, 4, 6, 8, 10))
"""Light mode, lowest to highest value: steps 100, 200, ... 600. A sequential magnitude ramp:
the low end recedes toward the light surface (as on the topic grid) instead of filling the
country with saturated blocks; monotone, one hue, anchor steps ΔL >= 0.06."""
NORMALS_ANCHORS_DARK: Final = tuple(SEQUENTIAL_ORANGE[i] for i in (12, 10, 8, 6, 4, 2))
"""Dark mode, lowest to highest value: steps 700, 600, ... 200 (the low end recedes into the
dark surface); monotone, one hue, anchor steps ΔL >= 0.06."""
ZONE_ANCHORS_LIGHT: Final = (
    "#80b4ed",
    "#7d98e3",
    "#8e76cd",
    "#a34f9e",
    "#a62d56",
    "#8b2324",
    "#692005",
)
"""Semantic-heat anchors, coldest to warmest, light mode: OKLCH L 0.755 to 0.355 in equal
steps; hue 252 (blue), 268, 295 (violet), 330, 5, 25, 38 (red)."""
ZONE_ANCHORS_DARK: Final = (
    "#1c4f97",
    "#4d5ab6",
    "#8b5dc6",
    "#ca61b2",
    "#f1778a",
    "#fa9e7b",
    "#fbc392",
)
"""Dark mode anchors, coldest to warmest: L 0.435 to 0.855 in equal steps; hue 258 (blue),
274, 302 (violet), 337, 12, 42, 62 (warm orange)."""
ZONE_RAMP_LIGHT: Final = (
    "#80b4ed",
    "#7fadeb",
    "#7da7e9",
    "#7da0e6",
    "#7d99e3",
    "#8091e0",
    "#8489dc",
    "#8881d6",
    "#8d79d0",
    "#9270c8",
    "#9866be",
    "#9d5db2",
    "#a253a5",
    "#a54a96",
    "#a74285",
    "#a83a74",
    "#a73262",
    "#a52b51",
    "#9e2945",
    "#982639",
    "#91242d",
    "#8a2222",
    "#82221b",
    "#792113",
    "#71210c",
    "#692005",
)
"""One colour per half-zone ``1a`` ... ``13b`` (:data:`~crop_explorer.overlays.HALF_ZONES`),
interpolated in OKLCH between :data:`ZONE_ANCHORS_LIGHT`."""
ZONE_RAMP_DARK: Final = (
    "#1c4f97",
    "#29529f",
    "#3554a6",
    "#4057ae",
    "#4b59b5",
    "#5a5bbb",
    "#695bc0",
    "#785cc3",
    "#865dc6",
    "#965dc5",
    "#a65ec1",
    "#b55fbc",
    "#c360b6",
    "#d063ad",
    "#db67a4",
    "#e46c9a",
    "#ec7390",
    "#f27a88",
    "#f68382",
    "#f88d7e",
    "#f9967b",
    "#faa07b",
    "#faa97f",
    "#fbb284",
    "#fbba8b",
    "#fbc392",
)
"""Dark mode twin of :data:`ZONE_RAMP_LIGHT`, between :data:`ZONE_ANCHORS_DARK`."""

_ZONE_RAMPS: Final = MappingProxyType({"light": ZONE_RAMP_LIGHT, "dark": ZONE_RAMP_DARK})
_NORMALS_RAMPS: Final = MappingProxyType(
    {"light": NORMALS_ANCHORS_LIGHT, "dark": NORMALS_ANCHORS_DARK}
)
_SEAM_PX: Final = 0.6
_OUTLINE_PX: Final = 0.6
_LABEL_PX: Final = 12
_FONT_PX: Final = 13
_TRANSPARENT: Final = "rgba(0,0,0,0)"
_HOVER: Final = "%{text}<extra></extra>"
_UNITS: Final[Mapping[NormalElement, str]] = MappingProxyType(
    {"july_avg_high_f": "°F", "jan_avg_low_f": "°F", "annual_precip_in": " in"}
)
_SCALE_TITLES: Final[Mapping[NormalElement, str]] = MappingProxyType(
    {
        "july_avg_high_f": "July average high (°F)",
        "jan_avg_low_f": "January average low (°F)",
        "annual_precip_in": "Annual precipitation (inches)",
    }
)
_SHORT: Final[Mapping[NormalElement, str]] = MappingProxyType(
    {
        "july_avg_high_f": "July average high",
        "jan_avg_low_f": "January average low",
        "annual_precip_in": "annual precipitation",
    }
)
_ZONE_SCALE_TITLE: Final = "USDA hardiness zone (2023): colder ← → warmer"
_ZONES_NOTE: Final = (
    "Zones describe the coldest winter nights only (not summer heat, humidity, season length, "
    "soil or sun) and are never assigned to the reports here."
)
_NORMALS_NOTE: Final = (
    "A statistical average over a whole state: not the weather of any season or garden, not "
    "NOAA's station-based Climate Normals, and never attached to the reports here."
)
_COUNTS_NOTE: Final = (
    "Numbers: shown reports naming each state (one per comment and state), at the state's "
    "centre: aggregates, not garden locations."
)
_COLORBAR: Final = MappingProxyType(
    {
        "orientation": "h",
        "x": 0.5,
        "xanchor": "center",
        "y": 0,
        "yanchor": "top",
        "thickness": 10,
        "outlinewidth": 0,
    }
)
"""Horizontal scale legend under the map, as on the report-count map (fits a phone)."""
_GEO: Final = MappingProxyType(
    {
        "scope": "usa",
        "projection": {"type": "albers usa"},
        "bgcolor": _TRANSPARENT,
        "showland": False,
        "showlakes": False,
        "showcoastlines": False,
        "showsubunits": False,
        "showcountries": False,
        "showframe": False,
    }
)
"""The U.S. only, no base-map fills or borders: the overlay is the only fill and the state
outlines come from the state layer (Plotly's base land would show a lens above the
49th parallel west of Minnesota)."""


# --------------------------------------------------------------------------- text


@dataclass(frozen=True)
class OverlayView:
    """The words shown around an overlay map.

    Attributes
    ----------
    title : str
        The line above the map ("Where zone 6b is (2023 USDA zones)").
    source, period : str
        Where the data come from and the period they describe.
    note : str
        What the layer does and does not say.
    disclaimer : str or None
        :data:`~crop_explorer.overlays.ZONE_DISCLAIMER` for the zone layer, else ``None``.
    """

    title: str
    source: str
    period: str
    note: str
    disclaimer: str | None

    @property
    def caption(self) -> str:
        """The caption under the map: label, source, period, the note and the counts note."""
        lead = f"{OVERLAY_LABEL.capitalize()} · {self.source} · {self.period}."
        return f"{lead} {self.note} {_COUNTS_NOTE}"


def emphasis_zones(zone: str | None) -> tuple[str, ...]:
    """Return the half-zones to emphasise for the viewer's zone choice.

    ``"6b"`` -> ``("6b",)``; a whole zone ``"6"`` -> ``("6a", "6b")``; ``None`` -> ``()``.

    Raises
    ------
    crop_explorer.overlays.OverlayDataError
        If ``zone`` is not a USDA zone such as ``6`` or ``6b``.
    """
    if zone is None:
        return ()
    halves = (zone,) if zone[-1] in "ab" else (f"{zone}a", f"{zone}b")
    for half in halves:
        zone_rank(half)
    return halves


def _zone_title(zone: str | None) -> str:
    return OVERLAY_LABELS["zones"] if zone is None else f"Where zone {zone} is (2023 USDA zones)"


def overlay_view(key: OverlayKey, zone: str | None = None) -> OverlayView:
    """Return the title, source, period, note and disclaimer of an overlay.

    Parameters
    ----------
    key : OverlayKey
        Any overlay but ``"none"``.
    zone : str or None
        The viewer's hardiness zone; it only changes the zone layer's title.

    Raises
    ------
    ValueError
        For ``"none"``, which has no overlay.
    """
    if key == "none":
        raise ValueError("The report-count map has no overlay")
    if key == "zones":
        title = _zone_title(zone)
        return OverlayView(title, ZONE_SOURCE, ZONE_PERIOD, _ZONES_NOTE, ZONE_DISCLAIMER)
    source = NORMALS_SOURCE
    return OverlayView(OVERLAY_LABELS[key], source, NORMALS_PERIOD, _NORMALS_NOTE, None)


@dataclass(frozen=True)
class RegionalReference:
    """The viewer's region described by the packaged references (never a commenter's).

    Attributes
    ----------
    summary : str
        One line of trusted text: zones and the three statewide averages, or what is missing.
    sources : str
        The NOAA source (when averages are shown) and what the numbers are not ("" when nothing
        is shown). The zones' source is named by the disclaimer.
    disclaimer : str or None
        :data:`~crop_explorer.overlays.ZONE_DISCLAIMER` whenever a zone range is shown.

    The viewer shows ``disclaimer`` and ``sources`` together as :attr:`notes`.
    """

    summary: str
    sources: str
    disclaimer: str | None

    @property
    def notes(self) -> str:
        """The caption under the summary: the disclaimer first, then the sources."""
        return " ".join(part for part in (self.disclaimer, self.sources) if part)


_REFERENCE_SCOPE: Final = (
    "Background for the region you chose, not a reading from any report's garden; never used "
    "to sort or match reports."
)
_REFERENCE_AVERAGES: Final = (
    "Averages: NOAA NCEI Climate at a Glance (nClimDiv), whole-state statistics, not observed "
    "weather."
)


def regional_reference(code: str | None, refs: Overlays) -> RegionalReference | None:
    """Describe the viewer's chosen region with the packaged references.

    Parameters
    ----------
    code : str or None
        The viewer's Region choice (``"US-CT"``, ``"CA-ON"``, ``"GB"``), or ``None``.
    refs : Overlays
        The loaded references.

    Returns
    -------
    RegionalReference or None
        ``None`` without a region. For a U.S. state: "Connecticut — USDA zones 6a-7b (2023,
        simplified; largest area 6b); July average high 83°F, January average low 18°F,
        annual precipitation 49 in (NOAA 1991-2020 statewide averages)", saying what is
        missing (DC has no NOAA statewide series). Elsewhere: a line saying no reference is
        available there.
    """
    if code is None:
        return None
    name = REGION_NAMES[code]
    if not is_us_state(code):
        missing = f"No regional climate reference for {name}: these references cover U.S. states."
        return RegionalReference(missing, "", None)
    zones = _state_zone_phrase(refs.state_zones, code)
    climate = _state_normals_phrase(refs.normals, code, name)
    has_averages = not _state_rows(refs.normals, code).empty
    sources = f"{_REFERENCE_AVERAGES} {_REFERENCE_SCOPE}" if has_averages else _REFERENCE_SCOPE
    return RegionalReference(f"{name} — {zones}{climate}", sources, ZONE_DISCLAIMER)


def _state_rows(table: pd.DataFrame, code: str) -> pd.DataFrame:
    return table[table["region_code"] == code]


def _zone_span(rows: pd.DataFrame) -> str:
    coldest = str(rows.loc[rows["is_coldest"], "zone"].iloc[0])
    warmest = str(rows.loc[rows["is_warmest"], "zone"].iloc[0])
    return coldest if coldest == warmest else f"{coldest}-{warmest}"


def _largest_zone(rows: pd.DataFrame) -> str:
    """Return the half-zone covering most of the state (the colder one on a tie)."""
    return str(rows["zone"].iloc[int(rows["area_share"].to_numpy().argmax())])


def _state_zone_phrase(state_zones: pd.DataFrame, code: str) -> str:
    rows = _state_rows(state_zones, code)
    span, largest = _zone_span(rows), _largest_zone(rows)
    return f"USDA zones {span} (2023, simplified; largest area {largest})"


def _whole(value: float) -> int:
    """Round to a whole number, never showing ``-0``."""
    return round(value) + 0


def _normal_text(element: NormalElement, value: float) -> str:
    return f"{_SHORT[element]} {_whole(value)}{_UNITS[element]}"


def _state_normals_phrase(normals: pd.DataFrame, code: str, name: str) -> str:
    rows = _state_rows(normals, code)
    if rows.empty:
        return f". NOAA statewide averages are not available for {name}."
    values = dict(zip(rows["element"], rows["value"], strict=True))
    parts = [_normal_text(element, values[element]) for element in _elements()]
    return f"; {', '.join(parts)} (NOAA 1991-2020 statewide averages)"


def _elements() -> tuple[NormalElement, ...]:
    return get_args(NormalElement)


# --------------------------------------------------------------------------- tables


def _percent(share: float) -> str:
    return "<1%" if share < 0.005 else f"{round(share * 100)}%"


def _zone_shares(rows: pd.DataFrame) -> str:
    pairs = zip(rows["zone"], rows["area_share"], strict=True)
    return " · ".join(f"{zone} {_percent(float(share))}" for zone, share in pairs)


def zone_range_table(state_zones: pd.DataFrame) -> pd.DataFrame:
    """Return the zone layer's table twin: each state's half-zones, by state name.

    Parameters
    ----------
    state_zones : pandas.DataFrame
        :func:`crop_explorer.overlays.load_state_zone_ranges` output.

    Returns
    -------
    pandas.DataFrame
        Columns "State", "Zones" ("6a-7b"), "Largest area" and "Share of the state's area"
        ("6a 19% · 6b 65% · 7a 16% · 7b <1%"), one row per state and DC.
    """
    rows = [
        (REGION_NAMES[str(code)], _zone_span(group), _largest_zone(group), _zone_shares(group))
        for code, group in state_zones.groupby("region_code", sort=False)
    ]
    columns = ["State", "Zones", "Largest area", "Share of the state's area"]
    frame = pd.DataFrame(sorted(rows), columns=columns)
    return frame.reset_index(drop=True)


def zone_key_table(zones: ZoneLayer) -> pd.DataFrame:
    """Return the zone legend as a table: each half-zone's temperature band, cold to warm."""
    return pd.DataFrame(
        {
            "Zone": list(zones.table["zone"]),
            "Average coldest winter night (°F)": list(zones.table["temp_range_f"]),
        }
    )


def normals_table(normals: pd.DataFrame, element: NormalElement) -> pd.DataFrame:
    """Return a NOAA layer's table twin: the state and its 1991-2020 average, by state name."""
    rows = normals[normals["element"] == element]
    frame = pd.DataFrame(
        {
            "State": [REGION_NAMES[str(code)] for code in rows["region_code"]],
            _SCALE_TITLES[element]: [round(float(value), 1) for value in rows["value"]],
        }
    )
    return frame.sort_values("State").reset_index(drop=True)


def overlay_tables(key: OverlayKey, refs: Overlays) -> tuple[tuple[str, pd.DataFrame], ...]:
    """Return an overlay's table twin as titled tables (every value without hovering).

    The zone layer: the half-zones of each state, then the zone key (each half-zone's
    temperature band). A NOAA layer: each state's 1991-2020 average.

    Raises
    ------
    ValueError
        For ``"none"``, which has no overlay.
    """
    if key == "none":
        raise ValueError("The report-count map has no overlay")
    if key == "zones":
        states = ("Zones by state (2023, simplified)", zone_range_table(refs.state_zones))
        return (states, ("Zone key", zone_key_table(refs.zones)))
    title = f"{_SCALE_TITLES[key]}: statewide {NORMALS_PERIOD} averages (NOAA)"
    return ((title, normals_table(refs.normals, key)),)


# --------------------------------------------------------------------------- figures


def overlay_figure(
    key: OverlayKey,
    refs: Overlays,
    counts: RegionCounts,
    theme: ChartTheme,
    zone: str | None = None,
) -> go.Figure:
    """Draw one overlay with the report counts as labels (dispatches on ``key``).

    Raises
    ------
    ValueError
        For ``"none"``: the report-count map is :func:`crop_explorer.geography.choropleth`.
    """
    if key == "none":
        raise ValueError("The report-count map has no overlay")
    if key == "zones":
        return zone_figure(refs, counts, theme, emphasis=emphasis_zones(zone))
    return normals_figure(refs.normals, key, counts, theme)


def zone_figure(
    refs: Overlays, counts: RegionCounts, theme: ChartTheme, *, emphasis: Sequence[str] = ()
) -> go.Figure:
    """Draw the half-zone layer under the state outlines, with report counts as labels.

    Parameters
    ----------
    refs : Overlays
        The loaded references (zone polygons and per-state zone ranges).
    counts : RegionCounts
        Shown reports per state; only states with reports get a label.
    theme : ChartTheme
        Light or dark tokens.
    emphasis : Sequence[str]
        Half-zones to emphasise (:func:`emphasis_zones`): they keep their colour while every
        other zone is blended toward the surface, on the map and in the scale legend alike,
        whose tick for the emphasised zone is bold ("6b").

    Returns
    -------
    plotly.graph_objects.Figure
        Three traces: the zone ``Choropleth`` (the GeoJSON as loaded, ``featureidkey
        "properties.zone"``, a stepped semantic-heat colour scale with a horizontal scale
        legend, no hover), a transparent ``USA-states`` ``Choropleth`` drawing the outlines and
        carrying each state's hover text and click target, and the count labels.
    """
    ranges = refs.state_zones
    states = sorted(set(ranges["region_code"]))
    hover = [_zone_hover(code, ranges, counts) for code in states]
    traces = [
        _zone_trace(refs.zones, theme, tuple(emphasis)),
        _state_layer(states, hover, theme),
        _count_labels(counts, theme),
    ]
    figure = go.Figure(traces)
    figure.update_layout(**_layout(theme))
    return figure


def normals_figure(
    normals: pd.DataFrame, element: NormalElement, counts: RegionCounts, theme: ChartTheme
) -> go.Figure:
    """Draw one NOAA statewide average per state on the orange ramp, with report counts.

    Returns
    -------
    plotly.graph_objects.Figure
        A ``USA-states`` ``Choropleth`` (the fill, the outlines, hover and the click target)
        with a horizontal scale legend titled e.g. "July average high (°F)", then the count
        labels.

    Raises
    ------
    ValueError
        If ``element`` is not one of the three averages.
    """
    if element not in _SCALE_TITLES:
        raise ValueError(f"Unknown NOAA statewide average: {element!r}")
    rows = normals[normals["element"] == element]
    codes = [str(code) for code in rows["region_code"]]
    values = [float(value) for value in rows["value"]]
    hover = [_normals_hover(c, element, v, counts) for c, v in zip(codes, values, strict=True)]
    fill = _normals_trace(codes, values, hover, element, theme)
    figure = go.Figure([fill, _count_labels(counts, theme)])
    figure.update_layout(**_layout(theme))
    return figure


# --------------------------------------------------------------------------- figure helpers


def _stepped(colors: Sequence[str]) -> list[list[Any]]:
    """Return a colour scale with one flat band per colour (no blending between half-zones)."""
    n = len(colors)
    return [[edge / n, color] for i, color in enumerate(colors) for edge in (i, i + 1)]


def _smooth(colors: Sequence[str]) -> list[list[Any]]:
    last = len(colors) - 1
    return [[i / last, color] for i, color in enumerate(colors)]


def _colorbar(theme: ChartTheme, title: str, length: float) -> dict[str, Any]:
    font = {"family": FONT_FAMILY, "size": 12, "color": theme.text_secondary}
    return {
        **_COLORBAR,
        "len": length,
        "title": {"text": plotly_text(title), "side": "top", "font": font},
        "tickfont": font,
    }


def _zone_ticks(emphasis: Sequence[str]) -> tuple[list[float], list[str]]:
    """One tick per whole zone, centred on its two halves; the emphasised zone in bold."""
    ticks = {number: (2 * number - 1.5, str(number)) for number in range(1, 14)}
    if len(emphasis) == 1:
        (zone,) = emphasis
        ticks[int(zone[:-1])] = (float(zone_rank(zone)), f"<b>{zone}</b>")
    elif emphasis:
        number = int(emphasis[0][:-1])
        ticks[number] = (ticks[number][0], f"<b>{number}</b>")
    values, labels = zip(*ticks.values(), strict=True)
    return list(values), list(labels)


def _mix(color: str, surface: str, weight: float) -> str:
    """Blend ``color`` toward ``surface`` by ``weight`` in sRGB (``#rrggbb`` in and out)."""
    pairs = [(int(color[i : i + 2], 16), int(surface[i : i + 2], 16)) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(c + (s - c) * weight):02x}" for c, s in pairs)


def _zone_colors(theme: ChartTheme, emphasis: Sequence[str]) -> list[str]:
    """One colour per half-zone; with an emphasis, every other zone is muted toward the surface.

    Muting lives in the colour scale itself, so the scale legend shows the same emphasis as
    the map (Plotly applies a choropleth's opacity per trace, not per location).
    """
    ramp = _ZONE_RAMPS[theme.name]
    if not emphasis:
        return list(ramp)
    return [
        color if zone in emphasis else _mix(color, theme.surface, _MUTE)
        for zone, color in zip(HALF_ZONES, ramp, strict=True)
    ]


def _zone_trace(zones: ZoneLayer, theme: ChartTheme, emphasis: tuple[str, ...]) -> go.Choropleth:
    """Return the half-zone fill: a stepped colour scale, each zone outlined in its own colour.

    The same-colour outline closes the anti-aliasing seams between neighbouring zones.
    """
    order = [str(zone) for zone in zones.table["zone"]]
    colors = _zone_colors(theme, emphasis)
    return go.Choropleth(
        geojson=zones.geojson,
        featureidkey="properties.zone",
        locations=order,
        z=[zone_rank(zone) for zone in order],
        zmin=-0.5,
        zmax=len(HALF_ZONES) - 0.5,
        colorscale=_stepped(colors),
        colorbar=_zone_colorbar(theme, emphasis),
        marker={"line": {"color": [colors[zone_rank(z)] for z in order], "width": _SEAM_PX}},
        hoverinfo="skip",
    )


def _zone_colorbar(theme: ChartTheme, emphasis: Sequence[str]) -> dict[str, Any]:
    tickvals, ticktext = _zone_ticks(emphasis)
    colorbar = _colorbar(theme, _ZONE_SCALE_TITLE, 0.95)
    return {**colorbar, "tickvals": tickvals, "ticktext": ticktext, "ticks": ""}


def _outline(theme: ChartTheme) -> dict[str, Any]:
    """State outlines in the surface colour: a gap between fills, not an ink border."""
    return {"line": {"color": theme.surface, "width": _OUTLINE_PX}}


def _state_layer(codes: Sequence[str], hover: Sequence[str], theme: ChartTheme) -> go.Choropleth:
    """Transparent states over the zones: outlines, hover text and the click target."""
    return go.Choropleth(
        locationmode="USA-states",
        locations=[us_postal(code) for code in codes],
        z=[0] * len(codes),
        colorscale=[[0, _TRANSPARENT], [1, _TRANSPARENT]],
        showscale=False,
        marker=_outline(theme),
        text=list(hover),
        hovertemplate=_HOVER,
    )


def _reports(code: str, counts: RegionCounts) -> str:
    rows = counts.table[counts.table["region_code"] == code]
    if rows.empty:
        return "no shown reports naming this state"
    n, suppressed = int(rows["n_comments"].iloc[0]), bool(rows["suppressed"].iloc[0])
    label = count_label(n, suppressed=suppressed, min_cell_count=counts.min_cell_count)
    return f"{label} naming this state"


def _lines(*lines: str) -> str:
    """Return hover text: each line escaped for Plotly, joined by Plotly line breaks."""
    return "<br>".join(plotly_text(line) for line in lines)


def _zone_hover(code: str, ranges: pd.DataFrame, counts: RegionCounts) -> str:
    zones = f"USDA zones {_zone_span(_state_rows(ranges, code))} (2023, simplified)"
    return _lines(US_STATES[code], zones, _reports(code, counts))


def _normals_hover(code: str, element: NormalElement, value: float, counts: RegionCounts) -> str:
    average = f"{_SHORT[element].capitalize()}: {value:.1f}{_UNITS[element]}"
    period = "statewide 1991-2020 average (NOAA)"
    return _lines(US_STATES[code], average, period, _reports(code, counts))


def _normals_trace(
    codes: Sequence[str],
    values: Sequence[float],
    hover: Sequence[str],
    element: NormalElement,
    theme: ChartTheme,
) -> go.Choropleth:
    return go.Choropleth(
        locationmode="USA-states",
        locations=[us_postal(code) for code in codes],
        z=list(values),
        zmin=min(values),
        zmax=max(values),
        colorscale=_smooth(_NORMALS_RAMPS[theme.name]),
        colorbar=_colorbar(theme, _SCALE_TITLES[element], 0.7),
        marker=_outline(theme),
        text=list(hover),
        hovertemplate=_HOVER,
    )


def _count_text(n_comments: int, suppressed: bool, min_cell_count: int) -> str:
    return f"<{min_cell_count}" if suppressed else str(n_comments)


def _halo(theme: ChartTheme) -> str:
    """Return a surface-coloured text halo, so a count reads on any fill."""
    return ", ".join(f"{x}px {y}px 2px {theme.surface}" for x in (-1, 1) for y in (-1, 1))


def _count_labels(counts: RegionCounts, theme: ChartTheme) -> go.Scattergeo:
    """One aggregate count per state with reports, placed by Plotly from the state's code.

    Text only (no lat/lon, no marker), never hovered or clicked: the state under it is.
    """
    table = counts.table
    flags = zip(table["n_comments"], table["suppressed"], strict=True)
    text = [_count_text(int(n), bool(s), counts.min_cell_count) for n, s in flags]
    font = {"family": FONT_FAMILY, "size": _LABEL_PX, "color": theme.text_primary}
    return go.Scattergeo(
        locationmode="USA-states",
        locations=list(table["postal"]),
        text=[plotly_text(label) for label in text],
        mode="text",
        textfont={**font, "weight": 600, "shadow": _halo(theme)},
        hoverinfo="skip",
        showlegend=False,
    )


def _layout(theme: ChartTheme) -> dict[str, Any]:
    """Return static, transparent chrome: the U.S. only, no zoom or drag, themed text."""
    font = {"family": FONT_FAMILY, "size": _FONT_PX, "color": theme.text_secondary}
    return {
        "height": MAP_HEIGHT,
        "margin": {"l": 0, "r": 0, "t": 0, "b": 0},
        "geo": dict(_GEO),
        "paper_bgcolor": _TRANSPARENT,
        "font": font,
        "hoverlabel": {
            "bgcolor": theme.surface,
            "bordercolor": theme.gridline,
            "font": {**font, "color": theme.text_primary},
        },
        "dragmode": False,
        "showlegend": False,
    }
