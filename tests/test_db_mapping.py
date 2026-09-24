import json
from datetime import date, datetime
from pathlib import Path

import pytest

from idcard_extractor.db.mapping import MappingError, load_mapping, parse_mapping, record_context
from idcard_extractor.models import IdentityRecord, ProcessedCard

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "db_mapping.example.yaml"

CONTEXT = {
    "first_name": "أحمد",
    "father_name": "",
    "grandfather_name": "حسن",
    "family_name": "الكعبي",
    "national_id": "127901234567",
    "date_of_birth": date(1979, 1, 5),
    "sex": "ذكر",
    "birth_place": "",
}


def test_example_mapping_is_valid():
    mapping = load_mapping(EXAMPLE)
    assert mapping.table == "citizens"
    assert mapping.mode == "upsert"
    assert mapping.key_columns == ("national_no",)


def test_shorthand_and_options():
    mapping = parse_mapping({
        "table": "people",
        "columns": {
            "nid": {"field": "national_id", "required": True},
            "fname": "first_name",
            "full_name": {"template": "{first_name} {father_name} {grandfather_name}"},
            "gender": {"field": "sex", "values": {"ذكر": "M", "أنثى": "F"}},
            "dob_text": {"field": "date_of_birth", "format": "%d/%m/%Y"},
            "birth_year": {"template": "{date_of_birth:%Y}"},
            "place": {"field": "birth_place", "default": "غير معروف"},
            "source": {"value": "scanner-1"},
        },
    })
    row, problems = mapping.build_row(CONTEXT)
    assert problems == []
    assert row == {
        "nid": "127901234567",
        "fname": "أحمد",
        "full_name": "أحمد حسن",  # the empty father name leaves no double space
        "gender": "M",
        "dob_text": "05/01/1979",
        "birth_year": "1979",
        "place": "غير معروف",
        "source": "scanner-1",
    }


def test_empty_text_becomes_null_unless_disabled():
    columns = {"place": "birth_place"}
    assert parse_mapping({"table": "t", "columns": columns}).build_row(CONTEXT)[0] == {"place": None}
    keep = parse_mapping({"table": "t", "empty_as_null": False, "columns": columns})
    assert keep.build_row(CONTEXT)[0] == {"place": ""}


def test_required_value_missing_is_a_problem():
    mapping = parse_mapping({"table": "t", "columns": {"place": {"field": "birth_place", "required": True}}})
    _, problems = mapping.build_row(CONTEXT)
    assert problems == ["column 'place' is required but field 'birth_place' is empty"]


def test_all_problems_are_reported_together():
    with pytest.raises(MappingError) as info:
        parse_mapping({
            "table": "t",
            "mode": "upsert",
            "colums": {},
            "columns": {
                "a": "frist_name",
                "b": {"field": "first_name", "value": "x"},
                "c": {"template": "{first_name} {nam}"},
                "d": {"field": "first_name", "format": "%Y"},
                "e": {"field": "sex", "required": "yes"},
                "f": {"field": "sex", "colour": "red"},
            },
        })
    problems = "\n".join(info.value.problems)
    assert "colums: unknown option (did you mean 'columns'?)" in problems
    assert "columns.a.field: unknown field 'frist_name' (did you mean 'first_name'?)" in problems
    assert "columns.b: set exactly one of 'field', 'template' or 'value'" in problems
    assert "columns.c.template: unknown field {nam}" in problems
    assert "columns.d.format: only valid with a date field" in problems
    assert "columns.e.required: must be true or false" in problems
    assert "columns.f.colour: unknown option" in problems
    assert "key_columns: required when mode is 'upsert'" in problems


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"table": "t", "columns": {}}, "columns: map at least one database column"),
        ({"columns": {"a": "sex"}}, "table: must be a non-empty name"),
        ({"table": "t", "mode": "merge", "columns": {"a": "sex"}}, "mode: must be one of"),
        ({"table": "t", "key_columns": ["x"], "columns": {"a": "sex"}}, "key_columns: 'x' is not one of"),
        ({"table": "t", "columns": {True: "sex"}}, "must be text (quote it in YAML)"),
        ({"table": "t", "columns": {"a": {"template": "{first_name.upper}"}}}, "unknown field {first_name.upper}"),
        ({"table": "t", "columns": {"a": {"template": "{first_name!r}"}}}, "conversion '!r' is not supported"),
        ({"table": "t", "version": 2, "columns": {"a": "sex"}}, "version: unsupported value 2"),
    ],
)
def test_invalid_mappings(data, message):
    with pytest.raises(MappingError) as info:
        parse_mapping(data)
    assert any(message in problem for problem in info.value.problems), info.value.problems


def test_yaml_json_and_bom_files(tmp_path):
    yaml_file = tmp_path / "m.yaml"
    yaml_file.write_text("﻿table: t\ncolumns:\n  الاسم: first_name\n", encoding="utf-8")
    assert load_mapping(yaml_file).columns[0].column == "الاسم"

    json_file = tmp_path / "m.json"
    json_file.write_text(json.dumps({"table": "t", "columns": {"nid": "national_id"}}), encoding="utf-8")
    assert load_mapping(json_file).columns[0].field == "national_id"


def test_missing_or_broken_file(tmp_path):
    with pytest.raises(MappingError, match="file not found"):
        load_mapping(tmp_path / "missing.yaml")
    broken = tmp_path / "broken.yaml"
    broken.write_text("table: [unclosed\n", encoding="utf-8")
    with pytest.raises(MappingError, match="cannot read the file"):
        load_mapping(broken)


def test_record_context(make_front, make_back):
    record = IdentityRecord(first_name="أحمد", national_id="127901234567", date_of_birth=date(1979, 1, 5))
    card = ProcessedCard("CARD_001", True, "مقبولة", front=make_front(), back=make_back(),
                         match_method="national_id", record=record)
    context = record_context(card, processed_at=datetime(2026, 9, 24, 10, 0))
    assert context["first_name"] == "أحمد"
    assert context["front_image"] == "CARD_001.jpg"
    assert context["back_image"] == "CARD_002.jpg"
    assert context["match_method"] == "national_id"
    assert context["processed_at"] == datetime(2026, 9, 24, 10, 0)

    with pytest.raises(ValueError):
        record_context(ProcessedCard("CARD_009", False, "x"))
