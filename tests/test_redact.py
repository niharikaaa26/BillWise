import pytest

from billwise.redact import Redactor, redact

SAMPLE = """Patient Name: DOE, JANE
Date of Birth: 03/12/1981
Member ID: W123456789
Account Number: 00482913
SSN: 912-34-5678
Mailing Address:
  Jane Doe
  123 Maple St Apt 4
  Riverton, OH 45011
Phone: (513) 555-0123
Email: jane.doe7@example.com
Dear Jane,
"""


def types_and_values(text):
    r = redact(text)
    return {(s.type, text[s.start:s.end]) for s in r.spans}, r


@pytest.mark.parametrize("typ,value", [
    ("NAME", "DOE, JANE"),
    ("NAME", "Jane Doe"),
    ("NAME", "Jane"),
    ("DOB", "03/12/1981"),
    ("MEMBER_ID", "W123456789"),
    ("ACCOUNT_ID", "00482913"),
    ("SSN", "912-34-5678"),
    ("ADDRESS", "123 Maple St Apt 4"),
    ("ADDRESS", "Riverton, OH 45011"),
    ("PHONE", "(513) 555-0123"),
    ("EMAIL", "jane.doe7@example.com"),
])
def test_detects_each_type(typ, value):
    found, _ = types_and_values(SAMPLE)
    assert (typ, value) in found


def test_no_original_values_remain():
    _, r = types_and_values(SAMPLE)
    for secret in ["DOE", "Jane", "03/12/1981", "W123456789", "00482913", "912-34-5678",
                   "Maple", "45011", "555-0123", "example.com"]:
        assert secret not in r.redacted_text, secret


def test_same_person_same_placeholder():
    _, r = types_and_values(SAMPLE)
    name_phs = {s.placeholder for s in r.spans if s.type == "NAME"}
    assert name_phs == {"[NAME_1]"}


def test_distinct_people_get_distinct_placeholders():
    r = redact("Patient: Jane Doe\nGuarantor: Kevin Smith\n")
    assert "[NAME_1]" in r.redacted_text and "[NAME_2]" in r.redacted_text


def test_restore_is_exact():
    r = redact(SAMPLE)
    assert r.restore() == SAMPLE


def test_restore_other_text_uses_mapping():
    r = redact("Patient: Jane Doe\n")
    assert r.restore("Hello [NAME_1]") == "Hello Jane Doe"


def test_mapping_not_in_repr():
    r = redact(SAMPLE)
    assert "W123456789" not in repr(r)


def test_service_dates_and_amounts_untouched():
    text = "03/14/2025 99213 Office visit 250.00 140.00 112.00 28.00\nDate of Birth: 01/02/1970\n"
    r = redact(text)
    assert "03/14/2025 99213 Office visit 250.00 140.00 112.00 28.00" in r.redacted_text
    assert "01/02/1970" not in r.redacted_text


def test_name_list_finds_unlabeled_name():
    r = redact("MARIA GARCIA\n12 Oak Ave\n")
    assert "MARIA" not in r.redacted_text and "GARCIA" not in r.redacted_text


def test_org_names_not_treated_as_people():
    r = redact("Grace Hospital\nMercy Valley Regional Hospital\n")
    assert r.spans == []


def test_custom_name_list():
    r = Redactor(first_names={"zebulon"}).redact("ZEBULON QUILL\n")
    assert "[NAME_1]" in r.redacted_text


def test_empty_text():
    r = redact("")
    assert r.redacted_text == "" and r.spans == []


@pytest.mark.parametrize("text,typ,value", [
    ("Patlent Name: SMITH, PATRICE\n", "NAME", "SMITH, PATRICE"),
    ("Subscrlber: Xiomara Hernandez\n", "NAME", "Xiomara Hernandez"),
    ("Date of Birlh: 9/19/46\n", "DOB", "9/19/46"),
    ("Memher ID: CPH-5900-9199\n", "MEMBER_ID", "CPH-5900-9199"),
    ("Acc0unt Number: 33036758\n", "ACCOUNT_ID", "33036758"),
    ("STATEMENT FOR: Kim, Marcus D\n", "NAME", "Kim, Marcus D"),
    ("Guarantor #: 6905929\n", "ACCOUNT_ID", "6905929"),
    ("Insured's ID No.: ZQ4410293-02\n", "MEMBER_ID", "ZQ4410293-02"),
    ("Cell: +1 845 555 0101\n", "PHONE", "+1 845 555 0101"),
])
def test_round2_patterns(text, typ, value):
    found, _ = types_and_values(text)
    assert (typ, value) in found


def test_name_above_street_address_without_label():
    found, _ = types_and_values("SIOBHAN KOWALSKI\n2110 BRIAR WAY APT 38B\nSPRINGDALE AR 72711\n")
    assert ("NAME", "SIOBHAN KOWALSKI") in found


def test_description_comma_is_not_a_name_label():
    r = redact("02/17/25 99213  OFFICE VISIT, ESTABLISHED PATIENT, LOW COMPLEXITY   1   150.00\n")
    assert r.spans == []


def test_org_above_address_is_not_a_name():
    r = redact("Northgate Pediatrics\n15 Commerce Blvd\n")
    assert all(s.type != "NAME" for s in r.spans)
