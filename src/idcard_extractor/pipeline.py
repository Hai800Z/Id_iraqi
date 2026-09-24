"""End-to-end pipeline: image files -> card sides -> matched & validated identities.

    FindCard (segment + warp) -> FindCard second pass
      front: text model (+ orientation) -> PaddleOCR
      back : MRZ1 model (+ orientation) -> PaddleOCR (city, nu_f) + MRZ reader
    -> match front/back -> validate -> IdentityRecord
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Optional

from idcard_extractor.config import Settings
from idcard_extractor.logging_utils import mask
from idcard_extractor.models import CardSide, PipelineResult, ProcessedCard

log = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}

# Rejection reasons are shown to the operators, in Arabic.
REASON_NO_BACK = "لا يوجد ظهر"
REASON_NO_FRONT = "لا يوجد وجه"
REASON_DETECTION_REJECTED = "فشل التحقق الثاني من نوع البطاقة (FindCard)"
REASON_PROCESSING_ERROR = "خطأ أثناء معالجة البطاقة"
REASON_UNSUPPORTED = "نوع مستند غير مدعوم: {}"


def collect_images(inputs: Iterable[str | Path]) -> list[Path]:
    """Expand files and directories into a sorted list of image paths."""
    images: list[Path] = []
    for item in inputs:
        path = Path(item)
        if path.is_dir():
            images.extend(sorted(p for p in path.rglob("*") if p.suffix.lower() in IMAGE_EXTENSIONS))
        elif path.is_file():
            images.append(path)
        else:
            log.warning("Input not found: %s", path)
    return images


def _read_image(path: Path):
    """cv2.imread cannot open non-ASCII paths on Windows; decode from bytes instead."""
    import cv2
    import numpy as np

    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None


class CardPipeline:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._findcard = None
        self._text = None
        self._mrz = None
        self._ocr = None
        self._counter = 0

    # ------------------------------------------------------------------ models

    def load_models(self) -> None:
        from idcard_extractor.detection.yolo_utils import load_yolo
        from idcard_extractor.ocr.text_reader import load_text_recognizer

        s = self.settings
        log.info("Loading FindCard model: %s", s.findcard_weights.name)
        self._findcard = load_yolo(s.findcard_weights)
        log.info("Loading text model: %s", s.text_weights.name)
        self._text = load_yolo(s.text_weights)
        log.info("Loading MRZ model: %s", s.mrz_weights.name)
        self._mrz = load_yolo(s.mrz_weights)
        log.info("Loading PaddleOCR model: %s", s.ocr_model_name)
        self._ocr = load_text_recognizer(s.ocr_model_name, s.ocr_device)

    # ------------------------------------------------------------- per image

    def process_image(self, path: Path) -> list[CardSide]:
        from idcard_extractor.detection.card_detector import detect_cards, second_pass_confidence

        s = self.settings
        image = _read_image(path)
        if image is None:
            log.error("Cannot read image: %s", path)
            return []

        cards = detect_cards(image, self._findcard, s.detect_conf, s.yolo_device)
        if not cards:
            log.warning("No cards detected in %s", path.name)
            return []

        sides: list[CardSide] = []
        for index, detected in enumerate(cards, start=1):
            self._counter += 1
            side = CardSide(
                card_id=f"CARD_{self._counter:03d}",
                source_image=str(path),
                card_index=index,
                card_class=detected.card_class,
                image=detected.image,
                detection_confidence=detected.confidence,
            )
            side.second_pass_confidence = second_pass_confidence(
                detected.image, detected.card_class, self._findcard, s.detect_conf, s.yolo_device
            )
            side.accepted = side.second_pass_confidence >= s.second_pass_conf

            log.info(
                "%s | %s #%d | class=%s conf=%.3f second_pass=%.3f -> %s",
                side.card_id, path.name, index, side.card_class,
                side.detection_confidence, side.second_pass_confidence,
                "accepted" if side.accepted else "REJECTED",
            )

            if side.accepted:
                try:
                    self._process_side(side)
                except Exception as exc:
                    # One bad card must not stop the batch.
                    log.exception("%s: processing failed", side.card_id)
                    side.error = type(exc).__name__
            sides.append(side)

        return sides

    def _process_side(self, side: CardSide) -> None:
        from idcard_extractor.detection.field_detector import detect_fields
        from idcard_extractor.detection.orientation import orient_back, orient_front
        from idcard_extractor.detection.yolo_utils import crop_detections
        from idcard_extractor.ocr.mrz_reader import read_mrz, split_mrz_crop
        from idcard_extractor.ocr.text_reader import recognize_fields

        s = self.settings
        if side.side == "front":
            card, _, detections, orientation = orient_front(
                side.image,
                lambda c: detect_fields(c, self._text, s.detect_conf, s.yolo_device),
                s.orientation_margin,
            )
        elif side.side == "back":
            card, _, detections, orientation = orient_back(
                side.image, lambda c: detect_fields(c, self._mrz, s.detect_conf, s.yolo_device)
            )
        else:
            log.info("%s: document class %r is not processed", side.card_id, side.card_class)
            return

        side.image = card
        side.orientation = orientation
        crops, mrz_crop = split_mrz_crop(crop_detections(card, detections))
        side.fields = recognize_fields(self._ocr, crops)

        if mrz_crop is not None:
            side.mrz = read_mrz(mrz_crop["crop"], s.tesseract_cmd)
            if not side.mrz:
                log.warning("%s: MRZ region found but could not be read", side.card_id)
        elif side.side == "back":
            log.warning("%s: MRZ region not found", side.card_id)

        log.info(
            "%s: %s, orientation=%s, fields=%s",
            side.card_id, side.side, orientation.get("decision"), sorted(side.fields),
        )

    # ------------------------------------------------------------------- run

    def run(
        self,
        images: Iterable[Path],
        progress: Optional[Callable[[int, int, Path], None]] = None,
    ) -> PipelineResult:
        """Process ``images``; ``progress(done, total, path)`` is called after each one."""
        if self._findcard is None:
            self.load_models()

        paths = [Path(p) for p in images]
        result = PipelineResult()
        for done, path in enumerate(paths, start=1):
            try:
                result.sides.extend(self.process_image(path))
            except Exception:
                log.exception("Failed to process image %s", path.name)
            if progress:
                progress(done, len(paths), path)

        accepted, rejected = combine_sides(result.sides)
        result.accepted, result.rejected = accepted, rejected
        return result


def _single_side(side: CardSide, reason: str) -> ProcessedCard:
    return ProcessedCard(
        side.card_id,
        False,
        reason,
        front=side if side.side == "front" else None,
        back=side if side.side == "back" else None,
        other=side if side.side is None else None,
    )


def combine_sides(sides: list[CardSide]) -> tuple[list[ProcessedCard], list[ProcessedCard]]:
    """Match fronts with backs and validate each pair."""
    from idcard_extractor.processing.matching import match_sides
    from idcard_extractor.processing.validation import validate_pair

    matching = match_sides(sides)
    accepted: list[ProcessedCard] = []
    rejected: list[ProcessedCard] = []

    for pair in matching.matched:
        outcome = validate_pair(pair.front, pair.back)
        card = ProcessedCard(
            card_id=pair.front.card_id,
            accepted=outcome.accepted,
            reason=outcome.reason,
            front=pair.front,
            back=pair.back,
            match_method=pair.match_method,
            record=outcome.record,
        )
        (accepted if outcome.accepted else rejected).append(card)
        if outcome.accepted:
            log.info("%s + %s (%s): accepted, national_id=%s",
                     pair.front.card_id, pair.back.card_id, pair.match_method,
                     mask(outcome.record.national_id))
        else:
            log.warning("%s + %s (%s): rejected: %s",
                        pair.front.card_id, pair.back.card_id, pair.match_method, outcome.reason)

    for front in matching.unmatched_fronts:
        rejected.append(_single_side(front, REASON_NO_BACK))
        log.warning("%s: rejected: no matching back side", front.card_id)
    for back in matching.unmatched_backs:
        rejected.append(_single_side(back, REASON_NO_FRONT))
        log.warning("%s: rejected: no matching front side", back.card_id)
    for side in matching.rejected_detection:
        rejected.append(_single_side(side, REASON_DETECTION_REJECTED))
    for side in matching.failed:
        rejected.append(_single_side(side, REASON_PROCESSING_ERROR))
    for side in matching.unsupported:
        rejected.append(_single_side(side, REASON_UNSUPPORTED.format(side.card_class)))

    return accepted, rejected
