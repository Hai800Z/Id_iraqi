"""Card localisation: segment cards in a photo, warp them flat, re-verify them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np

from idcard_extractor.detection.yolo_utils import run_detector


@dataclass
class DetectedCard:
    image: np.ndarray
    card_class: str
    confidence: float
    corners: np.ndarray


def order_points(pts) -> np.ndarray:
    """Order four corners as TL, TR, BR, BL regardless of card rotation."""
    pts = np.asarray(pts, dtype=np.float32)
    x_sorted = pts[np.argsort(pts[:, 0]), :]

    left_most = x_sorted[:2, :]
    left_most = left_most[np.argsort(left_most[:, 1]), :]
    tl, bl = left_most

    right_most = x_sorted[2:, :]
    right_most = right_most[np.argsort(right_most[:, 1]), :]
    tr, br = right_most

    return np.array([tl, tr, br, bl], dtype=np.float32)


def extract_corners(polygon: np.ndarray) -> np.ndarray:
    contour = polygon.astype(np.float32).reshape((-1, 1, 2))
    peri = cv2.arcLength(contour, True)
    corners = None

    for eps in (0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05):
        approx = cv2.approxPolyDP(contour, eps * peri, True)
        if len(approx) == 4:
            corners = approx.reshape(4, 2)
            break

    if corners is None:
        corners = cv2.boxPoints(cv2.minAreaRect(contour))

    return order_points(corners)


def find_card_polygons(result, model) -> list[tuple[np.ndarray, str, float]]:
    cards = []
    if result.masks is None or result.boxes is None:
        return cards

    for i, polygon in enumerate(result.masks.xy):
        cls_id = int(result.boxes.cls[i])
        cards.append((
            np.asarray(polygon),
            str(model.names[cls_id]),
            float(result.boxes.conf[i]),
        ))

    return cards


def align_and_crop_cards(image: np.ndarray, polygons) -> list[DetectedCard]:
    """Perspective-warp each card polygon into a flat, landscape image."""
    cards: list[DetectedCard] = []

    for polygon, class_name, card_conf in polygons:
        corners = extract_corners(polygon)
        tl, tr, br, bl = corners

        width_a = np.linalg.norm(br - bl)
        width_b = np.linalg.norm(tr - tl)
        height_a = np.linalg.norm(tr - br)
        height_b = np.linalg.norm(tl - bl)

        side_w = int(max(width_a, width_b))
        side_h = int(max(height_a, height_b))
        if side_w < 20 or side_h < 20:
            continue

        # Always produce a landscape card.
        max_width = max(side_w, side_h)
        max_height = min(side_w, side_h)

        if max(height_a, height_b) > max(width_a, width_b):
            # Card stands vertically: rotate the projection target.
            dst = np.array([
                [0, max_height - 1],
                [0, 0],
                [max_width - 1, 0],
                [max_width - 1, max_height - 1],
            ], dtype=np.float32)
        else:
            dst = np.array([
                [0, 0],
                [max_width - 1, 0],
                [max_width - 1, max_height - 1],
                [0, max_height - 1],
            ], dtype=np.float32)

        matrix = cv2.getPerspectiveTransform(corners, dst)
        warped = cv2.warpPerspective(image, matrix, (max_width, max_height))

        cards.append(DetectedCard(warped, class_name, card_conf, corners))

    return cards


def detect_cards(image: np.ndarray, model, conf: float, device: Optional[str] = None) -> list[DetectedCard]:
    result = run_detector(model, image, conf=conf, device=device)
    polygons = find_card_polygons(result, model)
    return align_and_crop_cards(image, polygons) if polygons else []


def second_pass_confidence(
    card_image: np.ndarray, expected_class: str, model, conf: float, device: Optional[str] = None
) -> float:
    """Re-run FindCard on the warped card; return the best score for its class."""
    result = run_detector(model, card_image, conf=conf, device=device)
    best = 0.0
    expected = str(expected_class).strip().lower()

    if result.boxes is not None:
        for box in result.boxes:
            label = str(model.names[int(box.cls[0])]).strip().lower()
            if label == expected:
                best = max(best, float(box.conf[0]))

    return best
