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

PII_TYPES = ("NAME", "ADDRESS", "PHONE", "EMAIL", "DOB", "SSN", "MEMBER_ID", "ACCOUNT_ID", "OTHER")
# OTHER: a field value redacted by the over-redaction fallback whose shape matches no specific type.

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
    "responsibility", "benefits", "care", "copay", "deductible", "of", "on", "file",
}

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
_TOK = r"[A-Z][A-Za-z'\-]+"

# Characters OCR commonly confuses inside words (b<->h, i/l/1, t->l, o->0, e<->c, m->rn).
_OCR_CONFUSIONS = {"i": "l1|!", "l": "i1|", "t": "l", "o": "0", "b": "h", "h": "b", "e": "c", "c": "e", "s": "5"}


def _label(*phrases: str) -> str:
    """Case-insensitive, OCR-tolerant regex alternation for field labels."""
    alts = []
    for phrase in phrases:
        out = []
        for ch in phrase:
            lo = ch.lower()
            if lo == "m":
                out.append("(?:[mM]|rn)")
            elif ch.isalpha():
                chars = {lo, lo.upper()} | set(_OCR_CONFUSIONS.get(lo, ""))
                out.append("[" + "".join(re.escape(c) for c in sorted(chars)) + "]")
            elif ch == " ":
                out.append(r"[ \t]+")
            elif ch == ".":
                out.append(r"\.?")
            elif ch == "'":
                out.append("'?")
            else:
                out.append(re.escape(ch))
        alts.append("".join(out))
    return "(?:" + "|".join(sorted(alts, key=len, reverse=True)) + ")"


# separator between a label and its value: "Label:", "Label #", "Label ....... value"
_SEP = r"[ \t]*(?:\.{2,}[ \t]*)?[:#,]?[ \t]*"
# "First [M.] Last [Last2]" | "LAST, FIRST [M]" | single token (used after "Dear")
NAME_VALUE = (
    rf"(?:{_TOK}, {_TOK}(?: [A-Z]\.?)?"
    rf"|{_TOK}(?: [A-Z]\.)?(?: {_TOK}){{1,2}}(?: [A-Z]\.?(?![A-Za-z]))?"
    rf"|{_TOK})"
)
# Name labels must be followed by ":" / "#" / dot leaders (so "...PATIENT, LOW COMPLEXITY"
# in a description is not a label); "Dear" is the one label used without punctuation.
_FIELD_SEP = r"[ \t]*(?:\.{2,}[ \t]*[:#]?|[:#])[ \t]*"
NAME_LABEL = re.compile(
    r"\b(?:" + _label("Patient Name", "Patient", "Member Name", "Member", "Subscriber Name", "Subscriber",
                      "Insured Name", "Insured", "Guarantor Name", "Guarantor", "Responsible Party",
                      "Account Holder", "Prepared for", "Statement for", "Name")
    + _FIELD_SEP
    # without punctuation: "Dear X", and labels that end in "Name" ("Pt Name DOE JANE" on scans)
    + "|" + _label("Dear", "Name") + r"[ \t]+)" + rf"(?P<v>{NAME_VALUE})"
)
# a courtesy title in running text: "This statement is for Ms. Jane Doe." (Dr. is left alone: providers)
HONORIFIC_NAME = re.compile(rf"\b(?:Mr|Mrs|Ms|Mx|Miss)\.? (?P<v>{NAME_VALUE})")

_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
# Any common date shape. Used only right after a birth-related label, so it can be permissive:
# 3/12/81, 03-12-1981, 1981.03.12, 1981-03-12, March 12, 1981, 12 Mar 1981, 03121981, 031281.
DATE_VALUE = (rf"(?:\d{{1,4}}[./-]\d{{1,2}}[./-]\d{{1,4}}"
              rf"|{_MONTH} \d{{1,2}},? \d{{4}}|\d{{1,2}} {_MONTH},? \d{{4}}"
              rf"|\d{{8}}(?!\d)|\d{{6}}(?!\d))")
DOB_LABEL = re.compile(
    r"\b" + _label("DOB", "D.O.B.", "Date of Birth", "Birth Date", "Birthdate", "Born")
    + r"[ \t]*(?:\([^)\n]{0,20}\))?" + _SEP + rf"(?P<v>{DATE_VALUE})"
)

SSN_LABELED = re.compile(
    r"\b(?:SSN|SS#|Social Security(?: No\.?| Number)?)[ \t]*[:#]?[ \t]*(?P<v>\d{3}[- ]?\d{2}[- ]?\d{4})\b"
)
SSN_BARE = re.compile(r"(?<![\w-])\d{3}-\d{2}-\d{4}(?![\w-])")

# 3-3-4 digit grouping with any common separator: (513) 555-0123, 513-555-0123, 513.555.0123,
# 513 555 0123, 513/555-0123, +1 513 555 0123. At least one separator is required, so bare
# 10-digit identifiers are not taken for phone numbers.
PHONE = re.compile(r"(?<![\w-])(?:\+?1[-. ])?(?:\(\d{3}\) ?|\d{3}[-. /])\d{3}[-. ]\d{4}(?![\w-])")
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

_ID_VALUE = r"(?P<v>[A-Z0-9][A-Z0-9\-]{4,24})"
_ID_SUFFIX = "(?: ?" + _label("ID No.", "ID", "No.", "Number", "#") + ")"
MEMBER_ID_LABEL = re.compile(
    r"\b(?:" + _label("Member", "Subscriber", "Insured's", "Insured", "Policy", "Policyholder") + _ID_SUFFIX
    + "|" + _label("ID") + " ?" + _label("#", "No.", "Number") + ")"
    + _SEP + _ID_VALUE
)
ACCOUNT_ID_LABEL = re.compile(
    r"\b(?:" + _label("Account", "Acct", "Claim", "Reference", "Ref", "Invoice", "Encounter", "Visit") + r"\.?"
    + _ID_SUFFIX + "?"
    + "|" + _label("Guarantor") + _ID_SUFFIX + ")"
    + _SEP + _ID_VALUE
)

# Generic fallback: a digit-heavy token right after an ID-like word ("ID", "No.", "Number",
# "#", "Acct"), whatever words come before it. Typed as MEMBER_ID after "ID" or a
# member-type word, otherwise ACCOUNT_ID. Not applied after group/NPI/tax/fax/page words.
ID_NEAR = re.compile(
    r"(?:\b(?P<kw>" + _label("ID", "No.", "Number", "Acct", "Policy") + r")|#)" + _SEP
    # a sentence-ending period may follow the value; a decimal point (".5") may not
    + r"(?P<v>[A-Z0-9][A-Z0-9\-]{4,24})(?!\w|\.\w)"
)
# member-type words, including common abbreviations seen on statements (Mbr, Memb, Subscr)
_ID_MEMBER_CTX = re.compile(r"\b(?:member|mbr|memb|mem|subscriber|subscr|sub|insured|ins|policy|plan)\b",
                            re.IGNORECASE)
_ID_EXCLUDE_CTX = re.compile(r"\b(?:group|grp|npi|tax|ein|tin|fax|page|line|item|rx|bin|pcn)\b\W*$",
                             re.IGNORECASE)

_SUFFIX = (r"(?:St|Street|Ave|Avenue|Rd|Road|Dr|Drive|Ln|Lane|Ct|Court|Blvd|Boulevard|Way|Pl|Place|"
           r"Pkwy|Parkway|Cir|Circle|Ter|Terrace|Hwy|Highway)")
# Street-name words must be capitalised (Title or UPPER case), so prose such as
# "2025 the patient received ct" is not taken for "<number> <words> Ct"; the suffix and
# unit words may be in any case.
STREET = re.compile(
    rf"\b\d{{1,6}} (?:[NSEW]\.? )?(?:[A-Z][A-Za-z]* ){{1,3}}(?i:{_SUFFIX})\.?"
    r"(?i:(?: (?:Apt|Unit|Suite|Ste)\.? ?\w+| ?#\w+))?(?![A-Za-z])"
)
PO_BOX = re.compile(r"\bP\.? ?O\.? Box \d+\b", re.IGNORECASE)
CITY_STATE_ZIP = re.compile(
    rf"\b[A-Za-z][A-Za-z.]*(?: [A-Za-z][A-Za-z.]*){{0,2}},? (?:{'|'.join(US_STATES)}) \d{{5}}(?:-\d{{4}})?\b"
)

NAME_FROM_LIST = re.compile(rf"\b(?P<first>{_TOK})(?: [A-Z]\.)?(?: (?P<last>{_TOK}))\b")

# Names in running text: any pair of Title-case words, unless either word is on this stop list of
# sentence starters, business/billing/place/calendar words. It fails towards redacting (an
# unfamiliar organisation name will be redacted too) and runs below every specific rule.
_TITLE_TOK = r"[A-Z][a-z]+(?:['\-][A-Z]?[a-z]+)?"
TITLE_PAIR = re.compile(rf"\b(?P<v>{_TITLE_TOK}(?: [A-Z]\.)? {_TITLE_TOK})\b")
PROSE_STOP = NON_NAME_WORDS | {
    # sentence starters and function words
    "a", "an", "as", "at", "by", "but", "from", "he", "her", "his", "hi", "hello", "how", "if", "in", "it",
    "let", "my", "note", "please", "re", "regards", "see", "she", "sincerely", "thank", "thanks", "they",
    "this", "to", "we", "what", "when", "where", "who", "why", "with", "best", "call", "dear", "hey",
    # organisations
    "agency", "administrators", "bank", "benefit", "cardiology", "collection", "collections", "company",
    "corp", "financial", "inc", "llc", "orthopedic", "orthopedics", "partner", "partners", "pllc",
    "recovery", "solutions", "urgent", "office", "department", "university", "laboratory", "lab", "labs",
    # billing vocabulary
    "allow", "allowed", "amt", "amount", "billed", "cd", "charge", "charged", "charges", "chg", "claim",
    "code", "coinsurance", "cost", "costs", "covered", "description", "discount", "dos", "dt", "due",
    "explanation", "ins", "item", "line", "mbr", "mod", "owe", "owes", "page", "paid", "payment", "pd",
    "proc", "procedure", "pt", "share", "stmt", "svc", "units", "summary", "breakdown", "subject",
    # calendar
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
    "nov", "dec", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "mon", "tue", "wed", "thu", "fri", "sat", "sun",
}

# priority: higher wins when candidate spans overlap
PRIORITY = {"EMAIL": 90, "SSN": 85, "DOB": 80, "MEMBER_ID": 80, "ACCOUNT_ID": 80,
            "NAME": 75, "PHONE": 70, "ADDRESS": 60, "OTHER": 30}

# ---------------------------------------------------------------------------
# Over-redaction fallback for field values
# ---------------------------------------------------------------------------
# Any "Key: value", "KEY=value", "Key #: value" or "Key ...... value" is redacted unless the key is
# known to be safe (service dates, codes, descriptions, amounts, provider/payer/plan). A key with a
# personal word in it is never safe, so "Date of birth" is not let through by the word "date".
# Money amounts are never redacted. This fails closed: an unfamiliar key is redacted.
FIELD_RE = re.compile(
    r"(?:(?<=^)|(?<=[\s;|,]))(?P<key>[A-Za-z][A-Za-z0-9_'#/. -]{0,40}?)[ \t]*"
    r"(?P<sep>=|:(?!//)|\.{2,}[ \t]*:?|#[ \t]*:)[ \t]*"
    r"(?P<val>[^;|\n]*?)[ \t]*(?=[ \t]{2,}|[;|]|$)",
    re.MULTILINE,
)
PII_KEY_WORDS = {
    "name", "patient", "pt", "member", "mbr", "subscriber", "insured", "guarantor", "holder", "birth",
    "birthdate", "dob", "born", "ssn", "social", "address", "addr", "residence", "home", "mailing", "mail",
    "street", "phone", "tel", "telephone", "cell", "mobile", "email", "mrn", "record", "id", "acct",
    "account", "claim", "reference", "ref", "policy", "contact", "prepared", "dear", "employer", "re",
}
SAFE_KEY_WORDS = {
    "date", "dt", "dos", "service", "svc", "statement", "stmt", "period", "printed", "processed", "generated",
    "code", "cpt", "hcpcs", "proc", "procedure", "description", "desc", "charge", "charges", "chg", "billed",
    "amount", "allowed", "covered", "paid", "payment", "pymt", "pmt", "share", "cost", "costs", "owe",
    "balance", "total", "totals", "adjustment", "adj", "discount", "deductible", "coinsurance", "copay",
    "units", "mod", "modifier", "provider", "physician", "rendering", "referring", "facility", "payer",
    "insurer", "insurance", "plan", "group", "npi", "tax", "page", "item", "line", "count", "status", "remit",
    "billing", "questions", "customer", "care", "ptresp", "resp", "export",
}
_MONEY_ONLY = re.compile(r"-?\(?\$?\s?[\d,]+\.\d{2}\)?")
_NAME_SHAPE = re.compile(r"[A-Z][A-Za-z'.\-]*(?:[ ,^]+[A-Z][A-Za-z'.\-]*){0,4}\^?")


def _key_words(key: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", key.lower())


def _key_is_safe(key: str) -> bool:
    words = _key_words(key)
    if not words or set(words) & PII_KEY_WORDS or words[-1] == "for":
        return False
    return bool(set(words) & SAFE_KEY_WORDS)


def _shape_type(val: str, key: str) -> str:
    v = val.strip()
    if (m := EMAIL.search(v)) and len(m.group(0)) >= 0.6 * len(v):
        return "EMAIL"
    if (m := PHONE.search(v)) and len(m.group(0)) >= 0.6 * len(v):
        return "PHONE"
    if re.fullmatch(DATE_VALUE, v):
        return "DOB"
    if STREET.search(v) or PO_BOX.search(v) or CITY_STATE_ZIP.search(v) or ("^" in v and re.search(r"\d", v)):
        return "ADDRESS"
    if _NAME_SHAPE.fullmatch(v) and not re.search(r"\d", v):
        return "NAME"
    if sum(ch.isdigit() for ch in v) >= 4:
        words = set(_key_words(key))
        if words & {"member", "mbr", "subscriber", "insured", "policy"} or (
                "id" in words and not words & {"acct", "account", "claim", "mrn", "ref", "reference"}):
            return "MEMBER_ID"
        return "ACCOUNT_ID"
    return "OTHER"


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
        for m in ID_NEAR.finditer(text):
            v = m.group("v")
            if sum(ch.isdigit() for ch in v) < 4:
                continue
            ctx = text[text.rfind("\n", 0, m.start()) + 1:m.start()]
            if _ID_EXCLUDE_CTX.search(ctx):
                continue
            if re.fullmatch(r"\d{5}(?:-\d{4})?", v) and m.group(0)[:2] in US_STATES:
                continue  # "Boise, ID 83701": Idaho + ZIP code, not an ID label
            kw = (m.group("kw") or "#").lower()
            typ = ("MEMBER_ID" if kw in ("id", "policy") or _ID_MEMBER_CTX.search(ctx[-24:])
                   else "ACCOUNT_ID")
            add(m, typ, "id_near", "v", -15)
        for m in HONORIFIC_NAME.finditer(text):
            if _is_plausible_name(m.group("v")):
                add(m, "NAME", "honorific", "v")
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
        for m in STREET.finditer(text):  # a name on the line just above a street address
            ls = text.rfind("\n", 0, m.start()) + 1
            if ls == 0 or text[ls:m.start()].strip():
                continue
            pe = ls - 1
            ps = text.rfind("\n", 0, pe) + 1
            pm = re.fullmatch(rf"[ \t]*(?P<v>{NAME_VALUE})[ \t]*", text[ps:pe])
            if pm and len(_name_tokens(pm.group("v"))) >= 2 and _is_plausible_name(pm.group("v")):
                c.append(_Cand(ps + pm.start("v"), ps + pm.end("v"), "NAME", PRIORITY["NAME"], "address_block"))
        for m in TITLE_PAIR.finditer(text):
            words = [w.lower() for w in re.findall(r"[A-Za-z]+", m.group("v")) if len(w) > 1]
            if re.search(r"\bDr\.?\s*$", text[max(0, m.start() - 4):m.start()]):
                continue  # provider names ("Dr. Gil Cho") are not patient information
            if words and not any(w in PROSE_STOP for w in words):
                add(m, "NAME", "title_pair", "v", -20)
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
    @staticmethod
    def _union_overlapping(cands: list[_Cand], typ: str = "ADDRESS") -> list[_Cand]:
        """Merge overlapping candidates of one type into their union, so that e.g. a street
        match and a city/state/ZIP match that share words cannot leave a house number behind."""
        same = sorted((c for c in cands if c.type == typ), key=lambda c: c.start)
        merged: list[_Cand] = []
        for c in same:
            if merged and c.start < merged[-1].end:
                last = merged[-1]
                merged[-1] = _Cand(last.start, max(last.end, c.end), typ, max(last.prio, c.prio),
                                   last.source + "|" + c.source)
            else:
                merged.append(c)
        return [c for c in cands if c.type != typ] + merged

    @staticmethod
    def field_candidates(text: str) -> list[_Cand]:
        """Values of fields whose key is not known to be safe (the over-redaction fallback)."""
        out = []
        for m in FIELD_RE.finditer(text):
            key, val = m.group("key"), m.group("val")
            if not val.strip() or _MONEY_ONLY.fullmatch(val.strip()) or _key_is_safe(key):
                continue
            typ = _shape_type(val, key)
            out.append(_Cand(m.start("val"), m.end("val"), typ, PRIORITY["OTHER"], "field"))
        return out

    @staticmethod
    def _absorb_fields(text: str, specific: list[_Cand], fields: list[_Cand]) -> list[_Cand]:
        """Combine field-value spans with specific detections. A field value overlapping exactly one
        specific span widens that span to the whole value (keeping its type); one overlapping several
        keeps them and adds the uncovered pieces; one overlapping none is kept with its shape type."""
        specific = list(specific)
        extra: list[_Cand] = []
        for f in fields:
            idx = [i for i, o in enumerate(specific) if o.start < f.end and f.start < o.end]
            strong = [i for i in idx if specific[i].source not in ("title_pair", "name_list")]
            if strong:  # weak name guesses ("Juniper Road") give way to a specific detection
                idx = strong
            if not idx:
                extra.append(f)
            elif len({specific[i].type for i in idx}) == 1:  # one type inside the value: widen to all of it
                group = [specific[i] for i in idx]
                o = max(group, key=lambda g: g.prio)
                merged = _Cand(min([f.start] + [g.start for g in group]), max([f.end] + [g.end for g in group]),
                               o.type, o.prio, o.source + "+field")
                for i in sorted(idx, reverse=True):
                    del specific[i]
                specific.append(merged)
            else:
                cuts = sorted((specific[i].start, specific[i].end) for i in idx)
                pos = f.start
                for s, e in cuts + [(f.end, f.end)]:
                    piece = text[pos:s]
                    if s > pos and re.search(r"[A-Za-z0-9]", piece):
                        lead = len(piece) - len(piece.lstrip(" ,/;-"))
                        trail = len(piece.rstrip(" ,/;-"))
                        if trail > lead:
                            extra.append(_Cand(pos + lead, pos + trail, _shape_type(piece, ""),
                                               PRIORITY["OTHER"], "field-gap"))
                    pos = max(pos, e)
        return specific + extra

    def redact(self, text: str) -> RedactionResult:
        cands = self._absorb_fields(text, self.detect(text), self.field_candidates(text))
        chosen = self._resolve(self._union_overlapping(cands))
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
