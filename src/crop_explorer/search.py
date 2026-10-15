"""Environment-first search: candidate filtering, relevance tiers, groups and honest counts.

Implements SPEC §5. Candidates come from scope-gated SQL/FTS reads in :mod:`crop_explorer.db`;
everything after that is the pure, deterministic :func:`build_result`. Reports are grouped by how
well their *stated* circumstances match the viewer's garden (match, partial, not stated,
different), never by predicted quality: outcome words and comment text play no part in a tier or
in the order. A region is a location preference that sorts same-region reports first within a
group; it never changes a tier. Counts are of distinct comments, never of mentions.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from loguru import logger
from pydantic import BaseModel, ConfigDict

from crop_explorer import db
from crop_explorer.config import QuickChip
from crop_explorer.environment_match import (
    applicable_facts,
    explain,
    match_context,
    region_status,
)
from crop_explorer.normalization import normalize_crop
from crop_explorer.regions import REGION_NAMES
from crop_explorer.schemas import EnvKey, Topic
from crop_explorer.views import (
    GROUP_TITLES,
    TIER_ORDER,
    CommentResult,
    CommentView,
    FactView,
    GardenContext,
    GroupKey,
    MatchResult,
    MentionMatch,
    MentionView,
    RegionStatus,
    ResultGroup,
    SearchQuery,
    SearchResult,
    Tier,
    ViewerScope,
)
from crop_explorer.vocab import COMPARABLE_KEYS, YEAR_PATTERN

__all__ = [
    "FilterOptions",
    "build_result",
    "chip_support",
    "filter_options",
    "relevant_mentions",
    "search",
]

_TIERS: Final[tuple[Tier, ...]] = ("match", "partial", "not_stated", "different")
"""Tiers in :data:`~crop_explorer.views.TIER_ORDER` order (the display order of tier groups)."""
_GROUP_ORDER: Final[tuple[GroupKey, ...]] = ("all", *_TIERS)
_STRICT_HIDDEN: Final[frozenset[GroupKey]] = frozenset({"not_stated", "different"})
"""Groups hidden by "Only show stated matches"."""
_COLLAPSED: Final[frozenset[GroupKey]] = frozenset({"different"})
_REGION_PREFERENCE: Final[tuple[RegionStatus, ...]] = ("same", "elsewhere")
_CONDITION_KEYS: Final[frozenset[EnvKey]] = frozenset((*COMPARABLE_KEYS, "region"))
"""Keys whose normalized facts count as "mentions growing conditions"."""
_UNNAMED_MENTION: Final = "Unidentified variety"

_SortKey = tuple[int, str, int, str]


class FilterOptions(BaseModel):
    """Choices for the Browse filters, drawn only from data visible in the viewer's scope.

    Every tuple is distinct and sorted, so options never reveal invisible rows and never
    change order between runs.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    crops: tuple[str, ...]
    """Normalized crop names of visible mentions."""
    topics: tuple[Topic, ...]
    """Topics of visible mentions."""
    years: tuple[str, ...]
    """Normalized growing years stated by visible facts."""


# --------------------------------------------------------------------------- mention filters


@dataclass(frozen=True)
class _MentionFilter:
    """The validated mention-level filters of a query (``None``/empty means not set)."""

    crop: str | None
    topics: frozenset[Topic]
    year: str | None
    region: str | None

    def is_set(self) -> bool:
        return any((self.crop, self.topics, self.year, self.region))

    def passes(self, mention: MentionView, facts: Sequence[FactView]) -> bool:
        """Return whether one mention satisfies every set filter."""
        return (
            (self.crop is None or mention.crop == self.crop)
            and (not self.topics or not self.topics.isdisjoint(mention.topics))
            and (self.year is None or _states(facts, "growing_year", self.year))
            and (self.region is None or _states(facts, "region", self.region))
        )


def _states(facts: Iterable[FactView], key: EnvKey, value: str) -> bool:
    return any(fact.key == key and fact.normalized_value == value for fact in facts)


def _mention_filter(query: SearchQuery) -> _MentionFilter:
    """Validate and normalize the query's mention-level filters.

    Raises
    ------
    ValueError
        If the year is not a 19xx/20xx year, the region filter is not a known region code or
        the crop is blank.
    """
    if query.year is not None and YEAR_PATTERN.fullmatch(query.year) is None:
        raise ValueError(f"year filter must be a 4-digit 19xx/20xx year, got {query.year!r}")
    if query.region_filter is not None and query.region_filter not in REGION_NAMES:
        raise ValueError(f"Unknown region_filter code {query.region_filter!r}")
    crop = None if query.crop is None else normalize_crop(query.crop)
    return _MentionFilter(crop, frozenset(query.topics), query.year, query.region_filter)


def _relevant(
    view: CommentView, garden: GardenContext, mention_filter: _MentionFilter
) -> tuple[MentionMatch, ...] | None:
    with_facts = ((m, applicable_facts(m, view.mentions)) for m in view.mentions)
    kept = tuple(
        MentionMatch(mention=mention, facts=facts, match=match_context(garden, facts))
        for mention, facts in with_facts
        if mention_filter.passes(mention, facts)
    )
    if mention_filter.is_set() and not kept:
        return None
    return kept


def relevant_mentions(view: CommentView, query: SearchQuery) -> tuple[MentionMatch, ...] | None:
    """Return the comment's mentions that pass the query's mention-level filters, with matches.

    Parameters
    ----------
    view : CommentView
        A visible comment.
    query : SearchQuery
        The viewer's query. ``crop``, ``topics``, ``year`` and ``region_filter`` are
        mention-level filters; ``text`` is applied by the database read and ignored here.

    Returns
    -------
    tuple[MentionMatch, ...] or None
        ``None`` when a mention-level filter is set and no single mention passes all of them.
        Otherwise the passing mentions (all mentions when no filter is set, so a comment
        without mentions gives ``()``), in mention order, each with its applicable facts and
        its match against the garden (``None`` when no comparable condition is selected).

    Raises
    ------
    ValueError
        If a filter value is invalid (see :func:`build_result`).
    """
    return _relevant(view, query.garden, _mention_filter(query))


# --------------------------------------------------------------------------- one comment


def _mention_label(mention: MentionView) -> str:
    return mention.variety_as_written or mention.crop or _UNNAMED_MENTION


def _match_rank(mention_match: MentionMatch) -> int:
    match = mention_match.match
    return 0 if match is None else TIER_ORDER[match.tier]


def _headline(
    matches: Sequence[MentionMatch], garden: GardenContext
) -> tuple[MatchResult | None, str]:
    """Return the best relevant mention's match and the comment's explanation.

    The best mention has the lowest tier rank; ties go to the earlier mention. A comment
    without relevant mentions is compared as if it stated nothing.
    """
    best = min(matches, key=_match_rank, default=None)
    if best is None:
        nothing = match_context(garden, ())
        return nothing, explain(nothing)
    explanation = explain(best.match)
    if len(matches) > 1 and explanation:
        explanation = f"{_mention_label(best.mention)}: {explanation}"
    return best.match, explanation


def _comment_region(
    matches: Sequence[MentionMatch], garden_region: str | None
) -> RegionStatus | None:
    if garden_region is None:
        return None
    found = {region_status(garden_region, mention_match.facts) for mention_match in matches}
    return next((status for status in _REGION_PREFERENCE if status in found), "not_stated")


def _has_conditions(matches: Sequence[MentionMatch]) -> bool:
    return any(
        fact.normalized_value is not None and fact.key in _CONDITION_KEYS
        for mention_match in matches
        for fact in mention_match.facts
    )


def _comment_result(
    view: CommentView, garden: GardenContext, mention_filter: _MentionFilter
) -> CommentResult | None:
    matches = _relevant(view, garden, mention_filter)
    if matches is None:
        return None
    headline, explanation = _headline(matches, garden)
    return CommentResult(
        comment=view,
        tier=None if headline is None else headline.tier,
        mention_matches=matches,
        region_status=_comment_region(matches, garden.region),
        explanation=explanation,
        has_conditions=_has_conditions(matches),
    )


# --------------------------------------------------------------------------- grouping and counts


def _group_key(result: CommentResult) -> GroupKey:
    return "all" if result.tier is None else result.tier


def _display_key(result: CommentResult) -> _SortKey:
    view = result.comment
    region_rank = 0 if result.region_status == "same" else 1
    return (region_rank, view.source_id, view.source_order, view.comment_id)


def _grouped(results: Iterable[CommentResult]) -> dict[GroupKey, list[CommentResult]]:
    """Return the non-empty groups in display order, each sorted for display."""
    members: dict[GroupKey, list[CommentResult]] = {key: [] for key in _GROUP_ORDER}
    for result in sorted(results, key=_display_key):
        members[_group_key(result)].append(result)
    return {key: group for key, group in members.items() if group}


def _result_group(key: GroupKey, results: Sequence[CommentResult]) -> ResultGroup:
    return ResultGroup(
        key=key,
        title=GROUP_TITLES[key],
        results=tuple(results),
        collapsed_by_default=key in _COLLAPSED,
    )


def _tier_counts(results: Sequence[CommentResult], garden: GardenContext) -> dict[str, int]:
    if not garden.selected():
        return {}
    counts = Counter(result.tier for result in results)
    return {tier: counts[tier] for tier in _TIERS}


def _limitation_message(n_with_conditions: int, n_candidates: int) -> str | None:
    """Say plainly how few reports describe growing conditions (``None`` when all or none)."""
    if n_candidates in (0, n_with_conditions):
        return None
    if n_with_conditions == 0 and n_candidates == 1:
        return (
            "The 1 report found does not mention growing conditions, so it cannot be compared "
            "with your garden. It is shown below."
        )
    if n_with_conditions == 0:
        return (
            f"None of these {n_candidates} reports mention growing conditions, so none can be "
            "compared with your garden. All are shown below."
        )
    verb = "mentions" if n_with_conditions == 1 else "mention"
    return f"Only {n_with_conditions} of {n_candidates} reports {verb} growing conditions."


def _assemble(results: Sequence[CommentResult], query: SearchQuery) -> SearchResult:
    hidden = _STRICT_HIDDEN if query.only_stated_matches else frozenset()
    grouped = _grouped(results)
    n_with_conditions = sum(result.has_conditions for result in results)
    return SearchResult(
        groups=tuple(_result_group(k, group) for k, group in grouped.items() if k not in hidden),
        n_candidates=len(results),
        n_with_conditions=n_with_conditions,
        n_hidden_by_strict=sum(len(group) for k, group in grouped.items() if k in hidden),
        tier_counts=_tier_counts(results, query.garden),
        limitation_message=_limitation_message(n_with_conditions, len(results)),
    )


def _require_unique_ids(views: Sequence[CommentView]) -> None:
    counts = Counter(view.comment_id for view in views)
    duplicates = sorted(comment_id for comment_id, n in counts.items() if n > 1)
    if duplicates:
        raise ValueError(f"Duplicate comment_id(s) in search input: {', '.join(duplicates)}")


# --------------------------------------------------------------------------- public API


def build_result(views: Sequence[CommentView], query: SearchQuery) -> SearchResult:
    """Filter, compare, group and count candidate comments (pure, deterministic core).

    Parameters
    ----------
    views : Sequence[CommentView]
        Visible candidate comments, already text-filtered by the caller (``query.text`` is not
        applied here). Their order does not matter.
    query : SearchQuery
        Mention-level filters, the viewer's garden and the strict-matching toggle.

    Returns
    -------
    SearchResult
        Without a selected comparable condition, one ``all`` group; otherwise the non-empty
        groups ``match``, ``partial``, ``not_stated`` and ``different`` (collapsed by default),
        in that order. Within a group, same-region reports come first, then
        ``(source_id, source_order, comment_id)``. ``only_stated_matches`` hides the
        ``not_stated`` and ``different`` groups and counts their comments in
        ``n_hidden_by_strict``; without a selected condition there is nothing to match, so it
        hides nothing. ``tier_counts`` (empty without a selected condition),
        ``n_candidates`` and ``n_with_conditions`` count distinct comments before hiding.

    Raises
    ------
    ValueError
        If two views share a ``comment_id``, the year filter is not a 19xx/20xx year, the
        region filter is not a known region code, or the crop filter is blank.
    """
    mention_filter = _mention_filter(query)
    _require_unique_ids(views)
    results = [
        result
        for view in views
        if (result := _comment_result(view, query.garden, mention_filter)) is not None
    ]
    return _assemble(results, query)


def search(
    conn: sqlite3.Connection, scope: ViewerScope, query: SearchQuery, now: str
) -> SearchResult:
    """Run a viewer search over the comments visible in ``scope`` at ``now``.

    Parameters
    ----------
    conn : sqlite3.Connection
        Open database connection.
    scope : ViewerScope
        Viewer scope; rights and expiry gating happen in every database read.
    query : SearchQuery
        The viewer's query; ``query.text`` is a sanitized full-text filter that also finds
        comments that were never extracted.
    now : str
        Current time in canonical UTC form.

    Returns
    -------
    SearchResult
        See :func:`build_result`.

    Raises
    ------
    ValueError
        If ``scope``, ``now`` or a filter value is invalid.
    """
    ids = db.visible_comment_ids(conn, scope, now, query.text)
    result = build_result(db.load_comment_views(conn, scope, now, ids), query)
    logger.debug(
        "Search in {} scope: {} candidate(s), {} with conditions, {} hidden by strict mode",
        scope,
        result.n_candidates,
        result.n_with_conditions,
        result.n_hidden_by_strict,
    )
    return result


def _stated_pairs(view: CommentView) -> frozenset[tuple[str, str]]:
    """Return every ``(key, normalized_value)`` applicable to some mention of the comment."""
    return frozenset(
        (fact.key, fact.normalized_value)
        for mention in view.mentions
        for fact in applicable_facts(mention, view.mentions)
        if fact.normalized_value is not None
    )


def _require_unique_labels(chips: Sequence[QuickChip]) -> None:
    counts = Counter(chip.label for chip in chips)
    duplicates = sorted(label for label, n in counts.items() if n > 1)
    if duplicates:
        raise ValueError(f"Duplicate quick-chip label(s): {', '.join(duplicates)}")


def chip_support(
    conn: sqlite3.Connection, scope: ViewerScope, now: str, chips: Sequence[QuickChip]
) -> dict[str, int]:
    """Count the visible reports that support each quick-condition chip.

    Parameters
    ----------
    conn : sqlite3.Connection
        Open database connection.
    scope : ViewerScope
        Viewer scope; invisible comments never contribute.
    now : str
        Current time in canonical UTC form.
    chips : Sequence[QuickChip]
        Chips to count.

    Returns
    -------
    dict[str, int]
        ``label -> n`` in chip order, where ``n`` is the number of distinct visible comments
        having at least one mention whose applicable facts state any of the chip's
        ``(key, value)`` conditions.

    Raises
    ------
    ValueError
        If two chips share a label, or ``scope`` or ``now`` is invalid.
    """
    _require_unique_labels(chips)
    stated = [_stated_pairs(view) for view in db.load_comment_views(conn, scope, now, None)]
    support: dict[str, int] = {}
    for chip in chips:
        wanted = frozenset(chip.conditions.items())
        support[chip.label] = sum(not wanted.isdisjoint(pairs) for pairs in stated)
    return support


def filter_options(conn: sqlite3.Connection, scope: ViewerScope, now: str) -> FilterOptions:
    """List the crops, topics and growing years present in data visible in ``scope``.

    Parameters
    ----------
    conn : sqlite3.Connection
        Open database connection.
    scope : ViewerScope
        Viewer scope; values that only occur in invisible comments are never listed.
    now : str
        Current time in canonical UTC form.

    Returns
    -------
    FilterOptions
        Distinct, sorted crops, topics and years.

    Raises
    ------
    ValueError
        If ``scope`` or ``now`` is invalid.
    """
    mentions = [m for view in db.load_comment_views(conn, scope, now, None) for m in view.mentions]
    years = {
        fact.normalized_value
        for mention in mentions
        for fact in mention.facts
        if fact.key == "growing_year" and fact.normalized_value is not None
    }
    return FilterOptions(
        crops=tuple(sorted({m.crop for m in mentions if m.crop is not None})),
        topics=tuple(sorted({topic for m in mentions for topic in m.topics})),
        years=tuple(sorted(years)),
    )
