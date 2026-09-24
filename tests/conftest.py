"""Shared fixtures. All identities used in the tests are synthetic."""

from __future__ import annotations

import os

import pytest

from idcard_extractor import logging_utils
from idcard_extractor.models import CardSide, OcrField

SETTINGS_KEYS = {
    "WEIGHTS_DIR", "FINDCARD_WEIGHTS", "TEXT_WEIGHTS", "MRZ_WEIGHTS", "YOLO_DEVICE",
    "OCR_MODEL_NAME", "OCR_DEVICE", "TESSERACT_CMD", "DETECT_CONF", "SECOND_PASS_CONF",
    "ORIENTATION_MARGIN", "OUTPUT_DIR", "LOG_LEVEL", "LOG_FILE", "MASK_PII_IN_LOGS",
    "DATABASE_URL",
}

FRONT_FIELDS = {
    "name": "أحمد",
    "dad": "علي",
    "gf": "حسن",
    "last": "الكعبي",
    "mom": "فاطمة",
    "gm": "محمد",
    "gn": "ذكر",
    "id": "127901234567",
    "id2": "A12345678",
}
BACK_MRZ = {
    "number": "A12345678<",
    "date_of_birth": "790105",
    "sex": "M",
    "optional1": "127901234567<<<",
}
BACK_FIELDS = {"city": "بغداد", "nu_f": "12345"}


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    """Run every test in an empty folder, without the developer's settings."""
    saved = dict(os.environ)
    for key in list(os.environ):
        if key in SETTINGS_KEYS or key.startswith("DB_"):
            del os.environ[key]
    monkeypatch.chdir(tmp_path)
    yield
    os.environ.clear()
    os.environ.update(saved)
    logging_utils._mask_enabled = True


def _side(card_id: str, card_class: str, fields: dict, mrz=None, accepted=True, source=None) -> CardSide:
    side = CardSide(
        card_id=card_id,
        source_image=source or f"/photos/{card_id}.jpg",
        card_index=1,
        card_class=card_class,
        image=None,
        detection_confidence=0.95,
        second_pass_confidence=0.9 if accepted else 0.2,
        accepted=accepted,
    )
    side.fields = {label: OcrField(text, 0.99) for label, text in fields.items()}
    side.mrz = mrz
    return side


@pytest.fixture
def make_front():
    def factory(card_id="CARD_001", accepted=True, **overrides):
        return _side(card_id, "iraq id card", {**FRONT_FIELDS, **overrides}, accepted=accepted)
    return factory


@pytest.fixture
def make_back():
    def factory(card_id="CARD_002", mrz=None, accepted=True, **overrides):
        data = dict(BACK_MRZ) if mrz is None else mrz
        return _side(card_id, "back", {**BACK_FIELDS, **overrides}, mrz=data, accepted=accepted)
    return factory
