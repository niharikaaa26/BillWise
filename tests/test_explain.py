"""Phase 2 tests: retrieval, data minimization, grounding checks, /explain, Claude client wiring."""
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from billwise.api import app
from billwise.explain import (ClaudeExplainer, Explanation, LineExplanation, MockExplainer,
                              build_request, check_grounding, explain)
from billwise.retrieve import KnowledgeBase, default_kb, tokenize

ROOT = Path(__file__).resolve().parents[1]
DEV_SETS = ["synthetic", "dev2", "dev3", "dev4", "dev5", "dev6"]

BILL = """CLEARPATH HEALTH INSURANCE
EXPLANATION OF BENEFITS -- THIS IS NOT A BILL
Patient Name: DOE, JANE
Member ID: W123456789
Service Date | Code  | Description              | Billed  | Allowed | Plan Paid | You Owe
03/14/2025   | 99213 | Office visit             | $250.00 | $140.00 | $112.00   | $28.00
03/14/2025   | 36415 | Venipuncture             | $30.00  | $12.00  | $0.00     | $12.00
03/14/2025   | 36415 | Venipuncture             | $30.00  | $12.00  | $0.00     | $12.00
"""


# --- retrieval -------------------------------------------------------------
def test_kb_loads_terms_and_all_generator_codes():
    from data_gen.generate import CATALOG
    kb = default_kb()
    assert {c for c, _, _ in CATALOG} <= set(kb.by_code)
    assert all(s.text for s in kb.snippets)


def test_tokenize_folds_plurals_and_drops_stopwords():
    assert tokenize("The charges for my visits") == ["charge", "visit"]


def test_search_ranks_obvious_question_first():
    assert default_kb().search("I was charged twice for the same thing", k=1)[0][0].id == "term:duplicate_charge"


def test_search_kind_filter():
    hits = default_kb().search("Ketorolac injection", k=3, kind="code")
    assert hits[0][0].id == "code:J1885" and all(s.kind == "code" for s, _ in hits)


# --- data minimization -----------------------------------------------------
def test_request_never_contains_raw_text_or_pii():
    req, _, _ = build_request(BILL, "Hi, I'm DOE, JANE, member W123456789. Why do I owe $52.00?")
    payload = req.model_dump_json()
    for secret in ("DOE", "JANE", "W123456789", "CLEARPATH", "EXPLANATION OF BENEFITS"):
        assert secret not in payload, secret
    assert "[NAME_1]" in req.question and "[MEMBER_ID_1]" in req.question


@pytest.mark.parametrize("dataset", DEV_SETS)
def test_no_ground_truth_pii_in_request_on_dev_sets(dataset):
    """For every development bill, asking a question that names the patient must not put any
    ground-truth PII value into the model request."""
    from eval.run_eval import load_docs
    for d in load_docs(ROOT / "data" / dataset):
        names = [p["text"] for p in d["truth"]["pii"] if p["type"] == "NAME"]
        req, _, _ = build_request(d["text"], f"This is {names[0]}, why do I owe this?")
        payload = req.model_dump_json().lower()
        leaked = [p["text"] for p in d["truth"]["pii"] if len(p["text"]) >= 3 and p["text"].lower() in payload]
        assert not leaked, (d["id"], leaked)


def test_totals_flags_and_snippets():
    req, _, pr = build_request(BILL)
    assert req.totals.billed_amount == "310.00" and req.totals.patient_responsibility == "52.00"
    assert req.totals.adjustment == "146.00"
    ids = {s.id for s in req.snippets}
    assert {"code:99213", "code:36415", "term:allowed_amount", "term:adjustment", "term:deductible",
            "term:duplicate_charge", "term:eob"} <= ids
    assert any("possible duplicate" in f for f in req.flags)


def test_missing_values_make_totals_incomplete():
    req, _, _ = build_request(BILL.replace("| $112.00   |", "| --        |"))
    assert req.lines[0].insurer_paid is None
    assert req.totals.insurer_paid is None and "insurer_paid" in req.totals.incomplete_fields
    assert any("no insurance payment" in f for f in req.flags)


def test_unknown_code_matched_by_description():
    text = BILL.replace("| 36415 | Venipuncture", "| 99999 | Routine venipuncture blood draw")
    req, _, _ = build_request(text)
    line2 = req.lines[1]
    assert line2.snippet_id == "code:36415"
    assert any("matched" in f for f in req.flags)


# --- grounding -------------------------------------------------------------
def test_mock_answer_is_fully_grounded():
    req, _, _ = build_request(BILL, "How much do I owe?")
    ex = MockExplainer().generate(req)
    warnings, unsupported = check_grounding(ex, req)
    assert warnings == [] and unsupported == []
    assert "$52.00" in ex.answer


def test_grounding_flags_invented_numbers_and_citations():
    req, _, _ = build_request(BILL)
    ex = Explanation(summary="You owe $99.99 in total.", line_explanations=[LineExplanation(line=7, text="x")],
                     cited_snippets=["term:made_up"])
    warnings, unsupported = check_grounding(ex, req)
    assert unsupported == ["$99.99"]
    assert any("made_up" in w for w in warnings) and any("line 7" in w for w in warnings)


def test_placeholders_restored_only_after_checks():
    class Echo(MockExplainer):
        def generate(self, req):
            ex = super().generate(req)
            return ex.model_copy(update={"answer": f"Hello {req.question.split()[3].rstrip(',')}."})
    r = explain(BILL, "Hello, this is DOE, JANE, what do I owe?", explainer=Echo())
    assert "DOE" not in r.request.model_dump_json()
    assert "DOE" in r.explanation.answer or "JANE" in r.explanation.answer


# --- API ---------------------------------------------------------------------
def test_explain_endpoint():
    c = TestClient(app)
    r = c.post("/explain", json={"text": BILL, "question": "Is this a bill?"})
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "mock" and body["unsupported_amounts"] == []
    assert "W123456789" not in json.dumps(body["request"])


@pytest.mark.parametrize("payload", [{"text": "  "}, {"text": BILL, "question": "x" * 1001},
                                     {"text": BILL, "question": "a\x00b"}])
def test_explain_endpoint_validation(payload):
    assert TestClient(app).post("/explain", json=payload).status_code == 422


# --- Claude client wiring (offline: a fake client, no network, no cost) -------
class _FakeMessages:
    def __init__(self, response):
        self.response, self.kwargs = response, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def _fake_client(text: str, stop_reason: str = "end_turn"):
    resp = SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])
    msgs = _FakeMessages(resp)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


def test_claude_explainer_sends_only_the_request_and_parses_json():
    pytest.importorskip("anthropic")
    out = {"summary": "You owe $52.00.", "line_explanations": [{"line": 1, "text": "Office visit."}],
           "answer": "", "next_steps": [], "cited_snippets": ["code:99213"]}
    client, msgs = _fake_client(json.dumps(out))
    req, _, _ = build_request(BILL, "What do I owe, I'm DOE, JANE?")
    ex = ClaudeExplainer(client=client).generate(req)
    assert ex.summary == "You owe $52.00."
    k = msgs.kwargs
    assert k["model"] == "claude-opus-5-5" and k["fallbacks"] == "default"
    assert k["output_config"]["format"]["type"] == "json_schema"
    sent = json.dumps(k["messages"]) + k["system"]
    assert "DOE" not in sent and "W123456789" not in sent and "CLEARPATH" not in sent


def test_claude_explainer_refusal_raises():
    pytest.importorskip("anthropic")
    client, _ = _fake_client("", stop_reason="refusal")
    req, _, _ = build_request(BILL)
    with pytest.raises(RuntimeError):
        ClaudeExplainer(client=client).generate(req)
