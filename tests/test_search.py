"""Tests for environment-first search: mention filters, relevance tiers, groups and counts.

Covers SPEC §5 (deterministic, explainable retrieval; region is a location preference, never a
climate comparison) and §11 #1 (search preservation), #2 (environment association), #6 (missing
is not mismatch), #7 (explainable match), #9 (different practice), #10 (zone precision), #11 (no
popularity inference), #12 (counting), #15 (rights isolation of chips and filter options) and
#16 (graceful sparsity).

Most tests drive the pure core ``build_result`` with hand-built views. The database-backed tests
fill an in-memory SQLite database through ``db.sync_source_comments`` and
``db.replace_extraction`` with directly constructed ``ValidatedComment`` objects.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from crop_explorer import db
from crop_explorer.config import QuickChip, load_config
from crop_explorer.extraction import load_extractions
from crop_explorer.ingest import build_source, import_file
from crop_explorer.normalization import variety_key
from crop_explorer.schemas import (
    Applicability,
    ColumnMapping,
    CommentRecord,
    EnvKey,
    ParseStatus,
    RightsStatus,
    SourceRecord,
    Topic,
    ValidatedComment,
    ValidatedFact,
    ValidatedMention,
)
from crop_explorer.search import (
    FilterOptions,
    build_result,
    chip_support,
    filter_options,
    relevant_mentions,
    search,
)
from crop_explorer.views import (
    GROUP_TITLES,
    TIER_ORDER,
    ChipSupport,
    CommentResult,
    CommentView,
    FactView,
    GardenContext,
    MatchResult,
    MentionMatch,
    MentionView,
    SearchQuery,
    SearchResult,
)
from crop_explorer.vocab import CATEGORICAL_VALUES, COMPARABLE_KEYS, VIEWER_VALUES

NOW = "2026-10-15T12:00:00Z"
PAST = "2026-01-01T00:00:00Z"
LOADED = "2026-10-14T08:00:00Z"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

NOTHING_STATED = "None of your selected conditions were stated."
GROUP_ORDER = ("all", "match", "partial", "not_stated", "different")
ALL_TIERS_ZERO = {"match": 0, "partial": 0, "not_stated": 0, "different": 0}

# --------------------------------------------------------------------------- view builders


def fact(
    key: EnvKey,
    value: str | None,
    *,
    applicability: Applicability = "this_mention",
    written: str | None = None,
) -> FactView:
    shown = written if written is not None else (value or "as written")
    return FactView(
        fact_id=f"{key}={value}",
        key=key,
        value_as_written=shown,
        normalized_value=value,
        evidence_quote=shown,
        applicability=applicability,
        basis="reported_garden_context",
    )


def mention(
    mention_id: str = "m0",
    *,
    crop: str | None = "tomato",
    variety: str | None = None,
    topics: tuple[Topic, ...] = ("flavor",),
    facts: Sequence[FactView] = (),
) -> MentionView:
    return MentionView(
        mention_id=mention_id,
        crop=crop,
        variety_as_written=variety,
        variety_key=None if variety is None else variety.casefold(),
        topics=topics,
        evidence_quote=variety or crop or "it",
        extractor_version="test-v1",
        facts=tuple(facts),
    )


def comment(
    comment_id: str,
    *mentions: MentionView,
    order: int = 0,
    source_id: str = "src-a",
    status: ParseStatus | None = "parsed",
    text: str = "A gardener's report.",
) -> CommentView:
    return CommentView(
        comment_id=comment_id,
        source_id=source_id,
        source_kind="synthetic_fixture",
        rights_status="synthetic",
        text=text,
        permalink=None,
        source_order=order,
        created_at=None,
        parse_status=status,
        mentions=mentions,
    )


def reporting(
    comment_id: str, *facts: FactView, order: int = 0, source_id: str = "src-a"
) -> CommentView:
    """A one-mention tomato comment whose mention states ``facts``."""
    return comment(
        comment_id, mention(f"{comment_id}#m0", facts=facts), order=order, source_id=source_id
    )


def ask(
    *,
    text: str | None = None,
    crop: str | None = None,
    topics: tuple[Topic, ...] = (),
    year: str | None = None,
    region_filter: str | None = None,
    only_stated: bool = False,
    **garden: str,
) -> SearchQuery:
    return SearchQuery(
        text=text,
        crop=crop,
        topics=topics,
        year=year,
        region_filter=region_filter,
        garden=GardenContext.model_validate(garden),
        only_stated_matches=only_stated,
    )


ZONE_6B = fact("zone", "6b", applicability="whole_comment")
CONTAINER = fact("method", "container")
IN_GROUND = fact("method", "in_ground")
HUMID = fact("humidity", "humid", applicability="whole_comment")
SPEC_GARDEN = ask(zone="6b", method="container", humidity="humid")
"""The SPEC §5 example garden."""

UNRESOLVED = comment("q1", status="unresolved", text="What size pots do you use?")
NEVER_EXTRACTED = comment("q2", status=None, order=1, text="Great video, thanks!")


def three_varieties() -> CommentView:
    """Three varieties sharing ``whole_comment`` region and method facts stored on the first."""
    shared = (
        fact("region", "US-CT", applicability="whole_comment"),
        fact("method", "container", applicability="whole_comment"),
    )
    return comment(
        "trio",
        mention("trio#m0", variety="Sungold", facts=shared),
        mention("trio#m1", variety="Juliet"),
        mention("trio#m2", variety="Black Krim"),
    )


def two_crops() -> CommentView:
    """A tomato and a pepper with different regions, years and topics in one comment."""
    return comment(
        "mixed",
        mention(
            "mixed#m0",
            crop="tomato",
            variety="Sungold",
            topics=("flavor",),
            facts=(fact("region", "US-CT"), fact("growing_year", "2023")),
        ),
        mention(
            "mixed#m1",
            crop="pepper",
            variety="Jalapeño",
            topics=("disease",),
            facts=(fact("region", "US-ME"), fact("growing_year", "2024")),
        ),
    )


# --------------------------------------------------------------------------- result accessors


def layout(result: SearchResult) -> dict[str, list[str]]:
    return {g.key: [r.comment.comment_id for r in g.results] for g in result.groups}


def shown(result: SearchResult) -> list[CommentResult]:
    return [r for g in result.groups for r in g.results]


def card(result: SearchResult, comment_id: str) -> CommentResult:
    found = [r for r in shown(result) if r.comment.comment_id == comment_id]
    assert len(found) == 1, f"{comment_id} shown {len(found)} times"
    return found[0]


def match_of(mention_match: MentionMatch) -> MatchResult:
    assert mention_match.match is not None
    return mention_match.match


def tiers(result: SearchResult) -> dict[str, str | None]:
    return {r.comment.comment_id: r.tier for r in shown(result)}


REGION_RANKS = {"same": 0, "same_country": 1}


def display_key(result: CommentResult) -> tuple[int, str, int, str]:
    region_rank = REGION_RANKS.get(str(result.region_status), 2)
    view = result.comment
    return (region_rank, view.source_id, view.source_order, view.comment_id)


# --------------------------------------------------------------------------- strategies

REGIONS = ("US-CT", "US-ME", "CA-ON", "GB", "US", "CA")
FACT_KEYS: tuple[EnvKey, ...] = (*COMPARABLE_KEYS, "region", "growing_year")
APPLICABILITIES: tuple[Applicability, ...] = ("this_mention", "whole_comment")


def reported_choices(key: EnvKey) -> tuple[str, ...]:
    special = {
        "region": REGIONS,
        "zone": ("6", "6a", "6b", "7a"),
        "growing_year": ("2023", "2024"),
    }
    return special.get(key) or CATEGORICAL_VALUES[key]


def viewer_choices(key: str) -> tuple[str, ...]:
    return ("6", "6b", "7a") if key == "zone" else VIEWER_VALUES[key]


@st.composite
def gardens(draw: st.DrawFn, min_selected: int = 0) -> GardenContext:
    keys = draw(
        st.lists(st.sampled_from(COMPARABLE_KEYS), unique=True, min_size=min_selected, max_size=3)
    )
    picks = {key: draw(st.sampled_from(viewer_choices(key))) for key in keys}
    region = draw(st.none() | st.sampled_from(REGIONS))
    return GardenContext.model_validate({**picks, "region": region})


@st.composite
def fact_views(draw: st.DrawFn, fact_id: str) -> FactView:
    key = draw(st.sampled_from(FACT_KEYS))
    value = draw(st.none() | st.sampled_from(reported_choices(key)))
    return FactView(
        fact_id=fact_id,
        key=key,
        value_as_written="as written",
        normalized_value=value,
        evidence_quote="as written",
        applicability=draw(st.sampled_from(APPLICABILITIES)),
        basis="observed_experience",
    )


@st.composite
def mention_views(draw: st.DrawFn, mention_id: str) -> MentionView:
    n_facts = draw(st.integers(0, 3))
    return MentionView(
        mention_id=mention_id,
        crop=draw(st.sampled_from(("tomato", "pepper", None))),
        variety_as_written=draw(st.sampled_from(("Sungold", "Juliet", None))),
        variety_key=None,
        topics=tuple(
            draw(st.lists(st.sampled_from(("flavor", "disease", "productivity")), unique=True))
        ),
        evidence_quote="quote",
        extractor_version="test-v1",
        facts=tuple(draw(fact_views(f"{mention_id}#f{j}")) for j in range(n_facts)),
    )


@st.composite
def comment_views(draw: st.DrawFn, comment_id: str) -> CommentView:
    n_mentions = draw(st.integers(0, 3))
    statuses: tuple[ParseStatus | None, ...] = ("parsed", "partial", "unresolved")
    return comment(
        comment_id,
        *(draw(mention_views(f"{comment_id}#m{i}")) for i in range(n_mentions)),
        order=draw(st.integers(0, 3)),
        source_id=draw(st.sampled_from(("src-a", "src-b"))),
        status=draw(st.sampled_from(statuses if n_mentions else (*statuses, None))),
    )


@st.composite
def view_lists(draw: st.DrawFn) -> list[CommentView]:
    n_comments = draw(st.integers(0, 6))
    return [draw(comment_views(f"c{i}")) for i in range(n_comments)]


SEARCH_TEXTS = ("Sungold", "juliet tomatoes", "peppers", "hornworms", "!!")


@st.composite
def queries(draw: st.DrawFn) -> SearchQuery:
    return SearchQuery(
        text=draw(st.none() | st.sampled_from(SEARCH_TEXTS)),
        crop=draw(st.none() | st.sampled_from(("tomato", "Peppers"))),
        topics=tuple(draw(st.lists(st.sampled_from(("flavor", "disease")), unique=True))),
        year=draw(st.none() | st.sampled_from(("2023", "2024"))),
        region_filter=draw(st.none() | st.sampled_from(REGIONS)),
        garden=draw(gardens()),
        only_stated_matches=draw(st.booleans()),
    )


# =========================================================================== pure core


class TestWithoutSelectedConditions:
    def test_all_reports_form_one_group_in_source_order(self) -> None:
        views = [
            reporting("c3", order=0, source_id="src-b"),
            reporting("c2", ZONE_6B, order=1),
            reporting("c1", order=0),
        ]
        result = build_result(views, ask())
        assert layout(result) == {"all": ["c1", "c2", "c3"]}
        group = result.groups[0]
        assert group.title == GROUP_TITLES["all"]
        assert not group.collapsed_by_default
        assert all(r.tier is None and r.region_status is None for r in group.results)
        assert all(r.explanation == "" for r in group.results)
        assert result.tier_counts == {}
        assert (result.n_candidates, result.n_hidden_by_strict) == (3, 0)

    def test_no_candidates_give_no_groups_and_no_message(self) -> None:
        for query in (ask(), ask(method="container")):
            result = build_result([], query)
            assert result.groups == ()
            assert result.n_candidates == result.n_with_conditions == 0
            assert result.limitation_message is None
        assert build_result([], ask()).tier_counts == {}
        assert build_result([], ask(method="container")).tier_counts == ALL_TIERS_ZERO

    def test_strict_toggle_hides_nothing_when_no_condition_is_selected(self) -> None:
        views = [reporting("c1"), reporting("c2", IN_GROUND, order=1)]
        result = build_result(views, ask(only_stated=True))
        assert layout(result) == {"all": ["c1", "c2"]}
        assert result.n_hidden_by_strict == 0

    def test_region_alone_orders_reports_but_never_tiers_them(self) -> None:
        views = [
            reporting("plain", order=0),
            reporting("maine", fact("region", "US-ME"), order=1),
            reporting("ct", fact("region", "US-CT"), order=2),
        ]
        result = build_result(views, ask(region="US-CT"))
        assert layout(result) == {"all": ["ct", "plain", "maine"]}
        statuses = [r.region_status for r in shown(result)]
        assert statuses == ["same", "not_stated", "elsewhere"]
        assert all(r.tier is None for r in shown(result))


class TestSearchPreservation:
    """SPEC §11 #1: comments without extraction stay retrievable and never break filters."""

    def test_a_never_extracted_comment_cannot_carry_mentions(self) -> None:
        with pytest.raises(ValidationError, match="never been through extraction"):
            comment("bad", mention("bad#m0"), status=None)

    def test_comments_without_mentions_are_kept_without_mention_filters(self) -> None:
        result = build_result([UNRESOLVED, NEVER_EXTRACTED, reporting("c1", order=2)], ask())
        assert layout(result) == {"all": ["q1", "q2", "c1"]}
        assert card(result, "q1").mention_matches == ()
        assert card(result, "q2").comment.parse_status is None

    def test_comments_without_mentions_are_not_stated_under_any_garden(self) -> None:
        result = build_result([UNRESOLVED, NEVER_EXTRACTED], ask(method="container", region="GB"))
        assert layout(result) == {"not_stated": ["q1", "q2"]}
        for result_card in shown(result):
            assert result_card.tier == "not_stated"
            assert result_card.region_status == "not_stated"
            assert result_card.explanation == NOTHING_STATED
            assert not result_card.has_conditions

    @pytest.mark.parametrize(
        "query",
        [
            ask(crop="Tomatoes"),
            ask(topics=("flavor",)),
            ask(year="2023", crop="tomato"),
            ask(region_filter="US-CT", method="container"),
        ],
        ids=["crop", "topics", "year", "region"],
    )
    def test_mention_filters_skip_comments_without_mentions(self, query: SearchQuery) -> None:
        tagged = reporting("c1", fact("growing_year", "2023"), fact("region", "US-CT"), order=2)
        result = build_result([UNRESOLVED, NEVER_EXTRACTED, tagged], query)
        assert [r.comment.comment_id for r in shown(result)] == ["c1"]
        assert result.n_candidates == 1


class TestEnvironmentAssociation:
    """SPEC §11 #2: greenhouse peppers and outdoor tomatoes in one comment stay separate."""

    SPLIT = comment(
        "split",
        mention(
            "split#m0", crop="pepper", variety="Jalapeño", facts=(fact("shelter", "greenhouse"),)
        ),
        mention("split#m1", crop="tomato", variety="Roma", facts=(fact("shelter", "outdoors"),)),
    )

    def test_tomato_grown_outdoors_is_different_for_a_greenhouse_garden(self) -> None:
        result = build_result([self.SPLIT], ask(crop="tomato", shelter="greenhouse"))
        assert layout(result) == {"different": ["split"]}
        split = card(result, "split")
        assert split.tier == "different"
        assert [mm.mention.variety_as_written for mm in split.mention_matches] == ["Roma"]
        assert [f.normalized_value for f in split.mention_matches[0].facts] == ["outdoors"]
        assert split.explanation == "Different: outdoors (you: greenhouse / tunnel)."

    def test_pepper_grown_in_the_greenhouse_matches(self) -> None:
        result = build_result([self.SPLIT], ask(crop="Peppers", shelter="greenhouse"))
        assert layout(result) == {"match": ["split"]}
        split = card(result, "split")
        assert [mm.mention.variety_as_written for mm in split.mention_matches] == ["Jalapeño"]
        assert split.explanation == "Matches your greenhouse / tunnel."

    def test_without_a_crop_filter_the_best_mention_decides_and_is_named(self) -> None:
        split = card(build_result([self.SPLIT], ask(shelter="greenhouse")), "split")
        assert split.tier == "match"
        assert [match_of(mm).tier for mm in split.mention_matches] == ["match", "different"]
        assert split.explanation == "Jalapeño: Matches your greenhouse / tunnel."


class TestTextNamesTheRelevantMentions:
    """F15: a search naming a variety or crop makes that mention the query unit (SPEC §4)."""

    SPLIT = TestEnvironmentAssociation.SPLIT

    def varieties(self, result: SearchResult) -> list[str | None]:
        return [mm.mention.variety_as_written for mm in card(result, "split").mention_matches]

    @pytest.mark.parametrize("text", ["Roma", "roma!", "my ROMA tomatoes", "tomatoes", "Tomato"])
    def test_searching_the_outdoor_tomato_shows_its_difference(self, text: str) -> None:
        result = build_result([self.SPLIT], ask(text=text, shelter="greenhouse"))
        assert layout(result) == {"different": ["split"]}
        assert self.varieties(result) == ["Roma"]
        assert card(result, "split").explanation == (
            "Different: outdoors (you: greenhouse / tunnel)."
        )

    @pytest.mark.parametrize("text", ["Jalapeño", "jalapeno", "JALAPENO peppers", "chillies"])
    def test_searching_the_greenhouse_pepper_shows_its_match(self, text: str) -> None:
        result = build_result([self.SPLIT], ask(text=text, shelter="greenhouse"))
        assert layout(result) == {"match": ["split"]}
        assert self.varieties(result) == ["Jalapeño"]

    def test_strict_mode_hides_a_searched_mention_that_differs(self) -> None:
        result = build_result(
            [self.SPLIT], ask(text="Roma", shelter="greenhouse", only_stated=True)
        )
        assert result.groups == ()
        assert (result.n_candidates, result.n_hidden_by_strict) == (1, 1)

    @pytest.mark.parametrize("text", ["hornworms", "greenhouse", "Rom", "!!", "  "])
    def test_text_naming_no_mention_keeps_every_mention(self, text: str) -> None:
        result = build_result([self.SPLIT], ask(text=text, shelter="greenhouse"))
        assert layout(result) == {"match": ["split"]}
        assert self.varieties(result) == ["Jalapeño", "Roma"]

    def test_text_naming_both_mentions_keeps_both(self) -> None:
        result = build_result([self.SPLIT], ask(text="Roma and Jalapeño", shelter="greenhouse"))
        assert self.varieties(result) == ["Jalapeño", "Roma"]

    @pytest.mark.parametrize(
        "query",
        [
            ask(text="Roma", topics=("flavor",), shelter="greenhouse"),
            ask(text="Roma", crop="pepper", shelter="greenhouse"),
        ],
        ids=["topics", "crop"],
    )
    def test_a_mention_filter_takes_precedence_over_the_text(self, query: SearchQuery) -> None:
        result = build_result([self.SPLIT], query)
        assert layout(result) == {"match": ["split"]}
        assert "Jalapeño" in self.varieties(result)

    def test_a_multi_word_variety_must_be_named_in_full(self) -> None:
        def named(text: str) -> list[str]:
            matches = relevant_mentions(three_varieties(), ask(text=text))
            assert matches is not None
            return [mm.mention.mention_id for mm in matches]

        assert named("black krim") == ["trio#m2"]
        assert named("Black-Krim seedlings") == ["trio#m2"]
        assert named("krim") == ["trio#m0", "trio#m1", "trio#m2"]
        assert named("Juliet or Sungold?") == ["trio#m0", "trio#m1"]

    def test_the_named_mentions_drive_region_and_conditions(self) -> None:
        mixed = two_crops()
        result = build_result([mixed], ask(text="Jalapeño", region="US-CT"))
        mixed_card = card(result, "mixed")
        assert mixed_card.region_status == "elsewhere"
        assert [mm.mention.crop for mm in mixed_card.mention_matches] == ["pepper"]

    def test_a_huge_pasted_text_names_mentions_in_linear_time(self) -> None:
        """F37: the text is read once per query, not once per mention."""
        views = [self.SPLIT, three_varieties(), two_crops()]
        started = time.perf_counter()
        for text in ("a " * 50_000, " ".join(f"x{i}" for i in range(20_000)) + " Roma"):
            build_result(views, ask(text=text, shelter="greenhouse"))
        assert time.perf_counter() - started < 2.0
        result = build_result([self.SPLIT], ask(text="x " * 20_000 + "Roma", shelter="greenhouse"))
        assert self.varieties(result) == ["Roma"]

    @settings(deadline=None)
    @given(view=comment_views("c0"), text=st.sampled_from(SEARCH_TEXTS))
    def test_text_never_drops_a_comment_or_all_its_mentions(
        self, view: CommentView, text: str
    ) -> None:
        matches = relevant_mentions(view, ask(text=text))
        assert matches is not None
        assert bool(matches) == bool(view.mentions)
        kept = [mm.mention for mm in matches]
        assert kept == [m for m in view.mentions if m in kept]


class TestMissingIsNotMismatch:
    """SPEC §11 #6: a flavor-only report stays findable and is labelled "not stated"."""

    FLAVOR_ONLY = comment(
        "flavor",
        mention("flavor#m0", variety="Brandywine", topics=("flavor",)),
        text="Best flavor I've ever tasted!",
    )

    @given(garden=gardens(min_selected=1))
    def test_flavor_only_report_is_not_stated_under_any_garden(
        self, garden: GardenContext
    ) -> None:
        result = build_result([self.FLAVOR_ONLY], SearchQuery(garden=garden))
        assert layout(result) == {"not_stated": ["flavor"]}
        flavor = card(result, "flavor")
        assert flavor.explanation == NOTHING_STATED
        assert not flavor.has_conditions
        dimensions = match_of(flavor.mention_matches[0]).dimensions
        assert {d.status for d in dimensions} == {"not_stated"}


class TestExplainableMatch:
    """SPEC §11 #7, #8, #10: full matches first, with the exact matching dimensions named."""

    @staticmethod
    def spec_example() -> list[CommentView]:
        return [
            reporting("partial", CONTAINER, order=0),
            reporting("unknown", order=1),
            reporting("different", IN_GROUND, fact("humidity", "dry_air"), order=2),
            reporting("full", ZONE_6B, CONTAINER, HUMID, order=3),
        ]

    def test_full_match_is_prioritised_and_names_its_conditions(self) -> None:
        result = build_result(self.spec_example(), SPEC_GARDEN)
        assert layout(result) == {
            "match": ["full"],
            "partial": ["partial"],
            "not_stated": ["unknown"],
            "different": ["different"],
        }
        assert [g.title for g in result.groups] == [
            GROUP_TITLES[key] for key in ("match", "partial", "not_stated", "different")
        ]
        full = card(result, "full")
        assert full.explanation == "Matches your zone 6b, containers and humid."
        dimensions = match_of(full.mention_matches[0]).dimensions
        assert [(d.key, d.status) for d in dimensions] == [
            ("zone", "match"),
            ("method", "match"),
            ("humidity", "match"),
        ]

    def test_partial_and_different_reports_say_what_is_unknown_or_different(self) -> None:
        result = build_result(self.spec_example(), SPEC_GARDEN)
        assert card(result, "partial").explanation == (
            "Matches your containers; hardiness zone and humidity not stated."
        )
        assert card(result, "unknown").explanation == NOTHING_STATED
        assert card(result, "different").explanation == (
            "Different: in the ground (you: containers), dry air (you: humid). "
            "Hardiness zone not stated."
        )

    def test_coarse_zone_is_never_elevated_to_a_full_match(self) -> None:
        coarse = reporting("coarse", fact("zone", "6"), CONTAINER, HUMID)
        result = build_result([coarse], SPEC_GARDEN)
        assert layout(result) == {"partial": ["coarse"]}
        assert "subzone unknown" in card(result, "coarse").explanation

    def test_tier_counts_count_every_candidate_once(self) -> None:
        result = build_result(self.spec_example(), SPEC_GARDEN)
        assert result.tier_counts == {"match": 1, "partial": 1, "not_stated": 1, "different": 1}


class TestDifferentPractice:
    """SPEC §11 #9: explicit in-ground vs containers is shown as different, never dropped."""

    VIEWS = (
        reporting("ground", IN_GROUND, order=0),
        reporting("pots", CONTAINER, order=1),
        reporting("silent", order=2),
    )

    def test_in_ground_report_is_different_and_collapsed(self) -> None:
        result = build_result(self.VIEWS, ask(method="container"))
        assert layout(result) == {
            "match": ["pots"],
            "not_stated": ["silent"],
            "different": ["ground"],
        }
        assert [g.collapsed_by_default for g in result.groups] == [False, False, True]
        assert card(result, "ground").explanation == "Different: in the ground (you: containers)."

    def test_strict_mode_hides_and_counts_unstated_and_different_reports(self) -> None:
        loose = build_result(self.VIEWS, ask(method="container"))
        strict = build_result(self.VIEWS, ask(method="container", only_stated=True))
        assert layout(strict) == {"match": ["pots"]}
        assert (loose.n_hidden_by_strict, strict.n_hidden_by_strict) == (0, 2)
        assert strict.n_candidates == loose.n_candidates == 3
        expected = {"match": 1, "partial": 0, "not_stated": 1, "different": 1}
        assert strict.tier_counts == loose.tier_counts == expected

    def test_strict_mode_keeps_partial_matches(self) -> None:
        views = [reporting("pots", CONTAINER), reporting("full", ZONE_6B, CONTAINER, order=1)]
        result = build_result(views, ask(method="container", zone="6b", only_stated=True))
        assert layout(result) == {"match": ["full"], "partial": ["pots"]}
        assert result.n_hidden_by_strict == 0


class TestNoPopularityInference:
    """SPEC §11 #11: outcome words never change a tier or the order within it."""

    SUCCESS = "Zone 6b, containers, muggy summer. Best harvest ever, pounds of fruit!"
    FAILURE = "Zone 6b, containers, muggy summer. Early blight killed every plant. Total failure."

    @pytest.mark.parametrize("failure_first", [True, False])
    def test_failure_and_success_share_a_tier_and_follow_source_order(
        self, failure_first: bool
    ) -> None:
        facts = (ZONE_6B, CONTAINER, HUMID)
        failure = comment(
            "fail",
            mention("fail#m0", topics=("disease",), facts=facts),
            order=0 if failure_first else 1,
            text=self.FAILURE,
        )
        success = comment(
            "win",
            mention("win#m0", topics=("productivity",), facts=facts),
            order=1 if failure_first else 0,
            text=self.SUCCESS,
        )
        result = build_result([success, failure], SPEC_GARDEN)
        expected = ["fail", "win"] if failure_first else ["win", "fail"]
        assert layout(result) == {"match": expected}
        assert card(result, "fail").explanation == card(result, "win").explanation

    @given(texts=st.lists(st.text(), min_size=2, max_size=4), garden=gardens())
    def test_comment_text_never_changes_tier_or_order(
        self, texts: list[str], garden: GardenContext
    ) -> None:
        n = len(texts)
        views = [
            comment(f"c{i}", mention(f"c{i}#m0", facts=(ZONE_6B, CONTAINER)), order=n - i, text=t)
            for i, t in enumerate(texts)
        ]
        result = build_result(views, SearchQuery(garden=garden))
        assert len(result.groups) == 1
        assert [r.comment.comment_id for r in shown(result)] == [
            f"c{i}" for i in reversed(range(n))
        ]


class TestCounting:
    """SPEC §11 #12: several varieties in one comment count as one report."""

    def test_three_varieties_count_once_everywhere(self) -> None:
        views = [three_varieties(), reporting("solo", order=1)]
        result = build_result(views, ask(method="container", zone="6b", region="US-CT"))
        assert result.n_candidates == 2
        assert result.n_with_conditions == 1
        assert result.tier_counts == {"match": 0, "partial": 1, "not_stated": 1, "different": 0}
        assert layout(result) == {"partial": ["trio"], "not_stated": ["solo"]}
        trio = card(result, "trio")
        assert [match_of(mm).tier for mm in trio.mention_matches] == ["partial"] * 3
        assert trio.region_status == "same"
        assert trio.explanation == "Sungold: Matches your containers; hardiness zone not stated."

    def test_three_varieties_count_once_when_hidden_by_strict_mode(self) -> None:
        result = build_result([three_varieties()], ask(method="in_ground", only_stated=True))
        assert result.groups == ()
        assert (result.n_candidates, result.n_hidden_by_strict) == (1, 1)


class TestGracefulSparsity:
    """SPEC §11 #16: no conditions anywhere gives honest broad results, never false matches."""

    NONE_STATED = (
        "None of these 3 reports mention comparable growing conditions, so none can be compared "
        "with your garden. All are shown below."
    )

    @staticmethod
    def bare(n: int) -> list[CommentView]:
        return [reporting(f"c{i}", order=i) for i in range(n)]

    def test_no_conditions_under_a_garden_gives_one_not_stated_group(self) -> None:
        result = build_result(self.bare(3), SPEC_GARDEN)
        assert layout(result) == {"not_stated": ["c0", "c1", "c2"]}
        assert result.limitation_message == self.NONE_STATED
        assert result.n_with_conditions == 0

    def test_no_conditions_without_a_garden_gives_one_all_group(self) -> None:
        result = build_result(self.bare(3), ask())
        assert layout(result) == {"all": ["c0", "c1", "c2"]}
        assert result.limitation_message == self.NONE_STATED

    def test_message_for_a_single_report_is_singular(self) -> None:
        assert build_result(self.bare(1), ask()).limitation_message == (
            "The 1 report found does not mention comparable growing conditions, so it cannot be "
            "compared with your garden. It is shown below."
        )

    @pytest.mark.parametrize(
        ("n_with", "n", "message"),
        [
            (1, 3, "Only 1 of 3 reports mentions comparable growing conditions."),
            (2, 3, "Only 2 of 3 reports mention comparable growing conditions."),
            (3, 3, None),
            (1, 1, None),
        ],
    )
    def test_coverage_message(self, n_with: int, n: int, message: str | None) -> None:
        views = [
            reporting(f"c{i}", *((CONTAINER,) if i < n_with else ()), order=i) for i in range(n)
        ]
        for query in (ask(), SPEC_GARDEN):
            result = build_result(views, query)
            assert result.limitation_message == message
            assert result.n_with_conditions == n_with

    def test_year_and_unnormalized_facts_are_not_growing_conditions(self) -> None:
        views = [
            reporting("year", fact("growing_year", "2023")),
            reporting("vague", fact("soil", None, written="rocky hillside dirt"), order=1),
        ]
        result = build_result(views, ask())
        assert result.n_with_conditions == 0
        assert not any(r.has_conditions for r in shown(result))

    def test_display_only_conditions_are_not_called_absent(self) -> None:
        """F19: the message speaks of comparable conditions, so it stays true for these."""
        views = [
            reporting("vague", fact("soil", None, written="rocky hillside dirt")),
            reporting("pots", CONTAINER, order=1),
        ]
        result = build_result(views, ask(soil="loam"))
        assert result.limitation_message == (
            "Only 1 of 2 reports mentions comparable growing conditions."
        )
        assert card(result, "vague").explanation == (
            'Soil mentioned as "rocky hillside dirt" (not comparable).'
        )

    def test_a_region_or_heatwave_counts_as_a_growing_condition(self) -> None:
        views = [
            reporting("ct", fact("region", "US-CT")),
            reporting("heat", fact("summer_heat", "heatwave"), order=1),
        ]
        result = build_result(views, ask(method="container"))
        assert result.n_with_conditions == 2
        assert result.limitation_message is None


class TestNotYetAnalysed:
    """F20: never-extracted comments are counted apart and never said to lack conditions."""

    @staticmethod
    def unanalysed(n: int, start: int = 0) -> list[CommentView]:
        return [
            comment(f"u{i}", status=None, order=i, text="Zone 6b here, Sungold in containers.")
            for i in range(start, start + n)
        ]

    def test_all_unanalysed(self) -> None:
        result = build_result(self.unanalysed(3), SPEC_GARDEN)
        assert (result.n_candidates, result.n_with_conditions, result.n_not_analysed) == (3, 0, 3)
        assert layout(result) == {"not_stated": ["u0", "u1", "u2"]}
        assert result.limitation_message == (
            "None of these 3 reports have been analysed for growing conditions yet, so none can "
            "be compared with your garden. All are shown below."
        )

    def test_one_unanalysed(self) -> None:
        assert build_result(self.unanalysed(1), ask()).limitation_message == (
            "The 1 report found has not been analysed for growing conditions yet, so it cannot "
            "be compared with your garden. It is shown below."
        )

    def test_unanalysed_and_unstated(self) -> None:
        views = [*self.unanalysed(1), UNRESOLVED, reporting("bare", order=5)]
        result = build_result(views, ask())
        assert result.n_not_analysed == 1
        assert result.limitation_message == (
            "None of these 3 reports can be compared with your garden: 2 do not mention "
            "comparable growing conditions and 1 has not been analysed yet. All are shown below."
        )
        mostly_new = build_result([*self.unanalysed(2), UNRESOLVED], ask())
        assert mostly_new.limitation_message == (
            "None of these 3 reports can be compared with your garden: 1 does not mention "
            "comparable growing conditions and 2 have not been analysed yet. All are shown below."
        )

    def test_some_with_conditions_and_some_unanalysed(self) -> None:
        views = [reporting("pots", CONTAINER), *self.unanalysed(1, start=1), UNRESOLVED]
        result = build_result(views, SPEC_GARDEN)
        assert (result.n_with_conditions, result.n_not_analysed) == (1, 1)
        assert result.limitation_message == (
            "Only 1 of 3 reports mentions comparable growing conditions (1 not yet analysed)."
        )

    def test_mention_filters_leave_no_unanalysed_candidates(self) -> None:
        result = build_result(
            [*self.unanalysed(2), reporting("pots", CONTAINER)], ask(crop="tomato")
        )
        assert (result.n_candidates, result.n_not_analysed) == (1, 0)


class TestStrictAwareLimitation:
    """F30: with reports hidden by strict mode the message never says they are shown below."""

    HIDDEN = 'hidden by "Only show stated matches"'

    def test_every_report_hidden(self) -> None:
        views = [reporting("c0"), reporting("c1", order=1)]
        result = build_result(views, ask(method="container", only_stated=True))
        assert result.groups == ()
        assert result.limitation_message == (
            "None of these 2 reports mention comparable growing conditions, so none can be "
            f"compared with your garden. All 2 are {self.HIDDEN}; untick it to see them."
        )

    def test_the_only_report_hidden(self) -> None:
        unanalysed = comment("new", status=None)
        result = build_result([unanalysed], ask(method="container", only_stated=True))
        assert result.limitation_message == (
            "The 1 report found has not been analysed for growing conditions yet, so it cannot "
            f"be compared with your garden. It is {self.HIDDEN}; untick it to see it."
        )

    def test_strict_mode_without_a_selection_still_shows_everything(self) -> None:
        result = build_result([reporting("c0"), reporting("c1", order=1)], ask(only_stated=True))
        assert result.limitation_message is not None
        assert result.limitation_message.endswith("All are shown below.")

    @settings(deadline=None, max_examples=150)
    @given(views=view_lists(), query=queries())
    def test_message_never_claims_hidden_reports_are_shown(
        self, views: list[CommentView], query: SearchQuery
    ) -> None:
        result = build_result(views, query)
        message = result.limitation_message or ""
        if result.n_hidden_by_strict:
            assert "shown below" not in message
        if "shown below" in message:
            assert result.n_hidden_by_strict == 0
        if result.n_with_conditions == 0 and result.n_hidden_by_strict and message:
            assert self.HIDDEN in message


class TestRegionPreference:
    """SPEC §5: same region sorts first within a tier but is never a climate match."""

    def test_same_region_first_within_tier_without_changing_tiers(self) -> None:
        views = [
            reporting("plain", CONTAINER, order=0),
            reporting("ct", CONTAINER, fact("region", "US-CT"), order=1),
            reporting("ct-ground", IN_GROUND, fact("region", "US-CT"), order=2),
            reporting("maine", CONTAINER, fact("region", "US-ME"), order=3),
        ]
        result = build_result(views, ask(method="container", zone="6b", region="US-CT"))
        assert layout(result) == {"partial": ["ct", "plain", "maine"], "different": ["ct-ground"]}
        statuses = {r.comment.comment_id: r.region_status for r in shown(result)}
        assert statuses == {
            "ct": "same",
            "plain": "not_stated",
            "maine": "elsewhere",
            "ct-ground": "same",
        }

    def test_a_country_prefers_its_own_states_and_provinces(self) -> None:
        """F34: picking Canada sorts Canadian reports first without changing their tier."""
        views = [
            reporting("minnesota", CONTAINER, fact("region", "US-MN"), order=0),
            reporting("alberta", CONTAINER, fact("region", "CA-AB"), order=1),
            reporting("ontario", IN_GROUND, fact("region", "CA-ON"), order=2),
            reporting("canada", CONTAINER, fact("region", "CA"), order=3),
            reporting("silent", CONTAINER, order=4),
        ]
        result = build_result(views, ask(method="container", region="CA"))
        assert layout(result) == {
            "match": ["canada", "alberta", "minnesota", "silent"],
            "different": ["ontario"],
        }
        statuses = {r.comment.comment_id: r.region_status for r in shown(result)}
        assert statuses == {
            "canada": "same",
            "alberta": "same_country",
            "ontario": "same_country",
            "minnesota": "elsewhere",
            "silent": "not_stated",
        }

    def test_comment_region_status_prefers_same_then_same_country(self) -> None:
        two = comment(
            "two",
            mention("two#m0", variety="A", facts=(fact("region", "GB"),)),
            mention("two#m1", variety="B", facts=(fact("region", "US-CT"),)),
            mention("two#m2", variety="C", facts=(fact("region", "US"),)),
        )
        assert card(build_result([two], ask(region="US")), "two").region_status == "same"
        three = comment("three", *two.mentions[:2])
        assert card(build_result([three], ask(region="US")), "three").region_status == (
            "same_country"
        )

    def test_comment_region_status_prefers_same_then_elsewhere(self) -> None:
        two = comment(
            "two",
            mention("two#m0", variety="A", facts=(fact("region", "US-ME"),)),
            mention("two#m1", variety="B", facts=(fact("region", "US-CT"),)),
            mention("two#m2", variety="C"),
        )
        assert card(build_result([two], ask(region="US-CT")), "two").region_status == "same"
        assert card(build_result([two], ask(region="GB")), "two").region_status == "elsewhere"

    @settings(deadline=None)
    @given(views=view_lists(), garden=gardens(min_selected=1), region=st.sampled_from(REGIONS))
    def test_region_preference_never_changes_a_tier(
        self, views: list[CommentView], garden: GardenContext, region: str
    ) -> None:
        conditions = garden.model_dump()
        without = GardenContext.model_validate({**conditions, "region": None})
        with_region = GardenContext.model_validate({**conditions, "region": region})
        assert tiers(build_result(views, SearchQuery(garden=without))) == tiers(
            build_result(views, SearchQuery(garden=with_region))
        )


class TestMentionFilters:
    def test_region_filter_keeps_only_mentions_reporting_that_region(self) -> None:
        views = [
            two_crops(),
            reporting("britain", fact("region", "GB"), order=1),
            reporting("silent", order=2),
        ]
        result = build_result(views, ask(region_filter="US-ME"))
        assert layout(result) == {"all": ["mixed"]}
        relevant = card(result, "mixed").mention_matches
        assert [mm.mention.variety_as_written for mm in relevant] == ["Jalapeño"]

    def test_region_filter_sees_whole_comment_regions_inherited_by_siblings(self) -> None:
        result = build_result([three_varieties()], ask(region_filter="US-CT"))
        assert len(card(result, "trio").mention_matches) == 3

    def test_year_filter_uses_applicable_growing_year_facts(self) -> None:
        season = comment(
            "season",
            mention(
                "season#m0", facts=(fact("growing_year", "2024", applicability="whole_comment"),)
            ),
            mention("season#m1", crop="zucchini"),
            order=1,
        )
        result = build_result([two_crops(), season], ask(year="2024"))
        assert layout(result) == {"all": ["mixed", "season"]}
        mixed, season_card = shown(result)
        assert [mm.mention.crop for mm in mixed.mention_matches] == ["pepper"]
        assert len(season_card.mention_matches) == 2

    def test_topics_filter_needs_a_shared_topic(self) -> None:
        result = build_result([two_crops()], ask(topics=("pests", "disease")))
        relevant = card(result, "mixed").mention_matches
        assert [mm.mention.crop for mm in relevant] == ["pepper"]
        assert build_result([two_crops()], ask(topics=("appearance",))).n_candidates == 0

    def test_every_set_filter_must_hold_on_the_same_mention(self) -> None:
        assert build_result([two_crops()], ask(crop="tomato", topics=("disease",))).groups == ()
        assert build_result([two_crops()], ask(crop="tomato", region_filter="US-ME")).groups == ()
        result = build_result(
            [two_crops()],
            ask(crop="Tomatoes", topics=("flavor",), year="2023", region_filter="US-CT"),
        )
        relevant = card(result, "mixed").mention_matches
        assert [mm.mention.variety_as_written for mm in relevant] == ["Sungold"]

    def test_crop_filter_compares_normalized_crop_names(self) -> None:
        result = build_result([two_crops()], ask(crop="  PEPPERS "))
        assert [mm.mention.crop for mm in card(result, "mixed").mention_matches] == ["pepper"]

    def test_relevant_mentions_drive_tier_and_explanation(self) -> None:
        result = build_result([two_crops()], ask(region_filter="US-ME", region="US-CT"))
        assert card(result, "mixed").region_status == "elsewhere"

    @pytest.mark.parametrize(
        "query",
        [
            ask(year="23"),
            ask(year="2023 "),
            ask(year="1850"),
            ask(region_filter="Connecticut"),
            ask(region_filter="us-ct"),
            ask(crop="   "),
        ],
        ids=["short-year", "padded-year", "old-year", "region-name", "lowercase-code", "blank"],
    )
    def test_invalid_filter_values_fail_loudly(self, query: SearchQuery) -> None:
        with pytest.raises(ValueError):
            build_result([two_crops()], query)
        with pytest.raises(ValueError):
            relevant_mentions(two_crops(), query)

    def test_invalid_filters_fail_even_without_candidates(self) -> None:
        with pytest.raises(ValueError, match="region"):
            build_result([], ask(region_filter="Atlantis"))


class TestRelevantMentions:
    def test_without_filters_every_mention_is_relevant_with_inherited_facts(self) -> None:
        matches = relevant_mentions(three_varieties(), ask(method="container"))
        assert matches is not None
        assert [mm.mention.mention_id for mm in matches] == ["trio#m0", "trio#m1", "trio#m2"]
        assert [f.fact_id for f in matches[1].facts] == ["region=US-CT", "method=container"]
        assert [match_of(mm).tier for mm in matches] == ["match"] * 3

    def test_without_a_selected_condition_there_is_no_match(self) -> None:
        matches = relevant_mentions(three_varieties(), ask(region="US-CT"))
        assert matches is not None
        assert all(mm.match is None for mm in matches)

    def test_an_excluded_comment_gives_none(self) -> None:
        assert relevant_mentions(three_varieties(), ask(crop="pepper")) is None

    def test_a_comment_without_mentions_is_kept_only_without_mention_filters(self) -> None:
        assert relevant_mentions(UNRESOLVED, ask(method="container")) == ()
        assert relevant_mentions(UNRESOLVED, ask(topics=("flavor",))) is None

    def test_text_is_not_a_mention_filter(self) -> None:
        assert relevant_mentions(UNRESOLVED, ask(text="anything at all")) == ()


class TestDeterminism:
    """SPEC §5: sorting is stable and reproducible; counts are of distinct comments."""

    @settings(deadline=None, max_examples=150)
    @given(views=view_lists(), query=queries(), data=st.data())
    def test_shuffling_the_input_never_changes_the_result(
        self, views: list[CommentView], query: SearchQuery, data: st.DataObject
    ) -> None:
        shuffled = data.draw(st.permutations(views))
        assert build_result(shuffled, query) == build_result(views, query)

    @settings(deadline=None, max_examples=150)
    @given(views=view_lists(), query=queries())
    def test_groups_partition_the_candidates(
        self, views: list[CommentView], query: SearchQuery
    ) -> None:
        result = build_result(views, query)
        kept = [v.comment_id for v in views if relevant_mentions(v, query) is not None]
        ids = [r.comment.comment_id for r in shown(result)]
        assert len(ids) == len(set(ids))
        assert result.n_candidates == len(kept)
        assert len(ids) + result.n_hidden_by_strict == result.n_candidates
        assert 0 <= result.n_with_conditions <= result.n_candidates
        assert result.n_with_conditions + result.n_not_analysed <= result.n_candidates
        kept_views = [v for v in views if v.comment_id in kept]
        assert result.n_not_analysed == sum(v.parse_status is None for v in kept_views)
        keys = [g.key for g in result.groups]
        assert keys == [key for key in GROUP_ORDER if key in keys]
        for group in result.groups:
            assert group.results
            assert {r.tier or "all" for r in group.results} == {group.key}
            assert [display_key(r) for r in group.results] == sorted(
                map(display_key, group.results)
            )
            assert group.collapsed_by_default == (group.key == "different")

    @settings(deadline=None)
    @given(views=view_lists(), query=queries())
    def test_tier_counts_cover_every_candidate(
        self, views: list[CommentView], query: SearchQuery
    ) -> None:
        result = build_result(views, query)
        if query.garden.selected():
            assert list(result.tier_counts) == ["match", "partial", "not_stated", "different"]
            assert sum(result.tier_counts.values()) == result.n_candidates
        else:
            assert result.tier_counts == {}
            assert [g.key for g in result.groups] in ([], ["all"])

    def test_tier_groups_follow_the_canonical_tier_order(self) -> None:
        assert list(GROUP_ORDER[1:]) == sorted(TIER_ORDER, key=TIER_ORDER.__getitem__)
        views = [
            reporting(f"c{i}", *facts, order=i)
            for i, facts in enumerate([(IN_GROUND,), (), (CONTAINER,), (CONTAINER, ZONE_6B)])
        ]
        result = build_result(views, ask(method="container", zone="6b"))
        assert [g.key for g in result.groups] == ["match", "partial", "not_stated", "different"]

    def test_duplicate_comment_ids_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="c1"):
            build_result([reporting("c1"), reporting("c1", order=1)], ask())


# =========================================================================== database-backed


def source_record(
    source_id: str, rights: RightsStatus = "synthetic", *, expires_at: str | None = None
) -> SourceRecord:
    return SourceRecord(
        source_id=source_id,
        kind="comments_export",
        rights_status=rights,
        imported_at=PAST,
        expires_at=expires_at,
        dataset_hash=f"hash-{source_id}",
    )


def vfact(
    key: EnvKey,
    quote: str,
    normalized: str,
    *,
    applicability: Applicability = "this_mention",
) -> ValidatedFact:
    return ValidatedFact(
        key=key,
        value_as_written=quote,
        normalized_value=normalized,
        evidence_quote=quote,
        applicability=applicability,
        basis="reported_garden_context",
    )


def vmention(
    quote: str,
    *,
    crop: str | None,
    variety: str | None = None,
    topics: tuple[Topic, ...] = ("flavor",),
    facts: tuple[ValidatedFact, ...] = (),
) -> ValidatedMention:
    return ValidatedMention(
        crop=crop,
        variety_as_written=variety,
        variety_key=None if variety is None else variety_key(variety),
        topics=topics,
        evidence_quote=quote,
        facts=facts,
    )


def validated(
    comment_id: str, *mentions: ValidatedMention, status: ParseStatus = "parsed"
) -> ValidatedComment:
    return ValidatedComment(
        comment_id=comment_id,
        extractor_version="test-v1",
        status=status,
        mentions=mentions,
        warnings=(),
        rejections=(),
    )


def store(
    conn: sqlite3.Connection,
    source: SourceRecord,
    texts: dict[str, str],
    extractions: Sequence[ValidatedComment] = (),
) -> None:
    records = [
        CommentRecord(comment_id=cid, text=text, source_order=i)
        for i, (cid, text) in enumerate(texts.items())
    ]
    db.sync_source_comments(conn, source, records)
    for extraction in extractions:
        db.replace_extraction(conn, extraction, LOADED)


PUBLIC_TEXTS = {
    "p-split": "I grew Jalapeño peppers in the greenhouse, but my Roma tomatoes were outside.",
    "p-pots": "In 2024: Sungold tomatoes in containers, zone 6b, Connecticut. Muggy summer.",
    "p-trio": "Sungold, Juliet and Black Krim all thrived in grow bags.",
    "p-trellis": "Any tips for building a cucumber trellis?",
    "p-thanks": "Great video, thanks!",
}
PRIVATE_TEXTS = {
    "x-okra": "2022: Clemson Spineless okra in grow bags loved our hot summer. Great trellis.",
}
EXPIRED_TEXTS = {"e-old": "Old notes about a cucumber trellis and greenhouse peppers."}


def public_extractions() -> list[ValidatedComment]:
    in_bags = vfact("method", "in grow bags", "container")
    return [
        validated(
            "p-split",
            vmention(
                "Jalapeño peppers in the greenhouse",
                crop="pepper",
                variety="Jalapeño",
                topics=("productivity",),
                facts=(vfact("shelter", "in the greenhouse", "greenhouse"),),
            ),
            vmention(
                "Roma tomatoes were outside",
                crop="tomato",
                variety="Roma",
                facts=(vfact("shelter", "outside", "outdoors"),),
            ),
        ),
        validated(
            "p-pots",
            vmention(
                "Sungold tomatoes in containers",
                crop="tomato",
                variety="Sungold",
                facts=(
                    vfact("method", "in containers", "container"),
                    vfact("zone", "zone 6b", "6b", applicability="whole_comment"),
                    vfact("region", "Connecticut", "US-CT", applicability="whole_comment"),
                    vfact("humidity", "Muggy", "humid", applicability="whole_comment"),
                    vfact("growing_year", "2024", "2024", applicability="whole_comment"),
                ),
            ),
        ),
        validated(
            "p-trio",
            vmention("Sungold", crop="tomato", variety="Sungold", facts=(in_bags,)),
            vmention("Juliet", crop="tomato", variety="Juliet", facts=(in_bags,)),
            vmention("Black Krim", crop="tomato", variety="Black Krim", facts=(in_bags,)),
        ),
        validated("p-trellis", status="unresolved"),
        # p-thanks deliberately has no extraction at all.
    ]


def private_extractions() -> list[ValidatedComment]:
    return [
        validated(
            "x-okra",
            vmention(
                "Clemson Spineless okra",
                crop="okra",
                variety="Clemson Spineless",
                topics=("weather_tolerance",),
                facts=(
                    vfact("method", "in grow bags", "container"),
                    vfact("summer_heat", "hot summer", "hot", applicability="whole_comment"),
                    vfact("growing_year", "2022", "2022", applicability="whole_comment"),
                ),
            ),
        )
    ]


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = db.connect(Path(":memory:"))
    store(connection, source_record("public-demo"), PUBLIC_TEXTS, public_extractions())
    store(
        connection,
        source_record("private-import", "private_unverified"),
        PRIVATE_TEXTS,
        private_extractions(),
    )
    store(connection, source_record("expired-demo", expires_at=PAST), EXPIRED_TEXTS)
    yield connection
    connection.close()


CHIPS = (
    QuickChip(label="Containers", conditions={"method": "container"}),
    QuickChip(label="Hot & humid", conditions={"summer_heat": "hot", "humidity": "humid"}),
    QuickChip(label="Greenhouse", conditions={"shelter": "greenhouse"}),
    QuickChip(label="Short season", conditions={"season_length": "short_season"}),
)


class TestSearchDatabase:
    def test_text_search_finds_comments_without_any_extraction(
        self, conn: sqlite3.Connection
    ) -> None:
        trellis = search(conn, "public", ask(text="trellis"), NOW)
        assert layout(trellis) == {"all": ["p-trellis"]}
        assert card(trellis, "p-trellis").comment.parse_status == "unresolved"
        thanks = search(conn, "public", ask(text="THANKS!"), NOW)
        assert layout(thanks) == {"all": ["p-thanks"]}
        assert card(thanks, "p-thanks").comment.parse_status is None

    def test_text_hits_without_mentions_survive_a_garden_and_crop_filter(
        self, conn: sqlite3.Connection
    ) -> None:
        under_garden = search(conn, "public", ask(text="trellis", method="container"), NOW)
        assert layout(under_garden) == {"not_stated": ["p-trellis"]}
        with_crop = search(conn, "public", ask(text="trellis", crop="cucumber"), NOW)
        assert with_crop.groups == ()
        assert with_crop.n_candidates == 0

    def test_private_and_expired_comments_never_reach_a_public_search(
        self, conn: sqlite3.Connection
    ) -> None:
        public = search(conn, "public", ask(text="trellis"), NOW)
        private = search(conn, "private", ask(text="trellis"), NOW)
        assert [r.comment.comment_id for r in shown(public)] == ["p-trellis"]
        assert sorted(r.comment.comment_id for r in shown(private)) == ["p-trellis", "x-okra"]
        everything = search(conn, "public", ask(), NOW)
        assert everything.n_candidates == len(PUBLIC_TEXTS)

    def test_greenhouse_peppers_and_outdoor_tomatoes_end_to_end(
        self, conn: sqlite3.Connection
    ) -> None:
        tomato = search(
            conn, "public", ask(text="greenhouse", crop="tomato", shelter="greenhouse"), NOW
        )
        assert layout(tomato) == {"different": ["p-split"]}
        pepper = search(
            conn, "public", ask(text="greenhouse", crop="pepper", shelter="greenhouse"), NOW
        )
        assert layout(pepper) == {"match": ["p-split"]}

    def test_spec_garden_end_to_end(self, conn: sqlite3.Connection) -> None:
        result = search(conn, "public", ask(zone="6b", method="container", humidity="humid"), NOW)
        assert layout(result) == {
            "match": ["p-pots"],
            "partial": ["p-trio"],
            "not_stated": ["p-split", "p-trellis", "p-thanks"],
        }
        assert (result.n_with_conditions, result.n_not_analysed) == (3, 1)
        assert result.limitation_message == (
            "Only 3 of 5 reports mention comparable growing conditions (1 not yet analysed)."
        )

    def test_searching_a_variety_compares_that_variety(self, conn: sqlite3.Connection) -> None:
        """F15 end to end: "Roma" puts the greenhouse-pepper comment under Different."""
        roma = search(conn, "public", ask(text="Roma", shelter="greenhouse"), NOW)
        assert layout(roma) == {"different": ["p-split"]}
        pepper = search(conn, "public", ask(text="jalapeno", shelter="greenhouse"), NOW)
        assert layout(pepper) == {"match": ["p-split"]}

    def test_filters_use_stored_facts(self, conn: sqlite3.Connection) -> None:
        by_year = search(conn, "public", ask(year="2024"), NOW)
        assert layout(by_year) == {"all": ["p-pots"]}
        by_region = search(conn, "public", ask(region_filter="US-CT"), NOW)
        assert layout(by_region) == {"all": ["p-pots"]}
        assert search(conn, "public", ask(year="2022"), NOW).n_candidates == 0
        assert search(conn, "private", ask(year="2022"), NOW).n_candidates == 1


def support(n_any: int, n_all: int) -> ChipSupport:
    return ChipSupport(n_any=n_any, n_all=n_all)


class TestChipSupport:
    def test_counts_distinct_visible_comments_per_chip(self, conn: sqlite3.Connection) -> None:
        assert chip_support(conn, "public", NOW, CHIPS) == {
            "Containers": support(2, 2),
            "Hot & humid": support(1, 0),
            "Greenhouse": support(1, 1),
            "Short season": support(0, 0),
        }

    def test_private_reports_only_count_in_private_scope(self, conn: sqlite3.Connection) -> None:
        private = chip_support(conn, "private", NOW, CHIPS)
        assert (private["Containers"], private["Hot & humid"]) == (support(3, 3), support(2, 0))
        public = chip_support(conn, "public", NOW, CHIPS)
        assert (public["Containers"], public["Hot & humid"]) == (support(2, 2), support(1, 0))

    def test_all_conditions_must_hold_for_one_mention(self, conn: sqlite3.Connection) -> None:
        """F18: hot peppers next to humid tomatoes state both, but no mention matches both."""
        hot = vfact("summer_heat", "hot summer", "hot")
        humid = vfact("humidity", "muggy", "humid")
        shared_humid = vfact("humidity", "muggy", "humid", applicability="whole_comment")
        store(
            conn,
            source_record("chip-demo"),
            {
                "split": "My peppers had a hot summer; my tomatoes were muggy.",
                "both": "My peppers had a hot summer and it was muggy for my tomatoes.",
            },
            [
                validated(
                    "split",
                    vmention("peppers", crop="pepper", facts=(hot,)),
                    vmention("tomatoes", crop="tomato", facts=(humid,)),
                ),
                validated(
                    "both",
                    vmention("peppers", crop="pepper", facts=(hot,)),
                    vmention("tomatoes", crop="tomato", facts=(shared_humid,)),
                ),
            ],
        )
        counts = chip_support(conn, "public", NOW, CHIPS)
        assert counts["Hot & humid"] == support(3, 1)

    @pytest.mark.parametrize(("n_any", "n_all"), [(1, 2), (-1, -1), (0, -1)])
    def test_support_counts_are_consistent(self, n_any: int, n_all: int) -> None:
        with pytest.raises(ValidationError):
            ChipSupport(n_any=n_any, n_all=n_all)

    def test_no_chips_gives_an_empty_mapping(self, conn: sqlite3.Connection) -> None:
        assert chip_support(conn, "public", NOW, ()) == {}

    def test_duplicate_chip_labels_are_rejected(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="Containers"):
            chip_support(conn, "public", NOW, (CHIPS[0], CHIPS[0]))


class TestFilterOptions:
    def test_public_options_list_only_visible_values_sorted(
        self, conn: sqlite3.Connection
    ) -> None:
        options = filter_options(conn, "public", NOW)
        assert options == FilterOptions(
            crops=("pepper", "tomato"), topics=("flavor", "productivity"), years=("2024",)
        )

    def test_private_options_include_private_imports(self, conn: sqlite3.Connection) -> None:
        options = filter_options(conn, "private", NOW)
        assert options.crops == ("okra", "pepper", "tomato")
        assert options.topics == ("flavor", "productivity", "weather_tolerance")
        assert options.years == ("2022", "2024")

    def test_options_are_immutable(self, conn: sqlite3.Connection) -> None:
        options = filter_options(conn, "public", NOW)
        with pytest.raises(ValueError):
            options.crops = ("okra",)  # type: ignore[misc]


# =========================================================================== synthetic fixtures


@pytest.fixture(scope="module")
def synthetic() -> Iterator[sqlite3.Connection]:
    """The demo fixtures loaded through the production pipeline (ingest, then extraction)."""
    connection = db.connect(Path(":memory:"))
    comments = FIXTURES / "synthetic_comments.jsonl"
    source = build_source(
        comments,
        source_id="synthetic-demo",
        kind="synthetic_fixture",
        rights_status="synthetic",
        source_url=None,
        expires_at=None,
        imported_at=PAST,
    )
    import_file(connection, comments, ColumnMapping(), source)
    report = load_extractions(
        connection, FIXTURES / "synthetic_extractions.jsonl", load_config().extraction, LOADED
    )
    assert report.rejections == ()
    yield connection
    connection.close()


EXAMPLE_GARDEN = ask(zone="6b", method="container", humidity="humid", region="US-CT")


class TestSyntheticFixtures:
    """The expected-tier table and coverage numbers of fixtures/SCENARIOS.md."""

    @pytest.mark.parametrize(
        ("comment_id", "tier", "region_status"),
        [
            ("syn-0001", "match", "not_stated"),
            ("syn-0003", "match", "not_stated"),
            ("syn-0006", "match", "same"),
            ("syn-0005", "partial", "not_stated"),
            ("syn-0011", "partial", "not_stated"),
            ("syn-0013", "partial", "same"),
            ("syn-0002", "not_stated", "not_stated"),
            ("syn-0007", "not_stated", "not_stated"),
            ("syn-0019", "different", "elsewhere"),
            ("syn-0036", "different", "same"),
            ("syn-0057", "different", "elsewhere"),
            ("syn-0063", "different", "same"),
            ("syn-0010", "different", "not_stated"),
        ],
    )
    def test_example_garden_tiers(
        self, synthetic: sqlite3.Connection, comment_id: str, tier: str, region_status: str
    ) -> None:
        result_card = card(search(synthetic, "public", EXAMPLE_GARDEN, NOW), comment_id)
        assert (result_card.tier, result_card.region_status) == (tier, region_status)

    def test_full_matches_put_same_region_first_and_ignore_outcomes(
        self, synthetic: sqlite3.Connection
    ) -> None:
        result = search(synthetic, "public", EXAMPLE_GARDEN, NOW)
        assert layout(result)["match"] == ["syn-0006", "syn-0001", "syn-0003"]

    def test_coverage_message_and_counts(self, synthetic: sqlite3.Connection) -> None:
        result = search(synthetic, "public", ask(), NOW)
        assert (result.n_candidates, result.n_not_analysed) == (69, 3)
        assert result.limitation_message == (
            "Only 35 of 69 reports mention comparable growing conditions (3 not yet analysed)."
        )

    def test_quick_chip_support(self, synthetic: sqlite3.Connection) -> None:
        assert chip_support(synthetic, "public", NOW, CHIPS) == {
            "Containers": support(8, 8),
            "Hot & humid": support(6, 2),
            "Greenhouse": support(5, 5),
            "Short season": support(4, 4),
        }

    def test_searching_roma_compares_the_roma_mention(self, synthetic: sqlite3.Connection) -> None:
        """F15: syn-0007 grows Jalapeño in a greenhouse and Roma outside."""
        roma = search(synthetic, "public", ask(text="Roma", shelter="greenhouse"), NOW)
        assert card(roma, "syn-0007").tier == "different"
        strict = search(
            synthetic, "public", ask(text="Roma", shelter="greenhouse", only_stated=True), NOW
        )
        assert "syn-0007" not in [r.comment.comment_id for r in shown(strict)]
        pepper = search(synthetic, "public", ask(text="Jalapeño", shelter="greenhouse"), NOW)
        assert card(pepper, "syn-0007").tier == "match"
        assert chip_support(synthetic, "private", NOW, CHIPS) == chip_support(
            synthetic, "public", NOW, CHIPS
        )

    def test_unextracted_and_injected_comments_stay_text_searchable_only(
        self, synthetic: sqlite3.Connection
    ) -> None:
        pots = search(synthetic, "public", ask(text="pots"), NOW)
        assert "syn-0012" in layout(pots)["all"]
        thanks = search(synthetic, "public", ask(text="great video"), NOW)
        assert layout(thanks)["all"] == ["syn-0004"]
        injected = search(synthetic, "public", ask(text="ignore previous instructions"), NOW)
        assert layout(injected) == {"all": ["syn-0020"]}
        sungold = search(synthetic, "public", ask(text="Sungold", crop="tomato"), NOW)
        assert "syn-0020" not in layout(sungold)["all"]
