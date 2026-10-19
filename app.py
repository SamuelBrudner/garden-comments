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

from crop_explorer import db, geography, overview, overview_charts, search, ui
from crop_explorer.config import AppConfig, LoggingConfig, QuickChip, load_config
from crop_explorer.geography import RegionCounts
from crop_explorer.logging_setup import configure_logging
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
_SUBTITLE: Final = "Find real experiences from gardens like yours."
_ZONE_HELP: Final = (
    "USDA hardiness zones describe average winter low temperatures only. They say nothing "
    "about summer heat, humidity, season length, soil or sun."
)
_ABOUT: Final = """
Comments from gardeners, grouped by how well the growing conditions **they stated** match the
conditions you choose. Matching is about stated circumstances, not variety quality: a failure
reported under conditions like yours is exactly as relevant as a success.

Every condition on a card comes from an exact quote in that comment (open **Evidence**). A
condition the commenter did not mention is shown as *not stated*, never as a match or a
mismatch; words that could not be mapped are shown as written, and comments not yet analysed
say so. A region is a location preference, not proof of a similar climate, and a zone is never
placed on the map.

Conditions were extracted offline and checked against the comment text; nothing is generated
when you search. Each source carries a rights status, and the public view shows only sources
cleared for publication and clearly labelled synthetic demo data.
"""
_OUTSIDE_INTRO: Final = (
    "Official references, published trials and outside gardeners' own ratings (community "
    "report). They describe regions, trials or other gardens, not what any commenter here "
    "experienced in their garden."
)
_MAP_BADGES: Final = (
    f"{ui.label_badge('community report')} {ui.label_badge('reported conditions')}"
)
_MAP_INTRO: Final = (
    "Where gardeners explicitly said they grew (one count per comment and state). Zones are "
    "never mapped, and no point locations are shown."
)
_MAP_HEIGHT: Final = 340
_MAP_ALT: Final = (
    "Map of U.S. states shaded by how many reports name each state as where they grew."
)
_MAP_CONFIG: Final = {"displayModeBar": False, "scrollZoom": False}
_GLANCE_TITLE: Final = "At a glance"
_GLANCE_ALTS: Final = {
    "varieties": "Bar chart: how many comments mention each of the most-discussed varieties.",
    "topics": (
        "Grid: how many comments about each of the most-discussed varieties touch each topic."
    ),
    "setups": "Bar charts: how many comments state each growing method and each setting.",
}
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
    with st.expander("About / Sources"):
        st.markdown(_ABOUT)
        st.caption(ui.summary_line(db.summary_counts(ctx.conn, ctx.scope, ctx.now)))
        for row in ui.source_rows(list(views)):
            line = f"{row.source_id} ({row.kind}): {row.n_comments} visible · {row.rights}"
            st.markdown(ui.escape_markdown(line))


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
        st.subheader("My growing conditions", anchor=False)
        st.button(
            "Clear", key="clear_all", on_click=_clear, help="Reset every condition and filter"
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
    with st.container(horizontal=True, gap="small"):
        for index, chip in enumerate(ctx.cfg.quick_chips):
            if chip in shown:
                _chip_button(index, chip, support[chip.label], ui.chip_is_active(chip, state))


# --------------------------------------------------------------------------- search and filters


def _search_box(max_chars: int) -> None:
    st.text_input(
        "Search crops, varieties, or anything people mentioned",
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
        "Only show stated matches",
        key=ui.STRICT_KEY,
        help=(
            "Hide reports whose conditions were not stated, not comparable or different from "
            "yours."
        ),
    )


# --------------------------------------------------------------------------- cards


def _evidence(result: CommentResult) -> None:
    rows = ui.evidence_rows(result)
    if not rows:
        return
    with st.expander(f"Evidence ({len(rows)})"):
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
        st.link_button("Source ↗", link)


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
        _filter_note(note, "Any variety", "clear_variety_filter", ui.VARIETY_FILTER_KEY)
    if query.year is not None:
        note = ui.year_filter_note(query.year)
        _filter_note(note, "Any year", "clear_year_filter", ui.YEAR_KEY)
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


def _map_figure(counts: RegionCounts) -> go.Figure:
    figure = geography.choropleth(counts)
    figure.update_traces(colorbar=ui.map_colorbar(counts))
    figure.update_layout(height=_MAP_HEIGHT)
    return figure


def _map_chart(counts: RegionCounts, region_filter: str | None) -> None:
    widget_key = ui.map_key(region_filter)
    st.plotly_chart(
        _map_figure(counts),
        key=widget_key,
        on_select=partial(_on_map_select, widget_key),
        selection_mode="points",
        config=_MAP_CONFIG,
        alt=_MAP_ALT,
    )


def _state_selector(counts: RegionCounts, region_filter: str | None) -> None:
    choices = ui.state_choices(counts, region_filter)
    st.selectbox(
        "Filter by reported state",
        tuple(choices),
        index=None,
        format_func=choices.__getitem__,
        key=ui.REGION_FILTER_KEY,
        placeholder=ui.ALL_STATES,
    )
    for note in ui.map_notes(counts):
        st.caption(note)


def _map(ctx: _Context, query: SearchQuery, result: SearchResult) -> None:
    counts = _map_counts(ctx, query, result)
    st.markdown(_MAP_BADGES)
    st.caption(_MAP_INTRO)
    _map_chart(counts, query.region_filter)
    _state_selector(counts, query.region_filter)


def _outside_context(cfg: AppConfig) -> None:
    with st.expander("Optional outside context"):
        st.caption(_OUTSIDE_INTRO)
        for link in cfg.outside_context:
            st.markdown(f"{ui.outside_link_markdown(link)} {ui.label_badge(link.label)}")
            if link.note:
                st.caption(ui.escape_markdown(link.note))


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
    st.markdown(_SUBTITLE)
    scope = args.scope or cfg.viewer.scope
    _require_local_server(scope)
    db_path = args.db or cfg.database.path
    _require_database(db_path)
    with closing(_connect(db_path)) as conn:
        _browse(_Context(conn, cfg, scope, utc_now_iso()))


if __name__ == "__main__":
    main()
