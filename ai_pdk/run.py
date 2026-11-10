"""End-to-end pipeline: python -m ai_pdk.run [--skip-tests]"""
import sys
import time

from . import build_db, deck_parse, doc_ingest, match, report, testgen, validate


def main():
    t0 = time.time()
    doc_ingest.main()
    deck = deck_parse.main()
    rules = build_db.build(deck["layers"])
    match.main()
    validate.validate(rules, deck["layers"])
    if "--skip-tests" not in sys.argv:
        testgen.main()
    report.main()
    print(f"[run] done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
