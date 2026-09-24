"""Orientation handling for warped cards (0 / 90 / 180 / 270 degrees).

Front side: the text model's field layout is scored geometrically.
Back side: the card is flipped 180 degrees when no MRZ fields are found.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from itertools import pairwise
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np

log = logging.getLogger(__name__)

# (card) -> (yolo_result, detections)
DetectFn = Callable[[Any], tuple]

# Minimum (best - second) / total score for orientation_score to commit.
_DECISION_CONFIDENCE = 0.15

# Reading order of the front-side text fields, top to bottom.
_FRONT_FIELD_ORDER = ("name", "dad", "gf", "last", "mom", "gm", "gn")


def rotate_card(card: np.ndarray, angle: int) -> np.ndarray:
    import cv2

    if angle == 0:
        return card
    if angle == 90:
        return cv2.rotate(card, cv2.ROTATE_90_CLOCKWISE)
    if angle == 180:
        return cv2.rotate(card, cv2.ROTATE_180)
    if angle == 270:
        return cv2.rotate(card, cv2.ROTATE_90_COUNTERCLOCKWISE)
    raise ValueError(f"Unsupported angle: {angle}")


def orientation_score(detections: list[dict]) -> dict:
    """Score the four orientations from the positions of the detected front fields."""
    points: dict[str, dict] = {}
    for d in detections:
        if d["label"] not in points or d["confidence"] > points[d["label"]]["confidence"]:
            points[d["label"]] = d

    scores = {"NORMAL": 0.0, "ROTATE_90": 0.0, "ROTATE_180": 0.0, "ROTATE_270": 0.0}
    evidence = {key: 0 for key in scores}

    def vote(direction: str, weight: float) -> None:
        scores[direction] += weight
        evidence[direction] += 1

    # 1. Relative position of id2 -> id (strongest cue, also resolves 90/270).
    if "id2" in points and "id" in points:
        dx = points["id"]["nx"] - points["id2"]["nx"]
        dy = points["id"]["ny"] - points["id2"]["ny"]
        if abs(dx) > abs(dy):
            vote("NORMAL" if dx > 0 else "ROTATE_180", 5.0)
        elif abs(dy) > abs(dx):
            vote("ROTATE_90" if dy > 0 else "ROTATE_270", 5.0)

    # 2. Absolute position of id2.
    if "id2" in points:
        x, y = points["id2"]["nx"], points["id2"]["ny"]
        if x < 0.40 and y > 0.60:
            vote("NORMAL", 3.0)
        elif x > 0.60 and y < 0.40:
            vote("ROTATE_180", 3.0)

    # 3. Absolute position of id.
    if "id" in points:
        x, y = points["id"]["nx"], points["id"]["ny"]
        if 0.30 < x < 0.70 and y < 0.55:
            vote("NORMAL", 2.0)
        elif 0.30 < x < 0.70 and y > 0.55:
            vote("ROTATE_180", 2.0)

    # 4. Vertical reading order of the name fields.
    available = [points[label]["ny"] for label in _FRONT_FIELD_ORDER if label in points]
    for upper, lower in pairwise(available):
        if upper < lower:
            vote("NORMAL", 0.75)
        elif upper > lower:
            vote("ROTATE_180", 0.75)

    # 5. The name sits in the upper part of an upright card.
    if "name" in points:
        vote("NORMAL" if points["name"]["ny"] < 0.60 else "ROTATE_180", 0.5)

    total = sum(scores.values())
    result = {
        "normal": scores["NORMAL"],
        "rotate_90": scores["ROTATE_90"],
        "rotated": scores["ROTATE_180"],
        "rotate_270": scores["ROTATE_270"],
        "evidence": evidence,
    }

    if total == 0:
        return {**result, "decision": "UNCERTAIN", "confidence": 0.0}

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best_direction, best_score = ranked[0]
    confidence = (best_score - ranked[1][1]) / total
    decision = best_direction if confidence >= _DECISION_CONFIDENCE else "UNCERTAIN"

    return {**result, "decision": decision, "confidence": confidence}


def _score_for_angle(orientation: dict, angle: int) -> float:
    key = {0: "normal", 90: "rotate_90", 180: "rotated", 270: "rotate_270"}[angle]
    return float(orientation.get(key, 0.0))


def orient_front(card: np.ndarray, detect: DetectFn, margin: float):
    """Return ``(card, result, detections, orientation)`` for the best front orientation.

    A confident NORMAL / ROTATE_180 decision on the original image is trusted.
    Otherwise all four rotations are tried, and the card is only rotated when the
    winner beats the runner-up by at least ``margin`` (relative).
    """
    result, detections = detect(card)
    orientation = orientation_score(detections)
    decision = orientation["decision"]

    if decision == "NORMAL":
        return card, result, detections, orientation

    if decision == "ROTATE_180":
        rotated = rotate_card(card, 180)
        result, detections = detect(rotated)
        return rotated, result, detections, orientation_score(detections)

    candidates = []
    for angle in (0, 90, 180, 270):
        candidate = rotate_card(card, angle)
        res, dets = detect(candidate)
        orient = orientation_score(dets)
        # Orientation score dominates; detection count is only a tie-breaker.
        score = _score_for_angle(orient, angle) + len(dets) * 0.05
        candidates.append((score, angle, candidate, res, dets, orient))
        log.debug("front orientation %d°: detections=%d score=%.3f", angle, len(dets), score)

    candidates.sort(key=lambda c: c[0], reverse=True)
    best, second = candidates[0], candidates[1]
    relative_margin = (best[0] - second[0]) / max(abs(best[0]), 1e-6)

    if relative_margin < margin:
        log.debug("front orientation uncertain (margin %.3f); keeping original", relative_margin)
        selected = next(c for c in candidates if c[1] == 0)
    else:
        selected = best

    _, angle, card_out, res, dets, orient = selected
    log.debug("front orientation selected: %d°", angle)
    return card_out, res, dets, orient


def orient_back(card: np.ndarray, detect: DetectFn):
    """Return ``(card, result, detections, orientation)``; flip 180° if nothing is detected."""
    result, detections = detect(card)
    orientation = {"decision": "NORMAL", "angle": 0}

    if not detections:
        rotated = rotate_card(card, 180)
        res_180, det_180 = detect(rotated)
        if len(det_180) > len(detections):
            log.debug("back card rotated 180° to detect MRZ")
            return rotated, res_180, det_180, {"decision": "ROTATE_180", "angle": 180}

    return card, result, detections, orientation
