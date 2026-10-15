"""Conservative reported-region counts and the coarse U.S. state map (pure, no I/O).

The map answers "where did gardeners explicitly say they grew this?" (SPEC §6). Each comment
counts once per U.S. state named by a normalized ``region`` fact applicable to one of its
relevant mentions. Zones never produce a region and no coordinates are ever produced.

Small-cell suppression only coarsens the map display (its shading, hover text and the state
selector's labels). It is not a privacy control for the reports themselves: every visible card
still shows its comment text and region line, and filtering by a state lists its reports.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

import pandas as pd
import plotly.graph_objects as go

from crop_explorer.regions import is_us_state, region_name, us_postal
from crop_explorer.views import CommentResult
from crop_explorer.vocab import is_valid_normalized

TABLE_COLUMNS: Final = ("region_code", "region_name", "postal", "n_comments", "suppressed")
"""Exact column order of :attr:`RegionCounts.table`."""

_DTYPES: Final = {
    "region_code": "str",
    "region_name": "str",
    "postal": "str",
    "n_comments": "int64",
    "suppressed": "bool",
}
_US_COUNTRY: Final = "US"
_LAYOUT: Final = MappingProxyType(
    {
        "geo": {"scope": "usa", "bgcolor": "rgba(0,0,0,0)"},
        "margin": {"l": 0, "r": 0, "t": 0, "b": 0},
        "dragmode": False,
    }
)
"""Figure layout: the U.S. states only, transparent, no margins, no panning."""
_SEQUENTIAL_BLUE: Final = (
    (0.0, "#cde2fb"),
    (0.25, "#86b6ef"),
    (0.5, "#3987e5"),
    (0.75, "#1c5cab"),
    (1.0, "#0d366b"),
)
"""One-hue light-to-dark ramp: the lightest step means "near zero" (or suppressed)."""

_Bucket = Literal["mapped", "us_state_not_reported", "outside_us", "not_reported"]


@dataclass(frozen=True, eq=False)
class RegionCounts:
    """Distinct-comment counts per reported U.S. state, plus the comments the map cannot show.

    Every counted comment is either on the map (in one row per state it names) or in exactly
    one of ``not_reported``, ``us_state_not_reported`` and ``outside_us``.

    Attributes
    ----------
    table : pandas.DataFrame
        Tidy table with columns :data:`TABLE_COLUMNS`, one row per state with at least one
        report, sorted by displayed ``n_comments`` descending then ``region_code``. Suppressed
        cells show ``n_comments == 0`` and ``suppressed == True``.
    not_reported : int
        Comments with no normalized region fact on any relevant mention ("Region not
        reported"); zone-only and display-only (unnormalized) regions land here.
    outside_us : int
        Comments whose normalized regions are all outside the U.S.
    us_state_not_reported : int
        Comments placed in the U.S. as a whole (country code ``US``) but in no state.
    min_cell_count : int
        Cells with fewer distinct comments than this were suppressed.
    """

    table: pd.DataFrame
    not_reported: int
    outside_us: int
    us_state_not_reported: int
    min_cell_count: int

    def __post_init__(self) -> None:
        """Validate the table shape and the bucket counts.

        Raises
        ------
        ValueError
            If the table columns differ from :data:`TABLE_COLUMNS`, a bucket count is
            negative, or ``min_cell_count`` is below 1.
        """
        if tuple(self.table.columns) != TABLE_COLUMNS:
            raise ValueError(f"RegionCounts.table columns must be exactly {TABLE_COLUMNS}")
        if min(self.not_reported, self.outside_us, self.us_state_not_reported) < 0:
            raise ValueError("RegionCounts bucket counts must be non-negative")
        _check_min_cell_count(self.min_cell_count)

    def __eq__(self, other: object) -> bool:
        """Compare by value; the table is compared with :meth:`pandas.DataFrame.equals`.

        The generated dataclass ``__eq__`` would compare DataFrames with ``==``, which raises
        instead of returning a bool.
        """
        if not isinstance(other, RegionCounts):
            return NotImplemented
        buckets = (self.not_reported, self.outside_us, self.us_state_not_reported)
        others = (other.not_reported, other.outside_us, other.us_state_not_reported)
        same_threshold = self.min_cell_count == other.min_cell_count
        return buckets == others and same_threshold and self.table.equals(other.table)

    __hash__ = None  # type: ignore[assignment]  # mutable DataFrame inside: unhashable


def region_counts(results: Sequence[CommentResult], min_cell_count: int) -> RegionCounts:
    """Count distinct comments per reported U.S. state, suppressing small cells.

    Parameters
    ----------
    results : Sequence[CommentResult]
        Visible search results; each must have a distinct ``comment_id``. Only the applicable
        facts of each result's relevant mentions (``mention_matches[*].facts``) are read.
    min_cell_count : int
        Cells with ``0 < n_comments < min_cell_count`` are reported as ``0`` and flagged
        ``suppressed``. Pass ``cfg.map.public_min_cell_count`` in public scope, ``1`` in
        private scope (suppresses nothing). This coarsens the map display only.

    Returns
    -------
    RegionCounts
        The state table plus the ``not_reported``, ``outside_us`` and
        ``us_state_not_reported`` comment counts.

    Raises
    ------
    ValueError
        If ``min_cell_count < 1``, two results share a ``comment_id``, or a region fact
        carries a normalized value outside the region vocabulary.
    """
    _check_min_cell_count(min_cell_count)
    _check_distinct_comments(results)
    regions = [_comment_regions(result) for result in results]
    per_state = Counter(code for codes in regions for code in codes if is_us_state(code))
    buckets = Counter(_bucket(codes) for codes in regions)
    return RegionCounts(
        table=_table(per_state, min_cell_count),
        not_reported=buckets["not_reported"],
        outside_us=buckets["outside_us"],
        us_state_not_reported=buckets["us_state_not_reported"],
        min_cell_count=min_cell_count,
    )


def count_label(n_comments: int, *, suppressed: bool, min_cell_count: int) -> str:
    """Return the display label for a map cell, never revealing a suppressed count.

    Parameters
    ----------
    n_comments : int
        Displayed count from :attr:`RegionCounts.table`.
    suppressed : bool
        Whether the cell was suppressed.
    min_cell_count : int
        The suppression threshold used to build the table.

    Returns
    -------
    str
        ``"1 report"``, ``"3 reports"`` or ``"fewer than 3 reports"``.
    """
    if suppressed:
        return f"fewer than {min_cell_count} reports"
    return f"{n_comments} report" if n_comments == 1 else f"{n_comments} reports"


def choropleth(counts: RegionCounts) -> go.Figure:
    """Draw reported-state counts as a U.S. state choropleth (no points, no coordinates).

    Parameters
    ----------
    counts : RegionCounts
        Output of :func:`region_counts`.

    Returns
    -------
    plotly.graph_objects.Figure
        A figure whose only trace is a ``Choropleth`` with ``locationmode="USA-states"`` and
        postal-code locations in table order. Hover shows the count, or "fewer than k
        reports" for suppressed cells. Renders (as an empty map) when the table is empty.
    """
    figure = go.Figure(data=[_trace(counts.table, _hover_text(counts))])
    figure.update_layout(**_LAYOUT)
    return figure


# --------------------------------------------------------------------------- private helpers


def _check_min_cell_count(min_cell_count: int) -> None:
    if min_cell_count < 1:
        raise ValueError(f"min_cell_count must be at least 1, got {min_cell_count}")


def _check_distinct_comments(results: Sequence[CommentResult]) -> None:
    seen = Counter(result.comment.comment_id for result in results)
    duplicates = sorted(comment_id for comment_id, n in seen.items() if n > 1)
    if duplicates:
        raise ValueError(f"Duplicate comment ids in map input: {', '.join(duplicates)}")


def _comment_regions(result: CommentResult) -> frozenset[str]:
    """Return the normalized region codes applicable to the result's relevant mentions."""
    codes: set[str] = set()
    for match in result.mention_matches:
        for fact in match.facts:
            if fact.key != "region" or fact.normalized_value is None:
                continue
            if not is_valid_normalized("region", fact.normalized_value):
                raise ValueError(
                    f"Comment {result.comment.comment_id}: region fact {fact.fact_id} has a "
                    "normalized value outside the region vocabulary"
                )
            codes.add(fact.normalized_value)
    return frozenset(codes)


def _bucket(codes: Iterable[str]) -> _Bucket:
    """Classify one comment's region codes; a U.S. state outranks every other code."""
    code_set = frozenset(codes)
    if any(is_us_state(code) for code in code_set):
        return "mapped"
    if _US_COUNTRY in code_set:
        return "us_state_not_reported"
    return "outside_us" if code_set else "not_reported"


def _table(per_state: Counter[str], min_cell_count: int) -> pd.DataFrame:
    """Build the sorted, suppressed tidy table from true per-state comment counts."""
    rows = sorted(
        (_row(code, n, min_cell_count) for code, n in per_state.items()),
        key=lambda row: (-row[3], row[0]),
    )
    columns = {name: [row[i] for row in rows] for i, name in enumerate(TABLE_COLUMNS)}
    return pd.DataFrame(columns).astype(_DTYPES)


def _row(code: str, n_comments: int, min_cell_count: int) -> tuple[str, str, str, int, bool]:
    """Return one table row; a small cell's true count is replaced by ``0``."""
    suppressed = 0 < n_comments < min_cell_count
    shown = 0 if suppressed else n_comments
    return (code, region_name(code), us_postal(code), shown, suppressed)


def _hover_text(counts: RegionCounts) -> list[str]:
    """Return one hover label per table row; a suppressed cell never shows its count."""
    table = counts.table
    rows = zip(
        table["region_name"],
        table["n_comments"].tolist(),
        table["suppressed"].tolist(),
        strict=True,
    )
    return [
        f"{name}: {count_label(n, suppressed=s, min_cell_count=counts.min_cell_count)}"
        for name, n, s in rows
    ]


def _trace(table: pd.DataFrame, hover: list[str]) -> go.Choropleth:
    z = table["n_comments"].tolist()
    return go.Choropleth(
        locationmode="USA-states",
        locations=table["postal"].tolist(),
        z=z,
        zmin=0,
        zmax=max([1, *z]),
        text=hover,
        hovertemplate="%{text}<extra></extra>",
        colorscale=[list(stop) for stop in _SEQUENTIAL_BLUE],
        marker={"line": {"color": "white", "width": 0.5}},
        colorbar={"title": {"text": "Reports"}},
    )
