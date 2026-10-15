"""Validate offline extractor output against stored comment text and load it (SPEC §4, §8).

The model step writes one :class:`~crop_explorer.schemas.ParsedComment` per JSONL line. Nothing
from it is trusted: every mention and fact is checked against the exact stored comment text
(docs/ARCHITECTURE.md, "Extraction validation and loading", rules 1-8). Failures never abort a
load. Each dropped line, mention or fact is recorded as a
:class:`~crop_explorer.schemas.Rejection` that carries ids, indexes and a machine-readable reason
only. Comment bodies, evidence quotes and extractor warnings are never logged.

Rejection reasons. Lines: ``schema_error``, ``unknown_comment_id``, ``duplicate_comment_id``,
``source_expired``, ``stale_text``. Mentions: ``mention_quote_not_substring``,
``variety_not_in_quote``, ``crop_not_in_quote``, ``invalid_crop``, ``invalid_variety``. Facts:
``fact_quote_not_substring``, ``value_not_in_quote``, ``value_in_variety_name``,
``invalid_normalized_value``, ``negated_value``, ``blocked_value``, ``strict_key_mismatch``,
``normalized_value_mismatch``, ``hypothetical_context``, ``question_context``,
``conflicting_region`` and ``conflicting_zone``. Rejection ``mention_index``/``fact_index`` are
positions in the extractor's input lists; the indexes stored in the database are positions
among the accepted records.
"""

from __future__ import annotations

import json
import re
import sqlite3
from bisect import bisect_right
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache, partial
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, get_args

from loguru import logger
from pydantic import BaseModel, ConfigDict, NonNegativeInt, ValidationError

from crop_explorer import db
from crop_explorer.config import ExtractionConfig
from crop_explorer.normalization import (
    STRICT_KEYS,
    WrittenSpan,
    mentions_crop,
    normalize_crop,
    normalize_written,
    resolve_spans,
    variety_key,
)
from crop_explorer.schemas import (
    EnvironmentFact,
    EnvKey,
    ParsedComment,
    ParseStatus,
    Rejection,
    ValidatedComment,
    ValidatedFact,
    ValidatedMention,
    VarietyMention,
)
from crop_explorer.visibility import require_utc_iso
from crop_explorer.vocab import is_valid_normalized

__all__ = [
    "ExtractionReport",
    "load_extractions",
    "read_extraction_jsonl",
    "validate_parsed_comment",
]

_SCHEMA_ERROR: Final = "schema_error"
_UNKNOWN_COMMENT_ID: Final = "unknown_comment_id"
_DUPLICATE_COMMENT_ID: Final = "duplicate_comment_id"
_SOURCE_EXPIRED: Final = "source_expired"
_STALE_TEXT: Final = "stale_text"
_MENTION_QUOTE_NOT_SUBSTRING: Final = "mention_quote_not_substring"
_VARIETY_NOT_IN_QUOTE: Final = "variety_not_in_quote"
_CROP_NOT_IN_QUOTE: Final = "crop_not_in_quote"
_INVALID_CROP: Final = "invalid_crop"
_INVALID_VARIETY: Final = "invalid_variety"
_FACT_QUOTE_NOT_SUBSTRING: Final = "fact_quote_not_substring"
_VALUE_NOT_IN_QUOTE: Final = "value_not_in_quote"
_VALUE_IN_VARIETY_NAME: Final = "value_in_variety_name"
_INVALID_NORMALIZED_VALUE: Final = "invalid_normalized_value"
_NEGATED_VALUE: Final = "negated_value"
_BLOCKED_VALUE: Final = "blocked_value"
_STRICT_KEY_MISMATCH: Final = "strict_key_mismatch"
_NORMALIZED_VALUE_MISMATCH: Final = "normalized_value_mismatch"
_HYPOTHETICAL_CONTEXT: Final = "hypothetical_context"
_QUESTION_CONTEXT: Final = "question_context"
_CONFLICT_REASONS: Final[Mapping[str, str]] = MappingProxyType(
    {"region": "conflicting_region", "zone": "conflicting_zone"}
)
_LINE_BUCKETS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "schema_errors": _SCHEMA_ERROR,
        "unknown_comment_ids": _UNKNOWN_COMMENT_ID,
        "duplicate_comment_ids": _DUPLICATE_COMMENT_ID,
        "expired_comment_ids": _SOURCE_EXPIRED,
        "stale_texts": _STALE_TEXT,
    }
)
"""``ExtractionReport`` field -> the line-level rejection reason it counts."""

_STATUSES: Final[tuple[ParseStatus, ...]] = get_args(ParseStatus)
_SENTENCE_END: Final = re.compile(r"[!?\n]|\.(?=[\s\"')\]\u201d\u2019]|\Z)")
"""``!``, ``?`` and line breaks always end a sentence; ``.`` only before space, a closing
quote or bracket, or the end of the text (so ``5.5 gallon`` and ``U.S.A`` stay whole)."""
_QUESTION_MARK: Final = "?"
_TRAILING_WORD: Final = re.compile(r"(?<![\w.])[^\W\d_]+(?:\.[^\W\d_]+)*\Z")
_TRAILING_WORD_WINDOW: Final = 32
"""How far before a ``.`` its word is looked for: far longer than any abbreviation, and it keeps
sentence splitting linear in the text length (a longer word is never an abbreviation)."""
_ABBREVIATIONS: Final = frozenset(
    {
        "mr", "mrs", "ms", "dr", "prof", "st", "mt", "ft", "jr", "sr",
        "vs", "e.g", "i.e", "eg", "ie", "cf", "approx",
        "var", "cv", "ssp", "subsp", "spp",
        "gal", "lb", "lbs", "oz",
    }
)  # fmt: skip
"""Words whose trailing ``.`` does not end a sentence ("I'd try Mr. Stripey in zone 5").

Joining across an abbreviation can only make the hypothetical guard stricter, never looser."""
_APOSTROPHES: Final = "'\u2019\u2018\u02bc\u00b4`"
"""Characters a marker's apostrophe matches: ASCII ``'``, U+2019, U+2018, U+02BC, the acute accent
U+00B4 and the backtick."""
_APOSTROPHE_CLASS: Final = f"[{_APOSTROPHES}]"
_WHOLE_WORD: Final = r"(?<![\w-])(?P<value>{})(?:s|es)?(?![\w-])"
"""A name as whole words, optionally plural; a hyphen counts as a word character, as in
normalisation ("humid" is not in "non-humid", "Roma" is not in "aroma")."""
_WHOLE_COMMENT: Final = "whole_comment"
_UTF8_BOM: Final = b"\xef\xbb\xbf"
_SAVEPOINT: Final = "crop_explorer_load_extractions"
_REPORT_LOG: Final = (
    "Loaded extractions from {}: {} line(s); {} comment(s) loaded {}; {} schema error(s), "
    "{} unknown id(s), {} duplicate-id line(s), {} expired-source line(s), {} stale-text "
    "line(s); mentions {} accepted, {} rejected; facts {} accepted, {} rejected; "
    "{} mention(s) without crop or variety"
)
_REPORT_LOG_FIELDS: Final = (
    "lines_read",
    "comments_loaded",
    "status_counts",
    *_LINE_BUCKETS,
    "mentions_accepted",
    "mentions_rejected",
    "facts_accepted",
    "facts_rejected",
    "unresolved_identities",
)

_Span = tuple[int, int]
_Indexed = tuple[int, ValidatedFact]
"""An accepted fact with its index in the extractor's input list."""


class ExtractionReport(BaseModel):
    """Aggregate outcome of loading one extraction JSONL file: counts, ids and reasons only.

    Every non-blank line lands in exactly one of ``schema_errors``, ``duplicate_comment_ids``,
    ``unknown_comment_ids``, ``expired_comment_ids``, ``stale_texts`` or ``comments_loaded``,
    so those six add up to ``lines_read``. Mention and fact counts cover the loaded comments
    only; facts of a rejected mention count as rejected.

    Attributes
    ----------
    lines_read
        Non-blank lines in the file.
    schema_errors
        Lines that were not valid JSON or did not match the ``ParsedComment`` schema.
    unknown_comment_ids
        Lines whose ``comment_id`` is not stored in the database.
    duplicate_comment_ids
        Lines whose ``comment_id`` occurs more than once in the file (every copy is rejected).
    expired_comment_ids
        Lines for a stored comment that may no longer be processed: its source's permission
        has expired (or the comment was withdrawn). Reason ``source_expired``.
    stale_texts
        Lines whose ``text_hash`` differs from the stored text's hash: the extraction was made
        from another version of the comment. Reason ``stale_text``.
    comments_loaded
        Comments whose validated extraction replaced the stored one.
    status_counts
        Final parse status of the loaded comments (``parsed``/``partial``/``unresolved``).
    mentions_accepted, mentions_rejected
        Mentions stored / dropped.
    facts_accepted, facts_rejected
        Environment facts stored / dropped.
    unresolved_identities
        Accepted mentions with neither a crop nor a variety.
    rejections
        Every rejection, ordered by line, mention and fact.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    lines_read: NonNegativeInt
    schema_errors: NonNegativeInt
    unknown_comment_ids: NonNegativeInt
    duplicate_comment_ids: NonNegativeInt
    expired_comment_ids: NonNegativeInt
    stale_texts: NonNegativeInt
    comments_loaded: NonNegativeInt
    status_counts: dict[ParseStatus, NonNegativeInt]
    mentions_accepted: NonNegativeInt
    mentions_rejected: NonNegativeInt
    facts_accepted: NonNegativeInt
    facts_rejected: NonNegativeInt
    unresolved_identities: NonNegativeInt
    rejections: tuple[Rejection, ...]


class _RejectedError(Exception):
    """Internal signal that the record under validation fails with ``reason``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _Comment:
    """The stored comment a parsed line is validated against, with its precomputed spans.

    Every span tuple is sorted and its spans are disjoint.
    """

    comment_id: str
    text: str
    hypothetical: tuple[_Span, ...]  # sentences that contain a hypothetical marker
    questions: tuple[_Span, ...]  # sentences that end with "?"
    variety_names: tuple[_Span, ...]  # merged occurrences of every mention's variety name


@dataclass(frozen=True)
class _Draft:
    """An accepted mention before the cross-mention check of rule 7 (``facts`` still empty)."""

    index: int
    mention: ValidatedMention
    facts: tuple[_Indexed, ...]


# --------------------------------------------------------------------------- validation


def validate_parsed_comment(
    parsed: ParsedComment, text: str, cfg: ExtractionConfig
) -> ValidatedComment:
    """Check one extractor record against the stored comment text (rules 1-8).

    Invalid mentions and facts are dropped and recorded as rejections; everything else is
    normalised. A comment with zero mentions is valid. The extractor's status is kept, except
    that ``parsed`` becomes ``partial`` when anything was rejected. ``parsed.text_hash`` is not
    checked here (see :func:`load_extractions`).

    Parameters
    ----------
    parsed
        One line of extractor output.
    text
        The exact stored text of ``parsed.comment_id``.
    cfg
        Validation settings (hypothetical/advice markers).

    Returns
    -------
    ValidatedComment
        Accepted mentions and facts, the final status, the extractor's warnings and the
        rejections (``line_number`` unset), ordered by mention and fact index.

    Raises
    ------
    ValueError
        If a configured hypothetical marker is blank.
    """
    comment = _comment(parsed, text, _marker_pattern(cfg.hypothetical_markers))
    drafts, rejections = _validate_mentions(comment, parsed.mentions)
    mentions, conflicts = _drop_shared_conflicts(comment.comment_id, drafts)
    rejections.extend(conflicts)
    return ValidatedComment(
        comment_id=parsed.comment_id,
        extractor_version=parsed.extractor_version,
        status=_final_status(parsed.status, rejections),
        mentions=tuple(mentions),
        warnings=tuple(parsed.warnings),
        rejections=tuple(sorted(rejections, key=_rejection_order)),
    )


def _comment(parsed: ParsedComment, text: str, pattern: re.Pattern[str]) -> _Comment:
    sentences = _sentence_spans(text)
    names = [m.variety_as_written for m in parsed.mentions if m.variety_as_written]
    return _Comment(
        comment_id=parsed.comment_id,
        text=text,
        hypothetical=tuple(s for s in sentences if pattern.search(text, *s)),
        questions=tuple(s for s in sentences if text[s[1] - 1] == _QUESTION_MARK),
        variety_names=_merged(span for name in names for span in _name_spans(text, name)),
    )


def _final_status(status: ParseStatus, rejections: Sequence[Rejection]) -> ParseStatus:
    return "partial" if status == "parsed" and rejections else status


def _rejection(
    comment_id: str | None,
    reason: str,
    mention_index: int | None = None,
    fact_index: int | None = None,
) -> Rejection:
    return Rejection(
        comment_id=comment_id,
        mention_index=mention_index,
        fact_index=fact_index,
        reason=reason,
    )


def _rejection_order(rejection: Rejection) -> tuple[int, int, int]:
    def position(value: int | None) -> int:
        return -1 if value is None else value

    return (
        position(rejection.line_number),
        position(rejection.mention_index),
        position(rejection.fact_index),
    )


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise _RejectedError(reason)


# --- spans


def _whole_word(value: str) -> re.Pattern[str]:
    return re.compile(_WHOLE_WORD.format(re.escape(value)), re.IGNORECASE)


def _word_occurrences(text: str, value: str) -> list[_Span]:
    """Spans of ``value`` as whole words in ``text`` (plural suffix excluded).

    Matching is case-insensitive, but when ``value`` occurs with its exact case only those
    occurrences count, so normalisation sees the source's own casing ("IN" vs the word "in").
    """
    found = list(_whole_word(value).finditer(text))
    exact = [match for match in found if match["value"] == value]
    return [match.span("value") for match in exact or found]


def _name_spans(text: str, name: str) -> list[_Span]:
    return [match.span() for match in _whole_word(name).finditer(text)]


def _merged(spans: Iterable[_Span]) -> tuple[_Span, ...]:
    """Sort spans and merge overlapping ones into disjoint spans."""
    merged: list[_Span] = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return tuple(merged)


def _span_start(span: _Span) -> int:
    return span[0]


def _span_end(span: _Span) -> int:
    return span[1]


def _overlaps(spans: Sequence[_Span], start: int, end: int) -> bool:
    """Whether ``[start, end)`` overlaps one of the sorted, disjoint ``spans``."""
    index = bisect_right(spans, start, key=_span_end)
    return index < len(spans) and spans[index][0] < end


def _inside(spans: Sequence[_Span], start: int, end: int) -> bool:
    """Whether ``[start, end)`` lies within one of the sorted, disjoint ``spans``."""
    index = bisect_right(spans, start, key=_span_start) - 1
    return index >= 0 and spans[index][1] >= end


def _occurrence_starts(text: str, quote: str) -> Iterator[int]:
    start = text.find(quote)
    while start != -1:
        yield start
        start = text.find(quote, start + 1)


# --- rules 1-3: mentions


def _validate_mentions(
    comment: _Comment, mentions: Sequence[VarietyMention]
) -> tuple[list[_Draft], list[Rejection]]:
    drafts: list[_Draft] = []
    rejections: list[Rejection] = []
    for index, mention in enumerate(mentions):
        draft, problems = _validate_mention(comment, index, mention)
        rejections.extend(problems)
        if draft is not None:
            drafts.append(draft)
    return drafts, rejections


def _validate_mention(
    comment: _Comment, index: int, mention: VarietyMention
) -> tuple[_Draft | None, list[Rejection]]:
    try:
        crop, key = _mention_identity(comment.text, mention)
    except _RejectedError as rejected:
        return None, [_rejection(comment.comment_id, rejected.reason, index)]
    facts, rejections = _validate_facts(comment, index, mention.environment)
    validated = ValidatedMention(
        crop=crop,
        variety_as_written=mention.variety_as_written,
        variety_key=key,
        topics=tuple(dict.fromkeys(mention.topics)),
        evidence_quote=mention.evidence_quote,
        facts=(),
    )
    return _Draft(index, validated, facts), rejections


def _mention_identity(text: str, mention: VarietyMention) -> tuple[str | None, str | None]:
    """Apply rules 1-3 and return the normalised crop and variety key."""
    _require(mention.evidence_quote in text, _MENTION_QUOTE_NOT_SUBSTRING)
    variety = mention.variety_as_written
    if variety is not None:
        _require(bool(_word_occurrences(mention.evidence_quote, variety)), _VARIETY_NOT_IN_QUOTE)
    crop = _normalized_crop(mention.crop)
    if crop is not None and variety is None:
        _require(_names_crop(mention.evidence_quote, crop), _CROP_NOT_IN_QUOTE)
    return crop, _variety_key(variety)


def _normalized_crop(crop: str | None) -> str | None:
    if crop is None:
        return None
    try:
        return normalize_crop(crop)
    except ValueError:
        raise _RejectedError(_INVALID_CROP) from None


def _names_crop(quote: str, crop: str) -> bool:
    try:
        return mentions_crop(quote, crop)
    except ValueError:
        raise _RejectedError(_INVALID_CROP) from None


def _variety_key(variety: str | None) -> str | None:
    if variety is None:
        return None
    try:
        return variety_key(variety)
    except ValueError:
        raise _RejectedError(_INVALID_VARIETY) from None


# --- rules 4-6: facts


def _validate_facts(
    comment: _Comment, mention_index: int, facts: Sequence[EnvironmentFact]
) -> tuple[tuple[_Indexed, ...], list[Rejection]]:
    """Apply rules 4-6 to each fact, then rule 7 within the mention."""
    accepted, rejections = _check_each(comment, mention_index, facts)
    conflicting = _conflicting_keys(fact for _, fact in accepted)
    kept, conflicts = _partition(
        comment.comment_id, mention_index, accepted, partial(_has_key, conflicting)
    )
    return kept, [*rejections, *conflicts]


def _check_each(
    comment: _Comment, mention_index: int, facts: Sequence[EnvironmentFact]
) -> tuple[list[_Indexed], list[Rejection]]:
    accepted: list[_Indexed] = []
    rejections: list[Rejection] = []
    for fact_index, fact in enumerate(facts):
        try:
            accepted.append((fact_index, _validate_fact(comment, fact)))
        except _RejectedError as rejected:
            rejections.append(
                _rejection(comment.comment_id, rejected.reason, mention_index, fact_index)
            )
    return accepted, rejections


def _validate_fact(comment: _Comment, fact: EnvironmentFact) -> ValidatedFact:
    """Apply rules 4-6 to one fact."""
    quote = fact.evidence_quote
    _require(quote in comment.text, _FACT_QUOTE_NOT_SUBSTRING)
    found = _word_occurrences(quote, fact.value_as_written)
    _require(bool(found), _VALUE_NOT_IN_QUOTE)
    _require(not _in_variety_name(comment, quote, found), _VALUE_IN_VARIETY_NAME)
    normalized = _normalized_value(fact, found)
    _require(not _in_sentences(comment.text, comment.hypothetical, quote), _HYPOTHETICAL_CONTEXT)
    _require(not _in_sentences(comment.text, comment.questions, quote), _QUESTION_CONTEXT)
    return ValidatedFact.model_validate({**fact.model_dump(), "normalized_value": normalized})


def _in_variety_name(comment: _Comment, quote: str, found: Sequence[_Span]) -> bool:
    """Rule 4: whether some occurrence of the value lies wholly inside a variety name."""
    return bool(comment.variety_names) and any(
        _inside(comment.variety_names, offset + start, offset + end)
        for offset in _occurrence_starts(comment.text, quote)
        for start, end in found
    )


def _in_sentences(text: str, sentences: Sequence[_Span], quote: str) -> bool:
    """Whether any occurrence of ``quote`` in ``text`` overlaps one of ``sentences``."""
    return bool(sentences) and any(
        _overlaps(sentences, start, start + len(quote))
        for start in _occurrence_starts(text, quote)
    )


# --- rule 5: normalised value


def _normalized_value(fact: EnvironmentFact, found: Sequence[_Span]) -> str | None:
    """Apply rule 5 at every position of the value in the quote; ``None`` stays display-only."""
    given = fact.normalized_value
    written = [fact.evidence_quote[start:end] for start, end in found]
    if given is None:
        others = any(_names_other_strict_key(fact.key, phrase) for phrase in written)
        _require(not others, _STRICT_KEY_MISMATCH)
        return None
    _require(is_valid_normalized(fact.key, given), _INVALID_NORMALIZED_VALUE)
    spans = resolve_spans(fact.key, fact.evidence_quote)
    for position, phrase in zip(found, written, strict=True):
        _check_position(fact.key, given, _covering(spans, position), position, phrase)
    return given


def _names_other_strict_key(key: EnvKey, written: str) -> bool:
    """Whether a strict-key phrase is really another strict key's value ("zone 6b" as region)."""
    others = sorted(STRICT_KEYS - {key}) if key in STRICT_KEYS else []
    return any(normalize_written(other, written) is not None for other in others)


def _covering(spans: Sequence[WrittenSpan], position: _Span) -> list[WrittenSpan]:
    start, end = position
    return [span for span in spans if span.start < end and start < span.end]


def _check_position(
    key: EnvKey, given: str, covering: Sequence[WrittenSpan], position: _Span, written: str
) -> None:
    """Rule 5 for one occurrence: the quote's own reading at that position must be ``given``.

    Without a resolved span there, the occurrence is normalised on its own instead.
    """
    if not covering:
        _check_derived(key, given, normalize_written(key, written))
        return
    _require(all(span.kind != "negated" for span in covering), _NEGATED_VALUE)
    named = {span.value for span in covering if span.kind == "value"}
    _require(bool(named) and not _blocked_around(covering, position), _BLOCKED_VALUE)
    _require(given in named and named <= _allowed_with(key, given), _NORMALIZED_VALUE_MISMATCH)


def _blocked_around(covering: Sequence[WrittenSpan], position: _Span) -> bool:
    """Whether a look-alike extends past the value ("Kansas" in "Kansas City")."""
    start, end = position
    return any(
        span.kind == "blocked" and (span.start < start or span.end > end) for span in covering
    )


def _allowed_with(key: EnvKey, given: str) -> set[str | None]:
    """Values that may share the position with ``given``: a region subdivision's own country."""
    return {given, given.split("-", 1)[0]} if key == "region" else {given}


def _check_derived(key: EnvKey, given: str, derived: str | None) -> None:
    if derived is None:
        _require(key not in STRICT_KEYS, _STRICT_KEY_MISMATCH)
    else:
        _require(derived == given, _NORMALIZED_VALUE_MISMATCH)


# --- rule 7: conflicting regions / zones


def _conflicting_keys(facts: Iterable[ValidatedFact]) -> frozenset[str]:
    """Keys of rule 7 (region, zone) that hold two or more distinct non-null values."""
    values = {
        (fact.key, fact.normalized_value)
        for fact in facts
        if fact.key in _CONFLICT_REASONS and fact.normalized_value is not None
    }
    counts = Counter(key for key, _ in values)
    return frozenset(key for key, n in counts.items() if n > 1)


def _has_key(keys: frozenset[str], fact: ValidatedFact) -> bool:
    return fact.key in keys


def _is_shared_with_key(keys: frozenset[str], fact: ValidatedFact) -> bool:
    return fact.applicability == _WHOLE_COMMENT and fact.key in keys


def _partition(
    comment_id: str,
    mention_index: int,
    facts: Sequence[_Indexed],
    conflicts: Callable[[ValidatedFact], bool],
) -> tuple[tuple[_Indexed, ...], list[Rejection]]:
    """Split facts into kept ones and a conflict rejection for each one ``conflicts`` flags."""
    kept = tuple(item for item in facts if not conflicts(item[1]))
    dropped = [
        _rejection(comment_id, _CONFLICT_REASONS[fact.key], mention_index, index)
        for index, fact in facts
        if conflicts(fact)
    ]
    return kept, dropped


def _drop_shared_conflicts(
    comment_id: str, drafts: Sequence[_Draft]
) -> tuple[list[ValidatedMention], list[Rejection]]:
    """Rule 7 across mentions: disagreeing ``whole_comment`` regions (or zones) all go."""
    shared = [f for draft in drafts for _, f in draft.facts if f.applicability == _WHOLE_COMMENT]
    conflicts = partial(_is_shared_with_key, _conflicting_keys(shared))
    mentions: list[ValidatedMention] = []
    rejections: list[Rejection] = []
    for draft in drafts:
        kept, dropped = _partition(comment_id, draft.index, draft.facts, conflicts)
        mentions.append(draft.mention.model_copy(update={"facts": tuple(f for _, f in kept)}))
        rejections.extend(dropped)
    return mentions, rejections


# --- rule 6: hypothetical / advice / question guard


@lru_cache(maxsize=16)
def _marker_pattern(markers: tuple[str, ...]) -> re.Pattern[str]:
    """Compile the markers into one case-insensitive, word-bounded alternation."""
    alternatives = sorted({_marker_regex(marker) for marker in markers})
    return re.compile(rf"(?<!\w)(?:{'|'.join(alternatives)})(?!\w)", re.IGNORECASE)


def _marker_regex(marker: str) -> str:
    words = marker.split()
    if not words:
        raise ValueError("extraction.hypothetical_markers must not contain a blank marker")
    return r"\s+".join(_word_regex(word) for word in words)


def _word_regex(word: str) -> str:
    return "".join(_APOSTROPHE_CLASS if char in _APOSTROPHES else re.escape(char) for char in word)


def _ends_sentence(text: str, delimiter: re.Match[str]) -> bool:
    """Whether a delimiter ends its sentence (a full stop after an abbreviation does not)."""
    if delimiter.group() != ".":
        return True
    end = delimiter.start()
    word = _TRAILING_WORD.search(text, max(0, end - _TRAILING_WORD_WINDOW), end)
    return word is None or word.group().casefold() not in _ABBREVIATIONS


def _sentence_spans(text: str) -> list[_Span]:
    """Split on ``.``, ``!``, ``?`` and line breaks; each delimiter ends its sentence.

    A ``.`` ends a sentence only when followed by whitespace, a closing quote or bracket, or
    the end of the text, and not after a known abbreviation (:data:`_ABBREVIATIONS`).
    """
    ends = [m.end() for m in _SENTENCE_END.finditer(text) if _ends_sentence(text, m)]
    starts = [0, *ends]
    bounds = zip(starts, [*ends, len(text)], strict=True)
    return [(start, end) for start, end in bounds if start < end]


# --------------------------------------------------------------------------- reading


def read_extraction_jsonl(
    path: Path,
) -> tuple[list[tuple[int, ParsedComment]], list[Rejection]]:
    """Parse an extractor JSONL file, turning every bad line into a ``schema_error`` rejection.

    Lines are split on line feeds only (raw U+2028 is legal inside JSON strings) and numbered from
    1 by physical line; blank lines are skipped; a leading UTF-8 BOM is ignored. A line fails
    when it is not UTF-8, not JSON, repeats a key, or does not match ``ParsedComment`` (unknown
    keys, bad enums, empty or over-long quotes, a missing or malformed ``text_hash``). Its
    rejection keeps the line's ``comment_id`` when that is a non-empty string, so duplicate
    detection still sees it; no other content is kept.

    Parameters
    ----------
    path
        The JSONL file.

    Returns
    -------
    tuple of (list of (int, ParsedComment), list of Rejection)
        Valid records with their line numbers, and one rejection per invalid line, both in
        file order.

    Raises
    ------
    ValueError
        If the file cannot be read.
    """
    records: list[tuple[int, ParsedComment]] = []
    rejections: list[Rejection] = []
    for number, raw in _numbered_lines(_read_bytes(path)):
        outcome = _parse_line(number, raw)
        if isinstance(outcome, Rejection):
            rejections.append(outcome)
        else:
            records.append((number, outcome))
    return records, rejections


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{path}: cannot read extraction file ({type(exc).__name__})") from exc


def _numbered_lines(data: bytes) -> Iterator[tuple[int, bytes]]:
    for number, raw in enumerate(data.removeprefix(_UTF8_BOM).split(b"\n"), start=1):
        if raw.strip():
            yield number, raw


def _parse_line(number: int, raw: bytes) -> ParsedComment | Rejection:
    try:
        value: object = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_keys)
    except (ValueError, RecursionError):  # includes UnicodeDecodeError and JSONDecodeError
        return _schema_error(number, None)
    try:
        return ParsedComment.model_validate(value)
    except ValidationError:
        return _schema_error(number, _comment_id_hint(value))


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key in JSON object")
    return dict(pairs)


def _comment_id_hint(value: object) -> str | None:
    comment_id = value.get("comment_id") if isinstance(value, dict) else None
    return comment_id if isinstance(comment_id, str) and comment_id else None


def _schema_error(number: int, comment_id: str | None) -> Rejection:
    return Rejection(comment_id=comment_id, line_number=number, reason=_SCHEMA_ERROR)


# --------------------------------------------------------------------------- loading


@dataclass(frozen=True)
class _Loaded:
    """A validated line that will replace its comment's stored extraction."""

    line_number: int
    parsed: ParsedComment
    validated: ValidatedComment


@dataclass(frozen=True)
class _Stored:
    """What the database holds for the ids of one extraction file."""

    known: frozenset[str]
    """Every stored id, whether or not it may still be processed."""
    extractable: Mapping[str, tuple[str, str]]
    """``comment_id -> (text, text_hash)`` for the comments that may still be processed."""


def load_extractions(
    conn: sqlite3.Connection,
    path: Path,
    cfg: ExtractionConfig,
    loaded_at: str,
    *,
    now: str | None = None,
) -> ExtractionReport:
    """Validate an extractor JSONL file against stored comments and load it atomically.

    Lines with a ``comment_id`` that occurs more than once in the file are all rejected
    (``duplicate_comment_id``); ids not in the database are rejected (``unknown_comment_id``),
    as are stored comments that may no longer be processed because their source expired at
    ``now`` or they were withdrawn (``source_expired``) and lines whose ``text_hash`` is not
    the stored text's hash (``stale_text``: the extraction was made from another version of the
    text). Nothing is written for a rejected line, so its comment stays in
    ``export-for-extraction --only-unparsed`` unless it already had an extraction. Every other
    valid line is checked with :func:`validate_parsed_comment` and replaces that comment's
    stored extraction. The whole load is one transaction: if storing fails, nothing changes.
    Reloading the same file leaves identical rows. Only counts and rejection reasons are
    logged.

    Parameters
    ----------
    conn
        Open connection from :func:`crop_explorer.db.connect`.
    path
        Extractor output, one ``ParsedComment`` per line.
    cfg
        Validation settings.
    loaded_at
        Canonical UTC load time (``schemas.to_utc_iso``), stored on each parse.
    now
        Canonical UTC time at which source expiry is judged; defaults to ``loaded_at`` (a load
        happens at its load time).

    Returns
    -------
    ExtractionReport
        Aggregate counts and every rejection, each with its line number.

    Raises
    ------
    ValueError
        If ``loaded_at`` or ``now`` is not canonical UTC, the file cannot be read, or a
        hypothetical marker is blank. Nothing is written in that case.
    """
    expiry_time = _expiry_time(loaded_at, now)
    _marker_pattern(cfg.hypothetical_markers)
    records, schema_rejections = read_extraction_jsonl(path)
    with _atomic(conn):
        stored = _stored(conn, records, expiry_time)
        loadable, line_rejections = _triage(records, schema_rejections, stored)
        loaded = [_validated_line(number, parsed, stored, cfg) for number, parsed in loadable]
        for item in loaded:
            db.replace_extraction(conn, item.validated, loaded_at)
    lines_read = len(records) + len(schema_rejections)
    report = _report(lines_read, [*schema_rejections, *line_rejections], loaded)
    _log_report(path, report)
    return report


def _expiry_time(loaded_at: str, now: str | None) -> str:
    require_utc_iso(loaded_at, "loaded_at")
    return loaded_at if now is None else require_utc_iso(now, "now")


@contextmanager
def _atomic(conn: sqlite3.Connection) -> Iterator[None]:
    """Run the block in one savepoint: release on success, roll back and re-raise on error."""
    conn.execute(f"SAVEPOINT {_SAVEPOINT}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO {_SAVEPOINT}")
        conn.execute(f"RELEASE {_SAVEPOINT}")
        raise
    conn.execute(f"RELEASE {_SAVEPOINT}")


def _stored(
    conn: sqlite3.Connection, records: Sequence[tuple[int, ParsedComment]], now: str
) -> _Stored:
    ids = sorted({parsed.comment_id for _, parsed in records})
    return _Stored(frozenset(db.stored_texts(conn, ids)), db.extractable_texts(conn, ids, now))


def _validated_line(
    number: int, parsed: ParsedComment, stored: _Stored, cfg: ExtractionConfig
) -> _Loaded:
    text, _ = stored.extractable[parsed.comment_id]
    return _Loaded(number, parsed, validate_parsed_comment(parsed, text, cfg))


def _triage(
    records: Sequence[tuple[int, ParsedComment]],
    schema_rejections: Sequence[Rejection],
    stored: _Stored,
) -> tuple[list[tuple[int, ParsedComment]], list[Rejection]]:
    """Split valid lines into loadable ones and line-level rejections."""
    duplicates = _duplicated_ids(records, schema_rejections)
    loadable: list[tuple[int, ParsedComment]] = []
    rejections: list[Rejection] = []
    for number, parsed in records:
        reason = _line_problem(parsed, duplicates, stored)
        if reason is None:
            loadable.append((number, parsed))
        else:
            rejections.append(
                Rejection(comment_id=parsed.comment_id, line_number=number, reason=reason)
            )
    return loadable, rejections


def _duplicated_ids(
    records: Sequence[tuple[int, ParsedComment]], schema_rejections: Sequence[Rejection]
) -> frozenset[str]:
    ids = [parsed.comment_id for _, parsed in records]
    ids.extend(r.comment_id for r in schema_rejections if r.comment_id is not None)
    return frozenset(comment_id for comment_id, n in Counter(ids).items() if n > 1)


def _line_problem(
    parsed: ParsedComment, duplicates: frozenset[str], stored: _Stored
) -> str | None:
    """Return the line-level rejection reason of a valid line, or ``None`` if it may load."""
    comment_id = parsed.comment_id
    if comment_id in duplicates:
        return _DUPLICATE_COMMENT_ID
    if comment_id not in stored.known:
        return _UNKNOWN_COMMENT_ID
    if comment_id not in stored.extractable:
        return _SOURCE_EXPIRED
    _, text_hash = stored.extractable[comment_id]
    return None if parsed.text_hash == text_hash else _STALE_TEXT


def _numbered(item: _Loaded) -> list[Rejection]:
    return [
        r.model_copy(update={"line_number": item.line_number}) for r in item.validated.rejections
    ]


def _report(
    lines_read: int, line_rejections: Sequence[Rejection], loaded: Sequence[_Loaded]
) -> ExtractionReport:
    rejections = [*line_rejections, *(r for item in loaded for r in _numbered(item))]
    reasons = Counter(rejection.reason for rejection in line_rejections)
    return ExtractionReport.model_validate(
        {
            "lines_read": lines_read,
            **{field: reasons[reason] for field, reason in _LINE_BUCKETS.items()},
            **_record_counts(loaded),
            "rejections": tuple(sorted(rejections, key=_rejection_order)),
        }
    )


def _record_counts(loaded: Sequence[_Loaded]) -> dict[str, object]:
    """Comment, status, mention and fact counts of the loaded lines."""
    accepted = [m for item in loaded for m in item.validated.mentions]
    proposed = [m for item in loaded for m in item.parsed.mentions]
    facts_accepted = sum(len(m.facts) for m in accepted)
    return {
        "comments_loaded": len(loaded),
        "status_counts": _status_counts(item.validated for item in loaded),
        "mentions_accepted": len(accepted),
        "mentions_rejected": len(proposed) - len(accepted),
        "facts_accepted": facts_accepted,
        "facts_rejected": sum(len(m.environment) for m in proposed) - facts_accepted,
        "unresolved_identities": sum(_is_unresolved(m) for m in accepted),
    }


def _is_unresolved(mention: ValidatedMention) -> bool:
    return mention.crop is None and mention.variety_as_written is None


def _status_counts(validated: Iterator[ValidatedComment]) -> dict[ParseStatus, int]:
    counts = Counter(comment.status for comment in validated)
    return {status: counts[status] for status in _STATUSES}


def _log_report(path: Path, report: ExtractionReport) -> None:
    logger.info(_REPORT_LOG, path.name, *(getattr(report, name) for name in _REPORT_LOG_FIELDS))
    reasons = Counter(rejection.reason for rejection in report.rejections)
    if reasons:
        summary = ", ".join(f"{reason}={n}" for reason, n in sorted(reasons.items()))
        logger.warning("Extraction rejections in {}: {}", path.name, summary)
