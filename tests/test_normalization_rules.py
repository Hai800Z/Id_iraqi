from datetime import date

import pytest

from idcard_extractor.processing.normalization import (
    FEMALE,
    MALE,
    clean_text,
    normalize_digits,
    normalize_national_id,
    normalize_registration_number,
    normalize_sex,
    parse_mrz_date,
    same_value,
)
from idcard_extractor.processing.rules import (
    birth_year_matches_national_id,
    is_valid_national_id,
    is_valid_registration_number,
)

TODAY = date(2026, 9, 24)


def test_clean_text_collapses_whitespace():
    assert clean_text("  أحمد \n  علي\t") == "أحمد علي"
    assert clean_text(None) == ""


def test_arabic_indic_digits_are_converted():
    assert normalize_digits("١٢٧٩") == "1279"
    assert normalize_national_id("١٢٧٩ ٠١٢٣ ٤٥٦٧") == "127901234567"


def test_registration_number_normalization():
    assert normalize_registration_number(" a1234 5678< ") == "A12345678"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ذكر", MALE), ("دكر", MALE), ("ذكرر", MALE), ("M", MALE), ("male", MALE),
        ("أنثى", FEMALE), ("انثى", FEMALE), ("انثي", FEMALE), ("f", FEMALE),
        ("<", ""), ("", ""), (None, ""), ("xyz", ""),
    ],
)
def test_normalize_sex(raw, expected):
    assert normalize_sex(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("790105", date(1979, 1, 5)),
        ("200315", date(2020, 3, 15)),
        ("260101", date(2026, 1, 1)),
        ("261231", date(1926, 12, 31)),  # later this year -> previous century
        ("791305", None),                # month 13
        ("12345", None),
        ("", None),
    ],
)
def test_parse_mrz_date(raw, expected):
    assert parse_mrz_date(raw, today=TODAY) == expected


def test_same_value_ignores_spaces_but_not_empty():
    assert same_value("عبد الله", "عبدالله")
    assert not same_value("", "")


@pytest.mark.parametrize(
    ("value", "valid"),
    [("127901234567", True), ("١٢٧٩٠١٢٣٤٥٦٧", True), ("12790123456", False), ("12790123456X", False)],
)
def test_national_id_format(value, valid):
    assert is_valid_national_id(value) is valid


@pytest.mark.parametrize(
    ("value", "valid"),
    [("A12345678", True), ("AB1234567", True), ("ABC123456", False), ("A1234567", False), ("123456789", False)],
)
def test_registration_number_format(value, valid):
    assert is_valid_registration_number(value) is valid


def test_birth_year_rule():
    assert birth_year_matches_national_id("127901234567", "790105")
    assert not birth_year_matches_national_id("127901234567", "800105")
    assert not birth_year_matches_national_id("1279", "790105")
