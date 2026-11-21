"""Generator integrity + end-to-end evaluation tests."""
import json
from pathlib import Path

import pytest

from data_gen.generate import generate
from eval import run_eval
from billwise.redact import RedactionResult

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "data" / "synthetic"
DEV2 = ROOT / "data" / "dev2"
DEV3 = ROOT / "data" / "dev3"
DEV4 = ROOT / "data" / "dev4"
DEV5 = ROOT / "data" / "dev5"
DEV6 = ROOT / "data" / "dev6"
DEV7 = ROOT / "data" / "dev7"
HELDOUT = ROOT / "data" / "heldout"


def test_generator_is_deterministic_and_matches_committed_data(tmp_path):
    generate(tmp_path, 48, 2025)
    for f in sorted(MAIN.glob("bill_*")):
        assert (tmp_path / f.name).read_text(encoding="utf-8") == f.read_text(encoding="utf-8"), f.name


def test_ground_truth_offsets_are_exact():
    files = sorted(MAIN.glob("bill_*.json")) + sorted(DEV2.glob("bill_*.json")) + sorted(DEV3.glob("bill_*.json")) + sorted(DEV4.glob("bill_*.json")) + sorted(DEV5.glob("bill_*.json")) + sorted(DEV6.glob("bill_*.json")) + sorted(DEV7.glob("bill_*.json")) + sorted(HELDOUT.glob("bill_*.json"))
    assert len(files) >= 40
    for jf in files:
        truth = json.loads(jf.read_text(encoding="utf-8"))
        text = jf.with_suffix(".txt").read_text(encoding="utf-8")
        for s in truth["pii"] + truth["allowed_redactions"]:
            assert text[s["start"]:s["end"]] == s["text"]


def test_dataset_covers_required_messy_cases():
    flags = set()
    for jf in MAIN.glob("bill_*.json"):
        flags.update(json.loads(jf.read_text(encoding="utf-8"))["messy"])
    assert {"multiline_item", "missing_field", "ocr_amount", "duplicate_charge"} <= flags


@pytest.fixture(scope="module")
def main_results():
    return run_eval.run(MAIN)


def test_eval_end_to_end_metrics_are_well_formed(main_results):
    pii, parsing = main_results["pii"], main_results["parsing"]
    assert pii["overall"]["gold"] > 300
    for v in list(pii["by_type"].values()) + [pii["overall"]]:
        for k in ("recall", "precision", "leak_free_rate"):
            assert v[k] is None or 0.0 <= v[k] <= 1.0
    assert parsing["gold_lines"] > 100
    assert all(0.0 <= a <= 1.0 for a in parsing["field_accuracy"].values())


def test_eval_regression_floors(main_results):
    """Guards against regressions on the development set. These are floors,
    not reported results; the real numbers come from eval/run_eval.py."""
    assert main_results["pii"]["overall"]["recall"] >= 0.95
    assert main_results["pii"]["overall"]["leak_free_rate"] >= 0.95
    assert main_results["parsing"]["field_accuracy_overall"] >= 0.95


def test_other_sets_regenerate_identically(tmp_path):
    generate(tmp_path / "dev2", 12, 777, 0.25, "dev2")
    generate(tmp_path / "dev3", 12, 4242, 0.2, "dev3")
    generate(tmp_path / "dev4", 12, 9001, 0.2, "dev4")
    generate(tmp_path / "dev5", 12, 31337, 0.2, "dev5")
    generate(tmp_path / "dev6", 12, 5150, 0.2, "dev6")
    generate(tmp_path / "dev7", 12, 6006, 0.2, "dev7")
    generate(tmp_path / "heldout", 12, 7117, 0.2, "heldout")
    for d in (DEV2, DEV3, DEV4, DEV5, DEV6, DEV7, HELDOUT):
        for f in sorted(d.glob("bill_*")):
            assert (tmp_path / d.name / f.name).read_text(encoding="utf-8") == f.read_text(encoding="utf-8"), f


@pytest.mark.parametrize("path", [DEV2, DEV3, DEV4, DEV5, DEV6, DEV7],
                         ids=["dev2", "dev3", "dev4", "dev5", "dev6", "dev7"])
def test_later_dev_set_regression_floors(path):
    r = run_eval.run(path)
    assert r["pii"]["overall"]["leak_free_rate"] >= 0.95
    assert r["parsing"]["field_accuracy_overall"] >= 0.95


def test_heldout_eval_runs():
    """No floor on purpose: held-out numbers are reported, not optimised."""
    r = run_eval.run(HELDOUT)
    assert r["dataset"]["documents"] == 12


def test_eval_detects_a_broken_redactor(monkeypatch):
    """Sanity check that the metric is computed, not assumed: a redactor that
    finds nothing must score 0 recall and leak every gold span."""
    monkeypatch.setattr(run_eval.Redactor, "redact", lambda self, text: RedactionResult(text, []))
    r = run_eval.run(MAIN)
    assert r["pii"]["overall"]["recall"] == 0.0
    assert len(r["pii"]["failures"]["leaks"]) == r["pii"]["overall"]["gold"]


def test_eval_detects_a_broken_parser(monkeypatch):
    from billwise.models import ParseResult
    monkeypatch.setattr(run_eval, "parse", lambda text: ParseResult(format="unknown", line_items=[]))
    r = run_eval.run(MAIN)
    assert r["parsing"]["line_recall"] == 0.0 and r["parsing"]["field_accuracy_overall"] == 0.0


