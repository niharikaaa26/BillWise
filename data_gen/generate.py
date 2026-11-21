"""Synthetic medical bill / EOB generator.

Every person, address, phone number, ID and email produced here is FAKE:
  * phone numbers use the reserved fictional 555-01XX range
  * SSN-like strings start with 9 (an area number never issued as an SSN)
  * emails use reserved example.* domains (RFC 2606)
  * names are drawn at random from generic first/last name lists

For each bill the generator writes:
  bill_NNN.txt   the document text
  bill_NNN.json  ground truth: line items, PII spans (exact character offsets),
                 "allowed" spans (business contact info that may be redacted
                 without penalty), and the list of messy conditions injected.

Usage:
    python -m data_gen.generate --out data/synthetic --n 48 --seed 2025
    python -m data_gen.generate --out data/dev2 --n 12 --seed 777 --ocr-p 0.25 --set dev2
    python -m data_gen.generate --out data/heldout --n 12 --seed 4242 --ocr-p 0.2 --set heldout
"""
from __future__ import annotations

import argparse
import json
import random
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

# --------------------------------------------------------------------------
# Vocabulary (deliberately independent from the redactor's own name list)
# --------------------------------------------------------------------------
FIRST_NAMES = [
    "Jane", "Michael", "Maria", "David", "Emily", "James", "Linda", "Robert",
    "Sarah", "Daniel", "Aisha", "Carlos", "Priyanka", "Wei", "Fatima", "Kevin",
    "Olivia", "Marcus", "Hannah", "Luis", "Grace", "Andre", "Thanh", "Siobhan",
    "Oluwaseun", "Xiomara", "Dmitri", "Keisha", "Rafael", "Ingrid", "Tomasz",
    "Naveen", "Brianna", "Elijah", "Yuki", "Patrice", "Desmond", "Lorena",
]
LAST_NAMES = [
    "Doe", "Garcia", "Johnson", "Nguyen", "Patel", "Smith", "Okafor", "Kowalski",
    "Hernandez", "Chen", "Williams", "Brown", "Rossi", "Haddad", "Murphy",
    "Kim", "Abernathy", "Delacroix", "Fitzgerald", "Yamamoto", "Ivanova",
    "Mbeki", "Lindqvist", "Castellanos", "Oyelaran", "Petrov", "Sullivan",
    "Bianchi", "Achterberg", "Ramirez", "Thompson", "Wojcik",
]
PROVIDER_LAST = ["Alvarez", "Brennan", "Cho", "Dubois", "Eriksen", "Farouk", "Gupta"]
PROVIDER_FIRST = ["Alan", "Beth", "Chris", "Dana", "Evan", "Farah", "Gil"]

STREET_NAMES = [
    "Maple", "Oak", "Cedar", "Willow", "Lakeview", "Hillcrest", "Pine", "Elm",
    "Sunset", "Riverbend", "Magnolia", "Chestnut", "Highland", "Briar", "Juniper",
]
STREET_SUFFIX = [("St", "Street"), ("Ave", "Avenue"), ("Rd", "Road"), ("Dr", "Drive"),
                 ("Ln", "Lane"), ("Ct", "Court"), ("Blvd", "Boulevard"), ("Way", "Way")]
CITIES = [
    ("Riverton", "OH", "450"), ("Fairview", "TN", "370"), ("Millbrook", "NY", "125"),
    ("Cedar Falls", "IA", "506"), ("Oak Harbor", "WA", "982"), ("Springdale", "AR", "727"),
    ("Lakewood", "CO", "802"), ("Greenville", "SC", "296"), ("Port Jervis", "NY", "127"),
]
AREA_CODES = ["513", "615", "845", "319", "360", "479", "303", "864", "212", "917"]
EMAIL_DOMAINS = ["example.com", "example.net", "example.org", "mail.example.com"]

HOSPITALS = [
    ("MERCY VALLEY REGIONAL HOSPITAL", "4100 Hospital Dr", "Riverton, OH 45011"),
    ("ST. BRIGID MEDICAL CENTER", "77 Cathedral Ave", "Millbrook, NY 12545"),
    ("HIGH PLAINS COMMUNITY HOSPITAL", "2 Prairie Way", "Lakewood, CO 80226"),
]
INSURERS = [
    ("CLEARPATH HEALTH INSURANCE", "PO Box 4410", "Hartwell, GA 30643"),
    ("BLUERIVER HEALTH PLANS", "PO Box 9902", "Dayton, OH 45401"),
    ("SUMMIT MUTUAL HEALTH", "PO Box 1180", "Boise, ID 83701"),
]
CLINICS = [
    ("Lakeside Family Clinic", "900 Shore Rd", "Oak Harbor, WA 98277", "lakesideclinic.example"),
    ("Northgate Pediatrics", "15 Commerce Blvd", "Fairview, TN 37062", "northgatepeds.example"),
    ("Harbor Imaging Associates", "310 Dock St", "Greenville, SC 29601", "harborimaging.example"),
]

# (code, description, (min_billed, max_billed))
CATALOG = [
    ("99213", "Office visit, established patient, low complexity", (120, 260)),
    ("99214", "Office visit, established patient, moderate complexity", (180, 340)),
    ("99203", "New patient office visit", (160, 300)),
    ("99285", "Emergency department visit, high severity", (1500, 3200)),
    ("99283", "Emergency department visit, moderate severity", (600, 1400)),
    ("80053", "Comprehensive metabolic panel", (90, 360)),
    ("85025", "Complete blood count with automated differential", (40, 180)),
    ("36415", "Routine venipuncture", (15, 60)),
    ("71046", "Chest x-ray, 2 views", (180, 520)),
    ("93000", "Electrocardiogram, routine, with interpretation and report", (90, 300)),
    ("74177", "CT abdomen and pelvis with contrast", (1800, 4800)),
    ("70450", "CT head or brain without contrast", (900, 2600)),
    ("81001", "Urinalysis, automated, with microscopy", (20, 90)),
    ("96374", "IV push, single or initial substance", (150, 420)),
    ("J1885", "Ketorolac tromethamine injection, per 15 mg", (25, 90)),
    ("J2405", "Ondansetron hydrochloride injection, per 1 mg", (20, 80)),
    ("A4649", "Surgical supply, miscellaneous", (30, 250)),
    ("90471", "Immunization administration, single vaccine", (25, 70)),
    ("90686", "Influenza vaccine, quadrivalent, preservative free", (30, 85)),
    ("97110", "Therapeutic exercises, each 15 minutes", (60, 180)),
    ("G0439", "Annual wellness visit, subsequent", (150, 280)),
    ("87880", "Rapid strep test", (25, 75)),
]

TWO = Decimal("0.01")


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(TWO, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------
# Document builder that tracks exact character offsets of PII
# --------------------------------------------------------------------------
class Doc:
    def __init__(self) -> None:
        self.buf: list[str] = []
        self.pos = 0
        self.pii: list[dict] = []
        self.allowed: list[dict] = []

    def add(self, s: str) -> "Doc":
        self.buf.append(s)
        self.pos += len(s)
        return self

    def nl(self, s: str = "") -> "Doc":
        return self.add(s + "\n")

    def pii_(self, s: str, typ: str) -> "Doc":
        self.pii.append({"start": self.pos, "end": self.pos + len(s), "type": typ, "text": s})
        return self.add(s)

    def org(self, s: str, typ: str) -> "Doc":
        self.allowed.append({"start": self.pos, "end": self.pos + len(s), "type": typ, "text": s})
        return self.add(s)

    def text(self) -> str:
        return "".join(self.buf)


# --------------------------------------------------------------------------
# Fake entity factories
# --------------------------------------------------------------------------
def make_person(rng: random.Random, last: str | None = None) -> dict:
    first = rng.choice(FIRST_NAMES)
    last = last or rng.choice(LAST_NAMES)
    middle = rng.choice("ABCDEFGHJKLMNPRSTW") if rng.random() < 0.3 else None
    city, state, zp = rng.choice(CITIES)
    suf = rng.choice(STREET_SUFFIX)
    street = f"{rng.randint(10, 9899)} {rng.choice(STREET_NAMES)} {suf[0] if rng.random() < 0.7 else suf[1]}"
    if rng.random() < 0.3:
        street += f" {rng.choice(['Apt', 'Unit', '#'])} {rng.randint(1, 40)}{rng.choice(['', 'B', 'C'])}".replace("# ", "#")
    area = rng.choice(AREA_CODES)
    line = f"01{rng.randint(0, 99):02d}"
    phone = rng.choice([f"({area}) 555-{line}", f"{area}-555-{line}", f"{area}.555.{line}"])
    dob = date(1940, 1, 1) + timedelta(days=rng.randint(0, 29000))
    return {
        "first": first, "last": last, "middle": middle,
        "street": street, "city": city, "state": state, "zip": f"{zp}{rng.randint(10, 99)}",
        "phone": phone,
        "email": f"{first.lower()}.{last.lower()}{rng.randint(1, 99)}@{rng.choice(EMAIL_DOMAINS)}",
        "dob": dob,
        "ssn": f"9{rng.randint(0, 99):02d}-{rng.randint(10, 99)}-{rng.randint(1000, 9999)}",
    }


def name_fmt(p: dict, style: str) -> str:
    mid = f" {p['middle']}." if p["middle"] else ""
    if style == "last_first":
        return f"{p['last'].upper()}, {p['first'].upper()}"
    if style == "upper":
        return f"{p['first']}{mid} {p['last']}".upper()
    return f"{p['first']}{mid} {p['last']}"


def dob_fmt(d: date, rng: random.Random) -> str:
    return rng.choice([
        d.strftime("%m/%d/%Y"),
        f"{d.month}/{d.day}/{d.strftime('%y')}",
        d.strftime("%B ") + f"{d.day}, {d.year}",
        d.isoformat(),
    ])


def member_id(rng: random.Random) -> str:
    pat = rng.choice(["W#########", "XGH########", "CPH-####-####", "MBR#######", "ZQ#######-01"])
    return "".join(str(rng.randint(0, 9)) if c == "#" else c for c in pat)


def digits(rng: random.Random, n: int) -> str:
    return "".join(str(rng.randint(0, 9)) for _ in range(n))


def biz_phone(rng: random.Random) -> str:
    return f"1-800-555-01{rng.randint(0, 99):02d}"


# --------------------------------------------------------------------------
# OCR-style noise
# --------------------------------------------------------------------------
OCR_LABELS = {
    "Patient Name": "Patlent Name", "Member ID": "Memher ID", "Date of Birth": "Date of Birlh",
    "Account Number": "Acc0unt Number", "Patient": "Patlent", "Subscriber": "Subscrlber",
}


def ocr_amount(s: str, rng: random.Random, decimal_comma: bool = True) -> str:
    """Corrupt one character of a printed amount the way OCR commonly does."""
    choices = []
    for i, ch in enumerate(s):
        if ch == "0":
            choices.append((i, "O"))
        elif ch == "1":
            choices.append((i, "l"))
        elif ch == "$":
            choices.append((i, "S"))
        elif ch == "." and decimal_comma:
            choices.append((i, ","))
    if not choices:
        return s
    i, rep = rng.choice(choices)
    return s[:i] + rep + s[i + 1:]


def ocr_text(s: str, rng: random.Random) -> str:
    subs = [("i", "l"), ("m", "rn"), ("o", "0"), ("e", "c"), ("t", "l")]
    rng.shuffle(subs)
    for a, b in subs:
        idx = [i for i, ch in enumerate(s) if ch == a]
        if idx:
            i = rng.choice(idx)
            return s[:i] + b + s[i + 1:]
    return s


# --------------------------------------------------------------------------
# Line item generation
# --------------------------------------------------------------------------
def make_items(rng: random.Random, start: date, flags: set[str], allow_dup: bool = True) -> list[dict]:
    n = rng.randint(2, 7)
    items = []
    d = start
    for _ in range(n):
        if rng.random() < 0.3:
            d = d + timedelta(days=rng.randint(1, 3))
        code, desc, (lo, hi) = rng.choice(CATALOG)
        billed = money(rng.uniform(lo, hi))
        allowed = money(billed * Decimal(str(rng.uniform(0.3, 0.8))))
        paid = money(allowed * Decimal(str(rng.choice([0.7, 0.8, 0.9, 1.0]))))
        if rng.random() < 0.15:  # deductible not met: plan pays nothing
            paid = money(0)
        items.append({
            "service_date": d, "code": code, "description": desc,
            "billed_amount": billed, "allowed_amount": allowed,
            "insurer_paid": paid, "patient_responsibility": allowed - paid,
        })
    if allow_dup and rng.random() < 0.25:
        i = rng.randrange(len(items))
        items.insert(i + 1, dict(items[i]))
        flags.add("duplicate_charge")
    return items


def gold_item(it: dict) -> dict:
    def s(v):
        return None if v is None else f"{v:.2f}"
    return {
        "service_date": it["service_date"].isoformat() if it["service_date"] else None,
        "code": it["code"],
        "description": " ".join(it["description"].split()),
        "billed_amount": s(it["billed_amount"]),
        "allowed_amount": s(it["allowed_amount"]),
        "insurer_paid": s(it["insurer_paid"]),
        "patient_responsibility": s(it["patient_responsibility"]),
    }


def fmt_amt(v: Decimal | None, dollar: bool = False) -> str:
    if v is None:
        return ""
    return f"{'$' if dollar else ''}{v:,.2f}"


def maybe_ocr_amt(s: str, rng: random.Random, flags: set[str], p: float, decimal_comma: bool = True) -> str:
    if s and rng.random() < p:
        flags.add("ocr_amount")
        return ocr_amount(s, rng, decimal_comma)
    return s


def label(lbl: str, rng: random.Random, flags: set[str], p: float) -> str:
    if lbl in OCR_LABELS and rng.random() < p:
        flags.add("ocr_label")
        return OCR_LABELS[lbl]
    return lbl


def wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        lines.append(cur)
    return lines


# --------------------------------------------------------------------------
# Layout A: hospital itemized statement, fixed-width columns
# --------------------------------------------------------------------------
def layout_hospital(rng: random.Random, ocr_p: float) -> tuple[Doc, list[dict], set[str]]:
    flags: set[str] = set()
    doc = Doc()
    p = make_person(rng)
    hosp, haddr, hcity = rng.choice(HOSPITALS)
    stmt_date = date(2025, 1, 1) + timedelta(days=rng.randint(0, 300))
    svc_start = stmt_date - timedelta(days=rng.randint(10, 40))

    doc.nl(hosp)
    doc.org(haddr, "ADDRESS").add(", ").org(hcity, "ADDRESS").nl()
    doc.add("Billing Questions: ").org(biz_phone(rng), "PHONE").nl()
    doc.nl()
    doc.nl("ITEMIZED STATEMENT")
    doc.nl(f"Statement Date: {stmt_date:%m/%d/%Y}")
    doc.add(f"{label('Account Number', rng, flags, ocr_p)}: ").pii_(digits(rng, rng.choice([8, 10])), "ACCOUNT_ID").nl()
    doc.add(f"{label('Patient Name', rng, flags, ocr_p)}: ").pii_(name_fmt(p, "last_first"), "NAME").nl()
    doc.add(f"{label('Date of Birth', rng, flags, ocr_p)}: ").pii_(dob_fmt(p["dob"], rng), "DOB").nl()
    if rng.random() < 0.4:
        g = make_person(rng, last=p["last"])
        doc.add("Guarantor: ").pii_(name_fmt(g, "title"), "NAME").nl()
    if rng.random() < 0.5:
        flags.add("ssn_present")
        doc.add("SSN: ").pii_(p["ssn"], "SSN").nl()
    doc.nl("Mailing Address:")
    doc.add("  ").pii_(name_fmt(p, "title"), "NAME").nl()
    doc.add("  ").pii_(p["street"], "ADDRESS").nl()
    doc.add("  ").pii_(f"{p['city']}, {p['state']} {p['zip']}", "ADDRESS").nl()
    if rng.random() < 0.8:
        doc.add("Phone: ").pii_(p["phone"], "PHONE").nl()
    else:
        flags.add("missing_phone")
    ins = rng.choice(INSURERS)[0].title()
    doc.add(f"Primary Insurance: {ins}   {label('Member ID', rng, flags, ocr_p)}: ").pii_(member_id(rng), "MEMBER_ID").nl()
    doc.nl()

    items = make_items(rng, svc_start, flags)
    pending = rng.random() < 0.2  # some lines with insurance not yet paid
    for it in items:
        it["allowed_amount"] = None  # hospital statements show adjustments, not allowed amount
    W = (10, 7, 34, 11, 11, 11, 11)
    doc.nl(f"{'DATE':<{W[0]}} {'CODE':<{W[1]}} {'DESCRIPTION':<{W[2]}}{'CHARGES':>{W[3]}}{'INS PAID':>{W[4]}}{'ADJUST':>{W[5]}}{'PT RESP':>{W[6]}}")
    doc.nl("-" * 96)
    prev_date = None
    blank_repeat_dates = rng.random() < 0.4
    total = Decimal(0)
    for idx, it in enumerate(items):
        billed = it["billed_amount"]
        paid = it["insurer_paid"]
        adjust = billed - (paid + it["patient_responsibility"])
        if pending and idx == len(items) - 1:
            flags.add("missing_field")
            it["insurer_paid"] = None
            paid_s, adj_s = "", ""
            it["patient_responsibility"] = billed
        else:
            paid_s, adj_s = fmt_amt(paid), fmt_amt(adjust)
        total += billed
        dstr = f"{it['service_date']:%m/%d/%Y}"
        if blank_repeat_dates and dstr == prev_date:
            flags.add("blank_repeated_date")
            dstr = ""
        prev_date = f"{it['service_date']:%m/%d/%Y}"
        desc = it["description"].upper()
        if rng.random() < ocr_p * 0.5:
            flags.add("ocr_description")
            desc = ocr_text(desc.lower(), rng).upper()
        it["description"] = desc
        parts = wrap(desc, W[2])
        if len(parts) > 1:
            flags.add("multiline_item")
        amts = [maybe_ocr_amt(fmt_amt(billed), rng, flags, ocr_p), maybe_ocr_amt(paid_s, rng, flags, ocr_p),
                adj_s, maybe_ocr_amt(fmt_amt(it["patient_responsibility"]), rng, flags, ocr_p)]
        doc.nl(f"{dstr:<{W[0]}} {it['code']:<{W[1]}} {parts[0]:<{W[2]}}"
               f"{amts[0]:>{W[3]}}{amts[1]:>{W[4]}}{amts[2]:>{W[5]}}{amts[3]:>{W[6]}}")
        for cont in parts[1:]:
            doc.nl(f"{'':<{W[0]}} {'':<{W[1]}} {cont}")
    doc.nl("-" * 96)
    doc.nl(f"{'':<{W[0] + W[1] + 2}}{'TOTAL CHARGES':<{W[2]}}{fmt_amt(total):>{W[3]}}")
    if rng.random() < 0.5:
        pay = money(rng.uniform(20, 200))
        doc.nl(f"{stmt_date:%m/%d/%Y} {'':<{W[1]}} {'PAYMENT - THANK YOU':<{W[2]}}{'-' + fmt_amt(pay):>{W[3]}}")
    doc.nl()
    doc.nl("Please remit the patient balance within 30 days.")
    return doc, items, flags


# --------------------------------------------------------------------------
# Layout B: insurer EOB, pipe-delimited table
# --------------------------------------------------------------------------
def layout_eob_table(rng: random.Random, ocr_p: float) -> tuple[Doc, list[dict], set[str]]:
    flags: set[str] = set()
    doc = Doc()
    p = make_person(rng)
    ins, iaddr, icity = rng.choice(INSURERS)
    prov = f"Dr. {rng.choice(PROVIDER_FIRST)} {rng.choice(PROVIDER_LAST)}"
    proc_date = date(2025, 1, 1) + timedelta(days=rng.randint(0, 300))

    doc.nl(ins)
    doc.org(iaddr, "ADDRESS").add(", ").org(icity, "ADDRESS").nl()
    doc.nl("EXPLANATION OF BENEFITS -- THIS IS NOT A BILL")
    doc.nl(f"Processed: {proc_date:%m/%d/%Y}")
    doc.nl()
    doc.add(f"{label('Patient', rng, flags, ocr_p)}: ").pii_(name_fmt(p, rng.choice(["title", "upper"])), "NAME")
    doc.add(f"     {label('Member ID', rng, flags, ocr_p)}: ").pii_(member_id(rng), "MEMBER_ID").nl()
    if rng.random() < 0.5:
        s = make_person(rng, last=p["last"])
        doc.add(f"{label('Subscriber', rng, flags, ocr_p)}: ").pii_(name_fmt(s, "title"), "NAME").nl()
    doc.add("Patient DOB: ").pii_(dob_fmt(p["dob"], rng), "DOB")
    doc.add(f"     Group #: {digits(rng, 5)}").nl()
    doc.add("Claim Number: ").pii_(digits(rng, 13), "ACCOUNT_ID").nl()
    doc.add("Provider: ").org(prov, "NAME").add(f", MD   NPI {digits(rng, 10)}").nl()
    doc.nl()
    doc.nl("Mail to:")
    doc.pii_(name_fmt(p, "upper"), "NAME").nl()
    doc.pii_(p["street"].upper(), "ADDRESS").nl()
    doc.pii_(f"{p['city'].upper()}, {p['state']} {p['zip']}", "ADDRESS").nl()
    doc.nl()
    if rng.random() < 0.6:
        doc.add("Email on file: ").pii_(p["email"], "EMAIL").nl()
    else:
        flags.add("missing_email")
    doc.add("Questions? Call Member Services at ").org(biz_phone(rng), "PHONE").nl()
    doc.nl()

    items = make_items(rng, proc_date - timedelta(days=rng.randint(15, 60)), flags)
    dollar = rng.random() < 0.6
    hdr = ["Service Date", "Code", "Description", "Billed", "Allowed", "Plan Paid", "You Owe"]
    if rng.random() < 0.5:
        hdr = ["Date of Service", "Procedure", "Service Description", "Amount Billed",
               "Allowed Amount", "Paid by Plan", "Your Responsibility"]
    widths = [15, 9, 46, 13, 14, 12, 19]
    def row(cells):
        return " | ".join(f"{c:<{w}}" for c, w in zip(cells, widths)).rstrip()
    doc.nl(row(hdr))
    doc.nl("-+-".join("-" * w for w in widths))
    for it in items:
        if rng.random() < 0.15:
            flags.add("missing_field")
            it["allowed_amount"] = None
        desc = it["description"]
        if rng.random() < ocr_p * 0.5:
            flags.add("ocr_description")
            desc = ocr_text(desc, rng)
        it["description"] = desc
        parts = wrap(desc, 30) if rng.random() < 0.3 else [desc]
        if len(parts) > 1:
            flags.add("multiline_item")
        cells = [
            f"{it['service_date']:%m/%d/%Y}", it["code"], parts[0],
            maybe_ocr_amt(fmt_amt(it["billed_amount"], dollar), rng, flags, ocr_p),
            fmt_amt(it["allowed_amount"], dollar) or "--",
            maybe_ocr_amt(fmt_amt(it["insurer_paid"], dollar), rng, flags, ocr_p),
            maybe_ocr_amt(fmt_amt(it["patient_responsibility"], dollar), rng, flags, ocr_p),
        ]
        doc.nl(row(cells))
        for cont in parts[1:]:
            doc.nl(row(["", "", cont, "", "", "", ""]))
    tot = lambda k: sum((i[k] or Decimal(0)) for i in items)
    doc.nl("-+-".join("-" * w for w in widths))
    doc.nl(row(["TOTALS", "", "", fmt_amt(tot("billed_amount"), dollar), fmt_amt(tot("allowed_amount"), dollar),
                fmt_amt(tot("insurer_paid"), dollar), fmt_amt(tot("patient_responsibility"), dollar)]))
    doc.nl()
    doc.nl("The amount you owe may be billed to you separately by your provider.")
    return doc, items, flags


# --------------------------------------------------------------------------
# Layout C: claim summary, key/value blocks
# --------------------------------------------------------------------------
def layout_blocks(rng: random.Random, ocr_p: float) -> tuple[Doc, list[dict], set[str]]:
    flags: set[str] = set()
    doc = Doc()
    p = make_person(rng)
    ins = rng.choice(INSURERS)[0].title()
    doc.nl(f"{ins} -- Your Claim Summary")
    doc.nl("=" * 48)
    doc.add("Prepared for: ").pii_(name_fmt(p, rng.choice(["upper", "title"])), "NAME").nl()
    doc.add(f"{label('Member ID', rng, flags, ocr_p)}: ").pii_(member_id(rng), "MEMBER_ID")
    if rng.random() < 0.5:
        flags.add("ssn_present")
        doc.add("   SSN: ").pii_(p["ssn"], "SSN")
    doc.nl()
    doc.add("Born: ").pii_(dob_fmt(p["dob"], rng), "DOB").nl()
    doc.add("Address: ").pii_(f"{p['street']}, {p['city']}, {p['state']} {p['zip']}", "ADDRESS").nl()
    contact = []
    if rng.random() < 0.7:
        contact.append(("EMAIL", p["email"]))
    if rng.random() < 0.7:
        contact.append(("PHONE", p["phone"]))
    if len(contact) < 2:
        flags.add("missing_contact")
    if contact:
        doc.add("Contact: ")
        for k, (typ, val) in enumerate(contact):
            if k:
                doc.add(" / ")
            doc.pii_(val, typ)
        doc.nl()
    doc.add("Reference #: ").pii_(f"CLM-{digits(rng, 9)}", "ACCOUNT_ID").nl()
    doc.nl()

    items = make_items(rng, date(2025, 1, 1) + timedelta(days=rng.randint(0, 280)), flags)
    long_dates = rng.random() < 0.5
    dollar = rng.random() < 0.8
    for n, it in enumerate(items, 1):
        doc.nl(f"Claim line {n}")
        d = it["service_date"]
        dstr = (d.strftime("%b ") + f"{d.day}, {d.year}") if long_dates else f"{d:%m/%d/%Y}"
        doc.nl(f"  Date of service: {dstr}")
        desc = it["description"]
        if rng.random() < ocr_p * 0.5:
            flags.add("ocr_description")
            desc = ocr_text(desc, rng)
        it["description"] = desc
        svc = f"{it['code']} {desc}"
        if len(svc) > 44 and rng.random() < 0.6:
            flags.add("multiline_item")
            parts = wrap(svc, 44)
            doc.nl(f"  Service: {parts[0]}")
            for cont in parts[1:]:
                doc.nl(f"           {cont}")
        else:
            doc.nl(f"  Service: {svc}")
        doc.nl(f"  Provider charged: {maybe_ocr_amt(fmt_amt(it['billed_amount'], dollar), rng, flags, ocr_p)}")
        if rng.random() < 0.15:
            flags.add("missing_field")
            it["allowed_amount"] = None
        else:
            doc.nl(f"  Plan allowed: {fmt_amt(it['allowed_amount'], dollar)}")
        doc.nl(f"  Plan paid: {maybe_ocr_amt(fmt_amt(it['insurer_paid'], dollar), rng, flags, ocr_p)}")
        doc.nl(f"  Your share: {maybe_ocr_amt(fmt_amt(it['patient_responsibility'], dollar), rng, flags, ocr_p)}")
        doc.nl()
    doc.nl("This summary is for your records.")
    return doc, items, flags


# --------------------------------------------------------------------------
# Layout D: clinic letter with CSV statement
# --------------------------------------------------------------------------
def layout_clinic_csv(rng: random.Random, ocr_p: float) -> tuple[Doc, list[dict], set[str]]:
    flags: set[str] = set()
    doc = Doc()
    p = make_person(rng)
    clinic, caddr, ccity, cdomain = rng.choice(CLINICS)
    doc.nl(clinic)
    doc.org(caddr, "ADDRESS").nl()
    doc.org(ccity, "ADDRESS").nl()
    doc.nl()
    # window-envelope address block: name is NOT behind a label
    flags.add("unlabeled_name")
    doc.pii_(name_fmt(p, "upper"), "NAME").nl()
    doc.pii_(p["street"].upper(), "ADDRESS").nl()
    doc.pii_(f"{p['city'].upper()} {p['state']} {p['zip']}", "ADDRESS").nl()
    doc.nl()
    if rng.random() < 0.5:
        doc.add("Dear ").pii_(f"{p['first']} {p['last']}", "NAME").nl(",")
    else:
        doc.add("Dear ").pii_(p["first"], "NAME").nl(",")
    doc.add("Our records show a balance on the account for ").pii_(p["first"], "NAME")
    doc.nl(". Please review the charges below.")
    doc.add("Questions? Call ").org(f"({rng.choice(AREA_CODES)}) 555-01{rng.randint(0, 99):02d}", "PHONE")
    doc.add(" or email ").org(f"billing@{cdomain}", "EMAIL").nl(".")
    doc.nl()
    if rng.random() < 0.5:
        doc.add(f"{label('Patient', rng, flags, ocr_p)}: ").pii_(name_fmt(p, "title"), "NAME").nl()
    doc.add("Patient Acct: ").pii_(digits(rng, 7), "ACCOUNT_ID")
    doc.add("   DOB ").pii_(dob_fmt(p["dob"], rng), "DOB").nl()
    if rng.random() < 0.5:
        doc.add("Phone on file: ").pii_(p["phone"], "PHONE").nl()
    if rng.random() < 0.3:
        flags.add("ssn_present")
        doc.add("Guarantor SSN: ").pii_(p["ssn"], "SSN").nl()
    doc.nl()

    items = make_items(rng, date(2025, 1, 1) + timedelta(days=rng.randint(0, 280)), flags)
    with_allowed = rng.random() < 0.4
    hdr = ["Svc Date", "CPT/HCPCS", "Description", "Charge"] + (["Allowed"] if with_allowed else []) + ["Ins Paid", "Your Balance"]
    doc.nl(",".join(hdr))
    for it in items:
        if not with_allowed:
            it["allowed_amount"] = None
        code = it["code"]
        if rng.random() < 0.1:
            flags.add("missing_code")
            it["code"] = None
            code = ""
        desc = it["description"]
        if rng.random() < ocr_p * 0.5:
            flags.add("ocr_description")
            desc = ocr_text(desc, rng)
        it["description"] = desc
        printed_desc = desc
        if len(desc) > 40 and rng.random() < 0.3:
            flags.add("multiline_item")
            parts = wrap(desc, 40)
            printed_desc = "\n".join(parts)
        if "," in printed_desc or "\n" in printed_desc:
            printed_desc = '"' + printed_desc + '"'
        amts = [maybe_ocr_amt(f"{it['billed_amount']:.2f}", rng, flags, ocr_p, False)]
        if with_allowed:
            amts.append(f"{it['allowed_amount']:.2f}")
        amts += [maybe_ocr_amt(f"{it['insurer_paid']:.2f}", rng, flags, ocr_p, False),
                 maybe_ocr_amt(f"{it['patient_responsibility']:.2f}", rng, flags, ocr_p, False)]
        doc.nl(",".join([f"{it['service_date']:%m/%d/%Y}", code, printed_desc] + amts))
    doc.nl()
    bal = sum(i["patient_responsibility"] for i in items)
    doc.nl(f"Total balance due: ${bal:,.2f}")
    return doc, items, flags


# --------------------------------------------------------------------------
# Layout E (HELD OUT): physician-group statement. Written after the parser and
# redactor were built and never used to tune them. Different header vocabulary,
# modifier and units columns, 2-digit-year dates, parenthesized negatives,
# page break with repeated header, space-separated phone, "Last, First M" name.
# --------------------------------------------------------------------------
GROUPS = [
    ("CASCADE ORTHOPEDIC PARTNERS, PLLC", "PO Box 2231", "Tacoma, WA 98401", "253"),
    ("RED MESA CARDIOLOGY GROUP", "PO Box 870", "Santa Fe, NM 87501", "505"),
]


def layout_group_statement(rng: random.Random, ocr_p: float) -> tuple[Doc, list[dict], set[str]]:
    flags: set[str] = set()
    doc = Doc()
    p = make_person(rng)
    grp, gbox, gcity, garea = rng.choice(GROUPS)
    doc.nl(grp)
    doc.nl(f"Tax ID {digits(rng, 2)}-{digits(rng, 7)}")
    doc.add("Remit to: ").org(gbox, "ADDRESS").add(", ").org(gcity, "ADDRESS").nl()
    doc.add("Phone ").org(f"{garea}.555.01{rng.randint(0, 99):02d}", "PHONE")
    doc.add(" | Fax ").org(f"{garea}.555.01{rng.randint(0, 99):02d}", "PHONE").nl()
    doc.nl()
    mid = f" {p['middle']}" if p["middle"] else ""
    doc.add("STATEMENT FOR: ").pii_(f"{p['last']}, {p['first']}{mid}", "NAME").nl()
    doc.add("Guarantor #: ").pii_(digits(rng, 7), "ACCOUNT_ID").nl()
    doc.add("Insured's ID No.: ").pii_(member_id(rng), "MEMBER_ID").nl()
    doc.add("Birthdate: ").pii_(dob_fmt(p["dob"], rng), "DOB").nl()
    doc.add("Home: ").pii_(f"{p['street']}, {p['city']}, {p['state']} {p['zip']}", "ADDRESS").nl()
    area = rng.choice(AREA_CODES)
    doc.add("Cell: ").pii_(f"+1 {area} 555 01{rng.randint(0, 99):02d}", "PHONE").nl()
    if rng.random() < 0.5:
        doc.add("E-mail: ").pii_(p["email"], "EMAIL").nl()
    doc.add("Referring physician: ").org(f"Dr. {rng.choice(PROVIDER_FIRST)} {rng.choice(PROVIDER_LAST)}", "NAME").nl()
    doc.add("Rendering: ").org(f"{rng.choice(['Jordan', 'Morgan', 'Casey'])} {rng.choice(PROVIDER_LAST)}", "NAME").nl(", PA-C")
    doc.nl()

    items = make_items(rng, date(2025, 1, 1) + timedelta(days=rng.randint(0, 280)), flags)
    W = (9, 9, 4, 32, 6, 10, 10, 10, 10)
    hdr = f"{'DOS':<{W[0]}} {'CPT':<{W[1]}} {'MOD':<{W[2]}} {'DESCRIPTION':<{W[3]}}{'UNITS':>{W[4]}}{'CHARGE':>{W[5]}}{'INS PMT':>{W[6]}}{'INS ADJ':>{W[7]}}{'BALANCE':>{W[8]}}"
    doc.nl(hdr)
    page_break_at = rng.randint(1, len(items) - 1) if len(items) > 3 and rng.random() < 0.5 else None
    for idx, it in enumerate(items):
        if idx == page_break_at:
            flags.add("page_break")
            doc.nl()
            doc.nl("--- continued on next page ---")
            doc.nl("\f")
            doc.nl(f"{grp}   Page 2")
            doc.nl(hdr)
        it["allowed_amount"] = None
        adj = it["billed_amount"] - it["insurer_paid"] - it["patient_responsibility"]
        mod = rng.choice(["", "", "", "25", "59", "RT", "LT"])
        if mod:
            flags.add("modifier")
        desc = it["description"].upper()
        if rng.random() < ocr_p * 0.5:
            flags.add("ocr_description")
            desc = ocr_text(desc.lower(), rng).upper()
        it["description"] = desc
        parts = wrap(desc, W[3])
        if len(parts) > 1:
            flags.add("multiline_item")
        units = str(rng.choice([1, 1, 1, 2]))
        charge = fmt_amt(it["billed_amount"])
        if it["billed_amount"] == it["billed_amount"].to_integral_value() or rng.random() < 0.15:
            flags.add("no_cents")
            charge = f"{it['billed_amount']:,.0f}" if it["billed_amount"] == it["billed_amount"].to_integral_value() else charge
        adj_s = f"({adj:,.2f})" if adj else "0.00"
        doc.nl(f"{it['service_date']:%m/%d/%y} {it['code']:<{W[1]}} {mod:<{W[2]}} {parts[0]:<{W[3]}}{units:>{W[4]}}"
               f"{maybe_ocr_amt(charge, rng, flags, ocr_p):>{W[5]}}"
               f"{maybe_ocr_amt(fmt_amt(it['insurer_paid']), rng, flags, ocr_p):>{W[6]}}"
               f"{adj_s:>{W[7]}}"
               f"{maybe_ocr_amt(fmt_amt(it['patient_responsibility']), rng, flags, ocr_p):>{W[8]}}")
        for cont in parts[1:]:
            doc.nl(f"{'':<{W[0]}} {'':<{W[1]}} {'':<{W[2]}} {cont}")
    bal = sum(i["patient_responsibility"] for i in items)
    doc.nl()
    doc.nl(f"{'AMOUNT DUE FROM PATIENT':>{sum(W[:5]) + 4}}{fmt_amt(bal):>{W[5]}}")
    return doc, items, flags


LAYOUTS = {
    "hospital_fixed": layout_hospital,
    "eob_pipe": layout_eob_table,
    "claim_blocks": layout_blocks,
    "clinic_csv": layout_clinic_csv,
}
# --------------------------------------------------------------------------
# Layout F (HELD OUT, round 2): multi-claim benefits statement from a third-
# party administrator. Written BEFORE the round-2 parser/redactor fixes and not
# used to tune them. Several tab-separated tables (one per claim), dot-leader
# labels, "Last First M" names with no comma, dash-format DOB, phone with no
# space after the area code, upper-case email, em-dash for missing values.
# --------------------------------------------------------------------------
TPAS = [
    ("NORTHWIND BENEFIT ADMINISTRATORS", "PO Box 6100", "Fargo, ND 58108"),
    ("KESTREL HEALTH PLAN SERVICES", "PO Box 33", "Omaha, NE 68101"),
]
FACILITIES = ["Harbor Imaging Associates", "Lakeside Family Clinic", "Mercy Valley Regional Hospital",
              "Northgate Pediatrics", "Riverside Urgent Care"]


def layout_multi_claim(rng: random.Random, ocr_p: float) -> tuple[Doc, list[dict], set[str]]:
    flags: set[str] = {"multi_table", "tab_separated"}
    doc = Doc()
    p = make_person(rng)
    tpa, tbox, tcity = rng.choice(TPAS)
    start = date(2025, 1, 1) + timedelta(days=rng.randint(0, 200))
    doc.nl(tpa)
    doc.nl("Member Statement of Benefits" + " " * 24 + "Page 1")
    doc.nl(f"Statement period: {start:%m/%d/%Y} - {start + timedelta(days=90):%m/%d/%Y}")
    doc.add("Send correspondence to ").org(tbox, "ADDRESS").add(", ").org(tcity, "ADDRESS").nl()
    doc.add("Customer care ").org(biz_phone(rng), "PHONE").nl("  (TTY 711)")
    doc.nl()
    mid = f" {p['middle']}" if p["middle"] else ""
    doc.add("Member .......... ").pii_(f"{p['last']} {p['first']}{mid}", "NAME").nl()
    mid_id = f"{digits(rng, 4)}-{digits(rng, 4)}-{digits(rng, 4)}"
    doc.add("Member # ........ ").pii_(mid_id, "MEMBER_ID").nl()
    d = p["dob"]
    doc.add("Date of birth ... ").pii_(f"{d:%m-%d-%Y}", "DOB").nl()
    doc.add("Residence ....... ").pii_(f"{p['street']}, {p['city']} {p['state']} {p['zip']}", "ADDRESS").nl()
    area = rng.choice(AREA_CODES)
    doc.add("Daytime tel ..... ").pii_(f"({area})555-01{rng.randint(0, 99):02d}", "PHONE").nl()
    if rng.random() < 0.6:
        doc.add("E-mail .......... ").pii_(p["email"].upper(), "EMAIL").nl()
    doc.nl()

    n_claims = rng.randint(2, 3)
    all_items: list[dict] = []
    dep = make_person(rng, last=p["last"]) if rng.random() < 0.5 else None
    hdr = ["Svc Dt", "Proc Cd", "Service", "Amt Charged", "Plan Discount", "Covered",
           "Deductible", "Coinsurance", "Plan Pymt", "Member Resp"]
    for c in range(n_claims):
        doc.add(f"CLAIM {c + 1} OF {n_claims}   Claim ID: ").pii_(f"{digits(rng, 2)}-{digits(rng, 4)}-{digits(rng, 4)}-00", "ACCOUNT_ID").nl()
        who = dep if (dep and rng.random() < 0.5) else p
        doc.add("Patient: ").pii_(f"{who['first']} {who['last']}", "NAME").nl()
        doc.nl(f"Provider: {rng.choice(FACILITIES)}")
        doc.nl("\t".join(hdr))
        items = make_items(rng, start + timedelta(days=rng.randint(0, 80)), flags)
        items = items[: rng.randint(1, 3)] if len(items) > 3 else items
        for it in items:
            covered = it["allowed_amount"]
            discount = it["billed_amount"] - covered
            resp = it["patient_responsibility"]
            ded = money(resp * Decimal(str(rng.choice([0, 0, 0.5, 1]))))
            coins = resp - ded
            missing_cov = rng.random() < 0.1
            if missing_cov:
                flags.add("missing_field")
                it["allowed_amount"] = None
            cells = [f"{it['service_date']:%m/%d/%y}", it["code"], it["description"],
                     maybe_ocr_amt(fmt_amt(it["billed_amount"], True), rng, flags, ocr_p),
                     fmt_amt(discount, True),
                     "—" if missing_cov else fmt_amt(covered, True),
                     fmt_amt(ded, True), fmt_amt(coins, True),
                     maybe_ocr_amt(fmt_amt(it["insurer_paid"], True), rng, flags, ocr_p),
                     maybe_ocr_amt(fmt_amt(resp, True), rng, flags, ocr_p)]
            doc.nl("\t".join(cells))
        tot = sum(i["patient_responsibility"] for i in items)
        doc.nl(f"Claim totals\t\t\t\t\t\t\t\t\t{fmt_amt(tot, True)}")
        doc.nl()
        all_items.extend(items)
    doc.nl("This is not a bill. Keep this statement for your records.")
    return doc, all_items, flags


LAYOUT_SETS = {
    "dev": LAYOUTS,
    # round-1 held-out layout; it was used to tune round-2 fixes, so it is now development data
    "dev2": {"group_statement": layout_group_statement},
    # round-2 held-out layout: never used for tuning
    "heldout": {"multi_claim_heldout": layout_multi_claim},
}


def duplicate_pairs(items: list[dict]) -> list[list[int]]:
    pairs = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            if (a["service_date"], a["code"], a["billed_amount"]) == (b["service_date"], b["code"], b["billed_amount"]):
                pairs.append([i, j])
    return pairs


def generate(out: Path, n: int, seed: int, ocr_p: float = 0.12, layout_set: str = "dev") -> list[Path]:
    rng = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    table = LAYOUT_SETS[layout_set]
    names = list(table)
    written = []
    for k in range(n):
        layout = names[k % len(names)]
        doc, items, flags = table[layout](rng, ocr_p)
        text = doc.text()
        for s in doc.pii + doc.allowed:  # self-check: offsets must be exact
            assert text[s["start"]:s["end"]] == s["text"], s
        stem = f"bill_{k + 1:03d}"
        (out / f"{stem}.txt").write_text(text, encoding="utf-8", newline="\n")
        truth = {
            "id": stem,
            "layout": layout,
            "messy": sorted(flags),
            "pii": doc.pii,
            "allowed_redactions": doc.allowed,
            "line_items": [gold_item(i) for i in items],
            "duplicate_pairs": duplicate_pairs(items),
        }
        (out / f"{stem}.json").write_text(json.dumps(truth, indent=2) + "\n", encoding="utf-8", newline="\n")
        written.append(out / f"{stem}.txt")
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path("data/synthetic"))
    ap.add_argument("--n", type=int, default=48)
    ap.add_argument("--seed", type=int, default=2025)
    ap.add_argument("--ocr-p", type=float, default=0.12, help="probability of each OCR corruption site")
    ap.add_argument("--set", dest="layout_set", choices=sorted(LAYOUT_SETS), default="dev",
                    help="dev: 4 development layouts; dev2: round-1 held-out layout (now dev data); "
                         "heldout: layout never used for tuning")
    args = ap.parse_args()
    files = generate(args.out, args.n, args.seed, args.ocr_p, args.layout_set)
    print(f"wrote {len(files)} synthetic bills + ground truth to {args.out}")


if __name__ == "__main__":
    main()
