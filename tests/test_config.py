"""Tests for runtime configuration: ``config.py``, ``config.yaml`` and ``.streamlit/config.toml``.

The configuration carries evidence labels for outside links (SPEC §7: reports, references and
trials must never be confused) and the viewer's limits; the Streamlit settings decide which
network interfaces the viewer listens on.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml
from pydantic import ValidationError

from crop_explorer.config import (
    DEFAULT_CONFIG_PATH,
    OutsideLabel,
    OutsideLink,
    is_official_host,
    load_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
STREAMLIT_CONFIG = REPO_ROOT / ".streamlit" / "config.toml"
LOOPBACK = {"localhost", "127.0.0.1", "::1"}


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


def test_streamlit_viewer_binds_to_this_machine_only() -> None:
    """A private-scope viewer has no authentication; it must not listen on the network."""
    settings = tomllib.loads(STREAMLIT_CONFIG.read_text(encoding="utf-8"))

    assert settings["server"]["address"] in LOOPBACK


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
