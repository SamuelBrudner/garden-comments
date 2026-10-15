"""Typed, validated runtime configuration loaded from ``config.yaml``."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

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


class DatabaseConfig(BaseModel):
    """Location of the SQLite database (relative paths resolve against the config file)."""

    model_config = _STRICT
    path: Path


class ViewerConfig(BaseModel):
    """Viewer visibility scope and UI thresholds."""

    model_config = _STRICT
    scope: ViewerScope
    min_reports_for_chip: int = Field(ge=1)


class MapConfig(BaseModel):
    """Privacy controls for the coarse region map."""

    model_config = _STRICT
    public_min_cell_count: int = Field(ge=1)


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


class OutsideLink(BaseModel):
    """An opt-in outbound context resource, labelled by evidence type."""

    model_config = _STRICT
    title: str = Field(min_length=1)
    url: str = Field(pattern=r"^https://")
    label: ContextLabel
    note: str


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
