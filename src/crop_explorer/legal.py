"""The privacy policy and terms of use pages: packaged Markdown, pure loaders.

The YouTube API Services Developer Policies require an API client to link to the YouTube Terms
of Service, state that its users agree to be bound by them (III.A.1), and show a privacy
policy that is easy to reach at all times (III.A.2). The texts are trusted, fixed Markdown
files shipped with the package (``data/legal``); the viewer shows a link to both on every page
and renders one of them instead of the Browse screen when the URL asks for it
(``?page=privacy`` or ``?page=terms``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal, get_args

LegalPage = Literal["privacy", "terms"]
LEGAL_PAGES: Final[tuple[LegalPage, ...]] = get_args(LegalPage)
LEGAL_TITLES: Final[dict[LegalPage, str]] = {
    "privacy": "Privacy policy",
    "terms": "Terms of use",
}
PAGE_PARAM: Final = "page"
"""The URL query parameter that selects a legal page."""
LEGAL_DIR: Final = Path(__file__).resolve().parent / "data" / "legal"
YOUTUBE_TERMS_URL: Final = "https://www.youtube.com/t/terms"
GOOGLE_PRIVACY_URL: Final = "https://policies.google.com/privacy"
_SHOWN_CHARS: Final = 20
"""How much of an unknown page value an error message repeats."""


def page_from_param(value: str | None) -> LegalPage | None:
    """Return the legal page a ``?page=`` value names, or ``None`` when there is no value.

    Raises
    ------
    ValueError
        If the value names no legal page (only its first characters are repeated).
    """
    if value is None:
        return None
    for page in LEGAL_PAGES:
        if value == page:
            return page
    raise ValueError(f"Unknown page {value[:_SHOWN_CHARS]!r}: choose one of {list(LEGAL_PAGES)}")


def legal_text(page: LegalPage) -> str:
    """Return the page's Markdown, read from the packaged file, without surrounding blank lines."""
    return (LEGAL_DIR / f"{page}.md").read_text(encoding="utf-8").strip()


def page_href(page: LegalPage) -> str:
    """Return the relative link that opens ``page`` in the viewer."""
    return f"?{PAGE_PARAM}={page}"


def footer_markdown() -> str:
    """Return the line shown on every page: both links and what using the site means."""
    privacy, terms = (f"[{LEGAL_TITLES[p]}]({page_href(p)})" for p in ("privacy", "terms"))
    return f"By using this site you agree to its {terms} and {privacy}."
