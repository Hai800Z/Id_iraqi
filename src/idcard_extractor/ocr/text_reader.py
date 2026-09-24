"""Arabic text recognition of cropped fields with PaddleOCR."""

from __future__ import annotations

import logging

from idcard_extractor.models import OcrField

log = logging.getLogger(__name__)


def load_text_recognizer(model_name: str, device: str):
    from paddleocr import TextRecognition

    return TextRecognition(model_name=model_name, device=device)


def recognize_crop(recognizer, image) -> OcrField:
    output = recognizer.predict(input=image, batch_size=1)
    text, score = "", 0.0
    if output:
        data = output[0].json or {}
        res = data.get("res", {})
        text = res.get("rec_text", "") or ""
        score = float(res.get("rec_score", 0.0) or 0.0)
    return OcrField(text=text, confidence=score)


def recognize_fields(recognizer, field_crops: dict[str, dict]) -> dict[str, OcrField]:
    """OCR every crop; a failure on one field is logged and does not stop the others."""
    results: dict[str, OcrField] = {}
    for label, data in field_crops.items():
        image = data.get("crop")
        if image is None or image.size == 0:
            continue
        try:
            results[label] = recognize_crop(recognizer, image)
        except Exception as exc:
            log.warning("PaddleOCR failed for field %r: %s", label, exc)
    return results
