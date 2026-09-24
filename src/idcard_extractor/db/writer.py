"""Write validated identities into an existing database table.

* The target table is reflected from the live database, and the mapping is checked
  against it before anything is written: missing columns, NOT NULL columns left
  without a value, key columns without a unique constraint...
* Values are always sent as bound parameters through SQLAlchemy Core, never
  formatted into SQL. Table and column names come from the reflected table, so a
  name from the mapping is only used after it was found in the database.
* Every record is written in its own transaction. A failing record is logged,
  saved to the failed-records file and skipped; the batch continues.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Engine,
    Float,
    Integer,
    MetaData,
    Table,
    Unicode,
    and_,
    insert,
    inspect,
    select,
    update,
)
from sqlalchemy.exc import (
    IntegrityError,
    InterfaceError,
    OperationalError,
    ProgrammingError,
    SQLAlchemyError,
)

from idcard_extractor.db.mapping import DATE_FIELDS, ColumnSpec, MappingError, TableMapping, suggest
from idcard_extractor.logging_utils import mask, redact

log = logging.getLogger(__name__)

# Errors that usually affect every following record too (connection lost,
# missing privileges...). After a few in a row the remaining writes are skipped.
_SYSTEMIC_ERRORS = (OperationalError, InterfaceError, ProgrammingError)


class RecordError(ValueError):
    """One record cannot be written. The message never contains the record's values."""


@dataclass
class Issue:
    level: str  # "error" or "warning"
    message: str


@dataclass
class RecordFailure:
    card_id: str
    reason: str
    record: dict[str, Any]


@dataclass
class WriteReport:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    failures: list[RecordFailure] = field(default_factory=list)
    aborted: bool = False

    @property
    def failed(self) -> int:
        return len(self.failures)

    @property
    def ok(self) -> bool:
        return not self.failures and not self.aborted

    def summary(self) -> str:
        text = f"inserted={self.inserted} updated={self.updated} skipped={self.skipped} failed={self.failed}"
        return text + (" (aborted)" if self.aborted else "")


# ----------------------------------------------------------------------------
# Value conversion to the column type
# ----------------------------------------------------------------------------

def _python_type(column: Column) -> Optional[type]:
    try:
        return column.type.python_type
    except NotImplementedError:
        return None


def _parse(parser, value: Any, column: str, expected: str):
    try:
        return parser(str(value).strip())
    except ValueError:
        raise RecordError(f"column {column!r}: expected {expected}") from None


def coerce_value(value: Any, column: Column, truncate: bool = False) -> Any:
    """Convert ``value`` to what ``column`` stores; raise RecordError when it does not fit."""
    if value is None:
        return None
    target = _python_type(column)
    name = column.name

    if target is str:
        text = value.isoformat() if isinstance(value, date) else str(value)
        length = getattr(column.type, "length", None)
        if length and len(text) > length:
            if not truncate:
                raise RecordError(f"column {name!r}: value has {len(text)} characters, the column allows {length}")
            log.warning("column %r: value truncated from %d to %d characters", name, len(text), length)
            text = text[:length]
        return text

    if target is datetime:
        if isinstance(value, datetime):
            result = value
        elif isinstance(value, date):
            result = datetime.combine(value, time())
        else:
            result = _parse(datetime.fromisoformat, value, name, "a date and time")
        if getattr(column.type, "timezone", False) and result.tzinfo is None:
            result = result.astimezone()
        return result

    if target is date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return _parse(date.fromisoformat, value, name, "a date (YYYY-MM-DD)")

    if target is int and not isinstance(value, bool):
        if isinstance(value, int):
            return value
        text = str(value).strip()
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text)
        raise RecordError(f"column {name!r}: expected a whole number")

    if target in (float, Decimal):
        try:
            return target(str(value).strip())
        except (ValueError, InvalidOperation):
            raise RecordError(f"column {name!r}: expected a number") from None

    return value


def describe_db_error(exc: SQLAlchemyError) -> str:
    """First line of the driver's error message, with identifiers and names redacted."""
    original = getattr(exc, "orig", None)
    text = str(original if original is not None else exc).strip()
    first_line = text.splitlines()[0] if text else type(exc).__name__
    return redact(first_line)[:300]


# ----------------------------------------------------------------------------
# Writer
# ----------------------------------------------------------------------------

class DatabaseWriter:
    def __init__(
        self,
        engine: Engine,
        mapping: TableMapping,
        failed_records_file: Optional[Path] = None,
        max_consecutive_errors: int = 3,
    ):
        self.engine = engine
        self.mapping = mapping
        self.failed_records_file = failed_records_file
        self.max_consecutive_errors = max_consecutive_errors
        self.table: Optional[Table] = None
        self._targets: dict[str, Column] = {}  # mapping column name -> reflected column

    # ----------------------------------------------------------------- checks

    def check(self) -> list[Issue]:
        """Compare the mapping with the live table. Never writes anything."""
        m = self.mapping
        issues: list[Issue] = []
        inspector = inspect(self.engine)
        if not inspector.has_table(m.table, schema=m.schema):
            issues.append(Issue("error", f"table {m.qualified_table!r} does not exist (create it, or run "
                                         "`idcard-extract db init` to create a table for trials)"))
            return issues

        table = Table(m.table, MetaData(), schema=m.schema, autoload_with=self.engine)
        by_name = {c.name: c for c in table.columns}
        by_lower: dict[str, list[Column]] = {}
        for column in table.columns:
            by_lower.setdefault(column.name.lower(), []).append(column)

        targets: dict[str, Column] = {}
        for spec in m.columns:
            column = by_name.get(spec.column)
            if column is None:
                candidates = by_lower.get(spec.column.lower(), [])
                if len(candidates) != 1:
                    issues.append(Issue("error", f"column {spec.column!r} does not exist in "
                                                 f"{m.qualified_table!r}{suggest(spec.column, by_name)}"))
                    continue
                column = candidates[0]
                issues.append(Issue("warning", f"column {spec.column!r} matched to {column.name!r} "
                                               "(different letter case)"))
            if column.computed is not None:
                issues.append(Issue("error", f"column {column.name!r} is computed by the database "
                                             "and cannot be written"))
                continue
            if any(existing is column for existing in targets.values()):
                issues.append(Issue("error", f"column {column.name!r} is mapped more than once"))
                continue
            targets[spec.column] = column
            if spec.format and _python_type(column) not in (str, None):
                issues.append(Issue("warning", f"column {column.name!r}: 'format' produces text but "
                                               f"the column type is {column.type}"))

        written = {c.name for c in targets.values()}
        autoincrement = table.autoincrement_column
        for column in table.columns:
            if column.name in written or column.nullable or column is autoincrement:
                continue
            if column.server_default is not None or column.identity is not None or column.computed is not None:
                continue
            issues.append(Issue("error", f"column {column.name!r} is NOT NULL without a default "
                                         "value, and the mapping does not fill it"))

        if m.key_columns:
            keys = {targets[k].name for k in m.key_columns if k in targets}
            if keys and not any(unique <= keys for unique in self._unique_sets(inspector, table)):
                issues.append(Issue("warning", "key columns " + ", ".join(sorted(keys)) + " are not covered "
                                               "by a primary key or unique constraint; the database itself "
                                               "will not prevent duplicates"))

        self.table, self._targets = table, targets
        return issues

    @staticmethod
    def _unique_sets(inspector, table: Table) -> list[set]:
        sets = [{c.name for c in table.primary_key.columns}]
        try:
            sets += [set(u["column_names"]) for u in inspector.get_unique_constraints(table.name, schema=table.schema)]
            sets += [set(i["column_names"]) for i in inspector.get_indexes(table.name, schema=table.schema)
                     if i.get("unique")]
        except (NotImplementedError, SQLAlchemyError):
            pass
        return [s for s in sets if s]

    def prepare(self) -> list[Issue]:
        """Reflect and validate. Raise MappingError if writing cannot work; return the warnings."""
        issues = self.check()
        errors = [issue.message for issue in issues if issue.level == "error"]
        if errors:
            raise MappingError(errors, f"mapping for table {self.mapping.qualified_table!r}")
        warnings = [issue for issue in issues if issue.level == "warning"]
        for warning in warnings:
            log.warning("Database mapping: %s", warning.message)
        return warnings

    # ------------------------------------------------------------------ write

    def write(self, records: Iterable[dict[str, Any]]) -> WriteReport:
        """Write each record (see ``mapping.record_context``) in its own transaction."""
        if self.table is None:
            self.prepare()

        report = WriteReport()
        consecutive = 0
        for number, record in enumerate(records, start=1):
            card_id = str(record.get("card_id") or f"record {number}")
            if report.aborted:
                self._fail(report, card_id, "not attempted: the database is unavailable", record)
                continue
            try:
                outcome = self._write_one(record)
            except RecordError as exc:
                self._fail(report, card_id, str(exc), record)
                continue
            except IntegrityError as exc:
                consecutive = 0
                self._fail(report, card_id, f"rejected by a database constraint: {describe_db_error(exc)}", record)
                continue
            except _SYSTEMIC_ERRORS as exc:
                consecutive += 1
                self._fail(report, card_id, f"database error: {describe_db_error(exc)}", record)
                if consecutive >= self.max_consecutive_errors:
                    report.aborted = True
                    log.error("Stopping database writes after %d consecutive database errors; "
                              "the remaining records are kept for a retry", consecutive)
                continue
            except SQLAlchemyError as exc:
                self._fail(report, card_id, f"database error: {describe_db_error(exc)}", record)
                continue
            except Exception as exc:  # a bug must not stop the batch either
                log.exception("%s: unexpected error while writing to the database", card_id)
                self._fail(report, card_id, f"unexpected error: {type(exc).__name__}", record)
                continue

            consecutive = 0
            setattr(report, outcome, getattr(report, outcome) + 1)
            log.info("%s: %s in %s (national_id=%s)", card_id, outcome, self.mapping.qualified_table,
                     mask(record.get("national_id")))

        self.save_failures(report.failures)
        log.info("Database write finished: %s", report.summary())
        return report

    def _write_one(self, record: dict[str, Any]) -> str:
        values, problems = self.mapping.build_row(record)
        if problems:
            raise RecordError("; ".join(problems))

        row: dict[Column, Any] = {}
        for spec in self.mapping.columns:
            column = self._targets[spec.column]
            row[column] = coerce_value(values[spec.column], column, spec.truncate)

        table, mode = self.table, self.mapping.mode
        keys = [self._targets[name] for name in self.mapping.key_columns]
        for key in keys:
            if row[key] is None:
                raise RecordError(f"key column {key.name!r} is empty")

        with self.engine.begin() as conn:
            if mode == "insert":
                conn.execute(insert(table).values(row))
                return "inserted"

            condition = and_(*(key == row[key] for key in keys))
            exists = conn.execute(select(keys[0]).where(condition).limit(1)).first() is not None
            if not exists:
                conn.execute(insert(table).values(row))
                return "inserted"
            if mode == "skip_existing":
                return "skipped"

            key_names = {key.name for key in keys}
            changes = {column: value for column, value in row.items() if column.name not in key_names}
            if changes:
                conn.execute(update(table).where(condition).values(changes))
            return "updated"

    def _fail(self, report: WriteReport, card_id: str, reason: str, record: dict[str, Any]) -> None:
        report.failures.append(RecordFailure(card_id, reason, dict(record)))
        log.warning("%s: not written to the database: %s", card_id, reason)

    # -------------------------------------------------------- failed records

    def save_failures(self, failures: list[RecordFailure], path: Optional[Path] = None) -> None:
        """Append failed records to a JSON Lines file so they can be retried.

        The file holds personal data: keep it as protected as the database itself.
        """
        path = path or self.failed_records_file
        if not path or not failures:
            return
        failed_at = datetime.now().replace(microsecond=0).isoformat()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                for failure in failures:
                    entry = {"failed_at": failed_at, "card_id": failure.card_id,
                             "reason": failure.reason, "record": failure.record}
                    fh.write(json.dumps(entry, ensure_ascii=False, default=_json_default) + "\n")
        except OSError as exc:
            log.error("Could not save the failed records to %s: %s", path, exc.strerror or exc)
            return
        log.warning("%d record(s) not written; saved to %s for `idcard-extract db retry`",
                    len(failures), path)


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def load_failed_records(path: str | Path) -> list[dict[str, Any]]:
    """Read the records saved by ``DatabaseWriter.save_failures``."""
    records = []
    with Path(path).open(encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                record = dict(json.loads(line)["record"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{path}:{line_number}: not a failed-record entry") from exc
            for name, kind in DATE_FIELDS.items():
                value = record.get(name)
                if isinstance(value, str) and value:
                    try:
                        record[name] = kind.fromisoformat(value)
                    except ValueError:
                        pass
            records.append(record)
    return records


def replace_failed_records(path: Path, failures: list[RecordFailure], writer: DatabaseWriter) -> None:
    """Rewrite ``path`` so it only keeps the records that still fail (removed when none)."""
    temp = path.with_name(path.name + ".tmp")
    if temp.exists():
        temp.unlink()
    if failures:
        writer.save_failures(failures, temp)
        os.replace(temp, path)
    else:
        path.unlink()


# ----------------------------------------------------------------------------
# Table creation for trials (`idcard-extract db init`)
# ----------------------------------------------------------------------------

def _column_type(spec: ColumnSpec):
    if spec.field in DATE_FIELDS and not spec.format and spec.values is None:
        return Date() if DATE_FIELDS[spec.field] is date else DateTime()
    if spec.field is None and spec.template is None:  # constant value
        value = spec.value
        if isinstance(value, bool):
            return Boolean()
        if isinstance(value, int):
            return Integer()
        if isinstance(value, float):
            return Float()
        if isinstance(value, datetime):
            return DateTime()
        if isinstance(value, date):
            return Date()
    # Unicode renders as NVARCHAR on SQL Server, so Arabic text is stored intact.
    return Unicode(255)


def create_table(engine: Engine, mapping: TableMapping) -> bool:
    """Create the mapped table if it does not exist. Returns True when it was created."""
    if inspect(engine).has_table(mapping.table, schema=mapping.schema):
        return False
    keys = set(mapping.key_columns)
    columns = [
        Column(spec.column, _column_type(spec),
               primary_key=spec.column in keys,
               nullable=not (spec.required or spec.column in keys))
        for spec in mapping.columns
    ]
    if not keys and "id" not in {spec.column for spec in mapping.columns}:
        columns.insert(0, Column("id", Integer, primary_key=True, autoincrement=True))
    Table(mapping.table, MetaData(), *columns, schema=mapping.schema).create(engine)
    return True
