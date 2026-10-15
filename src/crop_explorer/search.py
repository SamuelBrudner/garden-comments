"""Environment-first search: candidate filtering, relevance tiers, groups and honest counts.

Implements SPEC §5. Candidates come from scope-gated SQL/FTS reads in :mod:`crop_explorer.db`;
everything after that is the pure, deterministic :func:`build_result`. Reports are grouped by how
well their *stated* circumstances match the viewer's garden (match, partial, not stated,
different), never by predicted quality: outcome words and comment text play no part in a tier or
in the order. A search text that names a variety or crop makes those mentions the ones compared
(SPEC §4: the variety mention is the query unit); a crop, topic, year or state filter only narrows
them, so it never lets another variety's conditions decide a card's group (SPEC §11 #2). A region
is a location preference that sorts same-region (then same-country) reports first within a group;
it never changes a tier and never counts as a comparable condition. Counts are of distinct
comments, never of mentions.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
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
from crop_explorer.normalization import crops_named, normalize_crop
from crop_explorer.regions import REGION_NAMES
from crop_explorer.schemas import EnvKey, Topic
from crop_explorer.views import (
    GROUP_TITLES,
    TIER_ORDER,
    ChipSupport,
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
_REGION_PREFERENCE: Final[tuple[RegionStatus, ...]] = ("same", "same_country", "elsewhere")
"""A comment's region status is the first of these that any relevant mention has."""
_REGION_RANKS: Final[Mapping[RegionStatus | None, int]] = MappingProxyType(
    {"same": 0, "same_country": 1}
)
_OTHER_REGION_RANK: Final = 2
"""Display rank of ``elsewhere``, ``not_stated`` and no garden region."""
_CONDITION_KEYS: Final[frozenset[EnvKey]] = frozenset(COMPARABLE_KEYS)
"""Keys whose normalized facts count as "mentions comparable growing conditions". A region is a
location preference that is never compared, so a report stating only a region does not count."""
_UNNAMED_MENTION: Final = "Unidentified variety"
_SEARCH_WORD: Final = re.compile(r"[^\W_]+")
"""A word of search text or of a name: letters/digits; anything else separates words."""

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
class _TextNames:
    """The search text's words, computed once per query so each mention is a cheap lookup."""

    joined: str
    """The words joined by single spaces, with a space at each end."""
    crops: frozenset[str]
    """The normalized crops the words name (:func:`~crop_explorer.normalization.crops_named`)."""


def _text_names(text: str) -> _TextNames:
    words = _words(text)
    return _TextNames(f" {' '.join(words)} ", crops_named(" ".join(words)))


@dataclass(frozen=True)
class _MentionFilter:
    """The validated mention-level filters of a query (``None``/empty means not set)."""

    crop: str | None
    topics: frozenset[Topic]
    year: str | None
    region: str | None
    text: _TextNames
    """What the search text names; it picks the relevant mentions among those passing the rest."""

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


def _words(text: str) -> tuple[str, ...]:
    """Casefold, drop diacritics and split into words: ``"Jalapeño-ish"`` -> jalapeno, ish."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return tuple(_SEARCH_WORD.findall(plain))


@lru_cache(maxsize=4096)
def _name_pattern(name: str) -> re.Pattern[str] | None:
    """Match a name's letters and digits as whole consecutive words of :attr:`_TextNames.joined`.

    A word may break anywhere inside the name, so spellings that share a
    :func:`~crop_explorer.normalization.variety_key` name each other: ``"Sun Gold"``,
    ``"Sun-Gold"`` and ``"sungold"``. Each optional space precedes a letter or digit, so the
    pattern never backtracks and a search is linear in the text. ``None`` when nothing is left.
    """
    letters = "".join(_words(name))
    return re.compile(f" {' ?'.join(map(re.escape, letters))} ") if letters else None


def _names(text: _TextNames, mention: MentionView) -> bool:
    """Whether the search text names the mention's whole variety name, or its crop."""
    variety = mention.variety_as_written
    pattern = None if variety is None else _name_pattern(variety)
    if pattern is not None and pattern.search(text.joined):
        return True
    crop = () if mention.crop is None else _words(mention.crop)
    return bool(crop) and normalize_crop(" ".join(crop)) in text.crops


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
    text = _text_names(query.text or "")
    return _MentionFilter(crop, frozenset(query.topics), query.year, query.region_filter, text)


def _relevant(
    view: CommentView, garden: GardenContext, mention_filter: _MentionFilter
) -> tuple[MentionMatch, ...] | None:
    with_facts = ((m, applicable_facts(m, view.mentions)) for m in view.mentions)
    kept = tuple(
        MentionMatch(mention=mention, facts=facts, match=match_context(garden, facts))
        for mention, facts in with_facts
        if mention_filter.passes(mention, facts)
    )
    if not kept and mention_filter.is_set():
        return None
    named = tuple(match for match in kept if _names(mention_filter.text, match.mention))
    return named or kept


def relevant_mentions(view: CommentView, query: SearchQuery) -> tuple[MentionMatch, ...] | None:
    """Return the comment's mentions that pass the query's mention-level filters, with matches.

    Parameters
    ----------
    view : CommentView
        A visible comment.
    query : SearchQuery
        The viewer's query. ``crop``, ``topics``, ``year`` and ``region_filter`` are
        mention-level filters. ``text`` picks candidates in the database read; here it only
        chooses among the mentions that pass those filters, so a filter only ever narrows the
        mentions the text names and never brings back one it did not name.

    Returns
    -------
    tuple[MentionMatch, ...] or None
        ``None`` when a mention-level filter is set and no single mention passes all of them.
        Otherwise, in mention order and each with its applicable facts and its match against
        the garden (``None`` when no comparable condition is selected): the passing mentions
        whose whole variety name or crop (by :func:`~crop_explorer.normalization.mentions_crop`)
        ``text`` names as consecutive words (casefolded, diacritics ignored; a variety's words
        may be split or joined, so "Sun Gold" and "sungold" name each other), or every passing
        mention when ``text`` names none of them (so, without filters, a comment without
        mentions gives ``()``).

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
    region_rank = _REGION_RANKS.get(result.region_status, _OTHER_REGION_RANK)
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


@dataclass(frozen=True)
class _Coverage:
    """Distinct-comment counts behind the limitation message."""

    n: int
    """Candidates."""
    n_with: int
    """Candidates stating a comparable condition (a region alone does not count)."""
    n_new: int
    """Candidates never analysed (``parse_status is None``); disjoint from ``n_with``."""
    n_hidden: int
    """Candidates hidden by "Only show stated matches"."""


def _coverage(
    results: Sequence[CommentResult],
    grouped: Mapping[GroupKey, Sequence[CommentResult]],
    hidden: frozenset[GroupKey],
) -> _Coverage:
    return _Coverage(
        n=len(results),
        n_with=sum(result.has_conditions for result in results),
        n_new=sum(result.comment.parse_status is None for result in results),
        n_hidden=sum(len(group) for key, group in grouped.items() if key in hidden),
    )


_HIDDEN_BY_STRICT: Final = 'hidden by "Only show stated matches"'
_ONE_NOT_ANALYSED: Final = (
    "The 1 report found has not been analysed for growing conditions yet, so it cannot be "
    "compared with your garden."
)
_ONE_NOT_COMPARABLE: Final = (
    "The 1 report found does not mention comparable growing conditions, so it cannot be "
    "compared with your garden."
)
_NONE_ANALYSED: Final = (
    "None of these {n} reports have been analysed for growing conditions yet, so none can be "
    "compared with your garden."
)
_NONE_COMPARABLE: Final = (
    "None of these {n} reports mention comparable growing conditions, so none can be compared "
    "with your garden."
)
_SOME_NOT_ANALYSED: Final = (
    "None of these {n} reports can be compared with your garden: {stated} not mention "
    "comparable growing conditions and {new} not been analysed yet."
)


def _count(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _uncomparable(coverage: _Coverage) -> str:
    """Say why no candidate can be compared, never calling an unanalysed one condition-free."""
    n, n_new = coverage.n, coverage.n_new
    if n == 1:
        return _ONE_NOT_ANALYSED if n_new else _ONE_NOT_COMPARABLE
    if n_new in (0, n):
        return (_NONE_ANALYSED if n_new else _NONE_COMPARABLE).format(n=n)
    return _SOME_NOT_ANALYSED.format(
        n=n, stated=_count(n - n_new, "does", "do"), new=_count(n_new, "has", "have")
    )


def _where_shown(coverage: _Coverage) -> str:
    """Say where the candidates are: below, or hidden by strict mode (never both claimed)."""
    n, n_hidden = coverage.n, coverage.n_hidden
    if n_hidden == 0:
        return "It is shown below." if n == 1 else "All are shown below."
    if n == 1:
        return f"It is {_HIDDEN_BY_STRICT}; untick it to see it."
    who = f"All {n} are" if n_hidden == n else _count(n_hidden, "of them is", "of them are")
    return f"{who} {_HIDDEN_BY_STRICT}; untick it to see {'it' if n_hidden == 1 else 'them'}."


def _limitation_message(coverage: _Coverage) -> str | None:
    """Say plainly how few reports describe comparable conditions (``None`` when all do).

    Unanalysed reports are counted apart and never said to lack conditions; reports hidden by
    "Only show stated matches" are never said to be shown below.
    """
    if coverage.n in (0, coverage.n_with):
        return None
    if coverage.n_with == 0:
        return f"{_uncomparable(coverage)} {_where_shown(coverage)}"
    verb = "mentions" if coverage.n_with == 1 else "mention"
    pending = f" ({coverage.n_new} not yet analysed)" if coverage.n_new else ""
    return (
        f"Only {coverage.n_with} of {coverage.n} reports {verb} comparable growing conditions"
        f"{pending}."
    )


def _assemble(results: Sequence[CommentResult], query: SearchQuery) -> SearchResult:
    hidden = _STRICT_HIDDEN if query.only_stated_matches else frozenset()
    grouped = _grouped(results)
    coverage = _coverage(results, grouped, hidden)
    return SearchResult(
        groups=tuple(_result_group(k, group) for k, group in grouped.items() if k not in hidden),
        n_candidates=coverage.n,
        n_with_conditions=coverage.n_with,
        n_not_analysed=coverage.n_new,
        n_hidden_by_strict=coverage.n_hidden,
        tier_counts=_tier_counts(results, query.garden),
        limitation_message=_limitation_message(coverage),
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
        Visible candidate comments, already text-filtered by the caller. Their order does not
        matter.
    query : SearchQuery
        Mention-level filters, the search text (which only chooses the relevant mentions, see
        :func:`relevant_mentions`), the viewer's garden and the strict-matching toggle.

    Returns
    -------
    SearchResult
        Without a selected comparable condition, one ``all`` group; otherwise the non-empty
        groups ``match``, ``partial``, ``not_stated`` and ``different`` (collapsed by default),
        in that order. Within a group, same-region reports come first, then same-country
        ones, then ``(source_id, source_order, comment_id)``. ``only_stated_matches`` hides
        the ``not_stated`` and ``different`` groups and counts their comments in
        ``n_hidden_by_strict``; without a selected condition there is nothing to match, so it
        hides nothing. ``tier_counts`` (empty without a selected condition),
        ``n_candidates``, ``n_with_conditions`` and ``n_not_analysed`` count distinct comments
        before hiding.

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


_Pairs = frozenset[tuple[str, str]]


def _mention_pairs(view: CommentView) -> tuple[_Pairs, ...]:
    """Return, per mention, every applicable ``(key, normalized_value)``."""
    return tuple(
        frozenset(
            (fact.key, fact.normalized_value)
            for fact in applicable_facts(mention, view.mentions)
            if fact.normalized_value is not None
        )
        for mention in view.mentions
    )


def _support(chip: QuickChip, comments: Sequence[tuple[_Pairs, ...]]) -> ChipSupport:
    wanted = frozenset(chip.conditions.items())
    return ChipSupport(
        n_any=sum(any(not wanted.isdisjoint(pairs) for pairs in c) for c in comments),
        n_all=sum(any(wanted <= pairs for pairs in c) for c in comments),
    )


def _require_unique_labels(chips: Sequence[QuickChip]) -> None:
    counts = Counter(chip.label for chip in chips)
    duplicates = sorted(label for label, n in counts.items() if n > 1)
    if duplicates:
        raise ValueError(f"Duplicate quick-chip label(s): {', '.join(duplicates)}")


def chip_support(
    conn: sqlite3.Connection, scope: ViewerScope, now: str, chips: Sequence[QuickChip]
) -> dict[str, ChipSupport]:
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
    dict[str, ChipSupport]
        ``label -> support`` in chip order. ``n_any`` is the number of distinct visible
        comments having a mention whose applicable facts state any of the chip's
        ``(key, value)`` conditions (the display gate); ``n_all`` the number having a single
        mention whose applicable facts state all of them (the reports the chip would match).
        A heatwave never supports "hot".

    Raises
    ------
    ValueError
        If two chips share a label, or ``scope`` or ``now`` is invalid.
    """
    _require_unique_labels(chips)
    comments = [_mention_pairs(view) for view in db.load_comment_views(conn, scope, now, None)]
    return {chip.label: _support(chip, comments) for chip in chips}


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
