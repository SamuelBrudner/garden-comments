"""At-a-glance statistics over the visible comments (pure, no I/O).

Feeds the viewer's "At a glance" header: how many reports there are, which varieties people
talk about, what they talk about and how they say they grow. Every count is a number of
**distinct comments** (a comment naming Sungold twice counts once for Sungold), computed only
from the views passed in. The viewer passes the views it read through the scope-gated database
reads, so a public view never counts a ``private_unverified`` comment.

Counts measure conversation, never quality: a failed crop counts exactly like a great one,
outcome words play no part, and nothing here scores, rates or recommends a variety. Tables are
tidy pandas DataFrames with fixed columns and a deterministic order (stable tie-breaks), so
equal input always gives equal output whatever the order of the views.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal, Self

import pandas as pd
from pydantic import BaseModel, ConfigDict, NonNegativeInt, model_validator

from crop_explorer.config import OverviewConfig
from crop_explorer.environment_match import applicable_facts
from crop_explorer.regions import is_us_state
from crop_explorer.schemas import TOPICS, Topic
from crop_explorer.views import CommentView, FactView, MentionView
from crop_explorer.vocab import (
    CATEGORICAL_VALUES,
    COMPARABLE_KEYS,
    is_valid_normalized,
    value_label,
)

__all__ = [
    "SETUP_COLUMNS",
    "SETUP_KEYS",
    "TOPIC_COLUMNS",
    "VARIETY_COLUMNS",
    "Headline",
    "Overview",
    "SetupKey",
    "headline",
    "n_stating_setup",
    "setup_counts",
    "summarize",
    "tied_after",
    "topic_matrix",
    "variety_counts",
]

SetupKey = Literal["method", "shelter"]
SETUP_KEYS: Final[tuple[SetupKey, ...]] = ("method", "shelter")
"""The "How people grow" facets: growing method, then setting (``shelter``)."""

_VARIETY_DTYPES: Final = MappingProxyType(
    {"variety_key": "str", "label": "str", "crop": "object", "n_comments": "int64"}
)
_TOPIC_DTYPES: Final = MappingProxyType(
    {"variety_key": "str", "label": "str", "topic": "str", "n_comments": "int64"}
)
_SETUP_DTYPES: Final = MappingProxyType(
    {"key": "str", "value": "str", "label": "str", "n_comments": "int64"}
)
VARIETY_COLUMNS: Final = tuple(_VARIETY_DTYPES)
"""Columns of :func:`variety_counts` (``crop`` is ``None`` when no mention names one)."""
TOPIC_COLUMNS: Final = tuple(_TOPIC_DTYPES)
"""Columns of :func:`topic_matrix`."""
SETUP_COLUMNS: Final = tuple(_SETUP_DTYPES)
"""Columns of :func:`setup_counts`."""

_SETUP_KEY_OF: Final[Mapping[str, SetupKey]] = MappingProxyType({key: key for key in SETUP_KEYS})
_CONDITION_KEYS: Final = frozenset(COMPARABLE_KEYS)
"""Keys that make a report "state growing conditions" (search's ``has_conditions``)."""
_WHOLE_COUNTRY: Final = "US"
"""The United States as a whole: not a place of its own beside the states."""
_LAST_TOPIC: Final[Topic] = "general"
"""The catch-all topic, always the grid's last column: it says the least about a variety."""
_FROZEN = ConfigDict(extra="forbid", frozen=True)


class Headline(BaseModel):
    """The KPI numbers of the header, all counts of distinct visible comments or places.

    Attributes
    ----------
    n_reports
        Visible comments, analysed or not.
    n_varieties
        Distinct variety keys named by any mention (a crop alone is not a variety).
    n_crops
        Distinct crops of the mentions that name a variety ("44 varieties across 9 crops").
    n_with_conditions
        Comments with an applicable fact giving a normalized value for a comparable key (the
        definition of ``CommentResult.has_conditions``); a region or a year alone does not count.
    n_us_states
        Distinct U.S. states (and DC) named by normalized region facts.
    n_other_regions
        Distinct Canadian provinces and countries other than the United States named the same
        way. "United States" alone is not counted as a place.
    """

    model_config = _FROZEN

    n_reports: NonNegativeInt
    n_varieties: NonNegativeInt
    n_crops: NonNegativeInt
    n_with_conditions: NonNegativeInt
    n_us_states: NonNegativeInt
    n_other_regions: NonNegativeInt

    @model_validator(mode="after")
    def _conditions_within_reports(self) -> Self:
        if self.n_with_conditions > self.n_reports:
            raise ValueError(
                f"n_with_conditions ({self.n_with_conditions}) cannot exceed n_reports "
                f"({self.n_reports})"
            )
        return self

    @property
    def n_places(self) -> int:
        """Distinct places named: U.S. states plus places outside the U.S."""
        return self.n_us_states + self.n_other_regions


@dataclass(frozen=True, eq=False)
class Overview:
    """Everything the "At a glance" header shows, computed once per viewer run.

    Attributes
    ----------
    headline : Headline
        The KPI numbers.
    all_varieties : pandas.DataFrame
        :func:`variety_counts` of every visible variety (names for the variety filter).
    varieties : pandas.DataFrame
        The first ``OverviewConfig.top_varieties`` rows of ``all_varieties``.
    topics : pandas.DataFrame
        :func:`topic_matrix` of the first ``OverviewConfig.heatmap_varieties`` of
        ``varieties``, in the same order.
    setups : pandas.DataFrame
        :func:`setup_counts`.
    n_with_setup : int
        :func:`n_stating_setup`.
    bars_tied_off : int
        :func:`tied_after` the bar chart: varieties left out although they have as many
        comments as its last bar.
    rows_tied_off : int
        The same for the topic grid's rows.
    """

    headline: Headline
    all_varieties: pd.DataFrame
    varieties: pd.DataFrame
    topics: pd.DataFrame
    setups: pd.DataFrame
    n_with_setup: int
    bars_tied_off: int
    rows_tied_off: int


@dataclass
class _Tally:
    """Distinct comments, and per-comment spellings and crops, of one variety key."""

    comments: set[str] = field(default_factory=set)
    spellings: set[tuple[str, str]] = field(default_factory=set)
    crops: set[tuple[str, str]] = field(default_factory=set)

    def add(self, comment_id: str, mention: MentionView) -> None:
        self.comments.add(comment_id)
        if mention.variety_as_written is not None:
            self.spellings.add((comment_id, mention.variety_as_written))
        if mention.crop is not None:
            self.crops.add((comment_id, mention.crop))


@dataclass(frozen=True)
class _Variety:
    key: str
    label: str
    crop: str | None
    n_comments: int


# --------------------------------------------------------------------------- public API


def headline(views: Sequence[CommentView]) -> Headline:
    """Count reports, varieties, reports stating conditions and places named.

    Parameters
    ----------
    views : Sequence[CommentView]
        Visible comments with distinct ids, in any order.

    Returns
    -------
    Headline
        The KPI numbers (see :class:`Headline`).

    Raises
    ------
    ValueError
        If two views share a ``comment_id`` or a region fact carries a normalized value outside
        the region vocabulary (the message names the comment, never the value).
    """
    _require_unique_ids(views)
    places = {code for view in views for code in _region_codes(view)} - {_WHOLE_COUNTRY}
    n_states = sum(is_us_state(code) for code in places)
    return Headline(
        n_reports=len(views),
        n_varieties=len({key for view in views for key in _variety_keys(view)}),
        n_crops=len({crop for view in views for crop in _variety_crops(view)}),
        n_with_conditions=sum(_states_conditions(view) for view in views),
        n_us_states=n_states,
        n_other_regions=len(places) - n_states,
    )


def variety_counts(views: Sequence[CommentView], top_n: int | None) -> pd.DataFrame:
    """Count the distinct comments naming each variety; keep the ``top_n`` most-discussed.

    Parameters
    ----------
    views : Sequence[CommentView]
        Visible comments with distinct ids, in any order.
    top_n : int or None
        Rows to keep (at least 1), or ``None`` for every variety. The total number of
        varieties is :attr:`Headline.n_varieties`; :func:`tied_after` says how many varieties
        beyond the cut have as many comments as the last row (ties are cut alphabetically).

    Returns
    -------
    pandas.DataFrame
        Columns :data:`VARIETY_COLUMNS`. ``label`` is the spelling (``variety_as_written``)
        used by the most comments naming the key, ties alphabetical (casefolded); ``crop`` is
        the crop named by the most of those comments, ties alphabetical, ``None`` when none
        names one. Sorted by ``n_comments`` descending, then label (casefolded), then key.

    Raises
    ------
    ValueError
        If ``top_n < 1`` or two views share a ``comment_id``.
    """
    if top_n is not None and top_n < 1:
        raise ValueError(f"top_n must be at least 1, got {top_n}")
    _require_unique_ids(views)
    rows = [(v.key, v.label, v.crop, v.n_comments) for v in _varieties(views)[:top_n]]
    return _frame(_VARIETY_DTYPES, rows)


def tied_after(varieties: pd.DataFrame, n: int) -> int:
    """Return how many varieties after the first ``n`` rows have as many comments as row ``n``.

    Equal varieties are never ranked: when a cut at ``n`` splits a group of equal counts, the
    viewer says how many were left out and that ties are listed alphabetically.

    Parameters
    ----------
    varieties : pandas.DataFrame
        :func:`variety_counts` output (sorted most-discussed first), normally of every variety.
    n : int
        Rows shown (at least 1).

    Raises
    ------
    ValueError
        If ``n < 1``.
    """
    if n < 1:
        raise ValueError(f"The cut must be at least 1 row, got {n}")
    counts = [int(count) for count in varieties["n_comments"]]
    if len(counts) <= n:
        return 0
    return sum(count == counts[n - 1] for count in counts[n:])


def topic_matrix(views: Sequence[CommentView], variety_keys: Sequence[str]) -> pd.DataFrame:
    """Count, per variety and topic, the distinct comments whose mention of it has that topic.

    Parameters
    ----------
    views : Sequence[CommentView]
        Visible comments with distinct ids, in any order.
    variety_keys : Sequence[str]
        The rows, in display order (normally the first rows of :func:`variety_counts`).

    Returns
    -------
    pandas.DataFrame
        Columns :data:`TOPIC_COLUMNS`, one row per (variety, topic) for every topic that occurs
        on a mention of one of the given varieties, zero cells included. Rows follow
        ``variety_keys``, then topics by total comments across the grid descending, ties in the
        canonical :data:`~crop_explorer.schemas.TOPICS` order, except that the catch-all
        ``general`` is always last. ``label`` is the variety's
        :func:`variety_counts` label.

    Raises
    ------
    ValueError
        If two views share a ``comment_id``, or a key is repeated or names no visible variety.
    """
    _require_unique_ids(views)
    known = {variety.key: variety for variety in _varieties(views)}
    _check_variety_keys(variety_keys, known)
    cells = _topic_cells(views, frozenset(variety_keys))
    topics = _topic_order(cells)
    rows = [(k, known[k].label, t, cells[(k, t)]) for k in variety_keys for t in topics]
    return _frame(_TOPIC_DTYPES, rows)


def setup_counts(views: Sequence[CommentView]) -> pd.DataFrame:
    """Count the distinct comments stating each growing method and setting.

    A comment counts for a value when the applicable facts of one of its mentions
    (:func:`~crop_explorer.environment_match.applicable_facts`, so ``whole_comment`` facts
    reach every sibling) give that normalized value. Display-only facts never count.

    Parameters
    ----------
    views : Sequence[CommentView]
        Visible comments with distinct ids, in any order.

    Returns
    -------
    pandas.DataFrame
        Columns :data:`SETUP_COLUMNS`: ``key`` (``"method"`` or ``"shelter"``), ``value``,
        ``label`` (:func:`~crop_explorer.vocab.value_label`, capitalised, e.g. "Containers")
        and ``n_comments``; only values stated at least once. Sorted by key (method first),
        then ``n_comments`` descending, then vocabulary order.

    Raises
    ------
    ValueError
        If two views share a ``comment_id`` or a method/setting value is outside the
        vocabulary.
    """
    _require_unique_ids(views)
    counts = Counter(pair for view in views for pair in _setup_pairs(view))
    rows = [
        (key, value, _capitalize(value_label(key, value)), counts[(key, value)])
        for key, value in sorted(counts, key=lambda pair: _setup_rank(pair, counts))
    ]
    return _frame(_SETUP_DTYPES, rows)


def n_stating_setup(views: Sequence[CommentView]) -> int:
    """Return how many distinct comments state a growing method or a setting.

    Raises
    ------
    ValueError
        If two views share a ``comment_id`` or a method/setting value is outside the
        vocabulary.
    """
    _require_unique_ids(views)
    return sum(bool(_setup_pairs(view)) for view in views)


def summarize(views: Sequence[CommentView], cfg: OverviewConfig) -> Overview:
    """Compute everything the header shows, with the chart sizes from ``cfg``.

    The topic grid's rows are the first ``cfg.heatmap_varieties`` varieties of the bar chart,
    in the same order.

    Raises
    ------
    ValueError
        As :func:`headline`, :func:`variety_counts`, :func:`topic_matrix` and
        :func:`setup_counts`.
    """
    every = variety_counts(views, None)
    keys = [str(key) for key in every["variety_key"].head(cfg.heatmap_varieties)]
    return Overview(
        headline=headline(views),
        all_varieties=every,
        varieties=every.head(cfg.top_varieties),
        topics=topic_matrix(views, keys),
        setups=setup_counts(views),
        n_with_setup=n_stating_setup(views),
        bars_tied_off=tied_after(every, cfg.top_varieties),
        rows_tied_off=tied_after(every, cfg.heatmap_varieties),
    )


# --------------------------------------------------------------------------- helpers


def _require_unique_ids(views: Sequence[CommentView]) -> None:
    counts = Counter(view.comment_id for view in views)
    duplicates = sorted(comment_id for comment_id, n in counts.items() if n > 1)
    if duplicates:
        raise ValueError(f"Duplicate comment_id(s) in overview input: {', '.join(duplicates)}")


def _frame(dtypes: Mapping[str, str], rows: Sequence[tuple[object, ...]]) -> pd.DataFrame:
    """Build a DataFrame with exactly these columns and dtypes (also when there are no rows)."""
    columns = list(zip(*rows, strict=True)) if rows else [()] * len(dtypes)
    return pd.DataFrame(
        {
            name: pd.Series(list(values), dtype=dtype)
            for (name, dtype), values in zip(dtypes.items(), columns, strict=True)
        }
    )


def _capitalize(text: str) -> str:
    return text[:1].upper() + text[1:]


def _comment_facts(view: CommentView) -> Iterator[FactView]:
    """Yield every fact applicable to one of the comment's mentions (repeats possible)."""
    for mention in view.mentions:
        yield from applicable_facts(mention, view.mentions)


def _states_conditions(view: CommentView) -> bool:
    return any(
        fact.key in _CONDITION_KEYS and fact.normalized_value is not None
        for fact in _comment_facts(view)
    )


def _region_codes(view: CommentView) -> set[str]:
    codes = {
        fact.normalized_value
        for fact in _comment_facts(view)
        if fact.key == "region" and fact.normalized_value is not None
    }
    if any(not is_valid_normalized("region", code) for code in codes):
        raise ValueError(
            f"Comment {view.comment_id}: a region fact has a normalized value outside the "
            "region vocabulary"
        )
    return codes


def _variety_keys(view: CommentView) -> set[str]:
    return {mention.variety_key for mention in view.mentions if mention.variety_key is not None}


def _variety_crops(view: CommentView) -> set[str]:
    return {
        mention.crop
        for mention in view.mentions
        if mention.variety_key is not None and mention.crop is not None
    }


def _most_common(pairs: Iterable[tuple[str, str]]) -> str | None:
    """Return the value most ``(comment_id, value)`` pairs carry, ties alphabetical."""
    counts = Counter(value for _, value in pairs)
    return min(counts, key=lambda value: (-counts[value], value.casefold(), value), default=None)


def _tallies(views: Iterable[CommentView]) -> dict[str, _Tally]:
    tallies: dict[str, _Tally] = {}
    for view in views:
        for mention in view.mentions:
            if mention.variety_key is not None:
                tallies.setdefault(mention.variety_key, _Tally()).add(view.comment_id, mention)
    return tallies


def _varieties(views: Iterable[CommentView]) -> list[_Variety]:
    """Every visible variety, most-discussed first (ties: label casefolded, label, key)."""
    found = [
        _Variety(
            key,
            _most_common(tally.spellings) or key,
            _most_common(tally.crops),
            len(tally.comments),
        )
        for key, tally in _tallies(views).items()
    ]
    return sorted(found, key=lambda v: (-v.n_comments, v.label.casefold(), v.label, v.key))


def _check_variety_keys(keys: Sequence[str], known: Mapping[str, _Variety]) -> None:
    repeated = sorted(key for key, n in Counter(keys).items() if n > 1)
    if repeated:
        raise ValueError(f"Variety key(s) given more than once: {', '.join(repeated)}")
    unknown = [key for key in keys if key not in known]
    if unknown:
        raise ValueError(f"Variety key(s) not a visible variety: {', '.join(unknown)}")


def _topic_cells(views: Iterable[CommentView], keys: frozenset[str]) -> Counter[tuple[str, Topic]]:
    """Count distinct comments per (variety key, topic) among the given keys."""
    found = {
        (view.comment_id, mention.variety_key, topic)
        for view in views
        for mention in view.mentions
        if mention.variety_key is not None and mention.variety_key in keys
        for topic in mention.topics
    }
    return Counter((key, topic) for _, key, topic in found)


def _topic_order(cells: Counter[tuple[str, Topic]]) -> tuple[Topic, ...]:
    totals: Counter[Topic] = Counter()
    for (_, topic), n_comments in cells.items():
        totals[topic] += n_comments
    return tuple(
        sorted(
            totals,
            key=lambda topic: (topic == _LAST_TOPIC, -totals[topic], TOPICS.index(topic)),
        )
    )


def _setup_pairs(view: CommentView) -> set[tuple[SetupKey, str]]:
    """Return the comment's applicable ``(method|shelter, normalized value)`` pairs."""
    pairs: set[tuple[SetupKey, str]] = set()
    for fact in _comment_facts(view):
        key = _SETUP_KEY_OF.get(fact.key)
        if key is None or fact.normalized_value is None:
            continue
        if fact.normalized_value not in CATEGORICAL_VALUES[key]:
            raise ValueError(f"Comment {view.comment_id}: a {key} fact has an unknown value")
        pairs.add((key, fact.normalized_value))
    return pairs


def _setup_rank(
    pair: tuple[SetupKey, str], counts: Counter[tuple[SetupKey, str]]
) -> tuple[int, ...]:
    key, value = pair
    return (SETUP_KEYS.index(key), -counts[pair], CATEGORICAL_VALUES[key].index(value))
