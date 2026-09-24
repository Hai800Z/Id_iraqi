"""Cross-validation of a matched front/back pair into a final IdentityRecord.

Rejection reasons are kept in Arabic because they are shown to the operators.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from idcard_extractor.models import CardSide, IdentityRecord
from idcard_extractor.processing.matching import (
    back_national_id,
    back_registration,
    front_national_id,
    front_registration,
    mrz_value,
)
from idcard_extractor.processing.normalization import (
    normalize_date_digits,
    normalize_sex,
    parse_mrz_date,
    same_value,
)
from idcard_extractor.processing.rules import (
    birth_year_matches_national_id,
    is_valid_date_digits,
    is_valid_national_id,
    is_valid_registration_number,
)


@dataclass
class ValidationResult:
    accepted: bool
    reason: str
    record: Optional[IdentityRecord] = None


def _reject(reason: str) -> ValidationResult:
    return ValidationResult(False, reason)


def validate_pair(front: Optional[CardSide], back: Optional[CardSide]) -> ValidationResult:
    if front is None:
        return _reject("لا يوجد وجه")
    if back is None:
        return _reject("لا يوجد ظهر")

    text_id = front_national_id(front)
    text_reg = front_registration(front)
    text_sex = normalize_sex(front.field_text("gn"))

    mrz_id = back_national_id(back)
    mrz_reg = back_registration(back)
    mrz_dob = normalize_date_digits(mrz_value(back, "date_of_birth"))
    mrz_sex = normalize_sex(mrz_value(back, "sex"))

    # National ID: primary key, mandatory, both sides must agree.
    if not text_id and not mrz_id:
        return _reject("الرقم الوطني غير موجود")
    national_id = text_id or mrz_id
    if not is_valid_national_id(national_id):
        return _reject("الرقم الوطني غير صالح")
    if text_id and mrz_id and text_id != mrz_id:
        return _reject("اختلاف الرقم الوطني بين Text و MRZ")

    # Registration number: optional, but must agree and be well-formed if present.
    if text_reg and mrz_reg and text_reg != mrz_reg:
        return _reject("اختلاف رقم القيد بين Text و MRZ")
    registration = text_reg or mrz_reg
    if registration and not is_valid_registration_number(registration):
        return _reject("رقم القيد غير صالح")

    # Grandfather and family name are never identical on a real card.
    grandfather = front.field_text("gf")
    family_name = front.field_text("last")
    if grandfather and family_name and same_value(grandfather, family_name):
        return _reject("gf و last متطابقان")

    # Date of birth: mandatory, must be a real date and agree with the national ID.
    birth_date = parse_mrz_date(mrz_dob) if is_valid_date_digits(mrz_dob) else None
    if birth_date is None:
        return _reject("تاريخ الميلاد غير صالح أو غير موجود في MRZ")
    if not birth_year_matches_national_id(national_id, mrz_dob):
        return _reject("سنة الميلاد لا تطابق الخانتين الثالثة والرابعة من الرقم الوطني")

    # Sex: at least one side readable; both sides must agree when readable.
    if not text_sex and not mrz_sex:
        return _reject("الجنس غير قابل للتحديد")
    if text_sex and mrz_sex and text_sex != mrz_sex:
        return _reject("الجنس في Text لا يطابق الجنس في MRZ")

    record = IdentityRecord(
        first_name=front.field_text("name"),
        father_name=front.field_text("dad"),
        grandfather_name=grandfather,
        family_name=family_name,
        mother_name=front.field_text("mom"),
        maternal_grandfather=front.field_text("gm"),
        national_id=national_id,
        date_of_birth=birth_date,
        birth_place=back.field_text("city"),
        family_number=back.field_text("nu_f"),
        sex=text_sex or mrz_sex,
        registration_number=registration,
    )
    return ValidationResult(True, "مقبولة", record)
