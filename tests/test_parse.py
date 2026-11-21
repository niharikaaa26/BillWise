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


MODCOLS = """DOS       CPT       MOD  DESCRIPTION                      UNITS    CHARGE   INS PMT   INS ADJ   BALANCE
02/17/25 99203     LT   NEW PATIENT OFFICE VISIT             2    235.07    157.23   (60.37)     17.47
02/17/25 99214-25       ANNUAL WELLNESS VISIT,               1    214.67     77.80  (136.87)      0.00
                         SUBSEQUENT
"""


def test_modifier_and_units_columns_kept_out_of_description():
    r = parse(MODCOLS)
    a, b = r.line_items
    assert a.description == "NEW PATIENT OFFICE VISIT"
    assert (a.insurer_paid, a.patient_responsibility) == (Decimal("157.23"), Decimal("17.47"))
    assert b.code == "99214" and b.description == "ANNUAL WELLNESS VISIT, SUBSEQUENT"


def test_duplicate_detected_despite_ocr_in_description():
    text = FIXED.replace("97110   THERAPEUTIC EXERCISES, EACH 15         125.74                            125.74",
                         "97110   THERAPEUTIC EXERCISES, EACH 15         125.74                            125.74\n"
                         "07/19/2025 80053   COMPREHENSIVE METABOLlC PANEL          320.60     230.80      64.16      25.64")
    r = parse(text)
    assert (0, 2) in r.possible_duplicates


TSV_MULTI = (
    "CLAIM 1 OF 2\n"
    "Svc Dt\tProc Cd\tService\tAmt Charged\tPlan Discount\tCovered\tPlan Pymt\tMember Resp\n"
    "03/14/25\t71046\tChest x-ray, 2 views\t$310.00\t$120.00\t$190.00\t$152.00\t$38.00\n"
    "Claim totals\t\t\t\t\t\t\t$38.00\n"
    "\n"
    "CLAIM 2 OF 2\n"
    "Svc Dt\tProc Cd\tService\tAmt Charged\tPlan Discount\tCovered\tPlan Pymt\tMember Resp\n"
    "03/20/25\t36415\tRoutine venipuncture\t$30.00\t$10.00\t—\t$16.00\t$4.00\n"
)


def test_tsv_with_several_tables():
    r = parse(TSV_MULTI)
    assert r.format == "tsv"
    a, b = r.line_items
    assert (a.code, a.billed_amount, a.allowed_amount, a.insurer_paid, a.patient_responsibility) == \
        ("71046", Decimal("310.00"), Decimal("190.00"), Decimal("152.00"), Decimal("38.00"))
    assert b.service_date == date(2025, 3, 20) and b.allowed_amount is None
    assert not any("could not read" in w for w in r.warnings)


def test_unfamiliar_headers_typed_from_content():
    text = ("When|What|Item|Billed|You Owe\n"
            "03/14/2025|99213|Office visit|$250.00|$28.00\n"
            "03/15/2025|J1885|Ketorolac injection|$45.00|$5.00\n")
    r = parse(text)
    assert [i.code for i in r.line_items] == ["99213", "J1885"]
    assert r.line_items[0].service_date == date(2025, 3, 14)


PORTAL = """Item 1 of 2 — 2025-07-09 — CPT 99214
  Office visit, established patient,
  moderate complexity
  Billed $248.23 · Plan allowed — · Plan paid S94.24 · Your cost $23.56

Item 2 of 2 — 2025-07-10 — HCPCS J2405
  Ondansetron injection
  Billed $151.36 · Plan allowed $46.32 · Plan paid $41.69 · Your cost $4.63
Total of your costs: $28.19
Mailing: 12 Oak Ave, Riverton, OH 45011
"""


def test_anchored_fallback_two_line_items_with_inline_amounts():
    r = parse(PORTAL)
    assert r.format == "anchored"
    a, b = r.line_items
    assert (a.service_date, a.code, a.description) == \
        (date(2025, 7, 9), "99214", "Office visit, established patient, moderate complexity")
    assert (a.billed_amount, a.allowed_amount, a.insurer_paid, a.patient_responsibility) == \
        (Decimal("248.23"), None, Decimal("94.24"), Decimal("23.56"))
    assert (b.allowed_amount, b.insurer_paid) == (Decimal("46.32"), Decimal("41.69"))


def test_anchored_arithmetic_maps_unlabeled_amounts():
    text = "Some header we do not know\n06/21/25 81001 Urinalysis, automated 25.41 14.05 9.84 4.21\n"
    (it,) = parse(text).line_items
    # 14.05 = 9.84 + 4.21 -> billed, allowed, paid, patient
    assert (it.billed_amount, it.allowed_amount, it.insurer_paid, it.patient_responsibility) == \
        (Decimal("25.41"), Decimal("14.05"), Decimal("9.84"), Decimal("4.21"))
    assert it.description == "Urinalysis, automated"


def test_anchored_charges_paid_adjust_resp_arithmetic():
    text = "06/21/25 80053 Metabolic panel 320.60 230.80 64.16 25.64\n"
    (it,) = parse(text).line_items
    assert (it.insurer_paid, it.patient_responsibility, it.allowed_amount) == \
        (Decimal("230.80"), Decimal("25.64"), None)


def test_zip_code_is_not_an_anchor():
    assert parse("Mailing: 12 Oak Ave, Riverton, OH 45011\nBalance $20.00\n").line_items == []


def test_fallback_not_used_when_a_table_matches():
    assert parse(PIPE).format == "pipe_table"


def test_spaced_dash_is_missing_not_negative():
    (it,) = parse("06/12/25 70450 CT head 1483.93 - 520.20 57.80\n").line_items
    assert (it.allowed_amount, it.insurer_paid, it.patient_responsibility) == \
        (None, Decimal("520.20"), Decimal("57.80"))


def test_attached_minus_still_negative():
    assert parse_amount("-113.15") == Decimal("-113.15")
