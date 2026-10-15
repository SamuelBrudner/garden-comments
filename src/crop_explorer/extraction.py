"""Validate offline extractor output against stored comment text and load it (SPEC §4, §8).

The model step writes one :class:`~crop_explorer.schemas.ParsedComment` per JSONL line. Nothing
from it is trusted: every mention and fact is checked against the exact stored comment text
(docs/ARCHITECTURE.md, "Extraction validation and loading", rules 1-8). Failures never abort a
load. Each dropped line, mention or fact is recorded as a
:class:`~crop_explorer.schemas.Rejection` that carries ids, indexes and a machine-readable reason
only. Comment bodies, evidence quotes and extractor warnings are never logged.

Rejection reasons: ``schema_error``, ``unknown_comment_id``, ``duplicate_comment_id``,
``mention_quote_not_substring``, ``variety_not_in_quote``, ``invalid_crop``, ``invalid_variety``,
``fact_quote_not_substring``, ``value_not_in_quote``, ``invalid_normalized_value``,
``strict_key_mismatch``, ``normalized_value_mismatch``, ``hypothetical_context``,
``conflicting_region`` and ``conflicting_zone``. Rejection ``mention_index``/``fact_index``
are positions in the extractor's input lists; the indexes stored in the database are positions
among the accepted records.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, get_args

from loguru import logger
from pydantic import BaseModel, ConfigDict, NonNegativeInt, ValidationError

from crop_explorer import db
from crop_explorer.config import ExtractionConfig
from crop_explorer.normalization import STRICT_KEYS, normalize_crop, normalize_written, variety_key
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
_MENTION_QUOTE_NOT_SUBSTRING: Final = "mention_quote_not_substring"
_VARIETY_NOT_IN_QUOTE: Final = "variety_not_in_quote"
_INVALID_CROP: Final = "invalid_crop"
_INVALID_VARIETY: Final = "invalid_variety"
_FACT_QUOTE_NOT_SUBSTRING: Final = "fact_quote_not_substring"
_VALUE_NOT_IN_QUOTE: Final = "value_not_in_quote"
_INVALID_NORMALIZED_VALUE: Final = "invalid_normalized_value"
_STRICT_KEY_MISMATCH: Final = "strict_key_mismatch"
_NORMALIZED_VALUE_MISMATCH: Final = "normalized_value_mismatch"
_HYPOTHETICAL_CONTEXT: Final = "hypothetical_context"
_CONFLICT_REASONS: Final[Mapping[str, str]] = MappingProxyType(
    {"region": "conflicting_region", "zone": "conflicting_zone"}
)

_STATUSES: Final[tuple[ParseStatus, ...]] = get_args(ParseStatus)
_SENTENCE_END: Final = re.compile(r"[!?\n]|\.(?=[\s\"')\]\u201d\u2019]|\Z)")
"""``!``, ``?`` and line breaks always end a sentence; ``.`` only before space, a closing
quote or bracket, or the end of the text (so ``5.5 gallon`` and ``U.S.A`` stay whole)."""
_TRAILING_WORD: Final = re.compile(r"(?<![\w.])[^\W\d_]+(?:\.[^\W\d_]+)*\Z")
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
_APOSTROPHES: Final = "'\u2019\u2018\u02bc"
_APOSTROPHE_CLASS: Final = f"[{_APOSTROPHES}]"
_UTF8_BOM: Final = b"\xef\xbb\xbf"
_SAVEPOINT: Final = "crop_explorer_load_extractions"

_Span = tuple[int, int]


class ExtractionReport(BaseModel):
    """Aggregate outcome of loading one extraction JSONL file: counts, ids and reasons only.

    Every non-blank line lands in exactly one of ``schema_errors``, ``duplicate_comment_ids``,
    ``unknown_comment_ids`` or ``comments_loaded``, so those four add up to ``lines_read``.
    Mention and fact counts cover the loaded comments only; facts of a rejected mention count as
    rejected.

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
    """The stored comment a parsed line is validated against."""

    comment_id: str
    text: str
    hypothetical: tuple[_Span, ...]  # sentences that contain a hypothetical marker


# --------------------------------------------------------------------------- validation


def validate_parsed_comment(
    parsed: ParsedComment, text: str, cfg: ExtractionConfig
) -> ValidatedComment:
    """Check one extractor record against the stored comment text (rules 1-8).

    Invalid mentions and facts are dropped and recorded as rejections; everything else is
    normalised. A comment with zero mentions is valid. The extractor's status is kept, except
    that ``parsed`` becomes ``partial`` when anything was rejected.

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
    pattern = _marker_pattern(cfg.hypothetical_markers)
    comment = _Comment(parsed.comment_id, text, _hypothetical_sentences(text, pattern))
    mentions: list[ValidatedMention] = []
    rejections: list[Rejection] = []
    for index, mention in enumerate(parsed.mentions):
        kept, problems = _validate_mention(comment, index, mention)
        rejections.extend(problems)
        if kept is not None:
            mentions.append(kept)
    return ValidatedComment(
        comment_id=parsed.comment_id,
        extractor_version=parsed.extractor_version,
        status=_final_status(parsed.status, rejections),
        mentions=tuple(mentions),
        warnings=tuple(parsed.warnings),
        rejections=tuple(sorted(rejections, key=_rejection_order)),
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


def _as_written(quote: str, value: str, reason: str) -> str:
    """Return ``value`` as it is spelled in ``quote`` (matched case-insensitively), or reject.

    An exact-case occurrence wins; otherwise the first case-insensitive occurrence is used, so
    normalisation sees the source's own casing (``"IN"`` vs the word ``"in"``).
    """
    if value in quote:
        return value
    match = re.search(re.escape(value), quote, re.IGNORECASE)
    if match is None:
        raise _RejectedError(reason)
    return match.group()


# --- rules 1-3: mentions


def _validate_mention(
    comment: _Comment, index: int, mention: VarietyMention
) -> tuple[ValidatedMention | None, list[Rejection]]:
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
        facts=facts,
    )
    return validated, rejections


def _mention_identity(text: str, mention: VarietyMention) -> tuple[str | None, str | None]:
    """Apply rules 1-3 and return the normalised crop and variety key."""
    _require(mention.evidence_quote in text, _MENTION_QUOTE_NOT_SUBSTRING)
    variety = mention.variety_as_written
    if variety is not None:
        _as_written(mention.evidence_quote, variety, _VARIETY_NOT_IN_QUOTE)
    return _normalized_crop(mention.crop), _variety_key(variety)


def _normalized_crop(crop: str | None) -> str | None:
    if crop is None:
        return None
    try:
        return normalize_crop(crop)
    except ValueError:
        raise _RejectedError(_INVALID_CROP) from None


def _variety_key(variety: str | None) -> str | None:
    if variety is None:
        return None
    try:
        return variety_key(variety)
    except ValueError:
        raise _RejectedError(_INVALID_VARIETY) from None


# --- rules 4-7: facts


def _validate_facts(
    comment: _Comment, mention_index: int, facts: Sequence[EnvironmentFact]
) -> tuple[tuple[ValidatedFact, ...], list[Rejection]]:
    accepted: list[tuple[int, ValidatedFact]] = []
    rejections: list[Rejection] = []
    for fact_index, fact in enumerate(facts):
        try:
            accepted.append((fact_index, _validate_fact(comment, fact)))
        except _RejectedError as rejected:
            rejections.append(
                _rejection(comment.comment_id, rejected.reason, mention_index, fact_index)
            )
    kept, conflicts = _drop_conflicts(accepted)
    rejections.extend(
        _rejection(comment.comment_id, reason, mention_index, fact_index)
        for fact_index, reason in conflicts
    )
    return tuple(fact for _, fact in kept), rejections


def _validate_fact(comment: _Comment, fact: EnvironmentFact) -> ValidatedFact:
    """Apply rules 4-6 to one fact."""
    _require(fact.evidence_quote in comment.text, _FACT_QUOTE_NOT_SUBSTRING)
    written = _as_written(fact.evidence_quote, fact.value_as_written, _VALUE_NOT_IN_QUOTE)
    normalized = _normalized_value(fact.key, written, fact.normalized_value)
    _require(not _is_hypothetical(comment, fact.evidence_quote), _HYPOTHETICAL_CONTEXT)
    return ValidatedFact(
        key=fact.key,
        value_as_written=fact.value_as_written,
        normalized_value=normalized,
        evidence_quote=fact.evidence_quote,
        applicability=fact.applicability,
        basis=fact.basis,
    )


def _normalized_value(key: EnvKey, written: str, given: str | None) -> str | None:
    """Apply rule 5: vocabulary check, strict-key recomputation, or derivation when absent."""
    derived = normalize_written(key, written)
    if given is None:
        _require(
            derived is not None or not _names_other_strict_key(key, written), _STRICT_KEY_MISMATCH
        )
        return derived
    _require(is_valid_normalized(key, given), _INVALID_NORMALIZED_VALUE)
    _require(key not in STRICT_KEYS or given == derived, _STRICT_KEY_MISMATCH)
    _require(derived is None or given == derived, _NORMALIZED_VALUE_MISMATCH)
    return given


def _names_other_strict_key(key: EnvKey, written: str) -> bool:
    """Whether a strict-key phrase is really another strict key's value ("zone 6b" as region)."""
    others = sorted(STRICT_KEYS - {key}) if key in STRICT_KEYS else []
    return any(normalize_written(other, written) is not None for other in others)


def _drop_conflicts(
    accepted: Sequence[tuple[int, ValidatedFact]],
) -> tuple[list[tuple[int, ValidatedFact]], list[tuple[int, str]]]:
    """Apply rule 7: two distinct regions (or zones) on one mention reject all of that key."""
    conflicting = {
        key for key in _CONFLICT_REASONS if len(_distinct_normalized(accepted, key)) > 1
    }
    kept = [(index, fact) for index, fact in accepted if fact.key not in conflicting]
    dropped = [
        (index, _CONFLICT_REASONS[fact.key]) for index, fact in accepted if fact.key in conflicting
    ]
    return kept, dropped


def _distinct_normalized(accepted: Sequence[tuple[int, ValidatedFact]], key: str) -> set[str]:
    return {
        fact.normalized_value
        for _, fact in accepted
        if fact.key == key and fact.normalized_value is not None
    }


# --- rule 6: hypothetical / advice guard


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
    word = _TRAILING_WORD.search(text, 0, delimiter.start())
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


def _hypothetical_sentences(text: str, pattern: re.Pattern[str]) -> tuple[_Span, ...]:
    return tuple(span for span in _sentence_spans(text) if pattern.search(text, *span))


def _occurrences(text: str, quote: str) -> Iterator[_Span]:
    start = text.find(quote)
    while start != -1:
        yield start, start + len(quote)
        start = text.find(quote, start + 1)


def _is_hypothetical(comment: _Comment, quote: str) -> bool:
    """Whether any occurrence of ``quote`` overlaps a sentence holding a marker."""
    return any(
        start < sentence_end and sentence_start < end
        for start, end in _occurrences(comment.text, quote)
        for sentence_start, sentence_end in comment.hypothetical
    )


# --------------------------------------------------------------------------- reading


def read_extraction_jsonl(
    path: Path,
) -> tuple[list[tuple[int, ParsedComment]], list[Rejection]]:
    """Parse an extractor JSONL file, turning every bad line into a ``schema_error`` rejection.

    Lines are split on line feeds only (raw U+2028 is legal inside JSON strings) and numbered from
    1 by physical line; blank lines are skipped; a leading UTF-8 BOM is ignored. A line fails
    when it is not UTF-8, not JSON, repeats a key, or does not match ``ParsedComment`` (unknown
    keys, bad enums, empty quotes). Its rejection keeps the line's ``comment_id`` when that is a
    non-empty string, so duplicate detection still sees it; no other content is kept.

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


def load_extractions(
    conn: sqlite3.Connection, path: Path, cfg: ExtractionConfig, loaded_at: str
) -> ExtractionReport:
    """Validate an extractor JSONL file against stored comments and load it atomically.

    Lines with a ``comment_id`` that occurs more than once in the file are all rejected
    (``duplicate_comment_id``); ids not in the database are rejected (``unknown_comment_id``);
    every other valid line is checked with :func:`validate_parsed_comment` and replaces that
    comment's stored extraction. The whole load is one transaction: if storing fails, nothing
    changes. Reloading the same file leaves identical rows. Only counts and rejection reasons
    are logged.

    Parameters
    ----------
    conn
        Open connection from :func:`crop_explorer.db.connect`.
    path
        Extractor output, one ``ParsedComment`` per line.
    cfg
        Validation settings.
    loaded_at
        Canonical UTC load time (``schemas.to_utc_iso``).

    Returns
    -------
    ExtractionReport
        Aggregate counts and every rejection, each with its line number.

    Raises
    ------
    ValueError
        If ``loaded_at`` is not canonical UTC, the file cannot be read, or a hypothetical
        marker is blank. Nothing is written in that case.
    """
    require_utc_iso(loaded_at, "loaded_at")
    _marker_pattern(cfg.hypothetical_markers)
    records, schema_rejections = read_extraction_jsonl(path)
    with _atomic(conn):
        texts = db.stored_texts(conn, sorted({parsed.comment_id for _, parsed in records}))
        loadable, line_rejections = _triage(records, schema_rejections, texts)
        loaded = [
            _Loaded(number, parsed, validate_parsed_comment(parsed, texts[parsed.comment_id], cfg))
            for number, parsed in loadable
        ]
        for item in loaded:
            db.replace_extraction(conn, item.validated, loaded_at)
    report = _report(
        len(records) + len(schema_rejections), [*schema_rejections, *line_rejections], loaded
    )
    _log_report(path, report)
    return report


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


def _triage(
    records: Sequence[tuple[int, ParsedComment]],
    schema_rejections: Sequence[Rejection],
    texts: Mapping[str, str],
) -> tuple[list[tuple[int, ParsedComment]], list[Rejection]]:
    """Split valid lines into loadable ones and duplicate/unknown-id rejections."""
    duplicates = _duplicated_ids(records, schema_rejections)
    loadable: list[tuple[int, ParsedComment]] = []
    rejections: list[Rejection] = []
    for number, parsed in records:
        reason = _line_problem(parsed.comment_id, duplicates, texts)
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
    comment_id: str, duplicates: frozenset[str], texts: Mapping[str, str]
) -> str | None:
    if comment_id in duplicates:
        return _DUPLICATE_COMMENT_ID
    if comment_id not in texts:
        return _UNKNOWN_COMMENT_ID
    return None


def _numbered(item: _Loaded) -> list[Rejection]:
    return [
        r.model_copy(update={"line_number": item.line_number}) for r in item.validated.rejections
    ]


def _report(
    lines_read: int, line_rejections: Sequence[Rejection], loaded: Sequence[_Loaded]
) -> ExtractionReport:
    rejections = [*line_rejections, *(r for item in loaded for r in _numbered(item))]
    reasons = Counter(r.reason for r in rejections)
    accepted = [m for item in loaded for m in item.validated.mentions]
    mentions_in = sum(len(item.parsed.mentions) for item in loaded)
    facts_in = sum(len(m.environment) for item in loaded for m in item.parsed.mentions)
    facts_accepted = sum(len(m.facts) for m in accepted)
    return ExtractionReport(
        lines_read=lines_read,
        schema_errors=reasons[_SCHEMA_ERROR],
        unknown_comment_ids=reasons[_UNKNOWN_COMMENT_ID],
        duplicate_comment_ids=reasons[_DUPLICATE_COMMENT_ID],
        comments_loaded=len(loaded),
        status_counts=_status_counts(item.validated for item in loaded),
        mentions_accepted=len(accepted),
        mentions_rejected=mentions_in - len(accepted),
        facts_accepted=facts_accepted,
        facts_rejected=facts_in - facts_accepted,
        unresolved_identities=sum(
            m.crop is None and m.variety_as_written is None for m in accepted
        ),
        rejections=tuple(sorted(rejections, key=_rejection_order)),
    )


def _status_counts(validated: Iterator[ValidatedComment]) -> dict[ParseStatus, int]:
    counts = Counter(comment.status for comment in validated)
    return {status: counts[status] for status in _STATUSES}


def _log_report(path: Path, report: ExtractionReport) -> None:
    logger.info(
        "Loaded extractions from {}: {} line(s); {} comment(s) loaded {}; {} schema error(s), "
        "{} unknown id(s), {} duplicate-id line(s); mentions {} accepted, {} rejected; "
        "facts {} accepted, {} rejected; {} mention(s) without crop or variety",
        path.name,
        report.lines_read,
        report.comments_loaded,
        report.status_counts,
        report.schema_errors,
        report.unknown_comment_ids,
        report.duplicate_comment_ids,
        report.mentions_accepted,
        report.mentions_rejected,
        report.facts_accepted,
        report.facts_rejected,
        report.unresolved_identities,
    )
    reasons = Counter(rejection.reason for rejection in report.rejections)
    if reasons:
        summary = ", ".join(f"{reason}={n}" for reason, n in sorted(reasons.items()))
        logger.warning("Extraction rejections in {}: {}", path.name, summary)
