"""Extraction contract and raw-record types shared across ingestion and persistence.

Every model forbids unknown fields so malformed upstream records fail loudly.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal, Self, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

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

MAX_VALUE_CHARS = 200
"""Longest ``value_as_written`` an extractor may emit: a value is a short phrase, and the cap
bounds the validator's work on adversarial lines."""
MAX_QUOTE_CHARS = 2000
"""Longest evidence quote an extractor may emit (mention or fact)."""
TEXT_HASH_PATTERN = r"^[0-9a-f]{64}$"
"""A lowercase SHA-256 hex digest, as ``export-for-extraction`` writes ``text_hash``."""


class EnvironmentFact(BaseModel):
    """One source-supported growing-environment fact attached to a variety mention."""

    model_config = _STRICT

    key: EnvKey
    value_as_written: str = Field(min_length=1, max_length=MAX_VALUE_CHARS)
    normalized_value: str | None = None
    evidence_quote: str = Field(min_length=1, max_length=MAX_QUOTE_CHARS)
    applicability: Applicability
    basis: Basis


class VarietyMention(BaseModel):
    """A crop/variety mention inside one comment, with its applicable environment facts."""

    model_config = _STRICT

    crop: str | None = None
    variety_as_written: str | None = None
    topics: list[Topic] = Field(default_factory=list)
    evidence_quote: str = Field(min_length=1, max_length=MAX_QUOTE_CHARS)
    environment: list[EnvironmentFact] = Field(default_factory=list)


class ParsedComment(BaseModel):
    """One line of extractor output JSONL (the model step's deliverable).

    ``text_hash`` is copied from the export line: the SHA-256 hex digest of the UTF-8 comment
    text the extraction was made from. The loader rejects the line when the stored text has
    changed since (``stale_text``).
    """

    model_config = _STRICT

    comment_id: str = Field(min_length=1)
    text_hash: str = Field(pattern=TEXT_HASH_PATTERN)
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
    """Maps columns/keys of a user-supplied export onto the comment record fields.

    The id and text columns are always required. The permalink and date columns are optional
    (absent → ``None``) unless ``permalink_required`` / ``date_required`` is set, which the
    CLI does when the operator names the column explicitly: a mistyped name then fails instead
    of silently dropping every value.
    """

    model_config = _STRICT

    id_col: str = "comment_id"
    text_col: str = "text"
    permalink_col: str | None = "permalink"
    date_col: str | None = "created_at"
    permalink_required: bool = False
    date_required: bool = False

    @model_validator(mode="after")
    def _required_columns_are_named(self) -> Self:
        for column, required, name in (
            (self.permalink_col, self.permalink_required, "permalink"),
            (self.date_col, self.date_required, "date"),
        ):
            if required and column is None:
                raise ValueError(f"{name}_required needs a {name}_col")
        return self


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
    """Outcome of syncing one source's comments: counts and the source's effective rights.

    ``rights_status`` and ``expires_at`` are the values stored after the import (an import
    that names no expiry keeps the stored one), so the operator sees the rights state in force.
    """

    model_config = _STRICT

    source_id: str
    inserted: int
    updated: int
    unchanged: int
    deleted: int
    rights_status: RightsStatus
    expires_at: str | None


def to_utc_iso(value: str | datetime | date) -> str:
    """Normalize a date/datetime (or ISO string) to ``YYYY-MM-DDTHH:MM:SSZ`` in UTC.

    Naive datetimes and bare dates are interpreted as UTC. All timestamps stored in the
    database use this format so that lexicographic comparison equals temporal comparison;
    the year is always four digits (``strftime("%Y")`` does not zero-pad years before 1000
    on every platform, which would break that ordering).

    Raises
    ------
    ValueError
        If ``value`` is a string that is not ISO-8601 parseable.
    OverflowError
        If converting to UTC leaves the supported date range (e.g. year 1 with a positive
        offset).
    """
    if isinstance(value, str):
        value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    utc = value.astimezone(UTC)
    return f"{utc.year:04d}-{utc:%m-%dT%H:%M:%S}Z"


def utc_now_iso() -> str:
    """Return the current UTC time in the canonical storage format."""
    return to_utc_iso(datetime.now(UTC))
