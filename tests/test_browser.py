"""Browser test of the running viewer at phone width (SPEC §11 #17), with Playwright + Chromium.

``streamlit run app.py`` is started headless on a free loopback port against a temporary
database: the synthetic demo fixtures with their extractions, plus one ``cleared`` comment with a
permalink. Headless Chromium then checks, at 390x844, that the page never scrolls sideways, that
the search box and the always-visible condition selects are on screen, that choosing a zone
regroups the reports, that Clear restores the first-load labels (never "Choose an option"), and
that a source reference opens the comment's permalink; then a 1280x900 smoke test. Marked
``browser`` (deselect with ``-m "not browser"``); CI installs Chromium with
``uv run playwright install --with-deps chromium``.
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from playwright.sync_api import Browser, Locator, Page, expect, sync_playwright

from crop_explorer import db
from crop_explorer.config import DEFAULT_CONFIG_PATH, load_config
from crop_explorer.extraction import load_extractions
from crop_explorer.ingest import build_source, import_file
from crop_explorer.schemas import ColumnMapping, RightsStatus

pytestmark = pytest.mark.browser

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "fixtures"
CHROMIUM = Path("/opt/pw-browsers/chromium")
NOW = "2026-10-15T12:00:00Z"
STARTUP_SECONDS = 90
TIMEOUT_MS = 60_000
SEARCH_LABEL = "Search crops, varieties, or anything people mentioned"
ALWAYS_VISIBLE = ("Region", "Hardiness zone", "Growing method", "Setting")
CLEARED_URL = "https://www.youtube.com/watch?v=abc123&lc=Ug-cleared"
CLEARED_COMMENT = {
    "comment_id": "clr-0001",
    "text": "CLEAREDLINK Moonbeam okra in containers in Omaha, Nebraska.",
    "permalink": CLEARED_URL,
    "created_at": "2024-07-01T00:00:00Z",
}
N_PUBLIC = 70  # 69 synthetic fixtures + 1 cleared comment


# --------------------------------------------------------------------------- server


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


def _build(root: Path) -> Path:
    """Write a config and build its database; return the config path."""
    db_path = root / "browser.sqlite"
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    raw["database"]["path"] = str(db_path)
    raw["logging"] = {"level": "WARNING", "json_path": None}
    config = root / "config.yaml"
    config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    cleared = root / "cleared.jsonl"
    cleared.write_text(json.dumps(CLEARED_COMMENT) + "\n", encoding="utf-8")
    conn = db.connect(db_path)
    try:
        _import(conn, FIXTURES / "synthetic_comments.jsonl", "synthetic-demo", "synthetic")
        extractions = FIXTURES / "synthetic_extractions.jsonl"
        report = load_extractions(conn, extractions, load_config(config).extraction, NOW)
        assert report.rejections == ()
        _import(conn, cleared, "cleared-test", "cleared")
    finally:
        conn.close()
    return config


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _healthy(port: int) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback, no proxy
    try:
        with opener.open(f"http://127.0.0.1:{port}/_stcore/health", timeout=2) as response:
            return bool(response.status == 200)
    except OSError:
        return False


def _wait_until_healthy(port: int, process: subprocess.Popen[bytes], log: Path) -> None:
    deadline = time.monotonic() + STARTUP_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"streamlit exited with {process.returncode}; see {log}")
        if _healthy(port):
            return
        time.sleep(0.5)
    raise RuntimeError(f"streamlit did not become healthy in {STARTUP_SECONDS}s; see {log}")


def _streamlit_command(port: int, config: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(REPO / "app.py"),
        "--server.headless=true",
        f"--server.port={port}",
        "--server.address=127.0.0.1",
        "--server.fileWatcherType=none",
        "--browser.gatherUsageStats=false",
        "--",
        "--config",
        str(config),
    ]


@pytest.fixture(scope="module")
def viewer_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Serve the public viewer on a free loopback port; always stop the server afterwards."""
    root = tmp_path_factory.mktemp("browser")
    config = _build(root)
    port = _free_port()
    log = root / "streamlit.log"
    with log.open("wb") as sink:
        process = subprocess.Popen(
            _streamlit_command(port, config), cwd=REPO, stdout=sink, stderr=subprocess.STDOUT
        )
        try:
            _wait_until_healthy(port, process, log)
            yield f"http://127.0.0.1:{port}/"
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        options: dict[str, Any] = {"args": ["--no-proxy-server"]}
        if CHROMIUM.exists():
            options["executable_path"] = str(CHROMIUM)
        launched = playwright.chromium.launch(**options)
        try:
            yield launched
        finally:
            launched.close()


# --------------------------------------------------------------------------- page helpers


def open_viewer(browser: Browser, url: str, width: int, height: int) -> Page:
    page = browser.new_context(viewport={"width": width, "height": height}).new_page()
    page.set_default_timeout(TIMEOUT_MS)
    page.goto(url)
    expect(heading(page, f"Reports ({N_PUBLIC})")).to_be_visible()
    expect(combobox(page, "Region")).to_be_visible()
    return page


def heading(page: Page, name: str | re.Pattern[str]) -> Locator:
    return page.get_by_role("heading", name=name, exact=True)


def combobox(scope: Page | Locator, label: str) -> Locator:
    return scope.get_by_role("combobox", name=label, exact=True)


def shown_label(box: Locator) -> str:
    """Return what a select displays: its value, else its placeholder."""
    return box.input_value() or (box.get_attribute("placeholder") or "")


def select_states(page: Page) -> dict[str, tuple[str, str | None]]:
    """Return ``label -> (value, placeholder)`` of the condition and filter selects."""
    labels = (*ALWAYS_VISIBLE, "Crop", "Year grown")
    boxes = {label: combobox(page, label) for label in labels}
    return {label: (b.input_value(), b.get_attribute("placeholder")) for label, b in boxes.items()}


def pick(page: Page, label: str, option: str) -> None:
    """Choose an option by typing it (long option lists are virtualised) and clicking it."""
    box = combobox(page, label)
    box.click()
    box.fill(option)
    page.get_by_role("option", name=option, exact=True).click()


def assert_no_horizontal_overflow(page: Page) -> None:
    widths = page.evaluate(
        """() => {
            const main = document.querySelector('[data-testid="stMain"]');
            return [document.documentElement.scrollWidth, main.scrollWidth, window.innerWidth];
        }"""
    )
    page_width, main_width, viewport = widths
    assert page_width <= viewport, widths
    assert main_width <= viewport, widths


def assert_no_exception(page: Page) -> None:
    expect(page.get_by_test_id("stException")).to_have_count(0)


# --------------------------------------------------------------------------- tests


def test_phone_width_exposes_inputs_resets_them_and_opens_sources(
    browser: Browser, viewer_url: str
) -> None:
    page = open_viewer(browser, viewer_url, 390, 844)
    assert_no_horizontal_overflow(page)
    expect(page.get_by_label(SEARCH_LABEL)).to_be_visible()
    expect(page.get_by_role("button", name="Clear", exact=True)).to_be_visible()
    for label in ALWAYS_VISIBLE:
        expect(combobox(page, label)).to_be_visible()
        assert shown_label(combobox(page, label)) == "Not set", label
    first_load = select_states(page)

    pick(page, "Hardiness zone", "Zone 6b")
    expect(heading(page, re.compile(r"^Matching reported conditions \(\d+\)$"))).to_be_visible()
    expect(heading(page, f"Reports ({N_PUBLIC})")).to_have_count(0)
    assert_no_horizontal_overflow(page)

    page.get_by_role("button", name="Clear", exact=True).click()
    expect(heading(page, f"Reports ({N_PUBLIC})")).to_be_visible()
    expect(combobox(page, "Hardiness zone")).not_to_have_value("Zone 6b")
    assert select_states(page) == first_load  # same value and placeholder as on first load
    assert "Choose an option" not in page.content()

    source = page.get_by_role("link", name="Source ↗")
    expect(source).to_have_count(1)  # synthetic rows never link out
    expect(source).to_have_attribute("href", CLEARED_URL)
    assert_no_exception(page)


def test_phone_width_more_conditions_are_one_tap_away(browser: Browser, viewer_url: str) -> None:
    page = open_viewer(browser, viewer_url, 390, 844)
    expect(combobox(page, "Summer heat")).to_be_hidden()
    page.get_by_text("More conditions", exact=True).click()
    expect(combobox(page, "Summer heat")).to_be_visible()
    assert_no_horizontal_overflow(page)


def test_desktop_smoke(browser: Browser, viewer_url: str) -> None:
    page = open_viewer(browser, viewer_url, 1280, 900)
    assert_no_horizontal_overflow(page)
    expect(page.get_by_label(SEARCH_LABEL)).to_be_visible()
    page.get_by_role("tab", name="Map").click()
    map_panel = page.get_by_role("tabpanel", name="Map")
    expect(combobox(map_panel, "Filter by reported state")).to_be_visible()
    expect(map_panel.get_by_text("community report", exact=True)).to_be_visible()
    expect(map_panel.get_by_text("reported conditions", exact=True)).to_be_visible()
    assert_no_exception(page)
