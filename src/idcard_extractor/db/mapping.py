"""Field-to-column mapping for writing identity records into an existing table.

Each deployment describes its own table in a YAML (or JSON) file, so column names,
their order and value formats can differ per customer without code changes.
See ``config/db_mapping.example.yaml`` for a documented example.
"""

from __future__ import annotations

import json
import string
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from difflib import get_close_matches
from pathlib import Path
from typing import Any, Optional

import yaml

from idcard_extractor.models import IdentityRecord, ProcessedCard

IDENTITY_FIELDS: tuple[str, ...] = tuple(IdentityRecord.field_names())
META_FIELDS: tuple[str, ...] = ("card_id", "front_image", "back_image", "match_method", "processed_at")
SOURCE_FIELDS: tuple[str, ...] = IDENTITY_FIELDS + META_FIELDS
# Source fields holding dates instead of text.
DATE_FIELDS: dict[str, type] = {"date_of_birth": date, "processed_at": datetime}

WRITE_MODES = ("insert", "upsert", "skip_existing")

_TOP_LEVEL_KEYS = ("version", "table", "schema", "mode", "key_columns", "empty_as_null", "columns")
_COLUMN_KEYS = ("field", "template", "value", "values", "default", "format", "required", "truncate")
_MISSING = object()
_FORMATTER = string.Formatter()


class MappingError(ValueError):
    """The mapping is invalid; ``problems`` lists every issue found."""

    def __init__(self, problems: list[str], source: str = "mapping"):
        self.problems = list(problems)
        self.source = source
        super().__init__(f"Invalid {source}:\n" + "\n".join(f"  - {p}" for p in self.problems))


# ----------------------------------------------------------------------------
# Templates: "{first_name} {father_name}" with optional format specs
# ----------------------------------------------------------------------------

def parse_template(template: str) -> list[str]:
    """Return the field names used by ``template``; raise ValueError if malformed."""
    names = []
    for _, name, spec, conversion in _FORMATTER.parse(template):
        if name is None:
            continue
        if name == "":
            raise ValueError("empty placeholder {}; write a field name inside the braces")
        if conversion:
            raise ValueError(f"conversion '!{conversion}' is not supported")
        if spec and "{" in spec:
            raise ValueError("nested placeholders are not supported")
        names.append(name)
    return names


def render_template(template: str, context: dict[str, Any]) -> str:
    """Fill ``template``; empty fields render as nothing and whitespace is collapsed."""
    parts: list[str] = []
    for literal, name, spec, _ in _FORMATTER.parse(template):
        parts.append(literal)
        if name is None:
            continue
        value = context.get(name)
        if value is None or value == "":
            continue
        parts.append(format(value, spec or ""))
    return " ".join("".join(parts).split())


# ----------------------------------------------------------------------------
# Mapping model
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class ColumnSpec:
    """How the value of one database column is produced."""

    column: str
    field: Optional[str] = None
    template: Optional[str] = None
    value: Any = _MISSING
    values: Optional[dict] = None
    default: Any = _MISSING
    format: Optional[str] = None
    required: bool = False
    truncate: bool = False

    def describe(self) -> str:
        if self.field is not None:
            return f"field {self.field!r}"
        if self.template is not None:
            return f"template {self.template!r}"
        return "the constant value"

    def compute(self, context: dict[str, Any], empty_as_null: bool = True) -> Any:
        if self.field is not None:
            value = context.get(self.field)
        elif self.template is not None:
            value = render_template(self.template, context)
        else:
            value = self.value

        if isinstance(value, str):
            value = value.strip()
        if self.values is not None and value in self.values:
            value = self.values[value]
        if (value is None or value == "") and self.default is not _MISSING:
            value = self.default
        if self.format and isinstance(value, date):
            value = value.strftime(self.format)
        if empty_as_null and value == "":
            value = None
        return value


@dataclass(frozen=True)
class TableMapping:
    table: str
    columns: tuple[ColumnSpec, ...]
    schema: Optional[str] = None
    mode: str = "insert"
    key_columns: tuple[str, ...] = ()
    empty_as_null: bool = True
    source: str = "mapping"

    @property
    def qualified_table(self) -> str:
        return f"{self.schema}.{self.table}" if self.schema else self.table

    def build_row(self, context: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Compute ``{column: value}`` for one record, plus the problems found
        (a required column left empty, a failing format...)."""
        row: dict[str, Any] = {}
        problems: list[str] = []
        for spec in self.columns:
            try:
                value = spec.compute(context, self.empty_as_null)
            except (ValueError, TypeError) as exc:
                problems.append(f"column {spec.column!r}: cannot build the value ({exc})")
                continue
            if spec.required and (value is None or value == ""):
                problems.append(f"column {spec.column!r} is required but {spec.describe()} is empty")
            row[spec.column] = value
        return row, problems


# ----------------------------------------------------------------------------
# Loading and validation
# ----------------------------------------------------------------------------

def suggest(name: object, choices: Iterable[str]) -> str:
    matches = get_close_matches(str(name), list(choices), n=1)
    return f" (did you mean {matches[0]!r}?)" if matches else ""


def _check_keys(data: dict, allowed: tuple[str, ...], where: str, problems: list[str]) -> None:
    for key in data:
        if key not in allowed:
            prefix = f"{where}." if where else ""
            problems.append(f"{prefix}{key}: unknown option{suggest(key, allowed)}")


def _check_name(value: object, where: str, problems: list[str]) -> bool:
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{where}: must be a non-empty name")
        return False
    if value != value.strip() or len(value) > 128 or any(ord(ch) < 32 for ch in value):
        problems.append(f"{where}: {value!r} is not a valid name")
        return False
    return True


def _parse_column(name: str, raw: Any, problems: list[str]) -> Optional[ColumnSpec]:
    where = f"columns.{name}"
    if isinstance(raw, str):
        raw = {"field": raw}
    if not isinstance(raw, dict):
        problems.append(f"{where}: expected a field name or a set of options")
        return None
    _check_keys(raw, _COLUMN_KEYS, where, problems)

    sources = [key for key in ("field", "template", "value") if key in raw]
    if len(sources) != 1:
        problems.append(f"{where}: set exactly one of 'field', 'template' or 'value'")
        return None

    field_name = raw.get("field")
    if "field" in raw and (not isinstance(field_name, str) or field_name not in SOURCE_FIELDS):
        problems.append(f"{where}.field: unknown field {field_name!r}{suggest(field_name, SOURCE_FIELDS)}")

    template = raw.get("template")
    if "template" in raw:
        if not isinstance(template, str) or not template.strip():
            problems.append(f"{where}.template: must be non-empty text")
        else:
            try:
                names = parse_template(template)
            except ValueError as exc:
                problems.append(f"{where}.template: {exc}")
            else:
                if not names:
                    problems.append(f"{where}.template: uses no field; use 'value' for a constant")
                for used in names:
                    if used not in SOURCE_FIELDS:
                        problems.append(
                            f"{where}.template: unknown field {{{used}}}{suggest(used, SOURCE_FIELDS)}"
                        )

    values = raw.get("values")
    if values is not None and not isinstance(values, dict):
        problems.append(f"{where}.values: must map extracted values to stored values")

    fmt = raw.get("format")
    if fmt is not None:
        if field_name not in DATE_FIELDS:
            problems.append(f"{where}.format: only valid with a date field ({', '.join(DATE_FIELDS)})")
        elif not isinstance(fmt, str) or not fmt:
            problems.append(f"{where}.format: must be a date format such as '%Y-%m-%d'")
        else:
            try:
                date(2000, 1, 31).strftime(fmt)
            except ValueError as exc:
                problems.append(f"{where}.format: {exc}")

    for flag in ("required", "truncate"):
        if flag in raw and not isinstance(raw[flag], bool):
            problems.append(f"{where}.{flag}: must be true or false")

    return ColumnSpec(
        column=name,
        field=field_name if "field" in raw else None,
        template=template if "template" in raw else None,
        value=raw["value"] if "value" in raw else _MISSING,
        values=values if isinstance(values, dict) else None,
        default=raw["default"] if "default" in raw else _MISSING,
        format=fmt if isinstance(fmt, str) and fmt else None,
        required=raw.get("required") is True,
        truncate=raw.get("truncate") is True,
    )


def parse_mapping(data: Any, source: str = "mapping") -> TableMapping:
    """Validate an already-parsed mapping document; raise MappingError listing every problem."""
    if not isinstance(data, dict):
        raise MappingError(["the top level must be a set of 'key: value' options"], source)

    problems: list[str] = []
    _check_keys(data, _TOP_LEVEL_KEYS, "", problems)

    if data.get("version", 1) != 1:
        problems.append(f"version: unsupported value {data.get('version')!r} (expected 1)")

    table = data.get("table")
    _check_name(table, "table", problems)
    schema = data.get("schema")
    if schema is not None:
        _check_name(schema, "schema", problems)

    mode = data.get("mode", "insert")
    if mode not in WRITE_MODES:
        problems.append(f"mode: must be one of {', '.join(WRITE_MODES)}, got {mode!r}")

    empty_as_null = data.get("empty_as_null", True)
    if not isinstance(empty_as_null, bool):
        problems.append("empty_as_null: must be true or false")

    columns: list[ColumnSpec] = []
    raw_columns = data.get("columns")
    if not isinstance(raw_columns, dict) or not raw_columns:
        problems.append("columns: map at least one database column")
    else:
        for name, raw in raw_columns.items():
            if not isinstance(name, str):
                problems.append(f"columns: column name {name!r} must be text (quote it in YAML)")
                continue
            if not _check_name(name, f"columns.{name}", problems):
                continue
            spec = _parse_column(name, raw, problems)
            if spec is not None:
                columns.append(spec)

    key_columns = data.get("key_columns", [])
    if isinstance(key_columns, str):
        key_columns = [key_columns]
    if not isinstance(key_columns, list) or not all(isinstance(k, str) for k in key_columns):
        problems.append("key_columns: must be a list of column names")
        key_columns = []
    mapped = {spec.column for spec in columns}
    for key in key_columns:
        if isinstance(raw_columns, dict) and key not in raw_columns:
            problems.append(f"key_columns: {key!r} is not one of the mapped columns{suggest(key, mapped)}")
    if mode in ("upsert", "skip_existing") and not key_columns:
        problems.append(f"key_columns: required when mode is {mode!r} (e.g. the national ID column)")

    if problems:
        raise MappingError(problems, source)

    return TableMapping(
        table=table,
        columns=tuple(columns),
        schema=schema,
        mode=mode,
        key_columns=tuple(key_columns),
        empty_as_null=empty_as_null,
        source=source,
    )


def load_mapping(path: str | Path) -> TableMapping:
    """Read a ``.yaml`` / ``.yml`` / ``.json`` mapping file."""
    path = Path(path)
    source = f"mapping file {path}"
    if not path.is_file():
        raise MappingError(
            ["file not found; copy config/db_mapping.example.yaml and adapt it to your table"], source
        )
    try:
        text = path.read_text(encoding="utf-8-sig")
        data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise MappingError([f"cannot read the file: {exc}"], source) from exc
    return parse_mapping(data, source)


def record_context(card: ProcessedCard, processed_at: Optional[datetime] = None) -> dict[str, Any]:
    """Everything a mapping can refer to for one accepted card."""
    if card.record is None:
        raise ValueError(f"{card.card_id} has no validated record")
    context = card.record.to_dict()
    context.update(
        card_id=card.card_id,
        front_image=Path(card.front.source_image).name if card.front else None,
        back_image=Path(card.back.source_image).name if card.back else None,
        match_method=card.match_method,
        processed_at=processed_at or datetime.now().replace(microsecond=0),
    )
    return context
