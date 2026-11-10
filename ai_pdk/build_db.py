"""Stage 3 – Structured rule database.

Runs the LLM extraction for every target rule and normalises the answer into a
record that conforms to schema/rule.schema.json:
  * stable constraint ids  (<rule>#<n>)
  * layer GDS numbers from the manual's Drawn-Layer table, and the DRC-deck
    variable bound to the same GDS number (layer mapping by number, not by name)
  * provenance for every cited chunk: repo, commit, file, line span, SHA, URL
  * deterministic baseline numbers from the source row, to show which numbers the
    LLM used and which it deliberately dropped (e.g. values for n>=2)
Writes out/rules.json and out/rules.yaml.
"""
from __future__ import annotations

import json
import re
import subprocess

import yaml

from .config import OUT, PDK_REPO, SOURCES, TARGET_RULES
from .llm_extract import PROMPT_SHA, build_context, get_backend, load_chunks, render

NUM = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")


def git_commit(path):
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def layer_table(chunks):
    """Manual name -> (gds, datatype) from the Drawn Layer table (and any other 'GDS #' table)."""
    tbl = {}
    for c in chunks:
        if c["kind"] == "table_row" and c.get("header") and any("GDS" in h for h in c["header"]):
            h = c["header"]
            try:
                gi = next(i for i, x in enumerate(h) if "GDS" in x)
                di = gi + 1
                name = re.sub(r"\s*\(\d+\)\s*$", "", c["cells"][1]).strip()
                tbl.setdefault(name.lower(), (int(c["cells"][gi]), int(c["cells"][di])))
            except (ValueError, IndexError, StopIteration):
                pass
    return tbl


def deck_layer_by_gds(deck_layers):
    inv = {}
    for var, (l, d) in deck_layers.items():
        inv.setdefault((l, d), var)
    return inv


def build(deck_layers=None):
    chunks = load_chunks()
    by_id = {c["chunk_id"]: c for c in chunks}
    commit = git_commit(PDK_REPO)
    repo = SOURCES["design_manual"]["repo"]
    lt = layer_table(chunks)
    inv = deck_layer_by_gds(deck_layers or {})
    backend = get_backend()
    rules = []
    for rid in TARGET_RULES:
        mid, binding, ctx = build_context(rid, chunks)
        prompt, csha = render(rid, mid, binding, ctx)
        resp, meta = backend(rid, prompt, csha)
        primary = ctx[0]

        layers = []
        for L in resp["layers"]:
            gds = lt.get(L["name"].lower())
            deck = inv.get(tuple(gds)) if gds else None
            if deck is None:
                guess = L["name"].lower().replace(" ", "_")
                deck = guess if guess in (deck_layers or {}) else None
            layers.append({**L, "gds_layer": gds[0] if gds else None,
                           "gds_datatype": gds[1] if gds else None, "deck_name": deck})

        constraints = [{"id": f"{rid}#{i + 1}", **c} for i, c in enumerate(resp["constraints"])]

        cited = []
        for item in constraints + resp.get("exceptions", []) + resp.get("references", []):
            for e in item["evidence"]:
                if e["chunk_id"] not in cited:
                    cited.append(e["chunk_id"])
        prov = []
        for cid in cited:
            c = by_id.get(cid)
            if not c:  # unknown chunk id -> keep a stub so validation flags it
                prov.append({"chunk_id": cid, "kind": "note", "repo": repo, "commit": commit, "file": "?",
                             "line_start": 1, "line_end": 1, "sha256": "0" * 16, "url": repo, "section": None})
                continue
            prov.append({"chunk_id": cid, "kind": c["kind"], "repo": repo, "commit": commit, "file": c["file"],
                         "line_start": c["line_start"], "line_end": c["line_end"], "sha256": c["sha256"],
                         "url": f"{repo}/blob/{commit}/{c['file']}#L{c['line_start']}-L{c['line_end']}",
                         "section": c["section"]})

        value_cells = " ".join(primary["cells"][2:]) if primary.get("cells") else ""
        baseline = sorted({float(x) for x in NUM.findall(value_cells.replace("μ", " "))
                           if not re.search(rf"n\s*=\s*{x}\b|<=\s*{x}|{x}\s*<=", value_cells)})
        used = {c["value"] for c in constraints if c["value"] is not None}
        unused = [b for b in baseline if not any(abs(b - u) < 1e-9 for u in used)]

        rules.append({
            "rule_id": rid, "manual_rule_id": resp["manual_rule_id"], "template_binding": resp["template_binding"],
            "pdk": "gf180mcu", "section": primary["section"], "title": resp["title"], "rule_type": resp["rule_type"],
            "layers": layers, "constraints": constraints,
            "exceptions": resp.get("exceptions", []), "references": resp.get("references", []),
            "provenance": prov,
            "extraction": {"method": "llm", "backend": backend.name, "model": meta.get("model"),
                           "prompt_sha": PROMPT_SHA, "context_sha": csha, "stale": bool(meta.get("stale")),
                           "confidence": resp.get("confidence", 0.5), "notes": resp.get("notes", ""),
                           "baseline_numbers": baseline, "unused_numbers": unused},
            "review": {"status": "pending", "reviewer": None, "date": None, "comment": None},
        })
    OUT.mkdir(exist_ok=True)
    (OUT / "rules.json").write_text(json.dumps(rules, indent=1, ensure_ascii=False))
    (OUT / "rules.yaml").write_text(yaml.safe_dump(rules, sort_keys=False, allow_unicode=True, width=120))
    n_c = sum(len(r["constraints"]) for r in rules)
    print(f"[db] {len(rules)} rules, {n_c} constraints -> out/rules.json, out/rules.yaml (backend={backend.name})")
    return rules


if __name__ == "__main__":
    deck = json.loads((OUT / "deck_index.json").read_text())
    build(deck["layers"])
