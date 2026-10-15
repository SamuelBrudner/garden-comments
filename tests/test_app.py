"""End-to-end tests of the Streamlit viewer (``app.py``) with ``streamlit.testing.v1.AppTest``.

A temporary database is built with the real ``db``, ``ingest`` and ``extraction`` modules: the
synthetic fixtures (public demo data), one extra synthetic comment full of Markdown/HTML, and one
``private_unverified`` comment with rich, matching extracted facts. Covers SPEC §2 (Browse screen,
grouping, strict toggle, chips), §6 (state filter and map notes), §9/§11 #15 (rights isolation:
nothing private reaches the public view), §11 #1 (text search finds unextracted comments), #16
(limitation message) and #17 (inputs exist and Clear resets them), and that untrusted comment text
renders literally.
"""

from __future__ import annotations

import json
import re
import shutil
import string
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from loguru import logger
from streamlit.testing.v1 import AppTest

from crop_explorer import db, ui
from crop_explorer.config import DEFAULT_CONFIG_PATH, load_config
from crop_explorer.extraction import load_extractions
from crop_explorer.ingest import build_source, import_file
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
    def fact(key: str, written: str, quote: str) -> dict[str, str]:
        return {
            "key": key,
            "value_as_written": written,
            "evidence_quote": quote,
            "applicability": "this_mention",
            "basis": "reported_garden_context",
        }

    return {
        "comment_id": PRIVATE_ID,
        "extractor_version": "test-v1",
        "status": "parsed",
        "mentions": [
            {
                "crop": "tomato",
                "variety_as_written": "Brandywine",
                "topics": ["growing_method"],
                "evidence_quote": "Brandywine grown in containers",
                "environment": [
                    fact("method", "containers", "grown in containers"),
                    fact("zone", "zone 6b", "in zone 6b"),
                    fact("region", "Hartford, CT", "near Hartford, CT"),
                ],
            }
        ],
    }


def _write_config(root: Path, db_path: Path) -> Path:
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    raw["database"]["path"] = str(db_path)
    raw["logging"] = {"level": "WARNING", "json_path": None}
    config = root / "config.yaml"
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


def run_app(
    monkeypatch: pytest.MonkeyPatch,
    env: AppEnv,
    *,
    scope: str = "public",
    db_path: Path | None = None,
) -> AppTest:
    argv = ["app.py", "--config", str(env.config), "--db", str(db_path or env.db_path)]
    monkeypatch.setattr(sys, "argv", [*argv, "--scope", scope])
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
        assert "Only 35 of 70 reports mention growing conditions." in [i.value for i in at.info]

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
            "Growing year",
            "Crop",
            "Filter by reported state",
        } <= labels
        assert at.multiselect(key=ui.TOPICS_KEY).label == "Topics"
        assert at.checkbox(key=ui.STRICT_KEY).label == "Only show stated matches"
        assert [tab.label for tab in at.tabs] == ["Comments", "Map"]
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


class TestGrouping:
    def test_zone_and_method_regroup_results(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = select_garden(run_app(monkeypatch, app_env), zone="6b", method="container")
        heads = subheaders(at)
        assert "Matching reported conditions (3)" in heads
        assert "Partially matching conditions (4)" in heads
        assert any(h.startswith("Conditions not stated (") for h in heads)
        assert not has_group(at, "all")
        assert shown_ids(group(at, "match"), app_env) == set(FULL_MATCHES)
        assert shown_ids(group(at, "partial"), app_env) == {
            "syn-0005",
            "syn-0011",
            "syn-0013",
            "syn-0053",
        }

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
            "70 reports · 3 matching · 4 partially matching · 55 not stated · 8 different"
        ]

    def test_strict_checkbox_hides_groups_and_shows_count(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = select_garden(run_app(monkeypatch, app_env), zone="6b", method="container")
        assert (
            "Ticking this would hide 63 reports whose conditions were not stated or differ."
            in texts_of(at)
        )
        at.checkbox(key=ui.STRICT_KEY).check().run()
        assert has_group(at, "match") and has_group(at, "partial")
        assert not has_group(at, "not_stated")
        assert not has_group(at, "different")
        assert "63 reports hidden because their conditions were not stated or differ." in texts_of(
            at
        )

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

    def test_about_explains_provenance(
        self, monkeypatch: pytest.MonkeyPatch, app_env: AppEnv
    ) -> None:
        at = run_app(monkeypatch, app_env)
        (panel,) = [e for e in at.expander if e.label == "About / Sources"]
        text = "\n".join(texts_of(panel))
        assert "not variety quality" in text
        assert "synthetic demo data" in text
        assert "synthetic\\-demo" in text
