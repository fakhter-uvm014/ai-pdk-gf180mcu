# AI-PDK: AI-assisted rule extraction and DRC consistency validation for GF180MCU

Prototype that reads the open-source GF180MCU Design Manual, extracts design rules with an
LLM into a schema-validated database with line-level provenance, matches them against the
KLayout DRC deck shipped with the PDK, runs generated layout tests through that deck, and
produces a review report with MATCH / PARTIAL / REVIEW / MISSING classifications.

## Pipeline

| stage | module | output |
|---|---|---|
| 1 Engineering docs | `ai_pdk/doc_ingest.py` | `out/doc_chunks.jsonl` (table rows, notes, prose; file + line span + SHA) |
| 2 LLM extraction | `ai_pdk/llm_extract.py`, `prompts/extract_rule.md` | `out/prompts/*.prompt.md`, LLM JSON |
| 3 Rule database | `ai_pdk/build_db.py`, `schema/rule.schema.json` | `out/rules.json`, `out/rules.yaml` |
| 4 Deck analysis + matching | `ai_pdk/deck_parse.py`, `ai_pdk/match.py` | `out/deck_index.json`, `out/matches.json` |
| 5 Validation | `ai_pdk/validate.py`, `ai_pdk/testgen.py` | `out/validation.json`, `out/layout_tests/*.gds/.lyrdb`, `out/test_results.json` |
| 6 Review + report | `ai_pdk/report.py` | `out/report.html`, `out/summary.md`, `out/review.csv` |

## Run

```bash
git clone --depth 1 https://github.com/google/gf180mcu-pdk third_party_gf180mcu-pdk
git clone https://github.com/google/globalfoundries-pdk-libs-gf180mcu_fd_pr third_party_gf180mcu_fd_pr
git -C third_party_gf180mcu_fd_pr checkout 9f992d5a9186d1f7820c58f039c484ad35b2edea
pip install klayout jsonschema pyyaml pytest      # plus the KLayout application (apt install klayout) for DRC runs
python -m ai_pdk.run                              # ~6 min, most of it KLayout; --skip-tests for the static part only
python -m pytest -q tests/
```

LLM backend: by default recorded responses are replayed from `data/llm_responses/session.json`
(each keyed to the hash of the context it came from; a changed manual marks it stale).
`AI_PDK_BACKEND=anthropic` with `ANTHROPIC_API_KEY` set re-extracts live and records new responses.

## Design choices

- The LLM may only cite `chunk_id`s. File paths, line spans, commit and hashes are attached by
  the pipeline, so provenance cannot be invented. The validator then requires every quote to be
  verbatim in its chunk and every number to appear in its own quote.
- Layers are mapped manual → deck through GDS numbers, not names.
- Template rules (`Mn.x`, `Vn.x`) are instantiated for n = 1 (M1, V1).
- Deck matching is ID-first, constrained by applicability (voltage suffix, `METAL_TOP` branch),
  then by measurement primitive and value; TF-IDF similarity ranks clauses and supplies
  nearest neighbours when no ID matches.
- Layout tests are derived from the DB; the golden outcome is the manual's semantics.
  Every disagreement must be explained by a static finding or is reported as a new dynamic finding.

See `out/report.html` for results and limitations.
