"""Open the database and the mapping from the settings, with readable errors.

Shared by the command line and the desktop interface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from sqlalchemy.exc import SQLAlchemyError

from idcard_extractor.config import Settings
from idcard_extractor.db.connection import DatabaseSettings, create_db_engine
from idcard_extractor.db.mapping import load_mapping
from idcard_extractor.db.writer import DatabaseWriter, describe_db_error


class DatabaseSetupError(Exception):
    """The database stage cannot run; the message says why."""


def open_database(settings: Settings, mapping_path: Optional[str | Path] = None):
    """Return ``(engine, mapping)``."""
    path = Path(mapping_path) if mapping_path else settings.db_mapping_file
    try:
        mapping = load_mapping(path)
        engine = create_db_engine(DatabaseSettings.from_env(), settings.base_dir, settings.mask_pii)
    except ValueError as exc:  # MappingError, DatabaseConfigError
        raise DatabaseSetupError(str(exc)) from exc
    except SQLAlchemyError as exc:
        raise DatabaseSetupError(f"Cannot create the database engine: {type(exc).__name__}") from exc
    return engine, mapping


def prepare_writer(
    settings: Settings,
    mapping_path: Optional[str | Path] = None,
    failed_records_file: Optional[Path] = None,
) -> DatabaseWriter:
    """Open the database and check the mapping against the live table."""
    engine, mapping = open_database(settings, mapping_path)
    writer = DatabaseWriter(engine, mapping, failed_records_file)
    try:
        writer.prepare()
    except ValueError as exc:  # MappingError
        raise DatabaseSetupError(str(exc)) from exc
    except SQLAlchemyError as exc:
        raise DatabaseSetupError(f"Cannot reach the database: {describe_db_error(exc)}") from exc
    return writer
