"""MRZ reading for the back of the card.

Primary reader: ``mrzmini`` on the MRZ crop.
Fallback: Tesseract OCR with extra preprocessing + the built-in TD1 parser below.

Both need the external ``tesseract`` binary (on PATH, or set ``TESSERACT_CMD``).
Both return a dict with (at least) the keys used downstream:
``number``, ``date_of_birth``, ``sex``, ``optional1``.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    import numpy as np

log = logging.getLogger(__name__)

_TESSERACT_MISSING = (
    "Tesseract OCR binary not found; install it or set TESSERACT_CMD (see README)"
)

MRZ_LABELS = {"mrz", "mrz1", "mrz_1", "mrz-1"}
TD1_LINE_LENGTH = 30


def is_mrz_field(label: str) -> bool:
    return str(label).strip().lower() in MRZ_LABELS


def split_mrz_crop(field_crops: dict[str, dict]) -> tuple[dict[str, dict], Optional[dict]]:
    """Separate the MRZ crop from the ordinary text-field crops."""
    normal: dict[str, dict] = {}
    mrz = None
    for label, data in field_crops.items():
        if is_mrz_field(label):
            mrz = {"field": label, **data}
        else:
            normal[label] = data
    return normal, mrz


# ----------------------------------------------------------------------------
# TD1 parsing (ICAO 9303, 3 lines x 30 characters)
# ----------------------------------------------------------------------------

def _char_value(char: str) -> int:
    if char.isdigit():
        return int(char)
    if "A" <= char <= "Z":
        return ord(char) - ord("A") + 10
    return 0  # '<' filler


def check_digit(data: str) -> str:
    weights = (7, 3, 1)
    total = sum(_char_value(c) * weights[i % 3] for i, c in enumerate(data))
    return str(total % 10)


def parse_td1(lines: list[str]) -> Optional[dict[str, Any]]:
    """Parse a 3-line TD1 MRZ. Returns None if the layout is not TD1."""
    if len(lines) < 3:
        return None
    l1, l2, l3 = (line.strip().upper()[:TD1_LINE_LENGTH].ljust(TD1_LINE_LENGTH, "<") for line in lines[:3])

    number = l1[5:14]
    dob = l2[0:6]
    expiry = l2[8:14]
    names = l3.split("<<", 1)

    return {
        "mrz_type": "TD1",
        "type": l1[0:2].replace("<", ""),
        "country": l1[2:5].replace("<", ""),
        "number": number.replace("<", ""),
        "optional1": l1[15:30].replace("<", ""),
        "date_of_birth": dob,
        "sex": l2[7].replace("<", ""),
        "expiration_date": expiry,
        "nationality": l2[15:18].replace("<", ""),
        "optional2": l2[18:29].replace("<", ""),
        "surname": names[0].replace("<", " ").strip(),
        "names": names[1].replace("<", " ").strip() if len(names) > 1 else "",
        "valid_number": check_digit(number) == l1[14],
        "valid_date_of_birth": check_digit(dob) == l2[6],
        "valid_expiration_date": check_digit(expiry) == l2[14],
        "raw_mrz": "\n".join((l1, l2, l3)),
    }


# ----------------------------------------------------------------------------
# Readers
# ----------------------------------------------------------------------------

def _read_with_mrzmini(image: np.ndarray) -> Optional[dict]:
    import cv2
    from mrzmini import read_mrz

    # Hand the crop over in memory: no copy of the ID card is written to disk.
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        return None
    try:
        result = read_mrz(encoded.tobytes())
        return result.to_dict() if result else None
    except FileNotFoundError:
        log.warning("mrzmini failed: %s", _TESSERACT_MISSING)
        return None
    except Exception as exc:  # third-party reader: never let it crash the pipeline
        log.warning("mrzmini failed: %s", exc)
        return None


def _preprocess(image: np.ndarray) -> np.ndarray:
    import cv2

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    enhanced = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    _, thresh = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return thresh


def _read_with_tesseract(image: np.ndarray, tesseract_cmd: Optional[str] = None) -> Optional[dict]:
    try:
        import pytesseract
    except ImportError:
        log.warning("pytesseract is not installed; skipping Tesseract MRZ fallback")
        return None

    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    config = (
        "--oem 3 --psm 6 "
        "-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<"
    )
    try:
        text = pytesseract.image_to_string(_preprocess(image), config=config)
    except pytesseract.TesseractNotFoundError:
        log.warning("Tesseract fallback failed: %s", _TESSERACT_MISSING)
        return None
    except Exception as exc:
        log.warning("Tesseract fallback failed: %s", exc)
        return None

    lines = [
        line.strip() for line in text.splitlines()
        if re.fullmatch(r"[A-Z0-9<]{30,}", line.strip())
    ]
    if len(lines) < 3:
        return None
    return parse_td1(lines)


def read_mrz(image: Optional[np.ndarray], tesseract_cmd: Optional[str] = None) -> Optional[dict]:
    if image is None or image.size == 0:
        return None
    return _read_with_mrzmini(image) or _read_with_tesseract(image, tesseract_cmd)
