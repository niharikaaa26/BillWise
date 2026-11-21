"""Evaluate redaction and parsing against the synthetic ground truth.

    python -m eval.run_eval [--data data/synthetic] [--out eval/results.json]

Every number printed here is computed from the files in --data at run time.

PII matching
------------
* A gold span is *recalled* when a predicted span of the same type overlaps it.
* A predicted span is a *true positive* when it overlaps a gold span of the
  same type. It is *neutral* (excluded from precision) when it only overlaps an
  "allowed" span (business contact info such as an insurer's phone number).
  Everything else is a false positive.
* "Leak-free" means every character of the gold span is covered by some
  predicted span of any type, i.e. nothing of that value survives redaction.
* Exact-match numbers (same start, end and type) are also reported.

Line-item matching
------------------
Predicted lines are aligned to gold lines greedily by the number of agreeing
fields (minimum 3 of 7). Field accuracy = correct fields / gold lines, so a
missed line counts as wrong for every field.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from billwise.models import LINE_FIELDS, MONEY_FIELDS  # noqa: E402
from billwise.models import ParseResult  # noqa: E402
from billwise.parse import _finish, find_duplicates, parse, parse_anchored  # noqa: E402
from billwise.redact import PII_TYPES, Redactor  # noqa: E402

MIN_MATCH_FIELDS = 3


def _overlap(a0: int, a1: int, b0: int, b1: int) -> bool:
    return a0 < b1 and b0 < a1


def _ratio(n: int, d: int) -> float | None:
    return None if d == 0 else n / d


def _line_context(text: str, start: int, end: int) -> str:
    ls = text.rfind("\n", 0, start) + 1
    le = text.find("\n", end)
    return text[ls: le if le != -1 else len(text)].strip()


# ---------------------------------------------------------------------------
# PII
# ---------------------------------------------------------------------------
def eval_pii(docs: list[dict], redactor: Redactor) -> dict:
    per = {t: Counter() for t in PII_TYPES}
    missed, false_pos, leaks = [], [], []
    neutral = 0
    exact_tp = exact_pred = exact_gold = 0
    for d in docs:
        text, gold, allowed = d["text"], d["truth"]["pii"], d["truth"]["allowed_redactions"]
        res = redactor.redact(text)
        preds = res.spans
        covered = [False] * len(text)
        for p in preds:
            for i in range(p.start, p.end):
                covered[i] = True
        gold_keys = {(g["start"], g["end"], g["type"]) for g in gold}
        exact_gold += len(gold)
        exact_pred += len(preds)
        exact_tp += sum((p.start, p.end, p.type) in gold_keys for p in preds)
        for g in gold:
            t = g["type"]
            per[t]["gold"] += 1
            hit = any(p.type == t and _overlap(p.start, p.end, g["start"], g["end"]) for p in preds)
            per[t]["gold_recalled"] += hit
            any_hit = any(_overlap(p.start, p.end, g["start"], g["end"]) for p in preds)
            per[t]["gold_any_type"] += any_hit
            leak_free = all(covered[g["start"]:g["end"]])
            per[t]["leak_free"] += leak_free
            if not hit:
                other = [p.type for p in preds if _overlap(p.start, p.end, g["start"], g["end"])]
                missed.append({"bill": d["id"], "type": t, "text": g["text"],
                               "redacted_as": other or None,
                               "line": _line_context(text, g["start"], g["end"])})
            if not leak_free:
                leaked = "".join(ch for ch, c in zip(text[g["start"]:g["end"]], covered[g["start"]:g["end"]]) if not c)
                leaks.append({"bill": d["id"], "type": t, "text": g["text"], "unredacted_chars": leaked.strip()})
        for p in preds:
            per[p.type]["pred"] += 1
            if any(g["type"] == p.type and _overlap(p.start, p.end, g["start"], g["end"]) for g in gold):
                per[p.type]["pred_tp"] += 1
            elif any(_overlap(p.start, p.end, a["start"], a["end"]) for a in allowed) and \
                    not any(_overlap(p.start, p.end, g["start"], g["end"]) for g in gold):
                per[p.type]["pred_neutral"] += 1
                neutral += 1
            else:
                per[p.type]["pred_fp"] += 1
                false_pos.append({"bill": d["id"], "type": p.type, "text": text[p.start:p.end],
                                  "rule": p.source, "line": _line_context(text, p.start, p.end)})

    by_type = {}
    tot = Counter()
    for t in PII_TYPES:
        c = per[t]
        tot.update(c)
        by_type[t] = {
            "gold": c["gold"], "predicted": c["pred"], "neutral": c["pred_neutral"],
            "recall": _ratio(c["gold_recalled"], c["gold"]),
            "precision": _ratio(c["pred_tp"], c["pred"] - c["pred_neutral"]),
            "leak_free_rate": _ratio(c["leak_free"], c["gold"]),
            "recall_any_type": _ratio(c["gold_any_type"], c["gold"]),
        }
    overall = {
        "gold": tot["gold"], "predicted": tot["pred"], "neutral": neutral,
        "recall": _ratio(tot["gold_recalled"], tot["gold"]),
        "precision": _ratio(tot["pred_tp"], tot["pred"] - tot["pred_neutral"]),
        "leak_free_rate": _ratio(tot["leak_free"], tot["gold"]),
        "recall_any_type": _ratio(tot["gold_any_type"], tot["gold"]),
        "exact_match_recall": _ratio(exact_tp, exact_gold),
        "exact_match_precision": _ratio(exact_tp, exact_pred - neutral),  # neutral spans excluded, as above
    }
    return {"overall": overall, "by_type": by_type,
            "failures": {"missed": missed, "false_positives": false_pos, "leaks": leaks}}


# ---------------------------------------------------------------------------
# Line items
# ---------------------------------------------------------------------------
def _gold_value(field: str, v):
    if v is None:
        return None
    if field == "service_date":
        return date.fromisoformat(v)
    if field in MONEY_FIELDS:
        return Decimal(v)
    if field == "description":
        return " ".join(v.split()).casefold()
    return v


def _pred_value(field: str, item) -> object:
    v = getattr(item, field)
    if field == "description":
        return " ".join(v.split()).casefold()
    return v


def _align(gold: list[dict], pred: list) -> list[tuple[int, int, int]]:
    scores = []
    for gi, g in enumerate(gold):
        for pi, p in enumerate(pred):
            s = sum(_gold_value(f, g[f]) == _pred_value(f, p) for f in LINE_FIELDS)
            scores.append((s, -abs(gi - pi), gi, pi))
    scores.sort(reverse=True)
    used_g, used_p, pairs = set(), set(), []
    for s, _, gi, pi in scores:
        if s < MIN_MATCH_FIELDS or gi in used_g or pi in used_p:
            continue
        used_g.add(gi)
        used_p.add(pi)
        pairs.append((gi, pi, s))
    return sorted(pairs)


def eval_parse(docs: list[dict], text_key: str = "text", collect_failures: bool = True, parse_fn=None) -> dict:
    correct = Counter()
    n_gold = n_pred = n_matched = 0
    docs_perfect = 0
    by_layout = defaultdict(Counter)
    by_flag = defaultdict(Counter)
    field_fail, line_fail = [], []
    dup = Counter()
    formats = Counter()
    for d in docs:
        gold = d["truth"]["line_items"]
        res = (parse_fn or parse)(d[text_key])
        formats[f'{d["truth"]["layout"]}->{res.format}'] += 1
        pred = res.line_items
        pairs = _align(gold, pred)
        n_gold += len(gold)
        n_pred += len(pred)
        n_matched += len(pairs)
        doc_ok = len(pairs) == len(gold) == len(pred)
        lay = d["truth"]["layout"]
        by_layout[lay]["fields_total"] += len(gold) * len(LINE_FIELDS)
        for gi, pi, _ in pairs:
            for f in LINE_FIELDS:
                gv, pv = _gold_value(f, gold[gi][f]), _pred_value(f, pred[pi])
                ok = gv == pv
                correct[f] += ok
                by_layout[lay]["fields_correct"] += ok
                if not ok:
                    doc_ok = False
                    if collect_failures:
                        field_fail.append({"bill": d["id"], "layout": lay, "line": gi + 1, "field": f,
                                           "expected": gold[gi][f], "got": None if pv is None else str(getattr(pred[pi], f))})
        for flag in d["truth"]["messy"]:
            by_flag[flag]["docs"] += 1
        if doc_ok:
            docs_perfect += 1
            by_layout[lay]["docs_perfect"] += 1
            for flag in d["truth"]["messy"]:
                by_flag[flag]["docs_perfect"] += 1
        by_layout[lay]["docs"] += 1
        if collect_failures:
            mg = {gi for gi, _, _ in pairs}
            mp = {pi for _, pi, _ in pairs}
            for gi, g in enumerate(gold):
                if gi not in mg:
                    line_fail.append({"bill": d["id"], "layout": lay, "kind": "missed_line", "line": gi + 1,
                                      "expected": g})
            for pi, p in enumerate(pred):
                if pi not in mp:
                    line_fail.append({"bill": d["id"], "layout": lay, "kind": "spurious_line", "line": pi + 1,
                                      "got": json.loads(p.model_dump_json())})
        gold_dups = {tuple(x) for x in d["truth"]["duplicate_pairs"]}
        # map predicted duplicate pairs into gold indices via the alignment
        p2g = {pi: gi for gi, pi, _ in pairs}
        pred_dups = {tuple(sorted((p2g.get(i, -1 - i), p2g.get(j, -1 - j)))) for i, j in res.possible_duplicates}
        dup["gold"] += len(gold_dups)
        dup["pred"] += len(pred_dups)
        dup["tp"] += len(gold_dups & pred_dups)

    fields = {f: _ratio(correct[f], n_gold) for f in LINE_FIELDS}
    out = {
        "documents": len(docs),
        "documents_fully_correct": docs_perfect,
        "gold_lines": n_gold, "predicted_lines": n_pred, "matched_lines": n_matched,
        "line_recall": _ratio(n_matched, n_gold),
        "line_precision": _ratio(n_matched, n_pred),
        "field_accuracy": fields,
        "field_accuracy_overall": _ratio(sum(correct.values()), n_gold * len(LINE_FIELDS)),
        "by_layout": {k: {"docs": v["docs"], "docs_fully_correct": v["docs_perfect"],
                          "field_accuracy": _ratio(v["fields_correct"], v["fields_total"])}
                      for k, v in sorted(by_layout.items())},
        "by_messy_condition": {k: {"docs": v["docs"], "docs_fully_correct": v["docs_perfect"]}
                               for k, v in sorted(by_flag.items())},
        "duplicate_detection": {"gold_pairs": dup["gold"], "flagged_pairs": dup["pred"], "correct": dup["tp"],
                                "recall": _ratio(dup["tp"], dup["gold"]),
                                "precision": _ratio(dup["tp"], dup["pred"])},
        "detected_formats": dict(formats),
    }
    if collect_failures:
        out["failures"] = {"fields": field_fail, "lines": line_fail}
    return out


# ---------------------------------------------------------------------------
def fallback_only_parse(text: str) -> ParseResult:
    """Run only the line-oriented fallback, to measure how it copes with every layout."""
    items = _finish(parse_anchored(text, []))
    return ParseResult(format="anchored", line_items=items, possible_duplicates=find_duplicates(items))


def load_docs(data_dir: Path) -> list[dict]:
    docs = []
    for jf in sorted(data_dir.glob("bill_*.json")):
        truth = json.loads(jf.read_text(encoding="utf-8"))
        text = jf.with_suffix(".txt").read_text(encoding="utf-8")
        docs.append({"id": truth["id"], "text": text, "truth": truth})
    return docs


def run(data_dir: Path) -> dict:
    docs = load_docs(data_dir)
    if not docs:
        raise SystemExit(f"no bill_*.json files found in {data_dir}")
    redactor = Redactor()
    for d in docs:  # also check parsing still works after redaction
        d["redacted"] = redactor.redact(d["text"]).redacted_text
    return {
        "dataset": {"path": str(data_dir), "documents": len(docs),
                    "layouts": dict(Counter(d["truth"]["layout"] for d in docs)),
                    "note": "synthetic data only"},
        "pii": eval_pii(docs, redactor),
        "parsing": eval_parse(docs),
        "parsing_on_redacted_text": {
            k: v for k, v in eval_parse(docs, "redacted", collect_failures=False).items()
            if k in ("line_recall", "line_precision", "field_accuracy_overall", "documents_fully_correct")
        },
        # diagnostic: the fallback strategy alone (normally it runs only when nothing else matches)
        "fallback_only_parsing": {
            k: v for k, v in eval_parse(docs, collect_failures=False, parse_fn=fallback_only_parse).items()
            if k in ("line_recall", "line_precision", "field_accuracy_overall", "documents_fully_correct")
        },
    }


def _pct(x: float | None) -> str:
    return "  n/a " if x is None else f"{100 * x:6.2f}%"


def print_report(r: dict, max_fail: int = 60) -> None:
    ds = r["dataset"]
    print(f"BillWise phase-1 evaluation  ({ds['documents']} SYNTHETIC documents: {ds['layouts']})")
    print("=" * 88)
    p = r["pii"]
    print("PII REDACTION (overlap match, same type)")
    print(f"  {'type':<11}{'gold':>6}{'pred':>6}{'neutral':>9}{'recall':>10}{'precision':>11}{'leak-free':>11}{'any-type rec':>14}")
    for t, m in p["by_type"].items():
        print(f"  {t:<11}{m['gold']:>6}{m['predicted']:>6}{m['neutral']:>9}{_pct(m['recall']):>10}"
              f"{_pct(m['precision']):>11}{_pct(m['leak_free_rate']):>11}{_pct(m['recall_any_type']):>14}")
    o = p["overall"]
    print(f"  {'ALL':<11}{o['gold']:>6}{o['predicted']:>6}{o['neutral']:>9}{_pct(o['recall']):>10}"
          f"{_pct(o['precision']):>11}{_pct(o['leak_free_rate']):>11}{_pct(o['recall_any_type']):>14}")
    print(f"  exact-span match: recall {_pct(o['exact_match_recall'])}  precision {_pct(o['exact_match_precision'])}")
    print()
    q = r["parsing"]
    print("LINE-ITEM PARSING")
    print(f"  lines: gold {q['gold_lines']}, predicted {q['predicted_lines']}, matched {q['matched_lines']}"
          f"  -> recall {_pct(q['line_recall'])}, precision {_pct(q['line_precision'])}")
    print(f"  documents fully correct: {q['documents_fully_correct']}/{q['documents']}")
    print("  field accuracy (correct / gold lines):")
    for f, v in q["field_accuracy"].items():
        print(f"    {f:<24}{_pct(v)}")
    print(f"    {'ALL FIELDS':<24}{_pct(q['field_accuracy_overall'])}")
    print("  by layout:")
    for k, v in q["by_layout"].items():
        print(f"    {k:<16} docs fully correct {v['docs_fully_correct']:>2}/{v['docs']:<3} field acc {_pct(v['field_accuracy'])}")
    print("  by messy condition (docs fully correct / docs with condition):")
    for k, v in q["by_messy_condition"].items():
        print(f"    {k:<22} {v['docs_fully_correct']:>2}/{v['docs']}")
    dd = q["duplicate_detection"]
    print(f"  duplicate-charge flags: gold pairs {dd['gold_pairs']}, flagged {dd['flagged_pairs']}, correct {dd['correct']}"
          f"  (recall {_pct(dd['recall'])}, precision {_pct(dd['precision'])})")
    rr = r["parsing_on_redacted_text"]
    print(f"  parsing AFTER redaction: line recall {_pct(rr['line_recall'])}, field acc {_pct(rr['field_accuracy_overall'])},"
          f" docs fully correct {rr['documents_fully_correct']}/{q['documents']}")
    fb = r["fallback_only_parsing"]
    print(f"  diagnostic, fallback strategy ALONE: line recall {_pct(fb['line_recall'])},"
          f" field acc {_pct(fb['field_accuracy_overall'])}, docs fully correct {fb['documents_fully_correct']}/{q['documents']}")
    print()
    print("FAILURES")
    f = p["failures"]
    def show(title, rows, fmt):
        print(f"  {title}: {len(rows)}")
        for row in rows[:max_fail]:
            print("    - " + fmt(row))
        if len(rows) > max_fail:
            print(f"    ... {len(rows) - max_fail} more in results.json")
    show("PII missed (no same-type overlap)", f["missed"],
         lambda m: f"{m['bill']} {m['type']:<10} {m['text']!r:<34} redacted_as={m['redacted_as']}  | {m['line'][:60]}")
    show("PII leaked characters (any type)", f["leaks"],
         lambda m: f"{m['bill']} {m['type']:<10} {m['text']!r:<34} leaked={m['unredacted_chars']!r}")
    show("PII false positives", f["false_positives"],
         lambda m: f"{m['bill']} {m['type']:<10} {m['text']!r:<34} rule={m['rule']}  | {m['line'][:60]}")
    pf = q["failures"]
    show("parse field errors", pf["fields"],
         lambda m: f"{m['bill']} ({m['layout']}) line {m['line']} {m['field']}: expected {m['expected']!r} got {m['got']!r}")
    show("parse line errors", pf["lines"],
         lambda m: f"{m['bill']} ({m['layout']}) {m['kind']} #{m['line']}: "
                   f"{json.dumps(m.get('expected') or m.get('got'), default=str)[:110]}")


DEFAULT_SETS = {
    # the 4 layouts the parser/redactor were developed against
    "main": ROOT / "data" / "synthetic",
    # round-1 held-out layout; used to tune the round-2 fixes, so now development data
    "dev2": ROOT / "data" / "dev2",
    # round-2 held-out layout; used to tune the round-3 fixes, so now development data
    "dev3": ROOT / "data" / "dev3",
    # round-3 held-out layout; used to tune the round-4 rework, so now development data
    "dev4": ROOT / "data" / "dev4",
    # round-4 held-out layout; used to tune the round-5 fixes, so now development data
    "dev5": ROOT / "data" / "dev5",
    # round-5 held-out layout; used to tune the round-6 change, so now development data
    "dev6": ROOT / "data" / "dev6",
    # round-6 held-out layout; used to tune the round-7 changes, so now development data
    "dev7": ROOT / "data" / "dev7",
    # round-7 held-out layout, written before the round-7 changes and never used for tuning
    "heldout": ROOT / "data" / "heldout",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, action="append",
                    help="dataset directory (repeatable). Default: data/synthetic, dev2-dev7 and heldout")
    ap.add_argument("--out", type=Path, default=ROOT / "eval" / "results.json")
    ap.add_argument("--max-failures", type=int, default=60, help="failures to print per category")
    args = ap.parse_args()
    sets = {p.name: p for p in args.data} if args.data else {k: v for k, v in DEFAULT_SETS.items() if v.exists()}
    results = {}
    for name, path in sets.items():
        print(f"\n##### DATASET: {name} ({path}) #####")
        results[name] = run(path)
        print_report(results[name], args.max_failures)
    args.out.write_text(json.dumps(results, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"\nresults written to {args.out}")


if __name__ == "__main__":
    main()
