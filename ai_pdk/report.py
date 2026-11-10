"""Stage 6 – Engineer review and reporting.

Reconciles static matching with the layout-test outcomes, then writes
  out/report.html   self-contained review page (architecture, summary, per-rule evidence,
                    test matrix, limitations, approval checklist)
  out/summary.md    the same summary as Markdown
  out/review.csv    one row per rule for sign-off (status, reviewer, decision, comment)
"""
from __future__ import annotations

import csv
import html
import json
from collections import Counter
from datetime import date

from .config import OUT, SOURCES

ORDER = ["MISSING", "PARTIAL", "REVIEW", "MATCH"]
E = html.escape

LIMITATIONS = [
    "Scope is 20 rules from four sections (Poly2, Contact, Metal1/Via1 instantiated from Metaln/Vian, MetalTop). The ID-level scan of the whole manual is a coverage hint, not a validation.",
    "The LLM responses used in this run were produced by Claude from the rendered prompts and replayed from data/llm_responses/session.json. The live Anthropic backend is wired in but was not exercised here (no API key in the build environment). Every value is still verified against the source text by the validator, so a hallucinated number or quote fails the run.",
    "The deck is pinned to google/globalfoundries-pdk-libs-gf180mcu_fd_pr @ 9f992d5 (2023-05-31). The successor repo no longer ships the deck under rules/klayout/drc, so the findings must be re-checked against the deck version actually used in your flow (e.g. the open_pdks / volare build).",
    "Static deck analysis is pattern-based (Ruby DSL parsed with regexes plus variable-dependency resolution). It understands the idioms used in this deck, not arbitrary Ruby.",
    "Polarity (NMOS/PMOS) of PL.2 values is matched by value only; the deck's N/P selection logic is not checked.",
    "Layout tests are minimal single-feature cells. They prove whether a check fires on a clean, isolated case; they don't exercise hierarchy, angled geometry, or interactions with other rules. Clause-level tests for EOL / adjacent-edge enclosure (CO.6 i–ii, V1.3 i–ii) are not generated yet.",
    "Golden results come from the extracted manual semantics, not from foundry sign-off decks. A disagreement means the deck and the manual (as extracted) disagree; an engineer decides which one is wrong.",
    "Results were obtained with KLayout 0.28.16. The wide-to-wide M1.2b behaviour depends on KLayout's two-layer separation semantics and should be re-tested on the KLayout version used in production.",
]


def reconcile(matches, tests):
    by_rule = {m["rule_id"]: m for m in matches}
    for t in tests:
        m = by_rule[t["rule_id"]]
        con = next(c for c in m["constraints"] if c["constraint_id"] == t["constraint_id"])
        if t["agree"]:
            t["explanation"] = "deck behaves as the manual requires"
            t["confirms"] = None
            continue
        if con["status"] == "NOT_FOUND":
            t["confirms"], t["explanation"] = "NOT_FOUND", "confirms static finding: no deck check for this clause/domain"
        elif t["variant"] == "boundary_probe" and "BOUNDARY_MISMATCH" in con.get("flags", []):
            t["confirms"], t["explanation"] = "BOUNDARY_MISMATCH", "confirms static finding: deck and manual disagree exactly at the threshold"
        else:
            t["confirms"] = "DYNAMIC_ONLY"
            t["explanation"] = "new finding: the deck check exists and carries the right value, but does not fire on this layout"
            m["discrepancies"].append({"type": "DYNAMIC_MISMATCH", "constraint": t["constraint_id"],
                                       "detail": f"layout test {t['test_id']} ({t['description']}): manual expects a violation, "
                                                 f"{t['observe']} reports {t['observed_violations']}"})
            if m["status"] == "MATCH":
                m["status"] = "REVIEW"
            if m["status"] == "REVIEW":
                m["reason"] = "implemented, but needs engineering judgement: " + ", ".join(sorted(
                    {d["type"] for d in m["discrepancies"] if not d.get("info") and d["type"] != "NOT_FOUND"}))
    for m in matches:
        mt = [t for t in tests if t["rule_id"] == m["rule_id"]]
        m["tests"] = {"run": len(mt), "agree": sum(t["agree"] for t in mt)}
    return matches, tests


# ----------------------------------------------------------------------------- HTML

ARCH_SVG = """<svg viewBox="0 0 1080 300" role="img" aria-label="AI-PDK pipeline: six stages from documentation to engineer review" class="arch">
<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" class="ah"/></marker></defs>
{boxes}
{arrows}
<text x="540" y="292" text-anchor="middle" class="cap">Every arrow carries provenance: chunk ids → file:line@commit → SHA-256, re-verified at stage 5</text>
</svg>"""

STAGES = [
    ("1", "Docs ingest", ["Design Manual", "RST + CSV tables", "line-exact chunks", "SHA per chunk"]),
    ("2", "LLM extraction", ["context per rule", "JSON out, cites", "chunk ids only", "replay | live API"]),
    ("3", "Rule database", ["JSON Schema", "rules.json/.yaml", "GDS layer ↔", "deck variable"]),
    ("4", "Deck analysis", ["parse Ruby DSL", "ID + TF-IDF +", "value matching", "8 detectors"]),
    ("5", "Validation", ["schema, quotes,", "provenance", "generated GDS", "KLayout vs golden"]),
    ("6", "Review", ["4-way status", "evidence links", "traceability", "review.csv sign-off"]),
]


def arch_svg():
    w, gap, x0, y = 160, 22, 12, 40
    boxes, arrows = [], []
    for i, (n, title, lines) in enumerate(STAGES):
        x = x0 + i * (w + gap)
        boxes.append(f'<g><rect x="{x}" y="{y}" width="{w}" height="190" rx="6" class="stage"/>'
                     f'<text x="{x + 14}" y="{y + 30}" class="num">{n}</text>'
                     f'<text x="{x + 34}" y="{y + 30}" class="st">{E(title)}</text>'
                     f'<line x1="{x + 14}" y1="{y + 44}" x2="{x + w - 14}" y2="{y + 44}" class="rule"/>'
                     + "".join(f'<text x="{x + 14}" y="{y + 72 + j * 26}" class="sl">{E(t)}</text>' for j, t in enumerate(lines))
                     + "</g>")
        if i < len(STAGES) - 1:
            ax = x + w
            arrows.append(f'<line x1="{ax + 2}" y1="{y + 95}" x2="{ax + gap - 3}" y2="{y + 95}" class="arr" marker-end="url(#ah)"/>')
    # feedback loop 5 -> 2 (stale / failed grounding triggers re-extraction)
    x5 = x0 + 4 * (w + gap) + w / 2
    x2 = x0 + 1 * (w + gap) + w / 2
    arrows.append(f'<path d="M{x5},{y + 190} v22 H{x2} v-18" class="loop" marker-end="url(#ah)"/>'
                  f'<text x="{(x5 + x2) / 2}" y="{y + 228}" text-anchor="middle" class="cap">failed grounding / stale context → re-extract</text>')
    return ARCH_SVG.format(boxes="".join(boxes), arrows="".join(arrows))


def pill(s):
    return f'<span class="pill p-{s.lower()}">{s}</span>'


def render_html(rules, matches, tests, validation, deck_meta, coverage):
    cnt = Counter(m["status"] for m in matches)
    rmap = {r["rule_id"]: r for r in rules}
    vfail = [v for v in validation if not v["ok"]]
    n_con = sum(len(r["constraints"]) for r in rules)
    agree = sum(t["agree"] for t in tests)
    findings = [(m, d) for m in sorted(matches, key=lambda m: ORDER.index(m["status"]))
                for d in m["discrepancies"] if not d.get("info")]
    info = [(m, d) for m in matches for d in m["discrepancies"] if d.get("info")]

    def tile(label, val, sub=""):
        return f'<div class="tile"><div class="tv">{val}</div><div class="tl">{label}</div><div class="ts">{sub}</div></div>'

    rows = []
    for m in sorted(matches, key=lambda m: (ORDER.index(m["status"]), m["rule_id"])):
        r = rmap[m["rule_id"]]
        cons = []
        for c in m["constraints"]:
            dk = c.get("deck_rule_id") or c.get("deck_marker") or "—"
            ev = c.get("deck_evidence")
            dlink = f'<a href="{E(ev["url"])}">{E(dk)} · L{ev["output_line"]}</a>' if ev else E(dk)
            st = c["status"].replace("_", " ").lower()
            flags = " ".join(f'<span class="flag">{E(f)}</span>' for f in c.get("flags", []))
            aw = ", ".join(f"{k}={v}" if isinstance(v, str) else f"{k}: {v.get('dimension')} {v.get('operator')} {v.get('value')}"
                           for k, v in c["applies_when"].items())
            cons.append(f"<tr><td class='mono'>{E(c['constraint_id'])}</td><td>{E(c['clause'])}</td><td>{E(c['description'])}"
                        f"<div class='aw'>{E(aw)}</div></td><td class='mono'>{E(c['expected'])}</td>"
                        f"<td class='mono'>{dlink}<div class='aw'>{E(', '.join(map(str, c.get('deck_values', []))))}</div></td>"
                        f"<td><span class='cs cs-{c['status'].lower()}'>{E(st)}</span> {flags}</td></tr>")
        src = "".join(f'<li><a href="{E(p["url"])}">{E(p["file"].split("/")[-1])}:{p["line_start"]}'
                      f'{"-" + str(p["line_end"]) if p["line_end"] != p["line_start"] else ""}</a> <span class="aw">{E(p["section"] or "")}</span></li>'
                      for p in r["provenance"])
        quotes = "".join(f'<li><span class="mono">{E(c["id"])}</span> “{E(e["quote"])}” <span class="aw">[{E(e["chunk_id"])}]</span></li>'
                         for c in r["constraints"] for e in c["evidence"][:1])
        exc = "".join(f'<li><b>{E(x["kind"].replace("_", " "))}</b>{" · " + E(x["marker_layer"]) if x.get("marker_layer") else ""}: {E(x["text"])}</li>'
                      for x in r["exceptions"]) or "<li class='aw'>none extracted</li>"
        disc = "".join(f'<li><span class="flag{" info" if d.get("info") else ""}">{E(d["type"])}</span> {E(d["detail"])}</li>'
                       for d in m["discrepancies"]) or "<li class='aw'>none</li>"
        neigh = "".join(f'<li><a href="{E(n["url"])}">{E(n["deck_rule_id"])}</a> sim {n["similarity"]} · {E(n["header"] or "")}</li>'
                        for n in m["semantic_neighbours"])
        tst = "".join(f'<li>{"✓" if t["agree"] else "✗"} <span class="mono">{E(t["variant"])}</span> {E(t["description"])} → '
                      f'golden {"violation" if t["golden_violation"] else "clean"}, deck {t["observed_violations"]}× {E(t["observe"])}</li>'
                      for t in tests if t["rule_id"] == m["rule_id"])
        vrow = [v for v in validation if v["rule_id"] == m["rule_id"]]
        vtxt = " ".join(f'<span class="vchk {"ok" if v["ok"] else "bad"}" title="{E(v["detail"])}">{v["check"]}</span>' for v in vrow)
        rows.append(f"""
<section class="rule" id="r-{E(m['rule_id'].replace('.', '-'))}">
 <header><div class="rid mono">{E(m['rule_id'])}</div><div class="rt"><div class="rtt">{E(r['title'])}</div>
 <div class="aw">manual {E(r['manual_rule_id'])}{' (n=1)' if r['template_binding'] else ''} · §{E(r['section'])} · {len(r['constraints'])} constraints · tests {m['tests']['agree']}/{m['tests']['run']} agree</div></div>{pill(m['status'])}</header>
 <p class="reason">{E(m['reason'])}</p>
 <div class="scroll"><table class="ct"><thead><tr><th>id</th><th>clause</th><th>manual requirement</th><th>value</th><th>deck check</th><th>result</th></tr></thead><tbody>{''.join(cons)}</tbody></table></div>
 <div class="cols">
  <div><h4>Discrepancies</h4><ul>{disc}</ul><h4>Exceptions from the manual</h4><ul>{exc}</ul></div>
  <div><h4>Manual evidence</h4><ul>{quotes}</ul><h4>Source spans (pinned commit)</h4><ul class="src">{src}</ul></div>
 </div>
 <details><summary>Layout tests, nearest deck rules, validation checks</summary>
  <div class="cols"><div><h4>Layout tests</h4><ul>{tst or "<li class='aw'>no generated test for this rule</li>"}</ul></div>
  <div><h4>Semantic neighbours (TF-IDF)</h4><ul>{neigh}</ul><h4>DB validation</h4><p>{vtxt}</p></div></div>
 </details>
</section>""")

    ftable = "".join(f"<tr><td>{pill(m['status'])}</td><td class='mono'><a href='#r-{E(m['rule_id'].replace('.', '-'))}'>{E(m['rule_id'])}</a></td>"
                     f"<td><span class='flag'>{E(d['type'])}</span></td><td>{E(d['detail'])}</td></tr>" for m, d in findings)
    itable = "".join(f"<li><span class='mono'>{E(m['rule_id'])}</span> <span class='flag info'>{E(d['type'])}</span> {E(d['detail'])}</li>" for m, d in info)

    tm = "".join(f"<tr class='{'' if t['agree'] else 'bad'}'><td class='mono'>{E(t['test_id'])}</td><td>{E(t['variant'])}</td>"
                 f"<td>{E(t['description'])}</td><td class='mono'>{E(' '.join(f'{k}={v}' for k, v in t['switches'].items() if k != 'run_mode') or 'default')}</td>"
                 f"<td>{'violation' if t['golden_violation'] else 'clean'}</td><td class='mono'>{t['observed_violations']}× {E(t['observe'])}</td>"
                 f"<td>{'✓' if t['agree'] else '✗'}</td><td>{E(t['explanation'])}</td></tr>" for t in tests)
    lim = "".join(f"<li>{E(x)}</li>" for x in LIMITATIONS)

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>GF180MCU Rule–Deck Consistency</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Sans+Condensed:wght@600&display=swap" rel="stylesheet">
<style>
:root {{
  /* layout: one reading column of report sections; rule cards are review units, scanned top-down by severity */
  --ink:#16202b; --muted:#5b6876; --paper:#f6f7f5; --panel:#ffffff; --line:#d9dee2; --accent:#1d5a8c;
  --ok:#2e7d4f; --warn:#a86a00; --rev:#6b47b0; --bad:#b3261e;
  --display:"IBM Plex Sans Condensed","Arial Narrow",sans-serif; --body:"IBM Plex Sans",system-ui,sans-serif; --mono:"IBM Plex Mono",ui-monospace,monospace;
  color-scheme: light;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --ink:#e4e8ec; --muted:#97a3af; --paper:#11161c; --panel:#18202a; --line:#2b3644; --accent:#6aa8dc;
  --ok:#5cbf86; --warn:#e0a53c; --rev:#a98ae6; --bad:#ef6d63; color-scheme: dark; }} }}
:root[data-theme="dark"] {{ --ink:#e4e8ec; --muted:#97a3af; --paper:#11161c; --panel:#18202a; --line:#2b3644; --accent:#6aa8dc;
  --ok:#5cbf86; --warn:#e0a53c; --rev:#a98ae6; --bad:#ef6d63; color-scheme: dark; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--paper); color:var(--ink); font:15px/1.55 var(--body); }}
main {{ max-width:1180px; margin:0 auto; padding:28px 20px 80px; display:grid; gap:34px; }}
h1,h2,h3 {{ font-family:var(--display); text-wrap:balance; margin:0; letter-spacing:.01em; }}
h1 {{ font-size:34px; line-height:1.15; }} h2 {{ font-size:22px; margin-bottom:12px; }} h4 {{ margin:14px 0 6px; font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }}
p {{ max-width:75ch; margin:6px 0; }} a {{ color:var(--accent); }} a:focus-visible, summary:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}
.mono {{ font-family:var(--mono); font-size:13px; }} .aw {{ color:var(--muted); font-size:12.5px; }}
.lede {{ color:var(--muted); max-width:85ch; }}
.meta {{ display:flex; flex-wrap:wrap; gap:8px 22px; font-size:13px; color:var(--muted); margin-top:10px; }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }}
.tile {{ background:var(--panel); border:1px solid var(--line); border-radius:6px; padding:14px 16px; }}
.tv {{ font:600 30px/1 var(--display); font-variant-numeric:tabular-nums; }} .tl {{ font-weight:600; margin-top:6px; }} .ts {{ color:var(--muted); font-size:12.5px; }}
.bar {{ display:flex; height:14px; border-radius:3px; overflow:hidden; border:1px solid var(--line); margin-top:12px; }}
.bar span {{ display:block; }}
.legend {{ display:flex; gap:16px; flex-wrap:wrap; font-size:13px; margin-top:8px; }}
.pill {{ display:inline-block; font:600 11.5px/1 var(--mono); letter-spacing:.04em; padding:5px 8px; border-radius:3px; color:#fff; white-space:nowrap; }}
.p-match {{ background:var(--ok); }} .p-partial {{ background:var(--warn); }} .p-review {{ background:var(--rev); }} .p-missing {{ background:var(--bad); }}
.flag {{ font:500 11px/1 var(--mono); padding:3px 6px; border-radius:3px; border:1px solid var(--rev); color:var(--rev); white-space:nowrap; }}
.flag.info {{ border-color:var(--muted); color:var(--muted); }}
.cs {{ font:500 12px var(--mono); }} .cs-implemented,.cs-not_coded_ok {{ color:var(--ok); }} .cs-not_found,.cs-value_mismatch,.cs-coded_unexpectedly {{ color:var(--bad); }}
.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:6px; padding:18px 20px; }}
.scroll {{ overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; font-size:13.5px; }}
th {{ text-align:left; font-weight:600; color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.05em; border-bottom:1px solid var(--line); padding:6px 8px; }}
td {{ border-bottom:1px solid var(--line); padding:7px 8px; vertical-align:top; }}
tr.bad td {{ background:color-mix(in srgb, var(--bad) 8%, transparent); }}
.rule {{ background:var(--panel); border:1px solid var(--line); border-left:4px solid var(--line); border-radius:6px; padding:16px 18px; }}
.rule:has(.p-missing) {{ border-left-color:var(--bad); }} .rule:has(.p-partial) {{ border-left-color:var(--warn); }}
.rule:has(.p-review) {{ border-left-color:var(--rev); }} .rule:has(.p-match) {{ border-left-color:var(--ok); }}
.rule header {{ display:flex; gap:14px; align-items:flex-start; }} .rid {{ font-size:18px; font-weight:500; min-width:62px; }}
.rt {{ flex:1; }} .rtt {{ font-weight:600; }} .reason {{ margin:10px 0; }}
.rules {{ display:grid; gap:14px; }}
.cols {{ display:grid; grid-template-columns:1fr 1fr; gap:22px; }} @media (max-width:760px) {{ .cols {{ grid-template-columns:1fr; }} .rule header {{ flex-wrap:wrap; }} }}
ul {{ margin:0; padding-left:18px; }} li {{ margin:3px 0; }}
.src li {{ font-family:var(--mono); font-size:12.5px; }}
details {{ margin-top:10px; border-top:1px dashed var(--line); padding-top:8px; }} summary {{ cursor:pointer; color:var(--accent); font-weight:500; }}
.vchk {{ font:500 11px var(--mono); padding:2px 5px; border-radius:3px; margin-right:4px; display:inline-block; }}
.vchk.ok {{ color:var(--ok); border:1px solid var(--ok); }} .vchk.bad {{ color:#fff; background:var(--bad); }}
.arch {{ width:100%; min-width:900px; height:auto; }}
.arch .stage {{ fill:var(--panel); stroke:var(--line); stroke-width:1.2; }} .arch .num {{ font:600 18px var(--display); fill:var(--accent); }}
.arch .st {{ font:600 15px var(--body); fill:var(--ink); }} .arch .sl {{ font:13px var(--body); fill:var(--muted); }}
.arch .rule {{ stroke:var(--line); }} .arch .arr,.arch .loop {{ stroke:var(--accent); stroke-width:1.6; fill:none; }} .arch .loop {{ stroke-dasharray:4 3; }}
.arch .ah {{ fill:var(--accent); }} .arch .cap {{ font:12px var(--body); fill:var(--muted); }}
code {{ font-family:var(--mono); font-size:12.5px; background:color-mix(in srgb, var(--accent) 9%, transparent); padding:1px 4px; border-radius:3px; }}
pre {{ font:12.5px/1.5 var(--mono); background:var(--panel); border:1px solid var(--line); border-radius:6px; padding:14px; overflow-x:auto; margin:0; }}
.sign td:nth-child(n+4) {{ color:var(--muted); }}
@media (prefers-reduced-motion: reduce) {{ * {{ transition:none !important; }} }}
</style></head>
<body><main>

<section>
 <h1>GF180MCU rule–deck consistency: 20 design rules checked against the KLayout DRC deck</h1>
 <p class="lede">An AI-assisted pipeline extracted 20 rules from the open-source GF180MCU Design Manual, stored them as schema-validated records with line-level provenance, matched them against the shipped KLayout deck, and ran {len(tests)} generated layout tests through that deck. Every finding links to the manual line and the deck line it rests on.</p>
 <div class="meta"><span>Manual: <a href="{SOURCES['design_manual']['repo']}">google/gf180mcu-pdk</a> @ <span class="mono">{E(deck_meta['manual_commit'][:7])}</span></span>
 <span>Deck: <a href="{SOURCES['drc_deck']['repo']}">globalfoundries-pdk-libs-gf180mcu_fd_pr</a> @ <span class="mono">{E(deck_meta['deck_commit'][:7])}</span> (gf180mcu.drc, gf180mcu_density.drc)</span>
 <span>DRC engine: KLayout 0.28.16</span><span>Generated {date.today().isoformat()}</span></div>
</section>

<section>
 <div class="tiles">
  {tile("rules extracted", len(rules), f"{n_con} constraints, {sum(len(r['exceptions']) for r in rules)} exceptions")}
  {tile("MATCH", cnt.get('MATCH', 0), "consistent")}
  {tile("REVIEW", cnt.get('REVIEW', 0), "implemented, judgement needed")}
  {tile("PARTIAL", cnt.get('PARTIAL', 0), "a domain or clause missing")}
  {tile("MISSING", cnt.get('MISSING', 0), "no implementation")}
  {tile("layout tests", f"{agree}/{len(tests)}", "agree with manual golden")}
  {tile("DB checks", f"{len(validation) - len(vfail)}/{len(validation)}", "schema, provenance, grounding")}
 </div>
 <div class="bar" aria-hidden="true">{''.join(f'<span class="p-{s.lower()}" style="width:{100 * cnt.get(s, 0) / len(matches):.1f}%"></span>' for s in ORDER)}</div>
 <div class="legend">{''.join(f'<span>{pill(s)} {cnt.get(s, 0)}</span>' for s in ORDER)}</div>
</section>

<section class="panel">
 <h2>Findings for engineer review</h2>
 <p class="aw">Blocking discrepancies, most severe first. Each was found statically and, where a test exists, reproduced by running the deck.</p>
 <div class="scroll"><table><thead><tr><th>status</th><th>rule</th><th>type</th><th>detail</th></tr></thead><tbody>{ftable}</tbody></table></div>
 <h4>Informational</h4><ul>{itable}</ul>
</section>

<section>
 <h2>Architecture</h2>
 <div class="scroll panel">{arch_svg()}</div>
 <div class="cols" style="margin-top:14px">
  <div><h4>Classification rule</h4><ul>
   <li>{pill('MISSING')} no deck check for any mandatory clause</li>
   <li>{pill('PARTIAL')} some mandatory clauses or domains (voltage, MetalTop thickness) have no check</li>
   <li>{pill('REVIEW')} all implemented, but a detector fired: value or boundary mismatch, unexplained marker exclusion, implementation identical to another rule, dynamic test failure; or a guideline that is not coded by design and needs a waiver</li>
   <li>{pill('MATCH')} every mandatory clause implemented with the same value and a compatible operator</li></ul></div>
  <div><h4>Guards against LLM error</h4><ul>
   <li>The model cites <code>chunk_id</code>s only; file, line span and hash are attached by the pipeline.</li>
   <li>Every quote must appear verbatim in its chunk; every number must appear in its own quote.</li>
   <li>Not-coded claims must cite Appendix B. Recordings are keyed to a context hash, so a manual change marks them stale.</li>
   <li>The validator was itself tested with four corrupted records (wrong value, paraphrased quote, shifted line, missing Appendix-B support); all four were rejected.</li></ul></div>
 </div>
</section>

<section>
 <h2>Rule-by-rule evidence</h2>
 <div class="rules">{''.join(rows)}</div>
</section>

<section class="panel">
 <h2>Layout tests: generated GDS, real deck, golden comparison</h2>
 <p class="aw">Geometry values come from the rule DB. Golden = what the manual requires. Rows with ✗ are disagreements; the last column ties each to the static finding it confirms.</p>
 <div class="scroll"><table><thead><tr><th>test</th><th>variant</th><th>layout</th><th>switches</th><th>golden</th><th>deck</th><th></th><th>interpretation</th></tr></thead><tbody>{tm}</tbody></table></div>
</section>

<section class="panel">
 <h2>Demo: reproduce this report</h2>
<pre>git clone --depth 1 https://github.com/google/gf180mcu-pdk third_party_gf180mcu-pdk
git clone https://github.com/google/globalfoundries-pdk-libs-gf180mcu_fd_pr third_party_gf180mcu_fd_pr
git -C third_party_gf180mcu_fd_pr checkout 9f992d5
pip install klayout jsonschema pyyaml          # plus the KLayout application for DRC runs
python -m ai_pdk.run                           # ingest → extract → DB → deck → match → validate → tests → report
AI_PDK_BACKEND=anthropic python -m ai_pdk.run  # re-extract live with the Anthropic API</pre>
 <h4>Whole-manual ID coverage (hint only)</h4>
 <p>{coverage['manual_ids']} rule IDs in the manual tables; {coverage['with_deck_id']} have a deck output with the same base ID, {coverage['not_coded_marker']} are marked "not a DRC check" in the deck, {coverage['no_id_match']} have no ID-level match (many are template rules like <code>Mn.x</code>, pad/guideline sections, or use different naming).</p>
</section>

<section class="panel">
 <h2>Limitations</h2><ul>{lim}</ul>
</section>

<section class="panel">
 <h2>Approval</h2>
 <p class="aw">Sign-off is recorded in <code>out/review.csv</code> (one row per rule) and written back to each record's <code>review</code> field.</p>
 <div class="scroll"><table class="sign"><thead><tr><th>rule</th><th>status</th><th>proposed action</th><th>reviewer</th><th>decision</th></tr></thead><tbody>
 {''.join(f"<tr><td class='mono'>{E(m['rule_id'])}</td><td>{pill(m['status'])}</td><td>{E(action(m))}</td><td>—</td><td>pending</td></tr>" for m in sorted(matches, key=lambda m: (ORDER.index(m['status']), m['rule_id'])))}
 </tbody></table></div>
</section>
</main></body></html>"""


def action(m):
    s = m["status"]
    types = {d["type"] for d in m["discrepancies"] if not d.get("info")}
    if s == "MISSING":
        return "Implement the check in the deck, or record a waiver."
    if s == "PARTIAL":
        return "Add the missing domain branch (e.g. METAL_TOP == \"11K\") to the deck."
    if "DYNAMIC_MISMATCH" in types:
        return "Fix the deck check so it fires on the failing layout; add the test to regression."
    if "BOUNDARY_MISMATCH" in types:
        return "Decide whether the manual's strict inequality or the deck's inclusive threshold is intended; align one."
    if "IDENTICAL_TO_OTHER_RULE" in types or "UNEXPLAINED_EXCLUSION" in types:
        return "Confirm the related/unrelated distinction and the marker-layer exclusions with the deck owner."
    if s == "REVIEW":
        return "Approve waiver: guideline, not coded by design."
    return "Approve."


def coverage_hint():
    chunks = [json.loads(l) for l in open(OUT / "doc_chunks.jsonl")]
    deck = json.loads((OUT / "deck_index.json").read_text())
    import re
    ids = {c["rule_id"].replace("*", "") for c in chunks if c["kind"] == "table_row" and c["rule_id"]
           and not c["file"].endswith("70_not_coded_rules_187.csv")}
    base = lambda s: re.sub(r"_(3\.3V|5V|6V|LV|MV)$", "", s)
    dids = {base(c["deck_rule_id"]) for c in deck["checks"]}
    nids = {base(n["rule_id"]) for n in deck["not_coded"]}
    w = {i for i in ids if i in dids or any(d.startswith(i) and d[len(i):][:1].isalpha() for d in dids)}
    n = {i for i in ids - w if i in nids}
    return {"manual_ids": len(ids), "with_deck_id": len(w), "not_coded_marker": len(n), "no_id_match": len(ids - w - n)}


def main():
    import subprocess
    from .config import DECK_REPO, PDK_REPO
    rules = json.loads((OUT / "rules.json").read_text())
    matches = json.loads((OUT / "matches.json").read_text())
    tp = OUT / "test_results.json"
    tests = json.loads(tp.read_text()) if tp.exists() else []   # --skip-tests: static results only
    validation = json.loads((OUT / "validation.json").read_text())
    matches, tests = reconcile(matches, tests)
    (OUT / "matches_reconciled.json").write_text(json.dumps(matches, indent=1, ensure_ascii=False))
    (OUT / "test_results.json").write_text(json.dumps(tests, indent=1))
    meta = {"manual_commit": subprocess.check_output(["git", "-C", str(PDK_REPO), "rev-parse", "HEAD"], text=True).strip(),
            "deck_commit": subprocess.check_output(["git", "-C", str(DECK_REPO), "rev-parse", "HEAD"], text=True).strip()}
    cov = coverage_hint()
    (OUT / "report.html").write_text(render_html(rules, matches, tests, validation, meta, cov))
    with open(OUT / "review.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rule_id", "status", "reason", "blocking_findings", "proposed_action", "reviewer", "decision", "date", "comment"])
        for m in sorted(matches, key=lambda m: (ORDER.index(m["status"]), m["rule_id"])):
            w.writerow([m["rule_id"], m["status"], m["reason"],
                        " | ".join(d["type"] + ": " + d["detail"] for d in m["discrepancies"] if not d.get("info")),
                        action(m), "", "pending", "", ""])
    cnt = Counter(m["status"] for m in matches)
    md = [f"# GF180MCU rule–deck consistency (prototype)\n",
          f"Manual @ {meta['manual_commit'][:7]} · deck @ {meta['deck_commit'][:7]} · KLayout 0.28.16\n",
          "| status | count |\n|---|---|"] + [f"| {s} | {cnt.get(s, 0)} |" for s in ORDER] + [
          f"\nLayout tests: {sum(t['agree'] for t in tests)}/{len(tests)} agree with golden.\n",
          "| rule | status | reason |\n|---|---|---|"] + [
          f"| {m['rule_id']} | {m['status']} | {m['reason']} |" for m in sorted(matches, key=lambda m: (ORDER.index(m['status']), m['rule_id']))] + [
          "\n## Findings\n"] + [f"- **{m['rule_id']}** `{d['type']}` — {d['detail']}" for m in matches for d in m["discrepancies"] if not d.get("info")] + [
          "\n## Limitations\n"] + [f"- {x}" for x in LIMITATIONS]
    (OUT / "summary.md").write_text("\n".join(md) + "\n")
    print("[report] " + "  ".join(f"{k}={cnt.get(k, 0)}" for k in ORDER) + " -> out/report.html, out/summary.md, out/review.csv")
    print(f"[report] coverage hint: {cov}")


if __name__ == "__main__":
    main()
