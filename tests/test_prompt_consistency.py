"""Keep docs/EXTRACTION_PROMPT.md Part 2 consistent with the code that validates its output.

Part 2 is handed to the extractor model on its own, so it must carry the facts the loader
enforces: the hypothetical markers of ``config.yaml``, every rejection reason code, the region
vocabulary and aliases, the phrasings the loader reads, the crop synonyms and worked examples
that load without a single rejection.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml

from crop_explorer.config import ExtractionConfig, load_config
from crop_explorer.extraction import REJECTION_REASONS, validate_parsed_comment
from crop_explorer.normalization import CROP_SYNONYMS, mentions_crop, normalize_written
from crop_explorer.regions import REGION_NAMES
from crop_explorer.schemas import EnvironmentFact, ParsedComment, VarietyMention

REPO = Path(__file__).resolve().parents[1]
PROMPT = (REPO / "docs" / "EXTRACTION_PROMPT.md").read_text(encoding="utf-8")
PART_2 = PROMPT.split("## Part 2: Instructions for the extractor", 1)[1]
REPO_CFG: ExtractionConfig = load_config().extraction
BACKTICKED = re.compile(r"`([^`\n]+)`")
TABLE_ROW = re.compile(r"^\|(.+)\|\s*$", re.MULTILINE)
REGION_PAIR = re.compile(r"`([A-Z]{2}(?:-[A-Z]{2})?)` ([A-Z][^,`\n]*?)(?=,|\s*$)", re.MULTILINE)


def section(heading: str) -> str:
    """The body of the Part 2 subsection titled ``heading`` (up to the next heading)."""
    body = PART_2.split(f"\n{heading}\n", 1)[1]
    return re.split(r"\n#{2,4} ", body, maxsplit=1)[0]


def paragraph_after(label: str) -> str:
    """The lines that follow the Part 2 line starting with ``label`` (maybe indented), up to a
    blank line."""
    after = re.split(rf"\n[ \t]*{re.escape(label)}", PART_2, maxsplit=1)[1].split("\n", 1)[1]
    return after.split("\n\n", 1)[0]


def table_cells(text: str) -> list[list[str]]:
    """Cells of every Markdown table row in ``text``, header and separator rows excluded."""
    rows = [[cell.strip() for cell in m.group(1).split("|")] for m in TABLE_ROW.finditer(text)]
    return [row for row in rows[2:] if not set(row[0]) <= {"-", ":"}]


def unquote(cell: str) -> str:
    return cell.strip().strip("`")


# --------------------------------------------------------------------------- markers


def test_the_marker_list_equals_the_configured_markers() -> None:
    raw = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    configured = raw["extraction"]["hypothetical_markers"]

    documented = BACKTICKED.findall(paragraph_after("**Markers**"))

    assert sorted(documented) == sorted(configured)
    assert len(set(documented)) == len(documented)


def test_part_2_is_self_contained() -> None:
    """The model sees Part 2 only: it never points at repository files."""
    for reference in ("config.yaml", ".py", "ARCHITECTURE.md", "SCENARIOS.md"):
        assert reference not in PART_2, reference


# --------------------------------------------------------------------------- reason codes


@pytest.mark.parametrize("reason", sorted(REJECTION_REASONS))
def test_every_rejection_reason_is_documented(reason: str) -> None:
    assert f"`{reason}`" in PART_2


# --------------------------------------------------------------------------- vocabulary


def test_the_region_codes_are_the_whole_region_vocabulary() -> None:
    documented = REGION_PAIR.findall(section("#### Region codes"))

    assert dict(documented) == dict(REGION_NAMES)
    assert len(documented) == len(REGION_NAMES)


def test_every_documented_region_alias_is_read_as_its_code() -> None:
    rows = table_cells(section("#### Region aliases"))

    assert rows
    for written, code in rows:
        for alias in written.split("; "):
            assert normalize_written("region", alias) == unquote(code), alias


def test_every_place_documented_as_unread_stays_unread() -> None:
    places = paragraph_after("**Not read as a region**").split("; ")

    assert len(places) > 3
    for place in places:
        assert normalize_written("region", place.strip().rstrip(".")) is None, place


def test_every_documented_phrasing_is_read_as_its_value() -> None:
    rows = table_cells(section("#### Categorical values"))

    assert {unquote(key) for key, _, _ in rows} >= {"method", "humidity", "irrigation"}
    for key, value, phrasings in rows:
        for phrase in phrasings.split(", "):
            assert normalize_written(unquote(key), phrase) == unquote(value), (key, phrase)


def test_every_documented_look_alike_names_no_value() -> None:
    rows = table_cells(section("#### Look-alikes"))

    assert rows
    for key, phrases in rows:
        for phrase in phrases.split("; "):
            assert normalize_written(unquote(key), phrase) is None, (key, phrase)


def test_the_crop_synonyms_are_the_loaders() -> None:
    documented = BACKTICKED.findall(paragraph_after("**Crop synonyms**"))

    assert set(documented) == set(CROP_SYNONYMS)


@pytest.mark.parametrize(
    ("quote", "crop", "names_it"),
    [
        ("tomatoe plants", "tomato", False),  # an unlisted misspelling
        ("my beefsteaks", "tomato", False),  # a class word alone
        ("cherry tomatoes", "tomato", True),
        ("bell peppers", "pepper", True),
    ],
)
def test_the_crop_word_claims_hold(quote: str, crop: str, names_it: bool) -> None:
    assert mentions_crop(quote, crop) is names_it


# --------------------------------------------------------------------------- worked examples


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def worked_example() -> tuple[dict[str, str], list[ParsedComment]]:
    """The input and output JSON blocks of the prompt's worked example."""
    blocks = [part.split("```", 1)[0] for part in section("### Worked example").split("```json\n")]
    inputs = [json.loads(line) for line in blocks[1].splitlines() if line.strip()]
    assert all(row["text_hash"] == sha256(row["text"]) for row in inputs)
    outputs = [ParsedComment.model_validate_json(x) for x in blocks[2].splitlines() if x.strip()]
    return {row["comment_id"]: row["text"] for row in inputs}, outputs


def test_the_worked_example_validates_without_a_single_rejection() -> None:
    """The model is taught with these lines, so each must load exactly as written."""
    texts, outputs = worked_example()

    assert [line.comment_id for line in outputs] == list(texts)
    for line in outputs:
        assert line.text_hash == sha256(texts[line.comment_id]), line.comment_id
        result = validate_parsed_comment(line, texts[line.comment_id], REPO_CFG)
        assert result.rejections == (), line.comment_id
        assert result.status == line.status, line.comment_id
        assert len(result.mentions) == len(line.mentions), line.comment_id


def test_the_worked_example_covers_the_judgement_rules() -> None:
    """No-crop report, same crop twice, hearsay and a non-USDA zone are all demonstrated."""
    _, outputs = worked_example()
    per_comment = [line.mentions for line in outputs]
    mentions = [m for ms in per_comment for m in ms]
    facts = [f for m in mentions for f in m.environment]

    assert any(m.crop is None and m.variety_as_written is None for m in mentions)
    assert any(len({m.crop for m in ms}) < len(ms) for ms in per_comment)
    assert any(f.key == "zone" and f.normalized_value is None for f in facts)
    assert any(f.key == "summer_heat" and f.normalized_value == "heatwave" for f in facts)
    assert any("hearsay" in w for line in outputs for w in line.warnings)


def test_the_worked_hypothetical_cannot_carry_a_zone() -> None:
    texts, outputs = worked_example()
    (line,) = [line for line in outputs if line.comment_id == "ex-3"]
    zone = EnvironmentFact(
        key="zone",
        value_as_written="zone 5",
        normalized_value="5",
        evidence_quote="in zone 5",
        applicability="this_mention",
        basis="reported_garden_context",
    )
    added = VarietyMention(
        crop=None, variety_as_written=None, evidence_quote="Great video", environment=[zone]
    )

    result = validate_parsed_comment(
        line.model_copy(update={"mentions": [added]}), texts["ex-3"], REPO_CFG
    )

    assert [r.reason for r in result.rejections] == ["hypothetical_context"]
