"""Logging setup and helpers that keep personal data out of log files."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

_mask_enabled = True

_LONG_DIGITS = re.compile(r"\d{5,}")
_ARABIC_TEXT = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]+")


def configure_logging(
    level: str = "INFO",
    log_file: Optional[Path] = None,
    mask_pii: bool = True,
) -> None:
    global _mask_enabled
    _mask_enabled = mask_pii

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    # Third-party libraries are very chatty at INFO.
    for noisy in ("ultralytics", "paddle", "paddlex", "ppocr", "PIL", "sqlalchemy.engine",
                  "httpx", "httpx2", "httpcore", "huggingface_hub", "modelscope", "urllib3", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def mask(value: object, keep_start: int = 2, keep_end: int = 2) -> str:
    """Mask the middle of an identifier, e.g. ``127901234567`` -> ``12********67``."""
    text = "" if value is None else str(value)
    if not _mask_enabled or not text:
        return text
    if len(text) <= keep_start + keep_end:
        return "*" * len(text)
    hidden = len(text) - keep_start - keep_end
    return text[:keep_start] + "*" * hidden + text[len(text) - keep_end:]


def mask_text(value: object) -> str:
    """Mask a free-text personal field (names): keep only the first character."""
    text = "" if value is None else str(value)
    if not _mask_enabled or not text:
        return text
    return text[0] + "***"


def redact(message: object) -> str:
    """Hide identifiers (long digit runs) and Arabic text (names) inside a free-form
    message, e.g. a database error that echoes the offending value."""
    text = "" if message is None else str(message)
    if not _mask_enabled:
        return text
    text = _LONG_DIGITS.sub(lambda m: mask(m.group()), text)
    return _ARABIC_TEXT.sub("***", text)
