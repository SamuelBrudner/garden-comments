"""Tests for the shared timestamp helpers and the strict record models in ``schemas.py``.

Canonical UTC strings are compared lexicographically by the database and the visibility gate
(expiry), so their format must be fixed-width and order-preserving.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from crop_explorer.schemas import (
    SCOPE_TO_RIGHTS,
    CommentRecord,
    EnvironmentFact,
    ParsedComment,
    to_utc_iso,
    utc_now_iso,
)
from crop_explorer.visibility import require_utc_iso

CANONICAL = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-15", "2026-10-15T00:00:00Z"),
        ("2026-10-15T12:30:05Z", "2026-10-15T12:30:05Z"),
        ("2026-10-15T12:30:05+02:00", "2026-10-15T10:30:05Z"),
        ("  2026-10-15T12:30:05.999Z  ", "2026-10-15T12:30:05Z"),
        (date(2024, 2, 29), "2024-02-29T00:00:00Z"),
        (datetime(2024, 1, 1, 23, 0), "2024-01-01T23:00:00Z"),
        (datetime(2024, 1, 1, 1, 0, tzinfo=timezone(timedelta(hours=3))), "2023-12-31T22:00:00Z"),
    ],
)
def test_to_utc_iso_normalises_dates_and_offsets(value: str | date, expected: str) -> None:
    assert to_utc_iso(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0999-01-01", "0999-01-01T00:00:00Z"),
        ("0001-01-01T00:00:00Z", "0001-01-01T00:00:00Z"),
        (date(42, 3, 4), "0042-03-04T00:00:00Z"),
    ],
)
def test_to_utc_iso_always_writes_a_four_digit_year(value: str | date, expected: str) -> None:
    """Early years are zero-padded so string order stays time order (and the gate accepts them)."""
    assert to_utc_iso(value) == expected
    assert require_utc_iso(expected, "t") == expected


@pytest.mark.parametrize("value", ["", "yesterday", "2026-13-01", "15/10/2026"])
def test_to_utc_iso_rejects_unparseable_strings(value: str) -> None:
    with pytest.raises(ValueError):
        to_utc_iso(value)


@given(
    st.datetimes(
        min_value=datetime(1, 1, 2),
        max_value=datetime(9999, 12, 30),
        timezones=st.just(UTC),
    ),
    st.datetimes(
        min_value=datetime(1, 1, 2),
        max_value=datetime(9999, 12, 30),
        timezones=st.just(UTC),
    ),
)
def test_canonical_strings_sort_like_the_times_they_denote(a: datetime, b: datetime) -> None:
    first, second = to_utc_iso(a), to_utc_iso(b)
    assert len(first) == len(second) == 20
    whole_a, whole_b = a.replace(microsecond=0), b.replace(microsecond=0)
    assert (first < second) == (whole_a < whole_b)
    assert to_utc_iso(first) == first  # idempotent


def test_utc_now_iso_is_canonical() -> None:
    now = utc_now_iso()
    assert require_utc_iso(now, "now") == now


def test_records_forbid_unknown_fields_and_blank_quotes() -> None:
    with pytest.raises(ValidationError):
        CommentRecord.model_validate({"comment_id": "c1", "text": "t", "source_order": 0, "x": 1})
    with pytest.raises(ValidationError):
        EnvironmentFact(
            key="zone",
            value_as_written="zone 6b",
            evidence_quote="",
            applicability="this_mention",
            basis="reported_garden_context",
        )
    with pytest.raises(ValidationError):
        ParsedComment.model_validate(
            {"comment_id": "c1", "extractor_version": "v1", "status": "maybe"}
        )


def test_private_import_scope_maps_to_the_unverified_rights_status() -> None:
    assert SCOPE_TO_RIGHTS == {
        "private": "private_unverified",
        "synthetic": "synthetic",
        "cleared": "cleared",
    }
