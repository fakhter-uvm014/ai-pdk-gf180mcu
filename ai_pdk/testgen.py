"""Stage 5b – Layout test generation, DRC execution and golden-result comparison.

Tests are generated from the structured rule DB (values come from out/rules.json,
not from this file):
  positive        geometry exactly at the limit   -> golden: no violation
  negative        geometry just past the limit    -> golden: violation
  boundary_probe  geometry on a qualifier edge where manual and deck may disagree
The *golden* outcome is derived from the manual's semantics (operator, qualifiers).
Each test is run through the real KLayout deck shipped with the PDK
(gf180mcu.drc or gf180mcu_density.drc) with the switches the constraint needs
(e.g. -rd metal_top=11K) and the violation count for the rule's deck id is read
from the .lyrdb report.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import klayout.db as kdb

from .config import DECK_DIR, OUT

UM = 1000  # dbu = 1 nm
DUALGATE, V5_XTOR, FRAME = (55, 0), (112, 1), (63, 63)
GENERATED = {"min_width", "min_space", "min_area", "exact_size", "min_enclosure", "min_density", "min_space_wide"}
REGULAR_SPACE = {}  # filled from DB: layer -> min_space value for the same domain


def box(cell, layer, x0, y0, w, h):
    cell.shapes(layer).insert(kdb.Box(round(x0 * UM), round(y0 * UM), round((x0 + w) * UM), round((y0 + h) * UM)))


def make_gds(path, build):
    ly = kdb.Layout()
    ly.dbu = 1.0 / UM
    top = ly.create_cell("TOP")
    L = lambda ld: ly.layer(*ld)
    build(top, L)
    ly.write(str(path))


def domain_layers(aw, top, L, extent):
    dom = aw.get("voltage_domain")
    if dom in ("5V", "6V"):
        box(top, L(DUALGATE), -2, -2, extent + 4, extent + 4)
    if dom == "5V":
        box(top, L(V5_XTOR), -2, -2, extent + 4, extent + 4)


def geometry(rule, con, variant):
    """Return a builder(top, L) for the constraint and variant, plus a human description."""
    prim = next(l for l in rule["layers"] if l["role"] == "primary")
    sec = next((l for l in rule["layers"] if l["role"] == "secondary"), None)
    P = (prim["gds_layer"], prim["gds_datatype"])
    v, chk, aw = con["value"], con["check"], con["applies_when"]
    neg = variant == "negative"

    if chk == "min_width":
        w = v - 0.01 if neg else v
        return (lambda t, L: (box(t, L(P), 0, 0, w, 5), domain_layers(aw, t, L, 5))), f"{prim['name']} line {w:.3f} x 5 um"
    if chk == "min_space":
        s = v - 0.01 if neg else v
        Q = (sec["gds_layer"], sec["gds_datatype"]) if sec else P   # cross-layer space, e.g. Poly2 -> COMP
        other = sec["name"] if sec else prim["name"]
        return (lambda t, L: (box(t, L(P), 0, 0, 1, 5), box(t, L(Q), 1 + s, 0, 1, 5), domain_layers(aw, t, L, 5))), \
            f"1x5 um {prim['name']} and 1x5 um {other} spaced {s:.3f} um"
    if chk == "exact_size":
        a = v + 0.01 if neg else v
        return (lambda t, L: box(t, L(P), 0, 0, a, a)), f"{prim['name']} square {a:.3f} um"
    if chk == "min_area":
        side = round(v ** 0.5, 3)
        w, h = (side - 0.01, side) if neg else (side, side)
        if not neg and w * h < v - 1e-9:
            w = h = side + 0.005
        return (lambda t, L: box(t, L(P), 0, 0, w, h)), f"{prim['name']} {w:.3f} x {h:.3f} um = {w * h:.4f} um2"
    if chk == "min_enclosure":
        # primary encloses secondary (e.g. Poly2 over Contact); inner cut uses its exact size 0.22/0.26
        S = (sec["gds_layer"], sec["gds_datatype"])
        cut = 0.22 if sec["name"] == "Contact" else 0.26
        e = v - 0.01 if neg else v
        return (lambda t, L: (box(t, L(P), 0, 0, cut + 2 * e, cut + 2 * e), box(t, L(S), e, e, cut, cut))), \
            f"{sec['name']} {cut} um enclosed by {prim['name']} by {e:.3f} um on all sides"
    if chk == "min_density":
        pct = {"positive": v + 5, "negative": v - 5, "boundary_probe": v}[variant]
        return (lambda t, L: (box(t, L(FRAME), 0, 0, 0.5, 0.5), box(t, L(FRAME), 99.5, 99.5, 0.5, 0.5),
                              box(t, L(P), 0, 10, 100, pct))), f"{prim['name']} covering {pct:.1f}% of a 100x100 um die"
    if chk == "min_space_wide":
        q = aw["wide_metal"]["value"]
        size = q if variant == "boundary_probe" else q + 2       # boundary: plate exactly at the qualifier
        s = v if variant == "positive" else v - 0.02              # still above the regular min space
        if variant == "negative_pair":                            # wide plate next to wide plate
            return (lambda t, L: (box(t, L(P), 0, 0, size, size), box(t, L(P), size + s, 0, size, size))), \
                f"two {size:g}x{size:g} um {prim['name']} plates spaced {s:.3f} um"
        return (lambda t, L: (box(t, L(P), 0, 0, 1, size), box(t, L(P), 1 + s, 0, size, size))), \
            f"1x{size:g} um {prim['name']} line spaced {s:.3f} um from a {size:g}x{size:g} um plate"
    raise ValueError(chk)


def golden(con, variant):
    """Expected outcome under the manual's semantics."""
    if variant == "positive":
        return False
    if variant in ("negative", "negative_pair"):
        return True
    if con["check"] == "min_density":       # exactly at the threshold
        return con["operator"] == ">"
    if con["check"] == "min_space_wide":    # plates exactly at the qualifier size
        q = con["applies_when"]["wide_metal"]
        return q["operator"] == ">="
    return False


def plan(rules, matches):
    mres = {m["rule_id"]: {c["constraint_id"]: c for c in m["constraints"]} for m in matches}
    tests = []
    for r in rules:
        for con in r["constraints"]:
            if con["check"] not in GENERATED or not con["coded_expected"] or con["clause"] != "main":
                continue
            if con["check"] == "min_enclosure" and con["value"] < 0.02:   # no on-grid 'just inside' variant
                continue
            m = mres[r["rule_id"]][con["id"]]
            observe = m.get("deck_rule_id") or r["rule_id"]
            deck = "gf180mcu_density.drc" if con["check"] == "min_density" else "gf180mcu.drc"
            sw = {"run_mode": "flat"}
            if "metal_top_thickness" in con["applies_when"]:
                sw["metal_top"] = con["applies_when"]["metal_top_thickness"]
            variants = ["positive", "negative"]
            if con["check"] == "min_space_wide":
                variants.append("negative_pair")
            if con["check"] in ("min_density", "min_space_wide") and m["status"] != "NOT_FOUND":
                variants.append("boundary_probe")
            for var in variants:
                build, desc = geometry(r, con, var)
                tid = f"{con['id'].replace('#', '_c')}_{var}"
                tests.append({"test_id": tid, "rule_id": r["rule_id"], "constraint_id": con["id"],
                              "variant": var, "description": desc, "deck": deck, "switches": sw,
                              "observe": observe, "static_status": m["status"],
                              "golden_violation": golden(con, var), "_build": build})
    return tests


def count_violations(lyrdb, rule_id):
    if not lyrdb.exists():
        return None
    root = ET.parse(lyrdb).getroot()
    n = 0
    for it in root.iter("item"):
        cat = (it.findtext("category") or "").strip("'\"")
        if cat == rule_id or (cat.startswith(rule_id) and cat[len(rule_id):][:1] in ("_",)):
            n += 1
    return n


def run_one(t, tdir):
    gds = tdir / f"{t['test_id']}.gds"
    rep = tdir / f"{t['test_id']}.lyrdb"
    make_gds(gds, t["_build"])
    cmd = ["klayout", "-b", "-r", str(DECK_DIR / t["deck"]), "-rd", f"input={gds}", "-rd", f"report={rep}", "-rd", "thr=1"]
    for k, v in t["switches"].items():
        cmd += ["-rd", f"{k}={v}"]
    # GDS files embed timestamps, so key on the geometry description + deck + switches instead
    key = hashlib.sha256(json.dumps([t["description"], t["deck"], t["switches"], t["observe"]]).encode()).hexdigest()
    stamp = tdir / f"{t['test_id']}.key"
    if not (rep.exists() and stamp.exists() and stamp.read_text() == key):   # re-run only when inputs changed
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        (tdir / f"{t['test_id']}.log").write_text(p.stdout[-4000:] + p.stderr[-4000:])
        stamp.write_text(key)
    n = count_violations(rep, t["observe"])
    obs = None if n is None else n > 0
    out = {k: v for k, v in t.items() if not k.startswith("_")}
    out.update({"observed_violations": n, "observed_violation": obs,
                "agree": obs == t["golden_violation"], "gds": gds.name, "report": rep.name,
                "command": " ".join(cmd[:4] + ["…"] + [f"-rd {k}={v}" for k, v in t["switches"].items()])})
    return out


def main(workers=2):
    rules = json.loads((OUT / "rules.json").read_text())
    matches = json.loads((OUT / "matches.json").read_text())
    tdir = OUT / "layout_tests"
    tdir.mkdir(parents=True, exist_ok=True)
    tests = plan(rules, matches)
    print(f"[tests] {len(tests)} generated; running KLayout {subprocess.run(['klayout', '-v'], capture_output=True, text=True).stdout.strip()}")
    with ThreadPoolExecutor(workers) as ex:
        res = list(ex.map(lambda t: run_one(t, tdir), tests))
    (OUT / "test_results.json").write_text(json.dumps(res, indent=1))
    for r in res:
        print(f"   {'OK ' if r['agree'] else 'XX '} {r['test_id']:34} golden={'FAIL' if r['golden_violation'] else 'pass'} "
              f"deck={r['observed_violations']}x {r['observe']}  ({r['description']})")
    print(f"[tests] {sum(r['agree'] for r in res)}/{len(res)} agree with golden")
    return res


if __name__ == "__main__":
    main()
