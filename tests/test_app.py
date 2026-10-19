"""End-to-end tests of the Streamlit viewer (``app.py``) with ``streamlit.testing.v1.AppTest``.

A temporary database is built with the real ``db``, ``ingest`` and ``extraction`` modules: the
synthetic fixtures (public demo data), one extra synthetic comment full of Markdown/HTML, and one
``private_unverified`` comment with rich, matching extracted facts. Covers SPEC §2 (Browse screen,
grouping, strict toggle, chips), §6 (state filter and map notes), §9/§11 #15 (rights isolation:
nothing private reaches the public view), §11 #1 (text search finds unextracted comments), #16
(limitation message), #17 (inputs exist, Clear resets them, source references open: a cleared
comment's permalink becomes a "Source ↗" link while synthetic rows never link out) and #18 (every
card and the map carry their provenance labels), that untrusted comment text renders literally,
and that a private-scope viewer refuses to run unless it listens on this computer only. Phone
width is covered by ``tests/test_browser.py``.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import runpy
import shutil
import string
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import streamlit as st
import yaml
from loguru import logger
from streamlit import config as streamlit_config
from streamlit.testing.v1 import AppTest

from crop_explorer import db, overlay_charts, overlays, search, ui
from crop_explorer.config import DEFAULT_CONFIG_PATH, load_config
from crop_explorer.extraction import load_extractions
from crop_explorer.ingest import build_source, import_file
from crop_explorer.overlays import (
    NORMALS_SOURCE,
    OVERLAY_LABEL,
    ZONE_DISCLAIMER,
    ZONE_PERIOD,
    ZONE_SOURCE,
    OverlayDataError,
)
from crop_explorer.regions import US_STATES
from crop_explorer.schemas import ColumnMapping, RightsStatus

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "app.py"
FIXTURES = REPO / "fixtures"
NOW = "2026-10-15T12:00:00Z"
TIMEOUT = 60

PRIVATE_ID = "private-0001"
PRIVATE_TEXT = "PRIVATEONLY Brandywine grown in containers in zone 6b near Hartford, CT."
MARKDOWN_ID = "md-0001"
MARKDOWN_TEXT = (
    "Look [click me](https://evil.example) ![pic](https://evil.example/x.png) <b>bold</b>\n"
    "# Not a heading :smile: $x^2$ www.evil.example"
)
INJECTION_ID = "syn-0020"
PAGE_SIZE = 100
"""Cards per page in the test config: every group fits on one page unless a test lowers it."""
CLEARED_ID = "clr-0001"
CLEARED_TEXT = "CLEAREDLINK Moonbeam okra in containers in Omaha, Nebraska."
CLEARED_URL = "https://www.youtube.com/watch?v=abc123&lc=Ug-cleared"
UNLINKED_ID = "clr-0002"
UNLINKED_TEXT = "CLEAREDNOLINK Our okra never set pods."
SYNTH_LINK_ID = "syn-link-0001"
SYNTH_LINK_TEXT = "SYNTHLINK A fictional demo comment about okra."
CT_COMMENTS = ("syn-0006", "syn-0013", "syn-0036", "syn-0063")
FULL_MATCHES = ("syn-0001", "syn-0003", "syn-0006")

_PUNCT = frozenset(string.punctuation)
_UNESCAPED_LINK = re.compile(r"(?<!\\)\[[^\]\n]*(?<!\\)\]\((?!<https://)")


# --------------------------------------------------------------------------- database


@dataclass(frozen=True)
class AppEnv:
    """A config file and database for the viewer."""

    root: Path
    config: Path
    db_path: Path
    texts: Mapping[str, str]


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _import(conn: Any, path: Path, source_id: str, rights: RightsStatus) -> None:
    source = build_source(
        path,
        source_id=source_id,
        kind="test_comments",
        rights_status=rights,
        source_url=None,
        expires_at=None,
        imported_at=NOW,
    )
    import_file(conn, path, ColumnMapping(), source)


def _private_extraction() -> dict[str, Any]:
    def fact(key: str, written: str, value: str, quote: str) -> dict[str, str]:
        return {
            "key": key,
            "value_as_written": written,
            "normalized_value": value,
            "evidence_quote": quote,
            "applicability": "this_mention",
            "basis": "reported_garden_context",
        }

    return {
        "comment_id": PRIVATE_ID,
        "text_hash": _sha256(PRIVATE_TEXT),
        "extractor_version": "test-v1",
        "status": "parsed",
        "mentions": [
            {
                "crop": "tomato",
                "variety_as_written": "Brandywine",
                "topics": ["growing_method"],
                "evidence_quote": "Brandywine grown in containers",
                "environment": [
                    fact("method", "containers", "container", "grown in containers"),
                    fact("zone", "zone 6b", "6b", "in zone 6b"),
                    fact("region", "Hartford, CT", "US-CT", "near Hartford, CT"),
                ],
            }
        ],
    }


def _write_config(
    root: Path, db_path: Path, *, page_size: int = PAGE_SIZE, name: str = "config.yaml"
) -> Path:
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    raw["database"]["path"] = str(db_path)
    raw["viewer"]["page_size"] = page_size
    raw["logging"] = {"level": "WARNING", "json_path": None}
    config = root / name
    config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return config


def _build_db(root: Path, db_path: Path, config: Path) -> None:
    cfg = load_config(config)
    conn = db.connect(db_path)
    try:
        _import(conn, FIXTURES / "synthetic_comments.jsonl", "synthetic-demo", "synthetic")
        report = load_extractions(
            conn, FIXTURES / "synthetic_extractions.jsonl", cfg.extraction, NOW
        )
        assert report.rejections == ()
        markdown = [{"comment_id": MARKDOWN_ID, "text": MARKDOWN_TEXT}]
        _import(conn, _write_jsonl(root / "md.jsonl", markdown), "markdown-test", "synthetic")
        private = [{"comment_id": PRIVATE_ID, "text": PRIVATE_TEXT}]
        _import(
            conn,
            _write_jsonl(root / "private.jsonl", private),
            "private-import",
            "private_unverified",
        )
        extracted = _write_jsonl(root / "private.extractions.jsonl", [_private_extraction()])
        report = load_extractions(conn, extracted, cfg.extraction, NOW)
        assert report.rejections == ()
        assert report.facts_accepted == 3
    finally:
        conn.close()


@pytest.fixture(scope="module")
def app_env(tmp_path_factory: pytest.TempPathFactory) -> AppEnv:
    root = tmp_path_factory.mktemp("viewer")
    db_path = root / "viewer.sqlite"
    config = _write_config(root, db_path)
    _build_db(root, db_path, config)
    lines = (FIXTURES / "synthetic_comments.jsonl").read_text(encoding="utf-8").splitlines()
    texts = {row["comment_id"]: row["text"] for row in map(json.loads, lines)}
    texts |= {MARKDOWN_ID: MARKDOWN_TEXT, PRIVATE_ID: PRIVATE_TEXT}
    return AppEnv(root=root, config=config, db_path=db_path, texts=texts)


def _write_stderr(message: str) -> None:
    sys.stderr.write(message)  # resolved per call, so never a capture stream pytest closed


@pytest.fixture(autouse=True)
def _restore_loguru() -> Iterator[None]:
    """The app reconfigures loguru; give later tests a sink that never holds a closed stream."""
    yield
    logger.remove()
    logger.add(_write_stderr, level="DEBUG")


@pytest.fixture(autouse=True)
def _restore_server_address() -> Iterator[None]:
    """Tests may change Streamlit's process-wide ``server.address``; put it back afterwards."""
    original = streamlit_config.get_option("server.address")
    yield
    streamlit_config.set_option("server.address", original)


def run_app(
    monkeypatch: pytest.MonkeyPatch,
    env: AppEnv,
    *,
    scope: str = "public",
    db_path: Path | None = None,
    config: Path | None = None,
    address: str | None = "localhost",
    extra_args: Sequence[str] = (),
) -> AppTest:
    streamlit_config.set_option("server.address", address)
    argv = ["app.py", "--config", str(config or env.config), "--db", str(db_path or env.db_path)]
    monkeypatch.setattr(sys, "argv", [*argv, "--scope", scope, *extra_args])
    at = AppTest.from_file(str(APP), default_timeout=TIMEOUT)
    return at.run()


# --------------------------------------------------------------------------- inspection helpers


def texts_of(node: Any) -> list[str]:
    """Return every Markdown, caption, alert, heading and expander text under ``node``."""
    values: list[str] = []
    for kind in (
        "markdown",
        "caption",
        "info",
        "warning",
        "error",
        "success",
        "subheader",
        "title",
    ):
        values += [str(element.value) for element in getattr(node, kind)]
    values += [expander.label for expander in node.expander]
    return values


def all_text(at: AppTest) -> str:
    widgets = [
        w.label for kind in ("selectbox", "multiselect", "button") for w in getattr(at, kind)
    ]
    options = [o for box in at.selectbox for o in box.options]
    return "\n".join([*texts_of(at), *widgets, *options])


def group(at: AppTest, key: str) -> Any:
    return at.main.get_by_key(f"group-{key}")


def has_group(at: AppTest, key: str) -> bool:
    try:
        group(at, key)
    except KeyError:
        return False
    return True


def shown_ids(node: Any, env: AppEnv) -> set[str]:
    """Return the ids of comments whose escaped text is rendered under ``node``."""
    rendered = {element.value for element in node.markdown}
    return {cid for cid, text in env.texts.items() if ui.escape_markdown(text) in rendered}


def walk(node: Any) -> Iterator[Any]:
    """Yield ``node`` and every descendant in display order."""
    yield node
    children = getattr(node, "children", None) or {}
    for index in sorted(children):
        yield from walk(children[index])


def link_buttons(at: AppTest) -> list[tuple[str, str]]:
    return [(button.proto.label, button.proto.url) for button in at.get("link_button")]


def expander(at: AppTest, prefix: str) -> Any:
    (found,) = [e for e in at.expander if e.label.startswith(prefix)]
    return found


def infos(at: AppTest) -> list[str]:
    return [str(element.value) for element in at.info]


def subheaders(at: AppTest) -> list[str]:
    return [element.value for element in at.subheader]


def select_garden(at: AppTest, **conditions: str) -> AppTest:
    for key, value in conditions.items():
        at.selectbox(key=ui.CONDITION_KEYS[key]).set_value(value)
    return at.run()


def bare_punctuation(rendered: str) -> list[str]:
    bare, index = [], 0
    while index < len(rendered):
        if rendered[index] == "\\" and index + 1 < len(rendered) and rendered[index + 1] in _PUNCT:
            index += 2
            continue
        if rendered[index] in _PUNCT:
            bare.append(rendered[index])
        index += 1
    return bare


# --------------------------------------------------------------------------- browse screen


class TestBrowseScreen:
    def test_renders_with_no_conditions(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        assert not at.exception
        assert [t.value for t in at.title] == ["What Gardeners Are Saying"]
        assert "Find real experiences from gardens like yours." in texts_of(at)
        assert ui.DEMO_BANNER in [info.value for info in at.info]
        assert subheaders(at)[-1] == "Reports (70)"
        assert len(shown_ids(group(at, "all"), app_env)) == 70

    def test_limitation_message_is_prominent(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        assert (
            "Only 33 of 70 reports mention comparable growing conditions (4 not yet analysed)."
            in infos(at)
        )

    def test_search_and_condition_inputs_exist(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        assert at.text_input(key=ui.TEXT_KEY).label == (
            "Search crops, varieties, or anything people mentioned"
        )
        labels = {box.label for box in at.selectbox}
        assert {
            "Region",
            "Hardiness zone",
            "Growing method",
            "Setting",
            "Summer heat",
            "Humidity",
            "Rainfall",
            "Season length",
            "Sun exposure",
            "Soil",
            "Watering",
            "Year grown",
            "Crop",
            "Filter by reported state",
        } <= labels
        assert at.multiselect(key=ui.TOPICS_KEY).label == "Topics"
        assert at.checkbox(key=ui.STRICT_KEY).label == "Only show stated matches"
        assert [tab.label for tab in at.tabs] == [*ui.GLANCE_TABS, "Comments", "Map"]
        assert {"About / Sources", "More conditions", "Optional outside context"} <= {
            e.label for e in at.expander
        }

    def test_region_options_start_unset(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        region = at.selectbox(key=ui.REGION_KEY)
        assert region.options[0] == "Not set"
        assert region.options[1] == "Alabama"
        assert region.value is None

    def test_text_search_finds_unextracted_comments(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        at.text_input(key=ui.TEXT_KEY).input("kids watch").run()
        assert shown_ids(group(at, "all"), app_env) == {"syn-0025"}  # never extracted
        assert "1 report" in texts_of(at)
        assert ui.NOT_ANALYSED_LINE in texts_of(group(at, "all"))  # F20: never "not stated"
        at.text_input(key=ui.TEXT_KEY).input("trellis").run()
        assert "syn-0033" in shown_ids(group(at, "all"), app_env)  # unresolved, no mentions

    def test_crop_filter(self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv) -> None:
        at = run_app(monkeypatch, app_env)
        crop = at.selectbox(key=ui.CROP_KEY)
        assert crop.options[0] == "Any crop"
        assert "Tomato" in crop.options
        crop.set_value("bean").run()
        assert shown_ids(group(at, "all"), app_env) >= {"syn-0026", "syn-0051"}
        assert not at.exception


class TestLayout:
    """SPEC §2 layout: F45 (prominent controls, outside context by the tabs), F31, F37."""

    def test_only_the_spec_prominent_conditions_are_always_visible(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        more = expander(at, "More conditions")
        hidden = {box.key for box in more.selectbox}
        assert hidden == {ui.CONDITION_KEYS[key] for key in ui.MORE_KEYS}
        assert ui.CONDITION_KEYS["summer_heat"] in hidden
        shown = {box.key for box in at.selectbox} - hidden
        assert {ui.REGION_KEY, *(ui.CONDITION_KEYS[k] for k in ui.ALWAYS_VISIBLE_KEYS)} <= shown
        assert more.proto.expanded is False

    def test_year_is_a_filter_beside_crop_and_topics(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        assert at.selectbox(key=ui.YEAR_KEY).label == "Year grown"
        assert ui.YEAR_KEY not in {box.key for box in expander(at, "More conditions").selectbox}
        order = [n.key for n in walk(at.main) if getattr(n, "key", None)]
        crop, topics, year = (order.index(k) for k in (ui.CROP_KEY, ui.TOPICS_KEY, ui.YEAR_KEY))
        assert crop < topics < year

    def test_outside_context_sits_between_the_search_and_the_tabs(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        nodes = list(walk(at.main))
        outside = next(
            i for i, n in enumerate(nodes) if getattr(n, "label", "") == "Optional outside context"
        )
        strict = next(i for i, n in enumerate(nodes) if getattr(n, "key", "") == ui.STRICT_KEY)
        comments_tab = next(
            i for i, n in enumerate(nodes) if type(n).__name__ == "Tab" and n.label == "Comments"
        )
        assert strict < outside < comments_tab

    def test_search_box_is_capped(self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv) -> None:
        at = run_app(monkeypatch, app_env)
        cfg = load_config(app_env.config)
        assert at.text_input(key=ui.TEXT_KEY).proto.max_chars == cfg.viewer.max_query_chars


class TestGrouping:
    def test_zone_and_method_regroup_results(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = select_garden(run_app(monkeypatch, app_env), zone="6b", method="container")
        heads = subheaders(at)
        assert "Matching reported conditions (3)" in heads
        assert "Partially matching conditions (4)" in heads
        assert any(h.startswith("Conditions not stated or not comparable (") for h in heads)
        assert not has_group(at, "all")
        assert shown_ids(group(at, "match"), app_env) == set(FULL_MATCHES)
        assert shown_ids(group(at, "partial"), app_env) == {
            "syn-0005",
            "syn-0011",
            "syn-0013",
            "syn-0053",
        }

    def test_a_topic_filter_keeps_comparing_the_searched_crop(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        """Validation regression: Topics "pests" moved syn-0068's zucchini to Matching with the
        clay its Waltham Butternut grew in (SPEC §11 #2)."""
        at = select_garden(run_app(monkeypatch, app_env), soil="clay")
        at.text_input(key=ui.TEXT_KEY).input("zucchini").run()
        assert "syn-0068" in shown_ids(group(at, "not_stated"), app_env)
        at.multiselect(key=ui.TOPICS_KEY).select("pests").run()
        assert "syn-0068" in shown_ids(group(at, "not_stated"), app_env)
        assert not has_group(at, "match")

    def test_different_group_is_collapsed(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = select_garden(run_app(monkeypatch, app_env), zone="6b", method="container")
        (different,) = [e for e in at.expander if e.label.startswith("Different reported")]
        assert different.label == "Different reported conditions (8)"
        assert different.proto.expanded is False
        assert "syn-0019" in shown_ids(group(at, "different"), app_env)

    def test_cards_explain_conditions_and_evidence(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = select_garden(run_app(monkeypatch, app_env), zone="6b", method="container")
        match = "\n".join(texts_of(group(at, "match")))
        assert ui.escape_markdown("Relevant: zone 6b · containers") in match
        assert ui.escape_markdown("Region: Connecticut (same as yours)") not in match
        assert ui.escape_markdown("Region: Connecticut") in match
        assert ui.escape_markdown(ui.SYNTHETIC_NOTE) in match
        assert ui.EVIDENCE_LABEL in match
        partial = "\n".join(texts_of(group(at, "partial")))
        assert ui.escape_markdown("Not stated: hardiness zone") in partial
        imprecise = "Relevant: zone 6, subzone unknown (you: zone 6b) · containers"
        assert ui.escape_markdown(imprecise) in partial

    def test_count_summary_lists_every_tier(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = select_garden(run_app(monkeypatch, app_env), zone="6b", method="container")
        summary = [t for t in texts_of(at) if t.startswith("70 reports ·")]
        assert summary == [
            "70 reports · 3 matching · 4 partially matching · 55 not stated or not comparable "
            "· 8 different"
        ]

    def test_strict_checkbox_hides_groups_and_shows_count(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = select_garden(run_app(monkeypatch, app_env), zone="6b", method="container")
        assert (
            "Ticking this would hide 63 reports whose conditions were not stated, not comparable "
            "or differ." in texts_of(at)
        )
        assert at.checkbox(key=ui.STRICT_KEY).help == (
            "Hide reports whose conditions were not stated, not comparable or different from "
            "yours."
        )
        at.checkbox(key=ui.STRICT_KEY).check().run()
        assert has_group(at, "match") and has_group(at, "partial")
        assert not has_group(at, "not_stated")
        assert not has_group(at, "different")
        assert (
            "63 reports hidden because their conditions were not stated, not comparable or differ."
            in texts_of(at)
        )

    def test_chip_tooltips_state_what_they_count(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        """F18: a two-condition chip states how many reports mention any and both conditions."""
        at = run_app(monkeypatch, app_env)
        assert at.button(key="chip_0").proto.help == "Set this condition (8 reports state it)"
        assert at.button(key="chip_1").proto.help == (
            "6 reports describe hot summers or humid conditions in their own words; "
            "2 describe both"
        )
        assert at.button(key="chip_2").proto.help == (
            "Set this condition (4 reports describe it in their own words)"
        )

    def test_hot_and_humid_matches_quote_the_commenters_words(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        at.button(key="chip_1").click().run()
        assert shown_ids(group(at, "match"), app_env) == {"syn-0001", "syn-0041"}
        match = "\n".join(texts_of(group(at, "match")))
        georgia = "In their own words: “brutally hot” (counted as hot summers) · Georgia"
        assert ui.escape_markdown(georgia) in match
        assert "Relevant" not in match
        assert not has_group(at, "different")

    def test_a_self_description_unlike_yours_is_not_comparable(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = select_garden(run_app(monkeypatch, app_env), humidity="dry_air")
        assert "Conditions not stated or not comparable (" in "\n".join(subheaders(at))
        assert not has_group(at, "different")
        assert shown_ids(group(at, "match"), app_env) == {"syn-0019"}
        not_stated = group(at, "not_stated")
        assert {"syn-0001", "syn-0003", "syn-0006", "syn-0041", "syn-0052"} <= shown_ids(
            not_stated, app_env
        )
        line = (
            "In their own words: “muggy” (humid; you chose dry air) · not counted as a difference"
        )
        assert ui.escape_markdown(line) in "\n".join(texts_of(not_stated))

    def test_chip_conditions_inside_more_conditions_are_visible(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        """F32: a condition set out of sight is named on the expander, which opens."""
        at = run_app(monkeypatch, app_env)
        at.button(key="chip_1").click().run()
        more = expander(at, "More conditions")
        assert more.label == "More conditions (hot summers, humid)"
        assert more.proto.expanded is True
        at.selectbox(key=ui.CONDITION_KEYS["summer_heat"]).set_value(None).run()
        assert expander(at, "More conditions").label == "More conditions (humid)"
        assert at.button(key="chip_1").proto.type == "secondary"

    def test_chip_applies_its_conditions(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        labels = [b.label for b in at.button if b.key and b.key.startswith("chip_")]
        assert labels == ["Containers", "Hot & humid", "Short season", "Greenhouse"]
        at.button(key="chip_1").click().run()
        assert at.selectbox(key=ui.CONDITION_KEYS["summer_heat"]).value == "hot"
        assert at.selectbox(key=ui.CONDITION_KEYS["humidity"]).value == "humid"
        assert has_group(at, "match")
        assert at.button(key="chip_1").proto.type == "primary"  # shown as active
        assert at.button(key="chip_0").proto.type == "secondary"
        assert [w.value for w in at.warning] == []


class TestClear:
    def test_clear_resets_every_condition_and_filter(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        at.selectbox(key=ui.REGION_KEY).set_value("US-CT")
        for key, session_key in ui.CONDITION_KEYS.items():
            box = at.selectbox(key=session_key)
            box.set_value(ui.condition_options(key)[1])
        at.text_input(key=ui.TEXT_KEY).input("tomato")
        at.selectbox(key=ui.CROP_KEY).set_value("tomato")
        at.multiselect(key=ui.TOPICS_KEY).set_value(["flavor"])
        at.selectbox(key=ui.YEAR_KEY).set_value("2024")
        at.checkbox(key=ui.STRICT_KEY).check()
        at.selectbox(key=ui.REGION_FILTER_KEY).set_value("US-CT")
        at.run()
        assert at.session_state[ui.CONDITION_KEYS["zone"]] == "1"
        at.button(key="clear_all").click().run()
        assert not at.exception
        for key, default in ui.reset_values().items():
            assert at.session_state[key] == default, key
        assert subheaders(at)[-1] == "Reports (70)"
        assert [w.value for w in at.warning] == []  # no session-state API warnings

    def test_cleared_selects_show_their_first_load_labels(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        """F33: a select reset to ``None`` shows its placeholder, which must match first load."""
        at = run_app(monkeypatch, app_env)
        expected = {
            ui.REGION_KEY: "Not set",
            **dict.fromkeys(ui.CONDITION_KEYS.values(), "Not set"),
            ui.CROP_KEY: "Any crop",
            ui.YEAR_KEY: "Any year",
            ui.REGION_FILTER_KEY: "All states",
        }
        at.selectbox(key=ui.CONDITION_KEYS["zone"]).set_value("6b")
        at.button(key="clear_all").click().run()
        for key, label in expected.items():
            box = at.selectbox(key=key)
            assert box.value is None, key
            assert box.options[0] == label, key
            assert box.proto.placeholder == label, key


class TestFiltersAndEmptyStates:
    def test_year_filter_is_announced_and_can_be_reset(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        """F31: the year hides every report without that year, so the page says so."""
        at = run_app(monkeypatch, app_env)
        assert ui.year_filter_note("2024") not in infos(at)
        at.selectbox(key=ui.YEAR_KEY).set_value("2024").run()
        assert ui.year_filter_note("2024") in infos(at)
        assert at.button(key="clear_year_filter").label == "Any year"
        at.button(key="clear_year_filter").click().run()
        assert at.selectbox(key=ui.YEAR_KEY).value is None
        assert ui.year_filter_note("2024") not in infos(at)
        assert subheaders(at)[-1] == "Reports (70)"

    def test_strict_mode_that_hides_everything_says_so(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        """F30: the empty state names the checkbox instead of suggesting other searches."""
        at = select_garden(run_app(monkeypatch, app_env), zone="6b", method="container")
        at.text_input(key=ui.TEXT_KEY).input("Kennebec")  # states soil and region only
        at.checkbox(key=ui.STRICT_KEY).check().run()
        messages = infos(at)
        assert ui.NO_RESULTS not in messages
        assert (
            'The 1 report found is hidden by "Only show stated matches". Untick it to see it.'
            in messages
        )

    def test_strict_hiding_is_explained_once(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        """No report states comparable conditions: the limitation message already says where
        the reports are, so no second empty-state box repeats it."""
        at = select_garden(run_app(monkeypatch, app_env), method="container")
        at.text_input(key=ui.TEXT_KEY).input("thanks")
        at.checkbox(key=ui.STRICT_KEY).check().run()
        messages = infos(at)
        assert ui.NO_RESULTS not in messages
        hiding = [m for m in messages if '"Only show stated matches"' in m]
        assert len(hiding) == 1
        assert hiding[0].endswith("untick it to see them.")

    def test_plain_empty_state(self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv) -> None:
        at = run_app(monkeypatch, app_env)
        at.text_input(key=ui.TEXT_KEY).input("zzzzqqq").run()
        assert ui.NO_RESULTS in infos(at)

    def test_region_only_strict_note(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        """F42: Region is chosen in the conditions panel but cannot drive strict mode."""
        at = run_app(monkeypatch, app_env)
        at.selectbox(key=ui.REGION_KEY).set_value("US-CT")
        at.checkbox(key=ui.STRICT_KEY).check().run()
        assert any(
            t.startswith("Region only lists reports from your area first") for t in texts_of(at)
        )


class TestPaging:
    """F39: each group renders one page of cards, with a button for the next page."""

    def test_groups_render_one_page_at_a_time(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        config = _write_config(app_env.root, app_env.db_path, page_size=10, name="paged.yaml")
        at = run_app(monkeypatch, app_env, config=config)
        assert subheaders(at)[-1] == "Reports (70)"  # the heading keeps the full count
        assert len(shown_ids(group(at, "all"), app_env)) == 10
        assert at.button(key="more-all").label == "Show 10 more (60 not shown yet)"
        at.button(key="more-all").click().run()
        assert len(shown_ids(group(at, "all"), app_env)) == 20
        at.text_input(key=ui.TEXT_KEY).input("tomato").run()  # a new query starts at page one
        assert len(shown_ids(group(at, "all"), app_env)) == 10
        at.text_input(key=ui.TEXT_KEY).input("")
        at = select_garden(at, zone="6b", method="container")
        assert "Matching reported conditions (3)" in subheaders(at)
        assert len(shown_ids(group(at, "not_stated"), app_env)) == 10
        assert at.button(key="more-not_stated").label.startswith("Show 10 more")
        with pytest.raises(KeyError):
            at.button(key="more-match")  # a group that fits on one page has no button


class TestProvenanceLabels:
    """F16 / SPEC §7: cards and the map are labelled outside the collapsed Evidence."""

    def test_every_card_title_carries_the_community_report_badge(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        badge = ui.label_badge("community report")
        titles = [m.value for m in group(at, "all").markdown if m.value.endswith(f"** {badge}")]
        assert len(titles) == 70

    def test_stated_conditions_carry_the_reported_conditions_badge(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = select_garden(run_app(monkeypatch, app_env), zone="6b", method="container")
        badge = ui.label_badge("reported conditions")
        captions = [c.value for c in group(at, "match").caption if "Relevant" in c.value]
        assert len(captions) == 3
        assert all(caption.startswith(f"{badge} ") for caption in captions)

    def test_a_region_only_card_is_not_badged_and_is_called_not_comparable(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        """Validation regression: syn-0008 states only Maine, which is never compared."""
        at = select_garden(run_app(monkeypatch, app_env), zone="6b")
        at.text_input(key=ui.TEXT_KEY).input("visiting relatives").run()
        assert shown_ids(group(at, "not_stated"), app_env) == {"syn-0008"}
        captions = [c.value for c in group(at, "not_stated").caption if "Region" in c.value]
        assert captions == [ui.escape_markdown("Not stated: hardiness zone\nRegion: Maine")]
        assert (
            "The 1 report found does not mention comparable growing conditions, so it cannot be "
            "compared with your garden. It is shown below."
        ) in infos(at)

    def test_map_tab_is_labelled(self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv) -> None:
        at = run_app(monkeypatch, app_env)
        (map_tab,) = [tab for tab in at.tabs if tab.label == "Map"]
        badges = f"{ui.label_badge('community report')} {ui.label_badge('reported conditions')}"
        assert badges in [m.value for m in map_tab.markdown]


class TestMap:
    def test_state_selectbox_filters_reports(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        states = at.selectbox(key=ui.REGION_FILTER_KEY)
        assert states.options[0] == "All states"
        assert "Connecticut (4 reports)" in states.options
        states.set_value("US-CT").run()
        assert shown_ids(group(at, "all"), app_env) == set(CT_COMMENTS)
        assert ui.region_filter_note("US-CT") in [i.value for i in at.info]
        assert "Connecticut (4 reports)" in at.selectbox(key=ui.REGION_FILTER_KEY).options
        at.button(key="clear_region_filter").click().run()
        assert at.selectbox(key=ui.REGION_FILTER_KEY).value is None
        assert len(shown_ids(group(at, "all"), app_env)) == 70

    def test_map_notes_are_shown(self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv) -> None:
        at = run_app(monkeypatch, app_env)
        captions = texts_of(at)
        assert "Region not reported: 46" in captions
        assert "Outside the U.S.: 3" in captions

    def test_map_respects_other_filters(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        at.text_input(key=ui.TEXT_KEY).input("Juliet").run()
        options = at.selectbox(key=ui.REGION_FILTER_KEY).options
        # Juliet comments: syn-0006, 0013 and 0063 (Connecticut), 0041 (Georgia), 0044 (none).
        assert options == ["All states", "Connecticut (3 reports)", "Georgia (1 report)"]
        assert "Region not reported: 1" in texts_of(at)

    def test_map_is_remounted_without_a_stale_selection_when_the_filter_changes(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        assert ui.map_key(None) in at.session_state
        at.selectbox(key=ui.REGION_FILTER_KEY).set_value("US-CT").run()
        assert ui.map_key("US-CT") in at.session_state
        assert ui.map_key(None) not in at.session_state  # the old selection is gone
        at.button(key="clear_all").click().run()
        assert ui.map_key(None) in at.session_state
        assert ui.map_key("US-CT") not in at.session_state


class TestStaleFilters:
    def test_filter_values_that_vanish_from_the_data_are_reset(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv, tmp_path: Path
    ) -> None:
        """Deleting a source mid-session must not leave widgets holding impossible values."""
        db_copy = tmp_path / "viewer.sqlite"
        shutil.copyfile(app_env.db_path, db_copy)
        at = run_app(monkeypatch, app_env, db_path=db_copy)
        at.selectbox(key=ui.CROP_KEY).set_value("tomato")
        at.multiselect(key=ui.TOPICS_KEY).set_value(["flavor"])
        at.selectbox(key=ui.YEAR_KEY).set_value("2024")
        at.run()
        conn = db.connect(db_copy)
        try:
            db.delete_source(conn, "synthetic-demo")
        finally:
            conn.close()

        at.run()

        assert not at.exception
        assert at.session_state[ui.CROP_KEY] is None
        assert at.session_state[ui.TOPICS_KEY] == []
        assert at.session_state[ui.YEAR_KEY] is None
        assert at.selectbox(key=ui.CROP_KEY).options == ["Any crop"]


@pytest.fixture(scope="module")
def linked_env(app_env: AppEnv, tmp_path_factory: pytest.TempPathFactory) -> AppEnv:
    """The viewer database plus a cleared source with permalinks and a linked synthetic row."""
    root = tmp_path_factory.mktemp("linked")
    db_path = root / "linked.sqlite"
    shutil.copyfile(app_env.db_path, db_path)
    cleared = [
        {
            "comment_id": CLEARED_ID,
            "text": CLEARED_TEXT,
            "permalink": CLEARED_URL,
            "created_at": "2024-07-01T00:00:00Z",
        },
        {"comment_id": UNLINKED_ID, "text": UNLINKED_TEXT},
    ]
    fiction = [
        {
            "comment_id": SYNTH_LINK_ID,
            "text": SYNTH_LINK_TEXT,
            "permalink": "https://www.youtube.com/watch?v=fiction",
        }
    ]
    conn = db.connect(db_path)
    try:
        _import(conn, _write_jsonl(root / "cleared.jsonl", cleared), "cleared-test", "cleared")
        _import(conn, _write_jsonl(root / "fiction.jsonl", fiction), "fiction", "synthetic")
    finally:
        conn.close()
    texts = {**app_env.texts, CLEARED_ID: CLEARED_TEXT, UNLINKED_ID: UNLINKED_TEXT}
    texts[SYNTH_LINK_ID] = SYNTH_LINK_TEXT
    return AppEnv(root=root, config=_write_config(root, db_path), db_path=db_path, texts=texts)


class TestSourceLinks:
    """SPEC §11 #17 "opens source references", and F13 (fiction never links out)."""

    def test_cleared_permalink_is_a_source_button(
        self, monkeypatch: pytest.MonkeyPatch, linked_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, linked_env, db_path=linked_env.db_path)
        assert ui.MIXED_DEMO_BANNER in infos(at)
        assert link_buttons(at) == [("Source ↗", CLEARED_URL)]  # the only real permalink
        at.text_input(key=ui.TEXT_KEY).input("CLEAREDLINK").run()
        assert shown_ids(group(at, "all"), linked_env) == {CLEARED_ID}
        assert link_buttons(at) == [("Source ↗", CLEARED_URL)]
        assert ui.escape_markdown("Posted 2024-07-01") in texts_of(group(at, "all"))

    def test_cleared_comment_without_permalink_says_so(
        self, monkeypatch: pytest.MonkeyPatch, linked_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, linked_env, db_path=linked_env.db_path)
        at.text_input(key=ui.TEXT_KEY).input("CLEAREDNOLINK").run()
        assert link_buttons(at) == []
        assert ui.escape_markdown(ui.NO_LINK_NOTE) in texts_of(group(at, "all"))

    def test_synthetic_comment_with_a_permalink_stays_marked_and_unlinked(
        self, monkeypatch: pytest.MonkeyPatch, linked_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, linked_env, db_path=linked_env.db_path)
        at.text_input(key=ui.TEXT_KEY).input("SYNTHLINK").run()
        assert shown_ids(group(at, "all"), linked_env) == {SYNTH_LINK_ID}
        assert link_buttons(at) == []
        assert ui.escape_markdown(ui.SYNTHETIC_NOTE) in texts_of(group(at, "all"))


class TestViewerStartup:
    """F01 (private scope listens on loopback only) and F36 (no silently ignored options)."""

    @pytest.mark.parametrize("address", ["0.0.0.0", "192.168.1.20", "", None])
    def test_private_scope_refuses_a_network_address(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv, address: str | None
    ) -> None:
        at = run_app(monkeypatch, app_env, scope="private", address=address)
        assert not at.exception
        (error,) = at.error
        assert "only runs when the server listens on this computer alone" in error.value
        assert [block.value for block in at.code] == [ui.PRIVATE_COMMAND]
        assert "PRIVATEONLY" not in all_text(at)
        assert not at.text_input
        assert ui.PRIVATE_BANNER not in [w.value for w in at.warning]

    @pytest.mark.parametrize("address", ["localhost", "127.0.0.1", "::1"])
    def test_private_scope_runs_on_loopback(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv, address: str
    ) -> None:
        at = run_app(monkeypatch, app_env, scope="private", address=address)
        assert not at.error
        assert PRIVATE_ID in shown_ids(group(at, "all"), app_env)

    def test_public_scope_may_listen_on_the_network(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env, address="0.0.0.0")
        assert not at.error
        assert subheaders(at)[-1] == "Reports (70)"

    def test_importing_the_viewer_does_not_run_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``streamlit_app.py`` imports ``app``; only running it as ``__main__`` renders."""

        def refuse(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("app.main ran on import")

        monkeypatch.setattr(st, "set_page_config", refuse)
        namespace = runpy.run_path(str(APP), run_name="app")
        assert callable(namespace["main"])

    @pytest.mark.parametrize(
        "extra", [["--database", "other.sqlite"], ["--confg", "x.yaml"], ["--scope", "everyone"]]
    )
    def test_invalid_viewer_options_are_shown_and_stop_the_page(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv, extra: list[str]
    ) -> None:
        at = run_app(monkeypatch, app_env, extra_args=extra)
        assert not at.exception
        (error,) = at.error
        assert "Invalid viewer options" in error.value
        assert not at.text_input

    def test_unreadable_database_is_shown_and_stops_the_page(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv, tmp_path: Path
    ) -> None:
        bogus = tmp_path / "bogus.sqlite"
        bogus.write_bytes(b"this is not a database" * 100)
        at = run_app(monkeypatch, app_env, db_path=bogus)
        assert not at.exception
        (error,) = at.error
        assert "cannot be opened" in error.value
        assert not at.text_input


class TestRightsAndSafety:
    def test_private_source_never_appears_in_public_scope(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        at.text_input(key=ui.TEXT_KEY).input("PRIVATEONLY").run()
        assert "PRIVATEONLY" not in all_text(at)
        assert "0 reports" in texts_of(at)
        at.text_input(key=ui.TEXT_KEY).input("").run()
        text = all_text(at)
        assert "PRIVATEONLY" not in text
        assert "private-import" not in text
        assert "70 comments from 2 sources" in text
        assert "Connecticut (4 reports)" in at.selectbox(key=ui.REGION_FILTER_KEY).options
        at = select_garden(at, zone="6b", method="container")
        assert "Matching reported conditions (3)" in subheaders(at)

    def test_private_scope_shows_private_rows_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env, scope="private")
        assert ui.PRIVATE_BANNER in [w.value for w in at.warning]
        assert PRIVATE_ID in shown_ids(group(at, "all"), app_env)
        assert "Connecticut (5 reports)" in at.selectbox(key=ui.REGION_FILTER_KEY).options
        assert "71 comments from 3 sources" in all_text(at)

    def test_injection_comment_renders_literally(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        assert {INJECTION_ID, MARKDOWN_ID} <= shown_ids(group(at, "all"), app_env)
        for value in (element.value for element in at.markdown):
            assert not _UNESCAPED_LINK.search(value), value
            if "evil" in value:
                assert value == ui.escape_markdown(MARKDOWN_TEXT)
                assert bare_punctuation(value) == []
        at = select_garden(at, zone="6b", method="container")
        assert INJECTION_ID not in shown_ids(group(at, "match"), app_env)
        assert INJECTION_ID in shown_ids(group(at, "not_stated"), app_env)

    def test_missing_database_explains_how_to_build_it(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        missing = app_env.root / "nope" / "missing.sqlite"
        at = run_app(monkeypatch, app_env, db_path=missing)
        assert not at.exception
        (error,) = at.error
        assert "Run: uv run python -m crop_explorer demo" in error.value
        assert not missing.exists()
        assert not at.text_input


class TestContext:
    def test_outside_context_links_carry_their_labels(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        (panel,) = [e for e in at.expander if e.label == "Optional outside context"]
        text = "\n".join(texts_of(panel))
        cfg = load_config(app_env.config)
        for link in cfg.outside_context:
            assert ui.outside_link_markdown(link) in text
        assert "regional climate reference" in text
        assert "published external trial" in text
        assert ui.label_badge("community report") in text  # outside gardeners' own ratings

    def test_about_explains_provenance(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        (panel,) = [e for e in at.expander if e.label == "About / Sources"]
        text = "\n".join(texts_of(panel))
        assert "not variety quality" in text
        assert "synthetic demo data" in text
        assert "synthetic\\-demo" in text


# --------------------------------------------------------------------------- at a glance

GLANCE_VARIETY = "Aardvark Gold"
GLANCE_CROP = "rutabaga"
GLANCE_SECRETS = ("Aardvark", "aardvark", GLANCE_CROP, "PRIVATEGLANCE", "Hawaii", "glance-private")
QUALITY_WORDS = ("best", "top-rated", "recommend", "score", "%")
DEMO_TILES = (
    "Reports 70 comments in this view "
    "Varieties mentioned 44 across 8 crops "
    "Describe conditions 33 of 70 zone, method, heat… "
    "Places named 21 18 U.S. states · 3 outside the U.S."
)
"""The public test database: 69 synthetic fixtures plus one unextracted Markdown comment."""


def _glance_fact(key: str, written: str, value: str, quote: str) -> dict[str, str]:
    return {
        "key": key,
        "value_as_written": written,
        "normalized_value": value,
        "evidence_quote": quote,
        "applicability": "this_mention",
        "basis": "reported_garden_context",
    }


def _glance_private_rows(n: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``n`` private comments about a variety, crop and state that no public row mentions."""
    comments, extractions = [], []
    for i in range(n):
        comment_id = f"glance-private-{i}"
        text = f"PRIVATEGLANCE {i}: {GLANCE_VARIETY} {GLANCE_CROP} grew in pots in Hawaii."
        comments.append({"comment_id": comment_id, "text": text})
        mention = {
            "crop": GLANCE_CROP,
            "variety_as_written": GLANCE_VARIETY,
            "topics": ["flavor"],
            "evidence_quote": f"{GLANCE_VARIETY} {GLANCE_CROP}",
            "environment": [
                _glance_fact("method", "pots", "container", "grew in pots"),
                _glance_fact("region", "Hawaii", "US-HI", "in Hawaii"),
            ],
        }
        extractions.append(
            {
                "comment_id": comment_id,
                "extractor_version": "test-v1",
                "status": "parsed",
                "mentions": [mention],
            }
        )
    return comments, extractions


@pytest.fixture(scope="module")
def glance_env(
    app_env: AppEnv, tmp_path_factory: pytest.TempPathFactory, source_importer: Any
) -> AppEnv:
    """The viewer database plus three ``private_unverified`` comments with unique names."""
    root = tmp_path_factory.mktemp("glance")
    db_path = root / "glance.sqlite"
    shutil.copyfile(app_env.db_path, db_path)
    comments, extractions = _glance_private_rows(3)
    conn = db.connect(db_path)
    try:
        source_importer(
            conn, root, source_id="glance-private", comments=comments, extractions=extractions
        )
    finally:
        conn.close()
    return AppEnv(
        root=root, config=_write_config(root, db_path), db_path=db_path, texts=app_env.texts
    )


def glance_tabs(at: AppTest) -> list[Any]:
    tabs = [tab for tab in at.tabs if tab.label in ui.GLANCE_TABS]
    assert [tab.label for tab in tabs] == list(ui.GLANCE_TABS)
    return tabs


def glance_figures(at: AppTest) -> list[dict[str, Any]]:
    """The figure JSON of every chart in the "At a glance" tabs."""
    return [
        json.loads(chart.proto.spec)
        for tab in glance_tabs(at)
        for chart in tab.get("plotly_chart")
    ]


def tiles_markup(at: AppTest) -> str:
    (markup,) = [str(m.value) for m in at.markdown if "glance-kpis" in str(m.value)]
    return markup


def tile_text(markup: str) -> str:
    visible = re.sub(r"<style>.*?</style>", "", markup, flags=re.DOTALL)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", visible)).strip()


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def displayed_strings(figure: dict[str, Any]) -> list[str]:
    """What a reader of a figure can see: titles, tick labels, bar text, hover text, legends."""
    layout = figure["layout"]
    found = [a.get("text", "") for a in layout.get("annotations", [])]
    for name, axis in layout.items():
        if name.startswith(("xaxis", "yaxis")):
            found += list(_strings(axis.get("ticktext", [])))
    for trace in figure["data"]:
        found += [*_strings(trace.get("text", [])), *_strings(trace.get("hovertext", []))]
        found.append(trace.get("colorbar", {}).get("title", {}).get("text", ""))
    return found


def header_text(at: AppTest) -> str:
    """Every word the "At a glance" header shows: tiles, tab labels, captions and charts."""
    captions = [str(c.value) for tab in glance_tabs(at) for c in [*tab.caption, *tab.markdown]]
    figures = [s for figure in glance_figures(at) for s in displayed_strings(figure)]
    return " ".join(
        ["At a glance", tile_text(tiles_markup(at)), *ui.GLANCE_TABS, *captions, *figures]
    )


class TestAtAGlance:
    """The creator's overview at the top of the page: counts of comments, never quality."""

    def test_header_sits_after_the_banner_and_before_my_growing_conditions(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        values = [
            value if isinstance(value := getattr(node, "value", None), str) else None
            for node in walk(at.main)
        ]
        kinds = [type(node).__name__ for node in walk(at.main)]
        banner = values.index(ui.DEMO_BANNER)
        glance = values.index("At a glance")
        tiles = next(i for i, v in enumerate(values) if isinstance(v, str) and "glance-kpis" in v)
        first_tab = kinds.index("Tab")
        conditions = values.index("My growing conditions")
        assert banner < glance < tiles < first_tab < conditions
        assert [tab.label for tab in at.tabs][:3] == list(ui.GLANCE_TABS)

    def test_tiles_show_the_counts_of_the_visible_reports(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        markup = tiles_markup(at)
        assert tile_text(markup) == DEMO_TILES
        assert 'aria-valuenow="33"' in markup
        assert 'aria-valuemax="70"' in markup
        assert "Only 33 of 70 reports mention comparable growing conditions" in " ".join(infos(at))
        assert "State growing" not in markup  # never read as a U.S. state

    def test_each_tab_has_a_caption_a_chart_and_a_table(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        expected = ({"bar"}, {"heatmap", "scatter"}, {"bar"})
        for tab, types in zip(glance_tabs(at), expected, strict=True):
            (caption,) = tab.caption
            assert caption.value
            kinds = [getattr(node, "type", "") for node in walk(tab)][1:3]
            assert kinds == ["plotly_chart", "caption"], kinds  # the chart first, then its caption
            (chart,) = tab.get("plotly_chart")
            figure = json.loads(chart.proto.spec)
            assert {trace["type"] for trace in figure["data"]} == types
            assert json.loads(chart.proto.config)["displayModeBar"] is False
            (table,) = [e for e in tab.expander if e.label == ui.TABLE_LABEL]
            (frame,) = table.dataframe
            assert not frame.value.empty

    def test_the_variety_chart_and_captions_on_the_demo(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        varieties, topics, setups = glance_tabs(at)
        caption = varieties.caption[0].value
        assert caption.startswith("Comments that mention each variety: top 10 of 44.")
        assert "This counts conversation, not quality" in caption
        assert "Click a bar to show its comments below." in caption
        assert "ties are listed" not in caption  # the demo's cut falls between 2 and 1
        assert "the 8 most-discussed varieties" in topics.caption[0].value
        assert "not whether it went well" in topics.caption[0].value
        assert setups.caption[0].value.startswith(
            "Only reports that state a growing method or setting are counted: 20 of 70. Most "
            "don't say."
        )
        (bars,) = [t for t in glance_figures(at)[0]["data"] if t.get("hoverinfo") == "skip"]
        assert bars["x"][:3] == [10, 5, 5]
        ticks = glance_figures(at)[0]["layout"]["yaxis"]["ticktext"]
        assert ticks[:3] == ["Sungold · tomato", "Cherokee Purple · tomato", "Juliet · tomato"]
        rows = glance_figures(at)[1]["layout"]["yaxis"]["ticktext"]
        assert rows == [tick.split(" · ")[0] for tick in ticks[:8]]

    def test_only_the_variety_and_topic_charts_are_clickable(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        modes = [list(tab.get("plotly_chart")[0].proto.selection_mode) for tab in glance_tabs(at)]
        assert [bool(mode) for mode in modes] == [True, True, False]

    def test_charts_remount_when_a_click_sets_the_variety_filter(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        before = ui.glance_key("varieties", {})
        assert before in at.session_state
        at.session_state[ui.VARIETY_FILTER_KEY] = "juliet"
        at.run()
        assert before not in at.session_state
        assert ui.glance_key("varieties", {ui.VARIETY_FILTER_KEY: "juliet"}) in at.session_state

    def test_no_quality_words_anywhere_in_the_header(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        text = header_text(at).casefold()
        assert "sungold" in text
        assert "greenhouse" in text
        for word in QUALITY_WORDS:
            assert word not in text, word
        assert "%" not in tiles_markup(at)

    def test_private_rows_never_reach_the_public_header(
        self, monkeypatch: pytest.MonkeyPatch, glance_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, glance_env)
        markup = tiles_markup(at)
        figures = json.dumps(glance_figures(at), ensure_ascii=False)
        tables = " ".join(frame.value.to_string() for frame in at.dataframe)
        for secret in GLANCE_SECRETS:
            assert secret not in markup, secret
            assert secret not in figures, secret
            assert secret not in tables, secret
        page = all_text(at)  # every region is a Region option, so Hawaii is checked below
        assert not [secret for secret in GLANCE_SECRETS if secret != "Hawaii" and secret in page]
        reported = at.selectbox(key=ui.REGION_FILTER_KEY).options
        assert not [option for option in reported if "Hawaii" in option]
        assert tile_text(markup) == DEMO_TILES

    def test_the_same_rows_do_show_in_the_private_view(
        self, monkeypatch: pytest.MonkeyPatch, glance_env: AppEnv
    ) -> None:
        """Proves the public check above looks at the right places."""
        at = run_app(monkeypatch, glance_env, scope="private")
        figures = json.dumps(glance_figures(at), ensure_ascii=False)
        assert f"{GLANCE_VARIETY} · {GLANCE_CROP}" in figures
        text = tile_text(tiles_markup(at))
        assert "Varieties mentioned 45 " in text
        assert "Places named 22 19 U.S. states" in text

    def test_untrusted_names_are_escaped_in_the_figures(
        self,
        monkeypatch: pytest.MonkeyPatch,
        app_env: AppEnv,
        tmp_path: Path,
        source_importer: Any,
    ) -> None:
        db_copy = tmp_path / "evil.sqlite"
        shutil.copyfile(app_env.db_path, db_copy)
        evil = "<b>Bold</b> Beauty"
        text = f"EVILNAME {evil} tomato was lovely."
        line = {
            "comment_id": "evil-0001",
            "extractor_version": "test-v1",
            "status": "parsed",
            "mentions": [
                {
                    "crop": "tomato",
                    "variety_as_written": evil,
                    "topics": ["flavor"],
                    "evidence_quote": f"{evil} tomato",
                    "environment": [],
                }
            ],
        }
        conn = db.connect(db_copy)
        try:
            source_importer(
                conn,
                tmp_path,
                source_id="evil",
                comments=[{"comment_id": "evil-0001", "text": text}],
                extractions=[line],
                rights_status="synthetic",
            )
        finally:
            conn.close()
        config = _write_config(tmp_path, db_copy, name="evil.yaml")
        raw = yaml.safe_load(config.read_text(encoding="utf-8"))
        raw["overview"] = {"top_varieties": 50, "heatmap_varieties": 50}
        config.write_text(yaml.safe_dump(raw), encoding="utf-8")
        at = run_app(monkeypatch, app_env, db_path=db_copy, config=config)
        figures = json.dumps(glance_figures(at), ensure_ascii=False)
        assert "&lt;b&gt;Bold&lt;/b&gt; Beauty" in figures
        assert "<b>Bold" not in figures

    def test_an_empty_database_shows_zero_tiles_and_no_charts(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv, tmp_path: Path
    ) -> None:
        empty = tmp_path / "empty.sqlite"
        db.connect(empty).close()
        at = run_app(monkeypatch, app_env, db_path=empty)
        assert not at.exception
        assert tile_text(tiles_markup(at)).startswith("Reports 0 ")
        assert [tab.label for tab in at.tabs] == ["Comments", "Map"]

    def test_unanalysed_comments_explain_the_empty_charts(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv, tmp_path: Path
    ) -> None:
        bare = tmp_path / "bare.sqlite"
        conn = db.connect(bare)
        try:
            rows = [{"comment_id": MARKDOWN_ID, "text": MARKDOWN_TEXT}]
            _import(conn, _write_jsonl(tmp_path / "md.jsonl", rows), "markdown-test", "synthetic")
        finally:
            conn.close()
        at = run_app(monkeypatch, app_env, db_path=bare)
        assert not at.exception
        captions = [tab.caption[0].value for tab in glance_tabs(at)]
        assert captions == [ui.NO_VARIETIES, ui.NO_TOPICS, ui.NO_SETUPS]
        assert glance_figures(at) == []


def bar_counts(at: AppTest) -> list[tuple[str, int]]:
    """``(variety_key, count)`` of every bar, read from the figure the viewer drew."""
    figure = glance_figures(at)[0]
    (layer,) = [t for t in figure["data"] if t.get("hoverinfo") != "skip"]
    keys = [html.unescape(row[0]) for row in layer["customdata"]]
    (painted,) = [t for t in figure["data"] if t.get("hoverinfo") == "skip"]
    return list(zip(keys, painted["x"], strict=True))


def show_variety(at: AppTest, key: str, topics: Sequence[str] = ()) -> AppTest:
    """Apply what a chart click applies (``ui.*_click_updates``), then run."""
    updates = (
        ui.cell_click_updates({"selection": {"points": [{"customdata": [key, *topics]}]}}, {key})
        if topics
        else ui.variety_click_updates({"selection": {"points": [{"customdata": [key]}]}}, {key})
    )
    assert updates
    for name, value in updates.items():
        at.session_state[name] = value
    return at.run()


class TestGlanceClickThrough:
    """A click shows exactly the comments the mark counted, says so, and is easy to undo."""

    def test_every_bar_shows_exactly_the_reports_it_counts(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        bars = bar_counts(at)
        assert len(bars) == 10
        for key, count in bars:
            show_variety(at, key)
            assert not at.exception
            assert f"Reports ({count})" in subheaders(at), key

    def test_a_text_search_would_find_more_than_the_bar(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        """Why a click filters by variety key: "Sungold" as text also finds syn-0020."""
        at = run_app(monkeypatch, app_env)
        assert ("sungold", 10) in bar_counts(at)
        at.text_input(key=ui.TEXT_KEY).input("Sungold").run()
        assert "Reports (11)" in subheaders(at)
        show_variety(at, "sungold")
        assert at.session_state[ui.TEXT_KEY] == ""
        assert "Reports (10)" in subheaders(at)
        assert INJECTION_ID not in shown_ids(at, app_env)

    def test_a_cell_shows_exactly_the_reports_it_counts(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        grid = glance_figures(at)[1]
        (cells,) = [t for t in grid["data"] if t["type"] == "scatter"]
        (heat,) = [t for t in grid["data"] if t["type"] == "heatmap"]
        counts = [n for row in heat["z"] for n in row]
        picked = [(c, n) for c, n in zip(cells["customdata"], counts, strict=True) if n][:4]
        for (key, topic), count in picked:
            show_variety(at, html.unescape(key), [topic])
            assert f"Reports ({count})" in subheaders(at), (key, topic)
            assert at.session_state[ui.TOPICS_KEY] == [topic]

    def test_feedback_above_the_charts_names_what_is_shown_and_links_down(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        assert not [m for tab in glance_tabs(at) for m in tab.markdown]  # nothing clicked yet
        show_variety(at, "sungold", ["flavor"])
        expected = ui.glance_feedback("Sungold · tomato", ["flavor"])
        varieties, topics, setups = glance_tabs(at)
        assert [m.value for m in varieties.markdown] == [expected]
        assert [m.value for m in topics.markdown] == [expected]
        assert not setups.markdown
        for tab in (varieties, topics):  # the feedback right above the chart
            kinds = [getattr(node, "type", "") for node in walk(tab)][1:3]
            assert kinds == ["markdown", "plotly_chart"], kinds
        assert "about Flavor below" in expected
        assert at.multiselect(key=ui.TOPICS_KEY).value == ["flavor"]
        assert "Flavor" in at.multiselect(key=ui.TOPICS_KEY).options  # one label for a topic

    def test_the_link_target_sits_just_above_the_results(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        nodes = list(walk(at.main))
        values = [v if isinstance(v := getattr(n, "value", None), str) else None for n in nodes]
        anchor = values.index(ui.RESULTS_ANCHOR_HTML)
        comments = next(
            i for i, n in enumerate(nodes) if type(n).__name__ == "Tab" and n.label == "Comments"
        )
        strict = next(i for i, n in enumerate(nodes) if getattr(n, "key", "") == ui.STRICT_KEY)
        assert strict < anchor < comments

    def test_the_filter_note_names_the_variety_and_resets_it(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        show_variety(at, "earlygirl")
        assert ui.variety_filter_note("Early Girl · tomato") in infos(at)
        assert "Reports (2)" in subheaders(at)
        at.button(key="clear_variety_filter").click().run()
        assert at.session_state[ui.VARIETY_FILTER_KEY] is None
        assert "Reports (70)" in subheaders(at)
        assert not [m for tab in glance_tabs(at) for m in tab.markdown]

    def test_clear_resets_the_variety_filter(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        show_variety(at, "juliet", ["flavor"])
        at.button(key="clear_all").click().run()
        assert at.session_state[ui.VARIETY_FILTER_KEY] is None
        assert at.session_state[ui.TOPICS_KEY] == []
        assert "Reports (70)" in subheaders(at)

    def test_a_click_keeps_the_garden(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        select_garden(at, zone="6b").run()
        show_variety(at, "sungold")
        assert at.session_state[ui.CONDITION_KEYS["zone"]] == "6b"
        assert not has_group(at, "all")  # still grouped by the garden

    def test_a_stale_variety_filter_is_dropped(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        at.session_state[ui.VARIETY_FILTER_KEY] = "nosuchvariety"
        at.run()
        assert not at.exception
        assert at.session_state[ui.VARIETY_FILTER_KEY] is None
        assert "Reports (70)" in subheaders(at)

    def test_a_private_variety_cannot_be_shown_in_the_public_view(
        self, monkeypatch: pytest.MonkeyPatch, glance_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, glance_env)
        at.session_state[ui.VARIETY_FILTER_KEY] = "aardvarkgold"
        at.run()
        assert at.session_state[ui.VARIETY_FILTER_KEY] is None
        assert not [s for s in GLANCE_SECRETS if s != "Hawaii" and s in all_text(at)]


# --------------------------------------------------------------------------- climate overlays


def map_tab(at: AppTest) -> Any:
    (tab,) = [tab for tab in at.tabs if tab.label == "Map"]
    return tab


def map_figure(at: AppTest) -> dict[str, Any]:
    (chart,) = map_tab(at).get("plotly_chart")
    figure: dict[str, Any] = json.loads(chart.proto.spec)
    return figure


def show_overlay(at: AppTest, key: str) -> AppTest:
    return at.radio(key=ui.OVERLAY_KEY).set_value(key).run()


def count_labels(at: AppTest) -> dict[str, str]:
    """The count label written on each state of an overlay map (postal code -> text)."""
    (labels,) = [t for t in map_figure(at)["data"] if t["type"] == "scattergeo"]
    assert "lat" not in labels and "lon" not in labels  # placed from the state code only
    return dict(zip(labels["locations"], labels["text"], strict=True))


def selector_counts(at: AppTest) -> dict[str, str]:
    """``postal -> count`` read from the "Filter by reported state" options."""
    found = {}
    for option in at.selectbox(key=ui.REGION_FILTER_KEY).options[1:]:
        name, count = re.fullmatch(r"(.+) \((\d+) reports?\)", option).groups()  # type: ignore[union-attr]
        (code,) = [c for c, n in US_STATES.items() if n == name]
        found[code.removeprefix("US-")] = count
    return found


def card_texts(at: AppTest) -> str:
    """Every word on every result card (all groups, expanded or not)."""
    keys = [f"group-{key}" for key in ("all", "match", "partial", "not_stated", "different")]
    return " ".join(
        text
        for key in keys
        if has_group(at, key.removeprefix("group-"))
        for text in texts_of(at.main.get_by_key(key))
    )


def regional_box(at: AppTest) -> Any:
    return at.main.get_by_key("regional-reference")


def has_regional_box(at: AppTest) -> bool:
    try:
        regional_box(at)
    except KeyError:
        return False
    return True


def has_highlight(at: AppTest) -> bool:
    try:
        at.selectbox(key=ui.HIGHLIGHT_ZONE_KEY)
    except KeyError:
        return False
    return True


def result_snapshot(at: AppTest, env: AppEnv) -> tuple[list[str], set[str]]:
    """What the result list shows: its "reports" captions and the full matches."""
    captions = [c.value for c in at.caption if "reports" in c.value]
    return captions[:1], shown_ids(group(at, "match"), env)


class TestClimateOverlays:
    """SPEC §6-§7: optional regional climate references on the map, off by default."""

    def test_the_intro_says_zone_only_reports_are_never_placed_on_the_map(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        """With the zone overlay on screen, "zones are never mapped" would contradict it."""
        for key in ("none", "zones"):
            at = show_overlay(run_app(monkeypatch, app_env), key)
            (intro,) = [
                c.value
                for c in map_tab(at).caption
                if c.value.startswith("Where gardeners explicitly said they grew")
            ]
            assert "Reports that give only a hardiness zone are never placed on the map" in intro
            assert "no point locations are shown" in intro
            assert "Zones are never mapped" not in all_text(at)

    def test_the_overlay_is_off_by_default_and_the_map_is_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        picker = at.radio(key=ui.OVERLAY_KEY)
        assert picker.value == "none"
        assert picker.label == "Overlay"
        assert picker.options == list(overlay_charts.OVERLAY_LABELS.values())
        figure = map_figure(at)
        assert [trace["type"] for trace in figure["data"]] == ["choropleth"]
        assert figure["data"][0]["colorbar"]["title"]["text"] == "Reports"
        assert figure["layout"]["height"] == load_config(app_env.config).map.height_px
        assert not has_highlight(at)
        tab_text = "\n".join(texts_of(map_tab(at)))
        assert ui.label_badge(OVERLAY_LABEL) not in tab_text
        assert ZONE_DISCLAIMER not in all_text(at)
        assert ui.map_key(None) in at.session_state

    @pytest.mark.parametrize("key", list(overlay_charts.OVERLAY_KEYS))
    def test_every_map_loads_its_base_map_from_the_app_not_the_cdn(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv, key: str
    ) -> None:
        at = show_overlay(run_app(monkeypatch, app_env), key)
        (chart,) = map_tab(at).get("plotly_chart")
        config = json.loads(chart.proto.config)
        assert config["topojsonURL"] == ui.BASE_MAP_URL
        assert config["displayModeBar"] is False
        assert config["scrollZoom"] is False

    def test_the_zone_layer_says_what_it_is_and_is_not(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = show_overlay(run_app(monkeypatch, app_env), "zones")
        tab = map_tab(at)
        markdown = [m.value for m in tab.markdown]
        assert any(m.endswith(ui.label_badge(OVERLAY_LABEL)) for m in markdown)
        assert "**USDA hardiness zones (2023)**" in markdown
        # Prominent above the map, and again with the table twin, so it sits with the data.
        assert [i.value for i in tab.info] == [ZONE_DISCLAIMER, ZONE_DISCLAIMER]
        caption = overlay_charts.overlay_view("zones").caption
        assert caption in [c.value for c in tab.caption]
        assert ZONE_SOURCE in caption and ZONE_PERIOD in caption
        traces = map_figure(at)["data"]
        assert [t["type"] for t in traces] == ["choropleth", "choropleth", "scattergeo"]
        assert traces[0]["featureidkey"] == "properties.zone"
        assert traces[0]["geojson"] == overlay_charts.ZONE_LAYER_URL  # fetched once, cached
        assert map_figure(at)["layout"]["height"] == load_config(app_env.config).map.height_px
        assert "logo" not in all_text(at).lower()

    def test_the_zone_layer_has_a_table_twin(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = show_overlay(run_app(monkeypatch, app_env), "zones")
        (table,) = [e for e in map_tab(at).expander if e.label == ui.TABLE_LABEL]
        assert [i.value for i in table.info] == [ZONE_DISCLAIMER]  # with the data
        states, shares, key = (frame.value for frame in table.dataframe)
        assert list(states.columns) == ["State", "Main zones by area", "Largest area"]
        assert len(states) == 51
        assert states.set_index("State").loc["Connecticut", "Main zones by area"] == "6a-7b"
        assert list(shares.columns) == ["State", "Zone", "Share of the state's area (%)"]
        assert shares["Share of the state's area (%)"].dtype == "float64"
        assert list(key.columns) == ["Zone", "Average coldest winter night (°F)"]
        assert [c.value for c in table.caption] == [
            title for title, _ in overlay_charts.overlay_tables("zones", overlays.load_overlays())
        ]

    @pytest.mark.parametrize("key", ["july_avg_high_f", "jan_avg_low_f", "annual_precip_in"])
    def test_each_noaa_layer_says_what_it_is_and_is_not(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv, key: str
    ) -> None:
        at = show_overlay(run_app(monkeypatch, app_env), key)
        tab = map_tab(at)
        view = overlay_charts.overlay_view(key)  # type: ignore[arg-type]
        assert f"**{view.title}**" in [m.value for m in tab.markdown]
        captions = [c.value for c in tab.caption]
        assert view.caption in captions
        note = overlay_charts.scale_note(key, overlays.load_overlays())  # type: ignore[arg-type]
        assert note is not None and note in captions  # who is off the contiguous-state scale
        assert NORMALS_SOURCE in view.caption
        assert not tab.info  # no zone disclaimer on a NOAA layer
        assert ZONE_DISCLAIMER not in all_text(at)
        traces = map_figure(at)["data"]
        assert [t["type"] for t in traces] == ["choropleth", "scattergeo"]
        assert traces[0]["locationmode"] == "USA-states"
        (table,) = [e for e in tab.expander if e.label == ui.TABLE_LABEL]
        (frame,) = table.dataframe
        assert frame.value.columns[0] == "State"
        assert len(frame.value) == 50

    def test_count_labels_are_the_maps_own_public_counts(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        """A private CT comment exists, but the public overlay never counts it."""
        public = show_overlay(run_app(monkeypatch, app_env), "zones")
        labels = count_labels(public)
        assert labels == selector_counts(public)
        assert labels["CT"] == "4"
        private = show_overlay(run_app(monkeypatch, app_env, scope="private"), "zones")
        assert count_labels(private)["CT"] == "5"  # the private Hartford comment
        assert {k: v for k, v in count_labels(private).items() if k != "CT"} == {
            k: v for k, v in labels.items() if k != "CT"
        }

    def test_count_labels_follow_the_other_filters(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = show_overlay(run_app(monkeypatch, app_env), "july_avg_high_f")
        at.text_input(key=ui.TEXT_KEY).input("Juliet").run()
        assert count_labels(at) == {"CT": "3", "GA": "1"}

    def test_the_state_filter_works_with_an_overlay(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = show_overlay(run_app(monkeypatch, app_env), "jan_avg_low_f")
        at.selectbox(key=ui.REGION_FILTER_KEY).set_value("US-CT").run()
        assert shown_ids(group(at, "all"), app_env) == set(CT_COMMENTS)
        assert ui.map_key("US-CT", "jan_avg_low_f") in at.session_state
        assert count_labels(at)["CT"] == "4"  # the map stays usable as the selector
        assert at.radio(key=ui.OVERLAY_KEY).value == "jan_avg_low_f"

    def test_the_zone_emphasis_follows_the_viewers_zone(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = show_overlay(run_app(monkeypatch, app_env), "zones")
        select_garden(at, zone="6b")
        assert "**Where zone 6b is (2023 USDA zones)**" in [m.value for m in map_tab(at).markdown]
        assert "<b>6b</b>" in map_figure(at)["data"][0]["colorbar"]["ticktext"]
        select_garden(at, zone="5a")
        assert "**Where zone 5a is (2023 USDA zones)**" in [m.value for m in map_tab(at).markdown]
        ticks = map_figure(at)["data"][0]["colorbar"]["ticktext"]
        assert "<b>5a</b>" in ticks and "<b>6b</b>" not in ticks
        at.button(key="clear_all").click().run()
        assert "**USDA hardiness zones (2023)**" in [m.value for m in map_tab(at).markdown]
        assert at.radio(key=ui.OVERLAY_KEY).value == "zones"  # a view option, not a filter

    def test_highlight_a_zone_shows_where_it_is_without_setting_a_condition(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        """A view option next to the overlay picker: never the viewer's zone, never matching."""
        at = select_garden(run_app(monkeypatch, app_env), method="container")
        before = result_snapshot(at, app_env)
        show_overlay(at, "zones")
        box = at.selectbox(key=ui.HIGHLIGHT_ZONE_KEY)
        assert box.label == "Highlight a zone"
        assert box.value is None
        assert "never changes which reports are shown" in (box.help or "")
        box.set_value("5a").run()
        assert "**Where zone 5a is (2023 USDA zones)**" in [m.value for m in map_tab(at).markdown]
        assert "<b>5a</b>" in map_figure(at)["data"][0]["colorbar"]["ticktext"]
        assert at.selectbox(key=ui.CONDITION_KEYS["zone"]).value is None
        assert result_snapshot(at, app_env) == before
        at.button(key="clear_all").click().run()
        assert at.selectbox(key=ui.HIGHLIGHT_ZONE_KEY).value == "5a"  # a view option

    def test_the_highlight_wins_over_the_viewers_zone_and_falls_back_to_it(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = show_overlay(select_garden(run_app(monkeypatch, app_env), zone="6b"), "zones")
        assert at.selectbox(key=ui.HIGHLIGHT_ZONE_KEY).proto.placeholder == "Your zone (6b)"
        assert "**Where zone 6b is (2023 USDA zones)**" in [m.value for m in map_tab(at).markdown]
        at.selectbox(key=ui.HIGHLIGHT_ZONE_KEY).set_value("9").run()
        assert "**Where zone 9 is (2023 USDA zones)**" in [m.value for m in map_tab(at).markdown]
        assert at.selectbox(key=ui.CONDITION_KEYS["zone"]).value == "6b"
        at.selectbox(key=ui.HIGHLIGHT_ZONE_KEY).set_value(None).run()
        assert "**Where zone 6b is (2023 USDA zones)**" in [m.value for m in map_tab(at).markdown]

    @pytest.mark.parametrize("key", ["none", "july_avg_high_f"])
    def test_the_highlight_is_only_offered_on_the_zone_layer(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv, key: str
    ) -> None:
        at = show_overlay(run_app(monkeypatch, app_env), key)
        assert not has_highlight(at)

    def test_overlays_never_change_the_results(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        base = select_garden(run_app(monkeypatch, app_env), zone="6b", method="container")
        before = [c.value for c in base.caption if "reports" in c.value]
        for key in overlay_charts.OVERLAY_KEYS[1:]:
            show_overlay(base, key)
            assert [c.value for c in base.caption if "reports" in c.value][:1] == before[:1]
            assert shown_ids(group(base, "match"), app_env) == set(FULL_MATCHES)


class TestBrokenReferences:
    """Corrupt reference files fail loudly where they would show; the rest keeps working."""

    @pytest.fixture
    def broken(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        def fail() -> None:
            raise OverlayDataError("phzm_2023_simplified.geojson: features must be distinct")

        st.cache_resource.clear()
        monkeypatch.setattr(overlays, "load_overlays", fail)
        yield
        st.cache_resource.clear()

    def test_errors_are_shown_and_the_count_map_and_reports_stay(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv, broken: None
    ) -> None:
        at = run_app(monkeypatch, app_env)
        at.selectbox(key=ui.REGION_KEY).set_value("US-CT").run()
        show_overlay(at, "zones")
        assert not at.exception
        errors = [e.value for e in at.error]
        assert len(errors) == 2  # the regional panel and the map
        assert all("features must be distinct" in e for e in errors)
        assert not has_regional_box(at)
        assert [t["type"] for t in map_figure(at)["data"]] == ["choropleth"]  # the count map
        assert ui.label_badge(OVERLAY_LABEL) not in "\n".join(texts_of(map_tab(at)))
        assert "Reports (70)" in subheaders(at)  # the reports are all still listed


class TestUnservedZoneLayer:
    """Browsers fetch the zone layer from the app; a missing or stale copy fails loudly."""

    def test_a_missing_served_copy_is_an_error_not_an_empty_map(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        def missing(path: Path) -> None:
            raise OverlayDataError(f"The browser copy of the zone layer is missing: {path.name}")

        st.cache_resource.clear()
        monkeypatch.setattr(overlays, "check_served_copy", missing)
        try:
            at = show_overlay(run_app(monkeypatch, app_env), "zones")
            assert not at.exception
            (error,) = [e.value for e in at.error]
            assert "browser copy of the zone layer is missing" in error
            assert [t["type"] for t in map_figure(at)["data"]] == ["choropleth"]
        finally:
            st.cache_resource.clear()


class TestRegionalReference:
    """The viewer's chosen Region described by the references: never a card, never matching."""

    def test_nothing_without_a_region(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        assert not has_regional_box(at)

    def test_a_us_state_gets_zones_and_averages_with_the_disclaimer(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        at.selectbox(key=ui.REGION_KEY).set_value("US-CT").run()
        box = regional_box(at)
        (summary,) = [m.value for m in box.markdown]
        assert summary.startswith(
            f"{ui.label_badge(OVERLAY_LABEL)} Connecticut — main USDA zones by area 6a-7b"
        )
        assert "July average high 83°F" in summary
        assert [i.value for i in box.info] == [ZONE_DISCLAIMER]  # prominent, not a caption
        (notes,) = [c.value for c in box.caption]
        assert notes.startswith("Main zones: the half-zones covering at least 0.1%")
        assert "look up a ZIP code at planthardiness.ars.usda.gov" in notes
        assert "never used to sort or match reports" in notes

    def test_it_sits_under_the_conditions_and_never_on_a_card(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        at.selectbox(key=ui.REGION_KEY).set_value("US-CT").run()
        select_garden(at, zone="6b")
        nodes = list(walk(at.main))
        where = next(
            i for i, n in enumerate(nodes) if getattr(n, "key", "") == "regional-reference"
        )
        zone = next(
            i for i, n in enumerate(nodes) if getattr(n, "key", "") == ui.CONDITION_KEYS["zone"]
        )
        search_box = next(i for i, n in enumerate(nodes) if getattr(n, "key", "") == ui.TEXT_KEY)
        assert zone < where < search_box
        cards = card_texts(at)
        for leak in ("6a-7b", "83°F", OVERLAY_LABEL, "NOAA", "Simplified from"):
            assert leak not in cards

    def test_the_region_choice_groups_exactly_as_before(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        """The panel is display only: the results equal a search with the same query."""
        at = run_app(monkeypatch, app_env)
        at.selectbox(key=ui.REGION_KEY).set_value("US-CT").run()
        select_garden(at, zone="6b")
        state = {**ui.reset_values(), ui.REGION_KEY: "US-CT", ui.CONDITION_KEYS["zone"]: "6b"}
        query = ui.query_from_state(state, max_query_chars=200)
        conn = db.connect(app_env.db_path)
        try:
            expected = search.search(conn, "public", query, NOW)
        finally:
            conn.close()
        assert ui.count_summary(expected) in [c.value for c in at.caption]

    def test_dc_and_outside_the_us_say_what_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        at.selectbox(key=ui.REGION_KEY).set_value("US-DC").run()
        (summary,) = [m.value for m in regional_box(at).markdown]
        assert summary.endswith(
            "NOAA statewide averages are not available for District of Columbia."
        )
        at.selectbox(key=ui.REGION_KEY).set_value("CA-ON").run()
        (summary,) = [m.value for m in regional_box(at).markdown]
        assert "No regional climate reference for Ontario" in summary
        assert not regional_box(at).caption
        assert not regional_box(at).info

    def test_clear_removes_it(self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv) -> None:
        at = run_app(monkeypatch, app_env)
        at.selectbox(key=ui.REGION_KEY).set_value("US-CT").run()
        at.button(key="clear_all").click().run()
        assert not has_regional_box(at)


class TestOutsideContextOverlays:
    def test_climate_rows_point_to_the_map_overlays_and_trials_stay_links(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        panel = expander(at, "Optional outside context")
        captions = [c.value for c in panel.caption]
        cfg = load_config(app_env.config)
        overlays_by_title = {link.title: link.overlay for link in cfg.outside_context}
        assert overlays_by_title["USDA Plant Hardiness Zone Map"] == "zones"
        assert overlays_by_title["NOAA U.S. Climate Normals (1991-2020)"] == "normals"
        assert overlays_by_title["Cornell Vegetable Varieties for Gardeners"] is None
        assert overlays_by_title["Nebraska Vegetable Variety Navigator"] is None
        notes = [c for c in captions if c.startswith("Also available as a map overlay")]
        assert len(notes) == 2
        for link in cfg.outside_context:
            assert ui.outside_link_markdown(link) in [
                m.value.split(" :")[0] for m in panel.markdown
            ]
