"""Tests for extraction validation and loading (docs/ARCHITECTURE.md, SPEC §4 and §8).

Covers validation rules 1-8, the SPEC §11 required tests #2 (environment association), #3 (no
invented geography), #4 (location semantics), #5 (hypothetical exclusion) and #18 (provenance),
JSONL reading, and idempotent, all-or-nothing loading with an aggregate report that never
carries comment text.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from loguru import logger
from pydantic import ValidationError

from crop_explorer import db
from crop_explorer.config import ExtractionConfig, load_config
from crop_explorer.extraction import (
    ExtractionReport,
    load_extractions,
    read_extraction_jsonl,
    validate_parsed_comment,
)
from crop_explorer.regions import REGION_NAMES
from crop_explorer.schemas import (
    ENV_KEYS,
    MAX_QUOTE_CHARS,
    MAX_VALUE_CHARS,
    CommentRecord,
    EnvironmentFact,
    EnvKey,
    ParsedComment,
    ParseStatus,
    Rejection,
    SourceRecord,
    Topic,
    ValidatedComment,
    VarietyMention,
    to_utc_iso,
)
from crop_explorer.vocab import ZONE_VALUES

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "fixtures"
MEMORY = Path(":memory:")
LOADED = "2026-10-15T12:00:00Z"
LATER = "2026-10-16T09:30:00Z"
NOW = "2026-10-15T13:00:00Z"

MARKERS = ("i'd", "i would", "would try", "i'll", "should", "could", "might", "if you")
CFG = ExtractionConfig(hypothetical_markers=MARKERS)
REPO_CFG = load_config().extraction

KNOWN_REASONS = frozenset(
    {
        "schema_error",
        "unknown_comment_id",
        "duplicate_comment_id",
        "source_expired",
        "stale_text",
        "mention_quote_not_substring",
        "variety_not_in_quote",
        "crop_not_in_quote",
        "invalid_crop",
        "invalid_variety",
        "fact_quote_not_substring",
        "value_not_in_quote",
        "value_in_variety_name",
        "invalid_normalized_value",
        "negated_value",
        "blocked_value",
        "strict_key_mismatch",
        "normalized_value_mismatch",
        "hypothetical_context",
        "question_context",
        "conflicting_region",
        "conflicting_zone",
    }
)

TEXT_POTS = "Zone 6b here. I grow Sungold in pots on the deck and it loves the muggy summers."
TEXT_GREENHOUSE = (
    "I grew my Jalapeño peppers in the greenhouse, but my Roma tomatoes were outside in the "
    "main bed. The peppers loved the warmth."
)
TEXT_MAINE = "I live in Florida, but I grew these Stupice in Maine last summer."
TEXT_HYPOTHETICAL = "Those Glacier tomatoes look amazing. I'd try these in zone 5 next spring."
WHOLE = "whole_comment"

# --------------------------------------------------------------------------- builders


def fact(
    key: EnvKey,
    written: str,
    quote: str,
    normalized: str | None = None,
    *,
    applicability: Any = "this_mention",
    basis: Any = "reported_garden_context",
) -> EnvironmentFact:
    return EnvironmentFact(
        key=key,
        value_as_written=written,
        normalized_value=normalized,
        evidence_quote=quote,
        applicability=applicability,
        basis=basis,
    )


def mention(
    quote: str,
    *facts: EnvironmentFact,
    crop: str | None = None,
    variety: str | None = None,
    topics: Sequence[Topic] = ("general",),
) -> VarietyMention:
    return VarietyMention(
        crop=crop,
        variety_as_written=variety,
        topics=list(topics),
        evidence_quote=quote,
        environment=list(facts),
    )


def sha256(text: str) -> str:
    """The ``text_hash`` export-for-extraction writes for a comment text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parsed(
    comment_id: str,
    *mentions: VarietyMention,
    text: str = "",
    status: ParseStatus = "parsed",
    warnings: Sequence[str] = (),
) -> ParsedComment:
    """One extractor line; ``text_hash`` is copied from the exported ``text``."""
    return ParsedComment(
        comment_id=comment_id,
        text_hash=sha256(text),
        extractor_version="test-extractor-v1",
        mentions=list(mentions),
        status=status,
        warnings=list(warnings),
    )


def validate(
    text: str,
    *mentions: VarietyMention,
    status: ParseStatus = "parsed",
    cfg: ExtractionConfig = CFG,
) -> ValidatedComment:
    return validate_parsed_comment(parsed("c1", *mentions, text=text, status=status), text, cfg)


def reasons(result: ValidatedComment) -> list[tuple[int | None, int | None, str]]:
    return [(r.mention_index, r.fact_index, r.reason) for r in result.rejections]


def facts_of(result: ValidatedComment, index: int = 0) -> list[tuple[str, str | None]]:
    return [(f.key, f.normalized_value) for f in result.mentions[index].facts]


def sungold(*facts: EnvironmentFact) -> VarietyMention:
    """The Sungold mention of ``TEXT_POTS``."""
    return mention("I grow Sungold in pots", *facts, crop="tomato", variety="Sungold")


# --------------------------------------------------------------------------- db helpers


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = db.connect(MEMORY)
    yield connection
    connection.close()


def seed(
    conn: sqlite3.Connection,
    texts: dict[str, str],
    source_id: str = "src-a",
    expires_at: str | None = None,
) -> None:
    source = SourceRecord(
        source_id=source_id,
        kind="test_comments",
        rights_status="synthetic",
        imported_at=LOADED,
        expires_at=expires_at,
        dataset_hash="hash-a",
    )
    records = [
        CommentRecord(comment_id=cid, text=text, source_order=i)
        for i, (cid, text) in enumerate(texts.items())
    ]
    db.sync_source_comments(conn, source, records)


def write_jsonl(path: Path, *lines: str | ParsedComment) -> Path:
    rendered = [line if isinstance(line, str) else line.model_dump_json() for line in lines]
    path.write_text("\n".join(rendered) + "\n", encoding="utf-8")
    return path


def table_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def table_rows(conn: sqlite3.Connection, table: str) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]


def stored_facts(conn: sqlite3.Connection, comment_id: str) -> list[tuple[Any, ...]]:
    rows = conn.execute(
        "SELECT m.variety_as_written, f.key, f.normalized_value, f.applicability"
        " FROM mentions m JOIN environment_facts f ON f.mention_id = m.mention_id"
        " WHERE m.comment_id = ? ORDER BY m.mention_index, f.fact_index",
        (comment_id,),
    )
    return [tuple(row) for row in rows]


def stored_status(conn: sqlite3.Connection, comment_id: str) -> str | None:
    row = conn.execute("SELECT status FROM parses WHERE comment_id = ?", (comment_id,)).fetchone()
    return None if row is None else str(row[0])


def report_lines_add_up(report: ExtractionReport) -> bool:
    return report.lines_read == (
        report.schema_errors
        + report.unknown_comment_ids
        + report.duplicate_comment_ids
        + report.expired_comment_ids
        + report.stale_texts
        + report.comments_loaded
    )


# =========================================================================== rule 1


@pytest.mark.parametrize(
    "bad_quote",
    [
        "i grow sungold in pots",  # case differs
        "I grow  Sungold in pots",  # whitespace differs
        "I grow Sungold in pots.",  # punctuation not in the text
        "I grow Sungold in containers",  # paraphrase
    ],
)
def test_rule1_mention_quote_must_be_an_exact_substring(bad_quote: str) -> None:
    bad = mention(bad_quote, fact("method", "pots", "in pots", "container"), variety="Sungold")
    result = validate(TEXT_POTS, bad, sungold())

    assert [m.evidence_quote for m in result.mentions] == ["I grow Sungold in pots"]
    assert reasons(result) == [(0, None, "mention_quote_not_substring")]


def test_rule1_rejected_mention_takes_all_its_facts_with_it() -> None:
    bad = mention(
        "I grew Sungold in pots",
        fact("zone", "Zone 6b", "Zone 6b here", "6b"),
        fact("method", "pots", "in pots", "container"),
        variety="Sungold",
    )
    result = validate(TEXT_POTS, bad)

    assert result.mentions == ()
    assert reasons(result) == [(0, None, "mention_quote_not_substring")]


# =========================================================================== rule 2


@pytest.mark.parametrize("variety", ["Sungold", "SUNGOLD", "sungold"])
def test_rule2_variety_occurs_case_insensitively_in_mention_quote(variety: str) -> None:
    result = validate(TEXT_POTS, mention("I grow Sungold in pots", variety=variety))

    assert result.rejections == ()
    assert result.mentions[0].variety_as_written == variety


@pytest.mark.parametrize("variety", ["Sun Gold", "Cherokee Purple", "deck"])
def test_rule2_variety_absent_from_mention_quote_rejects_the_mention(variety: str) -> None:
    result = validate(TEXT_POTS, mention("I grow Sungold in pots", variety=variety))

    assert result.mentions == ()
    assert reasons(result) == [(0, None, "variety_not_in_quote")]


def test_rule2_a_mention_without_a_variety_is_fine_when_its_quote_names_the_crop() -> None:
    result = validate(
        TEXT_GREENHOUSE, mention("my Roma tomatoes were outside", crop="tomato", variety=None)
    )

    assert result.rejections == ()
    assert (result.mentions[0].crop, result.mentions[0].variety_key) == ("tomato", None)


@pytest.mark.parametrize(
    ("text", "quote", "variety"),
    [
        ("The aroma of these tomatoes was amazing.", "The aroma of these tomatoes", "Roma"),
        ("This garlic was a musical delight.", "This garlic was a musical delight", "Music"),
        ("Sungolden fruit everywhere.", "Sungolden fruit", "Sungold"),
        ("My Big-Beef plants did well.", "My Big-Beef plants", "Beef"),
    ],
)
def test_rule2_a_variety_must_occur_as_whole_words(text: str, quote: str, variety: str) -> None:
    """F09: a fragment of another word ("Roma" in "aroma") is not the variety."""
    result = validate(text, mention(quote, crop="tomato", variety=variety))

    assert result.mentions == ()
    assert reasons(result) == [(0, None, "variety_not_in_quote")]


@pytest.mark.parametrize(
    ("text", "variety"),
    [
        ("My Sungolds split after the rain.", "Sungold"),
        ("Two Brandywines and a Juliet.", "Brandywine"),
        ("JALAPENOES everywhere this year.", "jalapeno"),
    ],
)
def test_rule2_a_plural_s_or_es_after_the_variety_is_allowed(text: str, variety: str) -> None:
    result = validate(text, mention(text, variety=variety))

    assert result.rejections == ()


# --- F22: a crop-only mention must name its crop


@pytest.mark.parametrize(
    ("text", "quote", "crop"),
    [
        ("Great video, thanks!", "Great video", "pepper"),
        (  # a tomato mention quoting the pepper sentence (SPEC §11 #2)
            "My peppers were in the greenhouse and loved it. Tomatoes outside got blight.",
            "My peppers were in the greenhouse and loved it",
            "tomato",
        ),
        ("Pepperoni pizza after a day in the garden.", "Pepperoni pizza", "pepper"),
    ],
)
def test_rule2_a_crop_only_mention_whose_quote_does_not_name_the_crop_is_rejected(
    text: str, quote: str, crop: str
) -> None:
    result = validate(
        text, mention(quote, fact("shelter", "garden", quote), crop=crop, variety=None)
    )

    assert result.mentions == ()
    assert reasons(result) == [(0, None, "crop_not_in_quote")]


@pytest.mark.parametrize(
    ("quote", "crop"),
    [
        ("My peppers were in the greenhouse", "pepper"),
        ("Tomatoes outside got blight", "tomatoes"),
        ("the cukes went crazy", "cucumber"),
        ("chillies on the windowsill", "pepper"),
    ],
)
def test_rule2_a_crop_named_as_written_plural_or_synonym_is_accepted(
    quote: str, crop: str
) -> None:
    result = validate(f"{quote}.", mention(quote, crop=crop, variety=None))

    assert result.rejections == ()


def test_rule2_a_crop_with_a_variety_need_not_be_named_in_the_quote() -> None:
    result = validate(TEXT_POTS, sungold())

    assert result.rejections == ()
    assert result.mentions[0].crop == "tomato"


# =========================================================================== rule 3


def test_rule3_crop_and_variety_key_are_normalised() -> None:
    text = 'Our "Sun Gold" cherry tomatoes were the earliest.'
    result = validate(
        text, mention('"Sun Gold" cherry tomatoes', crop="Cherry Tomatoes", variety='"Sun Gold"')
    )

    only = result.mentions[0]
    assert (only.crop, only.variety_as_written, only.variety_key) == (
        "cherry tomato",
        '"Sun Gold"',
        "sun gold",
    )


def test_rule3_topics_keep_first_seen_order_without_repeats() -> None:
    result = validate(
        TEXT_POTS, mention("I grow Sungold in pots", topics=("flavor", "pests", "flavor"))
    )

    assert result.mentions[0].topics == ("flavor", "pests")


@pytest.mark.parametrize(
    ("crop", "variety", "reason"),
    [
        ("   ", None, "invalid_crop"),
        ("--", None, "invalid_crop"),
        ("tomato", "--", "invalid_variety"),
    ],
)
def test_rule3_names_that_cannot_be_normalised_reject_the_mention(
    crop: str, variety: str | None, reason: str
) -> None:
    text = "Grew -- tomatoes this year, they were fine."
    result = validate(text, mention("Grew -- tomatoes", crop=crop, variety=variety))

    assert result.mentions == ()
    assert reasons(result) == [(0, None, reason)]


# =========================================================================== rule 4


def test_rule4_fact_quote_must_be_an_exact_substring() -> None:
    result = validate(
        TEXT_POTS,
        sungold(
            fact("zone", "zone 6b", "zone 6b here", "6b"),  # text says "Zone 6b here"
            fact("method", "pots", "in pots", "container"),
        ),
    )

    assert facts_of(result) == [("method", "container")]
    assert reasons(result) == [(0, 0, "fact_quote_not_substring")]


def test_rule4_value_must_occur_case_insensitively_in_the_fact_quote() -> None:
    result = validate(
        TEXT_POTS,
        sungold(
            fact("zone", "ZONE 6B", "Zone 6b here", "6b"),
            fact("humidity", "muggy", "in pots", "humid"),  # value is elsewhere in the text
        ),
    )

    assert facts_of(result) == [("zone", "6b")]
    assert reasons(result) == [(0, 1, "value_not_in_quote")]


@pytest.mark.parametrize(
    ("text", "key", "written", "quote", "claimed"),
    [
        (
            "The plants remained healthy all season and Sungold was sweet.",
            "region",
            "Maine",
            "remained healthy",
            "US-ME",
        ),
        ("My Kennebec potatoes were huge.", "method", "pot", "My Kennebec potatoes", "container"),
        ("I GREW TOMATOES IN THE YARD.", "region", "MA", "I GREW TOMATOES", "US-MA"),
        ("A non-humid summer suited them.", "humidity", "humid", "A non-humid summer", "humid"),
        ("Semi-arid hills here.", "humidity", "arid", "Semi-arid hills", "dry_air"),
        ("The plants remained healthy all season.", "region", "Maine", "remained healthy", None),
    ],
)
def test_rule4_a_value_must_occur_as_whole_words(
    text: str, key: EnvKey, written: str, quote: str, claimed: str | None
) -> None:
    """F09: a fragment of another word ("Maine" in "remained") creates no fact."""
    result = validate(text, mention(text[:10], fact(key, written, quote, claimed)))

    assert facts_of(result) == []
    assert reasons(result) == [(0, 0, "value_not_in_quote")]


@pytest.mark.parametrize(
    ("text", "key", "written", "quote", "claimed"),
    [
        ("Sungold in pots did great.", "method", "pot", "in pots", "container"),
        ("Raised beds, every one of them.", "method", "raised bed", "Raised beds", "raised_bed"),
        ("Fort Wayne, IN in a hot summer.", "region", "IN", "Fort Wayne, IN in", "US-IN"),
    ],
)
def test_rule4_a_whole_word_value_may_carry_a_plural_and_exact_case_wins(
    text: str, key: EnvKey, written: str, quote: str, claimed: str
) -> None:
    result = validate(text, mention(text[:10], fact(key, written, quote, claimed)))

    assert result.rejections == ()
    assert facts_of(result) == [(key, claimed)]


# =========================================================================== rule 5


def test_rule5_normalized_value_must_be_a_vocabulary_value() -> None:
    result = validate(TEXT_POTS, sungold(fact("method", "pots", "in pots", "pots")))

    assert facts_of(result) == []
    assert reasons(result) == [(0, 0, "invalid_normalized_value")]


@pytest.mark.parametrize(
    ("key", "written", "quote", "claimed"),
    [
        ("method", "pots", "in pots", "raised_bed"),
        ("humidity", "muggy summers", "loves the muggy summers", "dry_air"),
    ],
)
def test_rule5_a_value_contradicting_the_quoted_words_is_rejected(
    key: EnvKey, written: str, quote: str, claimed: str
) -> None:
    """A valid vocabulary value is still refused when the quoted words say otherwise."""
    result = validate(TEXT_POTS, sungold(fact(key, written, quote, claimed)))

    assert facts_of(result) == []
    assert reasons(result) == [(0, 0, "normalized_value_mismatch")]
    assert result.status == "partial"


def test_rule5_the_extractor_may_normalise_what_the_loader_cannot() -> None:
    text = "Sungold sulked on our north-facing balcony railing."
    result = validate(
        text,
        mention(
            "Sungold sulked",
            fact("method", "balcony railing", "balcony railing", "container"),
            variety="Sungold",
        ),
    )

    assert facts_of(result) == [("method", "container")]
    assert result.rejections == ()


def test_rule5_a_missing_normalized_value_is_never_derived() -> None:
    """F03: null always means display-only, even when the loader could read the phrase."""
    result = validate(
        TEXT_POTS,
        sungold(
            fact("zone", "Zone 6b", "Zone 6b here"),
            fact("humidity", "muggy summers", "loves the muggy summers"),
        ),
    )

    assert facts_of(result) == [("zone", None), ("humidity", None)]
    assert result.rejections == ()


@pytest.mark.parametrize(
    ("text", "key", "written", "quote", "claimed", "reason"),
    [
        ("Juliet did fine; it wasn't humid.", "humidity", "humid", "it wasn't humid", "humid",
         "negated_value"),
        ("It was not humid at all this summer.", "humidity", "not humid",
         "It was not humid at all", "humid", "negated_value"),
        ("Juliet did fine though we had no greenhouse.", "shelter", "greenhouse",
         "we had no greenhouse", "greenhouse", "negated_value"),
        ("Sungold in clay pots on the deck.", "soil", "clay", "Sungold in clay pots", "clay",
         "blocked_value"),
        ("Hot peppers did great for me.", "summer_heat", "Hot", "Hot peppers did great", "hot",
         "blocked_value"),
        ("Grew Sungold in West Virginia.", "region", "Virginia", "in West Virginia", "US-VA",
         "normalized_value_mismatch"),
        ("Grew Sungold in Kansas City.", "region", "Kansas", "in Kansas City", "US-KS",
         "blocked_value"),
        ("I'm right on the zone 6b/7a line.", "zone", "zone 6b", "on the zone 6b/7a line", "6b",
         "normalized_value_mismatch"),
        ("Zones 6-7 for me.", "zone", "Zones 6-7", "Zones 6-7 for me", "6",
         "normalized_value_mismatch"),
        ("Grew it in 2022-2023.", "growing_year", "2022-2023", "in 2022-2023", "2022",
         "normalized_value_mismatch"),
        ("Grew it in 2022 / 2023.", "growing_year", "2022", "in 2022 / 2023", "2022",
         "normalized_value_mismatch"),
        ("Grew it in 2022-2023.", "growing_year", "2022", "in 2022-2023", "2022",
         "value_not_in_quote"),  # a hyphen joins words: "2022" is not a whole word here
        ("Here in Perth, WA our summers are brutal.", "region", "Perth, WA", "Here in Perth, WA",
         "US-WA", "blocked_value"),
        ("Here in Perth, WA our summers are brutal.", "region", "Perth, WA", "Here in Perth, WA",
         "AU", "blocked_value"),
        ("Chicago gardener near Lake Michigan.", "region", "Lake Michigan", "near Lake Michigan",
         "US-MI", "blocked_value"),
        ("Sunset zone 9 here. Early Girl did fine.", "zone", "Sunset zone 9",
         "Sunset zone 9 here", "9", "blocked_value"),
        ("Sunset zone 9 here. Early Girl did fine.", "zone", "zone 9", "Sunset zone 9 here", "9",
         "blocked_value"),
    ],
)  # fmt: skip
def test_rule5_a_value_is_judged_at_its_position_in_the_quote(
    text: str, key: EnvKey, written: str, quote: str, claimed: str, reason: str
) -> None:
    """F03: negated, look-alike, longer-place and range phrases never yield the claimed value."""
    result = validate(text, mention(text[:6], fact(key, written, quote, claimed)))

    assert facts_of(result) == []
    assert reasons(result) == [(0, 0, reason)]


@pytest.mark.parametrize(
    ("text", "key", "written", "quote", "claimed"),
    [
        ("Zone 7a, upstate NY. Sungold was the earliest.", "region", "NY", "Zone 7a, upstate NY",
         "US-NY"),
        ("Zone 7a, upstate NY. Sungold was the earliest.", "region", "upstate NY", "upstate NY",
         "US-NY"),
        ("Sungold in Kansas City, Missouri was great.", "region", "Kansas City, Missouri",
         "in Kansas City, Missouri", "US-MO"),
        ("Grew it in Maine, USA.", "region", "Maine, USA", "in Maine, USA", "US-ME"),
        ("Grew it in Maine, USA.", "region", "Maine", "in Maine, USA", "US-ME"),
        ("Hartford, CT 06103 is home.", "region", "CT 06103", "Hartford, CT 06103", "US-CT"),
        ("Zone: 6b and Sungold loved it.", "zone", "6b", "Zone: 6b", "6b"),
        ("Sungold in pots and Juliet in raised beds.", "method", "pots",
         "Sungold in pots and Juliet in raised beds", "container"),
    ],
)  # fmt: skip
def test_rule5_a_value_stated_at_its_position_is_accepted(
    text: str, key: EnvKey, written: str, quote: str, claimed: str
) -> None:
    result = validate(text, mention(text[:6], fact(key, written, quote, claimed)))

    assert result.rejections == ()
    assert facts_of(result) == [(key, claimed)]


def test_rule5_a_strict_value_the_loader_cannot_read_at_its_position_is_rejected() -> None:
    """Region, zone and year must be derivable where they are quoted ("CT" alone is fine)."""
    text = "Grew Sungold in pots. CT gardens are cold."

    accepted = validate(text, mention("Grew Sungold", fact("region", "CT", "CT gardens", "US-CT")))
    rejected = validate(
        text, mention("Grew Sungold", fact("region", "pots", "Sungold in pots", "US-CT"))
    )

    assert facts_of(accepted) == [("region", "US-CT")]
    assert reasons(rejected) == [(0, 0, "strict_key_mismatch")]


def test_rule5_every_occurrence_of_the_value_in_the_quote_is_judged() -> None:
    text = "Not humid in June, humid in July."
    result = validate(text, mention("Not humid", fact("humidity", "humid", text, "humid")))

    assert reasons(result) == [(0, 0, "negated_value")]


def test_rule5_unrecognised_value_is_kept_as_display_only() -> None:
    text = "I grow Sungold in rocky hillside dirt and it copes."
    result = validate(
        text,
        mention(
            "I grow Sungold",
            fact("soil", "rocky hillside dirt", "in rocky hillside dirt"),
            variety="Sungold",
        ),
    )

    assert facts_of(result) == [("soil", None)]
    assert result.mentions[0].facts[0].value_as_written == "rocky hillside dirt"
    assert result.rejections == ()


@pytest.mark.parametrize(
    ("text", "key", "written", "claimed", "reason"),
    [
        ("Zone 6 here and Sungold thrived.", "zone", "Zone 6", "6b", "normalized_value_mismatch"),
        ("My zone 6b garden grew Sungold.", "region", "zone 6b", "US-CT", "strict_key_mismatch"),
        (
            "Sungold in 2023 was great.",
            "growing_year",
            "2023",
            "2024",
            "normalized_value_mismatch",
        ),
        (  # the city name does not make it Kansas
            "Sungold in Kansas City, Missouri was great.",
            "region",
            "Kansas City, Missouri",
            "US-KS",
            "normalized_value_mismatch",
        ),
        ("Sungold did well in Kansas.", "region", "Kansas", "US-MO", "normalized_value_mismatch"),
    ],
)
def test_rule5_strict_keys_must_equal_the_loaders_normalisation(
    text: str, key: EnvKey, written: str, claimed: str, reason: str
) -> None:
    result = validate(text, mention("Sungold", fact(key, written, written, claimed)))

    assert facts_of(result) == []
    assert reasons(result) == [(0, 0, reason)]


def test_rule5_a_bare_postal_code_counts_only_as_written_in_the_comment() -> None:
    text = "I grew it in pots. Fort Wayne, IN summers are hot."
    word_in = mention("I grew it in pots", fact("region", "IN", "grew it in pots", "US-IN"))
    postal = mention("I grew it in pots", fact("region", "IN", "Fort Wayne, IN", "US-IN"))

    assert reasons(validate(text, word_in)) == [(0, 0, "strict_key_mismatch")]
    assert facts_of(validate(text, postal)) == [("region", "US-IN")]


@pytest.mark.parametrize(
    ("text", "key", "written"),
    [
        ("My zone 6b garden grew Sungold.", "region", "zone 6b"),
        ("Connecticut gardens grew Sungold.", "zone", "Connecticut"),
        ("Sungold in 2023 was great.", "region", "2023"),
    ],
)
def test_rule5_a_strict_fact_naming_another_strict_key_is_not_kept_display_only(
    text: str, key: EnvKey, written: str
) -> None:
    result = validate(text, mention("Sungold", fact(key, written, written)))

    assert reasons(result) == [(0, 0, "strict_key_mismatch")]


def test_rule5_an_unrecognised_place_stays_display_only() -> None:
    text = "Sandy soil and full sun on Cape Cod. Dragon Tongue beans loved it."
    result = validate(
        text,
        mention(
            "Dragon Tongue beans loved it",
            fact("region", "Cape Cod", "full sun on Cape Cod"),
            crop="bean",
            variety="Dragon Tongue",
        ),
    )

    assert facts_of(result) == [("region", None)]
    assert result.rejections == ()


# =========================================================================== rule 6


def test_rule6_fact_in_a_hypothetical_sentence_is_rejected() -> None:
    result = validate(
        TEXT_HYPOTHETICAL,
        mention(
            "Those Glacier tomatoes look amazing",
            fact("zone", "zone 5", "zone 5", "5"),
            variety="Glacier",
        ),
    )

    assert facts_of(result) == []
    assert reasons(result) == [(0, 0, "hypothetical_context")]
    assert [m.variety_as_written for m in result.mentions] == ["Glacier"]  # mention survives


@pytest.mark.parametrize(
    "opening",
    [
        "I\u2019d try",
        "I'D TRY",
        "i\u2018d try",
        "Maybe I\u2019ll try",
        "You SHOULD try",
        "If   you try",
    ],
)
def test_rule6_markers_match_any_case_spacing_and_apostrophe(opening: str) -> None:
    text = f"Glacier was fine. {opening} it in zone 5 for sure."
    result = validate(
        text, mention("Glacier was fine", fact("zone", "zone 5", "zone 5", "5"), variety="Glacier")
    )

    assert reasons(result) == [(0, 0, "hypothetical_context")]


@pytest.mark.parametrize("apostrophe", ["\u00b4", "`", "\u2019", "\u02bc"])
def test_rule6_acute_accent_and_backtick_count_as_apostrophes(apostrophe: str) -> None:
    text = f"Glacier was fine. I{apostrophe}d try it in zone 5."
    result = validate(
        text, mention("Glacier was fine", fact("zone", "zone 5", "zone 5", "5"), variety="Glacier")
    )

    assert reasons(result) == [(0, 0, "hypothetical_context")]


@pytest.mark.parametrize(
    "sentence",
    [
        "Id try Sungold in zone 5.",  # SPEC §11 #5 without its apostrophe
        "Ill try Sungold in zone 5.",
        "Id grow Sungold in zone 5.",
        "Sungold would do fine in zone 5.",
        "Wish I had Sungold in zone 5.",
        "If I had more room, Sungold in zone 5.",
        "If only Sungold grew in zone 5.",
        "Gonna put Sungold in zone 5.",
        "Hoping to grow Sungold in zone 5.",
        "I hope to grow Sungold in zone 5.",
        "Thinking of Sungold in zone 5.",
        "Maybe Sungold in zone 5.",
        "We\u2019ll grow Sungold in zone 5.",
        "We'd grow Sungold in zone 5.",
        "You'd love Sungold in zone 5.",
        "Anyone with Sungold in zone 5 says hi.",
    ],
)
def test_rule6_the_repository_markers_cover_plans_wishes_and_missing_apostrophes(
    sentence: str,
) -> None:
    """F07: the configured markers catch the common unpunctuated and plan/wish phrasings."""
    text = f"Sungold was sweet. {sentence}"
    result = validate(
        text,
        mention("Sungold was sweet", fact("zone", "zone 5", "zone 5", "5"), variety="Sungold"),
        cfg=REPO_CFG,
    )

    assert reasons(result) == [(0, 0, "hypothetical_context")]


@pytest.mark.parametrize(
    "text",
    [
        "Sungold was sweet. Does it do well in zone 6b?",
        "Sungold was sweet. Is zone 6b too cold?!",
        'Sungold was sweet. My friend asked "zone 6b?" and I nodded.',
    ],
)
def test_rule6_a_fact_from_a_question_is_rejected(text: str) -> None:
    """F07: a sentence ending in '?' asks; it does not report."""
    result = validate(
        text, mention("Sungold was sweet", fact("zone", "zone 6b", "zone 6b", "6b"), crop=None)
    )

    assert facts_of(result) == []
    assert reasons(result) == [(0, 0, "question_context")]


def test_rule6_a_question_elsewhere_in_the_comment_does_not_matter() -> None:
    text = "Zone 6b here and Sungold was sweet. Anything better for pots?"
    result = validate(text, mention("Sungold", fact("zone", "Zone 6b", "Zone 6b here", "6b")))

    assert result.rejections == ()


def test_rule6_wish_and_question_in_one_comment_reject_both_facts() -> None:
    text = "Wish I had a greenhouse for my Sungold. Does it do well in zone 6b?"
    result = validate(
        text,
        mention(
            "my Sungold",
            fact("shelter", "greenhouse", "a greenhouse", "greenhouse"),
            fact("zone", "zone 6b", "in zone 6b", "6b"),
            variety="Sungold",
        ),
        cfg=REPO_CFG,
    )

    assert reasons(result) == [(0, 0, "hypothetical_context"), (0, 1, "question_context")]


@pytest.mark.parametrize("marker", ["i\u2019d", "i'd"])
def test_rule6_configured_markers_may_use_either_apostrophe(marker: str) -> None:
    cfg = ExtractionConfig(hypothetical_markers=(marker,))
    for opening in ("I'd try", "I\u2019d try"):
        text = f"Glacier was fine. {opening} it in zone 5."
        result = validate(
            text,
            mention("Glacier was fine", fact("zone", "zone 5", "zone 5", "5"), variety="Glacier"),
            cfg=cfg,
        )
        assert reasons(result) == [(0, 0, "hypothetical_context")]


@pytest.mark.parametrize(
    "text",
    [
        "Glacier grew shoulder high in zone 5 for me.",  # "should" inside a word
        "Glacier couldn't care less about my zone 5 frosts.",  # "could" inside a word
        "I'd recommend Glacier to anyone! Zone 5 here and it ripened in July.",  # other sentence
        "I might grow more of it\nZone 5 here and Glacier ripened in July.",  # line break
    ],
)
def test_rule6_markers_are_word_bounded_and_sentence_scoped(text: str) -> None:
    quote = "Zone 5" if "Zone 5" in text else "zone 5"
    result = validate(text, mention("Glacier", fact("zone", quote, quote, "5"), variety="Glacier"))

    assert result.rejections == ()
    assert facts_of(result) == [("zone", "5")]


@pytest.mark.parametrize(
    "text",
    [
        "I'd try Mr. Stripey in zone 5 next spring.",
        "You should grow it, e.g. in zone 5 where it ripens early.",
        "I'd space them 2.5 feet apart in zone 5.",
        "Maybe I'll put Glacier (var. cerasiforme) out in zone 5.",
        "I'd try it in zone 5...it might ripen.",  # no space after the ellipsis: one sentence
    ],
)
def test_rule6_abbreviations_and_decimals_do_not_end_a_hypothetical_sentence(text: str) -> None:
    """SPEC §11 #5: a full stop inside "Mr.", "e.g." or "2.5" cannot hide the marker."""
    result = validate(text, mention(text[:6], fact("zone", "zone 5", "zone 5", "5"), crop=None))

    assert facts_of(result) == []
    assert reasons(result) == [(0, 0, "hypothetical_context")]


@pytest.mark.parametrize(
    "text",
    [
        "Glacier did fine in zone 5. I'd try Brandywine next.",
        "Glacier did fine in zone 5 out West. I'd try Brandywine next.",
        'My neighbor wrote "zone 5." I would try it again.',
        "Glacier did fine in zone 5... I'd try it again.",
    ],
)
def test_rule6_an_ordinary_full_stop_still_ends_the_sentence(text: str) -> None:
    result = validate(text, mention(text[:6], fact("zone", "zone 5", "zone 5", "5"), crop=None))

    assert result.rejections == ()
    assert facts_of(result) == [("zone", "5")]


def test_rule6_a_quote_spanning_into_a_hypothetical_sentence_is_rejected() -> None:
    text = "Zone 5 here. You should try Glacier."
    result = validate(
        text,
        mention("Glacier", fact("zone", "Zone 5", "Zone 5 here. You", "5"), variety="Glacier"),
    )

    assert reasons(result) == [(0, 0, "hypothetical_context")]


def test_rule6_a_repeated_quote_is_rejected_if_any_occurrence_is_hypothetical() -> None:
    text = "Glacier did well in zone 5. I would try Brandywine in zone 5 too."
    result = validate(
        text, mention("Glacier did well", fact("zone", "zone 5", "zone 5", "5"), variety="Glacier")
    )

    assert reasons(result) == [(0, 0, "hypothetical_context")]


@settings(max_examples=80)
@given(
    marker=st.sampled_from(REPO_CFG.hypothetical_markers),
    apostrophe=st.sampled_from(["'", "\u2019", "\u00b4", "`"]),
    upper=st.booleans(),
)
def test_rule6_every_configured_marker_rejects_a_fact_in_its_sentence(
    marker: str, apostrophe: str, upper: bool
) -> None:
    phrase = marker.replace("'", apostrophe)
    phrase = phrase.upper() if upper else phrase
    text = f"Glacier was fine. Honestly {phrase} put it in zone 5 again."
    result = validate(
        text,
        mention("Glacier was fine", fact("zone", "zone 5", "zone 5", "5"), variety="Glacier"),
        cfg=REPO_CFG,
    )

    assert reasons(result) == [(0, 0, "hypothetical_context")]


def test_rule6_blank_marker_in_config_fails_loudly() -> None:
    cfg = ExtractionConfig(hypothetical_markers=("   ",))
    with pytest.raises(ValueError, match="marker"):
        validate(TEXT_POTS, sungold(), cfg=cfg)


# =========================================================================== rule 7


def test_rule7_two_regions_on_one_mention_are_all_rejected() -> None:
    result = validate(
        TEXT_MAINE,
        mention(
            "I grew these Stupice in Maine last summer",
            fact("region", "Florida", "I live in Florida", "US-FL"),
            fact("season_length", "last summer", "in Maine last summer"),
            fact("region", "Maine", "Stupice in Maine", "US-ME"),
            variety="Stupice",
        ),
    )

    assert [f.key for f in result.mentions[0].facts] == ["season_length"]
    assert reasons(result) == [(0, 0, "conflicting_region"), (0, 2, "conflicting_region")]


def test_rule7_two_zones_on_one_mention_are_all_rejected() -> None:
    text = "Zone 6b at home, zone 7a at the community plot. Sungold did well."
    result = validate(
        text,
        mention(
            "Sungold did well",
            fact("zone", "Zone 6b", "Zone 6b at home", "6b"),
            fact("zone", "zone 7a", "zone 7a at the community plot", "7a"),
            variety="Sungold",
        ),
    )

    assert facts_of(result) == []
    assert reasons(result) == [(0, 0, "conflicting_zone"), (0, 1, "conflicting_zone")]


def test_rule7_the_same_region_twice_is_not_a_conflict() -> None:
    text = "Grew Sungold in Maine. Maine summers suit it."
    result = validate(
        text,
        mention(
            "Grew Sungold in Maine",
            fact("region", "Maine", "Grew Sungold in Maine", "US-ME"),
            fact("region", "Maine", "Maine summers", "US-ME"),
            fact("region", "Maine", "Maine summers"),  # display-only: never a conflict
            variety="Sungold",
        ),
    )

    assert facts_of(result) == [("region", "US-ME"), ("region", "US-ME"), ("region", None)]
    assert result.rejections == ()


def test_rule7_only_accepted_facts_can_conflict() -> None:
    text = "Glacier ripened early in zone 4. I'd try it in zone 5 as well."
    result = validate(
        text,
        mention(
            "Glacier ripened early",
            fact("zone", "zone 4", "early in zone 4", "4"),
            fact("zone", "zone 5", "in zone 5", "5"),
            variety="Glacier",
        ),
    )

    assert facts_of(result) == [("zone", "4")]
    assert reasons(result) == [(0, 1, "hypothetical_context")]


def test_rule7_conflicts_are_checked_per_mention() -> None:
    text = "Sungold grew in Maine. Juliet grew in Vermont."
    result = validate(
        text,
        mention("Sungold grew in Maine", fact("region", "Maine", "in Maine", "US-ME")),
        mention("Juliet grew in Vermont", fact("region", "Vermont", "in Vermont", "US-VT")),
    )

    assert (facts_of(result, 0), facts_of(result, 1)) == (
        [("region", "US-ME")],
        [("region", "US-VT")],
    )
    assert result.rejections == ()


def three_mentions(sungold_scope: Any, juliet_scope: Any = "whole_comment") -> ParsedComment:
    """F25: two mentions each carrying their own zone/region pair, plus a fact-less third."""
    return parsed(
        "c-cross",
        mention(
            "for Sungold",
            fact("zone", "Zone 6b", "Zone 6b in Connecticut", "6b", applicability=sungold_scope),
            fact("region", "Connecticut", "in Connecticut", "US-CT", applicability=sungold_scope),
            variety="Sungold",
        ),
        mention(
            "for Juliet",
            fact("zone", "Zone 7a", "Zone 7a in Virginia", "7a", applicability=juliet_scope),
            fact("region", "Virginia", "in Virginia", "US-VA", applicability=juliet_scope),
            variety="Juliet",
        ),
        mention("Roma did great too", variety="Roma"),
        text=TEXT_CROSS,
    )


TEXT_CROSS = (
    "Zone 6b in Connecticut for Sungold. Zone 7a in Virginia for Juliet. Roma did great too."
)


def test_rule7_conflicting_whole_comment_facts_on_different_mentions_are_all_rejected() -> None:
    result = validate_parsed_comment(three_mentions("whole_comment"), TEXT_CROSS, CFG)

    assert [facts_of(result, i) for i in range(3)] == [[], [], []]
    assert reasons(result) == [
        (0, 0, "conflicting_zone"),
        (0, 1, "conflicting_region"),
        (1, 0, "conflicting_zone"),
        (1, 1, "conflicting_region"),
    ]
    assert result.status == "partial"


def test_rule7_whole_comment_facts_only_conflict_with_whole_comment_facts() -> None:
    result = validate_parsed_comment(three_mentions("this_mention"), TEXT_CROSS, CFG)

    assert (facts_of(result, 0), facts_of(result, 1)) == (
        [("zone", "6b"), ("region", "US-CT")],
        [("zone", "7a"), ("region", "US-VA")],
    )
    assert result.rejections == ()


def test_rule7_the_same_whole_comment_value_on_several_mentions_is_not_a_conflict() -> None:
    text = "Zone 6b here. Sungold and Juliet loved zone 6b."
    result = validate(
        text,
        mention("Sungold", fact("zone", "Zone 6b", "Zone 6b here", "6b", applicability=WHOLE)),
        mention("Juliet", fact("zone", "zone 6b", "loved zone 6b", "6b", applicability=WHOLE)),
    )

    assert result.rejections == ()


# =========================================================================== rule 4: variety names


@pytest.mark.parametrize(
    ("text", "variety", "key", "written", "claimed"),
    [
        ("Arkansas Traveler never cracked for me, even in a wet year.", "Arkansas Traveler",
         "region", "Arkansas", "US-AR"),
        ("California Wonder peppers were huge.", "California Wonder", "region", "California",
         "US-CA"),
        ("Carolina Reaper is brutal.", "Carolina Reaper", "region", "Carolina", None),
        ("Hot Portugal peppers ripened early.", "Hot Portugal", "summer_heat", "Hot", "hot"),
        ("Cool Breeze cucumbers never got bitter.", "Cool Breeze", "summer_heat", "Cool", "cool"),
        ("Shade Star lettuce bolted late.", "Shade Star", "sun", "Shade", "shade"),
        ("Desert King figs everywhere.", "Desert King", "humidity", "Desert", "dry_air"),
        ("ARKANSAS TRAVELER never cracked.", "Arkansas Traveler", "region", "ARKANSAS", "US-AR"),
    ],
)  # fmt: skip
def test_rule4_variety_a_value_inside_a_variety_name_is_rejected(
    text: str, variety: str, key: EnvKey, written: str, claimed: str | None
) -> None:
    """F08: "Arkansas" in "Arkansas Traveler" is a cultivar, not a place."""
    quote = text[: len(variety) + 8]
    result = validate(text, mention(quote, fact(key, written, quote, claimed), variety=variety))

    assert facts_of(result) == []
    assert reasons(result) == [(0, 0, "value_in_variety_name")]


def test_rule4_variety_a_variety_named_by_another_mention_also_counts() -> None:
    text = "California Wonder did fine. The peppers were all California Wonder plants."
    result = validate(
        text,
        mention("California Wonder did fine", variety="California Wonder"),
        mention(
            "The peppers",
            fact("region", "California", "all California Wonder plants", "US-CA"),
            crop="pepper",
        ),
    )

    assert reasons(result) == [(1, 0, "value_in_variety_name")]


@pytest.mark.parametrize(
    ("text", "quote", "written", "claimed"),
    [
        ("Kentucky Wonder beans did great here in Colorado.", "here in Colorado", "Colorado",
         "US-CO"),
        ("Kentucky Wonder pole beans, grown here in Kentucky.", "here in Kentucky", "Kentucky",
         "US-KY"),
    ],
)  # fmt: skip
def test_rule4_variety_a_place_outside_the_variety_name_is_kept(
    text: str, quote: str, written: str, claimed: str
) -> None:
    result = validate(
        text,
        mention(
            "Kentucky Wonder",
            fact("region", written, quote, claimed),
            crop="bean",
            variety="Kentucky Wonder",
        ),
    )

    assert result.rejections == ()
    assert facts_of(result) == [("region", claimed)]


# =========================================================================== rule 8


@pytest.mark.parametrize(
    ("status", "with_rejection", "expected"),
    [
        ("parsed", False, "parsed"),
        ("parsed", True, "partial"),
        ("partial", False, "partial"),
        ("partial", True, "partial"),
        ("unresolved", True, "unresolved"),
    ],
)
def test_rule8_parsed_is_downgraded_to_partial_when_anything_is_rejected(
    status: ParseStatus, with_rejection: bool, expected: ParseStatus
) -> None:
    quote = "in pot" if with_rejection else "in pots"  # "in pot" + "pots" fails rule 4
    result = validate(TEXT_POTS, sungold(fact("method", "pots", quote)), status=status)

    assert result.status == expected
    assert bool(result.rejections) is with_rejection


def test_rule8_a_comment_with_zero_mentions_is_valid() -> None:
    result = validate_parsed_comment(
        parsed("c9", status="unresolved", warnings=["no_growing_report: question"]),
        "What size pots do you use?",
        CFG,
    )

    assert result == ValidatedComment(
        comment_id="c9",
        extractor_version="test-extractor-v1",
        status="unresolved",
        mentions=(),
        warnings=("no_growing_report: question",),
        rejections=(),
    )


def test_validated_facts_keep_provenance_fields() -> None:
    result = validate(
        TEXT_POTS,
        sungold(
            fact(
                "humidity",
                "muggy summers",
                "the muggy summers",
                applicability="whole_comment",
                basis="observed_experience",
            )
        ),
    )

    kept = result.mentions[0].facts[0]
    assert (kept.value_as_written, kept.evidence_quote) == ("muggy summers", "the muggy summers")
    assert (kept.applicability, kept.basis) == ("whole_comment", "observed_experience")


# =========================================================================== SPEC §11 #2


def greenhouse_and_outdoor() -> ParsedComment:
    return parsed(
        "c-assoc",
        mention(
            "I grew my Jalapeño peppers in the greenhouse",
            fact("shelter", "greenhouse", "peppers in the greenhouse", "greenhouse"),
            crop="peppers",
            variety="Jalapeño",
        ),
        mention(
            "my Roma tomatoes were outside in the main bed",
            fact("shelter", "outside", "Roma tomatoes were outside", "outdoors"),
            crop="tomatoes",
            variety="Roma",
        ),
        text=TEXT_GREENHOUSE,
    )


def test_spec2_environment_association_survives_validation() -> None:
    result = validate_parsed_comment(greenhouse_and_outdoor(), TEXT_GREENHOUSE, CFG)

    assert [(m.crop, facts_of(result, i)) for i, m in enumerate(result.mentions)] == [
        ("pepper", [("shelter", "greenhouse")]),
        ("tomato", [("shelter", "outdoors")]),
    ]


def test_spec2_environment_association_survives_loading(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    seed(conn, {"c-assoc": TEXT_GREENHOUSE})
    path = write_jsonl(tmp_path / "x.jsonl", greenhouse_and_outdoor())

    load_extractions(conn, path, CFG, LOADED)

    assert stored_facts(conn, "c-assoc") == [
        ("Jalapeño", "shelter", "greenhouse", "this_mention"),
        ("Roma", "shelter", "outdoors", "this_mention"),
    ]
    (view,) = db.load_comment_views(conn, "public", NOW, ["c-assoc"])
    assert [[f.normalized_value for f in m.facts] for m in view.mentions] == [
        ["greenhouse"],
        ["outdoors"],
    ]


# =========================================================================== SPEC §11 #3


@given(
    zone=st.sampled_from(ZONE_VALUES),
    claimed=st.one_of(st.none(), st.sampled_from(sorted(REGION_NAMES))),
)
def test_spec3_a_zone_phrase_never_becomes_a_region(zone: str, claimed: str | None) -> None:
    text = f"My zone {zone} garden grew Sungold."
    phrase = f"zone {zone}"
    result = validate(
        text,
        mention("Sungold", fact("region", phrase, phrase, claimed)),
        mention("Sungold", fact("zone", phrase, phrase, zone)),
    )

    assert reasons(result) == [(0, 0, "strict_key_mismatch")]
    assert facts_of(result, 1) == [("zone", zone)]


def test_spec3_zone_facts_never_create_region_rows(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    text = "Celebrity has been my most reliable slicer in my zone 7a garden."
    seed(conn, {"c-zone": text})
    line = parsed(
        "c-zone",
        mention(
            "Celebrity has been my most reliable slicer",
            fact("zone", "zone 7a", "in my zone 7a garden", "7a", applicability="whole_comment"),
            variety="Celebrity",
        ),
        text=text,
    )

    load_extractions(conn, write_jsonl(tmp_path / "x.jsonl", line), CFG, LOADED)

    assert stored_facts(conn, "c-zone") == [("Celebrity", "zone", "7a", "whole_comment")]
    assert table_count(conn, "environment_facts WHERE key = 'region'") == 0


# =========================================================================== SPEC §11 #4


def test_spec4_the_stated_growing_location_is_accepted() -> None:
    result = validate(
        TEXT_MAINE,
        mention(
            "I grew these Stupice in Maine last summer",
            fact("region", "Maine", "I grew these Stupice in Maine", "US-ME"),
            variety="Stupice",
        ),
    )

    assert facts_of(result) == [("region", "US-ME")]
    assert result.rejections == ()


def test_spec4_residence_and_growing_location_on_one_mention_are_both_rejected() -> None:
    result = validate(
        TEXT_MAINE,
        mention(
            "I grew these Stupice in Maine last summer",
            fact("region", "Florida", "I live in Florida", "US-FL"),
            fact("region", "Maine", "I grew these Stupice in Maine", "US-ME"),
            variety="Stupice",
        ),
    )

    assert facts_of(result) == []
    assert reasons(result) == [(0, 0, "conflicting_region"), (0, 1, "conflicting_region")]
    assert result.status == "partial"


def test_spec4_a_phrase_naming_both_places_cannot_claim_one() -> None:
    written = "Florida, but I grew these Stupice in Maine"
    result = validate(
        TEXT_MAINE,
        mention(
            "I grew these Stupice in Maine last summer",
            fact("region", written, written, "US-ME"),
            variety="Stupice",
        ),
    )

    assert reasons(result) == [(0, 0, "normalized_value_mismatch")]


# =========================================================================== SPEC §11 #5


@pytest.mark.parametrize("quote", ["zone 5", "in zone 5", "I'd try these in zone 5"])
def test_spec5_hypothetical_zone_is_never_a_growing_report(quote: str) -> None:
    result = validate(
        TEXT_HYPOTHETICAL,
        mention(
            "Those Glacier tomatoes look amazing",
            fact("zone", "zone 5", quote, "5"),
            variety="Glacier",
        ),
    )

    assert facts_of(result) == []
    assert reasons(result) == [(0, 0, "hypothetical_context")]


# =========================================================================== SPEC §11 #18


@given(
    text=st.text(min_size=1, max_size=MAX_QUOTE_CHARS),
    quote=st.text(min_size=1, max_size=MAX_VALUE_CHARS),
)
def test_spec18_quotes_not_in_the_text_are_always_rejected(text: str, quote: str) -> None:
    assume(quote not in text)

    bad_mention = validate(text, mention(quote, crop=None))
    bad_fact = validate(text, mention(text, fact("soil", quote, quote), crop=None))

    assert reasons(bad_mention) == [(0, None, "mention_quote_not_substring")]
    assert reasons(bad_fact) == [(0, 0, "fact_quote_not_substring")]


@st.composite
def maybe_substring(draw: st.DrawFn, text: str) -> str:
    if text and draw(st.booleans()):
        start = draw(st.integers(0, len(text) - 1))
        end = draw(st.integers(start + 1, len(text)))
        return text[start:end]
    return draw(st.text(min_size=1, max_size=12))


@st.composite
def random_extraction(draw: st.DrawFn) -> tuple[str, ParsedComment]:
    text = draw(st.text(alphabet="ab .!?\n'\u2019zone6b", min_size=1, max_size=40))
    mentions = []
    for _ in range(draw(st.integers(0, 3))):
        facts = []
        for _ in range(draw(st.integers(0, 3))):
            quote = draw(maybe_substring(text))
            facts.append(
                fact(draw(st.sampled_from(ENV_KEYS)), draw(maybe_substring(quote)), quote)
            )
        mentions.append(mention(draw(maybe_substring(text)), *facts, crop=None))
    status = draw(st.sampled_from(["parsed", "partial", "unresolved"]))
    return text, parsed("c-rand", *mentions, status=status)


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(case=random_extraction())
def test_spec18_every_accepted_quote_is_an_exact_substring(
    case: tuple[str, ParsedComment],
) -> None:
    text, line = case
    result = validate_parsed_comment(line, text, CFG)

    for kept in result.mentions:
        assert kept.evidence_quote in text
        assert all(f.evidence_quote in text for f in kept.facts)
    mention_level = [r for r in result.rejections if r.fact_index is None]
    assert len(result.mentions) + len(mention_level) == len(line.mentions)
    assert all(r.reason in KNOWN_REASONS for r in result.rejections)
    assert (result.status == "partial") is (
        line.status == "partial" or (line.status == "parsed" and bool(result.rejections))
    )


# =========================================================================== read_extraction_jsonl


def test_read_returns_valid_lines_with_physical_line_numbers(tmp_path: Path) -> None:
    first = parsed("c1", sungold())
    second = parsed("c2", status="unresolved")
    path = write_jsonl(tmp_path / "x.jsonl", first, "", "   ", second)

    lines, rejections = read_extraction_jsonl(path)

    assert lines == [(1, first), (4, second)]
    assert rejections == []


@pytest.mark.parametrize(
    "bad_line",
    [
        "{not json",
        "[1, 2]",
        '{"comment_id": "c2", "extractor_version": "v1", "status": "done", "mentions": []}',
        '{"comment_id": "c2", "extractor_version": "v1", "status": "parsed", "mentions": [],'
        ' "confidence": 0.9}',
        '{"comment_id": "c2", "extractor_version": "v1", "status": "parsed", "mentions":'
        ' [{"crop": "tomato", "evidence_quote": ""}]}',
        '{"comment_id": "c2", "extractor_version": "v1", "status": "parsed", "mentions":'
        ' [{"evidence_quote": "x", "environment": [{"key": "region", "value_as_written": "x",'
        ' "evidence_quote": "x", "applicability": "this_mention", "basis": "observed_experience",'
        ' "extra": 1}]}]}',
        '{"comment_id": "c2", "comment_id": "c3", "extractor_version": "v1", "status": "parsed"}',
        '{"comment_id": 7, "extractor_version": "v1", "status": "parsed"}',
    ],
    ids=[
        "bad-json",
        "not-an-object",
        "bad-status",
        "extra-key",
        "empty-quote",
        "extra-fact-key",
        "duplicate-json-key",
        "id-not-a-string",
    ],
)
def test_read_turns_bad_lines_into_schema_errors(tmp_path: Path, bad_line: str) -> None:
    good = parsed("c1", sungold())
    path = write_jsonl(tmp_path / "x.jsonl", good, bad_line, good)

    lines, rejections = read_extraction_jsonl(path)

    assert lines == [(1, good), (3, good)]
    assert [(r.line_number, r.reason, r.mention_index, r.fact_index) for r in rejections] == [
        (2, "schema_error", None, None)
    ]


def test_read_keeps_the_comment_id_of_a_schema_error_when_it_is_readable(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path / "x.jsonl",
        '{"comment_id": "c2", "extractor_version": "v1", "status": "maybe"}',
        "{broken",
    )

    _, rejections = read_extraction_jsonl(path)

    assert [(r.comment_id, r.line_number) for r in rejections] == [("c2", 1), (None, 2)]


def test_read_rejects_a_line_that_is_not_utf8_but_keeps_the_others(tmp_path: Path) -> None:
    good = parsed("c1", sungold()).model_dump_json().encode()
    path = tmp_path / "x.jsonl"
    path.write_bytes(
        b"\xef\xbb\xbf" + good + b"\n" + b'{"comment_id": "\xff"}\r\n' + good + b"\r\n"
    )

    lines, rejections = read_extraction_jsonl(path)

    assert [number for number, _ in lines] == [1, 3]
    assert [(r.line_number, r.reason) for r in rejections] == [(2, "schema_error")]


def test_read_does_not_split_lines_on_unicode_line_separators(tmp_path: Path) -> None:
    text_quote = "Sungold\u2028in pots"
    line = parsed("c1", mention(text_quote, variety="Sungold"))
    path = tmp_path / "x.jsonl"
    raw = json.dumps(line.model_dump(mode="json"), ensure_ascii=False)
    path.write_text(raw + "\n", encoding="utf-8")
    assert "\u2028" in path.read_text(encoding="utf-8")

    lines, rejections = read_extraction_jsonl(path)

    assert lines == [(1, line)]
    assert rejections == []


def test_read_missing_file_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"missing\.jsonl"):
        read_extraction_jsonl(tmp_path / "missing.jsonl")


# =========================================================================== load_extractions

TEXT_A = "Zone 6b here. I grow Sungold in pots and it loves the muggy summers."
TEXT_B = "Brandywine had the best flavor this year."
TEXT_C = "What size pots do you use?"


def line_a(status: ParseStatus = "parsed") -> ParsedComment:
    return parsed(
        "a",
        mention(
            "I grow Sungold in pots",
            fact("zone", "Zone 6b", "Zone 6b here", "6b", applicability="whole_comment"),
            fact("method", "pots", "in pots", "container"),
            fact("humidity", "muggy", "the muggy summers", "humid"),
            variety="Sungold",
            topics=("growing_method",),
        ),
        text=TEXT_A,
        status=status,
    )


def line_b() -> ParsedComment:
    return parsed(
        "b",
        mention("Brandywine had the best flavor", variety="Brandywine", topics=("flavor",)),
        text=TEXT_B,
    )


def line_c() -> ParsedComment:
    return parsed(
        "c", text=TEXT_C, status="unresolved", warnings=["no_growing_report: a question"]
    )


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    seed(conn, {"a": TEXT_A, "b": TEXT_B, "c": TEXT_C})
    return conn


def test_load_stores_validated_rows_and_reports_counts(
    seeded: sqlite3.Connection, tmp_path: Path
) -> None:
    path = write_jsonl(tmp_path / "x.jsonl", line_a(), line_b(), line_c())

    report = load_extractions(seeded, path, CFG, LOADED)

    assert report == ExtractionReport(
        lines_read=3,
        schema_errors=0,
        unknown_comment_ids=0,
        duplicate_comment_ids=0,
        expired_comment_ids=0,
        stale_texts=0,
        comments_loaded=3,
        status_counts={"parsed": 2, "partial": 0, "unresolved": 1},
        mentions_accepted=2,
        mentions_rejected=0,
        facts_accepted=3,
        facts_rejected=0,
        unresolved_identities=0,
        rejections=(),
    )
    assert (table_count(seeded, "parses"), table_count(seeded, "mentions")) == (3, 2)
    assert stored_facts(seeded, "a") == [
        ("Sungold", "zone", "6b", "whole_comment"),
        ("Sungold", "method", "container", "this_mention"),
        ("Sungold", "humidity", "humid", "this_mention"),
    ]


def test_load_counts_mixed_outcomes_and_numbers_every_rejection(
    seeded: sqlite3.Connection, tmp_path: Path
) -> None:
    bad_fact_a = parsed(
        "a",
        mention(
            "I grow Sungold in pots",
            fact("method", "pots", "in pots", "container"),
            fact("zone", "zone 7", "Zone 6b here", "7"),  # value not in quote
            variety="Sungold",
        ),
        mention("I grew Sungold in pots", fact("method", "pots", "in pots")),  # bad quote
        text=TEXT_A,
    )
    path = write_jsonl(tmp_path / "x.jsonl", "{oops", bad_fact_a, line_b(), parsed("zzz"))

    report = load_extractions(seeded, path, CFG, LOADED)

    assert (report.lines_read, report.schema_errors, report.unknown_comment_ids) == (4, 1, 1)
    assert (report.comments_loaded, report.duplicate_comment_ids) == (2, 0)
    assert (report.mentions_accepted, report.mentions_rejected) == (2, 1)
    assert (report.facts_accepted, report.facts_rejected) == (1, 2)
    assert report.status_counts == {"parsed": 1, "partial": 1, "unresolved": 0}
    assert [
        (r.line_number, r.comment_id, r.mention_index, r.fact_index, r.reason)
        for r in report.rejections
    ] == [
        (1, None, None, None, "schema_error"),
        (2, "a", 0, 1, "value_not_in_quote"),
        (2, "a", 1, None, "mention_quote_not_substring"),
        (4, "zzz", None, None, "unknown_comment_id"),
    ]
    assert report_lines_add_up(report)


def test_load_rejects_unknown_comment_ids_without_storing_anything(
    seeded: sqlite3.Connection, tmp_path: Path
) -> None:
    ghost = parsed("ghost", mention("Brandywine", variety="Brandywine"))
    report = load_extractions(seeded, write_jsonl(tmp_path / "x.jsonl", ghost), CFG, LOADED)

    assert [(r.comment_id, r.line_number, r.reason) for r in report.rejections] == [
        ("ghost", 1, "unknown_comment_id")
    ]
    assert (report.unknown_comment_ids, report.comments_loaded) == (1, 0)
    assert table_count(seeded, "parses") == 0


def test_load_rejects_every_copy_of_a_duplicated_comment_id(
    seeded: sqlite3.Connection, tmp_path: Path
) -> None:
    path = write_jsonl(tmp_path / "x.jsonl", line_a(), line_b(), line_a("partial"))

    report = load_extractions(seeded, path, CFG, LOADED)

    assert [(r.comment_id, r.line_number, r.reason) for r in report.rejections] == [
        ("a", 1, "duplicate_comment_id"),
        ("a", 3, "duplicate_comment_id"),
    ]
    assert (report.duplicate_comment_ids, report.comments_loaded) == (2, 1)
    assert (stored_status(seeded, "a"), stored_status(seeded, "b")) == (None, "parsed")
    assert report_lines_add_up(report)


def test_a_schema_error_copy_still_makes_the_id_a_duplicate(
    seeded: sqlite3.Connection, tmp_path: Path
) -> None:
    broken_copy = '{"comment_id": "a", "extractor_version": "v1", "status": "??"}'
    path = write_jsonl(tmp_path / "x.jsonl", broken_copy, line_a())

    report = load_extractions(seeded, path, CFG, LOADED)

    assert [(r.line_number, r.reason) for r in report.rejections] == [
        (1, "schema_error"),
        (2, "duplicate_comment_id"),
    ]
    assert stored_status(seeded, "a") is None


def test_reloading_the_same_file_is_idempotent(seeded: sqlite3.Connection, tmp_path: Path) -> None:
    path = write_jsonl(tmp_path / "x.jsonl", line_a(), line_b(), line_c())
    first = load_extractions(seeded, path, CFG, LOADED)
    before = {t: table_rows(seeded, t) for t in ("mentions", "environment_facts")}
    counts = [table_count(seeded, t) for t in ("parses", "mentions", "environment_facts")]

    second = load_extractions(seeded, path, CFG, LATER)

    assert second == first
    assert [table_count(seeded, t) for t in ("parses", "mentions", "environment_facts")] == counts
    assert {t: table_rows(seeded, t) for t in before} == before


def test_reextraction_replaces_the_previous_rows(
    seeded: sqlite3.Connection, tmp_path: Path
) -> None:
    load_extractions(seeded, write_jsonl(tmp_path / "1.jsonl", line_a()), CFG, LOADED)
    slim = parsed(
        "a", mention("I grow Sungold in pots", variety="Sungold"), text=TEXT_A, status="partial"
    )

    load_extractions(seeded, write_jsonl(tmp_path / "2.jsonl", slim), CFG, LATER)

    assert stored_facts(seeded, "a") == []
    assert (stored_status(seeded, "a"), table_count(seeded, "mentions")) == ("partial", 1)


def test_load_stores_the_downgraded_status(seeded: sqlite3.Connection, tmp_path: Path) -> None:
    hypothetical = parsed(
        "a",
        mention(
            "I grow Sungold in pots",
            fact("method", "pots", "in pots", "container"),
            fact("zone", "Zone 6b", "Zone 6b here", "6b"),
            variety="Sungold",
        ),
        text=TEXT_A,
    )
    seed(seeded, {"h": TEXT_HYPOTHETICAL}, source_id="src-h")
    glacier = parsed(
        "h",
        mention(
            "Those Glacier tomatoes look amazing",
            fact("zone", "zone 5", "zone 5", "5"),
            variety="Glacier",
        ),
        text=TEXT_HYPOTHETICAL,
    )
    path = write_jsonl(tmp_path / "x.jsonl", hypothetical, glacier)

    report = load_extractions(seeded, path, CFG, LOADED)

    assert (stored_status(seeded, "a"), stored_status(seeded, "h")) == ("parsed", "partial")
    statuses = seeded.execute("SELECT status FROM mentions WHERE comment_id = 'h'").fetchall()
    assert [row[0] for row in statuses] == ["partial"]
    assert report.status_counts == {"parsed": 1, "partial": 1, "unresolved": 0}
    assert stored_facts(seeded, "h") == []


def test_unresolved_identities_counts_accepted_mentions_without_crop_or_variety(
    seeded: sqlite3.Connection, tmp_path: Path
) -> None:
    anonymous = parsed(
        "a",
        mention("I grow Sungold in pots", crop=None, variety=None),
        mention("it loves the muggy summers", crop=None, variety=None),
        mention("I grew it in pots", crop=None, variety=None),  # rejected: not a substring
        mention("Zone 6b here", crop="tomato", variety=None),  # rejected: crop not named
        mention("I grow Sungold", crop=None, variety="Sungold"),
        text=TEXT_A,
    )
    path = write_jsonl(tmp_path / "x.jsonl", anonymous, line_b())

    report = load_extractions(seeded, path, CFG, LOADED)

    assert report.unresolved_identities == 2
    assert (report.mentions_accepted, report.mentions_rejected) == (4, 2)


def test_load_requires_a_canonical_loaded_at(seeded: sqlite3.Connection, tmp_path: Path) -> None:
    path = write_jsonl(tmp_path / "x.jsonl", line_a())

    with pytest.raises(ValueError, match="loaded_at"):
        load_extractions(seeded, path, CFG, "2026-10-15")
    assert table_count(seeded, "parses") == 0


def test_load_is_all_or_nothing(
    seeded: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = db.replace_extraction
    calls: list[str] = []

    def fail_on_second(conn: sqlite3.Connection, validated: ValidatedComment, at: str) -> None:
        calls.append(validated.comment_id)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        real(conn, validated, at)

    monkeypatch.setattr(db, "replace_extraction", fail_on_second)
    path = write_jsonl(tmp_path / "x.jsonl", line_a(), line_b(), line_c())

    with pytest.raises(RuntimeError, match="disk full"):
        load_extractions(seeded, path, CFG, LOADED)
    assert table_count(seeded, "parses") == 0
    assert not seeded.in_transaction


def test_an_empty_file_loads_nothing(seeded: sqlite3.Connection, tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("\n\n", encoding="utf-8")

    report = load_extractions(seeded, path, CFG, LOADED)

    assert (report.lines_read, report.comments_loaded, report.rejections) == (0, 0, ())
    assert report.status_counts == {"parsed": 0, "partial": 0, "unresolved": 0}


# --- F12: an extraction made for older text is stale


def test_an_extraction_of_an_older_text_is_rejected_as_stale(
    seeded: sqlite3.Connection, tmp_path: Path
) -> None:
    old_line = line_a()
    seed(seeded, {"a": TEXT_A + " EDIT: wrong video, I never grew it.", "b": TEXT_B, "c": TEXT_C})
    path = write_jsonl(tmp_path / "x.jsonl", old_line, line_b())

    report = load_extractions(seeded, path, CFG, LOADED)

    assert [(r.comment_id, r.line_number, r.reason) for r in report.rejections] == [
        ("a", 1, "stale_text")
    ]
    assert (report.stale_texts, report.comments_loaded) == (1, 1)
    assert report_lines_add_up(report)
    assert stored_status(seeded, "a") is None
    unparsed = db.iter_comments_for_extraction(seeded, only_unparsed=True, now=NOW)
    assert [item.comment_id for item in unparsed] == ["a", "c"]


def test_a_stale_line_leaves_the_previous_extraction_untouched(
    seeded: sqlite3.Connection, tmp_path: Path
) -> None:
    load_extractions(seeded, write_jsonl(tmp_path / "1.jsonl", line_a()), CFG, LOADED)
    stale = line_a().model_copy(update={"text_hash": sha256("an older text"), "mentions": []})

    report = load_extractions(seeded, write_jsonl(tmp_path / "2.jsonl", stale), CFG, LATER)

    assert [r.reason for r in report.rejections] == ["stale_text"]
    assert len(stored_facts(seeded, "a")) == 3


@pytest.mark.parametrize("text_hash", [None, "", "abc", "A" * 64, "g" * 64])
def test_a_missing_or_malformed_text_hash_is_a_schema_error(
    tmp_path: Path, text_hash: str | None
) -> None:
    raw = line_b().model_dump(mode="json")
    if text_hash is None:
        del raw["text_hash"]
    else:
        raw["text_hash"] = text_hash

    lines, rejections = read_extraction_jsonl(write_jsonl(tmp_path / "x.jsonl", json.dumps(raw)))

    assert lines == []
    assert [(r.comment_id, r.reason) for r in rejections] == [("b", "schema_error")]


# --- F26: comments whose source permission has ended are never extracted


EXPIRES = "2026-10-15T12:30:00Z"  # between LOADED (12:00) and NOW (13:00)


def test_a_comment_of_an_expired_source_is_rejected(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    seed(conn, {"a": TEXT_A}, expires_at=EXPIRES)
    seed(conn, {"b": TEXT_B}, source_id="src-b")
    path = write_jsonl(tmp_path / "x.jsonl", line_a(), line_b())

    report = load_extractions(conn, path, CFG, NOW)

    assert [(r.comment_id, r.line_number, r.reason) for r in report.rejections] == [
        ("a", 1, "source_expired")
    ]
    assert (report.expired_comment_ids, report.comments_loaded) == (1, 1)
    assert report_lines_add_up(report)
    assert (stored_status(conn, "a"), table_count(conn, "environment_facts")) == (None, 0)


def test_source_expiry_is_judged_at_now_which_defaults_to_the_load_time(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    seed(conn, {"a": TEXT_A}, expires_at=EXPIRES)
    path = write_jsonl(tmp_path / "x.jsonl", line_a())

    late = load_extractions(conn, path, CFG, LOADED, now=NOW)
    in_time = load_extractions(conn, path, CFG, LOADED)

    assert [r.reason for r in late.rejections] == ["source_expired"]
    assert (in_time.rejections, in_time.comments_loaded) == ((), 1)


def test_load_requires_a_canonical_now(seeded: sqlite3.Connection, tmp_path: Path) -> None:
    path = write_jsonl(tmp_path / "x.jsonl", line_a())

    with pytest.raises(ValueError, match="now"):
        load_extractions(seeded, path, CFG, LOADED, now="2026-10-15")
    assert table_count(seeded, "parses") == 0


# --- F38: bounded work on long, adversarial text


def test_a_long_comment_validates_in_well_under_a_second() -> None:
    text = "Sungold in pots. " + "a. " * 16_667  # about 50,000 characters, 16,667 full stops
    line = parsed(
        "c-long",
        mention(
            "Sungold in pots",
            fact("method", "pots", "in pots", "container"),
            fact("soil", "a", "a. a", None),
            variety="Sungold",
        ),
        text=text,
    )

    started = time.perf_counter()
    result = validate_parsed_comment(line, text, REPO_CFG)
    elapsed = time.perf_counter() - started

    assert facts_of(result) == [("method", "container"), ("soil", None)]
    assert elapsed < 1.0


@pytest.mark.parametrize(
    ("field", "limit"),
    [("value_as_written", 200), ("evidence_quote", 2000)],
)
def test_fact_strings_are_capped(field: str, limit: int) -> None:
    ok = {"key": "soil", "value_as_written": "x", "evidence_quote": "x"}
    common = {"applicability": "this_mention", "basis": "observed_experience"}

    EnvironmentFact.model_validate({**ok, **common, field: "x" * limit})
    with pytest.raises(ValidationError):
        EnvironmentFact.model_validate({**ok, **common, field: "x" * (limit + 1)})


def test_mention_quotes_are_capped() -> None:
    VarietyMention.model_validate({"evidence_quote": "x" * 2000})
    with pytest.raises(ValidationError):
        VarietyMention.model_validate({"evidence_quote": "x" * 2001})


def test_an_overlong_value_makes_the_line_a_schema_error(tmp_path: Path) -> None:
    raw = line_a().model_dump(mode="json")
    raw["mentions"][0]["environment"][0]["value_as_written"] = "z6 " * 100

    lines, rejections = read_extraction_jsonl(write_jsonl(tmp_path / "x.jsonl", json.dumps(raw)))

    assert (lines, [r.reason for r in rejections]) == ([], ["schema_error"])


# --------------------------------------------------------------------------- privacy


@pytest.fixture
def log_messages() -> Iterator[list[str]]:
    messages: list[str] = []
    handler = logger.add(messages.append, level="DEBUG", format="{level} {message} {extra}")
    yield messages
    logger.remove(handler)


SECRET_TEXT = "Quilldorf Zebrafinch grew Sungold in pots. I'd try Quilldorf in zone 5."
SECRET_WORDS = ("Quilldorf", "Zebrafinch")


def test_logs_and_rejections_never_contain_comment_text(
    conn: sqlite3.Connection, tmp_path: Path, log_messages: list[str]
) -> None:
    seed(conn, {"s1": SECRET_TEXT, "s2": SECRET_TEXT})
    leaky = parsed(
        "s1",
        mention(
            "Quilldorf Zebrafinch grew Sungold in pots",
            fact("method", "pots", "Sungold in pots", "container"),
            fact("zone", "zone 5", "Quilldorf in zone 5", "5"),  # hypothetical
            fact("region", "Quilldorf", "Quilldorf Zebrafinch", "US-CT"),  # strict mismatch
            variety="Sungold",
        ),
        mention("Zebrafinch Quilldorf", variety="Zebrafinch"),  # not a substring
        text=SECRET_TEXT,
        warnings=["Quilldorf Zebrafinch warning text"],
    )
    path = write_jsonl(
        tmp_path / "x.jsonl",
        leaky,
        '{"comment_id": "s2", "text": "Quilldorf Zebrafinch", "status": "parsed"}',
        "Quilldorf Zebrafinch is not JSON",
        parsed("s3", mention("Quilldorf Zebrafinch")),
    )

    report = load_extractions(conn, path, CFG, LOADED)

    assert len(report.rejections) == 6
    assert log_messages, "the aggregate report must be logged"
    rendered = [*log_messages, *(r.model_dump_json() for r in report.rejections)]
    assert not [line for line in rendered if any(word in line for word in SECRET_WORDS)]
    assert {r.reason for r in report.rejections} <= KNOWN_REASONS


def test_rejections_are_plain_ids_and_codes() -> None:
    result = validate(TEXT_POTS, sungold(fact("method", "pots", "in pot")))

    assert result.rejections == (
        Rejection(comment_id="c1", mention_index=0, fact_index=0, reason="value_not_in_quote"),
    )


# =========================================================================== fixtures


def fixture_records() -> list[CommentRecord]:
    raw = (FIXTURES / "synthetic_comments.jsonl").read_text(encoding="utf-8").split("\n")
    rows = [json.loads(line) for line in raw if line.strip()]
    return [
        CommentRecord(
            comment_id=row["comment_id"],
            text=row["text"],
            permalink=row["permalink"],
            created_at=None if row["created_at"] is None else to_utc_iso(row["created_at"]),
            source_order=order,
        )
        for order, row in enumerate(rows)
    ]


@pytest.fixture
def demo(conn: sqlite3.Connection) -> sqlite3.Connection:
    source = SourceRecord(
        source_id="synthetic-demo",
        kind="synthetic_fixture",
        rights_status="synthetic",
        imported_at=LOADED,
        dataset_hash="fixture",
    )
    db.sync_source_comments(conn, source, fixture_records())
    return conn


FIXTURE_EXTRACTIONS = FIXTURES / "synthetic_extractions.jsonl"


def fixture_lines() -> dict[str, ParsedComment]:
    lines, rejections = read_extraction_jsonl(FIXTURE_EXTRACTIONS)
    assert rejections == []
    return {line.comment_id: line for _, line in lines}


def fixture_texts() -> dict[str, str]:
    return {record.comment_id: record.text for record in fixture_records()}


def test_every_fixture_extraction_line_validates_without_rejection() -> None:
    texts = fixture_texts()
    for comment_id, line in fixture_lines().items():
        assert line.text_hash == sha256(texts[comment_id]), comment_id
        result = validate_parsed_comment(line, texts[comment_id], REPO_CFG)
        assert result.rejections == (), comment_id
        assert result.status == line.status, comment_id
        assert len(result.mentions) == len(line.mentions), comment_id


def prompt_worked_example() -> tuple[dict[str, str], list[ParsedComment]]:
    """The input and output JSON blocks of docs/EXTRACTION_PROMPT.md's worked example."""
    doc = (REPO / "docs" / "EXTRACTION_PROMPT.md").read_text(encoding="utf-8")
    section = doc.split("### Worked example", 1)[1].split("\n### ", 1)[0]
    blocks = [part.split("```", 1)[0] for part in section.split("```json\n")[1:]]
    inputs = [json.loads(line) for line in blocks[0].splitlines() if line.strip()]
    assert all(row["text_hash"] == sha256(row["text"]) for row in inputs)
    outputs = [ParsedComment.model_validate_json(x) for x in blocks[1].splitlines() if x.strip()]
    return {row["comment_id"]: row["text"] for row in inputs}, outputs


def test_the_prompts_worked_example_passes_the_validator() -> None:
    """The model is taught with this example, so it must load without any rejection."""
    texts, outputs = prompt_worked_example()

    assert [line.comment_id for line in outputs] == list(texts)
    for line in outputs:
        assert line.text_hash == sha256(texts[line.comment_id]), line.comment_id
        result = validate_parsed_comment(line, texts[line.comment_id], REPO_CFG)
        assert result.rejections == (), line.comment_id
        assert result.status == line.status


def test_the_prompts_hypothetical_example_cannot_carry_a_zone() -> None:
    texts, _ = prompt_worked_example()
    added = mention("Great video", fact("zone", "zone 5", "in zone 5", "5"), crop=None)

    line = parsed("ex-3", added, text=texts["ex-3"])

    result = validate_parsed_comment(line, texts["ex-3"], REPO_CFG)

    assert reasons(result) == [(0, 0, "hypothetical_context")]


def test_fixture_load_report(demo: sqlite3.Connection) -> None:
    raw = [json.loads(x) for x in FIXTURE_EXTRACTIONS.read_text(encoding="utf-8").split("\n") if x]
    anonymous = sum(
        1
        for row in raw
        for m in row["mentions"]
        if m.get("crop") is None and m.get("variety_as_written") is None
    )

    report = load_extractions(demo, FIXTURE_EXTRACTIONS, REPO_CFG, LOADED)

    assert report.rejections == ()
    assert report_lines_add_up(report)
    assert (report.lines_read, report.comments_loaded) == (66, 66)
    assert report.status_counts == {"parsed": 61, "partial": 1, "unresolved": 4}
    assert (report.mentions_accepted, report.facts_accepted) == (71, 82)
    assert (report.mentions_rejected, report.facts_rejected) == (0, 0)
    assert report.unresolved_identities == anonymous
    assert db.summary_counts(demo, "public", NOW) == {
        "sources": 1,
        "comments": 69,
        "parsed_comments": 66,
        "mentions": 71,
        "facts": 82,
    }


def test_fixture_every_stored_quote_is_an_exact_substring(demo: sqlite3.Connection) -> None:
    load_extractions(demo, FIXTURE_EXTRACTIONS, REPO_CFG, LOADED)
    rows = demo.execute(
        "SELECT c.text, m.evidence_quote, f.evidence_quote FROM mentions m"
        " JOIN comments c ON c.comment_id = m.comment_id"
        " LEFT JOIN environment_facts f ON f.mention_id = m.mention_id"
    ).fetchall()

    assert rows
    assert all(
        m_quote in text and (f_quote is None or f_quote in text) for text, m_quote, f_quote in rows
    )


def test_fixture_spec2_greenhouse_peppers_and_outdoor_tomatoes(demo: sqlite3.Connection) -> None:
    load_extractions(demo, FIXTURE_EXTRACTIONS, REPO_CFG, LOADED)

    assert stored_facts(demo, "syn-0007") == [
        ("Jalapeño", "shelter", "greenhouse", "this_mention"),
        ("Roma", "shelter", "outdoors", "this_mention"),
    ]


def test_fixture_spec3_zone_only_comment_has_no_region(demo: sqlite3.Connection) -> None:
    load_extractions(demo, FIXTURE_EXTRACTIONS, REPO_CFG, LOADED)

    assert stored_facts(demo, "syn-0010") == [("Celebrity", "zone", "7a", "whole_comment")]
    region_rows = demo.execute(
        "SELECT f.value_as_written FROM environment_facts f WHERE f.key = 'region'"
    ).fetchall()
    assert not [row[0] for row in region_rows if "zone" in row[0].casefold()]


def test_fixture_spec3_zone_relabelled_as_region_is_rejected() -> None:
    line = fixture_lines()["syn-0010"]
    tampered = line.model_copy(
        update={
            "mentions": [
                line.mentions[0].model_copy(
                    update={
                        "environment": [
                            *line.mentions[0].environment,
                            fact("region", "zone 7a", "in my zone 7a garden", "US-VA"),
                        ]
                    }
                )
            ]
        }
    )

    result = validate_parsed_comment(tampered, fixture_texts()["syn-0010"], REPO_CFG)

    assert reasons(result) == [(0, 1, "strict_key_mismatch")]
    assert facts_of(result) == [("zone", "7a")]


def test_fixture_spec4_growing_location_not_residence(demo: sqlite3.Connection) -> None:
    load_extractions(demo, FIXTURE_EXTRACTIONS, REPO_CFG, LOADED)

    assert stored_facts(demo, "syn-0008") == [("Stupice", "region", "US-ME", "this_mention")]


def test_fixture_spec4_adding_the_residence_rejects_both_regions() -> None:
    line = fixture_lines()["syn-0008"]
    stupice = line.mentions[0]
    with_residence = stupice.model_copy(
        update={
            "environment": [
                fact("region", "Florida", "I live in Florida", "US-FL"),
                *stupice.environment,
            ]
        }
    )

    result = validate_parsed_comment(
        line.model_copy(update={"mentions": [with_residence]}),
        fixture_texts()["syn-0008"],
        REPO_CFG,
    )

    assert reasons(result) == [(0, 0, "conflicting_region"), (0, 1, "conflicting_region")]
    assert (facts_of(result), result.status) == ([], "partial")


def test_fixture_spec5_hypothetical_zone_is_absent_and_cannot_be_added(
    demo: sqlite3.Connection,
) -> None:
    load_extractions(demo, FIXTURE_EXTRACTIONS, REPO_CFG, LOADED)
    assert stored_facts(demo, "syn-0009") == []

    line = fixture_lines()["syn-0009"]
    glacier = line.mentions[0].model_copy(
        update={"environment": [fact("zone", "zone 5", "zone 5", "5")]}
    )
    result = validate_parsed_comment(
        line.model_copy(update={"mentions": [glacier]}), fixture_texts()["syn-0009"], REPO_CFG
    )

    assert reasons(result) == [(0, 0, "hypothetical_context")]


def test_fixture_ambiguous_and_injected_comments(demo: sqlite3.Connection) -> None:
    load_extractions(demo, FIXTURE_EXTRACTIONS, REPO_CFG, LOADED)

    assert (stored_status(demo, "syn-0014"), stored_facts(demo, "syn-0014")) == ("partial", [])
    assert stored_status(demo, "syn-0020") == "unresolved"
    injected = demo.execute("SELECT COUNT(*) FROM mentions WHERE comment_id = 'syn-0020'")
    assert injected.fetchone()[0] == 0
    never_extracted = ("syn-0004", "syn-0025", "syn-0069")
    assert [stored_status(demo, cid) for cid in never_extracted] == [None, None, None]


def test_fixture_whole_comment_facts_are_stored_once(demo: sqlite3.Connection) -> None:
    load_extractions(demo, FIXTURE_EXTRACTIONS, REPO_CFG, LOADED)

    assert stored_facts(demo, "syn-0013") == [
        ("Sungold", "region", "US-CT", "whole_comment"),
        ("Sungold", "method", "container", "whole_comment"),
    ]
    assert table_count(demo, "mentions WHERE comment_id = 'syn-0013'") == 3
