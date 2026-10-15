"""Browse screen of "What Gardeners Are Saying" (SPEC §2), a thin Streamlit layer.

Run with ``uv run streamlit run app.py [-- --scope private --db PATH --config PATH]``.

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

import streamlit as st

from crop_explorer import db, geography, search, ui
from crop_explorer.config import AppConfig, LoggingConfig, QuickChip, load_config
from crop_explorer.logging_setup import configure_logging
from crop_explorer.schemas import utc_now_iso
from crop_explorer.search import FilterOptions
from crop_explorer.views import (
    CommentResult,
    CommentView,
    GardenContext,
    ResultGroup,
    SearchQuery,
    SearchResult,
    ViewerScope,
)
from crop_explorer.vocab import COMPARABLE_KEYS, PROMINENT_KEYS, ComparableKey

_TITLE = "What Gardeners Are Saying"
_SUBTITLE = "Find real experiences from gardens like yours."
_ZONE_HELP = (
    "USDA hardiness zones describe average winter low temperatures only. They say nothing "
    "about summer heat, humidity, season length, soil or sun."
)
_ABOUT = """
Comments from gardeners, grouped by how well the growing conditions **they stated** match the
conditions you choose. Matching is about stated circumstances, not variety quality: a failure
reported under conditions like yours is exactly as relevant as a success.

Every condition on a card comes from an exact quote in that comment (open **Evidence**). A
condition the commenter did not mention is shown as *not stated*, never as a match or a
mismatch. A region is a location preference, not proof of a similar climate, and a zone is
never placed on the map.

Conditions were extracted offline and checked against the comment text; nothing is generated
when you search. Each source carries a rights status, and the public view shows only sources
cleared for publication and clearly labelled synthetic demo data.
"""
_OUTSIDE_INTRO = (
    "Official references and published trials. They describe regions or trials, not what any "
    "commenter experienced in their garden."
)
_MAP_INTRO = (
    "Where gardeners explicitly said they grew (one count per comment and state). Zones are "
    "never mapped, and no point locations are shown."
)
_MAP_HEIGHT = 340
_MAP_ALT = "Map of U.S. states shaded by how many reports name each state as where they grew."
_NO_RESULTS = "No reports to show. Try a different search, or remove a filter or condition."
_MORE_KEYS: tuple[ComparableKey, ...] = tuple(
    k for k in COMPARABLE_KEYS if k not in PROMINENT_KEYS
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


def _clear_region_filter() -> None:
    st.session_state[ui.REGION_FILTER_KEY] = None


def _on_map_select(widget_key: str) -> None:
    code = ui.selected_state(st.session_state[widget_key])
    if code is not None:
        st.session_state[ui.REGION_FILTER_KEY] = code


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
    st.selectbox(
        ui.key_label(key),
        ui.condition_options(key),
        format_func=lambda value: ui.option_label(key, value),
        key=ui.CONDITION_KEYS[key],
        help=_ZONE_HELP if key == "zone" else None,
    )


def _region_select() -> None:
    st.selectbox("Region", ui.region_options(), format_func=ui.region_label, key=ui.REGION_KEY)


def _year_select(years: tuple[str, ...]) -> None:
    st.selectbox("Growing year", (None, *years), format_func=ui.year_label, key=ui.YEAR_KEY)


def _select_rows(keys: tuple[ComparableKey, ...]) -> None:
    """Lay condition selects out two per row (rows stack in order on a phone)."""
    for start in range(0, len(keys), 2):
        for column, key in zip(st.columns(2), keys[start : start + 2], strict=False):
            with column:
                _condition_select(key)


def _conditions_panel(options: FilterOptions) -> None:
    with st.container(horizontal=True, vertical_alignment="center"):
        st.subheader("My growing conditions", anchor=False)
        st.button(
            "Clear", key="clear_all", on_click=_clear, help="Reset every condition and filter"
        )
    left, right = st.columns(2)
    with left:
        _region_select()
    with right:
        _condition_select("zone")
    _select_rows(tuple(key for key in PROMINENT_KEYS if key != "zone"))
    with st.expander("More conditions"):
        _select_rows(_MORE_KEYS)
        _year_select(options.years)


def _chips(ctx: _Context) -> None:
    support = search.chip_support(ctx.conn, ctx.scope, ctx.now, ctx.cfg.quick_chips)
    shown = ui.visible_chips(ctx.cfg.quick_chips, support, ctx.cfg.viewer.min_reports_for_chip)
    if not shown:
        return
    state = st.session_state.to_dict()
    with st.container(horizontal=True, gap="small"):
        for index, chip in enumerate(ctx.cfg.quick_chips):
            if chip in shown:
                st.button(
                    chip.label,
                    key=f"chip_{index}",
                    on_click=_apply_chip,
                    args=(chip,),
                    type="primary" if ui.chip_is_active(chip, state) else "secondary",
                    help=f"Set these conditions ({support[chip.label]} reports state them)",
                )


def _filters(options: FilterOptions) -> None:
    st.text_input(
        "Search crops, varieties, or anything people mentioned",
        key=ui.TEXT_KEY,
        placeholder="e.g. Sungold, blight, pots",
    )
    crop_column, topic_column = st.columns(2)
    with crop_column:
        st.selectbox("Crop", (None, *options.crops), format_func=ui.crop_label, key=ui.CROP_KEY)
    with topic_column:
        st.multiselect(
            "Topics",
            options.topics,
            format_func=ui.topic_label,
            key=ui.TOPICS_KEY,
            placeholder="Any topic",
        )
    st.checkbox(
        "Only show stated matches",
        key=ui.STRICT_KEY,
        help="Hide reports whose conditions were not stated or differ from yours.",
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
        st.markdown(f"**{ui.escape_markdown(ui.card_title(result))}**")
        st.markdown(ui.escape_markdown(result.comment.text))
        lines = (*ui.condition_lines(result, garden), ui.region_line(result))
        st.caption("  \n".join(ui.escape_markdown(line) for line in lines))
        _evidence(result)
        _source(result.comment)


def _group_cards(group: ResultGroup, garden: GardenContext) -> None:
    with st.container(key=f"group-{group.key}"):
        for result in group.results:
            _card(result, garden)


def _group(group: ResultGroup, garden: GardenContext) -> None:
    heading = ui.group_heading(group)
    if group.collapsed_by_default:
        with st.expander(heading, expanded=False):
            _group_cards(group, garden)
        return
    st.subheader(heading, anchor=False)
    _group_cards(group, garden)


def _comments(result: SearchResult, query: SearchQuery) -> None:
    if query.region_filter is not None:
        st.info(ui.region_filter_note(query.region_filter))
        st.button("Show all states", key="clear_region_filter", on_click=_clear_region_filter)
    if result.limitation_message is not None:
        st.info(result.limitation_message)
    st.caption(ui.count_summary(result))
    if not result.groups:
        st.info(_NO_RESULTS)
    for group in result.groups:
        _group(group, query.garden)


# --------------------------------------------------------------------------- map and context


def _map(ctx: _Context, query: SearchQuery, result: SearchResult) -> None:
    unfiltered = query.model_copy(update={"region_filter": None})
    if query.region_filter is not None:
        result = search.search(ctx.conn, ctx.scope, unfiltered, ctx.now)
    threshold = ui.min_cell_count(ctx.scope, ctx.cfg.map.public_min_cell_count)
    counts = geography.region_counts(ui.flatten(result), threshold)
    st.caption(_MAP_INTRO)
    figure = geography.choropleth(counts)
    figure.update_traces(colorbar=ui.map_colorbar(counts))
    figure.update_layout(height=_MAP_HEIGHT)
    widget_key = ui.map_key(query.region_filter)
    st.plotly_chart(
        figure,
        key=widget_key,
        on_select=partial(_on_map_select, widget_key),
        selection_mode="points",
        config={"displayModeBar": False, "scrollZoom": False},
        alt=_MAP_ALT,
    )
    choices = ui.state_choices(counts, query.region_filter)
    st.selectbox(
        "Filter by reported state",
        tuple(choices),
        format_func=choices.__getitem__,
        key=ui.REGION_FILTER_KEY,
    )
    for note in ui.map_notes(counts):
        st.caption(note)


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
    for key, value in ui.stale_state_fixes(st.session_state.to_dict(), options).items():
        st.session_state[key] = value
    _conditions_panel(options)
    _chips(ctx)
    _filters(options)
    return ui.query_from_state(st.session_state.to_dict())


def _results(ctx: _Context, query: SearchQuery) -> None:
    result = search.search(ctx.conn, ctx.scope, query, ctx.now)
    note = ui.strict_note(result, query)
    if note is not None:
        st.caption(note)
    comments_tab, map_tab = st.tabs(["Comments", "Map"])
    with comments_tab:
        _comments(result, query)
    with map_tab:
        _map(ctx, query, result)


def _browse(ctx: _Context) -> None:
    views = db.load_comment_views(ctx.conn, ctx.scope, ctx.now, None)
    query = _controls(ctx, views)
    _results(ctx, query)
    _outside_context(ctx.cfg)


def _require_database(path: Path) -> None:
    if not path.exists():
        where = ui.escape_markdown(str(path))
        st.error(f"No database found at {where}. Run: {ui.MISSING_DB_COMMAND}")
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
    args = ui.parse_app_args(sys.argv[1:])
    cfg = load_config(args.config)
    _configure_logging_once(cfg.logging.model_dump_json())
    st.title(_TITLE, anchor=False)
    st.markdown(_SUBTITLE)
    db_path = args.db or cfg.database.path
    _require_database(db_path)
    with closing(db.connect(db_path)) as conn:
        _browse(_Context(conn, cfg, args.scope or cfg.viewer.scope, utc_now_iso()))


main()
