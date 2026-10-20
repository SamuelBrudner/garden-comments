"""Pure helpers for the Streamlit viewer: copy, labels, session-state mapping and escaping.

Nothing here imports Streamlit or touches I/O, so every rule the Browse screen follows can be
unit tested: how untrusted comment text is made inert (:func:`escape_markdown`), how a card is
titled, labelled and explained (:func:`card_heading`, :func:`condition_caption`,
:func:`condition_lines`, :func:`region_line`, :func:`evidence_rows`), which banner a scope
shows (:func:`scope_banner`), which session keys the Clear button resets
(:func:`reset_state_keys`), how results are paged (:func:`paging_fixes`), and which startup
options and server addresses are accepted (:func:`parse_app_args`,
:func:`is_loopback_address`). Wording follows SPEC §2 and §5: a condition that was not stated
is never called a match, words that could not be mapped are shown as written, self-described
conditions (summer heat, humidity, rainfall, season length) are quoted in the commenter's own words
and never called different, and reports are grouped by stated circumstances, never by variety
quality.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import ipaddress
import json
import math
import string
from collections import Counter
from collections.abc import Collection, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, NoReturn, cast, get_args

import pandas as pd

from crop_explorer.config import DEFAULT_CONFIG_PATH, ContextLabel, OutsideLink, QuickChip
from crop_explorer.environment_match import match_context, values_with
from crop_explorer.geography import RegionCounts, count_label
from crop_explorer.overlay_charts import OVERLAY_KEYS, OVERLAY_LABELS, OverlayKey
from crop_explorer.overview import Headline
from crop_explorer.overview_charts import SETUP_TITLES, TOPIC_TITLES, ChartTheme, variety_name
from crop_explorer.regions import CA_PROVINCES, COUNTRIES, REGION_NAMES, US_STATES
from crop_explorer.schemas import TOPICS, Basis, EnvKey, RightsStatus
from crop_explorer.search import FilterOptions
from crop_explorer.views import (
    TIER_ORDER,
    ChipSupport,
    CommentResult,
    CommentView,
    DimensionMatch,
    FactView,
    GardenContext,
    GroupKey,
    MatchResult,
    MatchStatus,
    MentionMatch,
    MentionView,
    ResultGroup,
    SearchQuery,
    SearchResult,
    ViewerScope,
)
from crop_explorer.vocab import (
    COMPARABLE_KEYS,
    KEY_LABELS,
    PROMINENT_KEYS,
    SELF_DESCRIBED_KEYS,
    VIEWER_VALUES,
    ComparableKey,
    value_label,
)

__all__ = [
    "ALL_STATES",
    "ALWAYS_VISIBLE_KEYS",
    "BASE_MAP_URL",
    "CONDITION_KEYS",
    "CROP_KEY",
    "DEMO_BANNER",
    "EVIDENCE_LABEL",
    "GLANCE_TABS",
    "HIGHLIGHT_ZONE_KEY",
    "MISSING_DB_COMMAND",
    "MORE_KEYS",
    "NOT_ANALYSED_LINE",
    "NO_RESULTS",
    "NO_SETUPS",
    "NO_TOPICS",
    "NO_VARIETIES",
    "OVERLAY_KEY",
    "PAGE_QUERY_KEY",
    "PRIVATE_BANNER",
    "REGION_FILTER_KEY",
    "REGION_KEY",
    "RESULTS_ANCHOR",
    "RESULTS_ANCHOR_HTML",
    "STRICT_KEY",
    "SYNTHETIC_NOTE",
    "TABLE_LABEL",
    "TEXT_KEY",
    "TOPICS_KEY",
    "VARIETY_FILTER_KEY",
    "WORD_JOINER",
    "WRITTEN_LABEL",
    "YEAR_KEY",
    "AppArgs",
    "EvidenceRow",
    "SourceRow",
    "active_more_conditions",
    "card_heading",
    "card_title",
    "cards_to_show",
    "cell_click_updates",
    "chip_help",
    "chip_is_active",
    "chip_state",
    "colorbar_dtick",
    "condition_caption",
    "condition_lines",
    "condition_options",
    "count_summary",
    "crop_label",
    "empty_state",
    "escape_markdown",
    "evidence_rows",
    "flatten",
    "garden_from_state",
    "glance_feedback",
    "glance_key",
    "group_heading",
    "highlight_label",
    "highlight_options",
    "highlight_zone",
    "is_loopback_address",
    "key_label",
    "kpi_tiles_html",
    "label_badge",
    "map_colorbar",
    "map_key",
    "map_notes",
    "min_cell_count",
    "more_conditions_label",
    "option_label",
    "outside_link_markdown",
    "outside_overlay_note",
    "overlay_choice",
    "page_key",
    "paging_fixes",
    "parse_app_args",
    "posted_line",
    "private_bind_error",
    "provenance_note",
    "query_from_state",
    "region_filter_note",
    "region_label",
    "region_line",
    "region_options",
    "reset_state_keys",
    "reset_values",
    "scope_banner",
    "selected_state",
    "setup_table",
    "setups_caption",
    "show_more_label",
    "source_link",
    "source_rows",
    "stale_state_fixes",
    "state_choices",
    "strict_note",
    "summary_line",
    "topic_label",
    "topic_table",
    "topic_title",
    "topics_caption",
    "topics_caption_html",
    "varieties_caption",
    "varieties_caption_html",
    "variety_click_updates",
    "variety_filter_note",
    "variety_names",
    "variety_table",
    "visible_chips",
    "year_filter_note",
    "year_label",
]

# --------------------------------------------------------------------------- copy

EVIDENCE_LABEL: Final = "community report · reported conditions"
"""Provenance label of every evidence chip (SPEC §7 hard data boundary, §11 #18)."""
DEMO_BANNER: Final = "Demo data: synthetic comments, not real gardener reports"
MIXED_DEMO_BANNER: Final = (
    "Includes synthetic demo comments, which are not real gardener reports. "
    "Each one is marked on its card."
)
PRIVATE_BANNER: Final = (
    "Private local view — includes unverified imported comments. Do not publish or share."
)
SYNTHETIC_NOTE: Final = "Synthetic demo comment — no source link"
"""Shown on every synthetic card: fiction is always marked and never links out."""
NO_LINK_NOTE: Final = "No source link available"
NOT_STATED_LINE: Final = "Growing conditions not stated"
NOT_ANALYSED_LINE: Final = "Not yet analysed for growing conditions"
"""Shown for a comment that has never been through extraction (``parse_status is None``)."""
WRITTEN_LABEL: Final = "Mentioned as written, not comparable"
"""Leads the line of display-only phrases (facts without a normalized value)."""
NO_RESULTS: Final = "No reports to show. Try a different search, or remove a filter or condition."
ALL_STATES: Final = "All states"
MISSING_DB_COMMAND: Final = "uv run python -m crop_explorer demo"
PRIVATE_COMMAND: Final = (
    "uv run streamlit run app.py --server.address localhost -- --scope private"
)
WORD_JOINER: Final = "\u2060"
"""Invisible character after ``:`` and before ``.``: shortcodes and autolinks cannot form."""

_NOT_SET: Final = "Not set"
_MORE_CONDITIONS: Final = "More conditions"
_STRICT_CHECKBOX: Final = '"Only show stated matches"'
_HIDDEN_CONDITIONS: Final = "not stated, not comparable or differ"
"""What strict mode hides: the "not stated or not comparable" and "different" groups."""
_STRICT_NEEDS_CONDITION: Final = (
    "Choose at least one growing condition first: until then nothing is hidden."
)
_STRICT_REGION_ONLY: Final = (
    "Region only lists reports from your area first; choose a condition such as zone or "
    "growing method to use this filter. Until then nothing is hidden."
)
_LOCALHOST: Final = "localhost"
_NBSP: Final = "\u00a0"
_ASCII_PUNCTUATION: Final = frozenset(string.punctuation)
_ITEM_SEPARATOR: Final = " · "
_BASIS_LABELS: Final[Mapping[Basis, str]] = MappingProxyType(
    {"observed_experience": "observed that season", "reported_garden_context": "garden context"}
)
_RIGHTS_LABELS: Final[Mapping[RightsStatus, str]] = MappingProxyType(
    {
        "synthetic": "synthetic demo data (fictional)",
        "cleared": "cleared for publication",
        "private_unverified": "private and unverified (local view only)",
    }
)
_TIER_WORDS: Final = MappingProxyType(
    {
        "match": "matching",
        "partial": "partially matching",
        "not_stated": "not stated or not comparable",
        "different": "different",
    }
)
"""Count-summary words, one per tier, echoing ``views.GROUP_TITLES``."""
_UNSAFE_URL_CHARS: Final = frozenset("<>\"' \t\r\n")
_BADGE_COLORS: Final[Mapping[ContextLabel, str]] = MappingProxyType(
    {
        "community report": "green",
        "reported conditions": "green",
        "regional climate reference": "blue",
        "published external trial": "violet",
    }
)

# --------------------------------------------------------------------------- session keys

REGION_KEY: Final = "garden_region"
CONDITION_KEYS: Final[Mapping[ComparableKey, str]] = MappingProxyType(
    {key: f"garden_{key}" for key in COMPARABLE_KEYS}
)
TEXT_KEY: Final = "query_text"
CROP_KEY: Final = "query_crop"
TOPICS_KEY: Final = "query_topics"
YEAR_KEY: Final = "query_year"
STRICT_KEY: Final = "query_strict"
REGION_FILTER_KEY: Final = "query_region_filter"
VARIETY_FILTER_KEY: Final = "query_variety"
"""Session key of the variety filter (a ``variety_key``), set by clicking "At a glance"."""
PAGE_QUERY_KEY: Final = "page_query"
"""Session key holding the query the per-group page counts belong to."""
OVERLAY_KEY: Final = "map_overlay"
"""Session key of the map's overlay choice (an ``OverlayKey``; ``"none"`` by default). It is a
view option, not a condition or filter, so Clear leaves it as it is."""
HIGHLIGHT_ZONE_KEY: Final = "map_highlight_zone"
"""Session key of the zone overlay's "Highlight a zone" choice (a zone such as ``"5a"``, or
``None``). A view option like the overlay: it never sets the viewer's hardiness zone, never
reaches the query, and Clear leaves it as it is."""
_GROUP_KEYS: Final[tuple[GroupKey, ...]] = get_args(GroupKey)

ALWAYS_VISIBLE_KEYS: Final[tuple[ComparableKey, ...]] = PROMINENT_KEYS
"""Conditions always shown besides Region (SPEC §2 "Which controls are prominent?")."""
MORE_KEYS: Final[tuple[ComparableKey, ...]] = tuple(
    key for key in COMPARABLE_KEYS if key not in ALWAYS_VISIBLE_KEYS
)
"""Conditions one tap away, in the "More conditions" expander (heat, season length, ...)."""


# --------------------------------------------------------------------------- command line


@dataclass(frozen=True)
class AppArgs:
    """Viewer command-line options (``streamlit run app.py -- [options]``).

    Attributes
    ----------
    config : pathlib.Path
        Configuration file.
    db : pathlib.Path or None
        Database override; ``None`` means the configured path.
    scope : {"public", "private"} or None
        Viewer scope override; ``None`` means the configured scope.
    """

    config: Path
    db: Path | None
    scope: ViewerScope | None


class _RaisingParser(argparse.ArgumentParser):
    """An argument parser that raises :class:`ValueError` instead of printing and exiting."""

    def error(self, message: str) -> NoReturn:
        """Raise the parse error so the viewer can show it on the page."""
        raise ValueError(f"Invalid viewer options: {message}")


def parse_app_args(argv: Sequence[str]) -> AppArgs:
    """Parse the viewer's options (Streamlit passes only the arguments after ``--``).

    Parameters
    ----------
    argv : Sequence[str]
        Arguments after the program name.

    Returns
    -------
    AppArgs
        The parsed options.

    Raises
    ------
    ValueError
        If an option is unknown (a typo such as ``--database`` must never silently open the
        default database), abbreviated, missing its value, or has an invalid value.
    """
    parser = _RaisingParser(prog="app.py", add_help=False, allow_abbrev=False)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--scope", choices=get_args(ViewerScope), default=None)
    args = parser.parse_args(list(argv))
    return AppArgs(config=args.config, db=args.db, scope=args.scope)


def is_loopback_address(address: str | None) -> bool:
    """Return whether a server address only accepts connections from this machine.

    Parameters
    ----------
    address : str or None
        Streamlit's ``server.address`` option; ``None`` or ``""`` (every interface) is not
        loopback.

    Returns
    -------
    bool
        ``True`` for ``localhost`` and loopback IP addresses (``127.0.0.0/8``, ``::1``).
    """
    if not address:
        return False
    if address.casefold() == _LOCALHOST:
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:  # a host name other than localhost
        return False


def private_bind_error(address: str | None) -> str:
    """Explain why a private-scope viewer refuses to run on a non-loopback address.

    The restart command itself is :data:`PRIVATE_COMMAND`, shown verbatim as code by the viewer
    (Markdown would turn its ``--`` into a dash).
    """
    where = "every network interface" if not address else f"the address {address}"
    return (
        "The private view shows unverified imported comments, so it only runs when the server "
        f"listens on this computer alone, but it is listening on {where}. Restart it with:"
    )


# --------------------------------------------------------------------------- escaping


def _escape_char(char: str) -> str:
    if char == ":":
        return f"\\:{WORD_JOINER}"
    if char == ".":
        return f"{WORD_JOINER}\\."
    return f"\\{char}" if char in _ASCII_PUNCTUATION else char


def _escape_line(line: str) -> str:
    body = line.lstrip(" \t")
    indent = _NBSP * (len(line) - len(body))
    return indent + "".join(_escape_char(char) for char in body)


def escape_markdown(text: str) -> str:
    """Make untrusted text render literally in Streamlit Markdown.

    Every ASCII punctuation character is backslash-escaped (CommonMark lets any of them be
    escaped), so links, images, HTML tags, headings, lists, emphasis, tables and math cannot
    form. Some constructs are recognised in the text *after* escapes are resolved (GFM
    autolink literals such as ``www.x.org``, ``https://x.org`` and ``a@x.org``, and emoji or
    icon shortcodes such as ``:smile:``), so an invisible word joiner (:data:`WORD_JOINER`)
    is also placed after every colon and before every full stop. Leading spaces and tabs
    become non-breaking spaces (no indented code block), and each line break becomes a hard
    break.

    Parameters
    ----------
    text : str
        Untrusted text, for example a comment body or an evidence quote.

    Returns
    -------
    str
        Markdown that displays ``text`` verbatim.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "  \n".join(_escape_line(line) for line in lines)


# --------------------------------------------------------------------------- labels


def _capitalize(text: str) -> str:
    return text[:1].upper() + text[1:]


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def key_label(key: EnvKey) -> str:
    """Return a widget label for an environment key, e.g. ``"Hardiness zone"``."""
    return _capitalize(KEY_LABELS[key])


def option_label(key: EnvKey, value: str | None) -> str:
    """Return the display label of a condition option (``None`` is ``"Not set"``)."""
    return _NOT_SET if value is None else _capitalize(value_label(key, value))


def condition_options(key: ComparableKey) -> tuple[str | None, ...]:
    """Return a condition selectbox's options: ``None`` (not set) then the selectable values."""
    return (None, *VIEWER_VALUES[key])


def region_options() -> tuple[str | None, ...]:
    """Return region choices: not set, then U.S. states, Canadian provinces and countries.

    Each block is sorted by display name.
    """
    blocks = (US_STATES, CA_PROVINCES, COUNTRIES)
    ordered = [code for block in blocks for code in sorted(block, key=REGION_NAMES.__getitem__)]
    return (None, *ordered)


def region_label(code: str | None) -> str:
    """Return the display name of a region choice; provinces are marked as Canadian."""
    if code is None:
        return _NOT_SET
    name = REGION_NAMES[code]
    return f"{name} (Canada)" if code in CA_PROVINCES else name


def crop_label(crop: str | None) -> str:
    """Return the display label of a crop filter choice."""
    return "Any crop" if crop is None else _capitalize(crop)


def year_label(year: str | None) -> str:
    """Return the display label of a growing-year filter choice."""
    return "Any year" if year is None else year


def topic_label(topic: str) -> str:
    """Return a readable topic name (``weather_tolerance`` -> ``weather tolerance``)."""
    return topic.replace("_", " ")


def topic_title(topic: str) -> str:
    """Return a topic's title, the same on the topic grid and the Topics filter.

    ``weather_tolerance`` -> ``"Weather tolerance"``.
    """
    return TOPIC_TITLES[topic]


# --------------------------------------------------------------------------- session state


def reset_values() -> dict[str, Any]:
    """Return the default value of every condition and filter widget, keyed by session key."""
    return {
        REGION_KEY: None,
        **dict.fromkeys(CONDITION_KEYS.values()),
        TEXT_KEY: "",
        CROP_KEY: None,
        TOPICS_KEY: [],
        YEAR_KEY: None,
        STRICT_KEY: False,
        REGION_FILTER_KEY: None,
        VARIETY_FILTER_KEY: None,
    }


def reset_state_keys() -> tuple[str, ...]:
    """Return every session key the Clear button resets (all conditions and filters)."""
    return tuple(reset_values())


def stale_state_fixes(state: Mapping[str, Any], options: FilterOptions) -> dict[str, Any]:
    """Return session-state updates that drop filter values no longer offered as options.

    A crop, topic, year or variety chosen earlier in a session can disappear from the visible
    data (for example after a re-extraction or a source deletion). Its control would then hold
    a value that is not among its options, so the value is reset rather than kept or guessed.

    Parameters
    ----------
    state : Mapping[str, Any]
        Current session state.
    options : FilterOptions
        The filter choices present in the visible data now.

    Returns
    -------
    dict[str, Any]
        ``{session_key: new_value}`` for each stale filter only; empty when all are valid.
    """
    fixes: dict[str, Any] = {}
    crop, year = state.get(CROP_KEY), state.get(YEAR_KEY)
    if crop is not None and crop not in options.crops:
        fixes[CROP_KEY] = None
    if year is not None and year not in options.years:
        fixes[YEAR_KEY] = None
    if state.get(VARIETY_FILTER_KEY) not in (None, *options.varieties):
        fixes[VARIETY_FILTER_KEY] = None
    topics = list(state.get(TOPICS_KEY) or ())
    kept = [topic for topic in topics if topic in options.topics]
    if kept != topics:
        fixes[TOPICS_KEY] = kept
    return fixes


def _search_text(value: object, max_chars: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"Search text must be a string, got {type(value).__name__}")
    return value[:max_chars].strip() or None


def garden_from_state(state: Mapping[str, Any]) -> GardenContext:
    """Build the viewer's garden from session state (missing keys mean not set).

    Raises
    ------
    pydantic.ValidationError
        If a stored value is not selectable for its condition.
    """
    values = {key: state.get(session_key) for key, session_key in CONDITION_KEYS.items()}
    return GardenContext.model_validate({"region": state.get(REGION_KEY), **values})


def query_from_state(state: Mapping[str, Any], *, max_query_chars: int) -> SearchQuery:
    """Build the search query from session state (missing keys mean defaults).

    Parameters
    ----------
    state : Mapping[str, Any]
        Current session state.
    max_query_chars : int
        The search text is cut to this many characters before it is stripped, as the text box
        does (``cfg.viewer.max_query_chars``); session state can be set without the widget.

    Returns
    -------
    SearchQuery
        The query the controls describe.

    Raises
    ------
    ValueError
        If ``max_query_chars`` is below 1.
    TypeError
        If the search text is not a string.
    pydantic.ValidationError
        If any other stored value is invalid.
    """
    if max_query_chars < 1:
        raise ValueError(f"max_query_chars must be at least 1, got {max_query_chars}")
    return SearchQuery.model_validate(
        {
            "text": _search_text(state.get(TEXT_KEY), max_query_chars),
            "crop": state.get(CROP_KEY),
            "topics": tuple(state.get(TOPICS_KEY) or ()),
            "year": state.get(YEAR_KEY),
            "region_filter": state.get(REGION_FILTER_KEY),
            "variety": state.get(VARIETY_FILTER_KEY),
            "garden": garden_from_state(state),
            "only_stated_matches": state.get(STRICT_KEY, False),
        }
    )


# --------------------------------------------------------------------------- quick chips


def visible_chips(
    chips: Sequence[QuickChip], support: Mapping[str, ChipSupport], minimum: int
) -> tuple[QuickChip, ...]:
    """Return the chips supported by at least ``minimum`` visible reports, in config order.

    The gate is :attr:`ChipSupport.n_any`: reports stating at least one of the chip's
    conditions (SPEC §2 "enough supported reports for those categories").

    Raises
    ------
    KeyError
        If ``support`` has no count for a chip.
    """
    return tuple(chip for chip in chips if support[chip.label].n_any >= minimum)


_GARDEN_STATE_KEYS: Final[Mapping[str, str]] = MappingProxyType(
    {"region": REGION_KEY, **{str(key): value for key, value in CONDITION_KEYS.items()}}
)


def _state_key(condition: str) -> str:
    if condition not in _GARDEN_STATE_KEYS:
        raise ValueError(f"Quick chip condition {condition!r} is not a garden condition")
    return _GARDEN_STATE_KEYS[condition]


def chip_state(chip: QuickChip) -> dict[str, str]:
    """Return the session-state updates that apply a chip's conditions."""
    return {_state_key(key): value for key, value in chip.conditions.items()}


def chip_is_active(chip: QuickChip, state: Mapping[str, Any]) -> bool:
    """Return whether every condition of ``chip`` is already selected."""
    return all(state.get(key) == value for key, value in chip_state(chip).items())


_CHIP_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {"humid": "humid conditions", "rainy": "rainy conditions"}
)
"""Tooltip wording for values whose short label is a bare adjective ("describe humid ...")."""


def _condition_value_label(condition: str, value: str) -> str:
    _state_key(condition)  # validates that the condition is a garden condition
    return _CHIP_LABELS.get(value) or value_label(cast("EnvKey", condition), value)


def _or_join(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} or {items[-1]}"


def _verb(n: int, singular: str, plural: str) -> str:
    return singular if n == 1 else plural


def _chip_verbs(described: bool, single: bool) -> tuple[str, str]:
    """Return a tooltip's verb (singular, plural): self-reports are described, not stated."""
    if described:
        return ("describes", "describe")
    return ("states", "state") if single else ("mentions", "mention")


def chip_help(chip: QuickChip, support: ChipSupport) -> str:
    """Return a chip's tooltip, stating what its support counts really count.

    Parameters
    ----------
    chip : QuickChip
        A configured quick-condition chip.
    support : ChipSupport
        Its counts from :func:`crop_explorer.search.chip_support`.

    Returns
    -------
    str
        ``"Set this condition (8 reports state it)"`` for one condition; for several, both
        counts, e.g. ``"6 reports mention containers or greenhouse / tunnel; 2 mention both"``.
        A chip with a self-described condition (``vocab.SELF_DESCRIBED_KEYS``) counts reports
        that describe it in their own, uncalibrated words, and says so: ``"6 reports describe
        hot summers or humid conditions in their own words; 2 describe both"``, ``"Set this
        condition (4 reports describe it in their own words)"``.

    Raises
    ------
    ValueError
        If a chip condition is not a garden condition.
    """
    labels = [_condition_value_label(key, value) for key, value in chip.conditions.items()]
    described = any(key in SELF_DESCRIBED_KEYS for key in chip.conditions)
    singular, plural = _chip_verbs(described, single=len(labels) == 1)
    suffix = " in their own words" if described else ""
    n_any, n_all = support.n_any, support.n_all
    if len(labels) == 1:
        counted = f"{_plural(n_any, 'report')} {_verb(n_any, singular, plural)} it{suffix}"
        return f"Set this condition ({counted})"
    every = "both" if len(labels) == 2 else "all of them"
    return (
        f"{_plural(n_any, 'report')} {_verb(n_any, singular, plural)} {_or_join(labels)}{suffix}; "
        f"{n_all} {_verb(n_all, singular, plural)} {every}"
    )


def active_more_conditions(state: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the labels of the conditions set inside "More conditions", in display order."""
    values = ((key, state.get(CONDITION_KEYS[key])) for key in MORE_KEYS)
    return tuple(value_label(key, value) for key, value in values if value is not None)


def more_conditions_label(active: Sequence[str]) -> str:
    """Return the "More conditions" expander label, naming the conditions set inside it.

    Parameters
    ----------
    active : Sequence[str]
        Output of :func:`active_more_conditions`.

    Returns
    -------
    str
        ``"More conditions"``, or e.g. ``"More conditions (humid, clay soil)"``, so a condition
        set by a chip is never active out of sight.
    """
    return f"{_MORE_CONDITIONS} ({', '.join(active)})" if active else _MORE_CONDITIONS


# --------------------------------------------------------------------------- result summaries


def flatten(result: SearchResult) -> tuple[CommentResult, ...]:
    """Return the shown results of every group, in display order."""
    return tuple(item for group in result.groups for item in group.results)


def count_summary(result: SearchResult) -> str:
    """Summarise how many reports were found and how they split across tiers."""
    total = _plural(result.n_candidates, "report")
    tiers = [f"{n} {_TIER_WORDS[tier]}" for tier, n in result.tier_counts.items()]
    return _ITEM_SEPARATOR.join([total, *tiers])


def group_heading(group: ResultGroup) -> str:
    """Return a group's heading with its number of reports."""
    return f"{group.title} ({len(group.results)})"


def _strict_note_without_conditions(query: SearchQuery) -> str | None:
    if not query.only_stated_matches:
        return None
    return _STRICT_REGION_ONLY if query.garden.region is not None else _STRICT_NEEDS_CONDITION


def strict_note(result: SearchResult, query: SearchQuery) -> str | None:
    """Explain what "Only show stated matches" hides (or would hide) right now.

    With no comparable condition selected nothing can be hidden; a Region alone is a location
    preference, which the note explains.
    """
    if not query.garden.selected():
        return _strict_note_without_conditions(query)
    if query.only_stated_matches:
        hidden = _plural(result.n_hidden_by_strict, "report")
        return f"{hidden} hidden because their conditions were {_HIDDEN_CONDITIONS}."
    n_hideable = result.tier_counts["not_stated"] + result.tier_counts["different"]
    return (
        f"Ticking this would hide {_plural(n_hideable, 'report')} whose conditions "
        f"were {_HIDDEN_CONDITIONS}."
    )


def region_filter_note(code: str) -> str:
    """Describe an active reported-state filter."""
    return f"Showing only reports that name {REGION_NAMES[code]} as where they grew."


def year_filter_note(year: str) -> str:
    """Describe an active growing-year filter (it hides every report without that year)."""
    return f"Showing only reports that say they grew in {year}."


def empty_state(result: SearchResult) -> str | None:
    """Return the message shown when no group has a report to show.

    Parameters
    ----------
    result : SearchResult
        A result whose ``groups`` are empty.

    Returns
    -------
    str or None
        Without strict hiding, a suggestion to broaden the search. When "Only show stated
        matches" hid every report, a message saying so and how to undo it, or ``None`` when
        no report states a comparable condition: the limitation message then already says
        the reports are hidden by the checkbox (Search rule 7), so it is not repeated.
    """
    n_hidden = result.n_hidden_by_strict
    if n_hidden == 0:
        return NO_RESULTS
    if result.n_with_conditions == 0 and result.limitation_message is not None:
        return None
    if n_hidden == 1:
        return f"The 1 report found is hidden by {_STRICT_CHECKBOX}. Untick it to see it."
    return f"All {n_hidden} reports are hidden by {_STRICT_CHECKBOX}. Untick it to see them."


# --------------------------------------------------------------------------- paging


def page_key(group_key: GroupKey) -> str:
    """Return the session key counting how many pages of a group are shown."""
    return f"page_count_{group_key}"


def paging_fixes(state: Mapping[str, Any], query: SearchQuery) -> dict[str, Any]:
    """Return session-state updates that show one page per group again after a query change.

    Parameters
    ----------
    state : Mapping[str, Any]
        Current session state.
    query : SearchQuery
        The query this run shows.

    Returns
    -------
    dict[str, Any]
        Empty when the page counts belong to ``query``; otherwise the new query fingerprint and
        a count of 1 for every group.
    """
    fingerprint = query.model_dump_json()
    if state.get(PAGE_QUERY_KEY) == fingerprint:
        return {}
    return {PAGE_QUERY_KEY: fingerprint, **{page_key(key): 1 for key in _GROUP_KEYS}}


def cards_to_show(state: Mapping[str, Any], group: ResultGroup, page_size: int) -> int:
    """Return how many of a group's cards to render (whole pages, at most the group size).

    Raises
    ------
    ValueError
        If ``page_size`` is below 1 or the stored page count is not a positive integer.
    """
    pages = state.get(page_key(group.key), 1)
    if type(pages) is not int or pages < 1:
        raise ValueError(f"Session page count for group {group.key!r} must be a positive int")
    if page_size < 1:
        raise ValueError(f"page_size must be at least 1, got {page_size}")
    return min(len(group.results), pages * page_size)


def show_more_label(n_remaining: int, page_size: int) -> str:
    """Return the label of a group's "Show N more" button."""
    n_next = min(n_remaining, page_size)
    if n_remaining > page_size:
        return f"Show {n_next} more ({n_remaining} not shown yet)"
    return f"Show {n_next} more"


# --------------------------------------------------------------------------- cards


def _mention_name(mention: MentionView) -> str:
    return " ".join(part for part in (mention.variety_as_written, mention.crop) if part)


def _mention_label(mention: MentionView) -> str:
    return mention.variety_as_written or mention.crop or "unidentified variety"


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def card_title(result: CommentResult) -> str:
    """Return ``"{variety} {crop} · {topics}"`` for the relevant mentions (else ``"Comment"``)."""
    mentions = [match.mention for match in result.mention_matches]
    names = ", ".join(_unique(_mention_name(mention) for mention in mentions)) or "Comment"
    topics = _unique(topic_label(topic) for mention in mentions for topic in mention.topics)
    title = _capitalize(names)
    return f"{title}{_ITEM_SEPARATOR}{', '.join(topics)}" if topics else title


def _rank(mention_match: MentionMatch) -> int:
    match = mention_match.match
    return len(TIER_ORDER) if match is None else TIER_ORDER[match.tier]


def _best(matches: Sequence[MentionMatch]) -> MentionMatch | None:
    """Return the relevant mention with the best tier (ties go to the earlier mention)."""
    return min(matches, key=_rank, default=None)


_RELEVANT: Final = "Relevant"
_NOT_STATED: Final = "Not stated"
_DIFFERENT: Final = "Different"
_OWN_WORDS: Final = "In their own words"
_COUNTED: Final = "own words, counted"
"""Part holding the own-words lines of self-described conditions that match (or imprecisely)."""
_NOT_COUNTED: Final = "own words, not counted"
"""Part holding the own-words lines described differently or not comparable."""
_COUNTED_STATUSES: Final[frozenset[MatchStatus]] = frozenset({"match", "imprecise"})
_OWN_WORDS_NOTES: Final[Mapping[str, str]] = MappingProxyType(
    {"described_differently": "not counted as a difference", "not_stated": "not comparable"}
)
_TIER_PARTS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "match": (_RELEVANT, _COUNTED),
        "partial": (_RELEVANT, _COUNTED),
        "not_stated": (_NOT_STATED, WRITTEN_LABEL, _NOT_COUNTED),
        "different": (_DIFFERENT,),
    }
)
"""The condition-line part that states why a mention has its tier, in order of preference."""


def _labels(dimension: DimensionMatch, status: MatchStatus) -> str:
    """Label only the reported values whose own comparison gave ``status``."""
    values = values_with(dimension, status)
    return " / ".join(value_label(dimension.key, value) for value in values)


def _viewer_label(dimension: DimensionMatch) -> str:
    return value_label(dimension.key, dimension.viewer_value)


def _quoted(phrase: str) -> str:
    """Quote untrusted words as written, with runs of whitespace collapsed."""
    return f'"{" ".join(phrase.split())}"'


def _imprecise_item(dimension: DimensionMatch) -> str:
    coarse = [value for value in values_with(dimension, "imprecise") if value.isdigit()]
    if dimension.key == "zone" and coarse:
        return f"zone {coarse[0]}, subzone unknown (you: {_viewer_label(dimension)})"
    return f"{_labels(dimension, 'imprecise')} (you: {_viewer_label(dimension)})"


def _relevant_item(dimension: DimensionMatch) -> str:
    if dimension.status == "match":
        return _viewer_label(dimension)
    return _imprecise_item(dimension)


def _only_written(dimension: DimensionMatch) -> bool:
    """Whether a not-stated dimension was mentioned only in words that could not be mapped."""
    stated_in_words = not dimension.reported_values and bool(dimension.written_values)
    return dimension.status == "not_stated" and stated_in_words


def _not_stated_item(dimension: DimensionMatch) -> str:
    label = KEY_LABELS[dimension.key]
    if not dimension.reported_values:
        return label
    return f"{label} (only {_labels(dimension, 'not_stated')} reported, not comparable)"


def _written_item(dimension: DimensionMatch) -> str:
    phrases = " / ".join(_quoted(phrase) for phrase in dimension.written_values)
    return f"{KEY_LABELS[dimension.key]} {phrases}"


def _different_item(dimension: DimensionMatch) -> str:
    return f"{_labels(dimension, 'different')} (you: {_viewer_label(dimension)})"


def _in_own_words(dimension: DimensionMatch) -> bool:
    """Whether a dimension is a self-described condition the commenter gave a comparable value.

    Such a dimension gets its own line in the commenter's words instead of a place in
    "Relevant", "Not stated" or "Different" (``vocab.SELF_DESCRIBED_KEYS``).
    """
    return dimension.key in SELF_DESCRIBED_KEYS and bool(dimension.reported_values)


def _typographic(phrase: str) -> str:
    """Quote untrusted words in typographic quotes, with runs of whitespace collapsed."""
    return f"“{' '.join(phrase.split())}”"


def _own_words_quotes(dimension: DimensionMatch) -> str:
    """Quote, once each, the words behind the reported values that produced the status."""
    values = values_with(dimension, dimension.status)
    words = (f.value_as_written for f in dimension.evidence if f.normalized_value in values)
    return " / ".join(_unique(_typographic(phrase) for phrase in words))


def _own_words_reading(dimension: DimensionMatch) -> str:
    """Say how the words were read: counted as the viewer's value, or their label beside it."""
    if dimension.status == "match":
        return f"counted as {_viewer_label(dimension)}"
    return f"{_labels(dimension, dimension.status)}; you chose {_viewer_label(dimension)}"


def _own_words_region(facts: Iterable[FactView]) -> str:
    """Name the regions a mention's applicable facts state (unmapped ones as written).

    Empty when none is stated: the card's own region line already says so.
    """
    regions = [fact for fact in facts if fact.key == "region"]
    names = _unique(REGION_NAMES[code] for f in regions if (code := f.normalized_value))
    unmapped = _unique(
        f"{f.value_as_written} (not mapped)" for f in regions if not f.normalized_value
    )
    return ", ".join(names or unmapped)


def _own_words_line(dimension: DimensionMatch, region: str) -> str:
    """Quote a self-described condition in the commenter's words, how it was read and where."""
    note = _OWN_WORDS_NOTES.get(dimension.status, "")
    parts = (f"{_own_words_quotes(dimension)} ({_own_words_reading(dimension)})", note, region)
    return f"{_OWN_WORDS}: {_ITEM_SEPARATOR.join(part for part in parts if part)}"


def _own_words_parts(
    dims: Sequence[DimensionMatch], facts: Iterable[FactView]
) -> dict[str, tuple[str, ...]]:
    """Return the own-words lines that count (match, imprecise), then those that do not."""
    region = _own_words_region(facts)
    counted = tuple(_own_words_line(d, region) for d in dims if d.status in _COUNTED_STATUSES)
    others = tuple(_own_words_line(d, region) for d in dims if d.status not in _COUNTED_STATUSES)
    return {part: lines for part, lines in ((_COUNTED, counted), (_NOT_COUNTED, others)) if lines}


def _hard_parts(dims: Sequence[DimensionMatch]) -> dict[str, tuple[str, ...]]:
    """Return the non-empty "Relevant", "Not stated", written and "Different" lines by lead."""
    unstated = [d for d in dims if d.status == "not_stated"]
    parts = {
        _RELEVANT: [_relevant_item(d) for d in dims if d.status in _COUNTED_STATUSES],
        _NOT_STATED: [_not_stated_item(d) for d in unstated if not _only_written(d)],
        WRITTEN_LABEL: [_written_item(d) for d in unstated if _only_written(d)],
        _DIFFERENT: [_different_item(d) for d in dims if d.status == "different"],
    }
    separators = {_NOT_STATED: ", "}
    return {
        lead: (f"{lead}: {separators.get(lead, _ITEM_SEPARATOR).join(items)}",)
        for lead, items in parts.items()
        if items
    }


def _match_parts(match: MatchResult, facts: Iterable[FactView]) -> dict[str, tuple[str, ...]]:
    """Return one mention's non-empty condition lines, keyed by part, in display order."""
    own = [d for d in match.dimensions if _in_own_words(d)]
    hard = [d for d in match.dimensions if not _in_own_words(d)]
    return {**_hard_parts(hard), **_own_words_parts(own, facts)}


def _match_lines(match: MatchResult, facts: Iterable[FactView]) -> tuple[str, ...]:
    return tuple(line for lines in _match_parts(match, facts).values() for line in lines)


def _tier_line(match: MatchResult, facts: Iterable[FactView]) -> str:
    """Return the one condition line that explains a mention's tier."""
    parts = _match_parts(match, facts)
    part = next(part for part in _TIER_PARTS[match.tier] if part in parts)
    return parts[part][0]


def _other_mention_lines(matches: Sequence[MentionMatch], best: MatchResult) -> tuple[str, ...]:
    """State the tier line of every other relevant mention whose tier differs from the best."""
    return tuple(
        f"{_mention_label(m.mention)}: {_tier_line(m.match, m.facts)}"
        for m in matches
        if m.match is not None and m.match.tier != best.tier
    )


def _condition_items(facts: Iterable[FactView]) -> tuple[str, ...]:
    """Label the comparable conditions stated by ``facts``, in canonical key order."""
    values: dict[str, list[str]] = {}
    for fact in facts:
        if fact.normalized_value is not None:
            values.setdefault(fact.key, []).append(fact.normalized_value)
    return tuple(
        value_label(key, value)
        for key in COMPARABLE_KEYS
        for value in _unique(values.get(key, ()))
    )


def _written_items(facts: Iterable[FactView]) -> tuple[str, ...]:
    """Quote the display-only comparable-key phrases of ``facts``, in canonical key order."""
    phrases: dict[str, list[str]] = {}
    for fact in facts:
        if fact.normalized_value is None and fact.key in COMPARABLE_KEYS:
            phrases.setdefault(fact.key, []).append(_quoted(fact.value_as_written))
    return tuple(
        f"{KEY_LABELS[key]} {phrase}"
        for key in COMPARABLE_KEYS
        for phrase in _unique(phrases.get(key, ()))
    )


def _reported_value_lines(matches: Sequence[MentionMatch]) -> tuple[str, ...]:
    """List the comparable conditions reported for each mention, merging identical lists."""
    by_items: dict[tuple[str, ...], list[str]] = {}
    for match in matches:
        items = _condition_items(match.facts)
        if items:
            by_items.setdefault(items, []).append(_mention_label(match.mention))
    if len(matches) == 1:
        return tuple(f"Reported: {_ITEM_SEPARATOR.join(items)}" for items in by_items)
    return tuple(
        f"Reported for {', '.join(_unique(labels))}: {_ITEM_SEPARATOR.join(items)}"
        for items, labels in by_items.items()
    )


def _reported_lines(matches: Sequence[MentionMatch]) -> tuple[str, ...]:
    """Without a selection: reported conditions, then display-only phrases, else not stated."""
    written = _written_items(fact for match in matches for fact in match.facts)
    lines = _reported_value_lines(matches)
    if written:
        lines = (*lines, f"{WRITTEN_LABEL}: {_ITEM_SEPARATOR.join(written)}")
    return lines or (NOT_STATED_LINE,)


def condition_lines(result: CommentResult, garden: GardenContext) -> tuple[str, ...]:
    """Explain a card's conditions as "Relevant", "Not stated", "Different" and own-words lines.

    Parameters
    ----------
    result : CommentResult
        A displayed comment.
    garden : GardenContext
        The viewer's garden used for the search.

    Returns
    -------
    tuple[str, ...]
        ``("Not yet analysed for growing conditions",)`` for a comment never extracted. With
        comparable conditions selected: the best relevant mention's ``Relevant: ...``
        (matches, and imprecise matches with both values), ``Not stated: ...``,
        ``Mentioned as written, not comparable: ...`` (display-only phrases, quoted) and
        ``Different: ...`` lines for zone, method, setting, sun, soil and watering, each only
        when non-empty and each naming only the reported values that produced its status;
        then one ``In their own words: ...`` line per self-described condition
        (``vocab.SELF_DESCRIBED_KEYS``) with a reported value, those that count first:
        ``In their own words: “brutal and muggy” (counted as humid) · Connecticut``,
        ``In their own words: “that July heat wave” (a heatwave; you chose hot summers) ·
        Texas``, ``In their own words: “mild summers” (mild summers; you chose hot summers) ·
        not counted as a difference`` (``· not comparable`` for a
        heatwave against mild or cool summers). Those words are the facts'
        ``value_as_written`` behind the values that produced the status (untrusted text,
        deduplicated, whitespace collapsed); the region is that mention's own. With several
        relevant mentions these are led by ``Shown for X (1 of n mentions)`` and followed by
        one ``"Y: <tier line>"`` per other mention whose tier differs. Without a selection:
        the conditions each mention reported, then display-only phrases, or ``"Growing
        conditions not stated"``.
    """
    if result.comment.parse_status is None:
        return (NOT_ANALYSED_LINE,)
    matches = result.mention_matches
    best = _best(matches)
    match = best.match if best is not None else match_context(garden, ())
    if match is None:
        return _reported_lines(matches)
    if best is None or len(matches) == 1:
        return _match_lines(match, best.facts if best is not None else ())
    lead = f"Shown for {_mention_label(best.mention)} (1 of {len(matches)} mentions)"
    return (lead, *_match_lines(match, best.facts), *_other_mention_lines(matches, match))


def _shows_reported_conditions(result: CommentResult) -> bool:
    """Whether the card's condition lines show a comparable condition the commenter stated.

    The region line never counts: a location is not a comparable condition, and a badge before
    "Growing conditions not stated" would contradict it.
    """
    if result.comment.parse_status is None:
        return False
    matches = result.mention_matches
    best = _best(matches)
    if best is None or best.match is None:
        keys = {fact.key for match in matches for fact in match.facts}
        return not keys.isdisjoint(COMPARABLE_KEYS)
    if any(m.match is not None and m.match.tier != "not_stated" for m in matches):
        return True
    return any(d.reported_values or d.written_values for d in best.match.dimensions)


def condition_caption(result: CommentResult, garden: GardenContext) -> str:
    """Return a card's condition caption as escaped Markdown (SPEC §7 labelled).

    Parameters
    ----------
    result : CommentResult
        A displayed comment.
    garden : GardenContext
        The viewer's garden used for the search.

    Returns
    -------
    str
        The :func:`condition_lines` and (for an analysed comment) the :func:`region_line`, each
        escaped, joined by hard line breaks and led by a ``reported conditions`` badge when a
        condition line shows a comparable condition the commenter stated (as a value or as
        written). A stated region alone gets no badge: the region line names the place and the
        heading's ``community report`` badge already says who reported it.
    """
    lines = condition_lines(result, garden)
    if result.comment.parse_status is not None:
        lines = (*lines, region_line(result))
    text = "  \n".join(escape_markdown(line) for line in lines)
    if _shows_reported_conditions(result):
        return f"{label_badge('reported conditions')} {text}"
    return text


def card_heading(result: CommentResult) -> str:
    """Return a card's title as escaped bold Markdown with a ``community report`` badge."""
    return f"**{escape_markdown(card_title(result))}** {label_badge('community report')}"


def _region_facts(result: CommentResult) -> list[FactView]:
    return [f for m in result.mention_matches for f in m.facts if f.key == "region"]


def region_line(result: CommentResult) -> str:
    """Describe where the gardener said they grew (never inferred from a zone)."""
    facts = _region_facts(result)
    codes = _unique(f.normalized_value or "" for f in facts)
    if codes:
        same = " (same as yours)" if result.region_status == "same" else ""
        return f"Region: {', '.join(REGION_NAMES[code] for code in codes)}{same}"
    written = _unique(f.value_as_written for f in facts)
    if written:
        return f"Region as written: {', '.join(written)} (not mapped)"
    return "Region not reported"


@dataclass(frozen=True)
class EvidenceRow:
    """One evidence chip of a card.

    Attributes
    ----------
    chip : str
        Normalized condition label, or the words as written for a display-only fact.
    key_label : str
        Which dimension the fact describes, e.g. ``"growing method"``.
    quote : str
        The exact passage of the comment supporting the fact (untrusted text).
    label : str
        Provenance label, always :data:`EVIDENCE_LABEL`.
    basis : str
        ``"observed that season"`` or ``"garden context"``.
    applies_to : str
        ``"whole comment"`` or the mention the fact belongs to.
    """

    chip: str
    key_label: str
    quote: str
    label: str
    basis: str
    applies_to: str


def _owners(matches: Sequence[MentionMatch]) -> dict[str, str]:
    return {f.fact_id: _mention_label(m.mention) for m in matches for f in m.mention.facts}


def _evidence_row(fact: FactView, owners: Mapping[str, str]) -> EvidenceRow:
    if fact.normalized_value is None:
        chip = f"{fact.value_as_written} (as written)"
    else:
        chip = value_label(fact.key, fact.normalized_value)
    whole = fact.applicability == "whole_comment" or fact.fact_id not in owners
    return EvidenceRow(
        chip=chip,
        key_label=KEY_LABELS[fact.key],
        quote=fact.evidence_quote,
        label=EVIDENCE_LABEL,
        basis=_BASIS_LABELS[fact.basis],
        applies_to="whole comment" if whole else owners[fact.fact_id],
    )


def evidence_rows(result: CommentResult) -> tuple[EvidenceRow, ...]:
    """Return one evidence row per distinct fact applicable to the card's relevant mentions."""
    owners = _owners(result.mention_matches)
    facts = {f.fact_id: f for m in result.mention_matches for f in m.facts}
    return tuple(_evidence_row(fact, owners) for fact in facts.values())


def source_link(view: CommentView) -> str | None:
    """Return the comment's permalink, if it has one and the comment is real.

    A synthetic comment is fiction, so it never links out, even when its import carried a
    permalink.

    Raises
    ------
    ValueError
        If a stored permalink is not an http(s) URL (imports only accept those).
    """
    link = view.permalink
    if link is not None and not link.lower().startswith(("https://", "http://")):
        raise ValueError(f"Comment {view.comment_id} has a permalink that is not http(s)")
    return None if view.rights_status == "synthetic" else link


def provenance_note(view: CommentView) -> str | None:
    """Return the card's provenance note: always the synthetic marker on synthetic rows.

    Otherwise a note that no source link exists, or ``None`` when the card shows a link.
    """
    if view.rights_status == "synthetic":
        return SYNTHETIC_NOTE
    return None if source_link(view) is not None else NO_LINK_NOTE


def posted_line(view: CommentView) -> str | None:
    """Return ``"Posted YYYY-MM-DD"`` when the comment's date is known."""
    return None if view.created_at is None else f"Posted {view.created_at[:10]}"


# --------------------------------------------------------------------------- scope and sources


def scope_banner(scope: ViewerScope, rights: Collection[RightsStatus]) -> str | None:
    """Return the banner stating what data the viewer is showing.

    Parameters
    ----------
    scope : {"public", "private"}
        Viewer scope.
    rights : Collection[RightsStatus]
        Rights statuses of the visible sources.

    Returns
    -------
    str or None
        The private warning, the demo-data notice, or ``None`` for cleared data only.

    Raises
    ------
    ValueError
        If private, unverified rows reached a public view.
    """
    if scope == "private":
        return PRIVATE_BANNER
    if "private_unverified" in rights:
        raise ValueError("private_unverified rows must never be visible in public scope")
    if "synthetic" not in rights:
        return None
    return DEMO_BANNER if set(rights) == {"synthetic"} else MIXED_DEMO_BANNER


@dataclass(frozen=True)
class SourceRow:
    """One visible source in the About / Sources panel."""

    source_id: str
    kind: str
    rights: str
    n_comments: int


def source_rows(views: Sequence[CommentView]) -> tuple[SourceRow, ...]:
    """Summarise visible comments per source, sorted by source id."""
    counts = Counter(view.source_id for view in views)
    first = {view.source_id: view for view in reversed(views)}
    return tuple(
        SourceRow(sid, first[sid].source_kind, _RIGHTS_LABELS[first[sid].rights_status], n)
        for sid, n in sorted(counts.items())
    )


def summary_line(counts: Mapping[str, int]) -> str:
    """Describe the visible data from :func:`crop_explorer.db.summary_counts`."""
    parts = (
        f"{_plural(counts['comments'], 'comment')} from {_plural(counts['sources'], 'source')}",
        f"{counts['parsed_comments']} extracted",
        _plural(counts["mentions"], "variety mention"),
        _plural(counts["facts"], "condition fact"),
    )
    return _ITEM_SEPARATOR.join(parts)


def outside_link_markdown(link: OutsideLink) -> str:
    """Return a Markdown link to an outside resource with an escaped title.

    Raises
    ------
    ValueError
        If the URL contains characters that could break out of the link.
    """
    if _UNSAFE_URL_CHARS.intersection(link.url):
        raise ValueError(f"Outside-context URL for {link.title!r} contains unsafe characters")
    return f"[{escape_markdown(link.title)}](<{link.url}>)"


_OVERLAY_NOTES: Final = MappingProxyType(
    {
        "zones": (
            "Also available as a map overlay: Map tab, Overlay, "
            f"\u201c{OVERLAY_LABELS['zones']}\u201d (simplified data, not the official map)."
        ),
        "normals": (
            "Also available as a map overlay: Map tab, Overlay, the three NOAA 1991-2020 "
            "choices (statewide averages from NOAA's Climate at a Glance, not this "
            "station-based product)."
        ),
    }
)


def outside_overlay_note(link: OutsideLink) -> str | None:
    """Return where an outside resource's kind of data is also shown as a map overlay, if it is.

    ``None`` for a link without ``overlay`` (trials, outside gardeners' ratings, NASA POWER).
    """
    return None if link.overlay is None else _OVERLAY_NOTES[link.overlay]


def label_badge(label: ContextLabel) -> str:
    """Return a Streamlit Markdown badge for an evidence-type label (SPEC §7 data boundary)."""
    return f":{_BADGE_COLORS[label]}-badge[{label}]"


# --------------------------------------------------------------------------- at a glance

GLANCE_TABS: Final = ("Varieties", "Topics", "How people grow")
"""Short tab labels, so all three fit across a phone (the captions say the rest)."""
NO_VARIETIES: Final = "No variety has been identified in these reports yet."
NO_SETUPS: Final = "No report states a growing method or setting yet."
NO_TOPICS: Final = "No topics have been identified for these varieties yet."
TABLE_LABEL: Final = "Show as table"
RESULTS_ANCHOR: Final = "glance-results"
"""Element id just above the results, the target of the "Go to the reports" link."""
RESULTS_ANCHOR_HTML: Final = f'<div id="{RESULTS_ANCHOR}"></div>'
"""Trusted, fixed markup placing :data:`RESULTS_ANCHOR` on the page."""
_CONVERSATION_NOT_QUALITY: Final = (
    "This counts conversation, not quality: a failed crop counts the same as a great one."
)
_TOPIC_NOT_OUTCOME: Final = (
    "A comment can touch several topics, and a topic says what was discussed, not whether it "
    "went well."
)
_HINTS: Final = MappingProxyType(
    {
        "bar": (
            "Click a bar to show its comments below.",
            "Tap a bar once to see its count, then tap it again to show its comments below.",
        ),
        "cell": (
            "Click a cell to show those comments below.",
            "Tap a cell once to see its count, then tap it again to show those comments below.",
        ),
    }
)
"""(mouse, touch) hints. On a touch screen Plotly selects a point only on the second tap: the
first tap shows its hover label (``selectOnClick`` reads the hover data before the tap
creates it), so touch devices are told to tap twice."""
_HINT: Final = '<span class="glance-mouse">{}</span><span class="glance-touch">{}</span>'
_TILE_STYLE: Final = (
    "<style>"
    ".glance-kpis{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;"
    "margin:0;font-family:inherit}}"
    "@media (max-width:640px){{.glance-kpis{{grid-template-columns:repeat(2,minmax(0,1fr));"
    "gap:8px}}.glance-tile{{padding:8px 12px}}.glance-value{{font-size:1.5rem;margin-top:0}}}}"
    ".glance-tile{{border:1px solid {border};border-radius:10px;padding:12px 14px;min-width:0;"
    "overflow-wrap:anywhere}}"
    ".glance-label{{color:{secondary};font-size:0.875rem;line-height:1.3}}"
    ".glance-value{{color:{primary};font-size:1.75rem;font-weight:600;line-height:1.2;"
    "margin-top:6px;font-variant-numeric:proportional-nums}}"
    ".glance-of{{font-size:1rem;font-weight:500;color:{secondary}}}"
    ".glance-note{{color:{secondary};font-size:0.8rem;line-height:1.35;margin-top:4px}}"
    ".glance-meter{{display:flex;height:6px;border-radius:3px;background:{track};"
    "overflow:hidden;margin-top:8px}}"
    ".glance-meter span:first-child{{background:{fill};border-radius:3px}}"
    ".glance-touch{{display:none}}"
    "@media (hover:none){{.glance-touch{{display:inline}}.glance-mouse{{display:none}}}}"
    "</style>"
)
"""Tile and hint styles, filled with one theme's tokens: tiles 4 across, 2 x 2 (and a little
tighter) at phone width, in the page's own font; notes in the secondary ink (AA contrast on
both themes); the touch hint only where the pointer cannot hover."""
_TILE: Final = (
    '<div class="glance-tile"><div class="glance-label">{label}</div>'
    '<div class="glance-value">{value}</div>{extra}</div>'
)
_METER: Final = (
    '<div class="glance-meter" role="meter" aria-label="Reports that describe growing '
    'conditions" aria-valuemin="0" aria-valuemax="{total}" aria-valuenow="{part}">'
    '<span style="flex:{part} 1 0px"></span><span style="flex:{rest} 1 0px"></span></div>'
)
_COMPACT_FROM: Final = 10_000
_CONDITIONS_NOTE: Final = "zone, method, heat…"
"""What "Describe conditions" counts: any comparable growing condition, not a U.S. state."""
_NAVIGATION_RESETS: Final = MappingProxyType(
    {TEXT_KEY: "", CROP_KEY: None, YEAR_KEY: None, REGION_FILTER_KEY: None}
)
"""Filters a chart click clears, so the reports shown are exactly those the mark counted. The
viewer's garden and "Only show stated matches" are kept: they group reports, they are not a
search."""


def _figure(n: int) -> str:
    """Format a tile value: ``1,284``, then ``12.9K`` and ``3.4M`` from ten thousand."""
    if n < _COMPACT_FROM:
        return f"{n:,}"
    return f"{n / 1_000:.1f}K" if n < 1_000_000 else f"{n / 1_000_000:.1f}M"


def _tile(label: str, value: str, extra: str = "") -> str:
    return _TILE.format(label=label, value=value, extra=extra)


def _note(text: str) -> str:
    return f'<div class="glance-note">{text}</div>' if text else ""


def _tile_style(theme: ChartTheme) -> str:
    return _TILE_STYLE.format(
        border=theme.border,
        primary=theme.text_primary,
        secondary=theme.text_secondary,
        track=theme.meter_track,
        fill=theme.series,
    )


def _conditions_tile(part: int, total: int) -> str:
    """Return the "33 of 69" tile, with a meter of the same proportion."""
    value = f'{_figure(part)}<span class="glance-of"> of {_figure(total)}</span>'
    meter = _METER.format(total=total, part=part, rest=total - part)
    return _tile("Describe conditions", value, meter + _note(_CONDITIONS_NOTE))


def kpi_tiles_html(headline: Headline, theme: ChartTheme) -> str:
    """Return the "At a glance" stat tiles as trusted HTML (numbers and fixed labels only).

    Parameters
    ----------
    headline : Headline
        The counts to show; nothing else (no comment text) ever reaches this HTML.
    theme : ChartTheme
        The light or dark tokens, the same ones the charts use.

    Returns
    -------
    str
        A style block (tiles, and the mouse/touch hints of :func:`varieties_caption_html`) and a
        CSS grid of four tiles, four across (two by two at phone width): "Reports",
        "Varieties mentioned" ("across 8 crops"), "Describe conditions" ("33 of 69" with a
        meter whose track is a lighter step of the fill's ramp) and "Places named" (U.S.
        states and places outside the U.S.). Values use proportional figures.
    """
    places = f"{_plural(headline.n_us_states, 'U.S. state')} · {headline.n_other_regions} "
    crops = f"across {_plural(headline.n_crops, 'crop')}" if headline.n_crops else ""
    tiles = (
        _tile("Reports", _figure(headline.n_reports), _note("comments in this view")),
        _tile("Varieties mentioned", _figure(headline.n_varieties), _note(crops)),
        _conditions_tile(headline.n_with_conditions, headline.n_reports),
        _tile("Places named", _figure(headline.n_places), _note(f"{places}outside the U.S.")),
    )
    return f'{_tile_style(theme)}<div class="glance-kpis">{"".join(tiles)}</div>'


def varieties_caption(n_shown: int, n_varieties: int) -> str:
    """Say in one short sentence what the variety bars count."""
    if n_shown < n_varieties:
        return f"Comments that mention each variety: top {n_shown} of {n_varieties}."
    return f"Comments that mention each variety (all {n_varieties})."


def _ties(n_tied_off: int, n_comments: int) -> str:
    """Say how many equal varieties a cut left out (empty when none were)."""
    if not n_tied_off:
        return ""
    more = (
        "1 more variety also has" if n_tied_off == 1 else f"{n_tied_off} more varieties also have"
    )
    return f" {more} {_plural(n_comments, 'comment')}; ties are listed alphabetically."


def _caption_html(lead: str, sentence: str, ties: str, mark: str) -> str:
    """Return a chart's caption: what it counts, the notes, then the mouse or touch hint.

    Trusted HTML built from fixed text and numbers only, never comment text; CSS shows one
    of the two hints (:func:`kpi_tiles_html` holds the rule).
    """
    mouse, touch = _HINTS[mark]
    return f"{lead} {sentence}{ties} {_HINT.format(mouse, touch)}"


def varieties_caption_html(
    n_shown: int, n_varieties: int, n_tied_off: int, last_count: int
) -> str:
    """Return the caption under the variety bars (trusted HTML: fixed text and numbers only).

    Parameters
    ----------
    n_shown : int
        Bars drawn.
    n_varieties : int
        Varieties named in all (``Headline.n_varieties``).
    n_tied_off : int
        Varieties left out though they have as many comments as the last bar
        (``Overview.bars_tied_off``).
    last_count : int
        The last bar's number of comments.

    Returns
    -------
    str
        "Comments that mention each variety: top 10 of 44.", that this counts conversation,
        not quality, how many equal varieties the cut left out (if any) and the click or tap
        hint.
    """
    lead = varieties_caption(n_shown, n_varieties)
    ties = _ties(n_tied_off, last_count)
    return _caption_html(lead, _CONVERSATION_NOT_QUALITY, ties, "bar")


def topics_caption(n_rows: int) -> str:
    """Say in one short sentence what the topic grid counts."""
    if n_rows == 1:
        return "Comments about each topic, for the most-discussed variety."
    return (
        f"Comments about each topic, for the {n_rows} most-discussed varieties in the bar "
        "chart's order."
    )


def topics_caption_html(n_rows: int, n_tied_off: int, last_count: int) -> str:
    """Return the caption under the topic grid (trusted HTML: fixed text and numbers only).

    As :func:`varieties_caption_html`: what the grid counts, that a topic is not an outcome,
    the tie note for its last row and the click or tap hint.
    """
    ties = _ties(n_tied_off, last_count)
    return _caption_html(topics_caption(n_rows), _TOPIC_NOT_OUTCOME, ties, "cell")


def setups_caption(n_with_setup: int, n_reports: int) -> str:
    """Say how few reports state a growing method or setting at all."""
    most = " Most don't say." if 2 * n_with_setup < n_reports else ""
    return (
        "Only reports that state a growing method or setting are counted: "
        f"{n_with_setup} of {n_reports}.{most} A report can state more than one."
    )


def _clicked(selection: Mapping[str, Any], size: int) -> list[str] | None:
    """Return the first selected point's ``customdata`` (unescaped) if it is ``size`` strings."""
    points = (selection.get("selection") or {}).get("points") or ()
    for point in points:
        data = point.get("customdata")
        if isinstance(data, list) and len(data) == size and all(isinstance(x, str) for x in data):
            return [html.unescape(item) for item in data]
    return None


def _show_variety(key: str, topics: list[str]) -> dict[str, Any]:
    return {VARIETY_FILTER_KEY: key, TOPICS_KEY: topics, **_NAVIGATION_RESETS}


def variety_click_updates(selection: Mapping[str, Any], known: Collection[str]) -> dict[str, Any]:
    """Return the session updates for a click on a variety bar (empty when nothing usable).

    Parameters
    ----------
    selection : Mapping[str, Any]
        The chart's selection state; its ``customdata`` is ``[variety_key]`` (escaped).
    known : Collection[str]
        The visible variety keys: any other key (stale or forged) changes nothing.

    Returns
    -------
    dict[str, Any]
        The variety filter set to the key, the Topics filter cleared and the narrowing filters
        of :data:`_NAVIGATION_RESETS` cleared, so the reports shown are exactly the comments
        the bar counts; the garden and strict mode are left alone.
    """
    data = _clicked(selection, 1)
    if data is None or data[0] not in known:
        return {}
    return _show_variety(data[0], [])


def cell_click_updates(selection: Mapping[str, Any], known: Collection[str]) -> dict[str, Any]:
    """Return the session updates for a click on a topic cell (empty when nothing usable).

    As :func:`variety_click_updates`, with the Topics filter set to the cell's topic; the
    ``customdata`` is ``[variety_key, topic]``.
    """
    data = _clicked(selection, 2)
    if data is None or data[0] not in known or data[1] not in TOPICS:
        return {}
    return _show_variety(data[0], [data[1]])


def glance_key(chart: str, state: Mapping[str, Any]) -> str:
    """Return an "At a glance" chart's widget key, which changes with what a click sets.

    Like :func:`map_key`: after a click sets the variety filter (and topics), the chart
    remounts without a stale click selection, so the next click always registers.
    """
    fingerprint = json.dumps(
        [
            state.get(VARIETY_FILTER_KEY) or "",
            state.get(TEXT_KEY) or "",
            sorted(state.get(TOPICS_KEY) or ()),
        ]
    )
    return f"glance_{chart}_{hashlib.sha256(fingerprint.encode()).hexdigest()[:12]}"


def variety_names(frame: pd.DataFrame) -> dict[str, str]:
    """Return ``variety_key -> "Sungold · tomato"`` (raw, untrusted: escape before display)."""
    pairs = zip(frame["variety_key"], frame["label"], frame["crop"], strict=True)
    return {
        str(key): variety_name(str(label), crop if isinstance(crop, str) else None)
        for key, label, crop in pairs
    }


def _about_topics(topics: Sequence[str]) -> str:
    return f" about {' or '.join(topic_title(topic) for topic in topics)}" if topics else ""


def glance_feedback(name: str, topics: Sequence[str]) -> str:
    """Return the Markdown line shown above a clicked chart while the variety filter is on.

    Parameters
    ----------
    name : str
        The variety's display name (untrusted: escaped here).
    topics : Sequence[str]
        The Topics filter.

    Returns
    -------
    str
        e.g. ``"Showing the reports that mention Sungold · tomato about Flavor below.
        [Go to the reports ↓](#glance-results)"``.
    """
    shown = f"{escape_markdown(name)}{_about_topics(topics)}"
    return (
        f"Showing the reports that mention {shown} below. [Go to the reports ↓](#{RESULTS_ANCHOR})"
    )


def variety_filter_note(name: str) -> str:
    """Describe an active variety filter (Markdown; the untrusted name is escaped)."""
    return f"Showing only reports that mention {escape_markdown(name)} (all spellings)."


def variety_table(frame: pd.DataFrame) -> pd.DataFrame:
    """Return the variety counts as a readable table (names as written: shown as plain text)."""
    crops = ["not named" if not isinstance(crop, str) else crop for crop in frame["crop"]]
    return pd.DataFrame(
        {"Variety": list(frame["label"]), "Crop": crops, "Comments": list(frame["n_comments"])}
    )


def topic_table(frame: pd.DataFrame) -> pd.DataFrame:
    """Return the variety x topic cells as a readable table."""
    topics = [TOPIC_TITLES[str(topic)] for topic in frame["topic"]]
    return pd.DataFrame(
        {"Variety": list(frame["label"]), "Topic": topics, "Comments": list(frame["n_comments"])}
    )


def setup_table(frame: pd.DataFrame) -> pd.DataFrame:
    """Return the growing method and setting counts as a readable table."""
    conditions = [SETUP_TITLES[str(key)] for key in frame["key"]]
    return pd.DataFrame(
        {
            "Condition": conditions,
            "Stated as": list(frame["label"]),
            "Comments": list(frame["n_comments"]),
        }
    )


# --------------------------------------------------------------------------- map

BASE_MAP_URL: Final = "app/static/topojson/"
"""Where Plotly loads its U.S. base map (``usa_110m.json``, the state shapes every map needs):
this app's own static folder (``static/topojson/``, served with ``server.enableStaticServing``),
never ``cdn.plot.ly``. Relative, so it also works under a base URL path."""


def map_key(region_filter: str | None, overlay: OverlayKey = "none") -> str:
    """Return the map widget's key, which changes whenever the state filter or overlay changes.

    The Plotly chart keeps its own click selection. Re-keying it on every filter change (a map
    click, the state selectbox, "Show all states" or Clear) or overlay change remounts it without
    a stale selection, so clicking a state always selects it on the first click.
    """
    key = f"map_selection_{region_filter or 'all'}"
    return key if overlay == "none" else f"{key}_{overlay}"


def min_cell_count(scope: ViewerScope, public_min_cell_count: int) -> int:
    """Return the map's small-cell threshold: the configured one in public scope, else 1."""
    return public_min_cell_count if scope == "public" else 1


_MAX_COLORBAR_TICKS: Final = 5


def colorbar_dtick(max_count: int) -> int:
    """Return a whole-number colorbar tick step giving at most five steps up to ``max_count``."""
    return max(1, math.ceil(max_count / _MAX_COLORBAR_TICKS))


def map_colorbar(counts: RegionCounts) -> dict[str, Any]:
    """Return Plotly colorbar settings that fit a phone: horizontal, below the map, whole ticks."""
    max_count = int(counts.table["n_comments"].max()) if len(counts.table) else 0
    return {
        "orientation": "h",
        "x": 0.5,
        "xanchor": "center",
        "y": 0,
        "yanchor": "top",
        "thickness": 10,
        "len": 0.7,
        "dtick": colorbar_dtick(max_count),
        "title": {"text": "Reports", "side": "top"},
    }


def map_notes(counts: RegionCounts) -> tuple[str, ...]:
    """Return the counts the map cannot show, always including the not-reported count."""
    notes = [
        f"Region not reported: {counts.not_reported}",
        f"Outside the U.S.: {counts.outside_us}",
    ]
    if counts.us_state_not_reported:
        notes.append(f"In the U.S., state not reported: {counts.us_state_not_reported}")
    return tuple(notes)


def state_choices(counts: RegionCounts, current: str | None) -> dict[str | None, str]:
    """Return the "Filter by reported state" options with labels, alphabetical by state.

    Suppressed cells never show their count. The current filter stays selectable even when
    no report under the other filters names it.
    """
    rows = counts.table.to_dict("records")
    labels = {
        str(row["region_code"]): f"{row['region_name']} ({_cell_label(row, counts)})"
        for row in rows
    }
    if current is not None and current not in labels:
        labels[current] = f"{REGION_NAMES[current]} (no reports with the current filters)"
    ordered = sorted(labels, key=REGION_NAMES.__getitem__)
    return {None: ALL_STATES, **{code: labels[code] for code in ordered}}


def _cell_label(row: Mapping[Hashable, Any], counts: RegionCounts) -> str:
    return count_label(
        int(row["n_comments"]),
        suppressed=bool(row["suppressed"]),
        min_cell_count=counts.min_cell_count,
    )


def highlight_options() -> tuple[str | None, ...]:
    """Return the "Highlight a zone" choices: ``None``, then every zone and half-zone."""
    return condition_options("zone")


def highlight_label(value: str | None, garden_zone: str | None) -> str:
    """Label a "Highlight a zone" choice; ``None`` names what is emphasised without one.

    ``(None, None)`` -> ``"None"``; ``(None, "6b")`` -> ``"Your zone (6b)"`` (the viewer's
    hardiness zone is emphasised); ``("5a", ...)`` -> ``"Zone 5a"``.
    """
    if value is not None:
        return option_label("zone", value)
    return "None" if garden_zone is None else f"Your zone ({garden_zone})"


def highlight_zone(state: Mapping[str, Any]) -> str | None:
    """Return the zone to emphasise on the zone overlay: the map's choice, else the viewer's.

    A value that is not a zone option is ignored. The result only styles the map: it is never
    written to the viewer's conditions or the query.
    """
    options = set(highlight_options()) - {None}
    for value in (state.get(HIGHLIGHT_ZONE_KEY), state.get(CONDITION_KEYS["zone"])):
        if value in options:
            return str(value)
    return None


def overlay_choice(state: Mapping[str, Any]) -> OverlayKey:
    """Return the map's overlay choice from session state (``"none"`` when unset or unknown)."""
    value = state.get(OVERLAY_KEY)
    return next((key for key in OVERLAY_KEYS if key == value), "none")


def selected_state(selection: Mapping[str, Any]) -> str | None:
    """Return the U.S. state clicked on the map, if Streamlit reported one.

    Parameters
    ----------
    selection : Mapping[str, Any]
        Streamlit's Plotly selection state (``{"selection": {"points": [...]}}``); each
        choropleth point carries its postal code as ``location``.

    Returns
    -------
    str or None
        The first selected state's code (``"US-CT"``), or ``None`` when nothing usable was
        selected.
    """
    points = (selection.get("selection") or {}).get("points") or ()
    for point in points:
        code = f"US-{point.get('location')}"
        if code in US_STATES:
            return code
    return None
