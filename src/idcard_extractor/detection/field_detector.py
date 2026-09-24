"""Field detection on a warped card (text model for the front, MRZ1 model for the back)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from idcard_extractor.detection.yolo_utils import get_detections, run_detector

if TYPE_CHECKING:
    import numpy as np


def detect_fields(card: np.ndarray, model, conf: float, device: Optional[str] = None):
    result = run_detector(model, card, conf=conf, device=device)
    return result, get_detections(result, model, card.shape)
