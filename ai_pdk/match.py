"""Stage 4b – Rule ↔ DRC-deck matching, coverage and discrepancy detection.

For each structured rule:
  1. candidate deck checks by ID (rule id + sub-clause letter + voltage suffix),
     excluding sub-ids that are themselves separate manual rules (PL.1 ≠ PL.1a);
     plus semantic nearest neighbours (TF-IDF over deck rule headers) as context
  2. per constraint: filter by applicability (voltage domain ↔ _3.3V/_5V suffix,
     MetalTop thickness ↔ `if METAL_TOP == "…"`), then pick the deck check whose
     measurement primitive is compatible with the check kind and carries the value
  3. discrepancy detectors:
       VALUE_MISMATCH           compatible check found, value differs
       NOT_FOUND                no deck check covers this constraint/domain
       BOUNDARY_MISMATCH        strict vs inclusive threshold (> vs >=), incl. wide-metal qualifiers
       UNEXPLAINED_EXCLUSION    deck excludes a marker layer no manual exception explains
       IDENTICAL_TO_OTHER_RULE  implementation is byte-identical to a different manual rule
       CODED_UNEXPECTEDLY       manual says not coded, deck implements it
       ORPHAN_DECK_SUBRULE      deck sub-rule id with no manual clause (info)
       DECK_VARIANT_DIVERGENCE  split rule_decks/*.drc differs from the main deck (info)
  4. classification: MISSING > PARTIAL > REVIEW > MATCH
"""
from __future__ import annotations

import json
import math
import re
import subprocess
from collections import Counter

from .config import DECK_DIR, DECK_REPO, OUT, SOURCES
from .deck_parse import parse_deck

DOMAIN_SUFFIX = {"3.3V": ["_3.3V", "_LV", ""], "5V": ["_5V", "_MV", ""], "6V": ["_5V", "_MV", ""]}
DOMAIN_SELECTORS = {"dualgate", "v5_xtor"}
COMPAT = {
    "min_width": {"width", "drc:width"},
    "min_gate_length": {"width", "space"},
    "min_space": {"space", "separation", "isolated"},
    "min_space_wide": {"space", "separation"},
    "min_enclosure": {"enclosing", "enclosed", "coverage", "overlap"},
    "exact_size": {"without_length"},
    "min_area": {"with_area"},
    "min_density": {"density"},
    "forbidden": {"coverage", "interacting"},
    "min_extension": {"enclosing"},
}
SYN = {"overlap": "enclos", "enclosing": "enclos", "enclosure": "enclos", "spacing": "space",
       "eol": "endofline", "end-of-line": "endofline", "min.": "min"}
STOP = {"the", "of", "a", "to", "is", "by", "on", "if", "in", "and", "or", "for", "um", "µm", "μm", "min", "minimum"}
STATUS_ORDER = ["MISSING", "PARTIAL", "REVIEW", "MATCH"]


def tokens(s):
    s = s.lower().replace("end-of-line", "endofline")
    out = []
    for t in re.findall(r"[a-z][a-z0-9\-]+", s):
        t = SYN.get(t, t)
        t = re.sub(r"(ing|s)$", "", t) if len(t) > 4 else t
        if t not in STOP:
            out.append(t)
    return out


class Tfidf:
    def __init__(self, docs):
        self.df = Counter(t for d in docs for t in set(tokens(d)))
        self.n = len(docs)

    def vec(self, s):
        tf = Counter(tokens(s))
        return {t: c * math.log((1 + self.n) / (1 + self.df.get(t, 0))) for t, c in tf.items()}

    def sim(self, a, b):
        va, vb = self.vec(a), self.vec(b)
        num = sum(va[t] * vb.get(t, 0) for t in va)
        den = math.sqrt(sum(x * x for x in va.values())) * math.sqrt(sum(x * x for x in vb.values()))
        return num / den if den else 0.0


def git_commit(path):
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def split_id(deck_id):
    m = re.match(r"^(.*?)(_(?:3\.3V|5V|6V|LV|MV))?$", deck_id)
    return m.group(1), (m.group(2) or "")


def output_expr(check):
    var_line = [s for s in check["statements"] if s["line"] < check["output_line"]]
    return re.sub(r"^\s*\w+\s*=\s*", "", var_line[-1]["code"]).strip() if var_line else ""


def diffsnip(a, b, w=45):
    i = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]), min(len(a), len(b)))
    lo = max(0, i - 15)
    return (("…" if lo else "") + a[lo:i + w], ("…" if lo else "") + b[lo:i + w])


def match_all(rules, deck, manual_ids):
    commit = git_commit(DECK_REPO)
    repo = SOURCES["drc_deck"]["repo"]
    url = lambda f, a, b=None: f"{repo}/blob/{commit}/{f}#L{a}" + (f"-L{b}" if b else "")
    checks = deck["checks"]
    tf = Tfidf([(c["header"] or {}).get("text", c["message"]) for c in checks])

    # implementation fingerprint -> deck ids, for duplicate detection
    fp = {}
    for c in checks:
        fp.setdefault((output_expr(c), tuple(c["context"])), set()).add(c["deck_rule_id"])

    # split decks for self-consistency
    split = {}
    for f in sorted((DECK_DIR / "rule_decks").glob("*.drc")):
        for c in parse_deck(f)[0]:
            split.setdefault(c["deck_rule_id"], []).append(c)

    results = []
    for r in rules:
        rid = r["rule_id"]
        rx = re.compile(rf"^{re.escape(rid)}([a-z]?)$")
        mine = []
        for c in checks:
            base, suf = split_id(c["deck_rule_id"])
            m = rx.match(base)
            if m and not (m.group(1) and (rid + m.group(1)) in manual_ids):
                mine.append(c)
        nc_markers = [n for n in deck["not_coded"] if rx.match(split_id(n["rule_id"])[0])
                      and not (rx.match(split_id(n["rule_id"])[0]).group(1)
                               and split_id(n["rule_id"])[0] in manual_ids)]
        query = r["title"] + " " + " ".join(c["description"] for c in r["constraints"])
        neighbours = sorted(((tf.sim(query, (c["header"] or {}).get("text", c["message"])), c) for c in checks
                             if any(L["deck_name"] in c["layers_used"] for L in r["layers"] if L["role"] == "primary")),
                            key=lambda x: -x[0])[:3]

        marker_ok = {L["deck_name"] for L in r["layers"] if L["role"] == "marker" and L["deck_name"]}
        explained = {x["marker_layer"].lower() for x in r["exceptions"] if x.get("marker_layer")}
        used_checks, cons_results, discrepancies = set(), [], []

        for con in r["constraints"]:
            aw = con["applies_when"]
            cands = list(mine)
            if "voltage_domain" in aw:
                sufs = DOMAIN_SUFFIX[aw["voltage_domain"]]
                has_suf = [c for c in cands if split_id(c["deck_rule_id"])[1]]
                cands = [c for c in cands if split_id(c["deck_rule_id"])[1] in sufs] if has_suf else cands
                if has_suf:
                    pref = [c for c in cands if split_id(c["deck_rule_id"])[1] == sufs[0]]
                    cands = pref or cands
            if "metal_top_thickness" in aw:
                t = aw["metal_top_thickness"]
                cands = [c for c in cands if not any("METAL_TOP" in x for x in c["context"])
                         or any(f'METAL_TOP == "{t}"' in x for x in c["context"])]
            compat = COMPAT[con["check"]]

            def vals(c):
                out = []
                for m in c["measures"]:
                    if m["op"] in compat and m["value"] is not None:
                        out.append(m["value"])
                if c.get("density") and "density" in compat:
                    out.append(c["density"]["threshold_pct"])
                return out

            scored = []
            for c in cands:
                v = vals(c)
                vmatch = con["value"] is None and (bool(v) or any(m["op"] in compat for m in c["measures"])) or \
                    (con["value"] is not None and any(abs(x - con["value"]) < 1e-9 for x in v))
                text = tf.sim(con["description"], (c["header"] or {}).get("text", c["message"]))
                scored.append((vmatch, bool(v) or (con["value"] is None and vmatch), text, c))
            scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)

            cr = {"constraint_id": con["id"], "clause": con["clause"], "description": con["description"],
                  "applies_when": aw, "expected": f"{con['operator']} {con['value']} {con['unit'] or ''}".strip(),
                  "coded_expected": con["coded_expected"], "flags": []}
            hit = scored[0] if scored and (scored[0][0] or scored[0][1]) else None
            if not con["coded_expected"]:
                implemented = hit and hit[0]
                cr["status"] = "CODED_UNEXPECTEDLY" if implemented else "NOT_CODED_OK"
                if implemented:
                    used_checks.add(hit[3]["deck_rule_id"])
                    cr["deck_rule_id"] = hit[3]["deck_rule_id"]
                    discrepancies.append({"type": "CODED_UNEXPECTEDLY", "constraint": con["id"],
                                          "detail": f"manual marks clause not coded, deck {hit[3]['deck_rule_id']} checks it"})
                cr["deck_markers"] = [n["rule_id"] for n in nc_markers]
                cons_results.append(cr)
                continue
            if not hit:
                cr["status"] = "NOT_FOUND"
                discrepancies.append({"type": "NOT_FOUND", "constraint": con["id"],
                                      "detail": f"no deck check for {con['description']}"
                                                + (f" ({', '.join(f'{k}={v}' for k, v in aw.items() if isinstance(v, str))})" if aw else "")})
                cons_results.append(cr)
                continue
            vmatch, _, text, c = hit
            used_checks.add(c["deck_rule_id"])
            cr.update({"deck_rule_id": c["deck_rule_id"], "deck_values": vals(c), "text_similarity": round(text, 2),
                       "deck_output_line": c["output_line"],
                       "deck_evidence": {"file": c["file"], "header_line": (c["header"] or {}).get("line"),
                                         "output_line": c["output_line"],
                                         "url": url(c["file"], (c["header"] or {}).get("line", c["output_line"]), c["output_line"]),
                                         "statements": c["statements"][-6:], "context": c["context"]}})
            cr["status"] = "IMPLEMENTED" if vmatch else "VALUE_MISMATCH"
            if not vmatch:
                discrepancies.append({"type": "VALUE_MISMATCH", "constraint": con["id"],
                                      "detail": f"manual {cr['expected']} vs deck {c['deck_rule_id']} values {vals(c)}"})
            # boundary: density
            if c.get("density") and con["check"] == "min_density":
                fail_if = c["density"]["fail_if"]
                if con["operator"] == ">" and fail_if == "<":
                    cr["flags"].append("BOUNDARY_MISMATCH")
                    discrepancies.append({"type": "BOUNDARY_MISMATCH", "constraint": con["id"],
                                          "detail": f"manual requires {con['operator']} {con['value']}% but deck only flags density {fail_if} "
                                                    f"{c['density']['threshold_pct']}% — exactly {con['value']}% passes the deck and fails the manual"})
            # boundary: wide-metal qualifier
            wq = aw.get("wide_metal")
            if wq:
                wl = [m for m in c["measures"] if m["op"] == "with_length" and m["value"] == wq["value"]
                      and m["args"].replace(" ", "").startswith("nil")]
                if wl and wq["operator"] == ">":
                    cr["flags"].append("BOUNDARY_MISMATCH")
                    discrepancies.append({"type": "BOUNDARY_MISMATCH", "constraint": con["id"],
                                          "detail": f"manual: wide = length & width > {wq['value']}um; deck treats polygons whose edges are all "
                                                    f">= {wq['value']}um as wide (not_interacting(edges.with_length(nil, {wq['value']}.um)))"})
            # exclusions
            unexpl = sorted(set(c["exclusion_layers"]) - DOMAIN_SELECTORS - marker_ok - explained)
            if unexpl:
                cr["flags"].append("UNEXPLAINED_EXCLUSION")
                discrepancies.append({"type": "UNEXPLAINED_EXCLUSION", "constraint": con["id"],
                                      "detail": f"{c['deck_rule_id']} excludes {', '.join(unexpl)}; no manual exception for this rule mentions it"})
            cr["explained_exclusions"] = sorted((set(c["exclusion_layers"]) - DOMAIN_SELECTORS) & (explained | marker_ok))
            # duplicates
            dup = {d for d in fp.get((output_expr(c), tuple(c["context"])), set()) if d != c["deck_rule_id"]
                   and not split_id(d)[0].startswith(rid)}
            if dup:
                cr["flags"].append("IDENTICAL_TO_OTHER_RULE")
                discrepancies.append({"type": "IDENTICAL_TO_OTHER_RULE", "constraint": con["id"],
                                      "detail": f"{c['deck_rule_id']} has the same implementation as {', '.join(sorted(dup))}: "
                                                f"`{output_expr(c)[:110]}`"})
            # split deck divergence
            for sc in split.get(c["deck_rule_id"], []):
                if output_expr(sc) and output_expr(sc) != output_expr(c) and tuple(sc["context"]) == tuple(c["context"]):
                    if not any(d["type"] == "DECK_VARIANT_DIVERGENCE" and c["deck_rule_id"] in d["detail"] for d in discrepancies):
                        discrepancies.append({"type": "DECK_VARIANT_DIVERGENCE", "constraint": con["id"], "info": True,
                                              "detail": f"{c['deck_rule_id']}: gf180mcu.drc uses `{diffsnip(output_expr(c), output_expr(sc))[0]}`, "
                                                        f"rule_decks/{sc['file'].split('/')[-1]} uses `{diffsnip(output_expr(c), output_expr(sc))[1]}`"})
            cons_results.append(cr)

        # orphans (deck sub-rules with no manual clause)
        for c in mine:
            if c["deck_rule_id"] not in used_checks:
                discrepancies.append({"type": "ORPHAN_DECK_SUBRULE", "info": True, "constraint": None,
                                      "detail": f"deck check {c['deck_rule_id']} (line {c['output_line']}) not mapped to any extracted clause"})
        # map 'not a DRC check' markers to not-coded clauses: deck sub-letters follow manual clause order,
        # so a not-coded clause takes the first marker whose letter comes after the previous implemented clause
        markers = sorted({split_id(n["rule_id"])[0] for n in nc_markers})
        letter = lambda d: (re.match(rf"^{re.escape(rid)}([a-z]?)", split_id(d)[0]).group(1) or "")
        prev, assigned = "", set()
        for cr in cons_results:
            if cr.get("deck_rule_id") and cr["status"] != "CODED_UNEXPECTEDLY":
                prev = max(prev, letter(cr["deck_rule_id"]))
            elif cr["status"] == "NOT_CODED_OK":
                pick = next((m for m in markers if m not in assigned and (letter(m) > prev or letter(m) == "")), None)
                if pick:
                    assigned.add(pick)
                    cr["deck_marker"] = pick
        for m in markers:
            if m not in assigned:
                discrepancies.append({"type": "ORPHAN_DECK_SUBRULE", "info": True, "constraint": None,
                                      "detail": f"deck declares '# rule {m} is not a DRC check' but no extracted manual clause corresponds to it"})
        seen, dd = set(), []
        for d in discrepancies:
            k = (d["type"], d["detail"])
            if k not in seen:
                seen.add(k)
                dd.append(d)
        discrepancies = dd

        mand = [x for x in cons_results if x["coded_expected"]]
        nf = [x for x in mand if x["status"] == "NOT_FOUND"]
        blocking = [d for d in discrepancies if not d.get("info") and d["type"] != "NOT_FOUND"]
        if mand and len(nf) == len(mand):
            status = "MISSING"
        elif nf:
            status = "PARTIAL"
        elif blocking or not mand:
            status = "REVIEW"
        else:
            status = "MATCH"
        reason = {
            "MISSING": "no deck implementation found for any mandatory clause",
            "PARTIAL": f"{len(mand) - len(nf)}/{len(mand)} mandatory clauses/domains implemented",
            "REVIEW": "implemented, but needs engineering judgement: " + ", ".join(sorted({d['type'] for d in blocking}))
                      if mand else "guideline not coded by design (manual Appendix B and deck agree) — confirm waiver",
            "MATCH": "all mandatory clauses implemented with consistent values and operators",
        }[status]
        results.append({
            "rule_id": rid, "title": r["title"], "status": status, "reason": reason,
            "constraints": cons_results, "discrepancies": discrepancies,
            "deck_candidates": sorted({c["deck_rule_id"] for c in mine}),
            "deck_not_coded_markers": [{"rule_id": n["rule_id"], "line": n["line"], "url": url(n["file"], n["line"])} for n in nc_markers],
            "semantic_neighbours": [{"deck_rule_id": c["deck_rule_id"], "similarity": round(s, 2),
                                     "header": (c["header"] or {}).get("text"), "url": url(c["file"], c["output_line"])}
                                    for s, c in neighbours],
            "manual_evidence": [{"chunk_id": p["chunk_id"], "file": p["file"], "lines": f"{p['line_start']}-{p['line_end']}",
                                 "url": p["url"]} for p in r["provenance"]],
            "deck_commit": commit,
        })
    return results


def main():
    rules = json.loads((OUT / "rules.json").read_text())
    deck = json.loads((OUT / "deck_index.json").read_text())
    chunks = [json.loads(l) for l in open(OUT / "doc_chunks.jsonl")]
    manual_ids = {(c["rule_id"] or "").replace("*", "") for c in chunks if c["kind"] == "table_row"}
    manual_ids |= {i.replace("Mn.", "M1.").replace("Vn.", "V1.") for i in manual_ids}
    res = match_all(rules, deck, manual_ids)
    (OUT / "matches.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
    cnt = Counter(r["status"] for r in res)
    print("[match] " + "  ".join(f"{k}={cnt.get(k, 0)}" for k in STATUS_ORDER))
    for r in res:
        print(f"   {r['rule_id']:6} {r['status']:8} {r['reason']}")
        for d in r["discrepancies"]:
            print(f"          - {d['type']}{' (info)' if d.get('info') else ''}: {d['detail'][:150]}")
    return res


if __name__ == "__main__":
    main()
