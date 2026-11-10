"""Stage 5a – Database validation (no layout needed).

Checks every record in out/rules.json:
  SCHEMA      JSON-Schema conformance (schema/rule.schema.json)
  PROVENANCE  the cited file/lines still exist at the pinned commit and hash to the stored SHA
  QUOTE       every evidence quote is a verbatim substring of the cited chunk
  GROUNDING   every numeric constraint value appears in at least one of its own evidence quotes
  NOT_CODED   coded_expected=false is backed by an Appendix-B ('Rules not coded') citation
  OPERATOR    operator is consistent with the check kind
  LAYER_MAP   manual GDS number == GDS number of the deck variable the rule is mapped to
  STALE       the LLM recording was produced from the current context
"""
from __future__ import annotations

import hashlib
import json
import re

from jsonschema import Draft202012Validator

from .config import OUT, PDK_REPO, SCHEMA

OPS_OK = {
    "min_width": {">=", ">"}, "min_space": {">=", ">"}, "min_space_wide": {">=", ">"},
    "min_enclosure": {">=", ">"}, "min_area": {">=", ">"}, "min_density": {">=", ">"},
    "min_extension": {">=", ">"}, "min_gate_length": {">=", ">"}, "exact_size": {"=="},
    "forbidden": {"forbidden"},
}


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\\)", ")").replace("\\*", "*")).strip()


def nums(s: str):
    return {float(x) for x in re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)", s)}


def validate(rules=None, deck_layers=None):
    rules = rules or json.loads((OUT / "rules.json").read_text())
    deck_layers = deck_layers or json.loads((OUT / "deck_index.json").read_text())["layers"]
    chunks = {json.loads(l)["chunk_id"]: json.loads(l) for l in open(OUT / "doc_chunks.jsonl")}
    schema = json.loads(SCHEMA.read_text())
    v = Draft202012Validator(schema)
    results = []

    def add(rule, check, ok, detail):
        results.append({"rule_id": rule, "check": check, "ok": bool(ok), "detail": detail})

    for r in rules:
        rid = r["rule_id"]
        errs = sorted(v.iter_errors(r), key=lambda e: e.path)
        add(rid, "SCHEMA", not errs, "; ".join(f"{list(e.path)}: {e.message}" for e in errs[:3]) or "conforms")

        bad = []
        for p in r["provenance"]:
            f = PDK_REPO / p["file"]
            if not f.exists():
                bad.append(f"{p['chunk_id']}: file missing")
                continue
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            excerpt = "\n".join(lines[p["line_start"] - 1:p["line_end"]])
            if hashlib.sha256(excerpt.encode()).hexdigest()[:16] != p["sha256"]:
                bad.append(f"{p['chunk_id']}: hash mismatch")
        add(rid, "PROVENANCE", not bad, "; ".join(bad) or f"{len(r['provenance'])} source spans verified")

        bad_q, n_q = [], 0
        items = r["constraints"] + r["exceptions"] + r["references"]
        for it in items:
            for e in it["evidence"]:
                n_q += 1
                c = chunks.get(e["chunk_id"])
                if not c or norm(e["quote"]) not in norm(c["text"]):
                    bad_q.append(f"{e['chunk_id']}: '{e['quote'][:40]}'")
        add(rid, "QUOTE", not bad_q, "; ".join(bad_q) or f"{n_q} quotes found verbatim")

        bad_g = []
        for c in r["constraints"]:
            if c["value"] is None:
                continue
            found = set().union(*(nums(norm(e["quote"])) for e in c["evidence"]))
            if not any(abs(c["value"] - x) < 1e-9 for x in found):
                bad_g.append(f"{c['id']} value {c['value']} not in its quotes")
        add(rid, "GROUNDING", not bad_g, "; ".join(bad_g) or "all values grounded in cited text")

        bad_nc = []
        for c in r["constraints"]:
            if not c["coded_expected"]:
                cites = [e["chunk_id"] for e in c["evidence"]] + \
                        [e["chunk_id"] for x in r["exceptions"] if x["kind"] == "not_coded" for e in x["evidence"]]
                if not any(cid.startswith("70_not_coded_rules") for cid in cites):
                    bad_nc.append(c["id"])
        add(rid, "NOT_CODED", not bad_nc, ("unsupported: " + ", ".join(bad_nc)) if bad_nc else "ok")

        bad_o = [c["id"] for c in r["constraints"] if c["operator"] not in OPS_OK[c["check"]]]
        add(rid, "OPERATOR", not bad_o, ", ".join(bad_o) or "ok")

        bad_l = []
        for L in r["layers"]:
            if L["deck_name"] and L["gds_layer"] is not None:
                dl = deck_layers.get(L["deck_name"])
                if dl and (dl[0], dl[1]) != (L["gds_layer"], L["gds_datatype"]):
                    bad_l.append(f"{L['name']}: manual {L['gds_layer']}/{L['gds_datatype']} vs deck "
                                 f"{L['deck_name']}={dl[0]}/{dl[1]}")
            if L["deck_name"] is None:
                bad_l.append(f"{L['name']}: no deck layer")
        add(rid, "LAYER_MAP", not bad_l, "; ".join(bad_l) or "GDS numbers agree")

        add(rid, "STALE", not r["extraction"]["stale"],
            "recording matches current context" if not r["extraction"]["stale"] else "context changed since recording")

    (OUT / "validation.json").write_text(json.dumps(results, indent=1))
    fails = [x for x in results if not x["ok"]]
    print(f"[validate] {len(results)} checks, {len(fails)} failures")
    for f in fails:
        print("   FAIL", f["rule_id"], f["check"], f["detail"])
    return results


if __name__ == "__main__":
    validate()
