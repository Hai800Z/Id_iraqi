"""Text normalisation for OCR / MRZ output."""

from __future__ import annotations

import re
from datetime import date
from difflib import SequenceMatcher
from typing import Optional

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

MALE = "ذكر"
FEMALE = "أنثى"

_MALE_VALUES = {"ذكر", "ذکر", "دكر", "دکر", "male", "m"}
_FEMALE_VALUES = {"أنثى", "انثى", "انث", "أنث", "انثي", "أنثي", "female", "f"}
_MALE_FUZZY = ("ذكر", "ذکر", "male")
_FEMALE_FUZZY = ("أنثى", "انثى", "انث", "female")


def clean_text(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_digits(value) -> str:
    return clean_text(value).translate(_ARABIC_DIGITS)


def normalize_compact(value) -> str:
    return re.sub(r"\s+", "", clean_text(value))


def normalize_national_id(value) -> str:
    return normalize_digits(value).replace(" ", "")


def normalize_registration_number(value) -> str:
    return clean_text(value).replace(" ", "").replace("<", "").upper()


def normalize_date_digits(value) -> str:
    """Keep only the digits of an MRZ date (expected ``YYMMDD``)."""
    return re.sub(r"[^0-9]", "", normalize_digits(value))


def parse_mrz_date(value, today: Optional[date] = None) -> Optional[date]:
    """Convert ``YYMMDD`` to a date. Birth dates are never in the future,
    so a two-digit year later than this year belongs to the 1900s."""
    digits = normalize_date_digits(value)
    if len(digits) != 6:
        return None
    today = today or date.today()
    yy, mm, dd = int(digits[:2]), int(digits[2:4]), int(digits[4:6])
    year = 2000 + yy if 2000 + yy <= today.year else 1900 + yy
    try:
        parsed = date(year, mm, dd)
        if parsed > today:  # e.g. "261231" read on 2026-09-23 -> 1926-12-31
            parsed = date(year - 100, mm, dd)
    except ValueError:
        return None
    return parsed


def normalize_sex(value) -> str:
    """Map OCR / MRZ readings of the sex field to ``ذكر`` or ``أنثى`` ('' if unreadable)."""
    compact = re.sub(r"\s+", "", clean_text(value).lower())
    if not compact:
        return ""
    if compact in _MALE_VALUES:
        return MALE
    if compact in _FEMALE_VALUES:
        return FEMALE

    best_male = max(SequenceMatcher(None, compact, x).ratio() for x in _MALE_FUZZY)
    best_female = max(SequenceMatcher(None, compact, x).ratio() for x in _FEMALE_FUZZY)
    if max(best_male, best_female) >= 0.70:
        return MALE if best_male >= best_female else FEMALE
    return ""


def same_value(a, b) -> bool:
    a, b = normalize_compact(a), normalize_compact(b)
    return bool(a) and bool(b) and a == b
