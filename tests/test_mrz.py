import sys
import types

from idcard_extractor.ocr import mrz_reader
from idcard_extractor.ocr.mrz_reader import check_digit, is_mrz_field, parse_td1, split_mrz_crop

# Typical Tesseract damage: fillers read as K or dropped, 'O' read for '0'.
DAMAGED_IRQ = [
    "IDIRQA123456784127901234567KK<",
    "79O1O58M3001019IRO<<<<<<<<<O",
    "ALKAABI<<AHMEDK<<<<<<KK<",
]

# Specimen from ICAO Doc 9303 part 5 (TD1).
ICAO_TD1 = [
    "I<UTOD231458907<<<<<<<<<<<<<<<",
    "7408122F1204159UTO<<<<<<<<<<<6",
    "ERIKSSON<<ANNA<MARIA<<<<<<<<<<",
]


def test_check_digit_matches_icao_specimen():
    assert check_digit("D23145890") == "7"
    assert check_digit("740812") == "2"
    assert check_digit("120415") == "9"


def test_parse_icao_specimen():
    data = parse_td1(ICAO_TD1)
    assert data["mrz_type"] == "TD1"
    assert data["number"] == "D23145890"
    assert data["date_of_birth"] == "740812"
    assert data["expiration_date"] == "120415"
    assert data["sex"] == "F"
    assert data["country"] == data["nationality"] == "UTO"
    assert data["surname"] == "ERIKSSON"
    assert data["names"] == "ANNA MARIA"
    assert data["valid_number"] and data["valid_date_of_birth"] and data["valid_expiration_date"]


def test_optional_data_carries_the_national_id():
    line1 = "IDIRQ" + "A12345678" + check_digit("A12345678") + "127901234567<<<"
    line2 = "790105" + check_digit("790105") + "M" + "300101" + check_digit("300101") + "IRQ" + "<" * 11 + "0"
    line3 = "ALKAABI<<AHMED".ljust(30, "<")
    data = parse_td1([line1, line2, line3])
    assert data["optional1"] == "127901234567"
    assert data["number"] == "A12345678"
    assert data["valid_number"]
    assert data["sex"] == "M"


def test_parse_td1_needs_three_lines():
    assert parse_td1(ICAO_TD1[:2]) is None


def test_parse_td1_tolerates_ocr_damage():
    data = parse_td1(DAMAGED_IRQ)
    assert data["number"] == "A12345678"
    assert data["optional1"] == "127901234567"
    assert data["date_of_birth"] == "790105"
    assert data["sex"] == "M"
    assert data["valid_number"] and data["valid_date_of_birth"] and data["valid_expiration_date"]


def test_tesseract_fallback_accepts_damaged_lines(monkeypatch):
    fake = types.ModuleType("pytesseract")
    fake.TesseractNotFoundError = type("TesseractNotFoundError", (Exception,), {})
    fake.image_to_string = lambda image, config: "\n".join(["noise", *DAMAGED_IRQ]) + "\n"
    monkeypatch.setitem(sys.modules, "pytesseract", fake)
    monkeypatch.setattr(mrz_reader, "_preprocess", lambda image: image)

    data = mrz_reader._read_with_tesseract(object())
    assert data["optional1"] == "127901234567" and data["valid_date_of_birth"]


def test_the_verified_reading_wins(monkeypatch):
    image = types.SimpleNamespace(size=1)
    good = parse_td1(ICAO_TD1)
    garbage = {"mrz_type": "TD2", "number": "7408122F1", "valid_number": False}
    fallback_calls = []

    def fallback(img, cmd=None):
        fallback_calls.append(img)
        return good

    monkeypatch.setattr(mrz_reader, "_read_with_tesseract", fallback)
    monkeypatch.setattr(mrz_reader, "_read_with_mrzmini", lambda img: garbage)
    assert mrz_reader.read_mrz(image) is good

    # A verified mrzmini reading is used directly; the fallback does not run.
    monkeypatch.setattr(mrz_reader, "_read_with_mrzmini", lambda img: good)
    fallback_calls.clear()
    assert mrz_reader.read_mrz(image) is good and fallback_calls == []

    # Nothing better available: keep whatever mrzmini returned.
    monkeypatch.setattr(mrz_reader, "_read_with_mrzmini", lambda img: garbage)
    monkeypatch.setattr(mrz_reader, "_read_with_tesseract", lambda img, cmd=None: None)
    assert mrz_reader.read_mrz(image) is garbage


def test_mrz_crop_is_separated_from_text_fields():
    crops = {"MRZ": {"crop": "m"}, "city": {"crop": "c"}, "nu_f": {"crop": "n"}}
    normal, mrz = split_mrz_crop(crops)
    assert set(normal) == {"city", "nu_f"}
    assert mrz["field"] == "MRZ" and mrz["crop"] == "m"
    assert is_mrz_field(" mrz1 ") and not is_mrz_field("city")
