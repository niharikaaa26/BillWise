"""Heuristic line-item extraction for text-based medical bills and EOBs.

The parser does not know which synthetic layout produced a document. It tries
several generic strategies and keeps the one that yields the most line items:

  * pipe_table  - rows separated by "|" under a header row (several tables allowed)
  * csv / tsv   - comma- or tab-separated rows under header rows (quoted multi-line ok)
  * fixed_width - whitespace-aligned columns; amounts assigned by column position
  * key_value   - "Date of service: ..." / "Plan paid: ..." blocks

Column meaning comes from header keywords (e.g. "Plan Paid", "You Owe").
Common OCR confusions in numeric context (O->0, l/I->1, S->$, decimal comma)
are repaired for amounts and billing codes, never for descriptions.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .models import LineItem, ParseResult

# ---------------------------------------------------------------------------
# Header keyword -> field. Checked in order; first match wins.
# ---------------------------------------------------------------------------
_HEADER_RULES: list[tuple[str | None, tuple[str, ...]]] = [
    (None, ("adjust", "adj", "discount", "deductible", "copay", "coinsurance", "not covered")),
    # non-amount columns that must be kept out of descriptions
    ("_modifier", ("modifier", "mod")),
    ("_units", ("units", "unit", "qty", "quantity")),
    ("patient_responsibility", ("your responsibility", "you owe", "your share", "pt resp", "patient resp",
                                "your balance", "patient balance", "amount you owe", "patient owes", "you pay",
                                "member resp", "member responsibility", "balance")),
    ("insurer_paid", ("paid by plan", "plan paid", "ins paid", "insurance paid", "plan pays", "insurer paid",
                      "ins pmt", "insurance pmt", "plan pmt", "pmt", "pymt", "payment", "paid")),
    ("allowed_amount", ("allowed", "covered")),
    ("billed_amount", ("amount billed", "billed", "charges", "charge", "provider charged", "amount charged")),
    ("service_date", ("date of service", "service date", "svc date", "svc dt", "dos", "date", "dt")),
    ("code", ("procedure", "cpt", "hcpcs", "code")),
    ("description", ("description", "service")),
]
AMOUNT_FIELDS = ("billed_amount", "allowed_amount", "insurer_paid", "patient_responsibility")
NON_DESC_COLUMNS = ("code", "_modifier", "_units")
SKIP_ROW = re.compile(r"\b(total|totals|payment|thank you|balance due|adjustment)\b", re.IGNORECASE)


def _has_key(text: str, key: str) -> bool:
    # short keys ("mod", "dos", "pmt", "adj") must be whole words; longer ones may be substrings
    return bool(re.search(rf"\b{re.escape(key)}\b", text)) if len(key) <= 4 else key in text


def classify_header(cell: str) -> str | None:
    c = " ".join(cell.lower().split())
    if not c:
        return None
    for fld, keys in _HEADER_RULES:
        if any(_has_key(c, k) for k in keys):
            return fld if fld else "_ignore"
    return None


# ---------------------------------------------------------------------------
# Value normalizers
# ---------------------------------------------------------------------------
MISSING_VALUES = {"--", "-", "—", "–", "N/A", "n/a"}  # includes em dash and en dash
_OCR_DIGIT = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1"})
AMOUNT_RE = re.compile(r"(?<![\w.,])-?[$S]?\s?[0-9OolI][0-9OolI,]*[.,][0-9OolI]{2}(?![\w])")


def parse_amount(tok: str | None) -> Decimal | None:
    if tok is None:
        return None
    t = tok.strip().replace(" ", "")
    if not t or t in MISSING_VALUES:
        return None
    neg = t.startswith("-") or (t.startswith("(") and t.endswith(")"))
    t = t.strip("-()")
    if t[:1] in "$S":
        t = t[1:]
    t = t.translate(_OCR_DIGIT)
    if re.fullmatch(r"\d{1,3}(,\d{3})*,\d{2}", t) or re.fullmatch(r"\d+,\d{2}", t):
        # decimal comma (OCR misread of '.'): last comma is the decimal point
        head, cents = t.rsplit(",", 1)
        t = head.replace(",", "") + "." + cents
    t = t.replace(",", "")
    if not re.fullmatch(r"\d+(\.\d{1,2})?", t):
        return None
    try:
        v = Decimal(t).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    return -v if neg else v


_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%m-%d-%Y")
DATE_RE = re.compile(
    r"\b(?:\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}-\d{1,2}-\d{4}|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.? \d{1,2},? \d{4})\b"
)


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    s = " ".join(s.strip().replace("Sept", "Sep").split())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


CODE_RE = re.compile(r"^(?:\d{5}|[A-V]\d{4})$")


def parse_code(tok: str | None) -> str | None:
    """Return a CPT/HCPCS code, repairing O/l/I misreads in digit positions."""
    if not tok:
        return None
    t = tok.strip()
    m = re.fullmatch(r"(\S{5})-[A-Z0-9]{2}", t)  # code with a modifier attached, e.g. 99214-25
    if m:
        t = m.group(1)
    t = t.upper() if t[:1].isalpha() and t[1:].isdigit() else t
    if CODE_RE.match(t):
        return t
    if len(t) == 5:
        head, tail = t[0], t[1:].translate(_OCR_DIGIT)
        cand = head.translate(_OCR_DIGIT) + tail if head in "OolI" else head.upper() + tail
        if CODE_RE.match(cand):
            return cand
    return None


def _clean_desc(s: str) -> str:
    return " ".join(s.split())


def _finish(items: list[dict]) -> list[LineItem]:
    out = []
    for it in items:
        it["description"] = _clean_desc(it.get("description", ""))
        out.append(LineItem(**it))
    return out


# ---------------------------------------------------------------------------
# Strategy: delimited tables (pipe, comma, tab); any number of tables per document
# ---------------------------------------------------------------------------
def _parse_delimited_rows(rows: list[list[str]], cols: list[str | None], warnings: list[str]) -> list[dict]:
    items: list[dict] = []
    for cells in rows:
        cells = [c.strip() for c in cells]
        if len(cells) != len(cols):
            if any(cells):
                warnings.append(f"skipped row with {len(cells)} cells (expected {len(cols)}): {','.join(cells)[:60]}")
            continue
        rec = dict(zip(cols, cells))
        nonempty = {k: v for k, v in rec.items() if v and k}
        if not nonempty:
            continue
        if set(nonempty) == {"description"} and items:  # wrapped description
            items[-1]["description"] += " " + nonempty["description"]
            continue
        if SKIP_ROW.search(" ".join(cells)) and not parse_code(rec.get("code")):
            continue
        d = parse_date(rec.get("service_date"))
        code = parse_code(rec.get("code"))
        if d is None and code is None:
            continue
        item = {"service_date": d, "code": code, "description": rec.get("description", "")}
        for f in AMOUNT_FIELDS:
            if f in rec:
                item[f] = parse_amount(rec[f])
                if rec[f] and item[f] is None and rec[f] not in MISSING_VALUES:
                    warnings.append(f"could not read {f} '{rec[f]}'")
        items.append(item)
    return items


def _split_rows(lines: list[str], delim: str) -> list[list[str]]:
    if delim == "|":
        return [ln.split("|") for ln in lines]
    return list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delim))


def _infer_columns(cols: list[str | None], rows: list[list[str]]) -> list[str | None]:
    """Type columns whose header is unfamiliar from their contents (dates, codes)."""
    out = list(cols)
    for j, c in enumerate(cols):
        if c is not None:
            continue
        vals = [r[j].strip() for r in rows if len(r) == len(cols) and r[j].strip()]
        if not vals:
            continue
        if "service_date" not in out and sum(parse_date(v) is not None for v in vals) >= 0.8 * len(vals):
            out[j] = "service_date"
        elif "code" not in out and sum(parse_code(v) is not None for v in vals) >= 0.8 * len(vals):
            out[j] = "code"
    return out


def _looks_like_header(line: str, delim: str, min_known: int) -> bool:
    if line.count(delim) < 3 or DATE_RE.search(line) or AMOUNT_RE.search(line):
        return False
    cells = _split_rows([line], delim)[0]
    return sum(classify_header(c) not in (None, "_ignore") for c in cells) >= min_known


def parse_delimited(text: str, delim: str, warnings: list[str]) -> list[dict]:
    lines = text.splitlines()
    items: list[dict] = []
    i = 0
    while i < len(lines):
        if not _looks_like_header(lines[i], delim, 1):
            i += 1
            continue
        header = _split_rows([lines[i]], delim)[0]
        body: list[str] = []
        j = i + 1
        while j < len(lines):
            ln = lines[j]
            if _looks_like_header(ln, delim, 3):
                break  # next table
            if delim == "|":
                if "|" in ln:  # pipe tables may contain separator lines; keep only rows
                    body.append(ln)
            elif not ln.strip():
                break
            else:
                body.append(ln)
            j += 1
        rows = _split_rows(body, delim)
        cols = _infer_columns([classify_header(c) for c in header], rows)
        if "service_date" in cols and any(c in AMOUNT_FIELDS for c in cols):
            items.extend(_parse_delimited_rows(rows, cols, warnings))
            i = j
        else:
            i += 1
    return items


def parse_pipe_table(text: str, warnings: list[str]) -> list[dict]:
    return parse_delimited(text, "|", warnings)


def parse_csv(text: str, warnings: list[str]) -> list[dict]:
    return parse_delimited(text, ",", warnings)


def parse_tsv(text: str, warnings: list[str]) -> list[dict]:
    return parse_delimited(text, "\t", warnings)


# ---------------------------------------------------------------------------
# Strategy: fixed-width columns
# ---------------------------------------------------------------------------
def parse_fixed_width(text: str, warnings: list[str]) -> list[dict]:
    lines = text.splitlines()
    for hi, header in enumerate(lines):
        if "|" in header or header.count(",") >= 3:
            continue
        segs = [(m.start(), m.end(), classify_header(m.group(0))) for m in re.finditer(r"\S+(?: \S+)*", header)]
        fields = [s[2] for s in segs]
        if not ("service_date" in fields and any(f in AMOUNT_FIELDS for f in fields)
                and ("code" in fields or "description" in fields)):
            continue
        amount_cols = [(e, f) for s, e, f in segs if f in AMOUNT_FIELDS or f == "_ignore"]
        desc_start = next((s for s, e, f in segs if f == "description"), None)
        items: list[dict] = []
        prev_date: date | None = None
        last_was_item = False
        for line in lines[hi + 1:]:
            if not line.strip() or re.fullmatch(r"[\s\-=_]+", line):
                last_was_item = last_was_item and bool(line.strip())
                continue
            amts = [m for m in AMOUNT_RE.finditer(line)
                    if desc_start is None or m.start() > desc_start + 5]
            dm = DATE_RE.match(line)
            rest_start = dm.end() if dm else 0
            tok = re.match(r"\s*(\S+)", line[rest_start:])
            code = parse_code(tok.group(1)) if tok else None
            if not amts:
                # continuation of a wrapped description
                if items and last_was_item and not dm and line.strip() and not SKIP_ROW.search(line):
                    items[-1]["description"] += " " + line.strip()
                continue
            last_was_item = False
            desc_from = rest_start + (tok.end() if (tok and code) else 0)
            # keep only words that do not sit under a code / modifier / units column header
            desc = " ".join(
                w.group(0) for w in re.finditer(r"\S+", line[:amts[0].start()])
                if w.start() >= desc_from and not any(
                    f in NON_DESC_COLUMNS and w.start() < e and s < w.end() for s, e, f in segs)
            )
            if code is None and (SKIP_ROW.search(desc) or not dm):
                continue
            d = parse_date(dm.group(0)) if dm else None
            if d is None and prev_date is not None:
                d = prev_date
                warnings.append(f"inherited service date for '{desc[:30]}'")
            item: dict = {"service_date": d, "code": code, "description": desc}
            for m in amts:
                end = m.end()
                col = min(amount_cols, key=lambda c: abs(c[0] - end))
                if abs(col[0] - end) <= 6 and col[1] in AMOUNT_FIELDS:
                    item[col[1]] = parse_amount(m.group(0))
            items.append(item)
            prev_date = d
            last_was_item = True
        if items:
            return items
    return []


# ---------------------------------------------------------------------------
# Strategy: key/value blocks
# ---------------------------------------------------------------------------
KV_RE = re.compile(r"^\s*(?P<k>[A-Za-z][A-Za-z '#/]{1,40}?)\s*:\s*(?P<v>.*)$")
BLOCK_START = re.compile(r"^\s*(?:claim )?(?:line|item|service line)\s*#?\s*\d+\s*$", re.IGNORECASE)


def parse_key_value(text: str, warnings: list[str]) -> list[dict]:
    items: list[dict] = []
    cur: dict | None = None
    last_key: str | None = None

    def flush():
        nonlocal cur
        if cur and (cur.get("service_date") or cur.get("billed_amount") is not None):
            items.append(cur)
        cur = None

    for line in text.splitlines():
        if BLOCK_START.match(line):
            flush()
            cur = {"description": ""}
            last_key = None
            continue
        m = KV_RE.match(line)
        if not m:
            if cur is not None and last_key == "description" and line.startswith(" ") and line.strip():
                cur["description"] += " " + line.strip()
            elif not line.strip():
                last_key = None
            continue
        fld = classify_header(m.group("k"))
        val = m.group("v").strip()
        if fld is None or fld == "_ignore":
            last_key = None
            continue
        if fld == "service_date":
            d = parse_date(val)
            if d is None:
                continue
            if cur is None or cur.get("service_date"):
                flush()
                cur = {"description": ""}
            cur["service_date"] = d
        elif cur is None:
            continue
        elif fld in ("description", "code"):
            first, _, rest = val.partition(" ")
            code = parse_code(first)
            if code:
                cur["code"] = code
                cur["description"] = rest
            else:
                cur["description"] = val
        elif fld in AMOUNT_FIELDS:
            cur[fld] = parse_amount(val)
            if val and cur[fld] is None:
                warnings.append(f"could not read {fld} '{val}'")
        last_key = fld
    flush()
    return items


# ---------------------------------------------------------------------------
STRATEGIES = (
    ("pipe_table", parse_pipe_table),
    ("csv", parse_csv),
    ("tsv", parse_tsv),
    ("fixed_width", parse_fixed_width),
    ("key_value", parse_key_value),
)


def find_duplicates(items: list[LineItem]) -> list[tuple[int, int]]:
    pairs = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            # description is compared only when there is no code: OCR noise often differs between copies
            ka = (a.service_date, a.code, a.billed_amount, None if a.code else a.description.lower())
            kb = (b.service_date, b.code, b.billed_amount, None if b.code else b.description.lower())
            if ka == kb and a.billed_amount is not None:
                pairs.append((i, j))
    return pairs


def parse(text: str) -> ParseResult:
    best_name, best_items, best_warn = "unknown", [], []
    for name, fn in STRATEGIES:
        warn: list[str] = []
        try:
            items = fn(text, warn)
        except Exception as exc:  # a strategy failing must not break the others
            items, warn = [], [f"{name} parser error: {exc!r}"]
        if len(items) > len(best_items):
            best_name, best_items, best_warn = name, items, warn
    line_items = _finish(best_items)
    warnings = list(best_warn)
    dups = find_duplicates(line_items)
    for i, j in dups:
        warnings.append(f"possible duplicate charge: lines {i + 1} and {j + 1}")
    if not line_items:
        warnings.append("no line items found")
    return ParseResult(format=best_name, line_items=line_items, possible_duplicates=dups, warnings=warnings)
