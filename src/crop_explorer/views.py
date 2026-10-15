"""Read-side types: viewer garden context, persisted report views, match and search results."""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, NonNegativeInt, model_validator

from crop_explorer.regions import REGION_NAMES
from crop_explorer.schemas import (
    Applicability,
    Basis,
    EnvKey,
    ParseStatus,
    RightsStatus,
    Topic,
)
from crop_explorer.vocab import COMPARABLE_KEYS, VIEWER_VALUES, ComparableKey

ViewerScope = Literal["public", "private"]
MatchStatus = Literal["match", "imprecise", "not_stated", "different"]
Tier = Literal["match", "partial", "not_stated", "different"]
RegionStatus = Literal["same", "same_country", "elsewhere", "not_stated"]
"""How a report's region relates to the viewer's: ``same`` code, ``same_country`` (the viewer
picked a country and the report names one of its states or provinces), ``elsewhere``, or
``not_stated``. A location preference only: it orders reports and never changes a tier."""
GroupKey = Literal["all", "match", "partial", "not_stated", "different"]

TIER_ORDER: Final = MappingProxyType({"match": 0, "partial": 1, "not_stated": 2, "different": 3})
"""Contextual-relevance order of tiers (lower sorts first). Never a quality ranking."""

GROUP_TITLES: Final = MappingProxyType(
    {
        "all": "Reports",
        "match": "Matching reported conditions",
        "partial": "Partially matching conditions",
        "not_stated": "Conditions not stated",
        "different": "Different reported conditions",
    }
)

_FROZEN = ConfigDict(extra="forbid", frozen=True)


class GardenContext(BaseModel):
    """Voluntary, ephemeral description of the viewer's garden. Every field is optional."""

    model_config = _FROZEN

    region: str | None = None
    zone: str | None = None
    method: str | None = None
    shelter: str | None = None
    summer_heat: str | None = None
    humidity: str | None = None
    precipitation: str | None = None
    season_length: str | None = None
    sun: str | None = None
    soil: str | None = None
    irrigation: str | None = None

    @model_validator(mode="after")
    def _check_vocabulary(self) -> Self:
        if self.region is not None and self.region not in REGION_NAMES:
            raise ValueError(f"Unknown region code: {self.region!r}")
        for key, value in self.selected().items():
            if value not in VIEWER_VALUES[key]:
                raise ValueError(f"Value {value!r} is not selectable for {key!r}")
        return self

    def selected(self) -> dict[ComparableKey, str]:
        """Return the comparable dimensions the viewer set, in canonical key order."""
        return {k: v for k in COMPARABLE_KEYS if (v := getattr(self, k)) is not None}

    def is_empty(self) -> bool:
        """Return whether no comparable dimension and no region is set."""
        return self.region is None and not self.selected()


class FactView(BaseModel):
    """A persisted, validated environment fact."""

    model_config = _FROZEN

    fact_id: str
    key: EnvKey
    value_as_written: str
    normalized_value: str | None
    evidence_quote: str
    applicability: Applicability
    basis: Basis


class MentionView(BaseModel):
    """A persisted variety mention with the facts stored under it."""

    model_config = _FROZEN

    mention_id: str
    crop: str | None
    variety_as_written: str | None
    variety_key: str | None
    topics: tuple[Topic, ...]
    evidence_quote: str
    extractor_version: str
    facts: tuple[FactView, ...]


class CommentView(BaseModel):
    """A visible comment with provenance, rights state and extracted mentions."""

    model_config = _FROZEN

    comment_id: str
    source_id: str
    source_kind: str
    rights_status: RightsStatus
    text: str
    permalink: str | None
    source_order: int
    created_at: str | None
    parse_status: ParseStatus | None
    """``None`` means the comment has never been through extraction (so it has no mentions)."""
    mentions: tuple[MentionView, ...]

    @model_validator(mode="after")
    def _check_unextracted_has_no_mentions(self) -> Self:
        if self.parse_status is None and self.mentions:
            raise ValueError(
                f"Comment {self.comment_id!r} has mentions but has never been through extraction"
            )
        return self


class DimensionMatch(BaseModel):
    """Comparison of one viewer-selected dimension against a mention's applicable facts."""

    model_config = _FROZEN

    key: ComparableKey
    status: MatchStatus
    viewer_value: str
    reported_values: tuple[str, ...]
    """Distinct normalized values reported for this key (empty when not stated)."""
    written_values: tuple[str, ...]
    """Distinct ``value_as_written`` of this key's facts that have no normalized value, in
    first-seen order. Display only (untrusted comment text): never compared, never a status."""
    evidence: tuple[FactView, ...]


class MatchResult(BaseModel):
    """Per-dimension comparison and resulting relevance tier for one mention."""

    model_config = _FROZEN

    tier: Tier
    dimensions: tuple[DimensionMatch, ...]

    def keys_with(self, status: MatchStatus) -> tuple[ComparableKey, ...]:
        """Return the dimension keys having ``status``, in canonical order."""
        return tuple(d.key for d in self.dimensions if d.status == status)


class MentionMatch(BaseModel):
    """A relevant mention with the facts applicable to it and its match (if any selection)."""

    model_config = _FROZEN

    mention: MentionView
    facts: tuple[FactView, ...]
    match: MatchResult | None


class CommentResult(BaseModel):
    """One displayed comment card: its tier, explanation and per-mention detail."""

    model_config = _FROZEN

    comment: CommentView
    tier: Tier | None
    """``None`` when the viewer selected no comparable condition."""
    mention_matches: tuple[MentionMatch, ...]
    region_status: RegionStatus | None
    """``None`` when the viewer selected no region."""
    explanation: str
    has_conditions: bool
    """Whether an applicable fact of a relevant mention has a normalized value for a comparable
    key or ``region`` (a growing year alone, or a display-only fact, does not count)."""


class SearchQuery(BaseModel):
    """Everything the viewer asked for on the Browse screen."""

    model_config = _FROZEN

    text: str | None = None
    crop: str | None = None
    topics: tuple[Topic, ...] = ()
    year: str | None = None
    region_filter: str | None = None
    garden: GardenContext = GardenContext()
    only_stated_matches: bool = False


class ResultGroup(BaseModel):
    """A titled group of results in display order."""

    model_config = _FROZEN

    key: GroupKey
    title: str
    results: tuple[CommentResult, ...]
    collapsed_by_default: bool


class SearchResult(BaseModel):
    """Grouped results plus honest counts about data coverage."""

    model_config = _FROZEN

    groups: tuple[ResultGroup, ...]
    n_candidates: int
    """Distinct visible comments matching text and structured filters (before strict hiding)."""
    n_with_conditions: int
    """Candidates whose relevant mentions state a comparable condition or a region."""
    n_not_analysed: int
    """Candidates that have never been through extraction (``parse_status is None``)."""
    n_hidden_by_strict: int
    tier_counts: dict[str, int]
    limitation_message: str | None


class ChipSupport(BaseModel):
    """How many visible reports support a quick-condition chip.

    Counts are distinct comments. ``n_any`` (the display gate) counts comments with a mention
    whose applicable facts state at least one of the chip's ``(key, value)`` conditions;
    ``n_all`` counts comments with a single mention stating every one of them, i.e. the
    reports the chip would put under "Matching reported conditions".
    """

    model_config = _FROZEN

    n_any: NonNegativeInt
    n_all: NonNegativeInt

    @model_validator(mode="after")
    def _check_all_within_any(self) -> Self:
        if self.n_all > self.n_any:
            raise ValueError(f"n_all ({self.n_all}) cannot exceed n_any ({self.n_any})")
        return self
