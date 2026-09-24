from idcard_extractor.ocr.mrz_reader import check_digit, is_mrz_field, parse_td1, split_mrz_crop

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


def test_mrz_crop_is_separated_from_text_fields():
    crops = {"MRZ": {"crop": "m"}, "city": {"crop": "c"}, "nu_f": {"crop": "n"}}
    normal, mrz = split_mrz_crop(crops)
    assert set(normal) == {"city", "nu_f"}
    assert mrz["field"] == "MRZ" and mrz["crop"] == "m"
    assert is_mrz_field(" mrz1 ") and not is_mrz_field("city")
