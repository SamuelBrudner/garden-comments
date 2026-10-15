"""Unit tests for the pure viewer helpers in ``crop_explorer.ui``.

Covers SPEC §2 (Browse screen copy, chips, grouping counts, strict toggle), §4/§11 #18
(provenance: every evidence chip carries its exact quote and is labelled a community report),
§6 (map notes, state selector, suppressed counts), §9 (scope banners) and §11 #17 (resets).
Comment text is untrusted: :func:`escape_markdown` must make every Markdown/HTML construct in it
render literally, which the Hypothesis properties check.
"""

from __future__ import annotations

import re
import string
from collections.abc import Sequence
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from crop_explorer import ui
from crop_explorer.config import DEFAULT_CONFIG_PATH, OutsideLink, QuickChip
from crop_explorer.geography import RegionCounts, region_counts
from crop_explorer.regions import CA_PROVINCES, COUNTRIES, REGION_NAMES, US_STATES
from crop_explorer.schemas import Applicability, Basis, EnvKey, RightsStatus, Topic
from crop_explorer.search import FilterOptions, build_result
from crop_explorer.views import (
    CommentResult,
    CommentView,
    FactView,
    GardenContext,
    MentionView,
    SearchQuery,
    SearchResult,
)
from crop_explorer.vocab import (
    COMPARABLE_KEYS,
    KEY_LABELS,
    VIEWER_VALUES,
    ComparableKey,
    is_valid_normalized,
)

# --------------------------------------------------------------------------- builders


def fact(
    key: EnvKey,
    value: str | None,
    written: str | None = None,
    *,
    fact_id: str = "c1#m0#f0",
    applicability: Applicability = "this_mention",
    basis: Basis = "reported_garden_context",
    quote: str | None = None,
) -> FactView:
    as_written = written or value or "somewhere"
    return FactView(
        fact_id=fact_id,
        key=key,
        value_as_written=as_written,
        normalized_value=value,
        evidence_quote=quote or f"quote for {as_written}",
        applicability=applicability,
        basis=basis,
    )


def mention(
    mention_id: str = "c1#m0",
    *,
    crop: str | None = "tomato",
    variety: str | None = "Sungold",
    topics: Sequence[Topic] = (),
    facts: Sequence[FactView] = (),
) -> MentionView:
    return MentionView(
        mention_id=mention_id,
        crop=crop,
        variety_as_written=variety,
        variety_key=None if variety is None else variety.casefold(),
        topics=tuple(topics),
        evidence_quote=variety or crop or "it",
        extractor_version="test-v1",
        facts=tuple(facts),
    )


def comment(
    comment_id: str = "c1",
    *,
    text: str = "Some comment text.",
    mentions: Sequence[MentionView] = (),
    permalink: str | None = None,
    rights: RightsStatus = "synthetic",
    source_id: str = "demo",
    source_kind: str = "synthetic_fixture",
    created_at: str | None = None,
) -> CommentView:
    return CommentView(
        comment_id=comment_id,
        source_id=source_id,
        source_kind=source_kind,
        rights_status=rights,
        text=text,
        permalink=permalink,
        source_order=0,
        created_at=created_at,
        parse_status=None if not mentions else "parsed",
        mentions=tuple(mentions),
    )


def result_of(view: CommentView, query: SearchQuery | None = None) -> CommentResult:
    found = build_result([view], query or SearchQuery())
    (group,) = found.groups
    (only,) = group.results
    return only


def search_of(views: Sequence[CommentView], query: SearchQuery) -> SearchResult:
    return build_result(views, query)


GARDEN_6B_POTS = GardenContext(zone="6b", method="container")
QUERY_6B_POTS = SearchQuery(garden=GARDEN_6B_POTS)


# --------------------------------------------------------------------------- escape_markdown

_PUNCT = frozenset(string.punctuation)
_UNESCAPED_LINK = re.compile(r"(?<!\\)\[[^\]\n]*(?<!\\)\]\(")
_UNESCAPED_IMAGE = re.compile(r"(?<!\\)!(?<!\\)\[")
_UNESCAPED_TAG = re.compile(r"(?<!\\)<[A-Za-z/!?]")
_SHORTCODE = re.compile(r":[A-Za-z0-9_+-]+:")
# GFM autolink literals are found in text *after* escapes are resolved, so they must be broken
# by the escaped output itself, not by backslashes.
_AUTOLINK_LITERAL = re.compile(r"https?://|www\.|[-.\w+]+@[-\w]+(?:\.[-\w]+)+", re.IGNORECASE)


def autolink_literals(rendered: str) -> list[str]:
    """Return autolink-literal matches in the text a Markdown parser sees after unescaping."""
    return _AUTOLINK_LITERAL.findall(re.sub(r"\\(.)", r"\1", rendered))


def bare_punctuation(rendered: str) -> list[str]:
    """Return ASCII punctuation characters not protected by a CommonMark backslash escape."""
    bare: list[str] = []
    index = 0
    while index < len(rendered):
        char = rendered[index]
        if char == "\\" and index + 1 < len(rendered) and rendered[index + 1] in _PUNCT:
            index += 2
            continue
        if char in _PUNCT:
            bare.append(char)
        index += 1
    return bare


def unescape(rendered: str) -> str:
    """Invert :func:`ui.escape_markdown` for text without tabs, CRs or leading spaces."""
    text = rendered.replace("  \n", "\n").replace(ui.WORD_JOINER, "")
    return re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", text)


class TestEscapeMarkdown:
    @pytest.mark.parametrize(
        "text",
        [
            "[click me](https://evil.example)",
            "![pic](https://evil.example/x.png)",
            "<script>alert(1)</script>",
            "<b>bold</b> &amp; <img src=x onerror=alert(1)>",
            "# Heading\n## Another",
            "> quote\n- item\n1. first\n***\n---",
            "`code` and ```fenced```",
            "*em* _em_ **strong** ~~strike~~ | table | cell |",
            "$x^2$ and $$y$$",
            ":red[coloured] :material/home: :smile:",
            "www.evil.example and https://evil.example and mail@evil.example",
            "Trailing backslash \\",
        ],
    )
    def test_markdown_constructs_are_fully_escaped(self, text: str) -> None:
        rendered = ui.escape_markdown(text)
        assert bare_punctuation(rendered) == []
        assert not _UNESCAPED_LINK.search(rendered)
        assert not _UNESCAPED_IMAGE.search(rendered)
        assert not _UNESCAPED_TAG.search(rendered)
        assert unescape(rendered) == text

    def test_link_is_escaped_character_by_character(self) -> None:
        assert ui.escape_markdown("[a](b)") == "\\[a\\]\\(b\\)"

    def test_heading_marker_is_escaped(self) -> None:
        assert ui.escape_markdown("# Heading").startswith("\\# ")

    def test_emoji_and_icon_shortcodes_cannot_form(self) -> None:
        rendered = ui.escape_markdown("so good :smile: :material/home: :+1:")
        assert not _SHORTCODE.search(rendered.replace("\\", ""))

    @pytest.mark.parametrize(
        "text",
        ["see www.evil.example", "https://evil.example/x", "HTTP://EVIL.EXAMPLE", "a@b.example"],
    )
    def test_autolink_literals_cannot_form(self, text: str) -> None:
        rendered = ui.escape_markdown(text)
        assert autolink_literals(rendered) == []
        assert unescape(rendered) == text

    def test_line_breaks_are_kept_as_hard_breaks(self) -> None:
        assert ui.escape_markdown("one\ntwo") == "one  \ntwo"
        assert ui.escape_markdown("one\r\ntwo\rthree") == "one  \ntwo  \nthree"

    def test_blank_line_keeps_paragraph_break(self) -> None:
        assert ui.escape_markdown("one\n\ntwo") == "one  \n  \ntwo"

    def test_leading_indent_cannot_start_a_code_block(self) -> None:
        rendered = ui.escape_markdown("    indented code\n\tand tab")
        lines = rendered.split("  \n")
        assert lines[0] == "\u00a0" * 4 + "indented code"
        assert lines[1] == "\u00a0and tab"
        assert all(not line.startswith((" ", "\t")) for line in lines)

    def test_plain_words_are_unchanged(self) -> None:
        assert ui.escape_markdown("Sungold in pots") == "Sungold in pots"

    @settings(max_examples=300)
    @given(st.text())
    def test_property_no_unescaped_markdown_or_html(self, text: str) -> None:
        rendered = ui.escape_markdown(text)
        assert bare_punctuation(rendered) == []
        assert not _UNESCAPED_LINK.search(rendered)
        assert not _UNESCAPED_IMAGE.search(rendered)
        assert not _UNESCAPED_TAG.search(rendered)
        assert not _SHORTCODE.search(rendered.replace("\\", ""))
        assert autolink_literals(rendered) == []

    @settings(max_examples=300)
    @given(
        st.text(
            alphabet=st.characters(
                blacklist_characters="\r\t\u00a0\u2060", blacklist_categories=("Cs",)
            )
        ).filter(lambda t: all(not line.startswith(" ") for line in t.split("\n")))
    )
    def test_property_round_trips_to_the_original_text(self, text: str) -> None:
        assert unescape(ui.escape_markdown(text)) == text

    @given(st.lists(st.sampled_from(["[", "]", "(", ")", "!", "<", ">", "a", "b", "\\", "/"])))
    def test_property_link_shaped_inputs_stay_inert(self, chars: list[str]) -> None:
        rendered = ui.escape_markdown("".join(chars))
        assert bare_punctuation(rendered) == []


# --------------------------------------------------------------------------- card_title


class TestCardTitle:
    def test_variety_crop_and_topics(self) -> None:
        view = comment(mentions=[mention(topics=["productivity"])])
        assert ui.card_title(result_of(view)) == "Sungold tomato · productivity"

    def test_topic_labels_are_readable_and_deduplicated(self) -> None:
        view = comment(
            mentions=[
                mention("c1#m0", topics=["weather_tolerance", "flavor"]),
                mention("c1#m1", variety="Juliet", topics=["flavor", "growing_method"]),
            ]
        )
        assert ui.card_title(result_of(view)) == (
            "Sungold tomato, Juliet tomato · weather tolerance, flavor, growing method"
        )

    def test_crop_only_mention_is_capitalised(self) -> None:
        view = comment(mentions=[mention(crop="zucchini", variety=None)])
        assert ui.card_title(result_of(view)) == "Zucchini"

    def test_same_name_twice_is_listed_once(self) -> None:
        view = comment(mentions=[mention("c1#m0"), mention("c1#m1")])
        assert ui.card_title(result_of(view)) == "Sungold tomato"

    def test_comment_without_mentions_falls_back(self) -> None:
        assert ui.card_title(result_of(comment())) == "Comment"

    def test_mention_without_identity_keeps_topics(self) -> None:
        view = comment(mentions=[mention(crop=None, variety=None, topics=["pests"])])
        assert ui.card_title(result_of(view)) == "Comment · pests"

    def test_only_relevant_mentions_are_named(self) -> None:
        view = comment(
            mentions=[mention("c1#m0", crop="pepper", variety="Jalapeño"), mention("c1#m1")]
        )
        found = result_of(view, SearchQuery(crop="tomato"))
        assert ui.card_title(found) == "Sungold tomato"


# --------------------------------------------------------------------------- condition_lines


def full_match_view() -> CommentView:
    return comment(
        mentions=[
            mention(
                facts=[
                    fact("zone", "6b", "Zone 6b", fact_id="c1#m0#f0"),
                    fact("method", "container", "containers", fact_id="c1#m0#f1"),
                ]
            )
        ]
    )


class TestConditionLines:
    def test_full_match_lists_relevant_conditions_only(self) -> None:
        lines = ui.condition_lines(result_of(full_match_view(), QUERY_6B_POTS), GARDEN_6B_POTS)
        assert lines == ("Relevant: zone 6b · containers",)

    def test_partial_match_lists_what_was_not_stated(self) -> None:
        view = comment(mentions=[mention(facts=[fact("method", "container", "pots")])])
        lines = ui.condition_lines(result_of(view, QUERY_6B_POTS), GARDEN_6B_POTS)
        assert lines == ("Relevant: containers", "Not stated: hardiness zone")

    def test_coarse_zone_is_shown_as_imprecise(self) -> None:
        view = comment(mentions=[mention(facts=[fact("zone", "6", "Zone 6")])])
        lines = ui.condition_lines(result_of(view, QUERY_6B_POTS), GARDEN_6B_POTS)
        assert lines == (
            "Relevant: zone 6, subzone unknown (you: zone 6b)",
            "Not stated: growing method",
        )

    def test_difference_names_both_values(self) -> None:
        view = comment(mentions=[mention(facts=[fact("method", "in_ground", "in the ground")])])
        lines = ui.condition_lines(result_of(view, QUERY_6B_POTS), GARDEN_6B_POTS)
        assert lines == (
            "Not stated: hardiness zone",
            "Different: in the ground (you: containers)",
        )

    def test_heatwave_against_mild_summers_is_not_comparable(self) -> None:
        garden = GardenContext(summer_heat="mild")
        view = comment(mentions=[mention(facts=[fact("summer_heat", "heatwave", "heat wave")])])
        lines = ui.condition_lines(result_of(view, SearchQuery(garden=garden)), garden)
        assert lines == ("Not stated: summer heat (only a heatwave reported, not comparable)",)

    def test_comment_without_mentions_states_every_selected_condition_as_unknown(self) -> None:
        lines = ui.condition_lines(result_of(comment(), QUERY_6B_POTS), GARDEN_6B_POTS)
        assert lines == ("Not stated: hardiness zone, growing method",)

    def test_without_selection_lists_reported_conditions(self) -> None:
        lines = ui.condition_lines(result_of(full_match_view()), GardenContext())
        assert lines == ("Reported: zone 6b · containers",)

    def test_without_selection_and_without_facts(self) -> None:
        view = comment(mentions=[mention()])
        assert ui.condition_lines(result_of(view), GardenContext()) == (
            "Growing conditions not stated",
        )

    def test_region_selection_alone_is_not_a_condition_comparison(self) -> None:
        garden = GardenContext(region="US-CT")
        lines = ui.condition_lines(
            result_of(full_match_view(), SearchQuery(garden=garden)), garden
        )
        assert lines == ("Reported: zone 6b · containers",)

    def test_reported_lines_name_each_mention_when_they_differ(self) -> None:
        view = comment(
            mentions=[
                mention(
                    "c1#m0",
                    crop="pepper",
                    variety="Jalapeño",
                    facts=[fact("shelter", "greenhouse")],
                ),
                mention("c1#m1", variety="Roma", facts=[fact("shelter", "outdoors", fact_id="x")]),
                mention("c1#m2", variety="Juliet"),
            ]
        )
        assert ui.condition_lines(result_of(view), GardenContext()) == (
            "Reported for Jalapeño: greenhouse / tunnel",
            "Reported for Roma: outdoors",
        )

    def test_shared_conditions_are_reported_once(self) -> None:
        shared = fact("method", "container", "grow bags", applicability="whole_comment")
        view = comment(
            mentions=[mention("c1#m0", facts=[shared]), mention("c1#m1", variety="Juliet")]
        )
        assert ui.condition_lines(result_of(view), GardenContext()) == (
            "Reported for Sungold, Juliet: containers",
        )

    def test_multiple_mentions_say_which_one_is_shown(self) -> None:
        view = comment(
            mentions=[
                mention("c1#m0", variety="Roma", facts=[fact("method", "in_ground", fact_id="a")]),
                mention(
                    "c1#m1", variety="Juliet", facts=[fact("method", "container", fact_id="b")]
                ),
            ]
        )
        lines = ui.condition_lines(result_of(view, QUERY_6B_POTS), GARDEN_6B_POTS)
        assert lines == (
            "Shown for Juliet (1 of 2 mentions)",
            "Relevant: containers",
            "Not stated: hardiness zone",
        )

    @settings(max_examples=200)
    @given(
        st.dictionaries(
            st.sampled_from(COMPARABLE_KEYS),
            st.sampled_from(["6b", "6", "7a", "container", "in_ground", "hot", "heatwave"]),
        ),
        st.dictionaries(
            st.sampled_from(COMPARABLE_KEYS), st.sampled_from(["6b", "container", "hot", "mild"])
        ),
    )
    def test_property_never_says_match_for_unstated_conditions(
        self, reported: dict[ComparableKey, str], selected: dict[ComparableKey, str]
    ) -> None:
        facts = [
            fact(key, value, fact_id=f"c1#m0#f{i}")
            for i, (key, value) in enumerate(reported.items())
            if is_valid_normalized(key, value)
        ]
        garden_values = {k: v for k, v in selected.items() if v in VIEWER_VALUES[k]}
        garden = GardenContext.model_validate(garden_values)
        found = result_of(comment(mentions=[mention(facts=facts)]), SearchQuery(garden=garden))
        lines = ui.condition_lines(found, garden)
        assert lines
        assert all("match" not in line.casefold() for line in lines)
        relevant = next((line for line in lines if line.startswith("Relevant:")), "")
        unstated = {key for key in garden_values if key not in {f.key for f in facts}}
        for key in unstated:
            assert KEY_LABELS[key] in next(
                line for line in lines if line.startswith("Not stated:")
            )
            assert KEY_LABELS[key] not in relevant


# --------------------------------------------------------------------------- region_line


class TestRegionLine:
    def view_in(self, *regions: tuple[str | None, str]) -> CommentView:
        facts = [
            fact("region", code, written, fact_id=f"c1#m0#f{i}")
            for i, (code, written) in enumerate(regions)
        ]
        return comment(mentions=[mention(facts=facts)])

    def test_reported_region_is_named(self) -> None:
        assert ui.region_line(result_of(self.view_in(("US-VT", "Vermont")))) == "Region: Vermont"

    def test_same_region_as_viewer_is_flagged(self) -> None:
        query = SearchQuery(garden=GardenContext(region="US-CT"))
        found = result_of(self.view_in(("US-CT", "Hartford, CT")), query)
        assert ui.region_line(found) == "Region: Connecticut (same as yours)"

    def test_display_only_region_is_marked_unmapped(self) -> None:
        line = ui.region_line(result_of(self.view_in((None, "Cape Cod"))))
        assert line == "Region as written: Cape Cod (not mapped)"

    def test_missing_region(self) -> None:
        assert ui.region_line(result_of(comment(mentions=[mention()]))) == "Region not reported"

    def test_zone_never_produces_a_region(self) -> None:
        view = comment(mentions=[mention(facts=[fact("zone", "6b", "zone 6b")])])
        assert ui.region_line(result_of(view)) == "Region not reported"

    def test_two_regions_are_both_named_once(self) -> None:
        view = comment(
            mentions=[
                mention("c1#m0", facts=[fact("region", "US-CT", "Connecticut", fact_id="a")]),
                mention(
                    "c1#m1",
                    variety="Juliet",
                    facts=[fact("region", "US-ME", "Maine", fact_id="b")],
                ),
                mention(
                    "c1#m2", variety="Roma", facts=[fact("region", "US-CT", "CT", fact_id="c")]
                ),
            ]
        )
        assert ui.region_line(result_of(view)) == "Region: Connecticut, Maine"


# --------------------------------------------------------------------------- evidence_rows


class TestEvidenceRows:
    def test_every_row_carries_its_exact_quote_and_report_label(self) -> None:
        zone = fact("zone", "6b", "Zone 6b", quote="Zone 6b here", fact_id="c1#m0#f0")
        pots = fact("method", "container", "containers", quote="in containers", fact_id="c1#m0#f1")
        rows = ui.evidence_rows(result_of(comment(mentions=[mention(facts=[zone, pots])])))
        assert [row.quote for row in rows] == ["Zone 6b here", "in containers"]
        assert {row.label for row in rows} == {"community report · reported conditions"}
        assert [row.chip for row in rows] == ["zone 6b", "containers"]
        assert [row.key_label for row in rows] == ["hardiness zone", "growing method"]

    def test_basis_is_described(self) -> None:
        rain = fact("precipitation", "rainy", "rained for weeks", basis="observed_experience")
        (row,) = ui.evidence_rows(result_of(comment(mentions=[mention(facts=[rain])])))
        assert row.basis == "observed that season"
        clay = fact("soil", "clay", "clay soil", basis="reported_garden_context")
        (row,) = ui.evidence_rows(result_of(comment(mentions=[mention(facts=[clay])])))
        assert row.basis == "garden context"

    def test_inherited_whole_comment_fact_is_listed_once(self) -> None:
        shared = fact("region", "US-CT", "Connecticut", applicability="whole_comment")
        view = comment(
            mentions=[mention("c1#m0", facts=[shared]), mention("c1#m1", variety="Juliet")]
        )
        (row,) = ui.evidence_rows(result_of(view))
        assert row.applies_to == "whole comment"
        assert row.chip == "Connecticut"

    def test_this_mention_fact_names_its_mention(self) -> None:
        view = comment(
            mentions=[
                mention(
                    "c1#m0",
                    crop="pepper",
                    variety="Jalapeño",
                    facts=[fact("shelter", "greenhouse")],
                ),
                mention("c1#m1", variety="Roma"),
            ]
        )
        (row,) = ui.evidence_rows(result_of(view))
        assert row.applies_to == "Jalapeño"

    def test_display_only_fact_shows_the_words_as_written(self) -> None:
        dirt = fact("soil", None, "rocky hillside dirt", quote="rocky hillside dirt")
        (row,) = ui.evidence_rows(result_of(comment(mentions=[mention(facts=[dirt])])))
        assert row.chip == "rocky hillside dirt (as written)"

    def test_no_facts_no_rows(self) -> None:
        assert ui.evidence_rows(result_of(comment(mentions=[mention()]))) == ()


# --------------------------------------------------------------------------- banners and sources


class TestScopeBanner:
    def test_private_scope_always_warns(self) -> None:
        assert ui.scope_banner("private", {"synthetic"}) == (
            "Private local view — includes unverified imported comments. Do not publish or share."
        )

    def test_public_synthetic_only_is_labelled_demo(self) -> None:
        assert ui.scope_banner("public", {"synthetic"}) == (
            "Demo data: synthetic comments, not real gardener reports"
        )

    def test_public_mixed_mentions_the_demo_comments(self) -> None:
        banner = ui.scope_banner("public", {"synthetic", "cleared"})
        assert banner is not None
        assert "synthetic demo comments" in banner

    def test_public_cleared_only_needs_no_banner(self) -> None:
        assert ui.scope_banner("public", {"cleared"}) is None
        assert ui.scope_banner("public", set()) is None

    def test_private_rows_in_public_scope_fail_loudly(self) -> None:
        with pytest.raises(ValueError, match="private_unverified"):
            ui.scope_banner("public", {"synthetic", "private_unverified"})


class TestSourceRows:
    def test_rows_count_comments_per_source_sorted_by_id(self) -> None:
        views = [
            comment("b1", source_id="b", source_kind="export", rights="cleared"),
            comment("a1", source_id="a"),
            comment("a2", source_id="a"),
        ]
        rows = ui.source_rows(views)
        assert [(r.source_id, r.kind, r.n_comments) for r in rows] == [
            ("a", "synthetic_fixture", 2),
            ("b", "export", 1),
        ]
        assert rows[0].rights == "synthetic demo data (fictional)"
        assert rows[1].rights == "cleared for publication"

    def test_summary_line(self) -> None:
        counts = {"sources": 1, "comments": 69, "parsed_comments": 66, "mentions": 71, "facts": 82}
        assert ui.summary_line(counts) == (
            "69 comments from 1 source · 66 extracted · 71 variety mentions · 82 condition facts"
        )


class TestSourceLink:
    def test_permalink_is_offered(self) -> None:
        view = comment(permalink="https://example.org/c/1", rights="cleared")
        assert ui.source_link(view) == "https://example.org/c/1"
        assert ui.provenance_note(view) is None

    def test_synthetic_comment_without_link_says_so(self) -> None:
        view = comment()
        assert ui.source_link(view) is None
        assert ui.provenance_note(view) == "Synthetic demo comment — no source link"

    def test_real_comment_without_link(self) -> None:
        assert ui.provenance_note(comment(rights="cleared")) == "No source link available"

    def test_non_http_permalink_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="c1"):
            ui.source_link(comment(permalink="javascript:alert(1)"))

    def test_posted_line_uses_the_date(self) -> None:
        assert ui.posted_line(comment(created_at="2024-10-21T00:00:00Z")) == "Posted 2024-10-21"
        assert ui.posted_line(comment()) is None


# --------------------------------------------------------------------------- options and labels


class TestOptions:
    def test_region_options_start_unset_then_states_provinces_countries(self) -> None:
        options = ui.region_options()
        assert options[0] is None
        codes = list(options[1:])
        assert sorted(codes) == sorted(REGION_NAMES)
        n_states, n_provinces = len(US_STATES), len(CA_PROVINCES)
        states = codes[:n_states]
        provinces = codes[n_states : n_states + n_provinces]
        countries = codes[n_states + n_provinces :]
        assert set(states) == set(US_STATES)
        assert set(provinces) == set(CA_PROVINCES)
        assert set(countries) == set(COUNTRIES)
        assert [REGION_NAMES[c] for c in states] == sorted(REGION_NAMES[c] for c in states)
        assert [REGION_NAMES[c] for c in countries] == sorted(REGION_NAMES[c] for c in countries)

    def test_region_label(self) -> None:
        assert ui.region_label(None) == "Not set"
        assert ui.region_label("US-CT") == "Connecticut"
        assert ui.region_label("CA-ON") == "Ontario (Canada)"
        assert ui.region_label("GB") == "United Kingdom"

    def test_condition_options_and_labels(self) -> None:
        assert ui.condition_options("zone")[0] is None
        assert ui.condition_options("zone")[1:] == VIEWER_VALUES["zone"]
        assert "heatwave" not in ui.condition_options("summer_heat")
        assert ui.option_label("zone", "6b") == "Zone 6b"
        assert ui.option_label("method", "container") == "Containers"
        assert ui.option_label("method", None) == "Not set"
        assert ui.key_label("zone") == "Hardiness zone"

    def test_filter_labels(self) -> None:
        assert ui.crop_label(None) == "Any crop"
        assert ui.crop_label("brussels sprout") == "Brussels sprout"
        assert ui.year_label(None) == "Any year"
        assert ui.year_label("2023") == "2023"
        assert ui.topic_label("weather_tolerance") == "weather tolerance"


# --------------------------------------------------------------------------- state


class TestState:
    def test_reset_keys_cover_every_condition_and_filter(self) -> None:
        keys = ui.reset_state_keys()
        assert len(keys) == len(set(keys))
        expected = {
            ui.REGION_KEY,
            *ui.CONDITION_KEYS.values(),
            ui.TEXT_KEY,
            ui.CROP_KEY,
            ui.TOPICS_KEY,
            ui.YEAR_KEY,
            ui.STRICT_KEY,
            ui.REGION_FILTER_KEY,
        }
        assert set(keys) == expected
        assert set(ui.CONDITION_KEYS) == set(COMPARABLE_KEYS)

    def test_reset_values_give_an_empty_query(self) -> None:
        values = ui.reset_values()
        assert tuple(values) == ui.reset_state_keys()
        assert ui.query_from_state(values) == SearchQuery()
        assert values[ui.TEXT_KEY] == ""
        assert values[ui.TOPICS_KEY] == []
        assert values[ui.STRICT_KEY] is False

    def test_query_from_state_reads_every_control(self) -> None:
        state = {
            **ui.reset_values(),
            ui.REGION_KEY: "US-CT",
            ui.CONDITION_KEYS["zone"]: "6b",
            ui.CONDITION_KEYS["humidity"]: "humid",
            ui.TEXT_KEY: "  Sungold  ",
            ui.CROP_KEY: "tomato",
            ui.TOPICS_KEY: ["flavor"],
            ui.YEAR_KEY: "2024",
            ui.STRICT_KEY: True,
            ui.REGION_FILTER_KEY: "US-ME",
        }
        assert ui.query_from_state(state) == SearchQuery(
            text="Sungold",
            crop="tomato",
            topics=("flavor",),
            year="2024",
            region_filter="US-ME",
            garden=GardenContext(region="US-CT", zone="6b", humidity="humid"),
            only_stated_matches=True,
        )

    def test_blank_search_text_is_no_filter(self) -> None:
        state = {**ui.reset_values(), ui.TEXT_KEY: "   "}
        assert ui.query_from_state(state).text is None

    def test_missing_keys_mean_defaults(self) -> None:
        assert ui.query_from_state({}) == SearchQuery()

    def test_invalid_condition_value_fails_loudly(self) -> None:
        with pytest.raises(ValidationError):
            ui.query_from_state({ui.CONDITION_KEYS["zone"]: "6c"})

    def test_non_text_search_value_fails_loudly(self) -> None:
        with pytest.raises(TypeError):
            ui.query_from_state({ui.TEXT_KEY: 42})


OPTIONS = FilterOptions(crops=("pepper", "tomato"), topics=("disease", "flavor"), years=("2024",))


class TestStaleState:
    def test_valid_or_unset_filters_need_no_fix(self) -> None:
        assert ui.stale_state_fixes(ui.reset_values(), OPTIONS) == {}
        assert ui.stale_state_fixes({}, OPTIONS) == {}
        state = {ui.CROP_KEY: "tomato", ui.TOPICS_KEY: ["flavor"], ui.YEAR_KEY: "2024"}
        assert ui.stale_state_fixes(state, OPTIONS) == {}

    def test_values_that_vanished_from_the_options_are_reset(self) -> None:
        state = {
            ui.CROP_KEY: "okra",
            ui.TOPICS_KEY: ["pests", "flavor"],
            ui.YEAR_KEY: "2019",
            ui.REGION_FILTER_KEY: "US-VT",
        }
        assert ui.stale_state_fixes(state, OPTIONS) == {
            ui.CROP_KEY: None,
            ui.TOPICS_KEY: ["flavor"],
            ui.YEAR_KEY: None,
        }

    @given(
        crop=st.sampled_from([None, "okra", "pepper", "tomato"]),
        topics=st.lists(st.sampled_from(["disease", "flavor", "pests", "general"]), unique=True),
        year=st.sampled_from([None, "2019", "2024"]),
    )
    def test_fixed_state_always_builds_a_query_within_the_options(
        self, crop: str | None, topics: list[str], year: str | None
    ) -> None:
        state = {ui.CROP_KEY: crop, ui.TOPICS_KEY: topics, ui.YEAR_KEY: year}
        query = ui.query_from_state(state | ui.stale_state_fixes(state, OPTIONS))
        assert query.crop is None or query.crop in OPTIONS.crops
        assert set(query.topics) <= set(OPTIONS.topics)
        assert query.year is None or query.year in OPTIONS.years
        assert [t for t in topics if t in OPTIONS.topics] == list(query.topics)


# --------------------------------------------------------------------------- chips


CHIPS = (
    QuickChip(label="Containers", conditions={"method": "container"}),
    QuickChip(label="Hot & humid", conditions={"summer_heat": "hot", "humidity": "humid"}),
    QuickChip(label="Near me", conditions={"region": "US-CT"}),
)


class TestChips:
    def test_only_chips_with_enough_support_are_shown(self) -> None:
        support = {"Containers": 8, "Hot & humid": 2, "Near me": 3}
        assert ui.visible_chips(CHIPS, support, 3) == (CHIPS[0], CHIPS[2])

    def test_unknown_support_fails_loudly(self) -> None:
        with pytest.raises(KeyError):
            ui.visible_chips(CHIPS, {"Containers": 8}, 3)

    def test_chip_state_maps_conditions_to_widget_keys(self) -> None:
        assert ui.chip_state(CHIPS[1]) == {
            ui.CONDITION_KEYS["summer_heat"]: "hot",
            ui.CONDITION_KEYS["humidity"]: "humid",
        }
        assert ui.chip_state(CHIPS[2]) == {ui.REGION_KEY: "US-CT"}

    def test_applying_a_chip_sets_its_conditions(self) -> None:
        state = {**ui.reset_values(), **ui.chip_state(CHIPS[1])}
        assert ui.query_from_state(state).garden == GardenContext(
            summer_heat="hot", humidity="humid"
        )

    def test_chip_is_active_when_all_its_conditions_are_set(self) -> None:
        state = {**ui.reset_values(), ui.CONDITION_KEYS["summer_heat"]: "hot"}
        assert not ui.chip_is_active(CHIPS[1], state)
        state[ui.CONDITION_KEYS["humidity"]] = "humid"
        assert ui.chip_is_active(CHIPS[1], state)


# --------------------------------------------------------------------------- result summaries


def sample_views() -> list[CommentView]:
    return [
        comment(
            "c1",
            mentions=[
                mention(
                    "c1#m0",
                    facts=[
                        fact("zone", "6b", fact_id="c1#m0#f0"),
                        fact("method", "container", fact_id="c1#m0#f1"),
                    ],
                )
            ],
        ),
        comment(
            "c2",
            mentions=[mention("c2#m0", facts=[fact("method", "container", fact_id="c2#m0#f0")])],
        ),
        comment("c3", mentions=[mention("c3#m0")]),
        comment(
            "c4",
            mentions=[mention("c4#m0", facts=[fact("method", "in_ground", fact_id="c4#m0#f0")])],
        ),
    ]


class TestSummaries:
    def test_count_summary_without_selection(self) -> None:
        found = search_of(sample_views(), SearchQuery())
        assert ui.count_summary(found) == "4 reports"
        one = search_of(sample_views()[:1], SearchQuery())
        assert ui.count_summary(one) == "1 report"

    def test_count_summary_with_selection_lists_every_tier(self) -> None:
        found = search_of(sample_views(), QUERY_6B_POTS)
        assert ui.count_summary(found) == (
            "4 reports · 1 matching · 1 partially matching · 1 not stated · 1 different"
        )

    def test_group_heading_counts_reports(self) -> None:
        found = search_of(sample_views(), QUERY_6B_POTS)
        assert [ui.group_heading(g) for g in found.groups] == [
            "Matching reported conditions (1)",
            "Partially matching conditions (1)",
            "Conditions not stated (1)",
            "Different reported conditions (1)",
        ]

    def test_flatten_keeps_display_order(self) -> None:
        found = search_of(sample_views(), QUERY_6B_POTS)
        assert [r.comment.comment_id for r in ui.flatten(found)] == ["c1", "c2", "c3", "c4"]

    def test_strict_note_reports_hidden_count(self) -> None:
        strict = QUERY_6B_POTS.model_copy(update={"only_stated_matches": True})
        found = search_of(sample_views(), strict)
        assert ui.strict_note(found, strict) == (
            "2 reports hidden because their conditions were not stated or differ."
        )

    def test_strict_note_previews_what_would_be_hidden(self) -> None:
        found = search_of(sample_views(), QUERY_6B_POTS)
        assert ui.strict_note(found, QUERY_6B_POTS) == (
            "Ticking this would hide 2 reports whose conditions were not stated or differ."
        )

    def test_strict_note_without_selection(self) -> None:
        strict = SearchQuery(only_stated_matches=True)
        found = search_of(sample_views(), strict)
        assert ui.strict_note(found, strict) == (
            "Choose at least one growing condition first: until then nothing is hidden."
        )
        assert ui.strict_note(found, SearchQuery()) is None

    def test_region_filter_note(self) -> None:
        assert ui.region_filter_note("US-CT") == (
            "Showing only reports that name Connecticut as where they grew."
        )


# --------------------------------------------------------------------------- map helpers


def ct_counts(min_cell_count: int = 1) -> RegionCounts:
    views = [
        comment(
            f"c{i}",
            mentions=[mention(f"c{i}#m0", facts=[fact("region", code, fact_id=f"c{i}#m0#f0")])],
        )
        for i, code in enumerate(["US-CT", "US-CT", "US-ME", "GB", "US"])
    ] + [comment("c9", mentions=[mention("c9#m0")])]
    return region_counts(ui.flatten(search_of(views, SearchQuery())), min_cell_count)


class TestMapHelpers:
    def test_map_key_changes_with_the_state_filter(self) -> None:
        keys = {ui.map_key(code) for code in (None, "US-CT", "US-ME")}
        assert len(keys) == 3
        assert ui.map_key(None) == ui.map_key(None)
        assert all(key.startswith("map_selection_") for key in keys)

    def test_min_cell_count_applies_only_in_public_scope(self) -> None:
        assert ui.min_cell_count("public", 5) == 5
        assert ui.min_cell_count("private", 5) == 1

    def test_map_notes(self) -> None:
        assert ui.map_notes(ct_counts()) == (
            "Region not reported: 1",
            "Outside the U.S.: 1",
            "In the U.S., state not reported: 1",
        )

    def test_map_notes_omit_empty_us_bucket(self) -> None:
        assert ui.map_notes(region_counts([], 1)) == (
            "Region not reported: 0",
            "Outside the U.S.: 0",
        )

    def test_state_choices_are_alphabetical_with_counts(self) -> None:
        assert ui.state_choices(ct_counts(), None) == {
            None: "All states",
            "US-CT": "Connecticut (2 reports)",
            "US-ME": "Maine (1 report)",
        }

    def test_state_choices_never_reveal_suppressed_counts(self) -> None:
        choices = ui.state_choices(ct_counts(min_cell_count=2), None)
        assert choices["US-ME"] == "Maine (fewer than 2 reports)"
        assert choices["US-CT"] == "Connecticut (2 reports)"

    def test_current_filter_stays_selectable_when_it_has_no_reports(self) -> None:
        choices = ui.state_choices(ct_counts(), "US-VT")
        assert choices["US-VT"] == "Vermont (no reports with the current filters)"

    def test_colorbar_is_horizontal_with_whole_report_ticks(self) -> None:
        bar = ui.map_colorbar(ct_counts())
        assert bar["orientation"] == "h"
        assert bar["dtick"] == 1
        assert bar["title"] == {"text": "Reports", "side": "top"}

    def test_colorbar_ticks_stay_whole_and_sparse_for_large_counts(self) -> None:
        assert ui.colorbar_dtick(0) == 1
        assert ui.colorbar_dtick(5) == 1
        assert ui.colorbar_dtick(6) == 2
        assert ui.colorbar_dtick(101) == 21

    def test_selected_state_reads_the_clicked_location(self) -> None:
        selection = {"selection": {"points": [{"location": "CT", "z": 2}], "point_indices": [0]}}
        assert ui.selected_state(selection) == "US-CT"

    def test_selected_state_ignores_empty_or_unknown_selections(self) -> None:
        assert ui.selected_state({"selection": {"points": []}}) is None
        assert ui.selected_state({"selection": {"points": [{"location": "ZZ"}]}}) is None
        assert ui.selected_state({}) is None


# --------------------------------------------------------------------------- outside links, args


class TestOutsideLinks:
    def test_link_markdown_escapes_the_title(self) -> None:
        link = OutsideLink(
            title="USDA [zone] map",
            url="https://example.gov/map",
            label="regional climate reference",
            note="",
        )
        assert ui.outside_link_markdown(link) == "[USDA \\[zone\\] map](<https://example.gov/map>)"

    def test_unsafe_url_fails_loudly(self) -> None:
        link = OutsideLink(
            title="x", url="https://example.gov/a b", label="published external trial", note=""
        )
        with pytest.raises(ValueError, match="URL"):
            ui.outside_link_markdown(link)


class TestLabelBadge:
    def test_each_outside_label_gets_its_own_badge(self) -> None:
        assert ui.label_badge("regional climate reference") == (
            ":blue-badge[regional climate reference]"
        )
        assert ui.label_badge("published external trial") == (
            ":violet-badge[published external trial]"
        )
        assert ui.label_badge("community report") == ":green-badge[community report]"


class TestParseAppArgs:
    def test_defaults(self) -> None:
        args = ui.parse_app_args([])
        assert args == ui.AppArgs(config=DEFAULT_CONFIG_PATH, db=None, scope=None)

    def test_overrides_and_unknown_arguments(self) -> None:
        args = ui.parse_app_args(["--db", "x.sqlite", "--scope", "private", "--other", "-q"])
        assert args == ui.AppArgs(config=DEFAULT_CONFIG_PATH, db=Path("x.sqlite"), scope="private")

    def test_invalid_scope_fails(self) -> None:
        with pytest.raises(SystemExit):
            ui.parse_app_args(["--scope", "everyone"])
