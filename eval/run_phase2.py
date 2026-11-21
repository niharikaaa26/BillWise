"""Phase 2 evaluation: privacy of what is sent to the model, retrieval, grounding, totals.

    python -m eval.run_phase2                 # mock explainer, all datasets (free, deterministic)
    python -m eval.run_phase2 --live 5        # ALSO run Claude on 5 docs per dataset (costs money;
                                              # needs BILLWISE_LLM-independent Anthropic credentials)

Every number is computed at run time from the synthetic data. Results: eval/phase2_results.json.

Metrics
-------
privacy      For each document, is any ground-truth PII value (>= 3 chars) present in the JSON
             request that would be sent to the model? Each document is also asked a question that
             contains the patient's own name. For comparison, the same check is run on the
             Phase 1 redacted full text (what would leak if the redacted document were sent).
code_kb      Share of ground-truth line-item codes whose knowledge-base snippet is in the request
             (depends on the parser reading the code).
code_search  Description-only retrieval: BM25 over code snippets using the printed description
             (OCR typos included). hit@1 / hit@3 against the true code.
questions    36 hand-written questions (eval/phase2_questions.json): is the expected snippet the
             top hit (hit@1) or in the top 3 (hit@3)?
grounding    Dollar figures in generated answers that match no parsed value or computed total;
             cited snippets that were not provided.
totals       Do the locally computed totals (billed, patient responsibility) equal the sums of the
             ground-truth line items? Model-independent: measures parse -> totals.
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from billwise.explain import (MockExplainer, build_request, check_grounding,  # noqa: E402
                              explanation_texts, _DOLLAR)
from billwise.redact import Redactor  # noqa: E402
from billwise.retrieve import default_kb, tokenize  # noqa: E402
from eval.run_eval import DEFAULT_SETS, load_docs  # noqa: E402

QUESTIONS = ROOT / "eval" / "phase2_questions.json"


def _ratio(n: int, d: int) -> float | None:
    return None if d == 0 else n / d


def _pct(x: float | None) -> str:
    return "  n/a " if x is None else f"{100 * x:6.2f}%"


def _leaks(haystack: str, pii: list[dict]) -> list[str]:
    low = haystack.lower()
    return [p["text"] for p in pii if len(p["text"]) >= 3 and p["text"].lower() in low]


def _gold_sum(items: list[dict], field: str) -> Decimal | None:
    vals = [it[field] for it in items]
    return None if any(v is None for v in vals) else sum((Decimal(v) for v in vals), Decimal("0"))


def eval_questions() -> dict:
    kb = default_kb()
    qs = json.loads(QUESTIONS.read_text(encoding="utf-8"))["questions"]
    h1 = h3 = 0
    misses = []
    for q in qs:
        hits = [s.id for s, _ in kb.search(q["q"], k=3)]
        h1 += bool(hits) and hits[0] == q["expect"]
        h3 += q["expect"] in hits
        if not hits or hits[0] != q["expect"]:
            misses.append({"question": q["q"], "expected": q["expect"], "got": hits})
    return {"questions": len(qs), "hit_at_1": _ratio(h1, len(qs)), "hit_at_3": _ratio(h3, len(qs)),
            "failures": misses}


def eval_dataset(docs: list[dict], explainer, collect: bool = True) -> dict:
    kb = default_kb()
    redactor = Redactor()
    c = {k: 0 for k in ("docs_leaking_request", "values_leaked_request", "docs_leaking_redacted_text",
                        "values_leaked_redacted_text", "pii_values", "gold_codes", "gold_codes_in_kb",
                        "code_snippet_found", "search_n", "search_h1", "search_h3", "dollar_figures",
                        "unsupported", "bad_citations", "docs_clean_grounding", "billed_total_ok",
                        "resp_total_ok", "resp_total_n")}
    c.update(errors=0, generated=0, lines_explained=0, lines_total=0)
    fails = {"privacy": [], "code_search": [], "grounding": [], "totals": [], "errors": []}
    answers: list[dict] = []
    for d in docs:
        pii = d["truth"]["pii"]
        gold = d["truth"]["line_items"]
        names = [p["text"] for p in pii if p["type"] == "NAME"]
        question = f"Hi, this is {names[0]}. Why do I owe this much?" if names else "Why do I owe this much?"
        req, _, _ = build_request(d["text"], question, kb=kb, redactor=redactor)
        payload = req.model_dump_json()

        c["pii_values"] += len(pii)
        leaked = _leaks(payload, pii)
        c["values_leaked_request"] += len(leaked)
        c["docs_leaking_request"] += bool(leaked)
        if leaked and collect:
            fails["privacy"].append({"bill": d["id"], "leaked_in_request": leaked})
        red_leaked = _leaks(redactor.redact(d["text"]).redacted_text, pii)
        c["values_leaked_redacted_text"] += len(red_leaked)
        c["docs_leaking_redacted_text"] += bool(red_leaked)

        provided = {s.id for s in req.snippets}
        for g in gold:
            if g["code"] is None:
                continue
            c["gold_codes"] += 1
            snip = kb.lookup_code(g["code"])
            if snip is None:
                continue
            c["gold_codes_in_kb"] += 1
            c["code_snippet_found"] += snip.id in provided
            hits = [s.id for s, _ in kb.search(g["description"], k=3, kind="code")]
            c["search_n"] += 1
            c["search_h1"] += bool(hits) and hits[0] == snip.id
            c["search_h3"] += snip.id in hits
            if (not hits or hits[0] != snip.id) and collect:
                fails["code_search"].append({"bill": d["id"], "description": g["description"],
                                             "expected": snip.id, "got": hits})

        try:
            ex = explainer.generate(req)
        except Exception as exc:  # live runs: a refusal, timeout or API error must not stop the eval
            c["errors"] += 1
            if collect:
                fails["errors"].append({"bill": d["id"], "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        warnings, unsupported = check_grounding(ex, req)
        c["generated"] += 1
        c["lines_explained"] += len({l.line for l in ex.line_explanations} & {l.line for l in req.lines})
        c["lines_total"] += len(req.lines)
        if explainer.name != "mock":
            answers.append({"bill": d["id"], "question_sent": req.question,
                            "explanation": ex.model_dump(), "warnings": warnings})
        c["dollar_figures"] += sum(len(_DOLLAR.findall(t)) for t in explanation_texts(ex))
        c["unsupported"] += len(unsupported)
        bad = [x for x in ex.cited_snippets if x not in provided]
        c["bad_citations"] += len(bad)
        c["docs_clean_grounding"] += not unsupported and not bad
        if (unsupported or bad) and collect:
            fails["grounding"].append({"bill": d["id"], "unsupported": unsupported, "bad_citations": bad})

        gb = _gold_sum(gold, "billed_amount")
        ok_b = gb is not None and req.totals.billed_amount is not None and Decimal(req.totals.billed_amount) == gb
        c["billed_total_ok"] += ok_b
        gr = _gold_sum(gold, "patient_responsibility")
        if gr is not None:
            c["resp_total_n"] += 1
            ok_r = req.totals.patient_responsibility is not None and Decimal(req.totals.patient_responsibility) == gr
            c["resp_total_ok"] += ok_r
        else:
            ok_r = True
        if (not ok_b or not ok_r) and collect:
            fails["totals"].append({"bill": d["id"], "billed": [str(gb), req.totals.billed_amount],
                                    "patient_resp": [str(gr), req.totals.patient_responsibility]})
    n = len(docs)
    out = {
        "documents": n,
        "privacy": {
            "pii_values": c["pii_values"],
            "request_docs_with_leak": c["docs_leaking_request"],
            "request_values_leaked": c["values_leaked_request"],
            "request_leak_free_rate": _ratio(c["pii_values"] - c["values_leaked_request"], c["pii_values"]),
            "baseline_redacted_text_docs_with_leak": c["docs_leaking_redacted_text"],
            "baseline_redacted_text_values_leaked": c["values_leaked_redacted_text"],
            "baseline_redacted_text_leak_free_rate":
                _ratio(c["pii_values"] - c["values_leaked_redacted_text"], c["pii_values"]),
        },
        "code_kb": {"gold_codes": c["gold_codes"], "gold_codes_in_kb": c["gold_codes_in_kb"],
                    "snippet_in_request": _ratio(c["code_snippet_found"], c["gold_codes_in_kb"])},
        "code_search": {"lines": c["search_n"], "hit_at_1": _ratio(c["search_h1"], c["search_n"]),
                        "hit_at_3": _ratio(c["search_h3"], c["search_n"])},
        "grounding": {"model": explainer.name, "generated": c["generated"], "errors": c["errors"],
                      "lines_explained_rate": _ratio(c["lines_explained"], c["lines_total"]),
                      "dollar_figures": c["dollar_figures"],
                      "unsupported_figures": c["unsupported"], "bad_citations": c["bad_citations"],
                      "docs_fully_grounded": c["docs_clean_grounding"],
                      "docs_fully_grounded_rate": _ratio(c["docs_clean_grounding"], n)},
        "totals": {"billed_total_correct": _ratio(c["billed_total_ok"], n),
                   "patient_resp_total_correct": _ratio(c["resp_total_ok"], c["resp_total_n"]),
                   "patient_resp_docs_with_full_gold": c["resp_total_n"]},
    }
    if collect:
        out["failures"] = fails
    if answers:
        out["answers"] = answers
    return out


def print_dataset(name: str, r: dict, max_fail: int) -> None:
    p, g, t = r["privacy"], r["grounding"], r["totals"]
    print(f"\n##### {name}: {r['documents']} SYNTHETIC documents #####")
    print(f"  privacy: PII values {p['pii_values']}; leaked into model request: {p['request_values_leaked']}"
          f" ({p['request_docs_with_leak']} docs) -> leak-free {_pct(p['request_leak_free_rate'])}")
    print(f"           baseline if the Phase 1 redacted TEXT were sent: leaked {p['baseline_redacted_text_values_leaked']}"
          f" ({p['baseline_redacted_text_docs_with_leak']} docs) -> leak-free {_pct(p['baseline_redacted_text_leak_free_rate'])}")
    print(f"  code snippets in request: {_pct(r['code_kb']['snippet_in_request'])}"
          f" of {r['code_kb']['gold_codes_in_kb']} gold codes")
    print(f"  description-only code search: hit@1 {_pct(r['code_search']['hit_at_1'])},"
          f" hit@3 {_pct(r['code_search']['hit_at_3'])} ({r['code_search']['lines']} lines)")
    print(f"  grounding ({g['model']}): generated {g['generated']}/{r['documents']} (errors {g['errors']});"
          f" {g['dollar_figures']} dollar figures, {g['unsupported_figures']} unsupported,"
          f" {g['bad_citations']} bad citations; docs fully grounded {g['docs_fully_grounded']}/{r['documents']};"
          f" lines explained {_pct(g['lines_explained_rate'])}")
    print(f"  totals: billed correct {_pct(t['billed_total_correct'])};"
          f" patient responsibility correct {_pct(t['patient_resp_total_correct'])}"
          f" (of {t['patient_resp_docs_with_full_gold']} docs where every gold line has it)")
    for k, rows in r.get("failures", {}).items():
        if rows:
            print(f"  FAILURES {k}: {len(rows)}")
            for row in rows[:max_fail]:
                print("    - " + json.dumps(row, default=str)[:150])
            if len(rows) > max_fail:
                print(f"    ... {len(rows) - max_fail} more in phase2_results.json")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "eval" / "phase2_results.json")
    ap.add_argument("--max-failures", type=int, default=8)
    ap.add_argument("--live", type=int, default=0,
                    help="also run Claude on the first N docs of each dataset (costs money; off by default)")
    args = ap.parse_args()
    results: dict = {"note": "synthetic data only"}
    q = eval_questions()
    results["questions"] = q
    print("BillWise phase-2 evaluation")
    print("=" * 88)
    print(f"question retrieval: {q['questions']} questions; hit@1 {_pct(q['hit_at_1'])}, hit@3 {_pct(q['hit_at_3'])}")
    for row in q["failures"]:
        print(f"    - not top-1: {row['question']!r} expected {row['expected']} got {row['got']}")
    mock = MockExplainer()
    live = None
    if args.live:
        from billwise.explain import ClaudeExplainer
        live = ClaudeExplainer()
    results["datasets"] = {}
    live_answers: dict = {}
    for name, path in DEFAULT_SETS.items():
        if not path.exists():
            continue
        docs = load_docs(path)
        r = eval_dataset(docs, mock)
        if live:
            r["live"] = eval_dataset(docs[: args.live], live, collect=True)
            live_answers[name] = r["live"].pop("answers", [])
        results["datasets"][name] = r
        print_dataset(name, r, args.max_failures)
        if "live" in r:
            print_dataset(f"{name} (LIVE Claude, first {args.live})", r["live"], args.max_failures)
    if live:
        u = live.usage
        # list price for claude-opus-5-5: $4 / 1M input tokens, $20 / 1M output tokens
        cost = (u["input_tokens"] * 4 + u["output_tokens"] * 20) / 1_000_000
        results["live_usage"] = {**u, "estimated_cost_usd_at_list_price": round(cost, 4), "model": live.model}
        print(f"\nLIVE usage: {u['requests']} requests, {u['input_tokens']} input + {u['output_tokens']} output tokens"
              f" -> about ${cost:.2f} at list price")
        answers_path = args.out.with_name("phase2_live_answers.json")
        answers_path.write_text(json.dumps(live_answers, indent=2, default=str) + "\n", encoding="utf-8")
        print(f"live answers written to {answers_path}")
    args.out.write_text(json.dumps(results, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"\nresults written to {args.out}")


if __name__ == "__main__":
    main()
