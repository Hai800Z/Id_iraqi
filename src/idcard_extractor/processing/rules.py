"""Field-level format rules for Iraqi unified national ID cards."""

from __future__ import annotations

import re

from idcard_extractor.processing.normalization import (
    normalize_date_digits,
    normalize_national_id,
    normalize_registration_number,
)


def is_valid_national_id(value) -> bool:
    """National ID: exactly 12 digits."""
    value = normalize_national_id(value)
    return len(value) == 12 and value.isdigit()


def is_valid_registration_number(value) -> bool:
    """Registration (document) number: 9 characters, 1-2 letters then digits.

    Examples: ``A12345678``, ``AB1234567``.
    """
    value = normalize_registration_number(value)
    return len(value) == 9 and bool(re.fullmatch(r"[A-Z]{1,2}[0-9]{7,8}", value))


def is_valid_date_digits(value) -> bool:
    """MRZ date: exactly six digits (``YYMMDD``)."""
    return len(normalize_date_digits(value)) == 6


def birth_year_matches_national_id(national_id, date_of_birth) -> bool:
    """The 3rd and 4th digits of the national ID are the two-digit birth year.

    ``1 2 [7 9] 0 1 2 3 4 5 6 7``  <->  MRZ DOB ``[79]0105``
    """
    national_id = normalize_national_id(national_id)
    dob = normalize_date_digits(date_of_birth)
    if not is_valid_national_id(national_id) or len(dob) != 6:
        return False
    return national_id[2:4] == dob[0:2]
