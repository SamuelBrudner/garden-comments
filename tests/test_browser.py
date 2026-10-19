"""Browser test of the running viewer at phone width (SPEC §11 #17), with Playwright + Chromium.

``streamlit run app.py`` is started headless on a free loopback port against a temporary
database: the synthetic demo fixtures with their extractions, plus one ``cleared`` comment with a
permalink. Headless Chromium then checks, at 390x844, that the page never scrolls sideways, that
the search box and the always-visible condition selects are on screen, that choosing a zone
regroups the reports, that Clear restores the first-load labels (never "Choose an option"),
that a source reference opens the comment's permalink, and that the Hot & humid chip's "In their
own words" condition lines fit, and that the "At a glance" stat tiles sit two by two with all
three tab labels on screen and the first bars above the fold; then a 1280x900 smoke test (tiles
four across) and click-through tests: clicking a variety bar or a topic cell shows exactly the
reports it counts, says so above the chart and links down to them; on a touch screen the touch
hint is shown and two taps on a bar (Plotly selects on the second) show its reports; and, at
390x844, the hardiness-zone overlay draws its own (self-contained) zone polygons with the "not
the official map" disclaimer on screen and no sideways scrolling.
Marked ``browser`` (deselect with
``-m "not browser"``); CI installs Chromium with ``uv run playwright install --with-deps
chromium``.
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
from crop_explorer.overlay_charts import ZONE_LAYER_URL
from crop_explorer.overlays import ZONE_DISCLAIMER
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
GLANCE_TABS = ("Varieties", "Topics", "How people grow")


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


def open_viewer(
    browser: Browser, url: str, width: int, height: int, *, touch: bool = False
) -> Page:
    viewport = {"width": width, "height": height}
    context = browser.new_context(viewport=viewport, has_touch=touch, is_mobile=touch)
    page = context.new_page()
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


def test_phone_width_own_words_lines_fit(browser: Browser, viewer_url: str) -> None:
    """The Hot & humid chip's cards quote the commenters' own words without sideways scroll."""
    page = open_viewer(browser, viewer_url, 390, 844)
    page.get_by_role("button", name="Hot & humid", exact=True).click()
    expect(heading(page, re.compile(r"^Matching reported conditions \(\d+\)$"))).to_be_visible()
    own_words = page.get_by_text(re.compile(r"In their own words.*counted as hot summers"))
    expect(own_words.first).to_be_visible()
    assert_no_horizontal_overflow(page)
    assert_no_exception(page)


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


# --------------------------------------------------------------------------- at a glance


def tile_boxes(page: Page) -> list[dict[str, float]]:
    tiles = page.locator(".glance-tile")
    expect(tiles).to_have_count(4)
    boxes = [tiles.nth(i).bounding_box() for i in range(4)]
    assert all(box is not None for box in boxes)
    return [box for box in boxes if box is not None]


def painted_bars(page: Page) -> Locator:
    """The visible variety bars (the second bar trace: the first is the invisible row layer)."""
    panel = page.get_by_role("tabpanel", name=GLANCE_TABS[0])
    return panel.locator(".barlayer .trace").nth(1).locator(".point")


def plot_area(page: Page, tab: str) -> dict[str, float]:
    """The plotting area (where clicks land) of the chart in an "At a glance" tab."""
    box = (
        page.get_by_role("tabpanel", name=tab).locator(".js-plotly-plot .nsewdrag").first
    ).bounding_box()
    assert box is not None
    return box


def test_phone_width_glance_tiles_are_two_by_two_and_the_first_chart_shows(
    browser: Browser, viewer_url: str
) -> None:
    page = open_viewer(browser, viewer_url, 390, 844, touch=True)  # a phone: overlay scrollbars
    boxes = tile_boxes(page)
    assert len({round(box["y"]) for box in boxes}) == 2, boxes
    assert len({round(box["x"]) for box in boxes}) == 2, boxes
    assert all(box["x"] >= 0 and box["x"] + box["width"] <= 390 for box in boxes), boxes
    assert_no_horizontal_overflow(page)
    for label in GLANCE_TABS:  # no tab label clipped in the tab strip
        tab = page.get_by_role("tab", name=label, exact=True).bounding_box()
        assert tab is not None
        assert tab["x"] + tab["width"] <= 390 - 16, (label, tab)
    chart = page.get_by_role("tabpanel", name=GLANCE_TABS[0]).locator(".js-plotly-plot")
    expect(chart).to_be_visible()
    first_bar = painted_bars(page).first.bounding_box()
    assert first_bar is not None
    assert first_bar["y"] < 844, first_bar  # the bars start above the fold
    chart.scroll_into_view_if_needed()
    expect(chart).to_be_in_viewport()
    expect(painted_bars(page)).to_have_count(10)
    chart_box = chart.bounding_box()
    assert chart_box is not None
    assert chart_box["x"] >= 0
    assert chart_box["x"] + chart_box["width"] <= 390
    assert_no_exception(page)


def test_desktop_glance_tiles_are_four_across(browser: Browser, viewer_url: str) -> None:
    page = open_viewer(browser, viewer_url, 1280, 900)
    boxes = tile_boxes(page)
    assert len({round(box["y"]) for box in boxes}) == 1, boxes
    assert_no_horizontal_overflow(page)
    panel = page.get_by_role("tabpanel", name=GLANCE_TABS[0])
    expect(panel.get_by_text("Click a bar to show its comments below.")).to_be_visible()
    expect(panel.locator(".glance-touch")).to_be_hidden()


def bar_box(page: Page, index: int) -> dict[str, float]:
    bar = painted_bars(page).nth(index)
    bar.scroll_into_view_if_needed()
    box = bar.bounding_box()
    assert box is not None
    return box


def hover_texts(page: Page) -> list[str]:
    texts: list[str] = page.evaluate(
        "[...document.querySelectorAll('.hoverlayer text')].map(t => t.textContent)"
    )
    return texts


def feedback(page: Page, tab: str, name: str) -> Locator:
    """The line above the chart in ``tab`` naming what a click is showing."""
    panel = page.get_by_role("tabpanel", name=tab)
    return panel.get_by_text(f"Showing the reports that mention {name}")


def test_clicking_a_bar_shows_exactly_its_reports_and_says_so(
    browser: Browser, viewer_url: str
) -> None:
    page = open_viewer(browser, viewer_url, 1280, 900)
    expect(painted_bars(page)).to_have_count(10)
    juliet = bar_box(page, 2)  # Sungold, Cherokee Purple, Juliet, ...
    grid = plot_area(page, GLANCE_TABS[0])
    row_y = juliet["y"] + juliet["height"] / 2
    page.mouse.move(grid["x"] + grid["width"] - 20, row_y)  # anywhere on the row
    expect(page.locator(".hoverlayer .hovertext")).to_have_count(1)  # one box, not two
    assert hover_texts(page) == ["Juliet · tomato: 5 comments"]
    page.mouse.click(grid["x"] + grid["width"] - 20, row_y)
    expect(heading(page, "Reports (5)")).to_be_visible()  # exactly the bar's count
    expect(page.get_by_label(SEARCH_LABEL)).to_have_value("")
    shown = feedback(page, GLANCE_TABS[0], "Juliet · tomato")
    expect(shown).to_be_in_viewport()  # right above the chart that was clicked
    expect(page.get_by_text("Showing only reports that mention Juliet · tomato")).to_be_attached()

    page.get_by_role("link", name="Go to the reports ↓").first.click()
    page.wait_for_function(  # the empty anchor div scrolls to the top of the window
        "() => Math.abs(document.getElementById('glance-results').getBoundingClientRect().top) < 4"
    )
    expect(heading(page, "Reports (5)")).to_be_in_viewport()
    page.get_by_role("button", name="Any variety", exact=True).click()
    expect(heading(page, f"Reports ({N_PUBLIC})")).to_be_visible()
    assert_no_exception(page)


def test_clicking_a_topic_cell_shows_exactly_its_reports(
    browser: Browser, viewer_url: str
) -> None:
    page = open_viewer(browser, viewer_url, 1280, 900)
    page.get_by_role("tab", name=GLANCE_TABS[1], exact=True).click()
    plot = page.get_by_role("tabpanel", name=GLANCE_TABS[1]).locator(".js-plotly-plot")
    expect(plot).to_be_visible()
    first: list[Any] = plot.evaluate(
        "e => [e.data[0].z[0][0], e.layout.xaxis.ticktext[0], e.layout.yaxis.ticktext[0]]"
    )
    count, topic, variety = first
    grid = plot_area(page, GLANCE_TABS[1])
    page.mouse.click(grid["x"] + 10, grid["y"] + 8)  # top-left cell
    expect(heading(page, f"Reports ({count})")).to_be_visible()
    expect(page.get_by_test_id("stMultiSelect")).to_contain_text(topic)  # same label as the grid
    expect(feedback(page, GLANCE_TABS[1], f"{variety} · tomato about {topic}")).to_be_in_viewport()
    assert_no_exception(page)


def test_on_a_touch_screen_two_taps_on_a_bar_show_its_reports(
    browser: Browser, viewer_url: str
) -> None:
    """Plotly selects on the second tap (the first shows the count), and the hint says so."""
    page = open_viewer(browser, viewer_url, 390, 844, touch=True)
    panel = page.get_by_role("tabpanel", name=GLANCE_TABS[0])
    expect(panel.get_by_text("Tap a bar once to see its count, then tap it again")).to_be_visible()
    expect(panel.locator(".glance-mouse")).to_be_hidden()
    juliet = bar_box(page, 2)
    point = (juliet["x"] + juliet["width"] / 2, juliet["y"] + juliet["height"] / 2)
    page.touchscreen.tap(*point)
    expect(page.locator(".hoverlayer .hovertext")).to_have_count(1)
    assert hover_texts(page) == ["Juliet · tomato: 5 comments"]
    page.touchscreen.tap(*point)
    expect(heading(page, "Reports (5)")).to_be_visible()
    expect(feedback(page, GLANCE_TABS[0], "Juliet · tomato")).to_be_attached()
    assert_no_horizontal_overflow(page)
    assert_no_exception(page)


# --------------------------------------------------------------------------- climate overlays


def test_phone_width_zone_overlay_shows_the_map_and_the_disclaimer(
    browser: Browser, viewer_url: str
) -> None:
    """The zone layer is packaged GeoJSON, so it draws even without Plotly's CDN state shapes."""
    page = open_viewer(browser, viewer_url, 390, 844, touch=True)
    page.get_by_role("tab", name="Map", exact=True).click()
    panel = page.get_by_role("tabpanel", name="Map")
    panel.get_by_text("USDA hardiness zones (2023)", exact=True).click()
    disclaimer = panel.get_by_text(ZONE_DISCLAIMER)
    disclaimer.scroll_into_view_if_needed()
    expect(disclaimer).to_be_in_viewport()
    plot = panel.locator(".js-plotly-plot")
    expect(plot).to_have_count(1)
    # 25 half-zones, then the 51 state outlines from the app's own base map (no CDN here).
    expect(plot.locator(".choroplethlocation")).to_have_count(25 + 51)
    labels = plot.locator(".scattergeo text")
    expect(labels.first).to_be_attached()
    assert "4" in labels.all_text_contents()  # Connecticut's count, a state-level aggregate
    plot.scroll_into_view_if_needed()
    expect(plot).to_be_in_viewport()
    expect(plot.locator(".colorbar")).to_have_count(1)  # the scale legend
    box = plot.bounding_box()
    assert box is not None
    assert box["x"] >= 0 and box["x"] + box["width"] <= 390, box
    assert box["height"] > 200, box
    assert_no_horizontal_overflow(page)
    assert_no_exception(page)


def test_the_base_map_is_served_by_the_app_not_the_cdn(viewer_url: str) -> None:
    """Plotly draws nothing on a map until its base map loads; the app serves it itself."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"{viewer_url}app/static/topojson/usa_110m.json", timeout=10) as response:
        topology = json.loads(response.read())
    assert topology["type"] == "Topology"
    assert len(topology["objects"]["subunits"]["geometries"]) == 51


def test_the_zone_layer_is_served_by_the_app(viewer_url: str) -> None:
    """The zone figure names this URL; the browser fetches the layer once and caches it."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"{viewer_url}{ZONE_LAYER_URL}"
    with opener.open(url, timeout=10) as response:
        layer = json.loads(response.read())
    assert layer["type"] == "FeatureCollection"
    assert layer["disclaimer"] == ZONE_DISCLAIMER
    assert len(layer["features"]) == 25
