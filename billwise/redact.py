"""Rule + regex PII redaction for medical bills and EOBs.

Detected identifiers are replaced with typed placeholders such as ``[NAME_1]``.
The same underlying value always gets the same placeholder within a document
(so "DOE, JANE", "Jane Doe" and "Dear Jane," all become ``[NAME_1]``).

The placeholder -> original mapping lives only on the ``RedactionResult`` /
``Redactor`` objects in process memory. Nothing is written to disk or logged,
and the HTTP API never returns the original values.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

PII_TYPES = ("NAME", "ADDRESS", "PHONE", "EMAIL", "DOB", "SSN", "MEMBER_ID", "ACCOUNT_ID")

# A small list of common US given names, used to find names that are not
# behind a label (e.g. the window-envelope address block). It is intentionally
# not tied to the synthetic generator's list; coverage gaps are a known
# limitation and show up in the evaluation.
COMMON_FIRST_NAMES = {
    "james", "john", "robert", "michael", "william", "david", "richard", "joseph", "thomas",
    "charles", "christopher", "daniel", "matthew", "anthony", "mark", "donald", "steven",
    "paul", "andrew", "joshua", "kenneth", "kevin", "brian", "george", "timothy", "ronald",
    "edward", "jason", "jeffrey", "ryan", "jacob", "gary", "nicholas", "eric", "jonathan",
    "stephen", "larry", "justin", "scott", "brandon", "benjamin", "samuel", "gregory",
    "alexander", "frank", "patrick", "raymond", "jack", "dennis", "jerry", "tyler", "aaron",
    "jose", "adam", "nathan", "henry", "douglas", "zachary", "peter", "kyle", "noah", "ethan",
    "jeremy", "walter", "christian", "keith", "roger", "terry", "austin", "sean", "gerald",
    "carl", "harold", "dylan", "arthur", "lawrence", "jordan", "jesse", "bryan", "billy",
    "bruce", "gabriel", "joe", "logan", "alan", "juan", "albert", "willie", "elijah", "wayne",
    "randy", "vincent", "mason", "roy", "ralph", "bobby", "russell", "bradley", "philip",
    "eugene", "luis", "carlos", "marcus", "andre", "rafael", "miguel", "antonio", "wei",
    "mary", "patricia", "jennifer", "linda", "elizabeth", "barbara", "susan", "jessica",
    "sarah", "karen", "lisa", "nancy", "betty", "sandra", "margaret", "ashley", "kimberly",
    "emily", "donna", "michelle", "carol", "amanda", "melissa", "deborah", "stephanie",
    "dorothy", "rebecca", "sharon", "laura", "cynthia", "amy", "kathleen", "angela",
    "shirley", "brenda", "emma", "anna", "pamela", "nicole", "samantha", "katherine",
    "christine", "helen", "debra", "rachel", "carolyn", "janet", "maria", "catherine",
    "heather", "diane", "olivia", "julie", "joyce", "victoria", "ruth", "virginia", "lauren",
    "kelly", "christina", "joan", "evelyn", "judith", "andrea", "hannah", "megan", "cheryl",
    "jacqueline", "martha", "madison", "teresa", "gloria", "sara", "janice", "ann", "kathryn",
    "abigail", "sophia", "frances", "jean", "alice", "judy", "isabella", "julia", "grace",
    "amber", "denise", "danielle", "marilyn", "beverly", "charlotte", "natalie", "theresa",
    "diana", "brittany", "doris", "kayla", "alexis", "lori", "marie", "jane", "fatima",
    "aisha", "keisha", "brianna", "priya", "rosa", "ana", "carmen", "yolanda",
}

US_STATES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR"
).split()

# Words that can follow a capitalized word but mean it is not a person name.
NON_NAME_WORDS = {
    "id", "number", "no", "dob", "date", "account", "acct", "name", "hospital", "clinic",
    "health", "medical", "center", "regional", "insurance", "plan", "plans", "services",
    "service", "member", "patient", "subscriber", "group", "claim", "summary", "statement",
    "pediatrics", "imaging", "associates", "mutual", "community", "family", "valley",
    "the", "and", "for", "your", "our", "total", "balance", "questions", "phone", "email",
    "address", "provider", "mail", "to", "born", "ssn", "contact", "reference", "visit",
    "office", "emergency", "department", "processed", "primary", "billing", "please",
}

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
_TOK = r"[A-Z][A-Za-z'\-]+"
# "First [M.] Last [Last2]" | "LAST, FIRST [M]" | single token (used after "Dear")
NAME_VALUE = (
    rf"(?:{_TOK}, {_TOK}(?: [A-Z]\.?)?"
    rf"|{_TOK}(?: [A-Z]\.)?(?: {_TOK}){{1,2}}"
    rf"|{_TOK})"
)
NAME_LABEL = re.compile(
    r"\b(?:Patient(?: Name)?|Member(?: Name)?|Subscriber(?: Name)?|Insured(?: Name)?|"
    r"Guarantor(?: Name)?|Responsible Party|Prepared for|Dear|Name)"
    rf"[ \t]*[:,]?[ \t]*(?P<v>{NAME_VALUE})"
)

_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
DATE_VALUE = rf"(?:\d{{1,2}}/\d{{1,2}}/\d{{2,4}}|\d{{4}}-\d{{2}}-\d{{2}}|{_MONTH} \d{{1,2}},? \d{{4}})"
DOB_LABEL = re.compile(
    rf"\b(?:DOB|D\.O\.B\.?|Date of Birth|Birth ?[Dd]ate|Born)\b[ \t]*[:#]?[ \t]*(?P<v>{DATE_VALUE})"
)

SSN_LABELED = re.compile(
    r"\b(?:SSN|SS#|Social Security(?: No\.?| Number)?)[ \t]*[:#]?[ \t]*(?P<v>\d{3}[- ]?\d{2}[- ]?\d{4})\b"
)
SSN_BARE = re.compile(r"(?<![\w-])\d{3}-\d{2}-\d{4}(?![\w-])")

PHONE = re.compile(r"(?<![\w-])(?:\+?1[-. ])?(?:\(\d{3}\) ?|\d{3}[-.])\d{3}[-.]\d{4}(?![\w-])")
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

_ID_VALUE = r"(?P<v>[A-Z0-9][A-Z0-9\-]{4,24})"
MEMBER_ID_LABEL = re.compile(
    r"\b(?:(?:Member|Subscriber|Insured|Policy) ?(?:ID|Id|#|No\.?|Number)|ID ?(?:#|No\.?|Number))"
    rf"[ \t]*[:#]?[ \t]*{_ID_VALUE}"
)
ACCOUNT_ID_LABEL = re.compile(
    r"\b(?:Account|Acct|Claim|Reference|Ref|Invoice|Encounter|Visit)\.?(?: ?(?:Number|No\.?|#|ID))?"
    rf"[ \t]*[:#]?[ \t]*{_ID_VALUE}"
)

_SUFFIX = (r"(?:St|Street|Ave|Avenue|Rd|Road|Dr|Drive|Ln|Lane|Ct|Court|Blvd|Boulevard|Way|Pl|Place|"
           r"Pkwy|Parkway|Cir|Circle|Ter|Terrace|Hwy|Highway)")
STREET = re.compile(
    rf"\b\d{{1,6}} (?:[NSEW]\.? )?(?:[A-Za-z]+ ){{1,3}}{_SUFFIX}\.?"
    r"(?: (?:Apt|Unit|Suite|Ste)\.? ?\w+| ?#\w+)?(?![A-Za-z])",
    re.IGNORECASE,
)
PO_BOX = re.compile(r"\bP\.? ?O\.? Box \d+\b", re.IGNORECASE)
CITY_STATE_ZIP = re.compile(
    rf"\b[A-Za-z][A-Za-z.]*(?: [A-Za-z][A-Za-z.]*){{0,2}},? (?:{'|'.join(US_STATES)}) \d{{5}}(?:-\d{{4}})?\b"
)

NAME_FROM_LIST = re.compile(rf"\b(?P<first>{_TOK})(?: [A-Z]\.)?(?: (?P<last>{_TOK}))\b")

# priority: higher wins when candidate spans overlap
PRIORITY = {"EMAIL": 90, "SSN": 85, "DOB": 80, "MEMBER_ID": 80, "ACCOUNT_ID": 80,
            "NAME": 75, "PHONE": 70, "ADDRESS": 60}


@dataclass
class Span:
    start: int
    end: int
    type: str
    placeholder: str = ""
    source: str = ""  # which rule produced it (for debugging/eval)


@dataclass
class RedactionResult:
    redacted_text: str
    spans: list[Span]
    # placeholder -> original text. In-memory only; never serialize this.
    mapping: dict[str, str] = field(repr=False, default_factory=dict)
    # placeholder -> every original surface form, in document order
    occurrences: dict[str, list[str]] = field(repr=False, default_factory=dict)

    def restore(self, text: str | None = None) -> str:
        """Reverse the redaction using the in-memory mapping.

        With no argument this reproduces the original text exactly (each
        occurrence gets back its own surface form, e.g. "DOE, JANE" vs
        "Jane Doe"). For other text (e.g. a later model answer) each
        placeholder is replaced by its first-seen form.
        """
        if text is None:
            queues = {ph: list(v) for ph, v in self.occurrences.items()}
            return re.sub(r"\[[A-Z_]+_\d+\]",
                          lambda m: queues[m.group(0)].pop(0) if queues.get(m.group(0)) else m.group(0),
                          self.redacted_text)
        out = text
        for ph, orig in self.mapping.items():
            out = out.replace(ph, orig)
        return out


@dataclass
class _Cand:
    start: int
    end: int
    type: str
    prio: int
    source: str


def _name_tokens(value: str) -> list[str]:
    toks = re.findall(r"[A-Za-z'\-]+", value)
    return [t for t in toks if len(t) > 1]


def _is_plausible_name(value: str) -> bool:
    toks = _name_tokens(value)
    if not toks:
        return False
    return not any(t.lower() in NON_NAME_WORDS for t in toks)


def _name_key(value: str) -> frozenset[str]:
    return frozenset(t.lower() for t in _name_tokens(value))


def _norm_key(value: str) -> str:
    return re.sub(r"[\s\-().]", "", value).upper()


class Redactor:
    """Stateless detector + per-call mapping. Use ``redact()`` per document."""

    def __init__(self, first_names: set[str] | None = None) -> None:
        self.first_names = {n.lower() for n in (first_names or COMMON_FIRST_NAMES)}

    # ---- detection -------------------------------------------------------
    def detect(self, text: str) -> list[_Cand]:
        c: list[_Cand] = []

        def add(m: re.Match, typ: str, source: str, group: str | int = 0, bonus: int = 0) -> None:
            s, e = m.span(group)
            if e > s:
                c.append(_Cand(s, e, typ, PRIORITY[typ] + bonus, source))

        for m in EMAIL.finditer(text):
            add(m, "EMAIL", "email")
        for m in SSN_LABELED.finditer(text):
            add(m, "SSN", "ssn_label", "v", 5)
        for m in SSN_BARE.finditer(text):
            add(m, "SSN", "ssn_pattern")
        for m in DOB_LABEL.finditer(text):
            add(m, "DOB", "dob_label", "v")
        for m in MEMBER_ID_LABEL.finditer(text):
            if re.search(r"\d", m.group("v")):
                add(m, "MEMBER_ID", "member_label", "v")
        for m in ACCOUNT_ID_LABEL.finditer(text):
            if re.search(r"\d", m.group("v")):
                add(m, "ACCOUNT_ID", "account_label", "v")
        for m in PHONE.finditer(text):
            add(m, "PHONE", "phone")
        for m in STREET.finditer(text):
            add(m, "ADDRESS", "street")
        for m in PO_BOX.finditer(text):
            add(m, "ADDRESS", "po_box")
        for m in CITY_STATE_ZIP.finditer(text):
            add(m, "ADDRESS", "city_state_zip")
        for m in NAME_LABEL.finditer(text):
            if _is_plausible_name(m.group("v")):
                add(m, "NAME", "name_label", "v", 2)
        for m in NAME_FROM_LIST.finditer(text):
            first, last = m.group("first"), m.group("last")
            if first.lower() in self.first_names and _is_plausible_name(m.group(0)):
                if first.istitle() == last.istitle() or (first.isupper() and last.isupper()):
                    add(m, "NAME", "name_list", 0, -10)
        return c

    @staticmethod
    def _resolve(cands: list[_Cand]) -> list[_Cand]:
        chosen: list[_Cand] = []
        for cand in sorted(cands, key=lambda x: (-x.prio, -(x.end - x.start), x.start)):
            if all(cand.end <= o.start or cand.start >= o.end for o in chosen):
                chosen.append(cand)
        return sorted(chosen, key=lambda x: x.start)

    def _propagate(self, text: str, chosen: list[_Cand]) -> list[_Cand]:
        """Find further occurrences of values already detected elsewhere."""
        extra: list[_Cand] = []
        names, others = set(), {}
        for cnd in chosen:
            val = text[cnd.start:cnd.end]
            if cnd.type == "NAME":
                names.add(val)
                for t in _name_tokens(val):
                    if len(t) >= 3 and t.lower() not in NON_NAME_WORDS:
                        names.add(t)
            elif cnd.type in ("MEMBER_ID", "ACCOUNT_ID", "SSN", "EMAIL", "PHONE"):
                others[val] = cnd.type
        for val in sorted(names, key=len, reverse=True):
            variants = {val, val.upper(), val.title()}
            for v in variants:
                for m in re.finditer(rf"(?<![A-Za-z]){re.escape(v)}(?![A-Za-z])", text):
                    extra.append(_Cand(m.start(), m.end(), "NAME", 50 + len(v) // 4, "propagated"))
        for val, typ in others.items():
            for m in re.finditer(rf"(?<![\w]){re.escape(val)}(?![\w])", text):
                extra.append(_Cand(m.start(), m.end(), typ, 50, "propagated"))
        return extra

    @staticmethod
    def _merge_adjacent(text: str, spans: list[_Cand]) -> list[_Cand]:
        """Join adjacent same-type pieces: 'street, city ST zip' on one line
        becomes one ADDRESS; 'Patrice' + 'Smith' becomes one NAME."""
        gaps = {"ADDRESS": r",? ?", "NAME": r" "}
        out: list[_Cand] = []
        for s in spans:
            if out and s.type in gaps and out[-1].type == s.type:
                if re.fullmatch(gaps[s.type], text[out[-1].end:s.start]):
                    out[-1] = _Cand(out[-1].start, s.end, s.type, out[-1].prio, out[-1].source + "+" + s.source)
                    continue
            out.append(s)
        return out

    # ---- public API ------------------------------------------------------
    def redact(self, text: str) -> RedactionResult:
        chosen = self._resolve(self.detect(text))
        chosen = self._resolve(chosen + self._propagate(text, chosen))
        chosen = self._merge_adjacent(text, chosen)

        counters: dict[str, int] = {}
        key_to_ph: dict[tuple, str] = {}
        name_keys: list[tuple[frozenset[str], str]] = []
        mapping: dict[str, str] = {}
        occurrences: dict[str, list[str]] = {}
        spans: list[Span] = []
        for cnd in chosen:
            val = text[cnd.start:cnd.end]
            ph = None
            if cnd.type == "NAME":
                k = _name_key(val)
                for existing, p in name_keys:  # subset match: "Jane" -> "Jane Doe"
                    if k and (k <= existing or existing <= k):
                        ph = p
                        break
            else:
                ph = key_to_ph.get((cnd.type, _norm_key(val)))
            if ph is None:
                counters[cnd.type] = counters.get(cnd.type, 0) + 1
                ph = f"[{cnd.type}_{counters[cnd.type]}]"
                mapping[ph] = val
                if cnd.type == "NAME":
                    name_keys.append((_name_key(val), ph))
                else:
                    key_to_ph[(cnd.type, _norm_key(val))] = ph
            occurrences.setdefault(ph, []).append(val)
            spans.append(Span(cnd.start, cnd.end, cnd.type, ph, cnd.source))

        parts, last = [], 0
        for s in spans:
            parts.append(text[last:s.start])
            parts.append(s.placeholder)
            last = s.end
        parts.append(text[last:])
        return RedactionResult("".join(parts), spans, mapping, occurrences)


_default = Redactor()


def redact(text: str) -> RedactionResult:
    return _default.redact(text)
