import pytest

from idcard_extractor.detection.orientation import orient_back, orient_front, orientation_score


def _det(label, nx, ny, confidence=0.9):
    return {"label": label, "confidence": confidence, "nx": nx, "ny": ny}


# Field centers of an upright front side (normalized coordinates).
UPRIGHT = [
    _det("id2", 0.15, 0.75),
    _det("id", 0.55, 0.40),
    _det("name", 0.70, 0.20),
    _det("dad", 0.70, 0.30),
    _det("gf", 0.70, 0.40),
]


def _rotate_180(detections):
    return [{**d, "nx": 1 - d["nx"], "ny": 1 - d["ny"]} for d in detections]


def test_upright_card_is_normal():
    result = orientation_score(UPRIGHT)
    assert result["decision"] == "NORMAL"
    assert result["confidence"] == 1.0


def test_upside_down_card_is_rotate_180():
    assert orientation_score(_rotate_180(UPRIGHT))["decision"] == "ROTATE_180"


def test_no_detections_is_uncertain():
    assert orientation_score([])["decision"] == "UNCERTAIN"


def test_the_most_confident_detection_of_a_label_is_used():
    wrong = _det("id2", 0.85, 0.25, confidence=0.3)
    assert orientation_score([*UPRIGHT, wrong])["decision"] == "NORMAL"


def test_confident_front_is_not_rotated():
    card = object()
    out, _, detections, orientation = orient_front(card, lambda c: ("result", UPRIGHT), margin=0.10)
    assert out is card and detections == UPRIGHT and orientation["decision"] == "NORMAL"


def test_back_with_detections_is_not_rotated():
    card = object()
    out, _, _, orientation = orient_back(card, lambda c: ("result", [_det("MRZ", 0.5, 0.8)]))
    assert out is card and orientation == {"decision": "NORMAL", "angle": 0}


def test_crop_keeps_the_most_confident_box():
    np = pytest.importorskip("numpy")
    from idcard_extractor.detection.yolo_utils import crop_detections

    image = np.zeros((100, 200, 3), dtype=np.uint8)
    strong = {"label": "id", "confidence": 0.9, "x1": 10, "y1": 10, "x2": 60, "y2": 30}
    weak = {"label": "id", "confidence": 0.4, "x1": 100, "y1": 50, "x2": 150, "y2": 90}
    # YOLO lists boxes by decreasing confidence: the weak duplicate comes last.
    for detections in ([strong, weak], [weak, strong]):
        crops = crop_detections(image, detections)
        assert crops["id"]["confidence"] == 0.9
        assert crops["id"]["bbox"] == (10, 10, 60, 30)


def test_rotation_helper():
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    from idcard_extractor.detection.orientation import rotate_card

    card = np.arange(6, dtype=np.uint8).reshape(2, 3)
    assert rotate_card(card, 90).shape == (3, 2)
    assert (rotate_card(card, 180) == card[::-1, ::-1]).all()
    with pytest.raises(ValueError):
        rotate_card(card, 45)
