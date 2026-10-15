"""Deterministic, explainable comparison of reported growing conditions with the viewer's garden.

Pure functions only: no I/O, no database, no model calls. Matching looks exclusively at each
fact's ``key`` and ``normalized_value``; quotes, as-written phrases, topics and outcome words
(failure, success, ...) never influence a status or tier. Missing information is reported as
``not_stated`` and is never treated as a mismatch. Region is a location preference handled by
:func:`region_status` and is never part of the relevance tier.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import Final, get_args

from crop_explorer.regions import REGION_NAMES
from crop_explorer.schemas import EnvKey
from crop_explorer.views import (
    DimensionMatch,
    FactView,
    GardenContext,
    MatchResult,
    MatchStatus,
    MentionView,
    RegionStatus,
    Tier,
)
from crop_explorer.vocab import (
    COMPARABLE_KEYS,
    KEY_LABELS,
    VIEWER_VALUES,
    ComparableKey,
    is_valid_normalized,
    value_label,
)

__all__ = [
    "applicable_facts",
    "compare_dimension",
    "explain",
    "match_context",
    "region_status",
    "tier_of",
]

_MATCH_STATUSES: Final[frozenset[str]] = frozenset(get_args(MatchStatus))
_AGGREGATE_PRIORITY: Final[tuple[MatchStatus, ...]] = ("match", "imprecise", "different")
_NOTHING_STATED: Final = "None of your selected conditions were stated."

_FactIdentity = tuple[str, str | None, str]

# --------------------------------------------------------------------------- applicability


def _identity(fact: FactView) -> _FactIdentity:
    return (fact.key, fact.normalized_value, fact.value_as_written)


def _dedupe(facts: Iterable[FactView]) -> tuple[FactView, ...]:
    """Keep the first fact of each ``(key, normalized_value, value_as_written)``."""
    first_by_identity: dict[_FactIdentity, FactView] = {}
    for fact in facts:
        first_by_identity.setdefault(_identity(fact), fact)
    return tuple(first_by_identity.values())


def applicable_facts(
    mention: MentionView, siblings: Sequence[MentionView]
) -> tuple[FactView, ...]:
    """Return the facts that describe the growing episode of ``mention``.

    Parameters
    ----------
    mention : MentionView
        The mention being evaluated.
    siblings : Sequence[MentionView]
        The other mentions of the same comment, in mention order. ``mention`` itself may be
        included; it is recognised by ``mention_id`` and not counted as its own sibling.

    Returns
    -------
    tuple[FactView, ...]
        The mention's own facts (fact order), then ``whole_comment`` facts of siblings for keys
        the mention does not state itself (mention order, fact order), deduplicated by
        ``(key, normalized_value, value_as_written)``. A sibling's ``this_mention`` facts never
        apply.
    """
    own_keys = {fact.key for fact in mention.facts}
    inherited = (
        fact
        for sibling in siblings
        if sibling.mention_id != mention.mention_id
        for fact in sibling.facts
        if fact.applicability == "whole_comment" and fact.key not in own_keys
    )
    return _dedupe((*mention.facts, *inherited))


# --------------------------------------------------------------------------- per-value rules


def _split_zone(zone: str) -> tuple[str, str | None]:
    """Split a validated zone like ``"6b"`` into ``("6", "b")``; ``"6"`` gives ``("6", None)``."""
    if zone[-1] in "ab":
        return zone[:-1], zone[-1]
    return zone, None


def _zone_status(reported: str, viewer: str) -> MatchStatus:
    if reported == viewer:
        return "match"
    reported_number, reported_sub = _split_zone(reported)
    viewer_number, viewer_sub = _split_zone(viewer)
    one_side_coarse = (reported_sub is None) != (viewer_sub is None)
    if reported_number == viewer_number and one_side_coarse:
        return "imprecise"
    return "different"


def _summer_heat_status(reported: str, viewer: str) -> MatchStatus:
    """Compare summer heat; a heatwave is an event, comparable only with "hot" (imprecisely)."""
    if reported == viewer:
        return "match"
    if reported == "heatwave":
        return "imprecise" if viewer == "hot" else "not_stated"
    return "different"


def _equality_status(reported: str, viewer: str) -> MatchStatus:
    return "match" if reported == viewer else "different"


_VALUE_RULES: Final[Mapping[str, Callable[[str, str], MatchStatus]]] = MappingProxyType(
    {"zone": _zone_status, "summer_heat": _summer_heat_status}
)
"""Per-key value comparison rules; every other comparable key uses plain equality."""


def _value_status(key: ComparableKey, reported: str, viewer: str) -> MatchStatus:
    """Compare one reported value; ``not_stated`` means "not comparable, ignore"."""
    return _VALUE_RULES.get(key, _equality_status)(reported, viewer)


def _aggregate(statuses: Iterable[MatchStatus]) -> MatchStatus:
    present = set(statuses)
    for status in _AGGREGATE_PRIORITY:
        if status in present:
            return status
    return "not_stated"


# --------------------------------------------------------------------------- validation


def _check_comparable(key: str, viewer_value: str) -> None:
    if key not in COMPARABLE_KEYS:
        raise ValueError(
            f"Key {key!r} is not a comparable dimension; expected one of "
            f"{', '.join(COMPARABLE_KEYS)}"
        )
    if viewer_value not in VIEWER_VALUES[key]:
        raise ValueError(f"Viewer value {viewer_value!r} is not selectable for {key!r}")


def _stated(key: EnvKey, facts: Sequence[FactView]) -> tuple[tuple[FactView, str], ...]:
    """Return ``(fact, normalized_value)`` for facts of ``key`` with a normalized value.

    Raises
    ------
    ValueError
        If a normalized value is not valid for ``key`` (corrupt or unvalidated data).
    """
    stated = tuple(
        (fact, value)
        for fact in facts
        if fact.key == key and (value := fact.normalized_value) is not None
    )
    for fact, value in stated:
        if not is_valid_normalized(key, value):
            raise ValueError(
                f"Fact {fact.fact_id!r} has invalid normalized value {value!r} for {key!r}"
            )
    return stated


# --------------------------------------------------------------------------- public matching


def compare_dimension(
    key: ComparableKey, viewer_value: str, facts: Sequence[FactView]
) -> DimensionMatch:
    """Compare one viewer-selected dimension with the reported facts.

    Parameters
    ----------
    key : ComparableKey
        The dimension to compare (never ``region`` or ``growing_year``).
    viewer_value : str
        The viewer's selected value; must be selectable for ``key``.
    facts : Sequence[FactView]
        Applicable facts; only those of ``key`` with a normalized value are considered.

    Returns
    -------
    DimensionMatch
        ``match`` if any reported value matches, else ``imprecise`` if any is imprecise, else
        ``different`` if any differs, else ``not_stated``.

    Raises
    ------
    ValueError
        If ``key`` is not comparable, ``viewer_value`` is not selectable, or a fact carries a
        normalized value that is invalid for ``key``.
    """
    _check_comparable(key, viewer_value)
    stated = _stated(key, facts)
    reported = tuple(dict.fromkeys(value for _, value in stated))
    status = _aggregate(_value_status(key, value, viewer_value) for value in reported)
    return DimensionMatch(
        key=key,
        status=status,
        viewer_value=viewer_value,
        reported_values=reported,
        evidence=tuple(fact for fact, _ in stated),
    )


def tier_of(statuses: Sequence[MatchStatus]) -> Tier:
    """Combine per-dimension statuses into a contextual-relevance tier.

    Parameters
    ----------
    statuses : Sequence[MatchStatus]
        One status per selected dimension.

    Returns
    -------
    Tier
        ``different`` if any dimension differs; ``match`` if all match; ``partial`` if any
        matches or is imprecise; otherwise ``not_stated``.

    Raises
    ------
    ValueError
        If ``statuses`` is empty or contains an unknown status.
    """
    if not statuses:
        raise ValueError("tier_of needs at least one dimension status")
    unknown = sorted(set(statuses) - _MATCH_STATUSES)
    if unknown:
        raise ValueError(f"Unknown match status(es): {', '.join(map(repr, unknown))}")
    if "different" in statuses:
        return "different"
    if all(status == "match" for status in statuses):
        return "match"
    if "match" in statuses or "imprecise" in statuses:
        return "partial"
    return "not_stated"


def match_context(garden: GardenContext, facts: Sequence[FactView]) -> MatchResult | None:
    """Compare a mention's applicable facts with every condition the viewer selected.

    Parameters
    ----------
    garden : GardenContext
        The viewer's voluntary garden description.
    facts : Sequence[FactView]
        The mention's applicable facts (see :func:`applicable_facts`).

    Returns
    -------
    MatchResult or None
        ``None`` when no comparable dimension is selected; otherwise one
        :class:`DimensionMatch` per selected key in canonical order, plus the tier.
    """
    selected = garden.selected()
    if not selected:
        return None
    dimensions = tuple(compare_dimension(key, value, facts) for key, value in selected.items())
    return MatchResult(tier=tier_of([d.status for d in dimensions]), dimensions=dimensions)


def region_status(garden_region: str | None, facts: Sequence[FactView]) -> RegionStatus | None:
    """Relate reported regions to the viewer's region (a preference, never a climate match).

    Parameters
    ----------
    garden_region : str or None
        The viewer's region code (e.g. ``US-CT``), or ``None`` when not set.
    facts : Sequence[FactView]
        Applicable facts; only normalized ``region`` facts are considered.

    Returns
    -------
    RegionStatus or None
        ``None`` without a garden region; ``same`` if any reported region equals it;
        ``elsewhere`` if only other regions are reported; otherwise ``not_stated``.

    Raises
    ------
    ValueError
        If ``garden_region`` or a reported region is not a known region code.
    """
    if garden_region is None:
        return None
    if garden_region not in REGION_NAMES:
        raise ValueError(f"Unknown garden region code: {garden_region!r}")
    reported = {value for _, value in _stated("region", facts)}
    if garden_region in reported:
        return "same"
    return "elsewhere" if reported else "not_stated"


# --------------------------------------------------------------------------- explanation


def _join(items: Sequence[str]) -> str:
    """Join as English prose: ``a``, ``a and b``, ``a, b and c``."""
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _labels_with(dimension: DimensionMatch, status: MatchStatus) -> str:
    """Return the labels of the reported values whose own comparison gave ``status``."""
    values = [
        value
        for value in dimension.reported_values
        if _value_status(dimension.key, value, dimension.viewer_value) == status
    ]
    return _join([value_label(dimension.key, value) for value in values])


def _viewer_label(dimension: DimensionMatch) -> str:
    return value_label(dimension.key, dimension.viewer_value)


def _with(result: MatchResult, status: MatchStatus) -> list[DimensionMatch]:
    return [d for d in result.dimensions if d.status == status]


def _different_clause(result: MatchResult) -> str:
    items = [
        f"{_labels_with(d, 'different')} (you: {_viewer_label(d)})"
        for d in _with(result, "different")
    ]
    return f"Different: {', '.join(items)}" if items else ""


def _matched_clause(result: MatchResult) -> str:
    labels = [_viewer_label(d) for d in _with(result, "match")]
    return f"Matches your {_join(labels)}" if labels else ""


def _imprecise_qualifier(dimension: DimensionMatch) -> str:
    if dimension.key == "zone" and _split_zone(dimension.viewer_value)[1] is not None:
        return "subzone unknown"
    return f"you: {_viewer_label(dimension)}"


def _imprecise_clause(result: MatchResult) -> str:
    items = [
        f"{_labels_with(d, 'imprecise')} reported ({_imprecise_qualifier(d)})"
        for d in _with(result, "imprecise")
    ]
    return ", ".join(items)


def _not_comparable_clause(result: MatchResult) -> str:
    items = [
        f"{KEY_LABELS[d.key]} not comparable ({_labels_with(d, 'not_stated')} reported)"
        for d in _with(result, "not_stated")
        if d.reported_values
    ]
    return ", ".join(items)


def _not_stated_clause(result: MatchResult) -> str:
    keys = [KEY_LABELS[d.key] for d in _with(result, "not_stated") if not d.reported_values]
    return f"{_join(keys)} not stated" if keys else ""


def _sentence(text: str) -> str:
    return f"{text[0].upper()}{text[1:]}." if text else ""


def explain(result: MatchResult | None) -> str:
    """Describe, in plain words, why a report was placed in its tier.

    Parameters
    ----------
    result : MatchResult or None
        A mention's comparison, or ``None`` when the viewer selected no condition.

    Returns
    -------
    str
        One or two short sentences naming the differing, matching, imprecise and unstated
        conditions (``""`` for ``None``). Never mentions outcomes, quality or scores.
    """
    if result is None:
        return ""
    if all(d.status == "not_stated" and not d.reported_values for d in result.dimensions):
        return _NOTHING_STATED
    rest = (_matched_clause, _imprecise_clause, _not_comparable_clause, _not_stated_clause)
    second = "; ".join(clause for build in rest if (clause := build(result)))
    sentences = (_sentence(_different_clause(result)), _sentence(second))
    return " ".join(sentence for sentence in sentences if sentence)
