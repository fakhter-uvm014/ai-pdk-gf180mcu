"""Run after `python -m ai_pdk.run`:  python -m pytest -q tests/"""
import copy
import json

from ai_pdk.config import OUT
from ai_pdk.validate import validate


def load(name):
    return json.loads((OUT / name).read_text())


def test_db_validates_clean():
    assert all(v["ok"] for v in validate())


def test_validator_rejects_corrupted_records():
    rules = load("rules.json")
    bad = copy.deepcopy(rules)
    idx = {r["rule_id"]: i for i, r in enumerate(bad)}
    bad[idx["M1.1"]]["constraints"][0]["value"] = 0.25                                  # hallucinated value
    bad[idx["CO.1"]]["constraints"][0]["evidence"][0]["quote"] = "Min contact size 0.22"  # paraphrased quote
    bad[idx["PL.1"]]["provenance"][0]["line_start"] = 4                                 # shifted line span
    bad[idx["PL.3b"]]["exceptions"] = []                                                # drop Appendix-B support
    for c in bad[idx["PL.3b"]]["constraints"]:
        c["evidence"] = c["evidence"][:1]
    fails = {(v["rule_id"], v["check"]) for v in validate(bad) if not v["ok"]}
    assert {("M1.1", "GROUNDING"), ("CO.1", "QUOTE"), ("PL.1", "PROVENANCE"), ("PL.3b", "NOT_CODED")} <= fails


def test_expected_classifications():
    st = {m["rule_id"]: m["status"] for m in load("matches_reconciled.json")}
    assert st["MT.2b"] == "MISSING"
    assert st["MT.1"] == st["MT.2a"] == "PARTIAL"
    assert {st["M1.4"], st["M1.2b"], st["PL.5b"], st["PL.3b"]} == {"REVIEW"}
    assert st["M1.1"] == st["CO.1"] == "MATCH"


def test_every_disagreeing_layout_test_is_explained():
    for t in load("test_results.json"):
        if not t["agree"]:
            assert t["confirms"] in {"NOT_FOUND", "BOUNDARY_MISMATCH", "DYNAMIC_ONLY"}
