"""Rights and expiry gating applied to every viewer-facing read (SPEC §9, §11 #15).

``private_unverified`` content must never reach a public result, count, chip, map cell, snippet
or cache. Every viewer-facing query in :mod:`crop_explorer.db` joins ``comments c JOIN sources s``
and applies :func:`visibility_clause`; nothing else decides what a viewer may see.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import Final

from crop_explorer.schemas import RIGHTS_STATUSES, RightsStatus, to_utc_iso
from crop_explorer.views import ViewerScope

__all__ = [
    "PUBLIC_RIGHTS",
    "require_utc_iso",
    "visibility_clause",
    "visible_rights",
]

PUBLIC_RIGHTS: Final[frozenset[RightsStatus]] = frozenset({"synthetic", "cleared"})
"""Rights states that a public viewer may see; ``private_unverified`` is never among them."""

_SCOPE_RIGHTS: Final = MappingProxyType(
    {"public": PUBLIC_RIGHTS, "private": frozenset(RIGHTS_STATUSES)}
)
_IDENTIFIER: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_UTC_ISO: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def visible_rights(scope: ViewerScope) -> frozenset[RightsStatus]:
    """Return the source rights states visible in ``scope``.

    Parameters
    ----------
    scope
        ``"public"`` (synthetic and cleared sources only) or ``"private"`` (every source).

    Returns
    -------
    frozenset[RightsStatus]
        The rights states a viewer in ``scope`` may see.

    Raises
    ------
    ValueError
        If ``scope`` is not a known viewer scope.
    """
    if scope not in _SCOPE_RIGHTS:
        known = ", ".join(sorted(_SCOPE_RIGHTS))
        raise ValueError(f"Unknown viewer scope {scope!r}; expected one of: {known}")
    return _SCOPE_RIGHTS[scope]


def visibility_clause(
    scope: ViewerScope, now: str, *, source_alias: str = "s", comment_alias: str = "c"
) -> tuple[str, list[str]]:
    """Build the SQL boolean term selecting rows visible in ``scope`` at ``now``.

    The term is parenthesised so it composes safely with ``AND``/``OR``/``NOT``. All values are
    bound parameters; only the validated aliases are interpolated.

    Parameters
    ----------
    scope
        Viewer scope, see :func:`visible_rights`.
    now
        Current time in canonical UTC form (``YYYY-MM-DDTHH:MM:SSZ``). Sources whose
        ``expires_at`` is at or before ``now`` are invisible.
    source_alias, comment_alias
        SQL aliases of the joined ``sources`` and ``comments`` tables.

    Returns
    -------
    tuple[str, list[str]]
        ``(sql_fragment, parameters)``: rights in the visible set, source not expired and comment
        not soft-deleted. Parameters are the sorted rights states followed by ``now``.

    Raises
    ------
    ValueError
        If ``scope`` is unknown, ``now`` is not canonical UTC or an alias is not a plain SQL
        identifier.
    """
    rights = sorted(visible_rights(scope))
    require_utc_iso(now, "now")
    src = _require_alias(source_alias)
    com = _require_alias(comment_alias)
    placeholders = ", ".join("?" for _ in rights)
    sql = (
        f"({src}.rights_status IN ({placeholders})"
        f" AND ({src}.expires_at IS NULL OR {src}.expires_at > ?)"
        f" AND {com}.deleted_at IS NULL)"
    )
    return sql, [*rights, now]


def require_utc_iso(value: str, name: str) -> str:
    """Return ``value`` unchanged if it is a canonical UTC timestamp, else raise.

    Stored timestamps are compared lexicographically, which equals temporal order only for the
    canonical ``YYYY-MM-DDTHH:MM:SSZ`` form produced by :func:`crop_explorer.schemas.to_utc_iso`.

    Parameters
    ----------
    value
        Timestamp to check.
    name
        Field name used in the error message.

    Returns
    -------
    str
        ``value`` itself.

    Raises
    ------
    ValueError
        If ``value`` is not a valid canonical UTC timestamp.
    """
    if not _is_canonical_utc(value):
        raise ValueError(
            f"{name} must be a canonical UTC timestamp like 2026-01-31T23:59:00Z, got {value!r}"
        )
    return value


def _is_canonical_utc(value: str) -> bool:
    if _UTC_ISO.fullmatch(value) is None:
        return False
    try:
        return to_utc_iso(value) == value
    except ValueError:
        return False


def _require_alias(alias: str) -> str:
    if _IDENTIFIER.fullmatch(alias) is None:
        raise ValueError(f"SQL alias must be a plain identifier, got {alias!r}")
    return alias
