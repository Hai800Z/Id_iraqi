"""Pair front and back sides that belong to the same card.

Every input image is processed independently, so a front and its back may come
from different files. Sides are paired by national ID first, then by
registration number. Unmatched sides are never combined by guesswork.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from idcard_extractor.models import CardSide, MatchedCard
from idcard_extractor.processing.normalization import (
    clean_text,
    normalize_national_id,
    normalize_registration_number,
)
from idcard_extractor.processing.rules import is_valid_national_id


def mrz_value(side: CardSide, key: str) -> str:
    data = side.mrz or {}
    value = data.get(key, "")
    if isinstance(value, str):
        value = value.replace("<", "")
    return clean_text(value)


def front_national_id(side: CardSide) -> str:
    return normalize_national_id(side.field_text("id"))


def front_registration(side: CardSide) -> str:
    return normalize_registration_number(side.field_text("id2"))


def back_national_id(side: CardSide) -> str:
    return normalize_national_id(mrz_value(side, "optional1"))


def back_registration(side: CardSide) -> str:
    return normalize_registration_number(mrz_value(side, "number"))


@dataclass
class MatchResult:
    matched: list[MatchedCard] = field(default_factory=list)
    unmatched_fronts: list[CardSide] = field(default_factory=list)
    unmatched_backs: list[CardSide] = field(default_factory=list)
    # FindCard second pass below threshold.
    rejected_detection: list[CardSide] = field(default_factory=list)
    # Field detection / OCR raised an exception.
    failed: list[CardSide] = field(default_factory=list)
    # Other document classes detected by FindCard (e.g. driving licence).
    unsupported: list[CardSide] = field(default_factory=list)


def match_sides(sides: list[CardSide]) -> MatchResult:
    result = MatchResult()
    fronts: list[CardSide] = []
    backs: list[CardSide] = []

    for side in sides:
        if not side.accepted:
            result.rejected_detection.append(side)
        elif side.error:
            result.failed.append(side)
        elif side.side == "front":
            fronts.append(side)
        elif side.side == "back":
            backs.append(side)
        else:
            result.unsupported.append(side)

    used_fronts: set[int] = set()
    used_backs: set[int] = set()

    def pair(front_key, back_key, is_usable, method: str) -> None:
        for fi, front in enumerate(fronts):
            if fi in used_fronts:
                continue
            fkey = front_key(front)
            if not is_usable(fkey):
                continue
            for bi, back in enumerate(backs):
                if bi in used_backs:
                    continue
                bkey = back_key(back)
                if is_usable(bkey) and fkey == bkey:
                    result.matched.append(MatchedCard(front, back, method))
                    used_fronts.add(fi)
                    used_backs.add(bi)
                    break

    pair(front_national_id, back_national_id, is_valid_national_id, "national_id")
    pair(front_registration, back_registration, bool, "registration_number")

    result.unmatched_fronts = [f for i, f in enumerate(fronts) if i not in used_fronts]
    result.unmatched_backs = [b for i, b in enumerate(backs) if i not in used_backs]
    return result
