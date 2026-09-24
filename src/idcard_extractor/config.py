"""Runtime settings, read from environment variables (and an optional .env file).

Relative paths are resolved against the directory of the loaded .env file, or the
current working directory when no .env file is found.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import find_dotenv, load_dotenv


class ConfigError(ValueError):
    pass


def _env_str(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_float(name: str, default: float) -> float:
    value = _env_str(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    value = _env_str(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def load_env_file(env_file: Optional[str | Path] = None) -> Path:
    """Load ``env_file`` (or the nearest ``.env`` above the working directory) into
    ``os.environ`` without overriding real environment variables.

    Returns the base directory for relative paths.
    """
    if env_file:
        path = Path(env_file).expanduser()
        if not path.is_file():
            raise ConfigError(f"env file not found: {path}")
        load_dotenv(path, override=False)
        return path.resolve().parent

    found = find_dotenv(usecwd=True)
    if found:
        load_dotenv(found, override=False)
        return Path(found).resolve().parent
    return Path.cwd()


@dataclass(frozen=True)
class Settings:
    base_dir: Path

    weights_dir: Path
    findcard_weights: Path
    text_weights: Path
    mrz_weights: Path

    yolo_device: Optional[str]
    ocr_model_name: str
    ocr_device: str
    tesseract_cmd: Optional[str]

    # Detection thresholds (defaults reproduce the original Colab pipeline).
    detect_conf: float
    second_pass_conf: float
    orientation_margin: float

    output_dir: Path
    log_level: str
    log_file: Optional[Path]
    mask_pii: bool

    db_mapping_file: Path
    db_failed_records_file: Optional[Path]

    def resolve(self, path: str | Path) -> Path:
        p = Path(path).expanduser()
        return p if p.is_absolute() else self.base_dir / p


def load_settings(env_file: Optional[str | Path] = None) -> Settings:
    base_dir = load_env_file(env_file)

    def resolve(path: str) -> Path:
        p = Path(path).expanduser()
        return p if p.is_absolute() else base_dir / p

    def optional_path(name: str, default: Optional[str] = None) -> Optional[Path]:
        value = _env_str(name, default)
        return resolve(value) if value else None

    weights_dir = resolve(_env_str("WEIGHTS_DIR", "models/weights"))

    def weight(name: str, default_file: str) -> Path:
        value = _env_str(name)
        return resolve(value) if value else weights_dir / default_file

    return Settings(
        base_dir=base_dir,
        weights_dir=weights_dir,
        findcard_weights=weight("FINDCARD_WEIGHTS", "findCard.pt"),
        text_weights=weight("TEXT_WEIGHTS", "text.pt"),
        mrz_weights=weight("MRZ_WEIGHTS", "MRZ1.pt"),
        yolo_device=_env_str("YOLO_DEVICE"),
        ocr_model_name=_env_str("OCR_MODEL_NAME", "arabic_PP-OCRv5_mobile_rec"),
        ocr_device=_env_str("OCR_DEVICE", "cpu"),
        tesseract_cmd=_env_str("TESSERACT_CMD"),
        detect_conf=_env_float("DETECT_CONF", 0.25),
        second_pass_conf=_env_float("SECOND_PASS_CONF", 0.50),
        orientation_margin=_env_float("ORIENTATION_MARGIN", 0.10),
        output_dir=resolve(_env_str("OUTPUT_DIR", "output")),
        log_level=_env_str("LOG_LEVEL", "INFO").upper(),
        log_file=optional_path("LOG_FILE"),
        mask_pii=_env_bool("MASK_PII_IN_LOGS", True),
        db_mapping_file=resolve(_env_str("DB_MAPPING_FILE", "config/db_mapping.yaml")),
        # Set DB_FAILED_RECORDS_FILE to "none" to disable the file.
        db_failed_records_file=(
            None if (_env_str("DB_FAILED_RECORDS_FILE") or "").lower() == "none"
            else optional_path("DB_FAILED_RECORDS_FILE", "output/db_failed_records.jsonl")
        ),
    )
