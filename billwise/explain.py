"""Phase 2: explain a bill or EOB in plain English, grounded in parsed data and the knowledge base.

Privacy design (data minimization):
  * The raw document text is NEVER sent to a model, not even in redacted form.
  * The model receives only an ``ExplainRequest``: parsed line items (descriptions run
    through the redactor), totals computed locally, flags, retrieved knowledge-base
    snippets and the user's question (also redacted).
  * Placeholders in the model's answer are restored locally from the in-memory mapping.

Grounding checks after generation:
  * every dollar figure in the answer must equal a number in the request;
  * every cited snippet id and line number must exist in the request.
"""
from __future__ import annotations

import json
import os
import re
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, Field

from .parse import parse
from .redact import Redactor
from .retrieve import KnowledgeBase, Snippet, default_kb

CLAUDE_MODEL = "claude-opus-5-5"
DEFAULT_READING_LEVEL = "plain English at about a 6th-8th grade reading level"
_SEPARATOR = "\n⁣⁣\n"  # invisible separator: keeps one redaction pass over question + descriptions


# ---------------------------------------------------------------------------
# What is sent to the model
# ---------------------------------------------------------------------------
class LineFact(BaseModel):
    line: int
    service_date: str | None = None
    code: str | None = None
    description: str = ""
    billed_amount: str | None = None
    allowed_amount: str | None = None
    insurer_paid: str | None = None
    patient_responsibility: str | None = None
    adjustment: str | None = Field(None, description="billed minus allowed, when both are known")
    snippet_id: str | None = None


class Totals(BaseModel):
    billed_amount: str | None = None
    allowed_amount: str | None = None
    insurer_paid: str | None = None
    patient_responsibility: str | None = None
    adjustment: str | None = None
    incomplete_fields: list[str] = Field(default_factory=list, description="totals left out because a line lacks the value")


class SnippetRef(BaseModel):
    id: str
    title: str
    text: str


class ExplainRequest(BaseModel):
    reading_level: str = DEFAULT_READING_LEVEL
    question: str | None = None
    lines: list[LineFact]
    totals: Totals
    flags: list[str] = Field(default_factory=list)
    snippets: list[SnippetRef] = Field(default_factory=list)
    question_snippets: list[str] = Field(default_factory=list, description="snippet ids retrieved for the question, best first")


# ---------------------------------------------------------------------------
# What comes back
# ---------------------------------------------------------------------------
class LineExplanation(BaseModel):
    line: int
    text: str


class Explanation(BaseModel):
    summary: str
    line_explanations: list[LineExplanation]
    answer: str = Field("", description="answer to the user's question; empty if none was asked")
    next_steps: list[str] = Field(default_factory=list)
    cited_snippets: list[str] = Field(default_factory=list)


EXPLANATION_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "line_explanations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"line": {"type": "integer"}, "text": {"type": "string"}},
                "required": ["line", "text"],
                "additionalProperties": False,
            },
        },
        "answer": {"type": "string"},
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "cited_snippets": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "line_explanations", "answer", "next_steps", "cited_snippets"],
    "additionalProperties": False,
}


class ExplainResult(BaseModel):
    explanation: Explanation
    request: ExplainRequest = Field(..., description="exactly what was (or would be) sent to the model")
    model: str
    parse_format: str
    warnings: list[str] = Field(default_factory=list)
    unsupported_amounts: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Building the request
# ---------------------------------------------------------------------------
def _money(v: Decimal | None) -> str | None:
    return None if v is None else f"{v:.2f}"


def _sum(values: list[Decimal | None]) -> Decimal | None:
    return None if not values or any(v is None for v in values) else sum(values, Decimal("0"))


def build_request(text: str, question: str | None = None, kb: KnowledgeBase | None = None,
                  redactor: Redactor | None = None, reading_level: str = DEFAULT_READING_LEVEL):
    """Return (request, redaction, parse_result). Only ``request`` is ever sent to a model."""
    kb = kb or default_kb()
    redactor = redactor or Redactor()
    pr = parse(text)
    items = pr.line_items

    # One redaction pass over the document text, the question and every description. The document
    # is included only as context: names and IDs found there (e.g. behind "Patient Name:") are then
    # also redacted where they reappear in the question or a description. The document text itself
    # is never put into the request.
    joined = _SEPARATOR.join([text, question or ""] + [it.description for it in items])
    red = redactor.redact(joined)
    parts = red.redacted_text.split(_SEPARATOR)
    if len(parts) == len(items) + 2:
        red_question = parts[1].strip() or None
        red_descs = parts[2:]
    else:  # the separator was altered (should not happen); fail closed
        red_question = "[QUESTION_WITHHELD]" if question else None
        red_descs = ["" for _ in items]

    lines: list[LineFact] = []
    used: dict[str, Snippet] = {}
    flags: list[str] = []
    for i, (it, desc) in enumerate(zip(items, red_descs), 1):
        snip = kb.lookup_code(it.code)
        if snip is None and desc:
            hits = kb.search(desc, k=1, kind="code")
            if hits and hits[0][1] >= 3.0:
                snip = hits[0][0]
                flags.append(f"line {i}: code {'missing' if not it.code else it.code + ' unknown'}; "
                             f"matched '{snip.title}' by description only")
        if snip:
            used[snip.id] = snip
        adj = (it.billed_amount - it.allowed_amount
               if it.billed_amount is not None and it.allowed_amount is not None else None)
        lines.append(LineFact(
            line=i, service_date=it.service_date.isoformat() if it.service_date else None, code=it.code,
            description=desc.strip(), billed_amount=_money(it.billed_amount),
            allowed_amount=_money(it.allowed_amount), insurer_paid=_money(it.insurer_paid),
            patient_responsibility=_money(it.patient_responsibility), adjustment=_money(adj),
            snippet_id=snip.id if snip else None,
        ))

    fields = ("billed_amount", "allowed_amount", "insurer_paid", "patient_responsibility")
    tot = {f: _sum([getattr(it, f) for it in items]) for f in fields}
    adj_total = (tot["billed_amount"] - tot["allowed_amount"]
                 if tot["billed_amount"] is not None and tot["allowed_amount"] is not None else None)
    totals = Totals(**{f: _money(v) for f, v in tot.items()}, adjustment=_money(adj_total),
                    incomplete_fields=[f for f, v in tot.items() if v is None and items])

    def term(tid: str) -> None:
        if tid in kb.by_id:
            used[tid] = kb.by_id[tid]

    if items:
        for tid in ("term:billed_amount", "term:insurer_paid", "term:patient_responsibility"):
            term(tid)
    if any(it.allowed_amount is not None for it in items):
        term("term:allowed_amount")
    if any(l.adjustment not in (None, "0.00") for l in lines):
        term("term:adjustment")
    zero_paid = [l.line for l, it in zip(lines, items)
                 if it.insurer_paid == 0 and (it.patient_responsibility or 0) > 0]
    if zero_paid:
        flags.append(f"insurer paid 0.00 on line(s) {', '.join(map(str, zero_paid))} while you owe something")
        term("term:deductible")
    pending = [l.line for l, it in zip(lines, items) if it.insurer_paid is None]
    if pending:
        flags.append(f"no insurance payment shown on line(s) {', '.join(map(str, pending))}")
        term("term:pending")
    for a, b in pr.possible_duplicates:
        flags.append(f"lines {a + 1} and {b + 1} have the same date, code and charge (possible duplicate)")
        term("term:duplicate_charge")
    if re.search(r"not\s+a\s+bill", text, re.IGNORECASE):  # local check on the raw text; the text itself is not sent
        flags.append("the document says it is not a bill (Explanation of Benefits)")
        term("term:eob")
    if not items:
        flags.append("no line items could be read from this document")
    question_hits: list[str] = []
    if red_question:
        query = re.sub(r"\[[A-Z_]+_\d+\]", " ", red_question)  # placeholders are not search words
        for s, _ in kb.search(query, k=2):
            used[s.id] = s
            question_hits.append(s.id)

    req = ExplainRequest(
        reading_level=reading_level, question=red_question, lines=lines, totals=totals, flags=flags,
        snippets=[SnippetRef(id=s.id, title=s.title, text=s.text) for s in used.values()],
        question_snippets=question_hits,
    )
    return req, red, pr


# ---------------------------------------------------------------------------
# Explainers
# ---------------------------------------------------------------------------
class Explainer(Protocol):
    name: str

    def generate(self, req: ExplainRequest) -> Explanation: ...


def _first_sentences(text: str, n: int = 1) -> str:
    return " ".join(re.split(r"(?<=[.!?])\s+", text.strip())[:n])


class MockExplainer:
    """Deterministic, template-based explainer for tests, CI and offline use. No network."""

    name = "mock"

    def generate(self, req: ExplainRequest) -> Explanation:
        snip = {s.id: s for s in req.snippets}
        cited: list[str] = []
        t = req.totals
        n = len(req.lines)
        parts = [f"This document lists {n} service{'s' if n != 1 else ''}."] if n else \
            ["I could not read any charges from this document."]
        if t.billed_amount:
            parts.append(f"The providers billed ${t.billed_amount} in total.")
        if t.allowed_amount:
            parts.append(f"Your plan allowed ${t.allowed_amount}.")
        if t.insurer_paid:
            parts.append(f"Insurance paid ${t.insurer_paid}.")
        if t.patient_responsibility:
            parts.append(f"Your share comes to ${t.patient_responsibility}.")
        elif "patient_responsibility" in t.incomplete_fields:
            parts.append("Your total share can't be worked out because some lines don't show it.")

        line_ex = []
        for lf in req.lines:
            what = snip[lf.snippet_id].title if lf.snippet_id in snip else (lf.description or "a service")
            money = [f"billed ${lf.billed_amount}" if lf.billed_amount else None,
                     f"plan allowed ${lf.allowed_amount}" if lf.allowed_amount else None,
                     f"insurance paid ${lf.insurer_paid}" if lf.insurer_paid else None,
                     f"you owe ${lf.patient_responsibility}" if lf.patient_responsibility else None]
            txt = f"{what}" + (f" on {lf.service_date}" if lf.service_date else "") + ": " + \
                  "; ".join(m for m in money if m) + "."
            if lf.snippet_id in snip:
                txt += " " + _first_sentences(snip[lf.snippet_id].text)
                cited.append(lf.snippet_id)
            line_ex.append(LineExplanation(line=lf.line, text=txt))

        answer = ""
        if req.question:
            best = snip.get(req.question_snippets[0]) if req.question_snippets else None
            if best:
                answer = _first_sentences(best.text, 2)
                cited.append(best.id)
            if re.search(r"\bowe\b|\bpay\b|\bcost\b", req.question, re.IGNORECASE) and t.patient_responsibility:
                answer = (answer + f" Based on this document, your share is ${t.patient_responsibility}.").strip()

        steps = []
        for f in req.flags:
            if "duplicate" in f:
                steps.append("Ask the provider for an itemized statement and whether the repeated line is a mistake.")
                cited.append("term:duplicate_charge")
            elif "not a bill" in f:
                steps.append("Wait for the provider's bill and check that it matches this statement before paying.")
                cited.append("term:eob")
            elif "no insurance payment" in f:
                steps.append("Check with your plan whether the claim is still being processed before paying.")
                cited.append("term:pending")
            elif "insurer paid 0.00" in f:
                steps.append("Check your plan's deductible status; the plan may not pay until it is met.")
                cited.append("term:deductible")
        for tid in ("term:allowed_amount", "term:adjustment"):
            if tid in snip:
                cited.append(tid)
        return Explanation(summary=" ".join(parts), line_explanations=line_ex, answer=answer,
                           next_steps=list(dict.fromkeys(steps)), cited_snippets=list(dict.fromkeys(cited)))


SYSTEM_PROMPT = """You explain US medical bills and insurance Explanations of Benefits to patients.

You receive JSON with parsed line items, totals computed by software, flags, reference snippets and
possibly a question. Write for {reading_level}. Be calm, concrete and brief.

Rules:
- Use only dollar amounts that appear in the JSON, written exactly as given with a $ sign. Do not do new
  arithmetic; the totals are already computed. If a value is missing, say it is not shown.
- Explain codes and terms using the provided snippets and cite their ids in cited_snippets.
- Text in square brackets such as [NAME_1] is a placeholder for personal information. Keep placeholders
  exactly as written; never guess what they stand for.
- Give one line_explanations entry per line item, using its line number.
- next_steps: practical actions (ask for an itemized bill, call the plan, ask about financial assistance).
- Do not give medical advice, and do not say a charge is fraudulent; say what to ask about instead.
- If no question was asked, return an empty string for answer."""


class ClaudeExplainer:
    """Claude via the official Anthropic SDK. Used only when explicitly selected (BILLWISE_LLM=claude)."""

    def __init__(self, model: str = CLAUDE_MODEL, client=None) -> None:
        import anthropic  # optional dependency: pip install -r requirements-llm.txt

        self.anthropic = anthropic
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.name = model
        self.usage = {"requests": 0, "input_tokens": 0, "output_tokens": 0}

    def generate(self, req: ExplainRequest) -> Explanation:
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # re-run on a fallback model if this one declines
            output_config={"effort": "medium",
                           "format": {"type": "json_schema", "schema": EXPLANATION_SCHEMA}},
            system=SYSTEM_PROMPT.format(reading_level=req.reading_level),
            messages=[{"role": "user", "content": req.model_dump_json(indent=1)}],
        )
        usage = getattr(response, "usage", None)
        self.usage["requests"] += 1
        self.usage["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
        self.usage["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
        if response.stop_reason == "refusal":
            raise RuntimeError("the model declined to answer this request")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("the model's answer was cut off (max_tokens)")
        text = next(b.text for b in response.content if b.type == "text")
        return Explanation.model_validate(json.loads(text))


def get_explainer() -> Explainer:
    """Mock by default; Claude only when BILLWISE_LLM=claude is set (it needs Anthropic credentials)."""
    if os.environ.get("BILLWISE_LLM", "").lower() == "claude":
        return ClaudeExplainer()
    return MockExplainer()


# ---------------------------------------------------------------------------
# Grounding checks and the end-to-end call
# ---------------------------------------------------------------------------
_DOLLAR = re.compile(r"\$\s?(\d[\d,]*\.\d{2})")


def allowed_amounts(req: ExplainRequest) -> set[Decimal]:
    vals = []
    for lf in req.lines:
        vals += [lf.billed_amount, lf.allowed_amount, lf.insurer_paid, lf.patient_responsibility, lf.adjustment]
    t = req.totals
    vals += [t.billed_amount, t.allowed_amount, t.insurer_paid, t.patient_responsibility, t.adjustment]
    return {Decimal(v) for v in vals if v is not None}


def explanation_texts(ex: Explanation) -> list[str]:
    return [ex.summary, ex.answer] + [l.text for l in ex.line_explanations] + list(ex.next_steps)


def check_grounding(ex: Explanation, req: ExplainRequest) -> tuple[list[str], list[str]]:
    """Return (warnings, unsupported dollar amounts)."""
    ok = allowed_amounts(req)
    unsupported = []
    for txt in explanation_texts(ex):
        for m in _DOLLAR.finditer(txt):
            if Decimal(m.group(1).replace(",", "")) not in ok:
                unsupported.append(m.group(0))
    warnings = [f"amount {a} does not match any parsed value or computed total" for a in unsupported]
    provided = {s.id for s in req.snippets}
    warnings += [f"cited snippet '{c}' was not provided" for c in ex.cited_snippets if c not in provided]
    lines = {l.line for l in req.lines}
    warnings += [f"explanation refers to line {l.line}, which does not exist"
                 for l in ex.line_explanations if l.line not in lines]
    return warnings, unsupported


def explain(text: str, question: str | None = None, explainer: Explainer | None = None,
            reading_level: str = DEFAULT_READING_LEVEL) -> ExplainResult:
    explainer = explainer or get_explainer()
    req, red, pr = build_request(text, question, reading_level=reading_level)
    ex = explainer.generate(req)
    warnings, unsupported = check_grounding(ex, req)
    # restore placeholders from the question/descriptions locally, after all checks
    ex = Explanation(
        summary=red.restore(ex.summary),
        line_explanations=[LineExplanation(line=l.line, text=red.restore(l.text)) for l in ex.line_explanations],
        answer=red.restore(ex.answer), next_steps=[red.restore(s) for s in ex.next_steps],
        cited_snippets=ex.cited_snippets,
    )
    return ExplainResult(explanation=ex, request=req, model=explainer.name, parse_format=pr.format,
                         warnings=list(pr.warnings) + warnings, unsupported_amounts=unsupported)
