"""Tests for the at-a-glance statistics (``crop_explorer.overview``).

Every count is a number of distinct comments, computed only from the views passed in (the
viewer passes the scope-gated views, so rights isolation is tested end to end in
``tests/test_app.py``). Covers headline numbers, variety counts (label and crop choice, tie
breaks, top-N truncation), the variety x topic matrix (zeros included, column order), growing
method / setting counts through applicable facts, configuration of the chart sizes, and
Hypothesis properties: counts never exceed the number of reports, results do not depend on the
order of the views, and the same input always gives the same output.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from crop_explorer import db
from crop_explorer.config import DEFAULT_CONFIG_PATH, OverviewConfig, load_config
from crop_explorer.normalization import variety_key
from crop_explorer.overview import (
    SETUP_COLUMNS,
    TOPIC_COLUMNS,
    VARIETY_COLUMNS,
    Headline,
    headline,
    n_stating_setup,
    setup_counts,
    summarize,
    tied_after,
    topic_matrix,
    variety_counts,
)
from crop_explorer.schemas import Applicability, EnvKey, ParseStatus, Topic
from crop_explorer.search import build_result
from crop_explorer.views import CommentView, FactView, MentionView, SearchQuery
from crop_explorer.vocab import CATEGORICAL_VALUES, COMPARABLE_KEYS

NOW = "2026-10-15T12:00:00Z"

# --------------------------------------------------------------------------- view builders


def fact(
    key: EnvKey, value: str | None, *, applicability: Applicability = "this_mention"
) -> FactView:
    shown = value or "as written"
    return FactView(
        fact_id=f"{key}={value}:{applicability}",
        key=key,
        value_as_written=shown,
        normalized_value=value,
        evidence_quote=shown,
        applicability=applicability,
        basis="reported_garden_context",
    )


def mention(
    mention_id: str,
    variety: str | None = None,
    *,
    crop: str | None = "tomato",
    topics: tuple[Topic, ...] = (),
    facts: Sequence[FactView] = (),
) -> MentionView:
    return MentionView(
        mention_id=mention_id,
        crop=crop,
        variety_as_written=variety,
        variety_key=None if variety is None else variety_key(variety),
        topics=topics,
        evidence_quote=variety or crop or "it",
        extractor_version="test-v1",
        facts=tuple(facts),
    )


def comment(
    comment_id: str, *mentions: MentionView, status: ParseStatus | None = "parsed"
) -> CommentView:
    return CommentView(
        comment_id=comment_id,
        source_id="src-a",
        source_kind="synthetic_fixture",
        rights_status="synthetic",
        text="A gardener's report.",
        permalink=None,
        source_order=0,
        created_at=None,
        parse_status=status,
        mentions=mentions,
    )


def rows(frame: pd.DataFrame) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in frame.itertuples(index=False)]


# --------------------------------------------------------------------------- headline


class TestHeadline:
    def test_no_views_gives_zeros(self) -> None:
        assert headline([]) == Headline(
            n_reports=0,
            n_varieties=0,
            n_crops=0,
            n_with_conditions=0,
            n_us_states=0,
            n_other_regions=0,
        )

    def test_every_visible_comment_is_a_report(self) -> None:
        views = [
            comment("a", mention("a#0", "Sungold")),
            comment("b", status="unresolved"),
            comment("c", status=None),
        ]
        assert headline(views).n_reports == 3

    def test_varieties_are_distinct_keys_not_mentions(self) -> None:
        views = [
            comment("a", mention("a#0", "Sungold"), mention("a#1", "Sun Gold")),
            comment("b", mention("b#0", "Sun-Gold"), mention("b#1", "Juliet")),
            comment("c", mention("c#0", None, crop="zucchini")),  # a crop alone is no variety
        ]
        assert headline(views).n_varieties == 2

    def test_crops_are_the_distinct_crops_of_named_varieties(self) -> None:
        views = [
            comment("a", mention("a#0", "Sungold"), mention("a#1", "Juliet")),
            comment("b", mention("b#0", "Jalapeño", crop="pepper")),
            comment("c", mention("c#0", None, crop="zucchini")),  # no variety: not counted
            comment("d", mention("d#0", "Mystery", crop=None)),
        ]
        assert headline(views).n_crops == 2

    def test_conditions_need_a_comparable_key_with_a_normalized_value(self) -> None:
        views = [
            comment("zone", mention("z#0", "Roma", facts=[fact("zone", "6b")])),
            comment("region", mention("r#0", "Roma", facts=[fact("region", "US-CT")])),
            comment("year", mention("y#0", "Roma", facts=[fact("growing_year", "2023")])),
            comment("written", mention("w#0", "Roma", facts=[fact("soil", None)])),
            comment(
                "inherited",
                mention("i#0", "Roma"),
                mention(
                    "i#1",
                    "Juliet",
                    facts=[fact("method", "container", applicability="whole_comment")],
                ),
            ),
        ]
        assert headline(views).n_with_conditions == 2  # zone, inherited

    def test_places_split_us_states_from_elsewhere(self) -> None:
        views = [
            comment("a", mention("a#0", "Roma", facts=[fact("region", "US-CT")])),
            comment("b", mention("b#0", "Roma", facts=[fact("region", "US-CT")])),
            comment("c", mention("c#0", "Roma", facts=[fact("region", "US-ME")])),
            comment("d", mention("d#0", "Roma", facts=[fact("region", "CA-ON")])),
            comment("e", mention("e#0", "Roma", facts=[fact("region", "GB")])),
            comment("f", mention("f#0", "Roma", facts=[fact("region", "US")])),  # no place
            comment("g", mention("g#0", "Roma", facts=[fact("region", None)])),  # as written
            comment("h", mention("h#0", "Roma", facts=[fact("zone", "6b")])),  # never a place
        ]
        result = headline(views)
        assert (result.n_us_states, result.n_other_regions) == (2, 2)
        assert result.n_places == 4

    def test_an_unknown_region_code_fails_loudly(self) -> None:
        bogus = FactView.model_construct(
            fact_id="f",
            key="region",
            value_as_written="Atlantis",
            normalized_value="XX-AT",
            evidence_quote="Atlantis",
            applicability="this_mention",
            basis="reported_garden_context",
        )
        view = comment("a", mention("a#0", "Roma", facts=[bogus]))
        with pytest.raises(ValueError, match="Comment a"):
            headline([view])

    def test_duplicate_comment_ids_fail_loudly(self) -> None:
        with pytest.raises(ValueError, match="Duplicate comment_id"):
            headline([comment("a"), comment("a")])

    def test_conditions_cannot_exceed_reports(self) -> None:
        with pytest.raises(ValidationError):
            Headline(
                n_reports=1,
                n_varieties=0,
                n_crops=0,
                n_with_conditions=2,
                n_us_states=0,
                n_other_regions=0,
            )


# --------------------------------------------------------------------------- varieties


class TestVarietyCounts:
    def test_columns_and_types(self) -> None:
        frame = variety_counts([comment("a", mention("a#0", "Sungold"))], top_n=10)
        assert tuple(frame.columns) == VARIETY_COLUMNS
        assert rows(frame) == [("sungold", "Sungold", "tomato", 1)]
        assert frame["n_comments"].dtype == "int64"

    def test_empty(self) -> None:
        frame = variety_counts([comment("a", status=None)], top_n=10)
        assert tuple(frame.columns) == VARIETY_COLUMNS
        assert frame.empty

    def test_a_comment_counts_once_per_variety(self) -> None:
        views = [
            comment(
                "a",
                mention("a#0", "Sungold"),
                mention("a#1", "Sun Gold"),
                mention("a#2", "Juliet"),
            ),
            comment("b", mention("b#0", "Sungold")),
        ]
        assert rows(variety_counts(views, top_n=10)) == [
            ("sungold", "Sungold", "tomato", 2),
            ("juliet", "Juliet", "tomato", 1),
        ]

    def test_label_is_the_spelling_most_comments_use(self) -> None:
        views = [
            comment("a", mention("a#0", "Sun Gold"), mention("a#1", "Sun Gold")),
            comment("b", mention("b#0", "Sungold")),
            comment("c", mention("c#0", "Sungold")),
        ]
        assert variety_counts(views, top_n=10)["label"].tolist() == ["Sungold"]

    def test_label_ties_go_alphabetically(self) -> None:
        views = [
            comment("a", mention("a#0", "Sungold")),
            comment("b", mention("b#0", "Sun Gold")),
        ]
        assert variety_counts(views, top_n=10)["label"].tolist() == ["Sun Gold"]

    def test_crop_is_the_most_common_and_may_be_unknown(self) -> None:
        views = [
            comment("a", mention("a#0", "Roma", crop="tomato")),
            comment("b", mention("b#0", "Roma", crop="bean")),
            comment("c", mention("c#0", "Roma", crop="bean")),
            comment("d", mention("d#0", "Mystery", crop=None)),
        ]
        crops = dict(
            zip(*(variety_counts(views, top_n=10)[c] for c in ("label", "crop")), strict=True)
        )
        assert crops == {"Roma": "bean", "Mystery": None}

    def test_crop_ties_go_alphabetically(self) -> None:
        views = [
            comment("a", mention("a#0", "Roma", crop="tomato")),
            comment("b", mention("b#0", "Roma", crop="bean")),
        ]
        assert variety_counts(views, top_n=10)["crop"].tolist() == ["bean"]

    def test_sorted_by_comments_then_label_then_key_and_cut_to_top_n(self) -> None:
        views = [
            comment("a", mention("a#0", "Zebrino"), mention("a#1", "apple")),
            comment("b", mention("b#0", "Zebrino"), mention("b#1", "Banana")),
            comment("c", mention("c#0", "Cherry")),
        ]
        assert variety_counts(views, top_n=10)["label"].tolist() == [
            "Zebrino",
            "apple",
            "Banana",
            "Cherry",
        ]
        assert variety_counts(views, top_n=2)["label"].tolist() == ["Zebrino", "apple"]

    def test_no_top_n_gives_every_variety(self) -> None:
        views = [comment(c, mention(f"{c}#0", name)) for c, name in zip("abc", "XYZ", strict=True)]
        assert variety_counts(views, top_n=None)["label"].tolist() == ["X", "Y", "Z"]

    @pytest.mark.parametrize("top_n", [0, -1])
    def test_top_n_must_be_positive(self, top_n: int) -> None:
        with pytest.raises(ValueError, match="top_n"):
            variety_counts([], top_n=top_n)


class TestTiesAtTheCutoff:
    """Equal varieties are never ranked: the caption says how many share the last count."""

    VIEWS = (
        comment("a", mention("a#0", "Sungold"), mention("a#1", "Juliet"), mention("a#2", "Roma")),
        comment("b", mention("b#0", "Sungold"), mention("b#1", "Juliet"), mention("b#2", "Roma")),
        comment("c", mention("c#0", "Sungold"), mention("c#1", "Stupice")),
    )

    def test_varieties_tied_with_the_last_shown_one_are_counted(self) -> None:
        everything = variety_counts(self.VIEWS, top_n=None)
        assert everything["n_comments"].tolist() == [3, 2, 2, 1]
        assert tied_after(everything, 1) == 0
        assert tied_after(everything, 2) == 1  # Roma has as many comments as Juliet
        assert tied_after(everything, 3) == 0
        assert tied_after(everything, 4) == 0
        assert tied_after(everything, 10) == 0

    def test_needs_a_positive_cut(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            tied_after(variety_counts(self.VIEWS, top_n=None), 0)


# --------------------------------------------------------------------------- topics


class TestTopicMatrix:
    VIEWS = (
        comment(
            "a",
            mention("a#0", "Sungold", topics=("flavor", "productivity")),
            mention("a#1", "Sungold", topics=("flavor",)),
            mention("a#2", "Juliet", topics=("disease",)),
        ),
        comment("b", mention("b#0", "Sungold", topics=("flavor",))),
        comment("c", mention("c#0", "Roma", topics=("pests",))),
    )

    def test_tidy_cells_with_zeros_in_variety_then_topic_order(self) -> None:
        frame = topic_matrix(self.VIEWS, ["sungold", "juliet"])
        assert tuple(frame.columns) == TOPIC_COLUMNS
        assert rows(frame) == [
            ("sungold", "Sungold", "flavor", 2),
            ("sungold", "Sungold", "productivity", 1),
            ("sungold", "Sungold", "disease", 0),
            ("juliet", "Juliet", "flavor", 0),
            ("juliet", "Juliet", "productivity", 0),
            ("juliet", "Juliet", "disease", 1),
        ]

    def test_only_topics_of_the_chosen_varieties_are_columns(self) -> None:
        assert "pests" not in set(topic_matrix(self.VIEWS, ["sungold"])["topic"])

    def test_general_is_always_the_last_column(self) -> None:
        views = [
            comment("a", mention("a#0", "Roma", topics=("general", "pests"))),
            comment("b", mention("b#0", "Roma", topics=("general",))),
        ]
        assert topic_matrix(views, ["roma"])["topic"].tolist() == ["pests", "general"]

    def test_topic_ties_follow_the_canonical_topic_order(self) -> None:
        views = [comment("a", mention("a#0", "Roma", topics=("pests", "flavor", "disease")))]
        assert topic_matrix(views, ["roma"])["topic"].tolist() == ["flavor", "disease", "pests"]

    def test_no_varieties_gives_an_empty_frame(self) -> None:
        frame = topic_matrix(self.VIEWS, [])
        assert tuple(frame.columns) == TOPIC_COLUMNS
        assert frame.empty

    def test_unknown_or_repeated_keys_fail_loudly(self) -> None:
        with pytest.raises(ValueError, match="not a visible variety"):
            topic_matrix(self.VIEWS, ["brandywine"])
        with pytest.raises(ValueError, match="more than once"):
            topic_matrix(self.VIEWS, ["sungold", "sungold"])


# --------------------------------------------------------------------------- setups


class TestSetupCounts:
    def test_counts_distinct_comments_per_stated_value(self) -> None:
        views = [
            comment(
                "a",
                mention("a#0", "Sungold", facts=[fact("method", "container")]),
                mention("a#1", "Juliet", facts=[fact("method", "container")]),
            ),
            comment("b", mention("b#0", "Roma", facts=[fact("method", "container")])),
            comment("c", mention("c#0", "Roma", facts=[fact("shelter", "greenhouse")])),
            comment("d", mention("d#0", "Roma", facts=[fact("method", "in_ground")])),
            comment("e", mention("e#0", "Roma", facts=[fact("method", None)])),  # as written
        ]
        frame = setup_counts(views)
        assert tuple(frame.columns) == SETUP_COLUMNS
        assert rows(frame) == [
            ("method", "container", "Containers", 2),
            ("method", "in_ground", "In the ground", 1),
            ("shelter", "greenhouse", "Greenhouse / tunnel", 1),
        ]

    def test_a_whole_comment_fact_counts_its_comment_once(self) -> None:
        shared = fact("shelter", "greenhouse", applicability="whole_comment")
        views = [
            comment(
                "a",
                mention("a#0", "Sungold", facts=[shared]),
                mention("a#1", "Juliet"),
                mention("a#2", "Black Krim"),
            )
        ]
        assert rows(setup_counts(views)) == [("shelter", "greenhouse", "Greenhouse / tunnel", 1)]

    def test_a_mentions_own_value_and_a_siblings_whole_comment_value_both_count(self) -> None:
        views = [
            comment(
                "a",
                mention("a#0", "Sungold", facts=[fact("method", "container")]),
                mention(
                    "a#1",
                    "Juliet",
                    facts=[fact("method", "raised_bed", applicability="whole_comment")],
                ),
            )
        ]
        assert set(setup_counts(views)["value"]) == {"container", "raised_bed"}

    def test_ties_follow_the_vocabulary_order(self) -> None:
        views = [
            comment("a", mention("a#0", "Roma", facts=[fact("method", "hydroponic")])),
            comment("b", mention("b#0", "Roma", facts=[fact("method", "container")])),
        ]
        assert setup_counts(views)["value"].tolist() == ["container", "hydroponic"]

    def test_comments_stating_a_setup(self) -> None:
        views = [
            comment(
                "a",
                mention("a#0", "Roma", facts=[fact("method", "container")]),
                mention("a#1", "Roma", facts=[fact("shelter", "outdoors")]),
            ),
            comment("b", mention("b#0", "Roma", facts=[fact("zone", "6b")])),
            comment("c", mention("c#0", "Roma", facts=[fact("shelter", None)])),
        ]
        assert n_stating_setup(views) == 1


# --------------------------------------------------------------------------- summary and config


def overview_config(top: int = 10, heatmap: int = 8) -> OverviewConfig:
    return OverviewConfig(top_varieties=top, heatmap_varieties=heatmap)


class TestSummarize:
    def test_heatmap_rows_are_the_first_bars_in_the_same_order(self) -> None:
        views = [
            comment("a", mention("a#0", "Sungold", topics=("flavor",)), mention("a#1", "Roma")),
            comment("b", mention("b#0", "Sungold", topics=("pests",))),
            comment("c", mention("c#0", "Juliet", topics=("flavor",))),
        ]
        summary = summarize(views, overview_config(top=3, heatmap=2))
        assert summary.varieties["variety_key"].tolist() == ["sungold", "juliet", "roma"]
        assert list(dict.fromkeys(summary.topics["variety_key"])) == ["sungold", "juliet"]
        assert summary.headline.n_varieties == 3
        assert summary.n_with_setup == 0
        assert (summary.bars_tied_off, summary.rows_tied_off) == (0, 1)

    def test_every_visible_variety_is_kept_for_names_and_filters(self) -> None:
        views = [comment(c, mention(f"{c}#0", name)) for c, name in zip("abc", "XYZ", strict=True)]
        summary = summarize(views, overview_config(top=1, heatmap=1))
        assert summary.varieties["label"].tolist() == ["X"]
        assert summary.all_varieties["label"].tolist() == ["X", "Y", "Z"]
        assert summary.bars_tied_off == 2

    def test_shipped_configuration(self) -> None:
        assert load_config().overview == overview_config(top=10, heatmap=8)

    @pytest.mark.parametrize(
        ("section", "match"),
        [
            ({"top_varieties": 0, "heatmap_varieties": 1}, "top_varieties"),
            ({"top_varieties": 5, "heatmap_varieties": 0}, "heatmap_varieties"),
            ({"top_varieties": 5, "heatmap_varieties": 6}, "heatmap_varieties"),
            ({"top_varieties": 5}, "heatmap_varieties"),
            ({"top_varieties": 5, "heatmap_varieties": 2, "extra": 1}, "extra"),
        ],
    )
    def test_invalid_sections_fail_at_load_time(
        self, tmp_path: Path, section: dict[str, int], match: str
    ) -> None:
        raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        raw["overview"] = section
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        with pytest.raises(ValidationError, match=match):
            load_config(path)

    def test_the_section_is_required(self, tmp_path: Path) -> None:
        raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        del raw["overview"]
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        with pytest.raises(ValidationError, match="overview"):
            load_config(path)


class TestDemoDatabase:
    def test_headline_of_the_public_demo(self, demo_conn: sqlite3.Connection) -> None:
        views = db.load_comment_views(demo_conn, "public", NOW, None)
        result = headline(views)
        assert (result.n_reports, result.n_varieties) == (69, 44)
        assert (result.n_us_states, result.n_other_regions) == (18, 3)
        assert result.n_with_conditions == build_result(views, SearchQuery()).n_with_conditions
        top = variety_counts(views, top_n=3)
        assert rows(top)[0] == ("sungold", "Sungold", "tomato", 10)
        assert result.n_crops == 8  # cross-checked against the mentions table with SQL


# --------------------------------------------------------------------------- properties

SPELLINGS = ("Sungold", "Sun Gold", "Juliet", "Black Krim", None)
CROPS = ("tomato", "pepper", None)
FACT_KEYS: tuple[EnvKey, ...] = (*COMPARABLE_KEYS, "region", "growing_year")
REGIONS = ("US-CT", "US-ME", "CA-ON", "GB", "US")


def choices(key: EnvKey) -> tuple[str, ...]:
    special = {"region": REGIONS, "zone": ("6", "6b"), "growing_year": ("2023",)}
    return special.get(key) or CATEGORICAL_VALUES[key]


@st.composite
def fact_views(draw: st.DrawFn, fact_id: str) -> FactView:
    key = draw(st.sampled_from(FACT_KEYS))
    value = draw(st.none() | st.sampled_from(choices(key)))
    applicability = draw(st.sampled_from(("this_mention", "whole_comment")))
    return fact(key, value, applicability=applicability).model_copy(update={"fact_id": fact_id})


@st.composite
def mention_views(draw: st.DrawFn, mention_id: str) -> MentionView:
    n_facts = draw(st.integers(0, 3))
    return mention(
        mention_id,
        draw(st.sampled_from(SPELLINGS)),
        crop=draw(st.sampled_from(CROPS)),
        topics=tuple(draw(st.lists(st.sampled_from(("flavor", "disease", "pests")), unique=True))),
        facts=[draw(fact_views(f"{mention_id}#f{j}")) for j in range(n_facts)],
    )


@st.composite
def view_lists(draw: st.DrawFn) -> list[CommentView]:
    views = []
    for i in range(draw(st.integers(0, 6))):
        n_mentions = draw(st.integers(0, 3))
        mentions = [draw(mention_views(f"c{i}#m{j}")) for j in range(n_mentions)]
        views.append(comment(f"c{i}", *mentions, status="parsed" if mentions else None))
    return views


def everything(views: Sequence[CommentView]) -> tuple[Any, ...]:
    summary = summarize(views, overview_config(top=3, heatmap=2))
    return (
        summary.headline,
        rows(summary.varieties),
        rows(summary.topics),
        rows(summary.setups),
        summary.n_with_setup,
    )


class TestProperties:
    @settings(max_examples=150, deadline=None)
    @given(views=view_lists())
    def test_counts_never_exceed_the_reports(self, views: list[CommentView]) -> None:
        summary = summarize(views, overview_config(top=50, heatmap=50))
        n = summary.headline.n_reports
        for frame in (summary.varieties, summary.topics, summary.setups):
            assert (frame["n_comments"] <= n).all()
            assert (frame["n_comments"] >= 0).all()
        assert summary.headline.n_with_conditions <= n
        assert summary.n_with_setup <= n
        assert (summary.varieties["n_comments"] >= 1).all()

    @settings(max_examples=150, deadline=None)
    @given(views=view_lists(), data=st.data())
    def test_the_order_of_the_views_does_not_matter(
        self, views: list[CommentView], data: st.DataObject
    ) -> None:
        shuffled = data.draw(st.permutations(views))
        assert everything(shuffled) == everything(views)

    @settings(max_examples=100, deadline=None)
    @given(views=view_lists())
    def test_deterministic(self, views: list[CommentView]) -> None:
        assert everything(views) == everything(list(views))

    @settings(max_examples=150, deadline=None)
    @given(views=view_lists())
    def test_conditions_use_the_search_definition(self, views: list[CommentView]) -> None:
        expected = build_result(views, SearchQuery()).n_with_conditions
        assert headline(views).n_with_conditions == expected

    @settings(max_examples=100, deadline=None)
    @given(views=view_lists())
    def test_total_varieties_match_the_bar_data(self, views: list[CommentView]) -> None:
        assert len(variety_counts(views, top_n=1000)) == headline(views).n_varieties

    @settings(max_examples=100, deadline=None)
    @given(views=view_lists())
    def test_a_bar_counts_exactly_the_reports_its_click_shows(
        self, views: list[CommentView]
    ) -> None:
        for key, n in zip(
            *(variety_counts(views, top_n=None)[c] for c in ("variety_key", "n_comments")),
            strict=True,
        ):
            assert build_result(views, SearchQuery(variety=key)).n_candidates == n

    @settings(max_examples=100, deadline=None)
    @given(views=view_lists())
    def test_a_cell_counts_exactly_the_reports_its_click_shows(
        self, views: list[CommentView]
    ) -> None:
        keys = variety_counts(views, top_n=None)["variety_key"].tolist()
        grid = topic_matrix(views, keys)
        for key, topic, n in zip(
            grid["variety_key"], grid["topic"], grid["n_comments"], strict=True
        ):
            query = SearchQuery(variety=key, topics=(topic,))
            assert build_result(views, query).n_candidates == n
