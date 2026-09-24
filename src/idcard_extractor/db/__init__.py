"""Database layer: connection settings from the environment, a YAML/JSON column
mapping per deployment, and a writer that validates before it writes."""

from idcard_extractor.db.connection import DatabaseConfigError, DatabaseSettings, create_db_engine
from idcard_extractor.db.mapping import MappingError, TableMapping, load_mapping, record_context
from idcard_extractor.db.writer import (
    DatabaseWriter,
    WriteReport,
    create_table,
    load_failed_records,
)

__all__ = [
    "DatabaseConfigError",
    "DatabaseSettings",
    "DatabaseWriter",
    "MappingError",
    "TableMapping",
    "WriteReport",
    "create_db_engine",
    "create_table",
    "load_failed_records",
    "load_mapping",
    "record_context",
]
