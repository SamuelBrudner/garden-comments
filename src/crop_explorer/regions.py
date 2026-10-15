"""Controlled region vocabulary: ISO 3166 codes and display names.

Normalized region values are ISO 3166-2 subdivision codes for U.S. states (incl. DC) and
Canadian provinces/territories (``US-CT``, ``CA-ON``), or ISO 3166-1 alpha-2 codes for
countries (``GB``). Only explicitly stated, unambiguous places become region facts.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

US_STATES: Final = MappingProxyType(
    {
        "US-AL": "Alabama",
        "US-AK": "Alaska",
        "US-AZ": "Arizona",
        "US-AR": "Arkansas",
        "US-CA": "California",
        "US-CO": "Colorado",
        "US-CT": "Connecticut",
        "US-DE": "Delaware",
        "US-DC": "District of Columbia",
        "US-FL": "Florida",
        "US-GA": "Georgia",
        "US-HI": "Hawaii",
        "US-ID": "Idaho",
        "US-IL": "Illinois",
        "US-IN": "Indiana",
        "US-IA": "Iowa",
        "US-KS": "Kansas",
        "US-KY": "Kentucky",
        "US-LA": "Louisiana",
        "US-ME": "Maine",
        "US-MD": "Maryland",
        "US-MA": "Massachusetts",
        "US-MI": "Michigan",
        "US-MN": "Minnesota",
        "US-MS": "Mississippi",
        "US-MO": "Missouri",
        "US-MT": "Montana",
        "US-NE": "Nebraska",
        "US-NV": "Nevada",
        "US-NH": "New Hampshire",
        "US-NJ": "New Jersey",
        "US-NM": "New Mexico",
        "US-NY": "New York",
        "US-NC": "North Carolina",
        "US-ND": "North Dakota",
        "US-OH": "Ohio",
        "US-OK": "Oklahoma",
        "US-OR": "Oregon",
        "US-PA": "Pennsylvania",
        "US-RI": "Rhode Island",
        "US-SC": "South Carolina",
        "US-SD": "South Dakota",
        "US-TN": "Tennessee",
        "US-TX": "Texas",
        "US-UT": "Utah",
        "US-VT": "Vermont",
        "US-VA": "Virginia",
        "US-WA": "Washington",
        "US-WV": "West Virginia",
        "US-WI": "Wisconsin",
        "US-WY": "Wyoming",
    }
)

CA_PROVINCES: Final = MappingProxyType(
    {
        "CA-AB": "Alberta",
        "CA-BC": "British Columbia",
        "CA-MB": "Manitoba",
        "CA-NB": "New Brunswick",
        "CA-NL": "Newfoundland and Labrador",
        "CA-NS": "Nova Scotia",
        "CA-NT": "Northwest Territories",
        "CA-NU": "Nunavut",
        "CA-ON": "Ontario",
        "CA-PE": "Prince Edward Island",
        "CA-QC": "Quebec",
        "CA-SK": "Saskatchewan",
        "CA-YT": "Yukon",
    }
)

COUNTRIES: Final = MappingProxyType(
    {
        "AR": "Argentina",
        "AT": "Austria",
        "AU": "Australia",
        "BE": "Belgium",
        "BR": "Brazil",
        "CA": "Canada",
        "CH": "Switzerland",
        "CL": "Chile",
        "CN": "China",
        "CZ": "Czechia",
        "DE": "Germany",
        "DK": "Denmark",
        "ES": "Spain",
        "FI": "Finland",
        "FR": "France",
        "GB": "United Kingdom",
        "GR": "Greece",
        "HU": "Hungary",
        "IE": "Ireland",
        "IL": "Israel",
        "IN": "India",
        "IT": "Italy",
        "JM": "Jamaica",
        "JP": "Japan",
        "KE": "Kenya",
        "KR": "South Korea",
        "MX": "Mexico",
        "NG": "Nigeria",
        "NL": "Netherlands",
        "NO": "Norway",
        "NZ": "New Zealand",
        "PH": "Philippines",
        "PL": "Poland",
        "PT": "Portugal",
        "RO": "Romania",
        "SE": "Sweden",
        "SG": "Singapore",
        "TR": "Turkey",
        "UA": "Ukraine",
        "US": "United States",
        "ZA": "South Africa",
    }
)

REGION_NAMES: Final = MappingProxyType({**US_STATES, **CA_PROVINCES, **COUNTRIES})
"""Every valid normalized region code mapped to its display name."""


def is_us_state(code: str) -> bool:
    """Return whether ``code`` is a U.S. state (or DC) subdivision code like ``US-CT``."""
    return code in US_STATES


def us_postal(code: str) -> str:
    """Return the two-letter postal abbreviation for a U.S. state code (``US-CT`` -> ``CT``).

    Raises
    ------
    KeyError
        If ``code`` is not a U.S. state code.
    """
    if code not in US_STATES:
        raise KeyError(f"Not a U.S. state code: {code!r}")
    return code.removeprefix("US-")


def region_name(code: str) -> str:
    """Return the display name of a normalized region code.

    Raises
    ------
    KeyError
        If ``code`` is not in the controlled vocabulary.
    """
    return REGION_NAMES[code]
