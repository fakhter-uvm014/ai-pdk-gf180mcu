# GF180MCU rule–deck consistency (prototype)

Manual @ de3240d · deck @ 9f992d5 · KLayout 0.28.16

| status | count |
|---|---|
| MISSING | 1 |
| PARTIAL | 2 |
| REVIEW | 4 |
| MATCH | 13 |

Layout tests: 46/57 agree with golden.

| rule | status | reason |
|---|---|---|
| MT.2b | MISSING | no deck implementation found for any mandatory clause |
| MT.1 | PARTIAL | 2/3 mandatory clauses/domains implemented |
| MT.2a | PARTIAL | 2/3 mandatory clauses/domains implemented |
| M1.2b | REVIEW | implemented, but needs engineering judgement: BOUNDARY_MISMATCH, DYNAMIC_MISMATCH |
| M1.4 | REVIEW | implemented, but needs engineering judgement: BOUNDARY_MISMATCH |
| PL.3b | REVIEW | guideline not coded by design (manual Appendix B and deck agree) — confirm waiver |
| PL.5b | REVIEW | implemented, but needs engineering judgement: IDENTICAL_TO_OTHER_RULE, UNEXPLAINED_EXCLUSION |
| CO.1 | MATCH | all mandatory clauses implemented with consistent values and operators |
| CO.11 | MATCH | all mandatory clauses implemented with consistent values and operators |
| CO.2a | MATCH | all mandatory clauses implemented with consistent values and operators |
| CO.3 | MATCH | all mandatory clauses implemented with consistent values and operators |
| CO.6 | MATCH | all mandatory clauses implemented with consistent values and operators |
| M1.1 | MATCH | all mandatory clauses implemented with consistent values and operators |
| M1.2a | MATCH | all mandatory clauses implemented with consistent values and operators |
| M1.3 | MATCH | all mandatory clauses implemented with consistent values and operators |
| PL.1 | MATCH | all mandatory clauses implemented with consistent values and operators |
| PL.2 | MATCH | all mandatory clauses implemented with consistent values and operators |
| PL.8 | MATCH | all mandatory clauses implemented with consistent values and operators |
| V1.1 | MATCH | all mandatory clauses implemented with consistent values and operators |
| V1.3 | MATCH | all mandatory clauses implemented with consistent values and operators |

## Findings

- **PL.5b** `UNEXPLAINED_EXCLUSION` — PL.5b_3.3V excludes mvpsd, mvsd; no manual exception for this rule mentions it
- **PL.5b** `IDENTICAL_TO_OTHER_RULE` — PL.5b_3.3V has the same implementation as PL.5a_3.3V: `poly_pl.separation(comp_pl, 0.1.um, euclidian).polygons(0.001).not_interacting(v5_xtor).not_interacting(dualga`
- **PL.5b** `UNEXPLAINED_EXCLUSION` — PL.5b_5V excludes mvpsd, mvsd; no manual exception for this rule mentions it
- **PL.5b** `IDENTICAL_TO_OTHER_RULE` — PL.5b_5V has the same implementation as PL.5a_5V: `poly_pl.outside(sramcore).separation(comp_pl, 0.3.um, euclidian).polygons(0.001).overlapping(dualgate)`
- **M1.2b** `BOUNDARY_MISMATCH` — manual: wide = length & width > 10um; deck treats polygons whose edges are all >= 10um as wide (not_interacting(edges.with_length(nil, 10.um)))
- **M1.2b** `DYNAMIC_MISMATCH` — layout test M1.2b_c1_negative_pair (two 12x12 um Metal1 plates spaced 0.280 um): manual expects a violation, M1.2b reports 0
- **M1.4** `BOUNDARY_MISMATCH` — manual requires > 30% but deck only flags density < 30.0% — exactly 30% passes the deck and fails the manual
- **MT.1** `NOT_FOUND` — no deck check for MetalTop width, 11K (metal_top_thickness=11K)
- **MT.2a** `NOT_FOUND` — no deck check for MetalTop space, 11K (metal_top_thickness=11K)
- **MT.2b** `NOT_FOUND` — no deck check for Space between wide MetalTop, 6K (metal_top_thickness=6K)
- **MT.2b** `NOT_FOUND` — no deck check for Space between wide MetalTop, 9K (metal_top_thickness=9K)
- **MT.2b** `NOT_FOUND` — no deck check for Space between wide MetalTop, 11K (metal_top_thickness=11K)

## Limitations

- Scope is 20 rules from four sections (Poly2, Contact, Metal1/Via1 instantiated from Metaln/Vian, MetalTop). The ID-level scan of the whole manual is a coverage hint, not a validation.
- The LLM responses used in this run were produced by Claude from the rendered prompts and replayed from data/llm_responses/session.json. The live Anthropic backend is wired in but was not exercised here (no API key in the build environment). Every value is still verified against the source text by the validator, so a hallucinated number or quote fails the run.
- The deck is pinned to google/globalfoundries-pdk-libs-gf180mcu_fd_pr @ 9f992d5 (2023-05-31). The successor repo no longer ships the deck under rules/klayout/drc, so the findings must be re-checked against the deck version actually used in your flow (e.g. the open_pdks / volare build).
- Static deck analysis is pattern-based (Ruby DSL parsed with regexes plus variable-dependency resolution). It understands the idioms used in this deck, not arbitrary Ruby.
- Polarity (NMOS/PMOS) of PL.2 values is matched by value only; the deck's N/P selection logic is not checked.
- Layout tests are minimal single-feature cells. They prove whether a check fires on a clean, isolated case; they don't exercise hierarchy, angled geometry, or interactions with other rules. Clause-level tests for EOL / adjacent-edge enclosure (CO.6 i–ii, V1.3 i–ii) are not generated yet.
- Golden results come from the extracted manual semantics, not from foundry sign-off decks. A disagreement means the deck and the manual (as extracted) disagree; an engineer decides which one is wrong.
- Results were obtained with KLayout 0.28.16. The wide-to-wide M1.2b behaviour depends on KLayout's two-layer separation semantics and should be re-tested on the KLayout version used in production.
