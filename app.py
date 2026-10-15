"""Browse screen of "What Gardeners Are Saying" (SPEC §2), a thin Streamlit layer.

Run with ``uv run streamlit run app.py [--server.address localhost] [-- --scope private --db PATH
--config PATH]``. A private view only runs while the server listens on this computer alone
(``--server.address localhost``, the shipped ``.streamlit/config.toml`` default).

All behaviour lives in the library (:mod:`crop_explorer.search`, :mod:`crop_explorer.geography`,
:mod:`crop_explorer.db`) and in the pure helpers of :mod:`crop_explorer.ui`; this file only lays
out widgets. A database connection is opened per script run, query results are never cached,
viewer choices live only in session state, and every piece of comment text is rendered through
``ui.escape_markdown`` (never as HTML).
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Collection
from contextlib import closing
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Final

import plotly.graph_objects as go
import streamlit as st

from crop_explorer import db, geography, search, ui
from crop_explorer.config import AppConfig, LoggingConfig, QuickChip, load_config
from crop_explorer.geography import RegionCounts
from crop_explorer.logging_setup import configure_logging
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

_TITLE: Final = "What Gardeners Are Saying"
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
_DB_ERROR: Final = (
    "The database at {path} cannot be opened: {reason}. Build a new one with: {command}"
)


@dataclass(frozen=True)
class _Context:
    """Everything one script run reads from: the connection, config, scope and time."""

    conn: sqlite3.Connection
    cfg: AppConfig
    scope: ViewerScope
    now: str


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
        format_func=ui.topic_label,
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


def _filter_notes(query: SearchQuery) -> None:
    """Announce the hard filters set outside the result list, each with its reset button."""
    if query.year is not None:
        st.info(ui.year_filter_note(query.year))
        st.button("Any year", key="clear_year_filter", on_click=_reset, args=(ui.YEAR_KEY,))
    if query.region_filter is not None:
        st.info(ui.region_filter_note(query.region_filter))
        st.button(
            "Show all states",
            key="clear_region_filter",
            on_click=_reset,
            args=(ui.REGION_FILTER_KEY,),
        )


def _comments(ctx: _Context, result: SearchResult, query: SearchQuery) -> None:
    _filter_notes(query)
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


def _controls(ctx: _Context, views: Collection[CommentView]) -> SearchQuery:
    """Render the header panels and every input; return the query they describe."""
    _about(ctx, views)
    _banner(ctx.scope, views)
    options = search.filter_options(ctx.conn, ctx.scope, ctx.now)
    _apply(ui.stale_state_fixes(_state(), options))
    _conditions_panel()
    _chips(ctx)
    _filters(ctx, options)
    query = ui.query_from_state(_state(), max_query_chars=ctx.cfg.viewer.max_query_chars)
    _apply(ui.paging_fixes(_state(), query))
    return query


def _results(ctx: _Context, query: SearchQuery) -> None:
    """Strict-mode note, outside context next to the tabs (SPEC §2 mockup), then the tabs."""
    result = search.search(ctx.conn, ctx.scope, query, ctx.now)
    note = ui.strict_note(result, query)
    if note is not None:
        st.caption(note)
    _outside_context(ctx.cfg)
    comments_tab, map_tab = st.tabs(["Comments", "Map"])
    with comments_tab:
        _comments(ctx, result, query)
    with map_tab:
        _map(ctx, query, result)


def _browse(ctx: _Context) -> None:
    views = db.load_comment_views(ctx.conn, ctx.scope, ctx.now, None)
    query = _controls(ctx, views)
    _results(ctx, query)


# --------------------------------------------------------------------------- startup guards


def _app_args() -> ui.AppArgs:
    """Parse the viewer options, or show why they are invalid and stop."""
    try:
        return ui.parse_app_args(sys.argv[1:])
    except ValueError as exc:
        st.error(ui.escape_markdown(str(exc)))
        st.stop()


def _require_local_server(scope: ViewerScope) -> None:
    """Stop a private view unless the server only listens on this computer (fail closed)."""
    address = st.get_option("server.address")
    if scope == "private" and not ui.is_loopback_address(address):
        st.error(ui.escape_markdown(ui.private_bind_error(address)))
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


def main() -> None:
    """Render the Browse screen for the scope and database given on the command line."""
    st.set_page_config(page_title=_TITLE, page_icon="🌱", layout="centered")
    args = _app_args()
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


main()
