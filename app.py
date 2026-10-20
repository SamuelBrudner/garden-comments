"""Browse screen of "Garden variety" (SPEC §2), a thin Streamlit layer.

Run with ``uv run streamlit run app.py [--server.address localhost] [-- --scope private --db PATH
--config PATH]``. A private view only runs while the server listens on this computer alone, so
its command must include ``--server.address localhost`` (``.streamlit/config.toml`` sets no
address, and without one the private view refuses to run). The hosted public demo
(``streamlit_app.py``) imports this module and calls :func:`main` with its own options; the
page only renders by itself when Streamlit runs this file as ``__main__``.

All behaviour lives in the library (:mod:`crop_explorer.search`, :mod:`crop_explorer.geography`,
:mod:`crop_explorer.db`) and in the pure helpers of :mod:`crop_explorer.ui`; this file only lays
out widgets. A database connection is opened per script run, query results are never cached,
viewer choices live only in session state, and every piece of comment text is rendered through
``ui.escape_markdown`` (never as HTML).
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Final

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from crop_explorer import (
    db,
    geography,
    legal,
    overlay_charts,
    overlays,
    overview,
    overview_charts,
    search,
    ui,
)
from crop_explorer.config import (
    AppConfig,
    LoggingConfig,
    MapConfig,
    OutsideLink,
    QuickChip,
    load_config,
)
from crop_explorer.geography import RegionCounts
from crop_explorer.legal import LegalPage
from crop_explorer.logging_setup import configure_logging
from crop_explorer.overlay_charts import OverlayKey
from crop_explorer.overlays import OVERLAY_LABEL, OverlayDataError, Overlays
from crop_explorer.overview import Overview
from crop_explorer.overview_charts import ChartTheme
from crop_explorer.schemas import utc_now_iso
from crop_explorer.search import FilterOptions
from crop_explorer.views import (
    ChipSupport,
    CommentResult,
    CommentView,
    GardenContext,
    GroupKey,
    ResultGroup,
    SearchQuery,
    SearchResult,
    ViewerScope,
)
from crop_explorer.vocab import ComparableKey

_TITLE: Final = "Garden variety"
_SUBTITLE: Final = (
    "Comments about crop varieties and the gardens they were grown in. Choose details about "
    "your garden to see comments from similar gardens first."
)
_ZONE_HELP: Final = (
    "A USDA hardiness zone is based only on the coldest winter temperatures. It says nothing "
    "about summer heat, humidity, season length, soil or sun."
)
_ABOUT: Final = """
If you choose details about your garden, comments are grouped by how closely the garden
details **they mention** match yours. The groups are not a rating. A crop that failed in a
garden like yours is grouped the same way as one that did well.

Each garden detail on a card comes from the exact words of that comment (open **Quotes** to
see them). If no clear mention of one of your garden details was found, the card says
*Not found*. That never counts as a match or a difference. Summer heat, humidity,
rainfall and season length are the gardener's own description, not a measurement. Words that
don't fit one of the choices are shown as written. Comments not yet read for garden details say
so. Region only puts comments from your area first; nearby gardens can still have different
climates. A comment that gives only a hardiness zone is not placed on the map.

Garden details were picked out of each comment ahead of time and checked against its exact
words. Searching only filters and sorts the comments; it does not write any new text. The list
below shows where the comments come from and whether they may be published. The public version
of this page shows only comments cleared for publishing and made-up example comments, which
are marked as made up.
"""
_OUTSIDE_INTRO: Final = (
    "These sites have climate data, university variety trials and other gardeners' ratings. "
    "They describe regions, trials or other gardens, not the gardens in these comments."
)
_MAP_BADGES: Final = ui.label_badge("reported conditions", "from the comments")
_MAP_INTRO: Final = (
    "This map shows the states where gardeners said they grew. Each comment counts once for "
    "each state it names. A comment that gives only a hardiness zone is not on the map. No "
    "exact locations are shown."
)
_MAP_ALT: Final = (
    "Map of U.S. states shaded by how many comments name each state as where the gardener grew."
)
_MAP_CONFIG: Final = {"displayModeBar": False, "scrollZoom": False, "topojsonURL": ui.BASE_MAP_URL}
"""The report-count map: no mode bar or zoom, and the base map from this app, not the CDN."""
_OVERLAY_CONFIG: Final = {**overview_charts.CHART_CONFIG, "topojsonURL": ui.BASE_MAP_URL}
_OVERLAY_HELP: Final = (
    "Climate choices color the map with USDA zones or NOAA state averages. Each state shows its "
    "number of comments. This data says nothing about any gardener's garden or any variety."
)
_OVERLAY_ALT: Final = (
    "Map of the U.S. colored by {layer}, with the number of comments that name each state "
    "written on it."
)
_HIGHLIGHT_HELP: Final = (
    "Shows where one zone is on this map. It does not change your hardiness zone or which "
    "comments are shown."
)
_REFERENCES_ERROR: Final = "The climate data can't be shown: {reason}"
_SERVED_ZONES: Final = (
    Path(__file__).resolve().parent / "static" / "overlays" / (overlays.SERVED_ZONES_NAME)
)
"""The zone layer's copy that browsers fetch (``overlay_charts.ZONE_LAYER_URL``)."""
_CHIPS_LABEL: Final = "Shortcuts for your garden:"
"""Above the quick chips: on a phone their tooltips never show."""
_GLANCE_TITLE: Final = "At a glance"
_GLANCE_ALTS: Final = {
    "varieties": "Bar chart: how many comments mention each of the most-discussed varieties.",
    "topics": (
        "Grid: for each of the most-discussed varieties, how many comments mention each topic."
    ),
    "setups": "Bar charts: how many comments mention each growing method and each setting.",
}
_BACK_LABEL: Final = "Back to the comments"
_DB_ERROR: Final = (
    "The database at {path} cannot be opened: {reason}. Build a new one with: {command}"
)


_ClickReader = Callable[[Mapping[str, Any], Collection[str]], dict[str, Any]]
"""Reads a chart's click selection into session-state updates (``ui.*_click_updates``), given
the visible variety keys."""


@dataclass(frozen=True)
class _Context:
    """Everything one script run reads from: the connection, config, scope and time."""

    conn: sqlite3.Connection
    cfg: AppConfig
    scope: ViewerScope
    now: str


@dataclass(frozen=True)
class _Glance:
    """What every "At a glance" panel draws from in one run."""

    data: Overview
    theme: ChartTheme
    known: frozenset[str]
    """The visible variety keys: a click naming any other key changes nothing."""
    feedback: str | None
    """The line shown above a clickable chart while the variety filter is on."""


# --------------------------------------------------------------------------- callbacks


def _clear() -> None:
    for key, value in ui.reset_values().items():
        st.session_state[key] = value


def _apply_chip(chip: QuickChip) -> None:
    for key, value in ui.chip_state(chip).items():
        st.session_state[key] = value


def _reset(key: str) -> None:
    st.session_state[key] = None


def _show_more(group_key: GroupKey) -> None:
    st.session_state[ui.page_key(group_key)] += 1


def _on_map_select(widget_key: str) -> None:
    code = ui.selected_state(st.session_state[widget_key])
    if code is not None:
        st.session_state[ui.REGION_FILTER_KEY] = code


def _on_glance_select(widget_key: str, read: _ClickReader, known: frozenset[str]) -> None:
    """Turn a click on an "At a glance" chart into the variety filter (nothing when empty)."""
    _apply(read(st.session_state[widget_key], known))


def _apply(updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        st.session_state[key] = value


def _state() -> dict[str, Any]:
    return st.session_state.to_dict()


# --------------------------------------------------------------------------- header


def _about(ctx: _Context, views: Collection[CommentView]) -> None:
    with st.expander("About and sources"):
        st.markdown(_ABOUT)
        st.caption(ui.summary_line(db.summary_counts(ctx.conn, ctx.scope, ctx.now)))
        for row in ui.source_rows(list(views)):
            st.markdown(ui.escape_markdown(ui.source_line(row)))


def _banner(scope: ViewerScope, views: Collection[CommentView]) -> None:
    banner = ui.scope_banner(scope, {view.rights_status for view in views})
    if banner is not None:
        (st.warning if scope == "private" else st.info)(banner)


# --------------------------------------------------------------------------- at a glance


def _glance_chart(
    figure: go.Figure, chart: str, read: _ClickReader | None, known: frozenset[str]
) -> None:
    key = ui.glance_key(chart, _state())
    st.plotly_chart(
        figure,
        key=key,
        on_select="ignore" if read is None else partial(_on_glance_select, key, read, known),
        selection_mode="points",
        config=dict(overview_charts.CHART_CONFIG),
        theme=None,
        width="stretch",
        alt=_GLANCE_ALTS[chart],
    )


def _table(frame: pd.DataFrame) -> None:
    """Show the chart's table-view twin: every value readable without hovering."""
    with st.expander(ui.TABLE_LABEL):
        st.dataframe(frame, hide_index=True, width="stretch")


def _feedback_line(glance: _Glance) -> None:
    """Above a clickable chart, while the variety filter is on: what the click is showing.

    Right above the chart, so it is on screen whenever the chart is (a line under a tall chart
    can fall below the fold). Markdown with the variety name escaped.
    """
    if glance.feedback is not None:
        st.markdown(glance.feedback)


def _caption(caption_html: str) -> None:
    """Under a chart: what it counts, the honesty notes and the mouse or touch hint.

    The chart comes first, right under its tab, so on a phone the bars start above the fold.
    ``caption_html`` is trusted HTML of fixed text and numbers only (``ui.*_caption_html``).
    """
    st.caption(caption_html, unsafe_allow_html=True)


def _last_count(varieties: pd.DataFrame, n_rows: int) -> int:
    return int(varieties["n_comments"].iloc[n_rows - 1])


def _varieties_panel(glance: _Glance) -> None:
    data = glance.data
    if data.varieties.empty:
        st.caption(ui.NO_VARIETIES)
        return
    _feedback_line(glance)
    figure = overview_charts.varieties_bar(data.varieties, glance.theme)
    _glance_chart(figure, "varieties", ui.variety_click_updates, glance.known)
    n_bars = len(data.varieties)
    ties = (data.bars_tied_off, _last_count(data.varieties, n_bars))
    _caption(ui.varieties_caption_html(n_bars, data.headline.n_varieties, *ties))
    _table(ui.variety_table(data.varieties))


def _topics_panel(glance: _Glance) -> None:
    data = glance.data
    if data.topics.empty:
        st.caption(ui.NO_TOPICS)
        return
    _feedback_line(glance)
    figure = overview_charts.topics_heatmap(data.topics, glance.theme)
    _glance_chart(figure, "topics", ui.cell_click_updates, glance.known)
    n_rows = data.topics["variety_key"].nunique()
    ties = (data.rows_tied_off, _last_count(data.varieties, n_rows))
    _caption(ui.topics_caption_html(n_rows, *ties))
    _table(ui.topic_table(data.topics))


def _setups_panel(glance: _Glance) -> None:
    data = glance.data
    if data.setups.empty:
        st.caption(ui.NO_SETUPS)
        return
    figure = overview_charts.setups_bar(data.setups, glance.theme)
    _glance_chart(figure, "setups", None, glance.known)
    st.caption(ui.setups_caption(data.n_with_setup, data.headline.n_reports))
    _table(ui.setup_table(data.setups))


_GLANCE_PANELS: Final = (_varieties_panel, _topics_panel, _setups_panel)


def _feedback(names: Mapping[str, str]) -> str | None:
    """Return the line naming what a chart click is showing (``None`` without the filter)."""
    state = _state()
    key = state.get(ui.VARIETY_FILTER_KEY)
    if key is None or key not in names:
        return None
    return ui.glance_feedback(names[key], state.get(ui.TOPICS_KEY) or ())


def _glance(data: Overview, names: Mapping[str, str]) -> None:
    """Render "At a glance": stat tiles, then one tab per chart, from the visible views only.

    ``data`` is computed from the run's scope-gated views, so a public view never counts
    private rows. Colors follow Streamlit's theme type (light tokens until the browser
    reports one).
    """
    theme = overview_charts.theme_for(st.context.theme.type)
    st.subheader(_GLANCE_TITLE, anchor=False)
    st.markdown(ui.kpi_tiles_html(data.headline, theme), unsafe_allow_html=True)
    if not data.headline.n_reports:
        return
    glance = _Glance(data, theme, frozenset(names), _feedback(names))
    for tab, panel in zip(st.tabs(list(ui.GLANCE_TABS)), _GLANCE_PANELS, strict=True):
        with tab:
            panel(glance)


# --------------------------------------------------------------------------- conditions


def _condition_select(key: ComparableKey) -> None:
    """Render one condition select, starting unselected with its "Not set" placeholder.

    Every select with a "not set" option is built this way, so first load, Clear and a reset
    callback look exactly the same.
    """
    st.selectbox(
        ui.key_label(key),
        ui.condition_options(key),
        index=None,
        format_func=lambda value: ui.option_label(key, value),
        key=ui.CONDITION_KEYS[key],
        help=_ZONE_HELP if key == "zone" else None,
        placeholder=ui.option_label(key, None),
    )


def _region_select() -> None:
    st.selectbox(
        "Region",
        ui.region_options(),
        index=None,
        format_func=ui.region_label,
        key=ui.REGION_KEY,
        placeholder=ui.region_label(None),
    )


def _select_rows(keys: tuple[ComparableKey, ...]) -> None:
    """Lay condition selects out two per row (rows stack in order on a phone)."""
    for start in range(0, len(keys), 2):
        for column, key in zip(st.columns(2), keys[start : start + 2], strict=False):
            with column:
                _condition_select(key)


def _conditions_heading() -> None:
    with st.container(horizontal=True, vertical_alignment="center"):
        st.subheader("Your garden", anchor=False)
        st.button(
            "Clear",
            key="clear_all",
            on_click=_clear,
            help="Clears your garden, the search and all filters.",
        )


def _conditions_panel() -> None:
    """Region, zone, method and setting always visible; the rest one tap away (SPEC §2)."""
    _conditions_heading()
    left, right = st.columns(2)
    with left:
        _region_select()
    with right:
        _condition_select(ui.ALWAYS_VISIBLE_KEYS[0])
    _select_rows(ui.ALWAYS_VISIBLE_KEYS[1:])
    active = ui.active_more_conditions(_state())
    with st.expander(ui.more_conditions_label(active), expanded=bool(active)):
        _select_rows(ui.MORE_KEYS)


def _reference_panel(reference: overlay_charts.RegionalReference) -> None:
    """Show the reference in a compact box: badge and summary, disclaimer, then sources.

    The zone disclaimer is an info box, as prominent as above the zone map; then which zones
    are listed and the sources, as a caption.
    """
    with st.container(border=True, key="regional-reference"):
        st.markdown(f"{ui.label_badge(OVERLAY_LABEL)} {reference.summary}")
        if reference.disclaimer is not None:
            st.info(reference.disclaimer)
        if reference.sources:
            st.caption(reference.sources)


def _regional_reference() -> None:
    """Under the conditions: the viewer's chosen Region described by the packaged references.

    It describes the viewer's region only (SPEC §6): it never feeds matching and never
    appears on a card. Nothing shows until a Region is chosen.
    """
    region = ui.garden_from_state(_state()).region
    if region is None:
        return
    refs = _references_or_error()
    reference = None if refs is None else overlay_charts.regional_reference(region, refs)
    if reference is not None:
        _reference_panel(reference)


def _chip_button(index: int, chip: QuickChip, support: ChipSupport, active: bool) -> None:
    st.button(
        chip.label,
        key=f"chip_{index}",
        on_click=_apply_chip,
        args=(chip,),
        type="primary" if active else "secondary",
        help=ui.chip_help(chip, support),
    )


def _chips(ctx: _Context) -> None:
    support = search.chip_support(ctx.conn, ctx.scope, ctx.now, ctx.cfg.quick_chips)
    shown = ui.visible_chips(ctx.cfg.quick_chips, support, ctx.cfg.viewer.min_reports_for_chip)
    if not shown:
        return
    state = _state()
    st.caption(_CHIPS_LABEL)
    with st.container(horizontal=True, gap="small"):
        for index, chip in enumerate(ctx.cfg.quick_chips):
            if chip in shown:
                _chip_button(index, chip, support[chip.label], ui.chip_is_active(chip, state))


# --------------------------------------------------------------------------- search and filters


def _search_box(max_chars: int) -> None:
    st.text_input(
        "Search comments",
        key=ui.TEXT_KEY,
        placeholder="e.g. Sungold, blight, pots",
        max_chars=max_chars,
    )


def _crop_select(options: FilterOptions) -> None:
    st.selectbox(
        "Crop",
        (None, *options.crops),
        index=None,
        format_func=ui.crop_label,
        key=ui.CROP_KEY,
        placeholder=ui.crop_label(None),
    )


def _topics_select(options: FilterOptions) -> None:
    st.multiselect(
        "Topics",
        options.topics,
        format_func=ui.topic_title,
        key=ui.TOPICS_KEY,
        placeholder="Any topic",
    )


def _year_select(options: FilterOptions) -> None:
    st.selectbox(
        "Year grown",
        (None, *options.years),
        index=None,
        format_func=ui.year_label,
        key=ui.YEAR_KEY,
        placeholder=ui.year_label(None),
    )


def _filter_row(options: FilterOptions) -> None:
    """Crop, Topics and Year grown: hard filters, unlike the garden conditions."""
    crop_column, topic_column, year_column = st.columns(3)
    with crop_column:
        _crop_select(options)
    with topic_column:
        _topics_select(options)
    with year_column:
        _year_select(options)


def _filters(ctx: _Context, options: FilterOptions) -> None:
    _search_box(ctx.cfg.viewer.max_query_chars)
    _filter_row(options)
    st.checkbox(
        ui.STRICT_LABEL,
        key=ui.STRICT_KEY,
        help="Hides comments where no garden detail matches yours, or where one is different.",
    )


# --------------------------------------------------------------------------- cards


def _evidence(result: CommentResult) -> None:
    rows = ui.evidence_rows(result)
    if not rows:
        return
    with st.expander(f"Quotes ({len(rows)})"):
        for row in rows:
            about = f"{row.key_label} · {row.basis} · applies to {row.applies_to}"
            st.markdown(f"**{ui.escape_markdown(row.chip)}** · {ui.escape_markdown(about)}")
            st.markdown(f"> {ui.escape_markdown(row.quote)}")
            st.caption(row.label)


def _source(view: CommentView) -> None:
    notes = [note for note in (ui.posted_line(view), ui.provenance_note(view)) if note]
    if notes:
        st.caption(ui.escape_markdown(" · ".join(notes)))
    link = ui.source_link(view)
    if link is not None:
        st.link_button("Original comment ↗", link)


def _card(result: CommentResult, garden: GardenContext) -> None:
    with st.container(border=True):
        st.markdown(ui.card_heading(result))
        st.markdown(ui.escape_markdown(result.comment.text))
        st.caption(ui.condition_caption(result, garden))
        _evidence(result)
        _source(result.comment)


def _group_cards(group: ResultGroup, garden: GardenContext, page_size: int) -> None:
    """Render one page of cards at a time; the heading keeps the group's full count."""
    shown = ui.cards_to_show(_state(), group, page_size)
    with st.container(key=f"group-{group.key}"):
        for result in group.results[:shown]:
            _card(result, garden)
    remaining = len(group.results) - shown
    if remaining:
        label = ui.show_more_label(remaining, page_size)
        st.button(label, key=f"more-{group.key}", on_click=_show_more, args=(group.key,))


def _group(group: ResultGroup, garden: GardenContext, page_size: int) -> None:
    heading = ui.group_heading(group)
    if group.collapsed_by_default:
        with st.expander(heading, expanded=False):
            _group_cards(group, garden, page_size)
        return
    st.subheader(heading, anchor=False)
    _group_cards(group, garden, page_size)


def _filter_note(note: str, label: str, button_key: str, state_key: str) -> None:
    st.info(note)
    st.button(label, key=button_key, on_click=_reset, args=(state_key,))


def _filter_notes(query: SearchQuery, names: Mapping[str, str]) -> None:
    """Announce the hard filters set outside the result list, each with its reset button."""
    if query.variety is not None:
        note = ui.variety_filter_note(names.get(query.variety, query.variety))
        _filter_note(note, "Show all varieties", "clear_variety_filter", ui.VARIETY_FILTER_KEY)
    if query.year is not None:
        note = ui.year_filter_note(query.year)
        _filter_note(note, "Show all years", "clear_year_filter", ui.YEAR_KEY)
    if query.region_filter is not None:
        note = ui.region_filter_note(query.region_filter)
        _filter_note(note, "Show all states", "clear_region_filter", ui.REGION_FILTER_KEY)


def _comments(
    ctx: _Context, result: SearchResult, query: SearchQuery, names: Mapping[str, str]
) -> None:
    _filter_notes(query, names)
    if result.limitation_message is not None:
        st.info(result.limitation_message)
    st.caption(ui.count_summary(result))
    empty = None if result.groups else ui.empty_state(result)
    if empty is not None:
        st.info(empty)
    for group in result.groups:
        _group(group, query.garden, ctx.cfg.viewer.page_size)


# --------------------------------------------------------------------------- map and context


def _map_counts(ctx: _Context, query: SearchQuery, result: SearchResult) -> RegionCounts:
    """Count the shown reports per state, ignoring the state filter (the map is its selector)."""
    if query.region_filter is not None:
        unfiltered = query.model_copy(update={"region_filter": None})
        result = search.search(ctx.conn, ctx.scope, unfiltered, ctx.now)
    threshold = ui.min_cell_count(ctx.scope, ctx.cfg.map.public_min_cell_count)
    return geography.region_counts(ui.flatten(result), threshold)


def _map_figure(counts: RegionCounts, height: int) -> go.Figure:
    figure = geography.choropleth(counts)
    figure.update_traces(colorbar=ui.map_colorbar(counts))
    figure.update_layout(height=height)
    return figure


def _map_chart(counts: RegionCounts, region_filter: str | None, height: int) -> None:
    widget_key = ui.map_key(region_filter)
    st.plotly_chart(
        _map_figure(counts, height),
        key=widget_key,
        on_select=partial(_on_map_select, widget_key),
        selection_mode="points",
        config=_MAP_CONFIG,
        alt=_MAP_ALT,
    )


def _state_selector(counts: RegionCounts, region_filter: str | None) -> None:
    choices = ui.state_choices(counts, region_filter)
    st.selectbox(
        "Filter by state",
        tuple(choices),
        index=None,
        format_func=choices.__getitem__,
        key=ui.REGION_FILTER_KEY,
        placeholder=ui.ALL_STATES,
    )
    for note in ui.map_notes(counts):
        st.caption(note)


def _overlay_picker() -> None:
    st.radio(
        "Color the map by",
        overlay_charts.OVERLAY_KEYS,
        format_func=overlay_charts.OVERLAY_LABELS.__getitem__,
        key=ui.OVERLAY_KEY,
        horizontal=True,
        help=_OVERLAY_HELP,
    )


def _overlay_chart(figure: go.Figure, region_filter: str | None, overlay: OverlayKey) -> None:
    """Draw an overlay map; clicking a state filters by it, as on the report-count map."""
    widget_key = ui.map_key(region_filter, overlay)
    st.plotly_chart(
        figure,
        key=widget_key,
        on_select=partial(_on_map_select, widget_key),
        selection_mode="points",
        config=_OVERLAY_CONFIG,
        theme=None,
        width="stretch",
        alt=_OVERLAY_ALT.format(layer=overlay_charts.OVERLAY_LABELS[overlay]),
    )


def _zone_highlight() -> None:
    """Offer "Highlight a zone" on the zone layer: a view option, never a condition."""
    garden_zone = ui.garden_from_state(_state()).zone
    st.selectbox(
        "Highlight a zone",
        ui.highlight_options(),
        index=None,
        format_func=lambda value: ui.highlight_label(value, garden_zone),
        key=ui.HIGHLIGHT_ZONE_KEY,
        placeholder=ui.highlight_label(None, garden_zone),
        help=_HIGHLIGHT_HELP,
    )


def _overlay_tables(overlay: OverlayKey, refs: Overlays, disclaimer: str | None) -> None:
    """Show the overlay's table twin: every value readable without hovering.

    The zone tables sit under the zone disclaimer, so the derived zones never appear without it.
    """
    with st.expander(ui.TABLE_LABEL):
        if disclaimer is not None:
            st.info(disclaimer)
        for title, frame in overlay_charts.overlay_tables(overlay, refs):
            st.caption(title)
            st.dataframe(
                frame, hide_index=True, width="stretch", column_config=_one_decimal(frame)
            )


def _one_decimal(frame: pd.DataFrame) -> dict[str, Any]:
    """Show every number column with one decimal ("18.0", not "18")."""
    numbers = frame.select_dtypes("number").columns
    return {str(name): st.column_config.NumberColumn(format="%.1f") for name in numbers}


def _overlay_figure(
    overlay: OverlayKey, refs: Overlays, counts: RegionCounts, zone: str | None, cfg: MapConfig
) -> go.Figure:
    return overlay_charts.overlay_figure(overlay, refs, counts, _chart_theme(), cfg, zone=zone)


def _overlay_map(
    refs: Overlays, counts: RegionCounts, query: SearchQuery, overlay: OverlayKey, cfg: MapConfig
) -> None:
    """Draw the overlay as the only fill, the reports as count labels, then say what it is.

    The zone layer first offers "Highlight a zone" (emphasising that zone, else the viewer's).
    Above the map: its title and, for the zone layer, the "not the official map" disclaimer
    (prominent, as the PHZM terms require for altered data). Below: what a NOAA scale spans,
    label, source, period and what the layer does not say, then the table twin.
    """
    zone = ui.highlight_zone(_state()) if overlay == "zones" else None
    if overlay == "zones":
        _zone_highlight()
    view = overlay_charts.overlay_view(overlay, zone)
    st.markdown(f"**{view.title}**")
    if view.disclaimer is not None:
        st.info(view.disclaimer)
    _overlay_chart(_overlay_figure(overlay, refs, counts, zone, cfg), query.region_filter, overlay)
    for note in (overlay_charts.scale_note(overlay, refs), view.caption):
        if note is not None:
            st.caption(note)
    _overlay_tables(overlay, refs, view.disclaimer)


def _map_badges(with_overlay: bool) -> str:
    return f"{_MAP_BADGES} {ui.label_badge(OVERLAY_LABEL)}" if with_overlay else _MAP_BADGES


def _map(ctx: _Context, query: SearchQuery, result: SearchResult) -> None:
    """Report counts per state, optionally over one regional climate reference (off by default).

    If the references cannot be loaded, the error is shown and the report-count map is drawn.
    """
    counts = _map_counts(ctx, query, result)
    overlay = ui.overlay_choice(_state())
    refs = None if overlay == "none" else _references_or_error()
    st.markdown(_map_badges(refs is not None))
    st.caption(_MAP_INTRO)
    _overlay_picker()
    if refs is None:
        _map_chart(counts, query.region_filter, ctx.cfg.map.height_px)
    else:
        _overlay_map(refs, counts, query, overlay, ctx.cfg.map)
    _state_selector(counts, query.region_filter)


def _outside_link(link: OutsideLink) -> None:
    st.markdown(f"{ui.outside_link_markdown(link)} {ui.outside_badge(link)}")
    if link.note:
        st.caption(ui.escape_markdown(link.note))
    overlay_note = ui.outside_overlay_note(link)
    if overlay_note is not None:
        st.caption(overlay_note)


def _outside_context(cfg: AppConfig) -> None:
    with st.expander("Links to other websites"):
        st.caption(_OUTSIDE_INTRO)
        for link in cfg.outside_context:
            _outside_link(link)


# --------------------------------------------------------------------------- references


@st.cache_resource(show_spinner=False)
def _references() -> Overlays:
    """Load and validate the packaged regional climate references once per server process.

    They are static, attributed reference files shipped with the package; only they are cached
    here, never a query result. Browsers draw the zone layer from the app's static copy, so that
    copy must equal the validated file (else the overlay shows an error, not unchecked data).
    """
    refs = overlays.load_overlays()
    overlays.check_served_copy(_SERVED_ZONES)
    return refs


def _references_or_error() -> Overlays | None:
    """Return the references, or say loudly why they cannot be shown (the page keeps working)."""
    try:
        return _references()
    except OverlayDataError as exc:
        st.error(ui.escape_markdown(_REFERENCES_ERROR.format(reason=exc)))
        return None


def _chart_theme() -> ChartTheme:
    return overview_charts.theme_for(st.context.theme.type)


# --------------------------------------------------------------------------- page


def _controls(
    ctx: _Context, views: Sequence[CommentView], data: Overview, names: Mapping[str, str]
) -> SearchQuery:
    """Render the header panels and every input; return the query they describe."""
    _about(ctx, views)
    _banner(ctx.scope, views)
    options = search.filter_options(ctx.conn, ctx.scope, ctx.now)
    _apply(ui.stale_state_fixes(_state(), options))
    _glance(data, names)
    _conditions_panel()
    _regional_reference()
    _chips(ctx)
    _filters(ctx, options)
    query = ui.query_from_state(_state(), max_query_chars=ctx.cfg.viewer.max_query_chars)
    _apply(ui.paging_fixes(_state(), query))
    return query


def _results(ctx: _Context, query: SearchQuery, names: Mapping[str, str]) -> None:
    """Strict-mode note, outside context next to the tabs (SPEC §2 mockup), then the tabs.

    The tabs sit under the anchor the "At a glance" feedback line links to.
    """
    result = search.search(ctx.conn, ctx.scope, query, ctx.now)
    note = ui.strict_note(result, query)
    if note is not None:
        st.caption(note)
    _outside_context(ctx.cfg)
    st.markdown(ui.RESULTS_ANCHOR_HTML, unsafe_allow_html=True)
    comments_tab, map_tab = st.tabs(["Comments", "Map"])
    with comments_tab:
        _comments(ctx, result, query, names)
    with map_tab:
        _map(ctx, query, result)


def _browse(ctx: _Context) -> None:
    """Load the visible views once; the overview and the variety names come from them."""
    views = db.load_comment_views(ctx.conn, ctx.scope, ctx.now, None)
    data = overview.summarize(views, ctx.cfg.overview)
    names = ui.variety_names(data.all_varieties)
    query = _controls(ctx, views, data, names)
    _results(ctx, query, names)


# --------------------------------------------------------------------------- legal pages


def _back_to_browse() -> None:
    st.query_params.clear()


def _back_button(key: str) -> None:
    st.button(_BACK_LABEL, on_click=_back_to_browse, key=key)


def _legal_page() -> LegalPage | None:
    """Return the legal page the URL asks for; an unknown one is an error that stops the run."""
    try:
        return legal.page_from_param(st.query_params.get(legal.PAGE_PARAM))
    except ValueError as exc:
        st.error(ui.escape_markdown(str(exc)))
        _back_button("back-top")
        st.stop()


def _legal(page: LegalPage) -> None:
    """Render one legal page (trusted, packaged Markdown) instead of the Browse screen."""
    _back_button("back-top")
    st.markdown(legal.legal_text(page))
    _back_button("back-bottom")


# --------------------------------------------------------------------------- startup guards


def _app_args(argv: Sequence[str]) -> ui.AppArgs:
    """Parse the viewer options, or show why they are invalid and stop."""
    try:
        return ui.parse_app_args(argv)
    except ValueError as exc:
        st.error(ui.escape_markdown(str(exc)))
        st.stop()


def _require_local_server(scope: ViewerScope) -> None:
    """Stop a private view unless the server only listens on this computer (fail closed)."""
    address = st.get_option("server.address")
    if scope == "private" and not ui.is_loopback_address(address):
        st.error(ui.escape_markdown(ui.private_bind_error(address)))
        st.code(ui.PRIVATE_COMMAND, language="bash")
        st.stop()


def _require_database(path: Path) -> None:
    if not path.exists():
        where = ui.escape_markdown(str(path))
        st.error(f"No database found at {where}. Run: {ui.MISSING_DB_COMMAND}")
        st.stop()


def _connect(path: Path) -> sqlite3.Connection:
    """Open the database, or show why it cannot be used and stop (never a traceback)."""
    try:
        return db.connect(path)
    except sqlite3.DatabaseError as exc:
        message = _DB_ERROR.format(path=path, reason=exc, command=ui.MISSING_DB_COMMAND)
        st.error(ui.escape_markdown(message))
        st.stop()


@st.cache_resource
def _configure_logging_once(settings_json: str) -> None:
    """Install the log sinks once per server process, not on every rerun of every session.

    Only this side effect is cached; query results never are.
    """
    configure_logging(LoggingConfig.model_validate_json(settings_json))


def main(argv: Sequence[str] | None = None) -> None:
    """Render the Browse screen for the given viewer options.

    Parameters
    ----------
    argv
        Viewer options (``--scope``, ``--db``, ``--config``); ``None`` reads the command line
        (Streamlit passes only the arguments after ``--`` in ``sys.argv[1:]``).
    """
    st.set_page_config(page_title=_TITLE, page_icon="🌱", layout="centered")
    args = _app_args(sys.argv[1:] if argv is None else argv)
    cfg = load_config(args.config)
    _configure_logging_once(cfg.logging.model_dump_json())
    st.title(_TITLE, anchor=False)
    page = _legal_page()
    if page is None:
        _browse_page(args, cfg)
    else:
        _legal(page)


def _browse_page(args: ui.AppArgs, cfg: AppConfig) -> None:
    """Render the Browse screen: the startup guards, the comments, then the legal links."""
    st.markdown(_SUBTITLE)
    scope = args.scope or cfg.viewer.scope
    _require_local_server(scope)
    db_path = args.db or cfg.database.path
    _require_database(db_path)
    with closing(_connect(db_path)) as conn:
        _browse(_Context(conn, cfg, scope, utc_now_iso()))
    st.divider()
    st.caption(legal.footer_markdown())


if __name__ == "__main__":
    main()
