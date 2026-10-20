"""Tests of the privacy policy and terms of use pages (``crop_explorer.legal``).

The texts must carry what the YouTube API Services Developer Policies require of an API client
(III.A.1: a link to the YouTube Terms of Service and a statement that users agree to be bound by
them; III.A.2: a privacy policy that says the client uses YouTube API Services, links to the
Google Privacy Policy and explains what information is accessed, used, stored and shared).
"""

from __future__ import annotations

import re

import pytest

from crop_explorer import legal

LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def links(page: legal.LegalPage) -> dict[str, str]:
    return {label: url for label, url in LINK.findall(legal.legal_text(page))}


@pytest.mark.parametrize("page", legal.LEGAL_PAGES)
def test_every_page_has_a_title_and_text(page: legal.LegalPage) -> None:
    text = legal.legal_text(page)
    assert text.startswith(f"## {legal.LEGAL_TITLES[page]}\n")
    assert "Last updated:" in text


def test_privacy_policy_meets_youtube_requirements() -> None:
    text = legal.legal_text("privacy")
    assert "YouTube API Services" in text
    assert legal.GOOGLE_PRIVACY_URL in links("privacy").values()
    for topic in ("What is collected", "How it is used", "Who it is shared with"):
        assert topic in text
    assert "30 days" in text


def test_terms_bind_users_to_youtube_terms() -> None:
    assert legal.YOUTUBE_TERMS_URL in links("terms").values()
    assert "agree to be bound by" in legal.legal_text("terms")


@pytest.mark.parametrize("page", legal.LEGAL_PAGES)
def test_links_are_https_or_the_other_legal_page(page: legal.LegalPage) -> None:
    internal = {legal.page_href(other) for other in legal.LEGAL_PAGES}
    for url in links(page).values():
        assert url.startswith("https://") or url in internal


def test_page_from_param() -> None:
    assert legal.page_from_param(None) is None
    assert legal.page_from_param("privacy") == "privacy"
    assert legal.page_from_param("terms") == "terms"


@pytest.mark.parametrize("value", ["", "Privacy", "about", "../privacy", "terms "])
def test_unknown_page_param_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="Unknown page"):
        legal.page_from_param(value)


def test_unknown_page_message_does_not_repeat_long_input() -> None:
    with pytest.raises(ValueError) as info:
        legal.page_from_param("x" * 500)
    assert "x" * 50 not in str(info.value)


def test_footer_links_both_pages() -> None:
    footer = legal.footer_markdown()
    found = dict(LINK.findall(footer))
    assert found == {legal.LEGAL_TITLES[page]: legal.page_href(page) for page in legal.LEGAL_PAGES}
    assert "you agree" in footer
