"""Tests for the "At a glance" figures (``crop_explorer.overview_charts``).

Checks the form and marks the dataviz rules ask for: one horizontal single-color bar series
with values at the bar tips and no legend; a heatmap on the sequential blue ramp with a
"Comments" scale legend and 2px cell gaps; two single-color facets for growing method and
setting; text in text tokens (never the series color); light and dark tokens selected from the
same ramps; no zoom, drag or mode bar; no coordinates; click data (``customdata``) on every
clickable mark. Every comment-derived string must be escaped before Plotly sees it, because
Plotly renders ``<a>``, ``<b>``, ``<br>`` and ``<span>`` inside text.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterator
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import pytest
from hypothesis import given
from hypothesis import strategies as st

from crop_explorer.overview import SETUP_COLUMNS, TOPIC_COLUMNS, VARIETY_COLUMNS
from crop_explorer.overview_charts import (
    BAR_PX,
    CELL_GAP_PX,
    CORNER_PX,
    DARK,
    LIGHT,
    MAX_BAR_PX,
    MAX_LABEL_CHARS,
    ROW_PX,
    SEQUENTIAL_BLUE,
    TOPIC_TITLES,
    ChartTheme,
    plotly_text,
    setups_bar,
    short_label,
    theme_for,
    topics_heatmap,
    varieties_bar,
    variety_name,
)

EVIL = '<a href="https://evil.example">x</a><b>y'
THEMES = pytest.mark.parametrize("theme", [LIGHT, DARK], ids=["light", "dark"])


def varieties(*rows: tuple[str, str, str | None, int]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=list(VARIETY_COLUMNS))
    return frame.astype({"crop": "object", "n_comments": "int64"})


def matrix(*rows: tuple[str, str, str, int]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=list(TOPIC_COLUMNS)).astype({"n_comments": "int64"})


def setups(*rows: tuple[str, str, str, int]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=list(SETUP_COLUMNS)).astype({"n_comments": "int64"})


TOP = varieties(
    ("sungold", "Sungold", "tomato", 10),
    ("juliet", "Juliet", "tomato", 5),
    ("mystery", "Mystery", None, 1),
)
GRID = matrix(
    ("sungold", "Sungold", "flavor", 4),
    ("sungold", "Sungold", "weather_tolerance", 1),
    ("juliet", "Juliet", "flavor", 0),
    ("juliet", "Juliet", "weather_tolerance", 2),
)
SETUPS = setups(
    ("method", "container", "Containers", 8),
    ("method", "in_ground", "In the ground", 3),
    ("shelter", "greenhouse", "Greenhouse / tunnel", 5),
)


def strings(value: Any) -> Iterator[str]:
    """Yield every string anywhere inside a JSON-like value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def figure_json(figure: go.Figure) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(figure.to_json())
    return parsed


def bars(figure: go.Figure) -> list[Any]:
    """The painted bar series (the invisible row-target layers left out)."""
    return [trace for trace in figure.data if trace.type == "bar" and trace.hoverinfo == "skip"]


def targets(figure: go.Figure) -> list[Any]:
    """The invisible row-target layers that carry hover and click data."""
    return [trace for trace in figure.data if trace.type == "bar" and trace.hoverinfo != "skip"]


def displayed(figure: go.Figure) -> list[str]:
    """Every string a reader can see: titles, tick labels, bar text and hover text."""
    found = [str(a.text) for a in figure.layout.annotations]
    for name in ("xaxis", "yaxis", "xaxis2", "yaxis2"):
        if name in figure.layout and figure.layout[name].ticktext:
            found += [str(t) for t in figure.layout[name].ticktext]
    for trace in figure.data:
        for field in ("text", "hovertext"):
            found += list(strings(json.loads(json.dumps(list(trace[field] or ())))))
        if trace.type == "heatmap" and trace.colorbar.title.text:
            found.append(str(trace.colorbar.title.text))
    return found


# --------------------------------------------------------------------------- themes


class TestThemes:
    @pytest.mark.parametrize(("kind", "theme"), [("dark", DARK), ("light", LIGHT), (None, LIGHT)])
    def test_theme_follows_streamlits_theme_type(
        self, kind: str | None, theme: ChartTheme
    ) -> None:
        assert theme_for(kind) is theme

    def test_an_unknown_theme_type_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="theme type"):
            theme_for("sepia")

    def test_tokens_come_from_the_palette(self) -> None:
        assert (LIGHT.series, DARK.series) == ("#2a78d6", "#3987e5")
        assert (LIGHT.text_primary, LIGHT.text_secondary, LIGHT.text_muted) == (
            "#0b0b0b",
            "#52514e",
            "#898781",
        )
        assert (DARK.text_primary, DARK.text_secondary, DARK.text_muted) == (
            "#f0efec",
            "#c3c2b7",
            "#898781",
        )
        assert (LIGHT.gridline, DARK.gridline) == ("#e1e0d9", "#2c2c2a")

    def test_sequential_ramps_are_selected_from_the_blue_ramp(self) -> None:
        assert len(SEQUENTIAL_BLUE) == 13
        assert LIGHT.ramp == SEQUENTIAL_BLUE  # light -> dark = more
        assert set(DARK.ramp) <= set(SEQUENTIAL_BLUE)
        assert DARK.ramp[0] == SEQUENTIAL_BLUE[-1]  # near zero recedes into the dark surface
        positions = [SEQUENTIAL_BLUE.index(step) for step in DARK.ramp]
        assert positions == sorted(positions, reverse=True)  # one hue, monotonic

    def test_meter_track_is_a_step_of_the_same_ramp(self) -> None:
        for theme in (LIGHT, DARK):
            assert theme.meter_track in SEQUENTIAL_BLUE
            assert theme.meter_track != theme.series


# --------------------------------------------------------------------------- escaping


class TestEscaping:
    def test_tags_entities_and_quotes_are_escaped(self) -> None:
        assert plotly_text(EVIL) == (
            "&lt;a href=&#34;https://evil.example&#34;&gt;x&lt;/a&gt;&lt;b&gt;y"
        )
        assert plotly_text("Tom & Jerry's") == "Tom &amp; Jerry&#39;s"

    @given(st.text())
    def test_escaped_text_has_no_markup_and_round_trips(self, text: str) -> None:
        escaped = plotly_text(text)
        assert not {"<", ">", '"', "'"} & set(escaped)
        assert html.unescape(escaped) == text

    @given(st.text())
    def test_plotly_displays_the_original_text(self, text: str) -> None:
        """Emulate plotly.js ``convertEntities``: only these entities are decoded."""
        named = {"mu": "μ", "amp": "&", "lt": "<", "gt": ">", "nbsp": "\xa0", "times": "\u00d7"}
        named |= {"plusmn": "±", "deg": "°"}

        def decode(match: re.Match[str]) -> str:
            inner = match.group(1)
            if inner.startswith("#x"):
                return chr(int(inner[2:], 16))
            if inner.startswith("#"):
                return chr(int(inner[1:]))
            return named.get(inner, match.group(0))

        shown = re.sub(r"&(#\d+|#x[\da-fA-F]+|[a-z]+);", decode, plotly_text(text))
        assert shown == text

    def test_variety_name(self) -> None:
        assert variety_name("Sungold", "tomato") == "Sungold · tomato"
        assert variety_name("Mystery", None) == "Mystery"

    def test_long_names_are_shortened_for_tick_labels_only(self) -> None:
        assert MAX_LABEL_CHARS == 24
        assert short_label("Marketmore 76", "cucumber") == "Marketmore 76 · cucumber"
        assert short_label("Cherokee Purple Heart Beefsteak", "tomato") == (
            "Cherokee Purpl… · tomato"
        )
        assert short_label("A" * 40, None) == "A" * 23 + "…"
        name = "Big Rainbow Mortgage Lifter Supreme"
        figure = varieties_bar(varieties(("big", name, "tomato", 4)), LIGHT)
        (layer,) = targets(figure)
        (tick,) = figure.layout.yaxis.ticktext
        assert len(html.unescape(tick)) <= MAX_LABEL_CHARS + len(" · tomato")
        assert tick.endswith("… · tomato")
        assert list(layer.hovertext) == [f"{name} · tomato: 4 comments"]
        assert [row[0] for row in layer.customdata] == ["big"]

    def test_shortening_happens_before_escaping(self) -> None:
        name = "Tom &amp; Jerry & friends' very long heirloom"
        (tick,) = varieties_bar(varieties(("x", name, None, 1)), LIGHT).layout.yaxis.ticktext
        assert html.unescape(tick) == short_label(name, None)
        assert "&amp;amp;" in tick
        assert not re.search(r"&[#a-z0-9]*…", tick)  # never a half entity

    @pytest.mark.parametrize("build", ["bar", "heatmap"])
    def test_an_evil_variety_name_is_escaped_in_the_figure_json(self, build: str) -> None:
        if build == "bar":
            figure = varieties_bar(varieties(("evil", EVIL, "tomato", 3)), LIGHT)
        else:
            figure = topics_heatmap(matrix(("evil", EVIL, "flavor", 3)), LIGHT)
        found = [s for s in strings(figure_json(figure)) if "evil.example" in s]
        assert found
        for value in found:
            assert "<" not in value
            assert ">" not in value
            assert "&lt;a href=&#34;https://evil.example&#34;&gt;" in value
        raw = figure.to_json()
        assert "<a href" not in raw
        assert "<b>" not in raw

    def test_variety_keys_in_click_data_are_escaped_too(self) -> None:
        (layer,) = targets(varieties_bar(varieties(("auntruby's", "Aunt Ruby's", None, 2)), LIGHT))
        assert [row[0] for row in layer.customdata] == ["auntruby&#39;s"]


# --------------------------------------------------------------------------- shared chrome


def assert_static_chrome(figure: go.Figure, theme: ChartTheme) -> None:
    layout = figure.layout
    assert layout.dragmode is False
    assert layout.showlegend is False
    assert layout.paper_bgcolor == "rgba(0,0,0,0)"
    assert layout.plot_bgcolor == "rgba(0,0,0,0)"
    assert layout.height and layout.height > 0
    assert layout.font.color == theme.text_secondary
    assert layout.font.family.startswith('"Source Sans"')  # Streamlit's own face
    assert "system-ui" in layout.font.family  # then the system sans
    assert layout.hoverlabel.font.color == theme.text_primary
    axes = [layout[name] for name in ("xaxis", "yaxis", "xaxis2", "yaxis2") if name in layout]
    for axis in axes:
        if axis.to_plotly_json():
            assert axis.fixedrange is True, axis
    text = json.dumps(figure_json(figure))
    for coordinate in ('"lat"', '"lon"', '"geo"'):
        assert coordinate not in text


# --------------------------------------------------------------------------- varieties


class TestVarietiesBar:
    @THEMES
    def test_one_horizontal_single_color_series_largest_on_top(self, theme: ChartTheme) -> None:
        figure = varieties_bar(TOP, theme)
        (trace,) = bars(figure)
        assert trace.type == "bar"
        assert trace.orientation == "h"
        assert trace.marker.color == theme.series  # one color for every bar, never a ramp
        assert trace.marker.cornerradius == CORNER_PX
        assert list(trace.x) == [10, 5, 1]
        assert list(figure.layout.yaxis.ticktext) == [
            "Sungold · tomato",
            "Juliet · tomato",
            "Mystery",
        ]
        assert list(figure.layout.yaxis.tickvals) == list(trace.y)
        assert figure.layout.yaxis.autorange == "reversed"
        assert_static_chrome(figure, theme)

    @THEMES
    def test_values_at_the_tips_in_text_ink(self, theme: ChartTheme) -> None:
        (trace,) = bars(varieties_bar(TOP, theme))
        assert list(trace.text) == ["10", "5", "1"]
        assert trace.textposition == "outside"
        assert trace.textfont.color == theme.text_primary
        assert trace.textfont.color != theme.series
        assert varieties_bar(TOP, theme).layout.yaxis.tickfont.color == theme.text_secondary

    def test_bars_are_thin_with_room_for_the_tip_labels(self) -> None:
        figure = varieties_bar(TOP, LIGHT)
        assert BAR_PX <= MAX_BAR_PX == 24
        assert (1 - figure.layout.bargap) * ROW_PX == pytest.approx(BAR_PX)
        margin = figure.layout.margin
        assert figure.layout.height == 3 * ROW_PX + margin.t + margin.b
        assert figure.layout.xaxis.visible is False  # every bar carries its value
        assert figure.layout.xaxis.range[1] > 10

    def test_hover_and_click_data(self) -> None:
        figure = varieties_bar(TOP, LIGHT)
        (layer,) = targets(figure)
        assert list(layer.hovertext) == [
            "Sungold · tomato: 10 comments",
            "Juliet · tomato: 5 comments",
            "Mystery: 1 comment",
        ]
        assert layer.hovertemplate == "%{hovertext}<extra></extra>"
        assert [row[0] for row in layer.customdata] == ["sungold", "juliet", "mystery"]
        (painted,) = bars(figure)
        assert painted.customdata is None
        assert painted.unselected.marker.opacity == 1  # a click never greys out the other bars

    def test_whole_rows_are_one_hover_box_and_one_click_target(self) -> None:
        figure = varieties_bar(TOP, LIGHT)
        assert figure.layout.hovermode == "closest"  # no second box on the axis
        assert figure.layout.barmode == "overlay"
        layer, painted = figure.data  # the layer is drawn first, under the bars
        assert layer.hoverinfo != "skip"
        assert painted.hoverinfo == "skip"
        assert layer.width == 1  # the whole band
        assert set(layer.x) == {figure.layout.xaxis.range[1]}  # the whole width
        assert list(layer.y) == list(painted.y)
        assert layer.marker.opacity == 0
        assert layer.selected.marker.opacity == layer.unselected.marker.opacity == 0
        assert not layer.text

    def test_empty_or_malformed_tables_fail_loudly(self) -> None:
        with pytest.raises(ValueError, match="no rows"):
            varieties_bar(TOP.iloc[0:0], LIGHT)
        with pytest.raises(ValueError, match="columns"):
            varieties_bar(GRID, LIGHT)


# --------------------------------------------------------------------------- topics


class TestTopicsHeatmap:
    @THEMES
    def test_sequential_ramp_with_a_scale_legend(self, theme: ChartTheme) -> None:
        figure = topics_heatmap(GRID, theme)
        (heatmap,) = [trace for trace in figure.data if trace.type == "heatmap"]
        stops = [tuple(stop) for stop in heatmap.colorscale]
        assert [color for _, color in stops] == list(theme.ramp)
        assert (stops[0][0], stops[-1][0]) == (0, 1)
        assert heatmap.zmin == 0
        assert heatmap.colorbar.title.text == "Comments"
        assert heatmap.colorbar.tickfont.color == theme.text_secondary
        assert heatmap.showscale is not False
        assert (heatmap.xgap, heatmap.ygap) == (CELL_GAP_PX, CELL_GAP_PX)
        assert_static_chrome(figure, theme)

    def test_rows_columns_and_zero_cells(self) -> None:
        figure = topics_heatmap(GRID, LIGHT)
        (heatmap,) = [trace for trace in figure.data if trace.type == "heatmap"]
        assert [list(row) for row in heatmap.z] == [[4, 1], [0, 2]]
        assert list(figure.layout.yaxis.ticktext) == ["Sungold", "Juliet"]
        assert figure.layout.yaxis.autorange == "reversed"
        assert list(figure.layout.xaxis.ticktext) == ["Flavor", "Weather tolerance"]
        assert TOPIC_TITLES["growing_method"] == "Growing method"
        assert not heatmap.texttemplate  # no number on every cell
        assert heatmap.hoverinfo == "skip"  # the click layer carries hover

    def test_every_cell_has_hover_and_click_data(self) -> None:
        figure = topics_heatmap(GRID, LIGHT)
        (cells,) = [trace for trace in figure.data if trace.type == "scatter"]
        assert list(cells.hovertext) == [
            "Sungold · Flavor: 4 comments",
            "Sungold · Weather tolerance: 1 comment",
            "Juliet · Flavor: 0 comments",
            "Juliet · Weather tolerance: 2 comments",
        ]
        assert [tuple(item) for item in cells.customdata] == [
            ("sungold", "flavor"),
            ("sungold", "weather_tolerance"),
            ("juliet", "flavor"),
            ("juliet", "weather_tolerance"),
        ]
        assert (list(cells.x), list(cells.y)) == ([0, 1, 0, 1], [0, 0, 1, 1])
        assert cells.marker.opacity == 0  # invisible: the heatmap paints, this layer listens
        assert cells.unselected.marker.opacity == 0
        assert figure.layout.hoverdistance == -1

    def test_incomplete_or_empty_grids_fail_loudly(self) -> None:
        with pytest.raises(ValueError, match="no rows"):
            topics_heatmap(GRID.iloc[0:0], LIGHT)
        with pytest.raises(ValueError, match="every variety and topic"):
            topics_heatmap(GRID.iloc[0:3], LIGHT)


# --------------------------------------------------------------------------- setups


class TestSetupsBar:
    @THEMES
    def test_two_single_color_facets(self, theme: ChartTheme) -> None:
        figure = setups_bar(SETUPS, theme)
        traces = bars(figure)
        assert len(traces) == 2
        assert {trace.marker.color for trace in traces} == {theme.series}
        assert all(trace.orientation == "h" for trace in traces)
        assert [list(trace.x) for trace in traces] == [[8, 3], [5]]
        titles = [annotation.text for annotation in figure.layout.annotations]
        assert titles == ["Growing method", "Setting"]
        assert {a.font.color for a in figure.layout.annotations} == {theme.text_primary}
        assert list(figure.layout.yaxis.ticktext) == ["Containers", "In the ground"]
        assert list(figure.layout.yaxis2.ticktext) == ["Greenhouse / tunnel"]
        assert_static_chrome(figure, theme)

    def test_facets_share_one_scale_and_bar_thickness(self) -> None:
        figure = setups_bar(SETUPS, LIGHT)
        assert figure.layout.xaxis.range == figure.layout.xaxis2.range
        assert (1 - figure.layout.bargap) * ROW_PX == pytest.approx(BAR_PX)
        domains = [figure.layout.yaxis.domain, figure.layout.yaxis2.domain]
        plot_height = figure.layout.height - figure.layout.margin.t - figure.layout.margin.b
        heights = [(top - bottom) * plot_height for bottom, top in domains]
        assert heights == [pytest.approx(2 * ROW_PX), pytest.approx(1 * ROW_PX)]

    def test_values_and_hover(self) -> None:
        figure = setups_bar(SETUPS, LIGHT)
        method, _ = bars(figure)
        assert list(method.text) == ["8", "3"]
        method_rows, shelter_rows = targets(figure)
        assert list(shelter_rows.hovertext) == ["Greenhouse / tunnel: 5 comments"]
        assert method_rows.customdata is None  # not clickable
        assert figure.layout.hovermode == "closest"

    def test_a_missing_facet_is_left_out(self) -> None:
        figure = setups_bar(SETUPS.iloc[0:2], LIGHT)
        assert len(bars(figure)) == 1
        assert [a.text for a in figure.layout.annotations] == ["Growing method"]

    def test_no_ranking_words(self) -> None:
        text = " ".join(displayed(setups_bar(SETUPS, LIGHT))).casefold()
        assert "greenhouse" in text
        for word in ("best", "top", "rank", "score", "recommend", "%"):
            assert word not in text, word

    def test_empty_table_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="no rows"):
            setups_bar(SETUPS.iloc[0:0], LIGHT)
