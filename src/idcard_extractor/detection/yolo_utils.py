"""Thin helpers around Ultralytics YOLO models."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    import numpy as np


def load_yolo(weights: Path):
    from ultralytics import YOLO

    if not Path(weights).is_file():
        raise FileNotFoundError(
            f"Model weights not found: {weights}. "
            "Download them into WEIGHTS_DIR with scripts/download_weights.py (see README)."
        )
    return YOLO(str(weights))


def run_detector(model, image: np.ndarray, conf: float, device: Optional[str] = None):
    # Without a device, Ultralytics picks one itself (a GPU when available).
    options = {"device": device} if device else {}
    return model.predict(source=image, conf=conf, verbose=False, **options)[0]


def get_detections(result, model, image_shape) -> list[dict]:
    """Convert a YOLO result into plain dicts with absolute and normalized centers."""
    detections: list[dict] = []
    h, w = image_shape[:2]

    if result.boxes is None:
        return detections

    for box in result.boxes:
        cls_id = int(box.cls[0])
        x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
        cx = (x1 + x2) / 2
        cy = (y1 + y2) / 2

        detections.append({
            "label": str(model.names[cls_id]),
            "confidence": float(box.conf[0]),
            "cx": float(cx),
            "cy": float(cy),
            "nx": float(cx / w),
            "ny": float(cy / h),
            "x1": float(x1),
            "y1": float(y1),
            "x2": float(x2),
            "y2": float(y2),
        })

    return detections


def crop_detections(image: np.ndarray, detections: list[dict]) -> dict[str, dict]:
    """Crop every detection out of ``image``, keyed by label.

    When a label is detected more than once, the most confident box is kept.
    """
    crops: dict[str, dict] = {}
    h, w = image.shape[:2]

    for detection in detections:
        label = detection["label"]
        if label in crops and crops[label]["confidence"] >= detection["confidence"]:
            continue

        x1 = max(0, int(detection["x1"]))
        y1 = max(0, int(detection["y1"]))
        x2 = min(w, int(detection["x2"]))
        y2 = min(h, int(detection["y2"]))

        if x2 <= x1 or y2 <= y1:
            continue

        crop = image[y1:y2, x1:x2].copy()
        if crop.size == 0:
            continue

        crops[label] = {
            "crop": crop,
            "confidence": detection["confidence"],
            "bbox": (x1, y1, x2, y2),
        }

    return crops
