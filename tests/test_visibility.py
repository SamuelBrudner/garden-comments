"""Tests for rights and expiry gating of viewer-facing reads (SPEC §9, §11 #15).

The visibility clause is exercised against a tiny independent SQLite schema (not ``db.py``) so
its semantics are pinned down on their own: rights, expiry and soft deletion.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from crop_explorer.schemas import RIGHTS_STATUSES, RightsStatus, to_utc_iso
from crop_explorer.visibility import (
    PUBLIC_RIGHTS,
    require_utc_iso,
    visibility_clause,
    visible_rights,
)

PAST = "2026-01-01T00:00:00Z"
NOW = "2026-10-15T12:00:00Z"
FUTURE = "2027-01-01T00:00:00Z"

# Independent oracle: what each scope may see (SPEC §9).
SCOPE_SEES: dict[str, frozenset[str]] = {
    "public": frozenset({"synthetic", "cleared"}),
    "private": frozenset({"private_unverified", "synthetic", "cleared"}),
}

Row = tuple[RightsStatus, str | None, str | None]  # (rights_status, expires_at, deleted_at)

rows_strategy = st.lists(
    st.tuples(
        st.sampled_from(RIGHTS_STATUSES),
        st.sampled_from([None, PAST, NOW, FUTURE]),
        st.sampled_from([None, PAST]),
    ),
    max_size=12,
)
scopes = st.sampled_from(["public", "private"])


def gate_db(rows: Sequence[Row]) -> sqlite3.Connection:
    """One source per row, each with one comment ``c{i}``; aliases are chosen by the query."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE src (source_id TEXT, rights_status TEXT, expires_at TEXT)")
    conn.execute("CREATE TABLE cm (comment_id TEXT, source_id TEXT, deleted_at TEXT)")
    for i, (rights, expires_at, deleted_at) in enumerate(rows):
        conn.execute("INSERT INTO src VALUES (?, ?, ?)", (f"s{i}", rights, expires_at))
        conn.execute("INSERT INTO cm VALUES (?, ?, ?)", (f"c{i}", f"s{i}", deleted_at))
    return conn


def select_ids(conn: sqlite3.Connection, where: str, params: Sequence[str]) -> set[str]:
    sql = f"SELECT c.comment_id FROM cm c JOIN src s ON s.source_id = c.source_id WHERE {where}"
    return {row[0] for row in conn.execute(sql, params)}


def oracle(rows: Sequence[Row], scope: str, now: str) -> set[str]:
    return {
        f"c{i}"
        for i, (rights, expires_at, deleted_at) in enumerate(rows)
        if rights in SCOPE_SEES[scope]
        and (expires_at is None or expires_at > now)
        and deleted_at is None
    }


@pytest.fixture
def one_row_db() -> Iterator[sqlite3.Connection]:
    conn = gate_db([("synthetic", None, None)])
    yield conn
    conn.close()


# --------------------------------------------------------------------------- visible_rights


def test_public_rights_are_exactly_synthetic_and_cleared() -> None:
    assert frozenset({"synthetic", "cleared"}) == PUBLIC_RIGHTS
    assert visible_rights("public") == PUBLIC_RIGHTS
    assert "private_unverified" not in visible_rights("public")


def test_private_scope_sees_every_rights_status() -> None:
    assert visible_rights("private") == frozenset(RIGHTS_STATUSES)


@pytest.mark.parametrize("scope", ["Public", "PRIVATE", "", "admin", "public "])
def test_unknown_scope_raises_value_error(scope: Any) -> None:
    with pytest.raises(ValueError, match="scope"):
        visible_rights(scope)
    with pytest.raises(ValueError, match="scope"):
        visibility_clause(scope, NOW)


# --------------------------------------------------------------------------- visibility_clause


@given(rows=rows_strategy, scope=scopes)
def test_clause_selects_exactly_visible_rows(rows: list[Row], scope: Any) -> None:
    conn = gate_db(rows)
    where, params = visibility_clause(scope, NOW)
    assert select_ids(conn, where, params) == oracle(rows, scope, NOW)


@given(rows=rows_strategy, scope=scopes)
def test_negated_clause_selects_exactly_invisible_rows(rows: list[Row], scope: Any) -> None:
    """The fragment composes as a single boolean term (safe under NOT / OR)."""
    conn = gate_db(rows)
    where, params = visibility_clause(scope, NOW)
    everything = {f"c{i}" for i in range(len(rows))}
    assert select_ids(conn, f"NOT {where}", params) == everything - oracle(rows, scope, NOW)


@given(rows=rows_strategy)
def test_public_visible_rows_are_a_subset_of_private(rows: list[Row]) -> None:
    conn = gate_db(rows)
    public = select_ids(conn, *visibility_clause("public", NOW))
    private = select_ids(conn, *visibility_clause("private", NOW))
    assert public <= private


def test_source_expiring_exactly_now_is_invisible(one_row_db: sqlite3.Connection) -> None:
    one_row_db.execute("UPDATE src SET expires_at = ?", (NOW,))
    for scope in ("public", "private"):
        assert select_ids(one_row_db, *visibility_clause(scope, NOW)) == set()
    assert select_ids(one_row_db, *visibility_clause("public", PAST)) == {"c0"}


def test_custom_aliases_are_honoured() -> None:
    conn = gate_db([("cleared", FUTURE, None), ("private_unverified", None, None)])
    where, params = visibility_clause("public", NOW, source_alias="srcx", comment_alias="cmx")
    sql = f"SELECT cmx.comment_id FROM cm cmx JOIN src srcx USING (source_id) WHERE {where}"
    assert [row[0] for row in conn.execute(sql, params)] == ["c0"]


def test_values_are_bound_parameters_never_interpolated() -> None:
    where, params = visibility_clause("public", NOW)
    assert NOW not in where
    assert not any(status in where for status in RIGHTS_STATUSES)
    assert where.count("?") == len(params)
    assert params == ["cleared", "synthetic", NOW]


@pytest.mark.parametrize(
    "alias", ["", "1s", "s.x", "s c", "s; DROP TABLE src; --", "s'", '"s"', "s\n"]
)
def test_unsafe_alias_raises_value_error(alias: str) -> None:
    with pytest.raises(ValueError, match="alias"):
        visibility_clause("public", NOW, source_alias=alias)
    with pytest.raises(ValueError, match="alias"):
        visibility_clause("public", NOW, comment_alias=alias)


# --------------------------------------------------------------------------- timestamps

NON_CANONICAL = [
    "",
    "now",
    "2026-10-15",
    "2026-10-15 12:00:00",
    "2026-10-15T12:00:00",
    "2026-10-15T12:00:00+00:00",
    "2026-10-15T12:00:00.000Z",
    "2026-10-15t12:00:00z",
    "2026-02-30T00:00:00Z",
    " 2026-10-15T12:00:00Z",
]


@pytest.mark.parametrize("now", NON_CANONICAL)
def test_non_canonical_now_raises_value_error(now: str) -> None:
    with pytest.raises(ValueError, match="now"):
        visibility_clause("private", now)
    with pytest.raises(ValueError, match="now"):
        require_utc_iso(now, "now")


@given(moment=st.datetimes(min_value=datetime(1000, 1, 1), timezones=st.just(UTC)))
def test_canonical_timestamps_are_accepted_unchanged(moment: datetime) -> None:
    canonical = to_utc_iso(moment)
    assert require_utc_iso(canonical, "now") == canonical
    _, params = visibility_clause("private", canonical)
    assert params[-1] == canonical


def test_require_utc_iso_names_the_offending_field() -> None:
    with pytest.raises(ValueError, match="expires_at"):
        require_utc_iso("tomorrow", "expires_at")
