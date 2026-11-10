"""Stage 4a – Static parsing of the KLayout DRC deck (Ruby DSL).

For every `<var>.output("RULE", "message")` the parser records
  * the rule header comment (`# Rule X: ...`) and its line
  * the control-flow context (e.g. `if METAL_TOP == "6K"`, `if BEOL`)
  * the full statement chain: the output variable plus every intermediate
    variable it depends on, resolved recursively back to `polygons(L, D)` layers
  * measurement primitives with their values (width / space / separation /
    enclosing / with_area / without_length / density threshold …)
  * marker layers used as exclusions (`.not(x)`, `.outside(x)`, `.not_interacting(x)`)
    and as selectors (`.overlapping(x)`, `.inside(x)`, `.and(x)`)
It also records `# rule X is not a DRC check` markers.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import DECK_DIR, DECK_REPO, OUT

DECK_FILES = ["gf180mcu.drc", "gf180mcu_density.drc"]

ASSIGN = re.compile(r"^\s*([a-z_][A-Za-z0-9_]*)\s*=(?!=)\s*(.+?)\s*$")
OUTPUT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\.output\(\s*\"([^\"]+)\"\s*,\s*\"([^\"]*)\"")
HEADER = re.compile(r"^\s*#\s*Rule\s+([^:\s]+)\s*:\s*(.*)$")
NOTCHECK = re.compile(r"^\s*#\s*rule\s+(\S+)\s+is not a DRC check", re.I)
LAYER_DEF = re.compile(r"^\s*([a-z_][A-Za-z0-9_]*)\s*=\s*polygons\(\s*(\d+)\s*,\s*(\d+)\s*\)")
IDENT = re.compile(r"\b([a-z_][A-Za-z0-9_]*)\b")
MEASURE_OPS = ("width", "space", "separation", "enclosing", "enclosed", "overlap",
               "with_area", "without_length", "with_length", "notch", "isolated")
DRAWN = {"comp", "poly2", "contact", "metal1", "metal2", "metal3", "metal4", "metal5", "metaltop",
         "via1", "via2", "via3", "via4", "via5", "nplus", "pplus", "nwell", "dnwell", "lvpwell", "sab"}
EXCL_OPS = ("not", "outside", "not_interacting", "not_inside", "not_overlapping")
SEL_OPS = ("overlapping", "inside", "and", "interacting", "not_outside")
DENSITY = re.compile(r"\(\s*\(?\s*(\w+)\.area\s*/\s*CHIP\.area\s*\)?\s*\*\s*100\s*\)?\s*(<=|<|>=|>)\s*([\d.]+)")
# a measurement written as an expression inside .drc(...)  e.g. drc(width <= 0.34.um)
DRC_EXPR = re.compile(r"(width|space|enclosed\([^)]*\)|enclosing\([^)]*\))\s*(<=|<|>=|>)\s*([\d.]+)\.um")


def calls(expr: str):
    """Yield (method, argstring) for `.method(args)` with balanced parentheses."""
    for m in re.finditer(r"\.([a-z_]+)\(", expr):
        depth, i = 1, m.end()
        while i < len(expr) and depth:
            depth += {"(": 1, ")": -1}.get(expr[i], 0)
            i += 1
        yield m.group(1), expr[m.end():i - 1]


def top_level(args: str) -> str:
    out, depth = [], 0
    for ch in args:
        depth += ch == "("
        if depth == 0:
            out.append(ch)
        depth -= ch == ")"
    return "".join(out)


def first_um(args: str):
    m = re.search(r"(?<![\w.])(\d+(?:\.\d+)?)\.um", top_level(args))
    return float(m.group(1)) if m else None


def parse_deck(path: Path):
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    rel = str(path.resolve().relative_to(DECK_REPO.resolve()))
    defs: dict[str, list[tuple[int, str]]] = {}
    layers: dict[str, tuple[int, int]] = {}
    ctx: list[str] = []
    checks, notcoded = [], []
    header = None
    pending = []  # statement lines since the last header

    for ln, raw in enumerate(lines, 1):
        s = raw.strip()
        code = s.split("#", 1)[0].strip() if not s.startswith("#") else ""
        # ---- control flow ----
        if re.match(r"^if\b", code) and not re.search(r"\bthen\b.*\bend\b", code):
            ctx.append(code)
        elif re.match(r"^(elsif)\b", code) and ctx:
            ctx[-1] = code
        elif re.match(r"^else\b", code) and ctx:
            ctx[-1] = "else (not: " + ctx[-1] + ")"
        elif re.match(r"^(case|unless|while|begin)\b", code) or re.search(r"\bdo(\s*\|[^|]*\|)?\s*$", code):
            ctx.append(code)
        elif re.match(r"^end\b", code) and ctx:
            ctx.pop()
        m = LAYER_DEF.match(raw)
        if m:
            layers[m.group(1)] = (int(m.group(2)), int(m.group(3)))
        m = NOTCHECK.match(raw)
        if m:
            notcoded.append({"rule_id": m.group(1), "file": rel, "line": ln, "text": s,
                             "context": [c for c in ctx if not c.endswith("do")]})
            continue
        m = HEADER.match(raw)
        if m:
            header = {"rule_id": m.group(1), "text": m.group(2).strip(), "line": ln}
            pending = []
            continue
        m = ASSIGN.match(code) if code else None
        if m:
            defs.setdefault(m.group(1), []).append((ln, m.group(2)))
            pending.append((ln, raw.rstrip()))
        m = OUTPUT.match(code) if code else None
        if m:
            var, rid, msg = m.groups()
            stmts = resolve(var, ln, defs, layers)
            feats = features(stmts, layers)
            if not stmts and code:  # e.g. `poly2.output(...)` inside a density `if`
                feats = features([(ln, code)] + [(0, c) for c in ctx], layers)
            dens = [DENSITY.search(c) for c in ctx]
            dens = [d for d in dens if d]
            if dens:
                d = dens[-1]
                feats["density"] = {"layer": d.group(1), "fail_if": d.group(2), "threshold_pct": float(d.group(3))}
            checks.append({
                "deck_rule_id": rid,
                "message": msg,
                "file": rel,
                "output_line": ln,
                "header": header if header and rid.startswith(header["rule_id"].split("_")[0]) else header,
                "context": [c for c in ctx if not c.endswith("do")],
                "statements": [{"line": l, "code": c} for l, c in sorted(set(stmts))],
                **feats,
            })
    return checks, notcoded, layers


def resolve(var, before_line, defs, layers, depth=0, seen=None):
    """Collect the assignment lines `var` depends on (most recent definition before `before_line`)."""
    seen = seen if seen is not None else set()
    if depth > 8 or var in seen or var in layers:
        return []
    seen.add(var)
    cands = [d for d in defs.get(var, []) if d[0] < before_line]
    if not cands:
        return []
    ln, expr = cands[-1]
    out = [(ln, f"{var} = {expr}")]
    for ident in set(IDENT.findall(re.sub(r"\"[^\"]*\"", "", expr))):
        if ident in defs and ident != var:
            out += resolve(ident, ln, defs, layers, depth + 1, seen)
    return out


def features(stmts, layers):
    measures, excl, sel, used = [], set(), set(), set()
    for _, code in stmts:
        expr = code.split("=", 1)[1] if "=" in code and not code.lstrip().startswith("if") else code
        for ident in IDENT.findall(expr):
            if ident in layers:
                used.add(ident)
        for meth, args in calls(expr):
            if meth in MEASURE_OPS:
                v = first_um(args)
                if meth in ("with_length",) and v is None:
                    v2 = re.findall(r"(\d+(?:\.\d+)?)\.um", args)
                    v = float(v2[0]) if v2 else None
                measures.append({"op": meth, "value": v, "args": args.strip()})
            arg0 = args.strip().split(",")[0].strip()
            if meth in EXCL_OPS and arg0 in layers and arg0 not in DRAWN:
                excl.add(arg0)
            if meth in SEL_OPS and arg0 in layers and arg0 not in DRAWN:
                sel.add(arg0)
        for m in DRC_EXPR.finditer(expr):
            measures.append({"op": "drc:" + m.group(1).split("(")[0], "cmp": m.group(2), "value": float(m.group(3))})
        # `A.not(B)` with drawn layers = "A must be fully covered by B" (zero enclosure / forbidden region)
        for m in re.finditer(r"\b(\w+)(?:\.outside\(\w+\))?\.not(?:_inside)?\(\s*(\w+(?:\.or\(\w+\))?)\s*\)", expr):
            a, b = m.group(1), m.group(2)
            if a in DRAWN and b.split(".")[0] in DRAWN:
                measures.append({"op": "coverage", "value": 0.0, "args": f"{a} not covered by {b}"})
    return {"measures": measures, "layers_used": sorted(used),
            "exclusion_layers": sorted(excl), "selector_layers": sorted(sel)}


def parse_all():
    allc, alln, layers = [], [], {}
    for f in DECK_FILES:
        c, n, l = parse_deck(DECK_DIR / f)
        allc += c
        alln += n
        layers.update(l)
    return {"checks": allc, "not_coded": alln, "layers": {k: list(v) for k, v in layers.items()}}


def main():
    OUT.mkdir(exist_ok=True)
    deck = parse_all()
    (OUT / "deck_index.json").write_text(json.dumps(deck, indent=1))
    print(f"[deck] {len(deck['checks'])} output checks, {len(deck['not_coded'])} 'not a DRC check' markers, "
          f"{len(deck['layers'])} layer definitions")
    return deck


if __name__ == "__main__":
    main()
