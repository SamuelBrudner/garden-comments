"""Tests for runtime configuration: ``config.py``, ``config.yaml`` and ``.streamlit/config.toml``.

The configuration carries evidence labels for outside links (SPEC §7: reports, references and
trials must never be confused) and the viewer's limits; the Streamlit settings pin neither the
address nor the port (Streamlit Community Cloud chooses both).
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml
from pydantic import ValidationError

from crop_explorer import ui
from crop_explorer.config import (
    DEFAULT_CONFIG_PATH,
    OutsideLabel,
    OutsideLink,
    is_official_host,
    load_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
STREAMLIT_CONFIG = REPO_ROOT / ".streamlit" / "config.toml"


def raw_config() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    return raw


def write_config(tmp_path: Path, raw: dict[str, Any]) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def outside_link(label: str, url: str = "https://example.org/map") -> dict[str, str]:
    return {"title": "Outside resource", "url": url, "label": label, "note": ""}


# --------------------------------------------------------------------------- streamlit (F01)


def streamlit_settings() -> dict[str, Any]:
    return tomllib.loads(STREAMLIT_CONFIG.read_text(encoding="utf-8"))


def test_streamlit_config_pins_neither_address_nor_port() -> None:
    """Streamlit Community Cloud health-checks the app on localhost:8501 and proxies to it.

    The shipped settings must leave both to the host. The private view stays local because
    ``app.py`` refuses private scope unless ``--server.address`` is a loopback address (tested
    in ``tests/test_app.py``), not because of this file.
    """
    server = streamlit_settings().get("server", {})

    assert "address" not in server
    assert "port" not in server


def test_streamlit_serves_the_apps_own_base_map() -> None:
    """Maps load Plotly's U.S. base map from ./static, never from cdn.plot.ly."""
    assert streamlit_settings()["server"]["enableStaticServing"] is True
    base_map = REPO_ROOT / "static" / "topojson" / "usa_110m.json"
    data = base_map.read_bytes()
    notice = (base_map.parent / "NOTICE.md").read_text(encoding="utf-8")
    assert hashlib.sha256(data).hexdigest() in notice
    assert f"{len(data):,} bytes" in notice
    assert "MIT License" in notice
    topology = json.loads(data)
    assert topology["type"] == "Topology"
    assert len(topology["objects"]["subunits"]["geometries"]) == 51  # 50 states and DC
    assert ui.BASE_MAP_URL == "app/static/topojson/"


def test_streamlit_theme_follows_the_visitors_light_or_dark_setting() -> None:
    """A single top-level theme colour pins a light custom theme: dark-mode visitors would get
    a light page while ``st.context.theme.type`` reports dark (the "At a glance" charts pick
    their tokens from it). Accents are set per mode instead."""
    theme = streamlit_settings()["theme"]
    assert set(theme) == {"light", "dark"}
    assert theme["light"]["primaryColor"]
    assert theme["dark"]["primaryColor"]
    assert "base" not in theme


def test_overview_chart_sizes_are_configured() -> None:
    overview = load_config().overview

    assert (overview.top_varieties, overview.heatmap_varieties) == (10, 8)


def test_streamlit_config_keeps_the_viewer_safety_settings() -> None:
    settings = streamlit_settings()

    assert settings["runner"]["magicEnabled"] is False
    assert settings["browser"]["gatherUsageStats"] is False


# --------------------------------------------------------------------------- outside links


def test_outside_labels_are_reference_trial_or_outside_community_report() -> None:
    assert set(get_args(OutsideLabel)) == {
        "regional climate reference",
        "published external trial",
        "community report",
    }


def test_reported_conditions_is_not_an_outside_link_label(tmp_path: Path) -> None:
    """'reported conditions' describes this channel's own comments, never an outside link."""
    raw = raw_config()
    raw["outside_context"] = [outside_link("reported conditions")]

    with pytest.raises(ValidationError, match="label"):
        load_config(write_config(tmp_path, raw))
    with pytest.raises(ValidationError):
        OutsideLink.model_validate(outside_link("reported conditions"))


@pytest.mark.parametrize("label", get_args(OutsideLabel))
def test_each_outside_label_is_accepted(label: str) -> None:
    assert OutsideLink.model_validate(outside_link(label)).label == label


OFFICIAL_URLS = [
    "https://planthardiness.ars.usda.gov/",
    "https://www.ncei.noaa.gov/products/land-based-station/us-climate-normals",
    "https://power.larc.nasa.gov/docs/",
    "https://www.army.mil/",
    "https://www.bom.gov.au/climate/",
    "https://agriculture.canada.gc.ca/",
    "https://meteofrance.gouv.fr/",
]


@pytest.mark.parametrize("url", OFFICIAL_URLS)
def test_an_official_resource_can_never_be_a_community_report(tmp_path: Path, url: str) -> None:
    """F48: copying the 'community report' label onto e.g. the USDA map fails at load time."""
    with pytest.raises(ValidationError, match="official resource"):
        OutsideLink.model_validate(outside_link("community report", url))
    raw = raw_config()
    for link in raw["outside_context"]:
        if "USDA" in link["title"]:
            link["label"] = "community report"
    with pytest.raises(ValidationError, match="official resource"):
        load_config(write_config(tmp_path, raw))
    for label in ("regional climate reference", "published external trial"):
        assert OutsideLink.model_validate(outside_link(label, url)).label == label


@pytest.mark.parametrize(
    ("url", "official"),
    [
        *((url, True) for url in OFFICIAL_URLS),
        ("https://vegvariety.cce.cornell.edu/", False),
        ("https://agronomy.unl.edu/vegetable-variety-navigator/", False),
        ("https://gov.example.com/", False),
        ("https://example.org/gov", False),
    ],
)
def test_is_official_host(url: str, official: bool) -> None:
    assert is_official_host(url) is official


def test_every_shipped_community_report_link_is_not_official() -> None:
    community = [x for x in load_config().outside_context if x.label == "community report"]
    assert community
    assert not any(is_official_host(link.url) for link in community)


def test_cornell_gardener_ratings_are_a_community_report_not_a_trial() -> None:
    links = {link.title: link for link in load_config().outside_context}

    cornell = links["Cornell Vegetable Varieties for Gardeners"]
    nebraska = links["Nebraska Vegetable Variety Navigator"]

    assert cornell.label == "community report"
    assert "outside" in cornell.note.lower()
    assert "not this channel" in cornell.note.lower()
    assert nebraska.label == "published external trial"


# --------------------------------------------------------------------------- viewer limits (F37)


def test_viewer_limits_are_configured() -> None:
    viewer = load_config().viewer

    assert viewer.max_query_chars == 200
    assert viewer.page_size == 20


@pytest.mark.parametrize("key", ["max_query_chars", "page_size"])
@pytest.mark.parametrize("value", [0, -1])
def test_viewer_limits_must_be_positive(tmp_path: Path, key: str, value: int) -> None:
    raw = raw_config()
    raw["viewer"][key] = value

    with pytest.raises(ValidationError, match=key):
        load_config(write_config(tmp_path, raw))


@pytest.mark.parametrize("key", ["max_query_chars", "page_size"])
def test_viewer_limits_are_required(tmp_path: Path, key: str) -> None:
    raw = raw_config()
    del raw["viewer"][key]

    with pytest.raises(ValidationError, match=key):
        load_config(write_config(tmp_path, raw))


# --------------------------------------------------------------------------- overlay links


@pytest.mark.parametrize("overlay", ["zones", "normals"])
def test_only_a_climate_reference_can_name_a_map_overlay(overlay: str) -> None:
    raw = {**outside_link("regional climate reference"), "overlay": overlay}
    assert OutsideLink.model_validate(raw).overlay == overlay
    for label in ("published external trial", "community report"):
        with pytest.raises(ValidationError, match="map overlay"):
            OutsideLink.model_validate({**outside_link(label), "overlay": overlay})


def test_an_unknown_overlay_is_rejected() -> None:
    with pytest.raises(ValidationError):
        OutsideLink.model_validate(
            {**outside_link("regional climate reference"), "overlay": "rain"}
        )


def test_the_shipped_config_links_the_usda_and_noaa_rows_to_overlays() -> None:
    links = {link.title: link.overlay for link in load_config().outside_context}
    assert links["USDA Plant Hardiness Zone Map"] == "zones"
    assert links["NOAA U.S. Climate Normals (1991-2020)"] == "normals"
    assert [title for title, overlay in links.items() if overlay is not None] == [
        "USDA Plant Hardiness Zone Map",
        "NOAA U.S. Climate Normals (1991-2020)",
    ]


# --------------------------------------------------------------------------- map drawing


def test_map_drawing_tunables_are_configured() -> None:
    """The map's height (both maps) and the zone highlight's fade come from config.yaml."""
    cfg = load_config().map
    assert cfg.height_px == 340
    assert cfg.zone_mute == 0.72


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("height_px", 199),
        ("height_px", 1201),
        ("zone_mute", 0),
        ("zone_mute", 1),
        ("zone_mute", -0.1),
    ],
)
def test_map_drawing_tunables_are_bounded(tmp_path: Path, key: str, value: float) -> None:
    raw = raw_config()
    raw["map"][key] = value
    with pytest.raises(ValidationError, match=key):
        load_config(write_config(tmp_path, raw))


@pytest.mark.parametrize("key", ["height_px", "zone_mute"])
def test_map_drawing_tunables_are_required(tmp_path: Path, key: str) -> None:
    raw = raw_config()
    del raw["map"][key]
    with pytest.raises(ValidationError, match=key):
        load_config(write_config(tmp_path, raw))


def test_no_overlay_can_be_switched_on_by_default(tmp_path: Path) -> None:
    """SPEC §7: overlays stay opt-in, so the default overlay is not a setting."""
    raw = raw_config()
    raw["map"]["default_overlay"] = "zones"
    with pytest.raises(ValidationError, match="default_overlay"):
        load_config(write_config(tmp_path, raw))


# --------------------------------------------------------------------------- youtube


def test_youtube_settings_are_configured() -> None:
    youtube = load_config().youtube

    assert youtube.api_base_url == "https://www.googleapis.com/youtube/v3"
    assert youtube.page_size == 100
    assert youtube.timeout_s > 0
    assert youtube.max_pages >= 1
    assert youtube.retention_days == 30
    assert youtube.api_key_env == "YOUTUBE_API_KEY"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("api_base_url", "http://www.googleapis.com/youtube/v3"),
        ("api_base_url", "www.googleapis.com/youtube/v3"),
        ("api_base_url", "https://www.googleapis.com/youtube/v3/"),
        ("api_base_url", "https://www.googleapis.com/youtube/v3?key=x"),
        ("api_base_url", "https://user@www.googleapis.com/youtube/v3"),
        # The key header goes to this host: only Google's API hosts, no port.
        ("api_base_url", "https://www.googleapis.co/youtube/v3"),
        ("api_base_url", "https://www.googleapi.com/youtube/v3"),
        ("api_base_url", "https://googleapis.com/youtube/v3"),
        ("api_base_url", "https://evil.example.com/youtube/v3"),
        ("api_base_url", "https://www.googleapis.com.example.com/youtube/v3"),
        ("api_base_url", "https://www.googleapis.com:8443/youtube/v3"),
        ("page_size", 0),
        ("page_size", 101),
        ("timeout_s", 0),
        ("timeout_s", -1),
        ("timeout_s", float("inf")),
        ("max_pages", 0),
        ("retention_days", 0),
        ("retention_days", 31),
        ("api_key_env", ""),
        ("api_key_env", "YOUTUBE API KEY"),
        ("api_key_env", "1KEY"),
        # Upper case only, so a key (Google keys mix cases) is never taken for a name.
        ("api_key_env", "youtube_api_key"),
        ("api_key_env", "AIzaSyntheticNotARealKey0123"),
    ],
)
def test_invalid_youtube_settings_are_rejected(tmp_path: Path, key: str, value: object) -> None:
    raw = raw_config()
    raw["youtube"][key] = value

    with pytest.raises(ValidationError, match=key):
        load_config(write_config(tmp_path, raw))


@pytest.mark.parametrize(
    "url", ["https://youtube.googleapis.com/youtube/v3", "https://WWW.GoogleAPIs.com/youtube/v3"]
)
def test_google_api_hosts_are_accepted(tmp_path: Path, url: str) -> None:
    raw = raw_config()
    raw["youtube"]["api_base_url"] = url

    assert load_config(write_config(tmp_path, raw)).youtube.api_base_url == url


SYNTHETIC_KEY = "AIzaSy-synthetic-not-a-real-key"
"""Shaped like the start of a Google API key; too short to be one."""


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("api_key_env", SYNTHETIC_KEY),
        ("api_key_env", SYNTHETIC_KEY.replace("-", "")),
        ("api_base_url", f"https://www.googleapis.com/youtube/v3?key={SYNTHETIC_KEY}"),
        ("api_base_url", f"https://{SYNTHETIC_KEY.lower()}.example.com/youtube/v3"),
    ],
)
def test_a_key_put_in_the_config_by_mistake_is_never_echoed(
    tmp_path: Path, key: str, value: str
) -> None:
    """Validation errors reach stderr and the viewer page: they name the setting, not its value."""
    raw = raw_config()
    raw["youtube"][key] = value

    with pytest.raises(ValidationError, match=key) as caught:
        load_config(write_config(tmp_path, raw))

    message = str(caught.value)
    for fragment in ("synthetic", "Synthetic", "not-a-real", "notareal", "NotAReal"):
        assert fragment not in message


@pytest.mark.parametrize(
    "key", ["api_base_url", "page_size", "timeout_s", "max_pages", "retention_days", "api_key_env"]
)
def test_every_youtube_setting_is_required(tmp_path: Path, key: str) -> None:
    raw = raw_config()
    del raw["youtube"][key]

    with pytest.raises(ValidationError, match=key):
        load_config(write_config(tmp_path, raw))


def test_the_youtube_section_is_required_and_strict(tmp_path: Path) -> None:
    raw = raw_config()
    raw["youtube"]["api_key"] = "never put a key in the config"
    with pytest.raises(ValidationError, match="api_key"):
        load_config(write_config(tmp_path, raw))

    del raw["youtube"]
    with pytest.raises(ValidationError, match="youtube"):
        load_config(write_config(tmp_path, raw))
