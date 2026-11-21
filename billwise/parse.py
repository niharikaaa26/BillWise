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
# A minus sign counts only when attached ("-113.15", "-$5.00"); "- 520.20" is a missing-value
# dash followed by a positive amount. A space is allowed only after the currency sign ("$ 45.00").
AMOUNT_RE = re.compile(r"(?<![\w.,])-?(?:[$S]\s?)?[0-9OolI][0-9OolI,]*[.,][0-9OolI]{2}(?![\w])")


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


_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%m-%d-%Y",
                 "%Y%m%d")
DATE_RE = re.compile(
    r"\b(?:\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}-\d{1,2}-\d{4}|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.? \d{1,2},? \d{4}|"
    # compact YYYYMMDD as used by billing-system exports, years 2000-2099 only
    r"20\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))\b"
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
# Fallback strategy: line-oriented, anchored on billing codes
# ---------------------------------------------------------------------------
# Used only when no table / key-value strategy finds anything. It does not need
# a header: each item starts at a line holding a billing code (plus a date, an
# amount or a "CPT"/"HCPCS" prefix), and runs until the next such line, a blank
# line or a totals line. Amounts are mapped by (1) label words right before
# them, (2) the nearest header-like line, (3) arithmetic consistency
# (allowed = paid + patient share), (4) conventional left-to-right column order.
CODE_TOKEN = re.compile(r"(?<![\w$.,/:-])(?P<code>\d{5}|[A-V]\d{4})(?:-[A-Z0-9]{2})?(?![\w.,/])")
CODE_PREFIX = re.compile(r"\b(?:CPT|HCPCS|Proc(?:edure)?|Code)\b[^\w\n]{0,3}$", re.IGNORECASE)
ZIP_CONTEXT = re.compile(r"\b[A-Z]{2},? $")
MISSING_TOKEN = re.compile(r"(?<!\S)(?:--|-|—|–)(?!\S)")
SEGMENT_BREAK = re.compile(r"[·|;•]|\s{3,}")
_AMOUNT_KEYS = [(f, k) for f, keys in _HEADER_RULES if f in AMOUNT_FIELDS or f is None for k in keys]


def _anchor(line: str) -> re.Match | None:
    for m in CODE_TOKEN.finditer(line):
        before = line[:m.start()]
        if ZIP_CONTEXT.search(before):
            continue  # "OH 45011" is a ZIP code
        if DATE_RE.search(line) or AMOUNT_RE.search(line[m.end():]) or CODE_PREFIX.search(before):
            return m
    return None


def _label_field(text_before: str) -> str | None:
    """Classify the words immediately before an amount (same segment), e.g. 'Plan paid'."""
    seg = SEGMENT_BREAK.split(text_before)[-1]
    seg = AMOUNT_RE.split(seg)[-1]
    words = re.findall(r"[A-Za-z][A-Za-z'/]*", seg)[-4:]
    if not words:
        return None
    f = classify_header(" ".join(words))
    return f if f in AMOUNT_FIELDS or f == "_ignore" else None


def _header_amount_fields(lines: list[str], before: int) -> list[str]:
    """Amount columns named, in order, by the nearest preceding header-like line."""
    for ln in reversed(lines[:before]):
        if AMOUNT_RE.search(ln) or _anchor(ln):
            continue
        low = " ".join(ln.lower().split())
        hits: list[tuple[int, int, str]] = []
        for f, k in sorted(_AMOUNT_KEYS, key=lambda x: -len(x[1])):
            for m in re.finditer(rf"\b{re.escape(k)}\b", low):
                if all(m.end() <= s or m.start() >= e for s, e, _ in hits):
                    hits.append((m.start(), m.end(), f or "_ignore"))
        if sum(f != "_ignore" for _, _, f in hits) >= 2:
            return [f for _, _, f in sorted(hits)]
    return []


def _close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= Decimal("0.01")


def _arithmetic_fields(vals: list[Decimal | None]) -> list[str]:
    n = len(vals)
    B, A, P, R, X = "billed_amount", "allowed_amount", "insurer_paid", "patient_responsibility", "_ignore"
    if n == 4 and None not in vals:
        v = vals
        if _close(v[1], v[2] + v[3]) and v[0] >= v[1]:
            return [B, A, P, R]
        if _close(v[0], v[1] + v[2] + v[3]):
            return [B, P, X, R]  # charges = paid + adjustment + patient share
    if n >= 5 and None not in vals:
        for i in range(1, n):
            for j in range(i + 1, n):
                for k in range(j + 1, n):
                    if _close(vals[i], vals[j] + vals[k]) and vals[0] >= vals[i]:
                        out = [X] * n
                        out[0], out[i], out[j], out[k] = B, A, P, R
                        return out
    return {1: [B], 2: [B, R], 3: [B, P, R], 4: [B, A, P, R]}.get(n, [B] + [X] * (n - 2) + [R])


_KV_DESC = re.compile(r"(?:^|[;|])\s*(?:DESC|DESCRIPTION|SERVICE|SVC_DESC|PROC_DESC)\s*=\s*([^;|]*)",
                      re.IGNORECASE)
_KV_KEY = re.compile(r"(?:^|[;|])\s*[A-Za-z_]{2,}\s*=\s*")
_NARRATIVE_LEAD = re.compile(
    r"^(?:the patient|patient|you|he|she|they|your (?:son|daughter|child))?\s*"
    r"(?:received|had|underwent|got|was seen for|were seen for)\s+", re.IGNORECASE)


def _clean_before_code(text: str) -> str:
    """Description written before the code, as in '... received office visit (CPT 99213), ...'."""
    t = DATE_RE.split(text)[-1]
    t = re.sub(r"[\s(\[]*(?:CPT|HCPCS|code|proc(?:edure)?)?[\s#:]*$", "", t, flags=re.IGNORECASE)
    t = _NARRATIVE_LEAD.sub("", t.strip(" ,;:-—"))
    return t.strip(" ,;:-—(")


def _assign_fields(labels: list[str | None], vals: list[Decimal | None], lines: list[str], i: int,
                   warnings: list[str], n: int) -> list[str]:
    """Labels where present. Each unlabeled amount gets a remaining field that fits between its
    labeled neighbours in the usual order (billed, allowed, paid, you owe): the earliest such
    field unless only labels precede it, then the latest. With no labels: header, then arithmetic."""
    if labels and all(labels):
        return labels  # type: ignore[return-value]
    if any(labels):
        order = {f: k for k, f in enumerate(AMOUNT_FIELDS)}
        remaining = [f for f in AMOUNT_FIELDS if f not in set(labels)]
        out = list(labels)
        for k, lb in enumerate(labels):
            if lb:
                continue
            before = [order[x] for x in labels[:k] if x in order]
            after = [order[x] for x in labels[k + 1:] if x in order]
            lo, hi = (max(before) if before else -1), (min(after) if after else len(AMOUNT_FIELDS))
            cands = [f for f in remaining if lo < order[f] < hi] or remaining
            if not cands:
                out[k] = "_ignore"
                continue
            pick = cands[-1] if before and not after else cands[0]
            out[k] = pick
            remaining.remove(pick)
        warnings.append(f"line {n + 1}: unlabeled amount(s) assigned by position between labeled ones")
        return out  # type: ignore[return-value]
    hdr = _header_amount_fields(lines, i)
    if len(hdr) == len(vals):
        return hdr
    warnings.append(f"amount columns for line {n + 1} inferred without labels")
    return _arithmetic_fields(vals)


def parse_anchored(text: str, warnings: list[str]) -> list[dict]:
    lines = text.splitlines()
    anchors = [(i, m) for i, ln in enumerate(lines) if (m := _anchor(ln))]
    items: list[dict] = []
    prev_date: date | None = None
    for n, (i, m) in enumerate(anchors):
        stop = anchors[n + 1][0] if n + 1 < len(anchors) else len(lines)
        block = [i]
        for j in range(i + 1, stop):
            if not lines[j].strip() or SKIP_ROW.search(lines[j]):
                break
            block.append(j)
        # amounts and explicit missing markers, with the label words before each
        slots: list[tuple[Decimal | None, str | None]] = []
        desc_parts: list[str] = []
        for j in block:
            ln = lines[j]
            start = m.end() if j == i else 0
            toks = [(a.start(), a.end(), parse_amount(a.group(0))) for a in AMOUNT_RE.finditer(ln, start)]
            first_amt = toks[0][0] if toks else None
            for mm in MISSING_TOKEN.finditer(ln, start):
                lbl = _label_field(ln[:mm.start()])
                if lbl or (first_amt is not None and mm.start() > first_amt):
                    toks.append((mm.start(), mm.end(), None))
            toks.sort()
            for s, e, v in toks:
                slots.append((v, _label_field(ln[:s])))
            text_end = toks[0][0] if toks else len(ln)
            if j == i:
                kv = _KV_DESC.search(ln)
                piece = kv.group(1) if kv else _KV_KEY.sub(" ", ln[m.end():text_end])
                after = piece.strip(" \t-—–:·|),;")
                if after and classify_header(after) in AMOUNT_FIELDS:
                    # only an amount label follows the code: the description came before it
                    before = _clean_before_code(ln[:m.start()])
                    piece = before if len(before.split()) >= 2 else ""
            elif not toks:
                piece = ln
            else:
                piece = ""
            piece = piece.strip(" \t-—–:·|")
            if piece:
                desc_parts.append(piece)
        dm = DATE_RE.search(lines[i]) or next((d for j in block if (d := DATE_RE.search(lines[j]))), None)
        d = parse_date(dm.group(0)) if dm else prev_date
        item: dict = {"service_date": d, "code": parse_code(m.group(0)), "description": " ".join(desc_parts)}
        vals = [v for v, _ in slots]
        labels = [lbl for _, lbl in slots]
        fields = _assign_fields(labels, vals, lines, i, warnings, n)
        for f, v in zip(fields, vals):
            if f in AMOUNT_FIELDS and f not in item:
                item[f] = v
        items.append(item)
        prev_date = d
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
    if not best_items:  # no table or key/value layout recognised: degrade to the line-oriented fallback
        warn = []
        try:
            items = parse_anchored(text, warn)
        except Exception as exc:
            items, warn = [], [f"anchored parser error: {exc!r}"]
        if items:
            best_name, best_items, best_warn = "anchored", items, warn
    line_items = _finish(best_items)
    warnings = list(best_warn)
    dups = find_duplicates(line_items)
    for i, j in dups:
        warnings.append(f"possible duplicate charge: lines {i + 1} and {j + 1}")
    if not line_items:
        warnings.append("no line items found")
    return ParseResult(format=best_name, line_items=line_items, possible_duplicates=dups, warnings=warnings)
