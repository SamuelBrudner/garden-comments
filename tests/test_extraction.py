"""Tests for extraction validation and loading (docs/ARCHITECTURE.md, SPEC §4 and §8).

Covers validation rules 1-8, the SPEC §11 required tests #2 (environment association), #3 (no
invented geography), #4 (location semantics), #5 (hypothetical exclusion) and #18 (provenance),
JSONL reading, and idempotent, all-or-nothing loading with an aggregate report that never
carries comment text.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from loguru import logger

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
        "mention_quote_not_substring",
        "variety_not_in_quote",
        "invalid_crop",
        "invalid_variety",
        "fact_quote_not_substring",
        "value_not_in_quote",
        "invalid_normalized_value",
        "strict_key_mismatch",
        "hypothetical_context",
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
    crop: str | None = "tomato",
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


def parsed(
    comment_id: str,
    *mentions: VarietyMention,
    status: ParseStatus = "parsed",
    warnings: Sequence[str] = (),
) -> ParsedComment:
    return ParsedComment(
        comment_id=comment_id,
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
    return validate_parsed_comment(parsed("c1", *mentions, status=status), text, cfg)


def reasons(result: ValidatedComment) -> list[tuple[int | None, int | None, str]]:
    return [(r.mention_index, r.fact_index, r.reason) for r in result.rejections]


def facts_of(result: ValidatedComment, index: int = 0) -> list[tuple[str, str | None]]:
    return [(f.key, f.normalized_value) for f in result.mentions[index].facts]


def sungold(*facts: EnvironmentFact) -> VarietyMention:
    """The Sungold mention of ``TEXT_POTS``."""
    return mention("I grow Sungold in pots", *facts, variety="Sungold")


# --------------------------------------------------------------------------- db helpers


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = db.connect(MEMORY)
    yield connection
    connection.close()


def seed(conn: sqlite3.Connection, texts: dict[str, str], source_id: str = "src-a") -> None:
    source = SourceRecord(
        source_id=source_id,
        kind="test_comments",
        rights_status="synthetic",
        imported_at=LOADED,
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


def test_rule2_a_mention_without_a_variety_is_fine() -> None:
    result = validate(TEXT_POTS, mention("I grow Sungold in pots", variety=None))

    assert result.rejections == ()
    assert result.mentions[0].variety_key is None


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
    [("   ", None, "invalid_crop"), ("tomato", "--", "invalid_variety")],
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


# =========================================================================== rule 5


def test_rule5_normalized_value_must_be_a_vocabulary_value() -> None:
    result = validate(TEXT_POTS, sungold(fact("method", "pots", "in pots", "pots")))

    assert facts_of(result) == []
    assert reasons(result) == [(0, 0, "invalid_normalized_value")]


def test_rule5_missing_normalized_value_is_derived_from_the_written_phrase() -> None:
    result = validate(
        TEXT_POTS,
        sungold(
            fact("zone", "Zone 6b", "Zone 6b here"),
            fact("humidity", "muggy summers", "loves the muggy summers"),
        ),
    )

    assert facts_of(result) == [("zone", "6b"), ("humidity", "humid")]
    assert result.rejections == ()


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
    ("text", "key", "written", "claimed"),
    [
        ("Zone 6 here and Sungold thrived.", "zone", "Zone 6", "6b"),  # never upgraded
        ("My zone 6b garden grew Sungold.", "region", "zone 6b", "US-CT"),  # zone is not a place
        ("Sungold in 2023 was great.", "growing_year", "2023", "2024"),
        (
            "Sungold in Kansas City, Missouri was great.",
            "region",
            "Kansas City, Missouri",
            "US-MO",
        ),
        ("Sungold did well in Kansas.", "region", "Kansas", "US-MO"),
    ],
)
def test_rule5_strict_keys_must_equal_the_loaders_normalisation(
    text: str, key: EnvKey, written: str, claimed: str
) -> None:
    result = validate(text, mention("Sungold", fact(key, written, written, claimed)))

    assert facts_of(result) == []
    assert reasons(result) == [(0, 0, "strict_key_mismatch")]


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


@settings(max_examples=60)
@given(
    marker=st.sampled_from(REPO_CFG.hypothetical_markers),
    apostrophe=st.sampled_from(["'", "\u2019"]),
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
            fact("region", "Maine", "Maine summers"),
            variety="Sungold",
        ),
    )

    assert facts_of(result) == [("region", "US-ME"), ("region", "US-ME")]
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
        mention("Sungold", fact("zone", phrase, phrase)),
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

    assert reasons(result) == [(0, 0, "strict_key_mismatch")]


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


@given(text=st.text(min_size=1), quote=st.text(min_size=1))
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
            fact("humidity", "muggy", "the muggy summers"),
            variety="Sungold",
            topics=("growing_method",),
        ),
        status=status,
    )


def line_b() -> ParsedComment:
    return parsed(
        "b",
        mention("Brandywine had the best flavor", variety="Brandywine", topics=("flavor",)),
    )


def line_c() -> ParsedComment:
    return parsed("c", status="unresolved", warnings=["no_growing_report: a question"])


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
    slim = parsed("a", mention("I grow Sungold in pots", variety="Sungold"), status="partial")

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
    )
    seed(seeded, {"h": TEXT_HYPOTHETICAL}, source_id="src-h")
    glacier = parsed(
        "h",
        mention(
            "Those Glacier tomatoes look amazing",
            fact("zone", "zone 5", "zone 5", "5"),
            variety="Glacier",
        ),
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
        mention("Zone 6b here", crop="tomato", variety=None),
    )
    path = write_jsonl(tmp_path / "x.jsonl", anonymous, line_b())

    report = load_extractions(seeded, path, CFG, LOADED)

    assert report.unresolved_identities == 2
    assert (report.mentions_accepted, report.mentions_rejected) == (4, 1)


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
        result = validate_parsed_comment(line, texts[comment_id], REPO_CFG)
        assert result.rejections == (), comment_id
        assert result.status == line.status, comment_id
        assert len(result.mentions) == len(line.mentions), comment_id


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
