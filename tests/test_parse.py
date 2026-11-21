from datetime import date
from decimal import Decimal

import pytest

from billwise.parse import classify_header, parse, parse_amount, parse_code, parse_date


@pytest.mark.parametrize("raw,expected", [
    ("250.00", "250.00"), ("$1,234.56", "1234.56"), ("S45.00", "45.00"),
    ("1O5.00", "105.00"), ("82.7l", "82.71"), ("82,40", "82.40"), ("1,060,94", "1060.94"),
    ("-113.15", "-113.15"), ("(45.20)", "-45.20"),
])
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["", "--", "N/A", "abc", None])
def test_parse_amount_missing(raw):
    assert parse_amount(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("03/14/2025", date(2025, 3, 14)), ("3/4/25", date(2025, 3, 4)),
    ("2025-03-14", date(2025, 3, 14)), ("Mar 14, 2025", date(2025, 3, 14)),
    ("Sept 8, 2025", date(2025, 9, 8)),
])
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("99213", "99213"), ("J1885", "J1885"), ("992l3", "99213"), ("G04O9", "G0409"),
    ("hello", None), ("1234", None), ("Z1234", None),
])
def test_parse_code(raw, expected):
    assert parse_code(raw) == expected


@pytest.mark.parametrize("header,field", [
    ("You Owe", "patient_responsibility"), ("Paid by Plan", "insurer_paid"),
    ("Allowed Amount", "allowed_amount"), ("CHARGES", "billed_amount"),
    ("Date of Service", "service_date"), ("CPT/HCPCS", "code"), ("Description", "description"),
    ("ADJUST", "_ignore"),
])
def test_classify_header(header, field):
    assert classify_header(header) == field


PIPE = """Service Date | Code  | Description              | Billed  | Allowed | Plan Paid | You Owe
-------------+-------+--------------------------+---------+---------+-----------+--------
03/14/2025   | 99213 | Office visit, est        | $250.00 | $140.00 | $112.00   | $28.00
             |       | patient                  |         |         |           |
03/14/2025   | 36415 | Venipuncture             | $30.00  | --      | $0.00     | $0.00
TOTALS       |       |                          | $280.00 |         |           |
"""


def test_pipe_table():
    r = parse(PIPE)
    assert r.format == "pipe_table"
    assert len(r.line_items) == 2
    a, b = r.line_items
    assert a.description == "Office visit, est patient"
    assert (a.billed_amount, a.allowed_amount, a.insurer_paid, a.patient_responsibility) == \
        (Decimal("250.00"), Decimal("140.00"), Decimal("112.00"), Decimal("28.00"))
    assert b.allowed_amount is None


CSV = '''Svc Date,CPT/HCPCS,Description,Charge,Ins Paid,Your Balance
06/23/2025,99285,"Emergency department visit, high
severity",1708.64,726.08,181.52
06/23/2025,,Supplies,20.00,0.00,20.00
'''


def test_csv_multiline_and_missing_code():
    r = parse(CSV)
    assert r.format == "csv"
    assert [i.code for i in r.line_items] == ["99285", None]
    assert r.line_items[0].description == "Emergency department visit, high severity"
    assert r.line_items[0].allowed_amount is None


FIXED = """DATE       CODE    DESCRIPTION                           CHARGES   INS PAID     ADJUST    PT RESP
------------------------------------------------------------------------------------------------
07/19/2025 80053   COMPREHENSIVE METABOLIC PANEL          320.60     230.80      64.16      25.64
           97110   THERAPEUTIC EXERCISES, EACH 15         125.74                            125.74
                   MINUTES
                   TOTAL CHARGES                          446.34
07/30/2025         PAYMENT - THANK YOU                   -113.15
"""


def test_fixed_width_columns_blank_date_and_wrap():
    r = parse(FIXED)
    assert r.format == "fixed_width"
    assert len(r.line_items) == 2
    a, b = r.line_items
    assert a.insurer_paid == Decimal("230.80") and a.patient_responsibility == Decimal("25.64")
    assert b.service_date == date(2025, 7, 19)  # inherited
    assert b.description == "THERAPEUTIC EXERCISES, EACH 15 MINUTES"
    assert b.insurer_paid is None and b.patient_responsibility == Decimal("125.74")


BLOCKS = """Claim line 1
  Date of service: Sep 8, 2025
  Service: 99214 Office visit, established patient,
           moderate complexity
  Provider charged: $323.12
  Plan paid: $95.10
  Your share: $23.78
"""


def test_key_value_blocks():
    r = parse(BLOCKS)
    assert r.format == "key_value"
    (it,) = r.line_items
    assert it.code == "99214"
    assert it.description == "Office visit, established patient, moderate complexity"
    assert it.allowed_amount is None and it.insurer_paid == Decimal("95.10")


def test_duplicates_flagged():
    text = PIPE.replace("TOTALS", "03/14/2025   | 99213 | Office visit, est patient | $250.00 | $140.00 | $112.00 | $28.00\nTOTALS")
    r = parse(text)
    assert r.possible_duplicates == [(0, 2)]
    assert any("duplicate" in w for w in r.warnings)


def test_no_items():
    r = parse("Hello, this is not a bill.")
    assert r.line_items == [] and "no line items found" in r.warnings
