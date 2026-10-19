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

import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from crop_explorer import ui
from crop_explorer.config import DEFAULT_CONFIG_PATH, OutsideLink, QuickChip
from crop_explorer.geography import RegionCounts, region_counts
from crop_explorer.overview import Headline
from crop_explorer.overview_charts import DARK, LIGHT, ChartTheme, plotly_text
from crop_explorer.regions import CA_PROVINCES, COUNTRIES, REGION_NAMES, US_STATES
from crop_explorer.schemas import Applicability, Basis, EnvKey, RightsStatus, Topic
from crop_explorer.search import FilterOptions, build_result
from crop_explorer.views import (
    ChipSupport,
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
    SELF_DESCRIBED_KEYS,
    VIEWER_VALUES,
    ComparableKey,
    is_valid_normalized,
    value_label,
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
    analysed: bool | None = None,
) -> CommentView:
    """Build a view; ``analysed`` defaults to "has mentions" (``False`` means never extracted)."""
    extracted = bool(mentions) if analysed is None else analysed
    return CommentView(
        comment_id=comment_id,
        source_id=source_id,
        source_kind=source_kind,
        rights_status=rights,
        text=text,
        permalink=permalink,
        source_order=0,
        created_at=created_at,
        parse_status="parsed" if extracted else None,
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
MAX_CHARS = 200
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
        assert lines == (
            "In their own words: “heat wave” (a heatwave; you chose mild summers) · "
            "not comparable",
        )

    def test_comment_without_mentions_states_every_selected_condition_as_unknown(self) -> None:
        unresolved = comment(analysed=True)  # extracted, but no variety mention was found
        lines = ui.condition_lines(result_of(unresolved, QUERY_6B_POTS), GARDEN_6B_POTS)
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
            "Roma: Different: in the ground (you: containers)",
        )

    # F15: a second relevant mention in a worse tier states its own tier line.
    def test_other_mention_in_a_different_tier_is_stated(self) -> None:
        view = comment(
            mentions=[
                mention(
                    "c1#m0",
                    crop="pepper",
                    variety="Jalapeño",
                    facts=[fact("shelter", "greenhouse", "in the greenhouse", fact_id="a")],
                ),
                mention(
                    "c1#m1",
                    variety="Roma",
                    facts=[fact("shelter", "outdoors", "outside", fact_id="b")],
                ),
            ]
        )
        garden = GardenContext(shelter="greenhouse")
        lines = ui.condition_lines(result_of(view, SearchQuery(garden=garden)), garden)
        assert lines == (
            "Shown for Jalapeño (1 of 2 mentions)",
            "Relevant: greenhouse / tunnel",
            "Roma: Different: outdoors (you: greenhouse / tunnel)",
        )

    def test_other_mentions_in_the_same_tier_are_not_repeated(self) -> None:
        view = comment(
            mentions=[
                mention("c1#m0", facts=[fact("method", "container", fact_id="a")]),
                mention(
                    "c1#m1", variety="Juliet", facts=[fact("method", "container", fact_id="b")]
                ),
                mention("c1#m2", variety="Roma"),
            ]
        )
        garden = GardenContext(method="container")
        lines = ui.condition_lines(result_of(view, SearchQuery(garden=garden)), garden)
        assert lines == (
            "Shown for Sungold (1 of 3 mentions)",
            "Relevant: containers",
            "Roma: Not stated: growing method",
        )

    # F21: only the reported values that produced a status are labelled with it.
    def test_mild_summers_with_a_heatwave_label_only_the_heatwave_as_relevant(self) -> None:
        facts = [
            fact("summer_heat", "mild", "usually mild", fact_id="a"),
            fact("summer_heat", "heatwave", "a heat wave hit", fact_id="b"),
        ]
        garden = GardenContext(summer_heat="hot")
        found = result_of(comment(mentions=[mention(facts=facts)]), SearchQuery(garden=garden))
        assert ui.condition_lines(found, garden) == (
            "In their own words: “a heat wave hit” (a heatwave; you chose hot summers)",
        )

    def test_hot_summers_with_a_heatwave_label_only_the_climate_as_described(self) -> None:
        facts = [
            fact("summer_heat", "hot", "hot summers", fact_id="a"),
            fact("summer_heat", "heatwave", "a heat wave", fact_id="b"),
        ]
        garden = GardenContext(summer_heat="mild")
        found = result_of(comment(mentions=[mention(facts=facts)]), SearchQuery(garden=garden))
        assert ui.condition_lines(found, garden) == (
            "In their own words: “hot summers” (hot summers; you chose mild summers) · "
            "not counted as a difference",
        )

    def test_imprecise_zone_ignores_a_differing_zone(self) -> None:
        facts = [
            fact("zone", "7a", "zone 7a", fact_id="a"),
            fact("zone", "6", "zone 6", fact_id="b"),
        ]
        garden = GardenContext(zone="6b")
        found = result_of(comment(mentions=[mention(facts=facts)]), SearchQuery(garden=garden))
        assert ui.condition_lines(found, garden) == (
            "Relevant: zone 6, subzone unknown (you: zone 6b)",
        )

    # F19: display-only phrases are stated as written, never as "not stated".
    def test_selected_condition_mentioned_only_as_written_is_not_called_unstated(self) -> None:
        facts = [
            fact("zone", None, "zones 6-7", fact_id="a"),
            fact("humidity", None, "not humid", fact_id="b"),
            fact("soil", None, "sandy\n  loam", fact_id="c"),
        ]
        garden = GardenContext(zone="6b", humidity="humid", soil="loam")
        found = result_of(comment(mentions=[mention(facts=facts)]), SearchQuery(garden=garden))
        assert ui.condition_lines(found, garden) == (
            'Mentioned as written, not comparable: hardiness zone "zones 6-7" · '
            'humidity "not humid" · soil "sandy loam"',
        )

    def test_written_phrases_sit_beside_unstated_and_matching_conditions(self) -> None:
        facts = [
            fact("method", "container", "pots", fact_id="a"),
            fact("soil", None, "rocky hillside dirt", fact_id="b"),
        ]
        garden = GardenContext(zone="6b", method="container", soil="clay")
        found = result_of(comment(mentions=[mention(facts=facts)]), SearchQuery(garden=garden))
        assert ui.condition_lines(found, garden) == (
            "Relevant: containers",
            "Not stated: hardiness zone",
            'Mentioned as written, not comparable: soil "rocky hillside dirt"',
        )

    def test_without_selection_written_phrases_replace_the_not_stated_line(self) -> None:
        dirt = fact("soil", None, "rocky hillside dirt", fact_id="a")
        found = result_of(comment(mentions=[mention(facts=[dirt])]))
        assert ui.condition_lines(found, GardenContext()) == (
            'Mentioned as written, not comparable: soil "rocky hillside dirt"',
        )

    def test_without_selection_written_phrases_follow_reported_conditions(self) -> None:
        facts = [
            fact("method", "container", "pots", fact_id="a"),
            fact("soil", None, "rocky hillside dirt", fact_id="b"),
            fact("growing_year", None, "last year", fact_id="c"),
        ]
        found = result_of(comment(mentions=[mention(facts=facts)]))
        assert ui.condition_lines(found, GardenContext()) == (
            "Reported: containers",
            'Mentioned as written, not comparable: soil "rocky hillside dirt"',
        )

    # F20: a comment that was never extracted is "not yet analysed", not "not stated".
    @pytest.mark.parametrize("garden", [GardenContext(), GARDEN_6B_POTS])
    def test_never_extracted_comment_is_not_yet_analysed(self, garden: GardenContext) -> None:
        view = comment(text="Zone 6b here, Sungold in containers.", analysed=False)
        found = result_of(view, SearchQuery(garden=garden))
        assert ui.condition_lines(found, garden) == ("Not yet analysed for growing conditions",)

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


_OWN_WORDS_VALUES = (
    "6b",
    "6",
    "7a",
    "container",
    "in_ground",
    "hot",
    "mild",
    "cool",
    "heatwave",
    "humid",
    "dry_air",
    "rainy",
    "dry_season",
    "short_season",
    "long_season",
    "full_sun",
)


def own_words_of(
    facts: Sequence[FactView], garden: GardenContext, text: str | None = None
) -> tuple[str, ...]:
    """Return the condition lines of a one-mention comment for ``garden``."""
    query = SearchQuery(text=text, garden=garden)
    return ui.condition_lines(result_of(comment(mentions=[mention(facts=facts)]), query), garden)


class TestOwnWords:
    """Self-described conditions (``vocab.SELF_DESCRIBED_KEYS``) are quoted in the commenter's
    words with the report's region, because "hot" or "humid" is relative to where they garden;
    one described unlike the viewer's is never called different."""

    def test_matched_self_description_is_quoted_and_counted_with_the_region(self) -> None:
        facts = [
            fact("zone", "6b", "Zone 6b", fact_id="a"),
            fact("method", "container", "pots", fact_id="b"),
            fact("humidity", "humid", "brutal and muggy", fact_id="c"),
            fact("region", "US-CT", "Connecticut", fact_id="d"),
        ]
        garden = GardenContext(zone="6b", method="container", humidity="humid")
        found = result_of(comment(mentions=[mention(facts=facts)]), SearchQuery(garden=garden))
        assert found.tier == "match"
        assert ui.condition_lines(found, garden) == (
            "Relevant: zone 6b · containers",
            "In their own words: “brutal and muggy” (counted as humid) · Connecticut",
        )

    def test_self_description_unlike_yours_is_not_counted_as_a_difference(self) -> None:
        garden = GardenContext(summer_heat="hot")
        view = comment(mentions=[mention(facts=[fact("summer_heat", "mild", "mild summers")])])
        found = result_of(view, SearchQuery(garden=garden))
        assert found.tier == "not_stated"
        assert ui.condition_lines(found, garden) == (
            "In their own words: “mild summers” (mild summers; you chose hot summers) · "
            "not counted as a difference",
        )

    def test_heatwave_for_hot_summers_names_the_heatwave_and_the_region(self) -> None:
        facts = [
            fact("summer_heat", "heatwave", "that July heat wave", fact_id="a"),
            fact("region", "US-TX", "North Texas", fact_id="b"),
        ]
        assert own_words_of(facts, GardenContext(summer_heat="hot")) == (
            "In their own words: “that July heat wave” (a heatwave; you chose hot summers) · "
            "Texas",
        )

    def test_self_described_conditions_leave_the_relevant_line(self) -> None:
        facts = [
            fact("method", "container", "pots", fact_id="a"),
            fact("summer_heat", "hot", "scorching", fact_id="b"),
            fact("humidity", "humid", "sticky", fact_id="c"),
        ]
        garden = GardenContext(method="container", summer_heat="hot", humidity="humid")
        assert own_words_of(facts, garden) == (
            "Relevant: containers",
            "In their own words: “scorching” (counted as hot summers)",
            "In their own words: “sticky” (counted as humid)",
        )

    def test_unstated_self_described_condition_is_still_not_stated(self) -> None:
        garden = GardenContext(summer_heat="hot", humidity="humid")
        assert own_words_of([fact("humidity", "humid", "muggy")], garden) == (
            "Not stated: summer heat",
            "In their own words: “muggy” (counted as humid)",
        )

    def test_counted_words_come_before_words_not_counted(self) -> None:
        facts = [
            fact("summer_heat", "mild", "mild", fact_id="a"),
            fact("humidity", "humid", "humid", fact_id="b"),
        ]
        garden = GardenContext(summer_heat="hot", humidity="humid")
        assert own_words_of(facts, garden) == (
            "In their own words: “humid” (counted as humid)",
            "In their own words: “mild” (mild summers; you chose hot summers) · "
            "not counted as a difference",
        )

    def test_a_checkable_difference_still_reads_different(self) -> None:
        """SPEC §5 last row, "I grow it in the ground in dry conditions": only the method
        differs; the dry air is the gardener's own description."""
        facts = [
            fact("method", "in_ground", "in the ground", fact_id="a"),
            fact("humidity", "dry_air", "dry conditions", fact_id="b"),
        ]
        garden = GardenContext(zone="6b", method="container", humidity="humid")
        assert own_words_of(facts, garden) == (
            "Not stated: hardiness zone",
            "Different: in the ground (you: containers)",
            "In their own words: “dry conditions” (dry air; you chose humid) · "
            "not counted as a difference",
        )

    def test_words_are_deduplicated_with_whitespace_collapsed(self) -> None:
        facts = [
            fact("humidity", "humid", "so\n  muggy", fact_id="a"),
            fact("humidity", "humid", "so muggy", fact_id="b"),
            fact("humidity", "humid", "sticky", fact_id="c"),
        ]
        assert own_words_of(facts, GardenContext(humidity="humid")) == (
            "In their own words: “so muggy” / “sticky” (counted as humid)",
        )

    def test_every_value_unlike_yours_is_named(self) -> None:
        facts = [
            fact("summer_heat", "mild", "mild", fact_id="a"),
            fact("summer_heat", "cool", "cool nights", fact_id="b"),
        ]
        assert own_words_of(facts, GardenContext(summer_heat="hot")) == (
            "In their own words: “mild” / “cool nights” (mild summers / cool summers; "
            "you chose hot summers) · not counted as a difference",
        )

    @pytest.mark.parametrize(
        ("key", "value", "viewer", "expected"),
        [
            ("precipitation", "rainy", "dry_season", "rainy; you chose dry spell / dry season"),
            (
                "season_length",
                "long_season",
                "short_season",
                "long season; you chose short season",
            ),
            ("humidity", "humid", "dry_air", "humid; you chose dry air"),
        ],
    )
    def test_each_self_described_key_is_not_counted_as_a_difference(
        self, key: ComparableKey, value: str, viewer: str, expected: str
    ) -> None:
        garden = GardenContext.model_validate({key: viewer})
        assert own_words_of([fact(key, value, "as they put it")], garden) == (
            f"In their own words: “as they put it” ({expected}) · not counted as a difference",
        )

    def test_region_is_inherited_from_a_whole_comment_fact(self) -> None:
        shared = [
            fact("region", "US-GA", "Georgia", applicability="whole_comment", fact_id="a"),
            fact("summer_heat", "hot", "brutally hot", applicability="whole_comment", fact_id="b"),
        ]
        view = comment(
            mentions=[
                mention("c1#m0", variety="Cherokee Purple", facts=shared),
                mention("c1#m1", variety="Juliet"),
            ]
        )
        garden = GardenContext(summer_heat="hot")
        found = result_of(view, SearchQuery(text="Juliet", garden=garden))
        assert ui.condition_lines(found, garden) == (
            "In their own words: “brutally hot” (counted as hot summers) · Georgia",
        )

    def test_several_regions_and_an_unmapped_region(self) -> None:
        hot = fact("summer_heat", "hot", "hot", fact_id="a")
        regions = [
            fact("region", "US-CT", "Connecticut", fact_id="b"),
            fact("region", "US-ME", "Maine", fact_id="c"),
        ]
        garden = GardenContext(summer_heat="hot")
        assert own_words_of([hot, *regions], garden) == (
            "In their own words: “hot” (counted as hot summers) · Connecticut, Maine",
        )
        cape_cod = fact("region", None, "Cape Cod", fact_id="d")
        assert own_words_of([hot, cape_cod], garden) == (
            "In their own words: “hot” (counted as hot summers) · Cape Cod (not mapped)",
        )

    def test_region_is_the_shown_mentions_own(self) -> None:
        view = comment(
            mentions=[
                mention("c1#m0", facts=[fact("region", "US-ME", "Maine", fact_id="a")]),
                mention(
                    "c1#m1", variety="Roma", facts=[fact("summer_heat", "hot", "hot", fact_id="b")]
                ),
            ]
        )
        garden = GardenContext(summer_heat="hot")
        assert ui.condition_lines(result_of(view, SearchQuery(garden=garden)), garden) == (
            "Shown for Roma (1 of 2 mentions)",
            "In their own words: “hot” (counted as hot summers)",
            "Sungold: Not stated: summer heat",
        )

    def test_another_mention_described_differently_states_its_own_words(self) -> None:
        view = comment(
            mentions=[
                mention("c1#m0", facts=[fact("summer_heat", "hot", "hot", fact_id="a")]),
                mention(
                    "c1#m1",
                    variety="Roma",
                    facts=[fact("summer_heat", "mild", "mild", fact_id="b")],
                ),
            ]
        )
        garden = GardenContext(summer_heat="hot")
        assert ui.condition_lines(result_of(view, SearchQuery(garden=garden)), garden) == (
            "Shown for Sungold (1 of 2 mentions)",
            "In their own words: “hot” (counted as hot summers)",
            "Roma: In their own words: “mild” (mild summers; you chose hot summers) · "
            "not counted as a difference",
        )

    def test_another_partial_mention_states_its_counted_words(self) -> None:
        view = comment(
            mentions=[
                mention(
                    "c1#m0",
                    facts=[
                        fact("method", "container", "pots", fact_id="a"),
                        fact("summer_heat", "hot", "hot", fact_id="b"),
                    ],
                ),
                mention(
                    "c1#m1",
                    variety="Roma",
                    facts=[fact("summer_heat", "hot", "really hot", fact_id="c")],
                ),
            ]
        )
        garden = GardenContext(method="container", summer_heat="hot")
        assert ui.condition_lines(result_of(view, SearchQuery(garden=garden)), garden) == (
            "Shown for Sungold (1 of 2 mentions)",
            "Relevant: containers",
            "In their own words: “hot” (counted as hot summers)",
            "Roma: In their own words: “really hot” (counted as hot summers)",
        )

    @settings(max_examples=300)
    @given(
        st.dictionaries(st.sampled_from(COMPARABLE_KEYS), st.sampled_from(_OWN_WORDS_VALUES)),
        st.dictionaries(st.sampled_from(COMPARABLE_KEYS), st.sampled_from(_OWN_WORDS_VALUES)),
    )
    def test_property_one_own_words_line_per_described_condition_never_different(
        self, reported: dict[ComparableKey, str], selected: dict[ComparableKey, str]
    ) -> None:
        facts = [
            fact(key, value, f"said {value}", fact_id=f"c1#m0#f{i}")
            for i, (key, value) in enumerate(reported.items())
            if is_valid_normalized(key, value)
        ]
        garden = GardenContext.model_validate(
            {k: v for k, v in selected.items() if v in VIEWER_VALUES[k]}
        )
        found = result_of(comment(mentions=[mention(facts=facts)]), SearchQuery(garden=garden))
        lines = ui.condition_lines(found, garden)
        stated = {f.key for f in facts}
        described = [k for k in garden.selected() if k in SELF_DESCRIBED_KEYS and k in stated]
        own = [line for line in lines if line.startswith("In their own words: ")]
        assert len(own) == len(described)
        assert all(line.endswith("") for line in own)
        assert any(line.startswith("Different:") for line in lines) == (found.tier == "different")
        relevant = next((line for line in lines if line.startswith("Relevant:")), "")
        for key in described:
            assert value_label(key, garden.selected()[key]) not in relevant
        statuses = [
            d.status for mm in found.mention_matches if mm.match for d in mm.match.dimensions
        ]
        n_not_counted = sum("not counted as a difference" in line for line in lines)
        assert n_not_counted == statuses.count("described_differently")


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


# --------------------------------------------------------------------------- card heading/caption


class TestCardLabels:
    """F16: every card carries the SPEC §7 provenance labels outside the Evidence expander."""

    def test_heading_is_badged_as_a_community_report(self) -> None:
        found = result_of(comment(mentions=[mention(topics=["flavor"])]))
        assert ui.card_heading(found) == (
            "**Sungold tomato · flavor** :green-badge[community report]"
        )

    def test_heading_escapes_untrusted_variety_names(self) -> None:
        found = result_of(comment(mentions=[mention(variety="[x](https://e.example)")]))
        heading = ui.card_heading(found)
        assert heading.startswith("**\\[x\\]\\(https\\:")
        assert heading.endswith(" :green-badge[community report]")

    def test_caption_with_stated_conditions_is_badged(self) -> None:
        found = result_of(full_match_view(), QUERY_6B_POTS)
        assert ui.condition_caption(found, GARDEN_6B_POTS) == (
            ":green-badge[reported conditions] "
            + ui.escape_markdown("Relevant: zone 6b · containers\nRegion not reported")
        )

    def test_caption_without_stated_conditions_has_no_badge(self) -> None:
        found = result_of(comment(mentions=[mention()]), QUERY_6B_POTS)
        assert ui.condition_caption(found, GARDEN_6B_POTS) == ui.escape_markdown(
            "Not stated: hardiness zone, growing method\nRegion not reported"
        )
        assert ui.condition_caption(result_of(comment(mentions=[mention()])), GardenContext()) == (
            "Growing conditions not stated  \nRegion not reported"
        )

    def test_a_region_alone_is_not_badged_as_reported_conditions(self) -> None:
        """Validation regression: "reported conditions Growing conditions not stated" read as a
        contradiction; a location is never a comparable growing condition."""
        view = comment(mentions=[mention(facts=[fact("region", "US-ME", "Maine")])])
        assert ui.condition_caption(result_of(view), GardenContext()) == ui.escape_markdown(
            "Growing conditions not stated\nRegion: Maine"
        )
        assert ui.condition_caption(
            result_of(view, QUERY_6B_POTS), GARDEN_6B_POTS
        ) == ui.escape_markdown("Not stated: hardiness zone, growing method\nRegion: Maine")

    def test_a_region_beside_a_stated_condition_keeps_the_badge(self) -> None:
        maine = fact("region", "US-ME", "Maine", fact_id="a")
        pots = fact("method", "container", "pots", fact_id="b")
        view = comment(mentions=[mention(facts=[maine, pots])])
        caption = ui.condition_caption(result_of(view), GardenContext())
        assert caption == ":green-badge[reported conditions] " + ui.escape_markdown(
            "Reported: containers\nRegion: Maine"
        )

    def test_caption_badges_display_only_phrases(self) -> None:
        dirt = fact("soil", None, "rocky dirt")
        caption = ui.condition_caption(
            result_of(comment(mentions=[mention(facts=[dirt])])), GardenContext()
        )
        assert caption.startswith(":green-badge[reported conditions] Mentioned as written")

    def test_caption_badges_another_mentions_difference(self) -> None:
        view = comment(
            mentions=[
                mention("c1#m0"),
                mention("c1#m1", variety="Roma", facts=[fact("method", "in_ground", fact_id="x")]),
            ]
        )
        caption = ui.condition_caption(result_of(view, QUERY_6B_POTS), GARDEN_6B_POTS)
        assert caption.startswith(":green-badge[reported conditions] Shown for Sungold")

    @pytest.mark.parametrize(
        ("value", "viewer"), [("mild", "hot"), ("heatwave", "mild"), ("heatwave", "hot")]
    )
    def test_caption_badges_a_self_description_whatever_its_status(
        self, value: str, viewer: str
    ) -> None:
        """A self-description unlike yours or not comparable is still a stated condition, so
        its own-words line carries the badge (the report itself is in the not-stated group)."""
        garden = GardenContext(summer_heat=viewer)
        view = comment(mentions=[mention(facts=[fact("summer_heat", value, "the summer")])])
        caption = ui.condition_caption(result_of(view, SearchQuery(garden=garden)), garden)
        assert caption.startswith(":green-badge[reported conditions] In their own words")
        assert caption.endswith(ui.escape_markdown("\nRegion not reported"))

    def test_another_mentions_self_description_in_the_same_tier_gets_no_badge(self) -> None:
        """The other mention's line is only shown when its tier differs, so no badge here."""
        view = comment(
            mentions=[
                mention("c1#m0"),
                mention("c1#m1", variety="Roma", facts=[fact("summer_heat", "mild", fact_id="x")]),
            ]
        )
        garden = GardenContext(summer_heat="hot")
        caption = ui.condition_caption(result_of(view, SearchQuery(garden=garden)), garden)
        assert caption == ui.escape_markdown(
            "Shown for Sungold (1 of 2 mentions)\nNot stated: summer heat\nRegion not reported"
        )

    def test_unanalysed_caption_omits_region_and_badge(self) -> None:
        found = result_of(comment(analysed=False), QUERY_6B_POTS)
        assert ui.condition_caption(found, GARDEN_6B_POTS) == (
            "Not yet analysed for growing conditions"
        )

    def test_caption_escapes_untrusted_phrases(self) -> None:
        dirt = fact("soil", None, "[x](https://e.example)")
        caption = ui.condition_caption(
            result_of(comment(mentions=[mention(facts=[dirt])])), GardenContext()
        )
        assert "\\[x\\]\\(" in caption
        assert "](" not in caption


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

    @pytest.mark.parametrize("rights", ["cleared", "private_unverified"])
    def test_real_comments_link_out(self, rights: RightsStatus) -> None:
        view = comment(permalink="https://example.org/c/1", rights=rights)
        assert ui.source_link(view) == "https://example.org/c/1"

    def test_synthetic_comment_with_a_permalink_never_links_and_stays_marked(self) -> None:
        """F13: fiction never links out, and its synthetic marker never depends on a link."""
        view = comment(permalink="https://www.youtube.com/watch?v=x", rights="synthetic")
        assert ui.source_link(view) is None
        assert ui.provenance_note(view) == ui.SYNTHETIC_NOTE

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
        assert ui.topic_title("weather_tolerance") == "Weather tolerance"  # as on the grid


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
            ui.VARIETY_FILTER_KEY,
        }
        assert set(keys) == expected
        assert set(ui.CONDITION_KEYS) == set(COMPARABLE_KEYS)

    def test_reset_values_give_an_empty_query(self) -> None:
        values = ui.reset_values()
        assert tuple(values) == ui.reset_state_keys()
        assert ui.query_from_state(values, max_query_chars=MAX_CHARS) == SearchQuery()
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
            ui.VARIETY_FILTER_KEY: "sungold",
        }
        assert ui.query_from_state(state, max_query_chars=MAX_CHARS) == SearchQuery(
            text="Sungold",
            crop="tomato",
            topics=("flavor",),
            year="2024",
            region_filter="US-ME",
            variety="sungold",
            garden=GardenContext(region="US-CT", zone="6b", humidity="humid"),
            only_stated_matches=True,
        )

    def test_blank_search_text_is_no_filter(self) -> None:
        state = {**ui.reset_values(), ui.TEXT_KEY: "   "}
        assert ui.query_from_state(state, max_query_chars=MAX_CHARS).text is None

    def test_missing_keys_mean_defaults(self) -> None:
        assert ui.query_from_state({}, max_query_chars=MAX_CHARS) == SearchQuery()

    def test_invalid_condition_value_fails_loudly(self) -> None:
        with pytest.raises(ValidationError):
            ui.query_from_state({ui.CONDITION_KEYS["zone"]: "6c"}, max_query_chars=MAX_CHARS)

    def test_non_text_search_value_fails_loudly(self) -> None:
        with pytest.raises(TypeError):
            ui.query_from_state({ui.TEXT_KEY: 42}, max_query_chars=MAX_CHARS)

    def test_search_text_is_capped(self) -> None:
        """F37: defence in depth behind the text box's max_chars (session state is writable)."""
        query = ui.query_from_state({ui.TEXT_KEY: "a " * 50_000}, max_query_chars=MAX_CHARS)
        assert query.text is not None
        assert len(query.text) <= MAX_CHARS
        capped = ui.query_from_state({ui.TEXT_KEY: "  Sungold tomato"}, max_query_chars=5)
        assert capped.text == "Sun"

    def test_cap_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="max_query_chars"):
            ui.query_from_state({}, max_query_chars=0)


class TestConditionLayout:
    """F45 and F32: which conditions are always visible, and the "More conditions" label."""

    def test_always_visible_conditions_follow_the_spec(self) -> None:
        assert ui.ALWAYS_VISIBLE_KEYS == ("zone", "method", "shelter")
        assert ui.MORE_KEYS == (
            "summer_heat",
            "humidity",
            "precipitation",
            "season_length",
            "sun",
            "soil",
            "irrigation",
        )
        assert sorted((*ui.ALWAYS_VISIBLE_KEYS, *ui.MORE_KEYS)) == sorted(COMPARABLE_KEYS)

    def test_more_conditions_label_without_hidden_conditions(self) -> None:
        state = {**ui.reset_values(), ui.CONDITION_KEYS["zone"]: "6b"}
        assert ui.active_more_conditions(state) == ()
        assert ui.more_conditions_label(()) == "More conditions"

    def test_more_conditions_label_lists_the_hidden_conditions_that_are_set(self) -> None:
        state = {
            **ui.reset_values(),
            ui.CONDITION_KEYS["soil"]: "clay",
            ui.CONDITION_KEYS["humidity"]: "humid",
            ui.CONDITION_KEYS["method"]: "container",
        }
        active = ui.active_more_conditions(state)
        assert active == ("humid", "clay soil")
        assert ui.more_conditions_label(active) == "More conditions (humid, clay soil)"

    def test_none_labels_match_the_select_placeholders(self) -> None:
        """F33: after Clear a select shows its placeholder, which must read like first load."""
        assert ui.ALL_STATES == "All states"
        assert ui.state_choices(region_counts([], 1), None)[None] == ui.ALL_STATES


class TestPaging:
    """F39: each group renders one page of cards at a time."""

    def found(self) -> SearchResult:
        views = [comment(f"c{i:02d}") for i in range(25)]
        return search_of(views, SearchQuery())

    def test_a_new_query_resets_every_group_to_one_page(self) -> None:
        query = SearchQuery(text="tomato")
        fixes = ui.paging_fixes({}, query)
        assert fixes[ui.PAGE_QUERY_KEY] == query.model_dump_json()
        assert {fixes[ui.page_key(key)] for key in ("all", "match", "different")} == {1}
        state = {**fixes, ui.page_key("all"): 3}
        assert ui.paging_fixes(state, query) == {}
        assert ui.paging_fixes(state, SearchQuery(text="pepper"))[ui.page_key("all")] == 1

    def test_cards_to_show_grows_by_one_page(self) -> None:
        (group,) = self.found().groups
        assert ui.cards_to_show({}, group, 10) == 10
        assert ui.cards_to_show({ui.page_key("all"): 2}, group, 10) == 20
        assert ui.cards_to_show({ui.page_key("all"): 3}, group, 10) == 25

    def test_corrupt_page_count_fails_loudly(self) -> None:
        (group,) = self.found().groups
        with pytest.raises(ValueError, match="page"):
            ui.cards_to_show({ui.page_key("all"): 0}, group, 10)
        with pytest.raises(ValueError, match="page"):
            ui.cards_to_show({ui.page_key("all"): "2"}, group, 10)

    def test_show_more_label(self) -> None:
        assert ui.show_more_label(45, 20) == "Show 20 more (45 not shown yet)"
        assert ui.show_more_label(7, 20) == "Show 7 more"


OPTIONS = FilterOptions(
    crops=("pepper", "tomato"),
    topics=("disease", "flavor"),
    years=("2024",),
    varieties=("juliet", "sungold"),
)


class TestStaleState:
    def test_valid_or_unset_filters_need_no_fix(self) -> None:
        assert ui.stale_state_fixes(ui.reset_values(), OPTIONS) == {}
        assert ui.stale_state_fixes({}, OPTIONS) == {}
        state = {
            ui.CROP_KEY: "tomato",
            ui.TOPICS_KEY: ["flavor"],
            ui.YEAR_KEY: "2024",
            ui.VARIETY_FILTER_KEY: "juliet",
        }
        assert ui.stale_state_fixes(state, OPTIONS) == {}

    def test_values_that_vanished_from_the_options_are_reset(self) -> None:
        state = {
            ui.CROP_KEY: "okra",
            ui.TOPICS_KEY: ["pests", "flavor"],
            ui.YEAR_KEY: "2019",
            ui.REGION_FILTER_KEY: "US-VT",
            ui.VARIETY_FILTER_KEY: "brandywine",
        }
        assert ui.stale_state_fixes(state, OPTIONS) == {
            ui.CROP_KEY: None,
            ui.TOPICS_KEY: ["flavor"],
            ui.YEAR_KEY: None,
            ui.VARIETY_FILTER_KEY: None,
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
        query = ui.query_from_state(
            state | ui.stale_state_fixes(state, OPTIONS), max_query_chars=MAX_CHARS
        )
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


def support(n_any: int, n_all: int | None = None) -> ChipSupport:
    return ChipSupport(n_any=n_any, n_all=n_any if n_all is None else n_all)


class TestChips:
    def test_only_chips_with_enough_support_are_shown(self) -> None:
        counts = {"Containers": support(8), "Hot & humid": support(2), "Near me": support(3)}
        assert ui.visible_chips(CHIPS, counts, 3) == (CHIPS[0], CHIPS[2])

    def test_gate_counts_reports_stating_any_condition(self) -> None:
        counts = {"Containers": support(8), "Hot & humid": support(6, 2), "Near me": support(0)}
        assert ui.visible_chips(CHIPS, counts, 3) == (CHIPS[0], CHIPS[1])

    def test_unknown_support_fails_loudly(self) -> None:
        with pytest.raises(KeyError):
            ui.visible_chips(CHIPS, {"Containers": support(8)}, 3)

    def test_help_of_a_multi_condition_chip_states_both_counts(self) -> None:
        """F18: the tooltip never implies that every counted report states every condition."""
        chip = QuickChip(
            label="Pots under cover", conditions={"method": "container", "shelter": "greenhouse"}
        )
        assert ui.chip_help(chip, support(6, 2)) == (
            "6 reports mention containers or greenhouse / tunnel; 2 mention both"
        )
        assert ui.chip_help(chip, support(1, 1)) == (
            "1 report mentions containers or greenhouse / tunnel; 1 mentions both"
        )

    def test_help_of_a_self_described_chip_counts_descriptions_in_their_own_words(self) -> None:
        """Hot and humid are the gardeners' own, uncalibrated words, and the tooltip says so."""
        assert ui.chip_help(CHIPS[1], support(6, 2)) == (
            "6 reports describe hot summers or humid conditions in their own words; "
            "2 describe both"
        )
        assert ui.chip_help(CHIPS[1], support(1, 1)) == (
            "1 report describes hot summers or humid conditions in their own words; "
            "1 describes both"
        )

    def test_help_of_a_three_condition_chip(self) -> None:
        chip = QuickChip(
            label="Muggy pots",
            conditions={"summer_heat": "hot", "humidity": "humid", "method": "container"},
        )
        assert ui.chip_help(chip, support(5, 0)) == (
            "5 reports describe hot summers, humid conditions or containers in their own words; "
            "0 describe all of them"
        )

    def test_help_of_a_single_condition_chip(self) -> None:
        assert ui.chip_help(CHIPS[0], support(8)) == "Set this condition (8 reports state it)"
        assert ui.chip_help(CHIPS[2], support(1)) == "Set this condition (1 report states it)"

    def test_help_of_a_single_self_described_chip(self) -> None:
        chip = QuickChip(label="Short season", conditions={"season_length": "short_season"})
        assert ui.chip_help(chip, support(4)) == (
            "Set this condition (4 reports describe it in their own words)"
        )
        assert ui.chip_help(chip, support(1)) == (
            "Set this condition (1 report describes it in their own words)"
        )

    def test_chip_state_maps_conditions_to_widget_keys(self) -> None:
        assert ui.chip_state(CHIPS[1]) == {
            ui.CONDITION_KEYS["summer_heat"]: "hot",
            ui.CONDITION_KEYS["humidity"]: "humid",
        }
        assert ui.chip_state(CHIPS[2]) == {ui.REGION_KEY: "US-CT"}

    def test_applying_a_chip_sets_its_conditions(self) -> None:
        state = {**ui.reset_values(), **ui.chip_state(CHIPS[1])}
        assert ui.query_from_state(state, max_query_chars=MAX_CHARS).garden == GardenContext(
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
            "4 reports · 1 matching · 1 partially matching · 1 not stated or not comparable · "
            "1 different"
        )

    def test_group_heading_counts_reports(self) -> None:
        found = search_of(sample_views(), QUERY_6B_POTS)
        assert [ui.group_heading(g) for g in found.groups] == [
            "Matching reported conditions (1)",
            "Partially matching conditions (1)",
            "Conditions not stated or not comparable (1)",
            "Different reported conditions (1)",
        ]

    def test_flatten_keeps_display_order(self) -> None:
        found = search_of(sample_views(), QUERY_6B_POTS)
        assert [r.comment.comment_id for r in ui.flatten(found)] == ["c1", "c2", "c3", "c4"]

    def test_strict_note_reports_hidden_count(self) -> None:
        strict = QUERY_6B_POTS.model_copy(update={"only_stated_matches": True})
        found = search_of(sample_views(), strict)
        assert ui.strict_note(found, strict) == (
            "2 reports hidden because their conditions were not stated, not comparable or differ."
        )

    def test_strict_note_previews_what_would_be_hidden(self) -> None:
        found = search_of(sample_views(), QUERY_6B_POTS)
        assert ui.strict_note(found, QUERY_6B_POTS) == (
            "Ticking this would hide 2 reports whose conditions were not stated, not "
            "comparable or differ."
        )

    def test_strict_note_without_selection(self) -> None:
        strict = SearchQuery(only_stated_matches=True)
        found = search_of(sample_views(), strict)
        assert ui.strict_note(found, strict) == (
            "Choose at least one growing condition first: until then nothing is hidden."
        )
        assert ui.strict_note(found, SearchQuery()) is None

    def test_strict_note_with_only_a_region(self) -> None:
        """F42: Region is a location preference, so it cannot drive "Only show stated matches"."""
        strict = SearchQuery(garden=GardenContext(region="US-CT"), only_stated_matches=True)
        found = search_of(sample_views(), strict)
        assert ui.strict_note(found, strict) == (
            "Region only lists reports from your area first; choose a condition such as zone or "
            "growing method to use this filter. Until then nothing is hidden."
        )
        relaxed = strict.model_copy(update={"only_stated_matches": False})
        assert ui.strict_note(found, relaxed) is None

    def test_region_filter_note(self) -> None:
        assert ui.region_filter_note("US-CT") == (
            "Showing only reports that name Connecticut as where they grew."
        )

    def test_year_filter_note(self) -> None:
        assert ui.year_filter_note("2023") == ("Showing only reports that say they grew in 2023.")

    def test_empty_state_without_strict_hiding(self) -> None:
        found = search_of([], SearchQuery())
        assert ui.empty_state(found) == (
            "No reports to show. Try a different search, or remove a filter or condition."
        )

    def test_empty_state_names_the_strict_checkbox(self) -> None:
        """F30: when strict mode empties the list, say so and how to undo it."""
        strict = QUERY_6B_POTS.model_copy(update={"only_stated_matches": True})
        views = [view for view in sample_views() if view.comment_id in ("c3", "c4")]
        found = search_of(views, strict)
        assert found.groups == ()
        assert ui.empty_state(found) == (
            'All 2 reports are hidden by "Only show stated matches". Untick it to see them.'
        )
        different = search_of(views[1:], strict)  # c4 states a differing method
        assert ui.empty_state(different) == (
            'The 1 report found is hidden by "Only show stated matches". Untick it to see it.'
        )

    def test_no_empty_state_when_the_limitation_message_already_explains_the_hiding(
        self,
    ) -> None:
        strict = QUERY_6B_POTS.model_copy(update={"only_stated_matches": True})
        found = search_of([comment("c1"), comment("c2", mentions=[mention("c2#m0")])], strict)
        assert found.groups == ()
        assert found.limitation_message is not None
        assert '"Only show stated matches"' in found.limitation_message
        assert ui.empty_state(found) is None


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

    def test_overrides(self) -> None:
        args = ui.parse_app_args(["--db", "x.sqlite", "--scope", "private"])
        assert args == ui.AppArgs(config=DEFAULT_CONFIG_PATH, db=Path("x.sqlite"), scope="private")

    @pytest.mark.parametrize(
        "argv", [["--database", "other.sqlite"], ["--confg", "c.yaml"], ["--db", "x", "-q"]]
    )
    def test_unknown_arguments_fail_loudly(self, argv: list[str]) -> None:
        """F36: a mistyped option must never silently open the default database or config."""
        with pytest.raises(ValueError, match="unrecognized arguments"):
            ui.parse_app_args(argv)

    def test_invalid_scope_fails(self) -> None:
        with pytest.raises(ValueError, match="--scope"):
            ui.parse_app_args(["--scope", "everyone"])

    def test_missing_value_fails(self) -> None:
        with pytest.raises(ValueError, match="--db"):
            ui.parse_app_args(["--db"])


class TestLoopback:
    """F01: a private-scope viewer may only listen on this machine."""

    @pytest.mark.parametrize(
        "address", ["localhost", "127.0.0.1", "::1", "127.0.0.2", "LOCALHOST"]
    )
    def test_loopback_addresses(self, address: str) -> None:
        assert ui.is_loopback_address(address)

    @pytest.mark.parametrize("address", [None, "", "0.0.0.0", "::", "192.168.1.5", "example.org"])
    def test_other_addresses_are_not_loopback(self, address: str | None) -> None:
        assert not ui.is_loopback_address(address)


# --------------------------------------------------------------------------- at a glance


def glance_headline(**changes: int) -> Headline:
    values = {
        "n_reports": 69,
        "n_varieties": 44,
        "n_crops": 8,
        "n_with_conditions": 33,
        "n_us_states": 18,
        "n_other_regions": 3,
    }
    return Headline.model_validate(values | changes)


def tile_text(markup: str) -> str:
    """The visible text of the tiles: the style block and tags removed."""
    visible = re.sub(r"<style>.*?</style>", "", markup, flags=re.DOTALL)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", visible)).strip()


def plotly_selection(*customdata: list[str]) -> dict[str, object]:
    points = [
        {"curve_number": 0, "point_index": i, "customdata": c} for i, c in enumerate(customdata)
    ]
    return {"selection": {"points": points, "point_indices": [], "box": [], "lasso": []}}


QUALITY_WORDS = ("best", "top-rated", "recommend", "score", "%", "success", "rating")
KNOWN = frozenset({"sungold", "juliet", "auntruby's"})
NAVIGATION = {
    ui.TEXT_KEY: "",
    ui.CROP_KEY: None,
    ui.YEAR_KEY: None,
    ui.REGION_FILTER_KEY: None,
}
"""What a chart click clears, so the reports shown are the ones the mark counted."""


class TestGlanceTiles:
    @pytest.mark.parametrize("theme", [LIGHT, DARK], ids=["light", "dark"])
    def test_four_tiles_with_labels_values_and_the_places_split(self, theme: ChartTheme) -> None:
        markup = ui.kpi_tiles_html(glance_headline(), theme)
        assert markup.count('class="glance-tile"') == 4
        assert tile_text(markup) == (
            "Reports 69 comments in this view "
            "Varieties mentioned 44 across 8 crops "
            "Describe conditions 33 of 69 zone, method, heat… "
            "Places named 21 18 U.S. states · 3 outside the U.S."
        )
        for token in (theme.text_primary, theme.text_secondary, theme.series, theme.meter_track):
            assert token in markup

    def test_notes_use_the_secondary_ink_for_contrast(self) -> None:
        markup = ui.kpi_tiles_html(glance_headline(), LIGHT)
        (note_rule,) = re.findall(r"\.glance-note\{[^}]*\}", markup)
        assert f"color:{LIGHT.text_secondary}" in note_rule  # 7.9:1 on white, muted is 3.6:1
        assert LIGHT.text_muted not in markup

    def test_tiles_use_the_page_font(self) -> None:
        markup = ui.kpi_tiles_html(glance_headline(), LIGHT)
        assert "font-family:inherit" in markup
        assert "system-ui" not in markup

    def test_four_across_on_desktop_two_by_two_on_a_phone(self) -> None:
        markup = ui.kpi_tiles_html(glance_headline(), LIGHT)
        assert "grid-template-columns:repeat(4,minmax(0,1fr))" in markup
        assert re.search(
            r"@media \(max-width:\d+px\)\{\.glance-kpis\{grid-template-columns:repeat\(2,", markup
        )
        assert "tabular-nums" not in markup
        assert "proportional-nums" in markup

    def test_the_meter_is_proportional_without_percentages(self) -> None:
        markup = ui.kpi_tiles_html(glance_headline(), LIGHT)
        assert 'role="meter"' in markup
        assert 'aria-valuenow="33"' in markup
        assert 'aria-valuemax="69"' in markup
        assert "flex:33 1 0px" in markup
        assert "flex:36 1 0px" in markup
        for word in QUALITY_WORDS:
            assert word not in markup.casefold(), word

    def test_the_touch_hint_shows_only_without_a_hovering_pointer(self) -> None:
        markup = ui.kpi_tiles_html(glance_headline(), LIGHT)
        assert ".glance-touch{display:none}" in markup
        assert (
            "@media (hover:none){.glance-touch{display:inline}.glance-mouse{display:none}}"
            in markup
        )

    def test_singulars_and_large_numbers(self) -> None:
        one = tile_text(
            ui.kpi_tiles_html(glance_headline(n_us_states=1, n_other_regions=0, n_crops=1), LIGHT)
        )
        assert "1 U.S. state · 0 outside the U.S." in one
        assert "across 1 crop " in one
        big = glance_headline(n_reports=12_900, n_with_conditions=1_234)
        assert "1,234 of 12.9K" in tile_text(ui.kpi_tiles_html(big, LIGHT))

    def test_empty_data(self) -> None:
        empty = Headline(
            n_reports=0,
            n_varieties=0,
            n_crops=0,
            n_with_conditions=0,
            n_us_states=0,
            n_other_regions=0,
        )
        markup = ui.kpi_tiles_html(empty, LIGHT)
        assert "0 of 0" in tile_text(markup)
        assert "crop" not in tile_text(markup)  # no "across 0 crops"
        assert 'aria-valuemax="0"' in markup


class TestGlanceCaptions:
    def test_variety_caption_is_one_short_line(self) -> None:
        caption = ui.varieties_caption(10, 44)
        assert caption == "Comments that mention each variety: top 10 of 44."
        assert len(caption) <= 52  # one line at phone width

    def test_variety_caption_when_every_variety_is_shown(self) -> None:
        assert ui.varieties_caption(1, 1) == "Comments that mention each variety (all 1)."
        assert ui.varieties_caption(44, 44) == "Comments that mention each variety (all 44)."

    def test_variety_caption_says_what_it_counts_and_that_it_is_not_quality(self) -> None:
        caption = ui.varieties_caption_html(10, 44, 0, 2)
        assert caption.startswith(
            "Comments that mention each variety: top 10 of 44. This counts conversation, not "
            "quality: a failed crop counts the same as a great one. "
        )
        assert "ties are listed" not in caption

    def test_variety_caption_says_how_many_equal_varieties_were_cut(self) -> None:
        assert "2 more varieties also have 2 comments; ties are listed alphabetically." in (
            ui.varieties_caption_html(10, 46, 2, 2)
        )
        assert "1 more variety also has 1 comment; ties are listed alphabetically." in (
            ui.varieties_caption_html(10, 46, 1, 1)
        )

    def test_captions_end_with_a_mouse_and_a_touch_hint(self) -> None:
        caption = ui.varieties_caption_html(10, 44, 0, 2)
        assert caption.endswith(
            '<span class="glance-mouse">Click a bar to show its comments below.</span>'
            '<span class="glance-touch">Tap a bar once to see its count, then tap it again to '
            "show its comments below.</span>"
        )
        cells = ui.topics_caption_html(8, 0, 3)
        assert "Click a cell to show those comments below." in cells
        assert "Tap a cell once to see its count, then tap it again" in cells

    def test_topic_caption(self) -> None:
        assert ui.topics_caption(8) == (
            "Comments about each topic, for the 8 most-discussed varieties in the bar chart's "
            "order."
        )
        assert ui.topics_caption(1) == "Comments about each topic, for the most-discussed variety."
        caption = ui.topics_caption_html(8, 0, 3)
        assert caption.startswith(ui.topics_caption(8))
        assert "not whether it went well" in caption
        assert "ties are listed" not in caption
        assert "1 more variety also has 3 comments" in ui.topics_caption_html(8, 1, 3)

    def test_captions_are_built_from_numbers_and_fixed_text_only(self) -> None:
        for caption in (ui.varieties_caption_html(10, 44, 3, 2), ui.topics_caption_html(8, 1, 2)):
            text = re.sub(r"<span class=\"glance-(mouse|touch)\">|</span>", "", caption)
            assert "<" not in text
            assert ">" not in text

    def test_setup_caption_is_honest_about_how_few_say(self) -> None:
        assert ui.setups_caption(20, 69) == (
            "Only reports that state a growing method or setting are counted: 20 of 69. Most "
            "don't say. A report can state more than one."
        )
        assert "Most don't say" not in ui.setups_caption(40, 69)

    def test_short_tab_labels_fit_a_phone(self) -> None:
        assert ui.GLANCE_TABS == ("Varieties", "Topics", "How people grow")
        assert sum(len(label) for label in ui.GLANCE_TABS) <= 30

    @pytest.mark.parametrize("n_shown", [1, 10])
    def test_no_quality_words(self, n_shown: int) -> None:
        text = " ".join(
            [
                ui.varieties_caption(n_shown, 44),
                ui.varieties_caption_html(n_shown, 44, 3, 2),
                ui.topics_caption(n_shown),
                ui.topics_caption_html(n_shown, 3, 2),
                ui.setups_caption(5, 69),
                ui.glance_feedback("Sungold · tomato", ["flavor"]),
                ui.variety_filter_note("Sungold · tomato"),
                *ui.GLANCE_TABS,
                ui.NO_VARIETIES,
                ui.NO_SETUPS,
            ]
        ).casefold()
        for word in QUALITY_WORDS:
            assert word not in text, word


class TestGlanceClicks:
    def test_a_bar_click_filters_to_the_variety_key_and_clears_narrowing_filters(self) -> None:
        selection = plotly_selection([plotly_text("auntruby's")])
        assert ui.variety_click_updates(selection, KNOWN) == {
            ui.VARIETY_FILTER_KEY: "auntruby's",
            ui.TOPICS_KEY: [],
            **NAVIGATION,
        }

    def test_a_cell_click_filters_to_the_variety_and_topic(self) -> None:
        selection = plotly_selection(["sungold", "weather_tolerance"])
        assert ui.cell_click_updates(selection, KNOWN) == {
            ui.VARIETY_FILTER_KEY: "sungold",
            ui.TOPICS_KEY: ["weather_tolerance"],
            **NAVIGATION,
        }

    def test_the_garden_and_strict_mode_are_left_alone(self) -> None:
        updates = ui.variety_click_updates(plotly_selection(["juliet"]), KNOWN)
        assert ui.REGION_KEY not in updates
        assert ui.STRICT_KEY not in updates
        assert not set(ui.CONDITION_KEYS.values()) & set(updates)

    @pytest.mark.parametrize(
        "selection",
        [
            {},
            {"selection": {"points": []}},
            {"selection": {"points": [{"x": 1}]}},
            plotly_selection(["a", "b", "c"]),
            plotly_selection(["sungold", "not_a_topic"]),
            plotly_selection(["brandywine", "flavor"]),  # not a visible variety
        ],
    )
    def test_anything_else_changes_nothing(self, selection: dict[str, object]) -> None:
        assert ui.cell_click_updates(selection, KNOWN) == {}
        assert ui.variety_click_updates(selection, KNOWN) == {}

    def test_an_unknown_or_forged_variety_changes_nothing(self) -> None:
        assert ui.variety_click_updates(plotly_selection(["brandywine"]), KNOWN) == {}
        assert ui.variety_click_updates(plotly_selection(["Sungold"]), KNOWN) == {}

    def test_a_cell_selection_is_not_a_bar_selection(self) -> None:
        assert ui.variety_click_updates(plotly_selection(["sungold", "flavor"]), KNOWN) == {}

    def test_chart_keys_change_with_the_variety_filter_and_topics(self) -> None:
        first = ui.glance_key("varieties", {ui.TEXT_KEY: "", ui.TOPICS_KEY: []})
        assert first == ui.glance_key("varieties", {})
        assert first != ui.glance_key("varieties", {ui.VARIETY_FILTER_KEY: "sungold"})
        assert first != ui.glance_key("varieties", {ui.TEXT_KEY: "Sungold"})
        assert first != ui.glance_key("topics", {})
        topics = {ui.VARIETY_FILTER_KEY: "sungold", ui.TOPICS_KEY: ["flavor", "pests"]}
        assert ui.glance_key("x", topics) == ui.glance_key(
            "x", {ui.VARIETY_FILTER_KEY: "sungold", ui.TOPICS_KEY: ["pests", "flavor"]}
        )


class TestGlanceFeedback:
    def test_names_come_from_the_variety_table(self) -> None:
        frame = pd.DataFrame(
            {
                "variety_key": ["sungold", "x"],
                "label": ["Sungold", "Mystery"],
                "crop": ["tomato", None],
                "n_comments": [3, 1],
            }
        )
        assert ui.variety_names(frame) == {"sungold": "Sungold · tomato", "x": "Mystery"}

    def test_feedback_names_the_variety_and_topics_and_links_to_the_reports(self) -> None:
        line = ui.glance_feedback("Sungold · tomato", ["flavor", "weather_tolerance"])
        assert line.startswith("Showing the reports that mention Sungold · tomato about ")
        assert "Flavor or Weather tolerance" in line
        assert line.endswith(f"[Go to the reports ↓](#{ui.RESULTS_ANCHOR})")
        assert "about" not in ui.glance_feedback("Sungold · tomato", [])

    def test_feedback_and_filter_note_escape_the_untrusted_name(self) -> None:
        evil = "[click](https://evil.example) <b>x</b>"
        for text in (ui.glance_feedback(evil, []), ui.variety_filter_note(evil)):
            assert "](https" not in text
            assert "<b>" not in text
            assert ui.escape_markdown(evil) in text

    def test_filter_note(self) -> None:
        assert ui.variety_filter_note("Juliet · tomato") == (
            "Showing only reports that mention Juliet · tomato (all spellings)."
        )

    def test_the_results_anchor_is_trusted_fixed_markup(self) -> None:
        expected = f'<div id="{ui.RESULTS_ANCHOR}"></div>'
        assert expected == ui.RESULTS_ANCHOR_HTML


class TestGlanceTables:
    def test_tables_use_readable_columns_and_raw_names(self) -> None:
        varieties = pd.DataFrame(
            {
                "variety_key": ["evil", "x"],
                "label": ["<b>Evil</b>", "Mystery"],
                "crop": ["tomato", None],
                "n_comments": [3, 1],
            }
        )
        table = ui.variety_table(varieties)
        assert list(table.columns) == ["Variety", "Crop", "Comments"]
        assert table.values.tolist() == [["<b>Evil</b>", "tomato", 3], ["Mystery", "not named", 1]]

    def test_topic_and_setup_tables(self) -> None:
        topics = pd.DataFrame(
            {
                "variety_key": ["s"],
                "label": ["Sungold"],
                "topic": ["weather_tolerance"],
                "n_comments": [2],
            }
        )
        assert ui.topic_table(topics).values.tolist() == [["Sungold", "Weather tolerance", 2]]
        setups = pd.DataFrame(
            {
                "key": ["shelter"],
                "value": ["greenhouse"],
                "label": ["Greenhouse / tunnel"],
                "n_comments": [5],
            }
        )
        table = ui.setup_table(setups)
        assert list(table.columns) == ["Condition", "Stated as", "Comments"]
        assert table.values.tolist() == [["Setting", "Greenhouse / tunnel", 5]]
