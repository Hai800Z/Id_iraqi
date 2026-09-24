from datetime import date, datetime

import pytest
from sqlalchemy import Date, DateTime, MetaData, Table, create_engine, inspect, select, text

from idcard_extractor.db.mapping import MappingError, parse_mapping
from idcard_extractor.db.writer import (
    DatabaseWriter,
    create_table,
    load_failed_records,
    replace_failed_records,
)

CITIZENS_DDL = """
CREATE TABLE citizens (
    id INTEGER PRIMARY KEY,
    national_no VARCHAR(12) NOT NULL,
    fname VARCHAR(50),
    full_name VARCHAR(200),
    birth_date DATE,
    gender CHAR(1),
    family_no INTEGER,
    imported_at DATETIME,
    imported_by VARCHAR(30) NOT NULL DEFAULT 'system',
    CONSTRAINT uq_citizens_national_no UNIQUE (national_no)
)
"""

COLUMNS = {
    "national_no": {"field": "national_id", "required": True},
    "fname": "first_name",
    "full_name": {"template": "{first_name} {father_name} {family_name}"},
    "birth_date": "date_of_birth",
    "gender": {"field": "sex", "values": {"ذكر": "M", "أنثى": "F"}},
    "family_no": "family_number",
    "imported_at": "processed_at",
}


def person(n=1, **overrides):
    record = {
        "card_id": f"CARD_{n:03d}",
        "first_name": "أحمد",
        "father_name": "علي",
        "family_name": "الكعبي",
        "national_id": f"12790123456{n}",
        "date_of_birth": date(1979, 1, 5),
        "sex": "ذكر",
        "family_number": "12345",
        "processed_at": datetime(2026, 9, 24, 10, 0),
    }
    record.update(overrides)
    return record


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", hide_parameters=True)
    with engine.begin() as conn:
        conn.execute(text(CITIZENS_DDL))
    return engine


def writer_for(engine, failed_file=None, **options):
    options.setdefault("columns", COLUMNS)
    return DatabaseWriter(engine, parse_mapping({"table": "citizens", **options}), failed_file)


def rows(engine, table_name="citizens", order_by="national_no"):
    table = Table(table_name, MetaData(), autoload_with=engine)
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(table).order_by(table.c[order_by]))]


def test_matching_table_has_no_issues(engine):
    assert writer_for(engine).check() == []
    assert writer_for(engine, mode="upsert", key_columns=["national_no"]).check() == []


def test_values_are_converted_to_the_column_types(engine):
    report = writer_for(engine).write([person(1), person(2, sex="أنثى", father_name="")])
    assert report.ok and report.inserted == 2

    first, second = rows(engine)
    assert first["birth_date"] == date(1979, 1, 5)
    assert first["imported_at"] == datetime(2026, 9, 24, 10, 0)
    assert first["family_no"] == 12345
    assert first["gender"] == "M" and second["gender"] == "F"
    assert first["full_name"] == "أحمد علي الكعبي"
    assert second["full_name"] == "أحمد الكعبي"
    assert first["imported_by"] == "system"  # database default


def test_a_duplicate_fails_alone_and_is_saved_for_retry(engine, tmp_path):
    failed_file = tmp_path / "failed.jsonl"
    report = writer_for(engine, failed_file).write([person(1), person(1), person(2)])

    assert (report.inserted, report.failed) == (2, 1)
    failure = report.failures[0]
    assert failure.card_id == "CARD_001"
    assert failure.reason.startswith("rejected by a database constraint")
    assert "127901234561" not in failure.reason

    saved = load_failed_records(failed_file)
    assert saved[0]["national_id"] == "127901234561"
    assert saved[0]["date_of_birth"] == date(1979, 1, 5)
    assert saved[0]["processed_at"] == datetime(2026, 9, 24, 10, 0)


def test_skip_existing_and_upsert(engine):
    writer_for(engine).write([person(1)])

    skip = writer_for(engine, mode="skip_existing", key_columns=["national_no"])
    report = skip.write([person(1, first_name="محمد"), person(2)])
    assert (report.inserted, report.skipped) == (1, 1)
    assert rows(engine)[0]["fname"] == "أحمد"

    upsert = writer_for(engine, mode="upsert", key_columns=["national_no"])
    report = upsert.write([person(1, first_name="محمد")])
    assert report.updated == 1
    assert rows(engine)[0]["fname"] == "محمد"
    assert len(rows(engine)) == 2


def test_values_are_never_executed_as_sql(engine):
    hostile = "x'); DROP TABLE citizens; --"
    assert writer_for(engine).write([person(1, first_name=hostile)]).ok
    assert inspect(engine).has_table("citizens")
    assert rows(engine)[0]["fname"] == hostile


def test_arabic_table_and_column_names(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ar.db'}")
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE "المواطنون" ("الرقم الوطني" VARCHAR(12) PRIMARY KEY, '
                          '"الاسم" VARCHAR(50), "تاريخ الميلاد" DATE)'))
    mapping = parse_mapping({
        "table": "المواطنون",
        "columns": {"الرقم الوطني": "national_id", "الاسم": "first_name", "تاريخ الميلاد": "date_of_birth"},
    })
    assert DatabaseWriter(engine, mapping).write([person(1)]).ok
    assert rows(engine, "المواطنون", "الرقم الوطني")[0]["الاسم"] == "أحمد"


def test_missing_column_stops_before_writing(engine):
    writer = writer_for(engine, columns={**COLUMNS, "surnam": "family_name"})
    issues = writer.check()
    assert [issue.level for issue in issues] == ["error"]
    assert "column 'surnam' does not exist in 'citizens'" in issues[0].message
    with pytest.raises(MappingError):
        writer.prepare()
    assert rows(engine) == []


def test_missing_table(engine):
    writer = DatabaseWriter(engine, parse_mapping({"table": "nope", "columns": {"a": "sex"}}))
    assert "table 'nope' does not exist" in writer.check()[0].message


def test_letter_case_differences_are_tolerated(engine):
    columns = {"NATIONAL_NO": "national_id", "FName": "first_name"}
    writer = writer_for(engine, columns=columns)
    assert [issue.level for issue in writer.check()] == ["warning", "warning"]
    assert writer.write([person(1)]).ok
    assert rows(engine)[0]["fname"] == "أحمد"


def test_the_same_column_cannot_be_mapped_twice(engine):
    issues = writer_for(engine, columns={"fname": "first_name", "FNAME": "family_name",
                                         "national_no": "national_id"}).check()
    assert any("mapped more than once" in issue.message for issue in issues)


def test_not_null_column_without_value_is_an_error(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'strict.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE strict (id INTEGER PRIMARY KEY, nid VARCHAR(12), code VARCHAR(5) NOT NULL)"))
    writer = DatabaseWriter(engine, parse_mapping({"table": "strict", "columns": {"nid": "national_id"}}))
    messages = [issue.message for issue in writer.check()]
    assert messages == ["column 'code' is NOT NULL without a default value, and the mapping does not fill it"]


def test_key_without_unique_constraint_is_a_warning(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'loose.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE loose (nid VARCHAR(12), name VARCHAR(50))"))
    mapping = parse_mapping({"table": "loose", "mode": "upsert", "key_columns": ["nid"],
                             "columns": {"nid": "national_id", "name": "first_name"}})
    issues = DatabaseWriter(engine, mapping).check()
    assert [issue.level for issue in issues] == ["warning"]
    assert "not covered by a primary key or unique constraint" in issues[0].message


def test_too_long_values_fail_or_are_truncated(engine):
    report = writer_for(engine, columns={"national_no": "national_id", "gender": "sex"}).write([person(1)])
    assert report.failed == 1
    assert report.failures[0].reason == "column 'gender': value has 3 characters, the column allows 1"

    truncating = writer_for(engine, columns={"national_no": "national_id",
                                             "gender": {"field": "sex", "truncate": True}})
    assert truncating.write([person(1)]).ok
    assert rows(engine)[0]["gender"] == "ذ"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"family_number": "12a45"}, "column 'family_no': expected a whole number"),
        ({"national_id": ""}, "column 'national_no' is required but field 'national_id' is empty"),
    ],
)
def test_invalid_records_are_skipped(engine, overrides, reason):
    report = writer_for(engine).write([person(1, **overrides), person(2)])
    assert report.inserted == 1
    assert report.failures[0].reason == reason


def test_empty_key_value_is_rejected(engine):
    columns = {"national_no": "national_id", "fname": "first_name"}
    writer = writer_for(engine, columns=columns, mode="upsert", key_columns=["national_no"])
    report = writer.write([person(1, national_id="")])
    assert report.failures[0].reason == "key column 'national_no' is empty"


def test_database_outage_stops_writes_without_raising(engine, tmp_path):
    failed_file = tmp_path / "failed.jsonl"
    writer = writer_for(engine, failed_file)
    writer.prepare()
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE citizens"))

    report = writer.write([person(n) for n in range(1, 6)])

    assert report.aborted and report.failed == 5
    kinds = [failure.reason.split(":")[0] for failure in report.failures]
    assert kinds == ["database error"] * 3 + ["not attempted"] * 2
    assert len(load_failed_records(failed_file)) == 5


def test_retry_keeps_only_records_that_still_fail(engine, tmp_path):
    failed_file = tmp_path / "failed.jsonl"
    writer_for(engine, failed_file).write([person(1), person(1)])
    assert len(load_failed_records(failed_file)) == 1

    retry = writer_for(engine)
    report = retry.write(load_failed_records(failed_file))
    replace_failed_records(failed_file, report.failures, retry)
    assert len(load_failed_records(failed_file)) == 1  # still a duplicate

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM citizens"))
    report = retry.write(load_failed_records(failed_file))
    replace_failed_records(failed_file, report.failures, retry)
    assert report.ok and not failed_file.exists()


def test_create_table_from_mapping(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'new.db'}")
    mapping = parse_mapping({
        "table": "people",
        "mode": "upsert",
        "key_columns": ["nid"],
        "columns": {
            "nid": "national_id",
            "name": "first_name",
            "dob": "date_of_birth",
            "dob_text": {"field": "date_of_birth", "format": "%d/%m/%Y"},
            "created": "processed_at",
        },
    })
    assert create_table(engine, mapping) is True
    assert create_table(engine, mapping) is False

    inspector = inspect(engine)
    types = {column["name"]: column["type"] for column in inspector.get_columns("people")}
    assert isinstance(types["dob"], Date) and isinstance(types["created"], DateTime)
    assert inspector.get_pk_constraint("people")["constrained_columns"] == ["nid"]

    writer = DatabaseWriter(engine, mapping)
    assert writer.check() == []
    assert writer.write([person(1)]).ok
    row = rows(engine, "people", "nid")[0]
    assert row["dob"] == date(1979, 1, 5) and row["dob_text"] == "05/01/1979"
