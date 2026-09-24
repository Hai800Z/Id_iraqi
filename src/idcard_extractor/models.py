"""Data structures shared by every stage of the pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import date
from typing import Any, Optional

from idcard_extractor.processing.normalization import clean_text

# Class names produced by the FindCard model.
FRONT_CLASS = "iraq id card"
BACK_CLASS = "back"


@dataclass
class OcrField:
    """A single OCR reading of one detected field."""

    text: str
    confidence: float


@dataclass
class CardSide:
    """One physical side (front or back) of a card found in an input image."""

    card_id: str
    source_image: str
    card_index: int
    card_class: str
    image: Any
    detection_confidence: float
    second_pass_confidence: float = 0.0
    accepted: bool = False
    orientation: dict = field(default_factory=dict)
    # OCR readings of the non-MRZ fields, keyed by the detector label
    # (front: name, dad, gf, last, mom, gm, gn, id, id2; back: city, nu_f).
    fields: dict[str, OcrField] = field(default_factory=dict)
    # Parsed MRZ data (back side only).
    mrz: Optional[dict[str, Any]] = None
    # Set when field detection / OCR raised an exception for this side.
    error: Optional[str] = None

    @property
    def side(self) -> Optional[str]:
        card_class = self.card_class.strip().lower()
        if card_class == FRONT_CLASS:
            return "front"
        if card_class == BACK_CLASS:
            return "back"
        return None

    def field_text(self, name: str) -> str:
        """OCR text of a field with whitespace normalized ('' when not detected)."""
        item = self.fields.get(name)
        return clean_text(item.text) if item else ""


@dataclass
class IdentityRecord:
    """The final, validated identity built from a matched front/back pair.

    Field names are the canonical keys used by the database mapping file.
    """

    first_name: str = ""
    father_name: str = ""
    grandfather_name: str = ""
    family_name: str = ""
    mother_name: str = ""
    maternal_grandfather: str = ""
    national_id: str = ""
    date_of_birth: Optional[date] = None
    birth_place: str = ""
    family_number: str = ""
    sex: str = ""
    registration_number: str = ""

    @classmethod
    def field_names(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Arabic column headers used by the Excel export, in display order.
ARABIC_LABELS: dict[str, str] = {
    "first_name": "الاسم",
    "father_name": "الاب",
    "grandfather_name": "الجد",
    "family_name": "اللقب",
    "mother_name": "الام",
    "maternal_grandfather": "اب الام",
    "national_id": "الرقم الوطني",
    "date_of_birth": "تاريخ الميلاد",
    "birth_place": "مكان الولادة",
    "family_number": "الرقم العائلي",
    "sex": "الجنس",
    "registration_number": "رقم القيد",
}


@dataclass
class MatchedCard:
    front: CardSide
    back: CardSide
    match_method: str


@dataclass
class ProcessedCard:
    """Outcome of validating one card (or an unmatched side)."""

    card_id: str
    accepted: bool
    reason: str
    front: Optional[CardSide] = None
    back: Optional[CardSide] = None
    match_method: Optional[str] = None
    record: Optional[IdentityRecord] = None
    # A side whose class is neither front nor back (unsupported document type).
    other: Optional[CardSide] = None

    @property
    def sides(self) -> list[CardSide]:
        return [s for s in (self.front, self.back, self.other) if s is not None]


@dataclass
class PipelineResult:
    sides: list[CardSide] = field(default_factory=list)
    accepted: list[ProcessedCard] = field(default_factory=list)
    rejected: list[ProcessedCard] = field(default_factory=list)

    @property
    def records(self) -> list[IdentityRecord]:
        return [card.record for card in self.accepted if card.record]
