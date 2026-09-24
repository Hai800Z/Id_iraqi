import json
from datetime import date

import pytest
from openpyxl import load_workbook
from sqlalchemy import create_engine, text

from idcard_extractor import cli, pipeline
from idcard_extractor.models import IdentityRecord, PipelineResult, ProcessedCard

MAPPING = """\
table: citizens
mode: upsert
key_columns: [national_no]
columns:
  national_no: {field: national_id, required: true}
  fname: first_name
  birth_date: date_of_birth
  gender: {field: sex, values: {"ذكر": "M", "أنثى": "F"}}
  source_file: front_image
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "db_mapping.yaml").write_text(MAPPING, encoding="utf-8")
    (tmp_path / ".env").write_text("DB_NAME=data/test.db\nLOG_LEVEL=WARNING\n", encoding="utf-8")
    return tmp_path


def db_rows(project):
    engine = create_engine(f"sqlite:///{project / 'data' / 'test.db'}")
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(text("SELECT * FROM citizens ORDER BY national_no"))]


def test_version(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["--version"])
    assert info.value.code == 0
    assert "idcard-extract" in capsys.readouterr().out


def test_db_init_check_and_retry(project, capsys):
    assert cli.main(["db", "check"]) == 1
    assert "does not exist" in capsys.readouterr().out

    assert cli.main(["db", "init"]) == 0
    assert cli.main(["db", "init"]) == 0
    assert "already exists" in capsys.readouterr().out
    assert cli.main(["db", "check"]) == 0
    assert ": OK" in capsys.readouterr().out

    failed = project / "output" / "db_failed_records.jsonl"
    failed.parent.mkdir()
    entry = {"card_id": "CARD_001", "reason": "database error", "record": {
        "card_id": "CARD_001", "national_id": "127901234567", "first_name": "أحمد",
        "date_of_birth": "1979-01-05", "sex": "ذكر", "front_image": "front.jpg"}}
    failed.write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")

    assert cli.main(["db", "retry"]) == 0
    assert not failed.exists()
    [row] = db_rows(project)
    assert (row["fname"], row["birth_date"], row["gender"]) == ("أحمد", "1979-01-05", "M")


def test_invalid_mapping_is_reported(project, capsys):
    (project / "config" / "db_mapping.yaml").write_text("table: citizens\ncolumns:\n  a: frist_name\n",
                                                        encoding="utf-8")
    assert cli.main(["db", "check"]) == 1
    assert "did you mean 'first_name'" in capsys.readouterr().err


def test_run_without_images_fails(project):
    assert cli.main(["run", str(project / "no-such-folder")]) == 1


class FakePipeline:
    """Stands in for the YOLO / PaddleOCR pipeline."""

    def __init__(self, settings):
        self.settings = settings

    def load_models(self):
        pass

    def run(self, images):
        from idcard_extractor.models import CardSide

        front = CardSide("CARD_001", str(images[0]), 1, "iraq id card", None, 0.9, 0.9, True)
        record = IdentityRecord(first_name="أحمد", national_id="127901234567",
                                date_of_birth=date(1979, 1, 5), sex="ذكر")
        accepted = ProcessedCard("CARD_001", True, "مقبولة", front=front, match_method="national_id",
                                 record=record)
        rejected = ProcessedCard("CARD_002", False, "لا يوجد وجه")
        return PipelineResult(sides=[front], accepted=[accepted], rejected=[rejected])


def test_run_writes_excel_and_database(project, monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "CardPipeline", FakePipeline)
    images = project / "images"
    images.mkdir()
    (images / "front.jpg").write_bytes(b"not a real image")
    assert cli.main(["db", "init"]) == 0

    excel = project / "result.xlsx"
    assert cli.main(["run", str(images), "--db", "--excel", str(excel)]) == 0

    out = capsys.readouterr().out
    assert "Accepted cards      : 1" in out
    assert "inserted=1" in out
    assert load_workbook(excel)["Cards"]["A2"].value == "أحمد"
    [row] = db_rows(project)
    assert (row["national_no"], row["source_file"]) == ("127901234567", "front.jpg")


def test_run_continues_when_the_database_is_misconfigured(project, monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "CardPipeline", FakePipeline)
    (project / "img.png").write_bytes(b"x")

    # The table does not exist: the database stage is disabled, Excel is still written.
    code = cli.main(["run", str(project / "img.png"), "--db", "--excel", str(project / "r.xlsx")])
    assert code == 2
    assert (project / "r.xlsx").is_file()
    assert "database stage disabled" in capsys.readouterr().out
