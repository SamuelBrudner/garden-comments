"""Plotly figures for the viewer's "At a glance" header (pure: tidy data in, Figure out).

The form follows the data's job. Comparing how many comments name each variety is magnitude
across nominal categories: horizontal bars in **one** color (categorical slot 1), the value at
each bar tip and no legend (a single series). Which topics come up for which variety is a grid
of magnitudes: a heatmap on the sequential blue ramp with a "Comments" scale legend. How people
say they grow is two small single-color bar facets (growing method, setting) on one scale.

Colors come from one :class:`ChartTheme` per Streamlit theme type. Both are selected from the
same validated ramps (the dataviz skill's reference palette), not inverted automatically: in
dark mode the heatmap's "near zero" step is the one that recedes into the dark surface. Labels,
values and legends use text tokens, never the series color. Marks are thin (bars at most
:data:`MAX_BAR_PX` thick with :data:`CORNER_PX` rounded data ends), backgrounds transparent,
and every figure is static: no zoom, drag or mode bar.

Plotly renders a subset of HTML (``<a href>``, ``<b>``, ``<br>``, ``<span style>``) inside tick
labels, bar text and hover labels, so **every comment-derived string placed in a figure goes
through** :func:`plotly_text` first, including the variety keys in ``customdata`` (the viewer
unescapes the click data with :func:`html.unescape`).

Hover and click targets are whole rows and whole cells: an invisible layer under the bars
(full width, full band) and an invisible point per heatmap cell carry the hover text and the
click data, so one hover box shows per mark and a short bar is as easy to click as a long one.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from crop_explorer.overview import SETUP_COLUMNS, SETUP_KEYS, TOPIC_COLUMNS, VARIETY_COLUMNS
from crop_explorer.schemas import TOPICS

__all__ = [
    "BAR_PX",
    "CELL_GAP_PX",
    "CHART_CONFIG",
    "CORNER_PX",
    "DARK",
    "FONT_FAMILY",
    "LIGHT",
    "MAX_BAR_PX",
    "MAX_LABEL_CHARS",
    "ROW_PX",
    "SEQUENTIAL_BLUE",
    "SETUP_TITLES",
    "TOPIC_TITLES",
    "ChartTheme",
    "plotly_text",
    "setups_bar",
    "short_label",
    "theme_for",
    "topics_heatmap",
    "varieties_bar",
    "variety_name",
]

# --------------------------------------------------------------------------- tokens

SEQUENTIAL_BLUE: Final = (
    "#cde2fb",
    "#b7d3f6",
    "#9ec5f4",
    "#86b6ef",
    "#6da7ec",
    "#5598e7",
    "#3987e5",
    "#2a78d6",
    "#256abf",
    "#1c5cab",
    "#184f95",
    "#104281",
    "#0d366b",
)
"""The one-hue sequential ramp, steps 100 to 700 (lightest to darkest)."""
FONT_FAMILY: Final = '"Source Sans", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'
"""Streamlit's own sans face first, so the header reads as one system with the page; the
system sans when it is not loaded."""


@dataclass(frozen=True)
class ChartTheme:
    """The color tokens of one Streamlit theme type, defined once for every chart and tile.

    Attributes
    ----------
    name : {"light", "dark"}
        The Streamlit theme type these tokens are for.
    series : str
        Categorical slot 1: the one color of every bar and of the meter fill.
    ramp : tuple[str, ...]
        Sequential steps from "no comments" (the step nearest the surface) to "most comments".
    meter_track : str
        The meter's unfilled track, a quieter step of the same blue ramp.
    text_primary, text_secondary, text_muted : str
        Text ink: values, labels, and notes.
    gridline : str
        Hairline rules (the hover label border).
    baseline : str
        The axis line bars grow from.
    surface : str
        Chart surface (hover label background).
    border : str
        Hairline ring around a stat tile.
    """

    name: Literal["light", "dark"]
    series: str
    ramp: tuple[str, ...]
    meter_track: str
    text_primary: str
    text_secondary: str
    text_muted: str
    gridline: str
    baseline: str
    surface: str
    border: str


LIGHT: Final = ChartTheme(
    name="light",
    series="#2a78d6",
    ramp=SEQUENTIAL_BLUE,
    meter_track="#cde2fb",
    text_primary="#0b0b0b",
    text_secondary="#52514e",
    text_muted="#898781",
    gridline="#e1e0d9",
    baseline="#c3c2b7",
    surface="#fcfcfb",
    border="rgba(11,11,11,0.10)",
)
DARK: Final = ChartTheme(
    name="dark",
    series="#3987e5",
    ramp=tuple(reversed(SEQUENTIAL_BLUE[1:])),
    meter_track="#184f95",
    text_primary="#f0efec",
    text_secondary="#c3c2b7",
    text_muted="#898781",
    gridline="#2c2c2a",
    baseline="#383835",
    surface="#1a1a19",
    border="rgba(255,255,255,0.10)",
)
"""Dark tokens: the dark-surface steps of the same hues. The heatmap ramp runs from step 700
(recedes into the dark surface: no comments) to step 150 (most comments)."""

_THEMES: Final = MappingProxyType({None: LIGHT, "light": LIGHT, "dark": DARK})

# --------------------------------------------------------------------------- geometry

ROW_PX: Final = 32
"""Height of one bar's band."""
BAR_PX: Final = 20
"""Bar thickness: the rest of the band is air."""
MAX_BAR_PX: Final = 24
CORNER_PX: Final = 4
"""Rounded data end of every bar (the baseline end stays square)."""
CELL_GAP_PX: Final = 2
"""Surface gap between heatmap cells."""
MAX_LABEL_CHARS: Final = 24
"""Longest tick label (characters), so names fit beside the bars on a phone."""
_MIN_NAME_CHARS: Final = 8
_EDGE_PX: Final = 8
_HEATMAP_ROW_PX: Final = 30
_HEATMAP_TOP_PX: Final = 90
"""Room above the grid for the horizontal "Comments" scale legend."""
_HEATMAP_BOTTOM_PX: Final = 104
"""Room below the grid for the topic labels, rotated so they never overlap."""
_TOPIC_TICK_ANGLE: Final = -45
_FACET_TITLE_PX: Final = 28
_FACET_GAP_PX: Final = 16
_VALUE_HEADROOM: Final = 1.22
"""The value axis runs to this multiple of the largest count, leaving room for tip labels."""
_FONT_PX: Final = 13
_MAX_SCALE_TICKS: Final = 5
_TRANSPARENT: Final = "rgba(0,0,0,0)"
_HOVER: Final = "%{hovertext}<extra></extra>"
_STEADY: Final = MappingProxyType({"marker": {"opacity": 1}})
"""Selected and unselected bars look the same: a click searches, it does not grey others out."""
_HIDDEN: Final = MappingProxyType({"marker": {"opacity": 0}})

CHART_CONFIG: Final = MappingProxyType(
    {"displayModeBar": False, "scrollZoom": False, "doubleClick": False, "showTips": False}
)
"""Plotly config for every "At a glance" chart: no mode bar, no zoom, no double-click reset."""

TOPIC_TITLES: Final[Mapping[str, str]] = MappingProxyType(
    {topic: topic.replace("_", " ").capitalize() for topic in TOPICS}
)
"""Human topic labels: ``weather_tolerance`` -> "Weather tolerance"."""
SETUP_TITLES: Final = MappingProxyType({"method": "Growing method", "shelter": "Setting"})
"""Facet titles of :func:`setups_bar`, matching the viewer's condition labels."""

_BAR_STATIC: Final = MappingProxyType(
    {
        "orientation": "h",
        "textposition": "outside",
        "constraintext": "none",
        "cliponaxis": False,
        "hoverinfo": "skip",
        "selected": _STEADY,
        "unselected": _STEADY,
    }
)
"""Painted-bar settings that never change: values outside the tip, never shrunk or clipped;
hover and clicks go to the row-target layer."""
_ROW_TARGETS: Final = MappingProxyType(
    {
        "orientation": "h",
        "width": 1,
        "marker": {"opacity": 0},
        "hovertemplate": _HOVER,
        "selected": _HIDDEN,
        "unselected": _HIDDEN,
    }
)
"""The invisible layer under the bars: one full-width, full-band bar per row."""
_CATEGORY_AXIS: Final = MappingProxyType(
    {
        "tickmode": "array",
        "autorange": "reversed",
        "ticks": "",
        "ticklabelstandoff": 6,
        "showgrid": False,
        "zeroline": False,
        "linewidth": 1,
        "fixedrange": True,
        "automargin": True,
    }
)
"""A category axis drawn top-down from positions ``0..n-1`` (so equal names never merge)."""
_COLORBAR: Final = MappingProxyType(
    {
        "orientation": "h",
        "thickness": 10,
        "len": 220,
        "lenmode": "pixels",
        "x": 0,
        "xref": "paper",
        "xanchor": "left",
        "y": 1,
        "yref": "paper",
        "yanchor": "bottom",
        "ypad": 6,
        "outlinewidth": 0,
    }
)
"""The scale legend: horizontal, in the band above the grid, left-aligned."""
_CELL_TARGETS: Final = MappingProxyType(
    {
        "mode": "markers",
        "marker": {"size": 24, "opacity": 0},
        "hovertemplate": _HOVER,
        "selected": _HIDDEN,
        "unselected": _HIDDEN,
    }
)
"""The invisible click layer over the heatmap: hover and click, never painted."""

_PLOTLY_ESCAPES: Final = MappingProxyType(
    {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&#34;", "'": "&#39;"}
)
"""Plotly.js decodes ``&amp;``, ``&lt;``, ``&gt;`` and numeric entities (not ``&quot;``)."""


# --------------------------------------------------------------------------- public helpers


def theme_for(theme_type: str | None) -> ChartTheme:
    """Return the tokens for Streamlit's theme type (``st.context.theme.type``).

    Parameters
    ----------
    theme_type : {"light", "dark"} or None
        ``None`` (not reported yet, e.g. in tests) means light.

    Raises
    ------
    ValueError
        For any other value.
    """
    if theme_type not in _THEMES:
        raise ValueError(f"Unknown Streamlit theme type: {theme_type!r}")
    return _THEMES[theme_type]


def plotly_text(text: str) -> str:
    """Escape untrusted text so Plotly displays it literally and never renders it as markup.

    ``&``, ``<``, ``>``, ``"`` and ``'`` become entities Plotly decodes back for display
    (``&#34;`` and ``&#39;`` for quotes, since Plotly does not know ``&quot;``), so no tag or
    link can form. :func:`html.unescape` restores the original text.
    """
    return "".join(_PLOTLY_ESCAPES.get(char, char) for char in text)


def variety_name(label: str, crop: str | None) -> str:
    """Return a variety's display name, ``"Sungold · tomato"`` (raw: escape it for Plotly)."""
    return label if crop is None else f"{label} · {crop}"


def short_label(label: str, crop: str | None) -> str:
    """Return a tick label of at most about :data:`MAX_LABEL_CHARS` characters.

    A long variety name is cut and ends in "…" (the crop is kept); the hover text and the
    table keep the full name. Raw text: shorten first, then escape with :func:`plotly_text`,
    so an entity is never cut in half.
    """
    suffix = "" if crop is None else f" · {crop}"
    if len(label) + len(suffix) <= MAX_LABEL_CHARS:
        return label + suffix
    room = max(MAX_LABEL_CHARS - len(suffix), _MIN_NAME_CHARS)
    return f"{label[: room - 1].rstrip()}…{suffix}"


# --------------------------------------------------------------------------- varieties


def varieties_bar(table: pd.DataFrame, theme: ChartTheme) -> go.Figure:
    """Draw the most-discussed varieties as one horizontal single-color bar series.

    Parameters
    ----------
    table : pandas.DataFrame
        :func:`crop_explorer.overview.variety_counts` output (columns
        :data:`~crop_explorer.overview.VARIETY_COLUMNS`), most-discussed first.
    theme : ChartTheme
        Light or dark tokens.

    Returns
    -------
    plotly.graph_objects.Figure
        An invisible row-target ``Bar`` layer (the whole row is the hover and click target:
        hover "Sungold · tomato: 10 comments", ``customdata`` rows ``[variety_key]``, escaped)
        under one painted ``Bar`` series, largest at the top, labelled "Sungold · tomato", its
        count at the tip.

    Raises
    ------
    ValueError
        If the table has other columns or no rows.
    """
    _require(table, VARIETY_COLUMNS, "varieties_bar")
    pairs = [(str(label), _crop(crop)) for label, crop in _pairs(table, "label", "crop")]
    counts = [int(n) for n in table["n_comments"]]
    layer = _row_targets([variety_name(*pair) for pair in pairs], counts, max(counts))
    layer.customdata = [[plotly_text(str(key))] for key in table["variety_key"]]
    ticks = [short_label(*pair) for pair in pairs]
    figure = go.Figure([layer, _bar(counts, theme)])
    figure.update_layout(**_bar_layout(theme, len(counts)), **_bar_axes(theme, ticks, max(counts)))
    return figure


# --------------------------------------------------------------------------- topics


@dataclass(frozen=True)
class _Grid:
    """A complete variety x topic grid in display order."""

    keys: tuple[str, ...]
    labels: tuple[str, ...]
    topics: tuple[str, ...]
    z: tuple[tuple[int, ...], ...]


def topics_heatmap(matrix: pd.DataFrame, theme: ChartTheme) -> go.Figure:
    """Draw comments per variety and topic as a sequential-blue heatmap with a scale legend.

    Parameters
    ----------
    matrix : pandas.DataFrame
        :func:`crop_explorer.overview.topic_matrix` output: every (variety, topic) cell, rows
        in variety order, topics in column order.
    theme : ChartTheme
        Light or dark tokens.

    Returns
    -------
    plotly.graph_objects.Figure
        A ``Heatmap`` (rows top-down in the given order, columns labelled "Weather tolerance"
        etc., zero cells in the ramp's first step, 2px cell gaps, a horizontal "Comments"
        colorbar) under an invisible ``Scatter`` with one point per cell that carries hover
        ("Sungold · Flavor: 4 comments") and click data (``[variety_key, topic]``, key
        escaped): Plotly cannot select heatmap cells, but it can select scatter points.

    Raises
    ------
    ValueError
        If the matrix has other columns, no rows, or is missing a (variety, topic) cell.
    """
    _require(matrix, TOPIC_COLUMNS, "topics_heatmap")
    grid = _grid(matrix)
    figure = go.Figure([_heatmap(grid, theme), _cell_targets(grid)])
    figure.update_layout(**_heatmap_layout(theme, grid))
    return figure


# --------------------------------------------------------------------------- setups


def setups_bar(table: pd.DataFrame, theme: ChartTheme) -> go.Figure:
    """Draw growing methods and settings as two single-color bar facets on one scale.

    Parameters
    ----------
    table : pandas.DataFrame
        :func:`crop_explorer.overview.setup_counts` output.
    theme : ChartTheme
        Light or dark tokens.

    Returns
    -------
    plotly.graph_objects.Figure
        One horizontal ``Bar`` facet per key that has rows ("Growing method", then
        "Setting"), sharing the value scale and the bar thickness, counts at the tips, each
        over an invisible row-target layer carrying the hover text (no click data).

    Raises
    ------
    ValueError
        If the table has other columns or no rows.
    """
    _require(table, SETUP_COLUMNS, "setups_bar")
    facets = [
        (key, table[table["key"] == key]) for key in SETUP_KEYS if (table["key"] == key).any()
    ]
    figure = _facet_figure([len(rows) for _, rows in facets], [SETUP_TITLES[k] for k, _ in facets])
    largest = int(table["n_comments"].max())
    for row, (_, rows) in enumerate(facets, start=1):
        _add_facet(figure, row, rows, theme, largest)
    figure.update_layout(**_bar_layout(theme, len(table), n_facets=len(facets)))
    figure.update_annotations(_facet_title(theme))
    return figure


# --------------------------------------------------------------------------- shared helpers


def _require(table: pd.DataFrame, columns: Sequence[str], what: str) -> None:
    if tuple(table.columns) != tuple(columns):
        raise ValueError(f"{what} needs columns {tuple(columns)}, got {tuple(table.columns)}")
    if table.empty:
        raise ValueError(f"{what} got a table with no rows; show a note instead of a chart")


def _plain(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deep, mutable copy of constant settings (Plotly may write into them)."""
    return {
        key: _plain(value) if isinstance(value, Mapping) else value
        for key, value in settings.items()
    }


def _pairs(table: pd.DataFrame, first: str, second: str) -> list[tuple[Any, Any]]:
    return list(zip(table[first], table[second], strict=True))


def _crop(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _comments(n_comments: int) -> str:
    return f"{n_comments} comment" if n_comments == 1 else f"{n_comments} comments"


def _layout(theme: ChartTheme, height: int, margin: Mapping[str, int]) -> dict[str, Any]:
    """Return the static, transparent chrome every "At a glance" figure shares."""
    return {
        "height": height,
        "margin": {**margin, "pad": 0},
        "template": "none",
        "paper_bgcolor": _TRANSPARENT,
        "plot_bgcolor": _TRANSPARENT,
        "font": {"family": FONT_FAMILY, "size": _FONT_PX, "color": theme.text_secondary},
        "hoverlabel": {
            "bgcolor": theme.surface,
            "bordercolor": theme.gridline,
            "font": {"family": FONT_FAMILY, "size": _FONT_PX, "color": theme.text_primary},
        },
        "dragmode": False,
        "showlegend": False,
    }


def _bar(counts: Sequence[int], theme: ChartTheme) -> go.Bar:
    """Return one painted single-color horizontal bar series with values at the tips."""
    return go.Bar(
        x=list(counts),
        y=list(range(len(counts))),
        marker={"color": theme.series, "cornerradius": CORNER_PX, "line": {"width": 0}},
        text=[str(n) for n in counts],
        textfont={"family": FONT_FAMILY, "size": _FONT_PX, "color": theme.text_primary},
        **_plain(_BAR_STATIC),
    )


def _row_targets(names: Sequence[str], counts: Sequence[int], largest: int) -> go.Bar:
    """Return the invisible full-row layer carrying each row's (escaped) hover text.

    Each row's target runs the whole value axis, which ends at :func:`_reach` of ``largest``
    (the figure's largest count).
    """
    hover = [f"{name}: {_comments(n)}" for name, n in zip(names, counts, strict=True)]
    return go.Bar(
        x=[_reach(largest)] * len(counts),
        y=list(range(len(counts))),
        hovertext=[plotly_text(text) for text in hover],
        **_plain(_ROW_TARGETS),
    )


def _reach(largest: int) -> float:
    """Return the end of the value axis: the largest count plus room for its tip label."""
    return largest * _VALUE_HEADROOM


def _bar_layout(theme: ChartTheme, n_bars: int, *, n_facets: int = 0) -> dict[str, Any]:
    """Size a bar figure exactly: one band per bar, plus facet titles and edges."""
    spacing = max(0, n_facets - 1) * (_FACET_TITLE_PX + _FACET_GAP_PX)
    top = _EDGE_PX + (_FACET_TITLE_PX if n_facets else 0)
    margin = {"l": _EDGE_PX, "r": _EDGE_PX, "t": top, "b": _EDGE_PX}
    height = n_bars * ROW_PX + spacing + top + _EDGE_PX
    return {
        **_layout(theme, height, margin),
        "bargap": 1 - BAR_PX / ROW_PX,
        "barmode": "overlay",
        "hovermode": "closest",
    }


def _category_axis(theme: ChartTheme, names: Sequence[str]) -> dict[str, Any]:
    """Return an axis with escaped names as tick labels, top-down, on a hairline baseline."""
    return {
        **_plain(_CATEGORY_AXIS),
        "tickvals": list(range(len(names))),
        "ticktext": [plotly_text(name) for name in names],
        "tickfont": {"family": FONT_FAMILY, "size": _FONT_PX, "color": theme.text_secondary},
        "showline": True,
        "linecolor": theme.baseline,
    }


def _value_axis(largest: int) -> dict[str, Any]:
    """Return a hidden value axis from the baseline: every bar carries its own value."""
    return {"visible": False, "range": [0, _reach(largest)], "fixedrange": True}


def _bar_axes(theme: ChartTheme, names: Sequence[str], largest: int) -> dict[str, Any]:
    return {"yaxis": _category_axis(theme, names), "xaxis": _value_axis(largest)}


def _grid(matrix: pd.DataFrame) -> _Grid:
    keys = list(dict.fromkeys(str(key) for key in matrix["variety_key"]))
    topics = list(dict.fromkeys(str(topic) for topic in matrix["topic"]))
    cells = {
        (str(key), str(topic)): int(n)
        for key, topic, n in zip(
            matrix["variety_key"], matrix["topic"], matrix["n_comments"], strict=True
        )
    }
    if len(cells) != len(matrix) or len(cells) != len(keys) * len(topics):
        raise ValueError("topics_heatmap needs exactly one cell for every variety and topic")
    labels = dict(_pairs(matrix, "variety_key", "label"))
    z = tuple(tuple(cells[(key, topic)] for topic in topics) for key in keys)
    return _Grid(tuple(keys), tuple(str(labels[key]) for key in keys), tuple(topics), z)


def _colorscale(ramp: Sequence[str]) -> list[list[Any]]:
    """Evenly spaced stops over the ramp: 0 is its first step, 1 its last."""
    last = len(ramp) - 1
    return [[index / last, color] for index, color in enumerate(ramp)]


def _scale_dtick(largest: int) -> int:
    return max(1, math.ceil(largest / _MAX_SCALE_TICKS))


def _colorbar(theme: ChartTheme, largest: int) -> dict[str, Any]:
    """Return a horizontal scale legend titled "Comments", with whole-number ticks."""
    return {
        **_plain(_COLORBAR),
        "title": {"text": "Comments", "side": "top", "font": {"color": theme.text_secondary}},
        "tickfont": {"family": FONT_FAMILY, "size": 12, "color": theme.text_secondary},
        "dtick": _scale_dtick(largest),
    }


def _heatmap(grid: _Grid, theme: ChartTheme) -> go.Heatmap:
    largest = max(max(row) for row in grid.z)
    return go.Heatmap(
        z=[list(row) for row in grid.z],
        x=list(range(len(grid.topics))),
        y=list(range(len(grid.labels))),
        zmin=0,
        zmax=max(1, largest),
        colorscale=_colorscale(theme.ramp),
        colorbar=_colorbar(theme, largest),
        xgap=CELL_GAP_PX,
        ygap=CELL_GAP_PX,
        hoverinfo="skip",
    )


def _cell_targets(grid: _Grid) -> go.Scatter:
    """Return one invisible point per cell, carrying the cell's hover text and click data."""
    cells = [
        (column, row, label, topic, grid.z[row][column])
        for row, label in enumerate(grid.labels)
        for column, topic in enumerate(grid.topics)
    ]
    hover = [f"{label} · {TOPIC_TITLES[topic]}: {_comments(n)}" for _, _, label, topic, n in cells]
    return go.Scatter(
        x=[cell[0] for cell in cells],
        y=[cell[1] for cell in cells],
        hovertext=[plotly_text(text) for text in hover],
        customdata=[[plotly_text(grid.keys[row]), topic] for _, row, _, topic, _ in cells],
        **_plain(_CELL_TARGETS),
    )


def _topic_axis(theme: ChartTheme, topics: Sequence[str]) -> dict[str, Any]:
    return {
        "tickmode": "array",
        "tickvals": list(range(len(topics))),
        "ticktext": [TOPIC_TITLES[topic] for topic in topics],
        "tickangle": _TOPIC_TICK_ANGLE,
        "tickfont": {"family": FONT_FAMILY, "size": 12, "color": theme.text_secondary},
        "ticks": "",
        "showgrid": False,
        "zeroline": False,
        "fixedrange": True,
        "automargin": True,
    }


def _heatmap_layout(theme: ChartTheme, grid: _Grid) -> dict[str, Any]:
    margin = {"l": _EDGE_PX, "r": _EDGE_PX, "t": _HEATMAP_TOP_PX, "b": _HEATMAP_BOTTOM_PX}
    height = len(grid.labels) * _HEATMAP_ROW_PX + margin["t"] + margin["b"]
    ticks = [short_label(label, None) for label in grid.labels]
    rows = {**_category_axis(theme, ticks), "showline": False}
    return {
        **_layout(theme, height, margin),
        "xaxis": _topic_axis(theme, grid.topics),
        "yaxis": rows,
        "hovermode": "closest",
        "hoverdistance": -1,
    }


def _facet_figure(rows_per_facet: Sequence[int], titles: Sequence[str]) -> go.Figure:
    """Stack one subplot per facet; each subplot's height is its number of bar rows."""
    gap = _FACET_TITLE_PX + _FACET_GAP_PX
    plot_height = sum(rows_per_facet) * ROW_PX + (len(titles) - 1) * gap
    return make_subplots(
        rows=len(titles),
        cols=1,
        shared_xaxes=True,
        row_heights=list(rows_per_facet),
        vertical_spacing=gap / plot_height if len(titles) > 1 else 0,
        subplot_titles=list(titles),
    )


def _add_facet(
    figure: go.Figure, row: int, rows: pd.DataFrame, theme: ChartTheme, largest: int
) -> None:
    names = [str(label) for label in rows["label"]]
    counts = [int(n) for n in rows["n_comments"]]
    figure.add_trace(_row_targets(names, counts, largest), row=row, col=1)
    figure.add_trace(_bar(counts, theme), row=row, col=1)
    figure.update_yaxes(_category_axis(theme, names), row=row, col=1)
    figure.update_xaxes(_value_axis(largest), row=row, col=1)


def _facet_title(theme: ChartTheme) -> dict[str, Any]:
    """Facet titles sit left-aligned above their bars, in primary ink."""
    return {
        "x": 0,
        "xanchor": "left",
        "yanchor": "bottom",
        "yshift": 4,
        "font": {
            "family": FONT_FAMILY,
            "size": _FONT_PX,
            "color": theme.text_primary,
            "weight": 600,
        },
    }
