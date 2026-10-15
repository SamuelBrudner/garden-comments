"""Extraction contract and raw-record types shared across ingestion and persistence.

Every model forbids unknown fields so malformed upstream records fail loudly.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

Topic = Literal[
    "flavor",
    "productivity",
    "earliness",
    "disease",
    "pests",
    "weather_tolerance",
    "growing_method",
    "appearance",
    "general",
]
TOPICS: tuple[Topic, ...] = get_args(Topic)

EnvKey = Literal[
    "region",
    "zone",
    "method",
    "shelter",
    "summer_heat",
    "humidity",
    "precipitation",
    "season_length",
    "sun",
    "soil",
    "irrigation",
    "growing_year",
]
ENV_KEYS: tuple[EnvKey, ...] = get_args(EnvKey)

Applicability = Literal["this_mention", "whole_comment"]
Basis = Literal["observed_experience", "reported_garden_context"]
ParseStatus = Literal["parsed", "partial", "unresolved"]

RightsStatus = Literal["private_unverified", "synthetic", "cleared"]
"""Publication state of a source.

``private_unverified`` is the default for user-supplied imports and must never reach a
public viewer. ``synthetic`` is fabricated demo data that is safe to show (always labelled
as demo). ``cleared`` means the distribution workflow for real comments was explicitly
cleared by the operator.
"""
RIGHTS_STATUSES: tuple[RightsStatus, ...] = get_args(RightsStatus)

ImportScope = Literal["private", "synthetic", "cleared"]
"""CLI-facing name for the rights status assigned at import time."""
SCOPE_TO_RIGHTS: dict[ImportScope, RightsStatus] = {
    "private": "private_unverified",
    "synthetic": "synthetic",
    "cleared": "cleared",
}

_STRICT = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)


class EnvironmentFact(BaseModel):
    """One source-supported growing-environment fact attached to a variety mention."""

    model_config = _STRICT

    key: EnvKey
    value_as_written: str = Field(min_length=1)
    normalized_value: str | None = None
    evidence_quote: str = Field(min_length=1)
    applicability: Applicability
    basis: Basis


class VarietyMention(BaseModel):
    """A crop/variety mention inside one comment, with its applicable environment facts."""

    model_config = _STRICT

    crop: str | None = None
    variety_as_written: str | None = None
    topics: list[Topic] = Field(default_factory=list)
    evidence_quote: str = Field(min_length=1)
    environment: list[EnvironmentFact] = Field(default_factory=list)


class ParsedComment(BaseModel):
    """One line of extractor output JSONL (the model step's deliverable)."""

    model_config = _STRICT

    comment_id: str = Field(min_length=1)
    extractor_version: str = Field(min_length=1)
    mentions: list[VarietyMention] = Field(default_factory=list)
    status: ParseStatus
    warnings: list[str] = Field(default_factory=list)


class ValidatedFact(BaseModel):
    """An environment fact that passed validation; ``normalized_value`` is vocabulary-checked."""

    model_config = _STRICT

    key: EnvKey
    value_as_written: str
    normalized_value: str | None
    evidence_quote: str
    applicability: Applicability
    basis: Basis


class ValidatedMention(BaseModel):
    """A mention that passed validation, with normalized crop and variety key."""

    model_config = _STRICT

    crop: str | None
    variety_as_written: str | None
    variety_key: str | None
    topics: tuple[Topic, ...]
    evidence_quote: str
    facts: tuple[ValidatedFact, ...]


class Rejection(BaseModel):
    """Why a record (line, mention or fact) was dropped. Never carries comment text."""

    model_config = _STRICT

    comment_id: str | None
    line_number: int | None = None
    mention_index: int | None = None
    fact_index: int | None = None
    reason: str


class ValidatedComment(BaseModel):
    """Validation output for one comment, ready for transactional replacement in the DB."""

    model_config = _STRICT

    comment_id: str
    extractor_version: str
    status: ParseStatus
    mentions: tuple[ValidatedMention, ...]
    warnings: tuple[str, ...]
    rejections: tuple[Rejection, ...]


class ColumnMapping(BaseModel):
    """Maps columns/keys of a user-supplied export onto the comment record fields."""

    model_config = _STRICT

    id_col: str = "comment_id"
    text_col: str = "text"
    permalink_col: str | None = "permalink"
    date_col: str | None = "created_at"


class CommentRecord(BaseModel):
    """One imported raw comment, before persistence."""

    model_config = _STRICT

    comment_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    permalink: str | None = None
    created_at: str | None = None
    source_order: int = Field(ge=0)


class SourceRecord(BaseModel):
    """Provenance and rights state of one imported dataset."""

    model_config = _STRICT

    source_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    source_url: str | None = None
    rights_status: RightsStatus
    imported_at: str
    expires_at: str | None = None
    dataset_hash: str = Field(min_length=1)


class ImportStats(BaseModel):
    """Outcome counts of syncing one source's comments into the database."""

    model_config = _STRICT

    source_id: str
    inserted: int
    updated: int
    unchanged: int
    deleted: int


def to_utc_iso(value: str | datetime | date) -> str:
    """Normalize a date/datetime (or ISO string) to ``YYYY-MM-DDTHH:MM:SSZ`` in UTC.

    Naive datetimes and bare dates are interpreted as UTC. All timestamps stored in the
    database use this format so that lexicographic comparison equals temporal comparison.

    Raises
    ------
    ValueError
        If ``value`` is a string that is not ISO-8601 parseable.
    """
    if isinstance(value, str):
        value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now_iso() -> str:
    """Return the current UTC time in the canonical storage format."""
    return to_utc_iso(datetime.now(UTC))
