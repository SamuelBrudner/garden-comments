"""End-to-end tests of the hosted public demo entrypoint (``streamlit_app.py``) with AppTest.

Streamlit Community Cloud runs ``streamlit run streamlit_app.py`` from the repository root and
cannot pass script arguments. The entrypoint must show the bundled synthetic comments only: it
builds its own demo database once per server process in a fresh temporary directory and runs
the normal viewer in public scope against it, whatever the configuration, the command line or
an existing database say. Every test runs from an empty working directory with its own
temporary directory and an empty ``st.cache_resource``.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import streamlit as st
import yaml
from loguru import logger
from streamlit import config as streamlit_config
from streamlit.testing.v1 import AppTest

from crop_explorer import config as config_module
from crop_explorer import db, ui

if TYPE_CHECKING:
    from conftest import SourceImporter

REPO = Path(__file__).resolve().parents[1]
HOSTED = REPO / "streamlit_app.py"
VIEWER = REPO / "app.py"
TIMEOUT = 60
PRIVATE_ID = "private-0001"
PRIVATE_SOURCE = "my-private-export"
PRIVATE_TEXT = "PRIVATEONLY Brandywine grown in containers in zone 6b near Hartford, CT."
DEMO_HEADING = "Reports (69)"


@dataclass(frozen=True)
class Sandbox:
    """Where a test may write: an empty working directory and the process's temp directory."""

    cwd: Path
    temp: Path


def _write_stderr(message: str) -> None:
    sys.stderr.write(message)  # resolved per call, so never a capture stream pytest closed


@pytest.fixture(autouse=True)
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Sandbox]:
    """Isolate cwd, ``tempfile`` and argv; start and end with an empty resource cache."""
    box = Sandbox(cwd=tmp_path / "cwd", temp=tmp_path / "tmp")
    box.cwd.mkdir()
    box.temp.mkdir()
    monkeypatch.chdir(box.cwd)
    monkeypatch.setattr(tempfile, "tempdir", str(box.temp))
    monkeypatch.setattr(sys, "argv", ["streamlit_app.py"])
    address = streamlit_config.get_option("server.address")
    st.cache_resource.clear()
    yield box
    st.cache_resource.clear()
    streamlit_config.set_option("server.address", address)
    logger.remove()
    logger.add(_write_stderr, level="DEBUG")


def run_hosted() -> AppTest:
    return AppTest.from_file(str(HOSTED), default_timeout=TIMEOUT).run()


def all_text(at: AppTest) -> str:
    """Every Markdown, caption, alert, heading, expander and widget text on the page."""
    kinds = ("markdown", "caption", "info", "warning", "error", "success", "subheader", "title")
    values = [str(element.value) for kind in kinds for element in getattr(at, kind)]
    values += [expander.label for expander in at.expander]
    values += [w.label for kind in ("selectbox", "button") for w in getattr(at, kind)]
    values += [option for box in at.selectbox for option in box.options]
    return "\n".join(values)


def subheaders(at: AppTest) -> list[str]:
    return [element.value for element in at.subheader]


def assert_public_demo(at: AppTest) -> None:
    assert not at.exception
    assert not at.error
    assert [title.value for title in at.title] == ["What Gardeners Are Saying"]
    assert ui.DEMO_BANNER in [info.value for info in at.info]
    assert ui.PRIVATE_BANNER not in [warning.value for warning in at.warning]
    assert subheaders(at)[-1] == DEMO_HEADING


def demo_databases(temp: Path) -> list[Path]:
    return sorted(temp.rglob("*.sqlite"))


def repo_snapshot() -> dict[str, int]:
    """Names and sizes of the files at the repository root and under ``data/``."""
    files = [*REPO.glob("*"), *(REPO / "data").rglob("*")]
    return {str(path): path.stat().st_size for path in files if path.is_file()}


# --------------------------------------------------------------------------- hosted demo


def test_renders_the_synthetic_demo_in_public_scope() -> None:
    at = run_hosted()

    assert_public_demo(at)
    assert at.text_input(key=ui.TEXT_KEY).label == (
        "Search crops, varieties, or anything people mentioned"
    )
    assert [tab.label for tab in at.tabs] == ["Comments", "Map"]


def test_search_reruns_against_the_demo_database() -> None:
    at = run_hosted()
    at.text_input(key=ui.TEXT_KEY).input("kids watch").run()

    assert not at.exception
    assert "1 report" in [caption.value for caption in at.caption]


def test_builds_its_database_in_a_fresh_temporary_directory(sandbox: Sandbox) -> None:
    before = repo_snapshot()
    run_hosted()

    (built,) = demo_databases(sandbox.temp)
    assert built.name == "demo.sqlite"
    assert built.parent.parent == sandbox.temp
    assert built.parent.name.startswith("crop-explorer-demo-")
    assert not built.resolve().is_relative_to(REPO)
    assert list(sandbox.cwd.iterdir()) == []
    assert repo_snapshot() == before
    with closing(db.connect(built)) as conn:
        rows = conn.execute("SELECT source_id, rights_status FROM sources").fetchall()
    assert [tuple(row) for row in rows] == [("synthetic-demo", "synthetic")]


def test_builds_the_database_once_per_server_process(sandbox: Sandbox) -> None:
    first = run_hosted()
    second = run_hosted()
    first.text_input(key=ui.TEXT_KEY).input("tomato").run()

    assert_public_demo(second)
    assert not first.exception
    assert len(demo_databases(sandbox.temp)) == 1


# --------------------------------------------------------------------------- user data


@dataclass(frozen=True)
class UserSetup:
    """A user's private database and a configuration that points at it in private scope."""

    config: Path
    db_path: Path


@pytest.fixture
def user_setup(tmp_path: Path, add_private_source: SourceImporter) -> UserSetup:
    root = tmp_path / "user"
    root.mkdir()
    db_path = root / "crop_explorer.sqlite"
    with closing(db.connect(db_path)) as conn:
        add_private_source(
            conn,
            source_id=PRIVATE_SOURCE,
            comments=[{"comment_id": PRIVATE_ID, "text": PRIVATE_TEXT}],
        )
    raw: dict[str, Any] = yaml.safe_load(
        config_module.DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    )
    raw["database"]["path"] = str(db_path)
    raw["viewer"]["scope"] = "private"
    raw["logging"] = {"level": "WARNING", "json_path": None}
    config = root / "config.yaml"
    config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return UserSetup(config=config, db_path=db_path)


def leaks_private_data(at: AppTest) -> bool:
    text = all_text(at)
    return "PRIVATEONLY" in text or ui.escape_markdown(PRIVATE_SOURCE) in text


def test_the_user_setup_would_show_private_text_in_the_normal_viewer(
    monkeypatch: pytest.MonkeyPatch, user_setup: UserSetup
) -> None:
    """Control: without the hosted entrypoint, this configuration shows the private comment."""
    streamlit_config.set_option("server.address", "localhost")
    monkeypatch.setattr(sys, "argv", ["app.py", "--config", str(user_setup.config)])
    at = AppTest.from_file(str(VIEWER), default_timeout=TIMEOUT).run()

    assert not at.exception
    assert leaks_private_data(at)


def test_ignores_a_private_database_at_the_configured_path(
    monkeypatch: pytest.MonkeyPatch, user_setup: UserSetup
) -> None:
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", user_setup.config)
    streamlit_config.set_option("server.address", "localhost")
    before = user_setup.db_path.read_bytes()
    at = run_hosted()

    assert_public_demo(at)
    assert not leaks_private_data(at)
    assert user_setup.db_path.read_bytes() == before
    assert sorted(path.name for path in user_setup.db_path.parent.iterdir()) == [
        "config.yaml",
        "crop_explorer.sqlite",
    ]


def test_ignores_command_line_arguments(
    monkeypatch: pytest.MonkeyPatch, user_setup: UserSetup
) -> None:
    streamlit_config.set_option("server.address", "localhost")
    argv = ["--scope", "private", "--db", str(user_setup.db_path), "--config"]
    monkeypatch.setattr(sys, "argv", ["streamlit_app.py", *argv, str(user_setup.config)])
    at = run_hosted()

    assert_public_demo(at)
    assert not leaks_private_data(at)


def test_a_private_comment_in_the_built_database_would_still_stay_hidden(
    sandbox: Sandbox, add_private_source: SourceImporter
) -> None:
    """Public scope is forced too: even a private row slipped into the demo database is hidden."""
    run_hosted()
    (built,) = demo_databases(sandbox.temp)
    with closing(db.connect(built)) as conn:
        add_private_source(
            conn,
            source_id=PRIVATE_SOURCE,
            comments=[{"comment_id": PRIVATE_ID, "text": PRIVATE_TEXT}],
        )
    at = run_hosted()

    assert_public_demo(at)
    assert not leaks_private_data(at)


def _load_entrypoint() -> Any:
    spec = importlib.util.spec_from_file_location("hosted_entrypoint", HOSTED)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repo_root_is_found_from_any_folder_depth(tmp_path: Path) -> None:
    (tmp_path / "src" / "crop_explorer").mkdir(parents=True)
    nested = tmp_path / "hosted" / "deeper" / "streamlit_app.py"
    assert _load_entrypoint()._repo_root(nested) == tmp_path


def test_repo_root_outside_a_checkout_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="src/crop_explorer"):
        _load_entrypoint()._repo_root(tmp_path / "elsewhere" / "streamlit_app.py")
