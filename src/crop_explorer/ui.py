"""Pure helpers for the Streamlit viewer: copy, labels, session-state mapping and escaping.

Nothing here imports Streamlit or touches I/O, so every rule the Browse screen follows can be
unit tested: how untrusted comment text is made inert (:func:`escape_markdown`), how a card is
titled and explained (:func:`card_title`, :func:`condition_lines`, :func:`region_line`,
:func:`evidence_rows`), which banner a scope shows (:func:`scope_banner`), and which session
keys the Clear button resets (:func:`reset_state_keys`). Wording follows SPEC §2 and §5: a
condition that was not stated is never called a match, and reports are grouped by stated
circumstances, never by variety quality.
"""

from __future__ import annotations

import argparse
import math
import string
from collections import Counter
from collections.abc import Collection, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, get_args

from crop_explorer.config import DEFAULT_CONFIG_PATH, ContextLabel, OutsideLink, QuickChip
from crop_explorer.environment_match import match_context
from crop_explorer.geography import RegionCounts, count_label
from crop_explorer.regions import CA_PROVINCES, COUNTRIES, REGION_NAMES, US_STATES
from crop_explorer.schemas import Basis, EnvKey, RightsStatus
from crop_explorer.search import FilterOptions
from crop_explorer.views import (
    TIER_ORDER,
    CommentResult,
    CommentView,
    DimensionMatch,
    FactView,
    GardenContext,
    MatchResult,
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
    VIEWER_VALUES,
    ComparableKey,
    value_label,
)

__all__ = [
    "CONDITION_KEYS",
    "CROP_KEY",
    "DEMO_BANNER",
    "EVIDENCE_LABEL",
    "MISSING_DB_COMMAND",
    "PRIVATE_BANNER",
    "REGION_FILTER_KEY",
    "REGION_KEY",
    "STRICT_KEY",
    "SYNTHETIC_NOTE",
    "TEXT_KEY",
    "TOPICS_KEY",
    "WORD_JOINER",
    "YEAR_KEY",
    "AppArgs",
    "EvidenceRow",
    "SourceRow",
    "card_title",
    "chip_is_active",
    "chip_state",
    "colorbar_dtick",
    "condition_lines",
    "condition_options",
    "count_summary",
    "crop_label",
    "escape_markdown",
    "evidence_rows",
    "flatten",
    "garden_from_state",
    "group_heading",
    "key_label",
    "label_badge",
    "map_colorbar",
    "map_key",
    "map_notes",
    "min_cell_count",
    "option_label",
    "outside_link_markdown",
    "parse_app_args",
    "posted_line",
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
    "source_link",
    "source_rows",
    "stale_state_fixes",
    "state_choices",
    "strict_note",
    "summary_line",
    "topic_label",
    "visible_chips",
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
NO_LINK_NOTE: Final = "No source link available"
NOT_STATED_LINE: Final = "Growing conditions not stated"
MISSING_DB_COMMAND: Final = "uv run python -m crop_explorer demo"
WORD_JOINER: Final = "\u2060"
"""Invisible character after ``:`` and before ``.``: shortcodes and autolinks cannot form."""

_NOT_SET: Final = "Not set"
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
        "not_stated": "not stated",
        "different": "different",
    }
)
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


def parse_app_args(argv: Sequence[str]) -> AppArgs:
    """Parse the viewer's own options, ignoring any others (Streamlit or pytest flags).

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
    SystemExit
        If a known option has an invalid value (for example an unknown scope).
    """
    parser = argparse.ArgumentParser(prog="app.py", add_help=False)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--scope", choices=get_args(ViewerScope), default=None)
    known, _ = parser.parse_known_args(list(argv))
    return AppArgs(config=known.config, db=known.db, scope=known.scope)


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
    }


def reset_state_keys() -> tuple[str, ...]:
    """Return every session key the Clear button resets (all conditions and filters)."""
    return tuple(reset_values())


def stale_state_fixes(state: Mapping[str, Any], options: FilterOptions) -> dict[str, Any]:
    """Return session-state updates that drop filter values no longer offered as options.

    A crop, topic or year chosen earlier in a session can disappear from the visible data (for
    example after a re-extraction or a source deletion). Its widget would then hold a value
    that is not among its options, so the value is reset rather than kept or guessed.

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
    topics = list(state.get(TOPICS_KEY) or ())
    kept = [topic for topic in topics if topic in options.topics]
    if kept != topics:
        fixes[TOPICS_KEY] = kept
    return fixes


def _search_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"Search text must be a string, got {type(value).__name__}")
    return value.strip() or None


def garden_from_state(state: Mapping[str, Any]) -> GardenContext:
    """Build the viewer's garden from session state (missing keys mean not set).

    Raises
    ------
    pydantic.ValidationError
        If a stored value is not selectable for its condition.
    """
    values = {key: state.get(session_key) for key, session_key in CONDITION_KEYS.items()}
    return GardenContext.model_validate({"region": state.get(REGION_KEY), **values})


def query_from_state(state: Mapping[str, Any]) -> SearchQuery:
    """Build the search query from session state (missing keys mean defaults).

    Raises
    ------
    TypeError
        If the search text is not a string.
    pydantic.ValidationError
        If any other stored value is invalid.
    """
    return SearchQuery.model_validate(
        {
            "text": _search_text(state.get(TEXT_KEY)),
            "crop": state.get(CROP_KEY),
            "topics": tuple(state.get(TOPICS_KEY) or ()),
            "year": state.get(YEAR_KEY),
            "region_filter": state.get(REGION_FILTER_KEY),
            "garden": garden_from_state(state),
            "only_stated_matches": state.get(STRICT_KEY, False),
        }
    )


# --------------------------------------------------------------------------- quick chips


def visible_chips(
    chips: Sequence[QuickChip], support: Mapping[str, int], minimum: int
) -> tuple[QuickChip, ...]:
    """Return the chips supported by at least ``minimum`` visible reports, in config order.

    Raises
    ------
    KeyError
        If ``support`` has no count for a chip.
    """
    return tuple(chip for chip in chips if support[chip.label] >= minimum)


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


def strict_note(result: SearchResult, query: SearchQuery) -> str | None:
    """Explain what "Only show stated matches" hides (or would hide) right now."""
    if not query.garden.selected():
        if query.only_stated_matches:
            return "Choose at least one growing condition first: until then nothing is hidden."
        return None
    if query.only_stated_matches:
        hidden = _plural(result.n_hidden_by_strict, "report")
        return f"{hidden} hidden because their conditions were not stated or differ."
    n_hideable = result.tier_counts["not_stated"] + result.tier_counts["different"]
    return (
        f"Ticking this would hide {_plural(n_hideable, 'report')} whose conditions "
        "were not stated or differ."
    )


def region_filter_note(code: str) -> str:
    """Describe an active reported-state filter."""
    return f"Showing only reports that name {REGION_NAMES[code]} as where they grew."


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


def _reported_labels(dimension: DimensionMatch) -> str:
    return " / ".join(value_label(dimension.key, value) for value in dimension.reported_values)


def _imprecise_item(dimension: DimensionMatch) -> str:
    viewer = value_label(dimension.key, dimension.viewer_value)
    coarse = [value for value in dimension.reported_values if value.isdigit()]
    if dimension.key == "zone" and coarse:
        return f"zone {coarse[0]}, subzone unknown (you: {viewer})"
    return f"{_reported_labels(dimension)} (you: {viewer})"


def _relevant_item(dimension: DimensionMatch) -> str:
    if dimension.status == "match":
        return value_label(dimension.key, dimension.viewer_value)
    return _imprecise_item(dimension)


def _not_stated_item(dimension: DimensionMatch) -> str:
    label = KEY_LABELS[dimension.key]
    if not dimension.reported_values:
        return label
    return f"{label} (only {_reported_labels(dimension)} reported, not comparable)"


def _different_item(dimension: DimensionMatch) -> str:
    viewer = value_label(dimension.key, dimension.viewer_value)
    return f"{_reported_labels(dimension)} (you: {viewer})"


def _match_lines(match: MatchResult) -> tuple[str, ...]:
    dims = match.dimensions
    relevant = [_relevant_item(d) for d in dims if d.status in ("match", "imprecise")]
    unstated = [_not_stated_item(d) for d in dims if d.status == "not_stated"]
    different = [_different_item(d) for d in dims if d.status == "different"]
    lines = (
        f"Relevant: {_ITEM_SEPARATOR.join(relevant)}" if relevant else "",
        f"Not stated: {', '.join(unstated)}" if unstated else "",
        f"Different: {_ITEM_SEPARATOR.join(different)}" if different else "",
    )
    return tuple(line for line in lines if line)


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


def _reported_lines(matches: Sequence[MentionMatch]) -> tuple[str, ...]:
    """List the comparable conditions reported for each mention, merging identical lists."""
    by_items: dict[tuple[str, ...], list[str]] = {}
    for match in matches:
        items = _condition_items(match.facts)
        if items:
            by_items.setdefault(items, []).append(_mention_label(match.mention))
    if not by_items:
        return (NOT_STATED_LINE,)
    if len(matches) == 1:
        (items,) = by_items
        return (f"Reported: {_ITEM_SEPARATOR.join(items)}",)
    return tuple(
        f"Reported for {', '.join(_unique(labels))}: {_ITEM_SEPARATOR.join(items)}"
        for items, labels in by_items.items()
    )


def condition_lines(result: CommentResult, garden: GardenContext) -> tuple[str, ...]:
    """Explain a card's conditions as "Relevant", "Not stated" and "Different" lines.

    Parameters
    ----------
    result : CommentResult
        A displayed comment.
    garden : GardenContext
        The viewer's garden used for the search.

    Returns
    -------
    tuple[str, ...]
        With comparable conditions selected: the best relevant mention's ``Relevant: ...``
        (matches, and imprecise matches with both values), ``Not stated: ...`` and
        ``Different: ...`` lines, each only when non-empty, preceded by ``Shown for ...`` when
        the comment has several relevant mentions. Without a selection: the conditions each
        mention reported, or ``"Growing conditions not stated"``.
    """
    matches = result.mention_matches
    best = _best(matches)
    match = best.match if best is not None else match_context(garden, ())
    if match is None:
        return _reported_lines(matches)
    lines = _match_lines(match)
    if best is not None and len(matches) > 1:
        lead = f"Shown for {_mention_label(best.mention)} (1 of {len(matches)} mentions)"
        return (lead, *lines)
    return lines


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
    """Return the comment's permalink, if it has one.

    Raises
    ------
    ValueError
        If a stored permalink is not an http(s) URL (imports only accept those).
    """
    link = view.permalink
    if link is not None and not link.lower().startswith(("https://", "http://")):
        raise ValueError(f"Comment {view.comment_id} has a permalink that is not http(s)")
    return link


def provenance_note(view: CommentView) -> str | None:
    """Return the note shown instead of a source link (``None`` when a link exists)."""
    if source_link(view) is not None:
        return None
    return SYNTHETIC_NOTE if view.rights_status == "synthetic" else NO_LINK_NOTE


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


def label_badge(label: ContextLabel) -> str:
    """Return a Streamlit Markdown badge for an evidence-type label (SPEC §7 data boundary)."""
    return f":{_BADGE_COLORS[label]}-badge[{label}]"


# --------------------------------------------------------------------------- map


def map_key(region_filter: str | None) -> str:
    """Return the map widget's key, which changes whenever the state filter changes.

    The Plotly chart keeps its own click selection. Re-keying it on every filter change (a map
    click, the state selectbox, "Show all states" or Clear) remounts it without a stale
    selection, so clicking a state always selects it on the first click.
    """
    return f"map_selection_{region_filter or 'all'}"


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
    return {None: "All states", **{code: labels[code] for code in ordered}}


def _cell_label(row: Mapping[Hashable, Any], counts: RegionCounts) -> str:
    return count_label(
        int(row["n_comments"]),
        suppressed=bool(row["suppressed"]),
        min_cell_count=counts.min_cell_count,
    )


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
