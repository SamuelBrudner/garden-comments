"""Typed, validated runtime configuration loaded from ``config.yaml``."""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal, Self
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from crop_explorer.views import GardenContext, ViewerScope

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.yaml"

_STRICT = ConfigDict(extra="forbid", frozen=True)

ContextLabel = Literal[
    "community report",
    "reported conditions",
    "regional climate reference",
    "published external trial",
]
"""Every evidence label the viewer can show (badges accept any of them)."""

OutsideLabel = Literal[
    "regional climate reference",
    "published external trial",
    "community report",
]
"""Labels an outside-context link may carry.

``"community report"`` marks outside gardeners' own ratings (not this channel's comments);
``"reported conditions"`` describes this channel's comments only and is never allowed here.
"""


class DatabaseConfig(BaseModel):
    """Location of the SQLite database (relative paths resolve against the config file)."""

    model_config = _STRICT
    path: Path


class ViewerConfig(BaseModel):
    """Viewer visibility scope, UI thresholds and input limits.

    ``max_query_chars`` caps the search box (it bounds full-text search cost); ``page_size`` is
    the number of result cards shown per page.
    """

    model_config = _STRICT
    scope: ViewerScope
    min_reports_for_chip: int = Field(ge=1)
    max_query_chars: int = Field(ge=1)
    page_size: int = Field(ge=1)


class MapConfig(BaseModel):
    """The coarse region map: its privacy control and how it is drawn.

    ``height_px`` is the height of the map (the report-count map and every overlay, the scale
    legend included); ``zone_mute`` is how far the other half-zones fade toward the background
    when one zone is emphasised on the zone overlay (0: not at all, 1: invisible). Overlays are
    always off until the viewer picks one (SPEC §7), so the default overlay is not a setting.
    """

    model_config = _STRICT
    public_min_cell_count: int = Field(ge=1)
    height_px: int = Field(ge=200, le=1200)
    zone_mute: float = Field(gt=0, lt=1)


class OverviewConfig(BaseModel):
    """Sizes of the viewer's "At a glance" charts.

    ``top_varieties`` is how many of the most-discussed varieties the bar chart lists;
    ``heatmap_varieties`` is how many of those same varieties (the first ones, in the same
    order) the topic grid shows, so it can never exceed ``top_varieties``.
    """

    model_config = _STRICT
    top_varieties: int = Field(ge=1)
    heatmap_varieties: int = Field(ge=1)

    @model_validator(mode="after")
    def _heatmap_within_top(self) -> Self:
        if self.heatmap_varieties > self.top_varieties:
            raise ValueError(
                f"heatmap_varieties ({self.heatmap_varieties}) cannot exceed top_varieties "
                f"({self.top_varieties}): the grid shows the bar chart's first varieties"
            )
        return self


class ExtractionConfig(BaseModel):
    """Validation rules applied to extractor output."""

    model_config = _STRICT
    hypothetical_markers: tuple[str, ...] = Field(min_length=1)


class QuickChip(BaseModel):
    """A one-tap preset of garden conditions."""

    model_config = _STRICT
    label: str = Field(min_length=1)
    conditions: dict[str, str] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_conditions(self) -> Self:
        GardenContext.model_validate(self.conditions)
        return self

    def garden(self) -> GardenContext:
        """Return the garden context this chip selects."""
        return GardenContext.model_validate(self.conditions)


_OFFICIAL_TOP_LEVEL: Final = frozenset({"gov", "mil"})
"""Top-level domains of official (government) hosts: ``planthardiness.ars.usda.gov``."""
_OFFICIAL_SECOND_LEVEL: Final = frozenset({"gov", "gc", "gouv", "gob", "govt", "go"})
"""Second-level labels of official hosts elsewhere: ``bom.gov.au``, ``agr.gc.ca``."""


def is_official_host(url: str) -> bool:
    """Return whether ``url`` points at a government host (``.gov``, ``.mil``, ``.gov.au`` ...).

    Parameters
    ----------
    url
        An absolute URL.

    Returns
    -------
    bool
        True when the host's top-level label is ``gov``/``mil`` or its second-to-last label is
        a government label such as ``gov``, ``gc`` or ``gouv``.
    """
    labels = (urlsplit(url).hostname or "").split(".")
    return labels[-1] in _OFFICIAL_TOP_LEVEL or (
        len(labels) > 2 and labels[-2] in _OFFICIAL_SECOND_LEVEL
    )


OverlayData = Literal["zones", "normals"]
"""Packaged reference data a link's resource is also shown from, as a map overlay: the 2023
USDA hardiness zones, or the NOAA statewide 1991-2020 averages."""


class OutsideLink(BaseModel):
    """An opt-in outbound context resource, labelled by evidence type (:data:`OutsideLabel`).

    An official (government) resource is a reference or a trial, never gardener testimony, so
    a ``"community report"`` label on a government host is rejected at load time. ``overlay``
    names the map overlay built from the same kind of data, if any; only a ``"regional climate
    reference"`` can have one (variety trials and gardener ratings stay links).
    """

    model_config = _STRICT
    title: str = Field(min_length=1)
    url: str = Field(pattern=r"^https://")
    label: OutsideLabel
    note: str
    overlay: OverlayData | None = None

    @model_validator(mode="after")
    def _official_is_not_community(self) -> Self:
        if self.label == "community report" and is_official_host(self.url):
            raise ValueError(
                f"Outside link {self.title!r} is an official resource; label it "
                "'regional climate reference' or 'published external trial', "
                "not 'community report'"
            )
        return self

    @model_validator(mode="after")
    def _overlay_is_a_climate_reference(self) -> Self:
        if self.overlay is not None and self.label != "regional climate reference":
            raise ValueError(
                f"Outside link {self.title!r} names a map overlay; only a 'regional climate "
                "reference' can have one"
            )
        return self


class LoggingConfig(BaseModel):
    """Loguru sinks."""

    model_config = _STRICT
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]
    json_path: Path | None


class AppConfig(BaseModel):
    """Root configuration object."""

    model_config = _STRICT
    database: DatabaseConfig
    viewer: ViewerConfig
    map: MapConfig
    overview: OverviewConfig
    extraction: ExtractionConfig
    quick_chips: tuple[QuickChip, ...]
    outside_context: tuple[OutsideLink, ...]
    logging: LoggingConfig


def _resolve(base: Path, path: Path | None) -> Path | None:
    if path is None or path.is_absolute():
        return path
    return (base / path).resolve()


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    """Load and validate configuration; relative paths resolve against the file's directory.

    Parameters
    ----------
    path
        The YAML configuration file.

    Returns
    -------
    AppConfig
        The validated configuration with absolute database and log paths.

    Raises
    ------
    FileNotFoundError
        If ``path`` does not exist.
    pydantic.ValidationError
        If the YAML does not satisfy the schema.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg = AppConfig.model_validate(raw)
    base = path.resolve().parent
    db = cfg.database.model_copy(update={"path": _resolve(base, cfg.database.path)})
    log = cfg.logging.model_copy(update={"json_path": _resolve(base, cfg.logging.json_path)})
    return cfg.model_copy(update={"database": db, "logging": log})
