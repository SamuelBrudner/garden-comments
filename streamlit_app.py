"""Hosted public demo of the viewer: the Streamlit Community Cloud entrypoint.

Community Cloud runs ``streamlit run streamlit_app.py`` from the repository root and cannot pass
arguments to the script, so this entrypoint takes none: it ignores the command line and always
shows the bundled synthetic comments, in public scope. Once per server process it builds a fresh
demo database from ``fixtures/`` in a new temporary directory, next to a configuration file that
points only at that database and writes no JSON log file
(:func:`crop_explorer.demo.prepare_hosted_demo`). Every script run then hands the normal viewer
(:func:`app.main`) ``--scope public`` and those two files. Nothing here opens ``data/`` or any
other database, so a private import can never be served from this entrypoint. Only the location
of the built files is cached (``st.cache_resource``); query results never are.

Community Cloud may install the third-party dependencies without installing this project, so
this entrypoint always imports the package from the checkout: it puts ``<repo>/src`` (and the
repository root, for ``app``) on ``sys.path`` explicitly. This is how the hosted entrypoint
locates the package, not a fallback.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Final


def _repo_root(start: Path) -> Path:
    """Return the checkout root: the nearest ancestor of ``start`` holding ``src/crop_explorer``.

    Raises
    ------
    RuntimeError
        If no ancestor holds the package (the entrypoint was copied out of the checkout).
    """
    for parent in start.resolve().parents:
        if (parent / "src" / "crop_explorer").is_dir():
            return parent
    raise RuntimeError(f"No src/crop_explorer found above {start}")


_ROOT: Final = _repo_root(Path(__file__))
for _entry in (str(_ROOT), str(_ROOT / "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import app  # noqa: E402
import streamlit as st  # noqa: E402

from crop_explorer.config import DEFAULT_CONFIG_PATH, load_config  # noqa: E402
from crop_explorer.demo import HostedDemo, prepare_hosted_demo  # noqa: E402
from crop_explorer.schemas import utc_now_iso  # noqa: E402

_TEMP_PREFIX: Final = "crop-explorer-demo-"


@st.cache_resource(show_spinner="Preparing the demo comments...")
def _hosted_demo() -> HostedDemo:
    """Build the demo in a fresh temporary directory, once per server process."""
    directory = Path(tempfile.mkdtemp(prefix=_TEMP_PREFIX))
    return prepare_hosted_demo(directory, load_config(DEFAULT_CONFIG_PATH), utc_now_iso())


def main() -> None:
    """Run the viewer in public scope against the hosted demo, ignoring the command line."""
    demo = _hosted_demo()
    app.main(["--scope", "public", "--db", str(demo.database), "--config", str(demo.config)])


if __name__ == "__main__":
    main()
