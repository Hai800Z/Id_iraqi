from datetime import date

import pytest

from idcard_extractor.pipeline import (
    REASON_DETECTION_REJECTED,
    REASON_NO_BACK,
    REASON_PROCESSING_ERROR,
    combine_sides,
)
from idcard_extractor.processing.matching import match_sides
from idcard_extractor.processing.validation import validate_pair


def test_pairs_by_national_id_first(make_front, make_back):
    front, back = make_front(), make_back()
    result = match_sides([front, back])
    assert len(result.matched) == 1
    assert result.matched[0].match_method == "national_id"


def test_falls_back_to_registration_number(make_front, make_back):
    front = make_front(id="")
    back = make_back(mrz={"number": "A12345678", "date_of_birth": "790105", "sex": "M", "optional1": ""})
    result = match_sides([front, back])
    assert result.matched[0].match_method == "registration_number"


def test_never_pairs_different_cards(make_front, make_back):
    front = make_front(id="127901234567", id2="A12345678")
    back = make_back(mrz={"number": "B87654321", "date_of_birth": "850505", "sex": "F",
                          "optional1": "228501234567"})
    result = match_sides([front, back])
    assert not result.matched
    assert result.unmatched_fronts == [front] and result.unmatched_backs == [back]


def test_accepted_record(make_front, make_back):
    outcome = validate_pair(make_front(), make_back())
    assert outcome.accepted, outcome.reason
    record = outcome.record
    assert record.first_name == "أحمد"
    assert record.family_name == "الكعبي"
    assert record.national_id == "127901234567"
    assert record.registration_number == "A12345678"
    assert record.date_of_birth == date(1979, 1, 5)
    assert record.sex == "ذكر"
    assert record.birth_place == "بغداد"
    assert record.family_number == "12345"


def test_ocr_whitespace_is_normalized(make_front, make_back):
    front = make_front(name="  أحمد\n", mom="فاطمة  الزهراء\t", id=" 127901234567 ")
    record = validate_pair(front, make_back()).record
    assert record.first_name == "أحمد"
    assert record.mother_name == "فاطمة الزهراء"
    assert record.national_id == "127901234567"


def test_impossible_date_of_birth_is_rejected(make_front, make_back):
    back = make_back()
    back.mrz["date_of_birth"] = "791305"  # six digits, but month 13
    assert validate_pair(make_front(), back).reason == "تاريخ الميلاد غير صالح أو غير موجود في MRZ"


@pytest.mark.parametrize(
    ("front_overrides", "mrz_overrides", "reason"),
    [
        ({"id": "127901234568"}, {}, "اختلاف الرقم الوطني بين Text و MRZ"),
        ({"id": "", }, {"optional1": ""}, "الرقم الوطني غير موجود"),
        ({"id": "12790123"}, {"optional1": ""}, "الرقم الوطني غير صالح"),
        ({"id2": "A12345679"}, {}, "اختلاف رقم القيد بين Text و MRZ"),
        ({"id2": "A1234"}, {"number": ""}, "رقم القيد غير صالح"),
        ({"gf": "الكعبي"}, {}, "gf و last متطابقان"),
        ({}, {"date_of_birth": ""}, "تاريخ الميلاد غير صالح أو غير موجود في MRZ"),
        ({}, {"date_of_birth": "800105"}, "سنة الميلاد لا تطابق الخانتين الثالثة والرابعة من الرقم الوطني"),
        ({"gn": "أنثى"}, {}, "الجنس في Text لا يطابق الجنس في MRZ"),
        ({"gn": ""}, {"sex": "<"}, "الجنس غير قابل للتحديد"),
    ],
)
def test_rejection_reasons(make_front, make_back, front_overrides, mrz_overrides, reason):
    back = make_back()
    back.mrz.update(mrz_overrides)
    outcome = validate_pair(make_front(**front_overrides), back)
    assert not outcome.accepted
    assert outcome.reason == reason


def test_combine_sides_reports_every_rejection(make_front, make_back):
    front, back = make_front("CARD_001"), make_back("CARD_002")
    lonely_front = make_front("CARD_003", id="229901234567", id2="B11111111")
    low_confidence = make_back("CARD_004", accepted=False)
    crashed = make_front("CARD_005")
    crashed.error = "RuntimeError"
    licence = make_front("CARD_006")
    licence.card_class = "drive"

    accepted, rejected = combine_sides([front, back, lonely_front, low_confidence, crashed, licence])

    assert [card.card_id for card in accepted] == ["CARD_001"]
    reasons = {card.card_id: card.reason for card in rejected}
    assert reasons == {
        "CARD_003": REASON_NO_BACK,
        "CARD_004": REASON_DETECTION_REJECTED,
        "CARD_005": REASON_PROCESSING_ERROR,
        "CARD_006": "نوع مستند غير مدعوم: drive",
    }
    by_id = {card.card_id: card for card in rejected}
    assert by_id["CARD_004"].back is low_confidence
    assert by_id["CARD_006"].sides == [licence]
