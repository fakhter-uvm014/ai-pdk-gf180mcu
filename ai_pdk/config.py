"""Paths and source pins for the AI-PDK prototype."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PDK_REPO = ROOT / "third_party_gf180mcu-pdk"            # github.com/google/gf180mcu-pdk
DECK_REPO = ROOT / "third_party_gf180mcu_fd_pr"         # github.com/google/globalfoundries-pdk-libs-gf180mcu_fd_pr
MANUAL_DIR = PDK_REPO / "docs" / "physical_verification" / "design_manual"
DECK_DIR = DECK_REPO / "rules" / "klayout" / "drc"

OUT = ROOT / "out"
DATA = ROOT / "data"
SCHEMA = ROOT / "schema" / "rule.schema.json"
LLM_RESPONSES = DATA / "llm_responses"

SOURCES = {
    "design_manual": {
        "repo": "https://github.com/google/gf180mcu-pdk",
        "path": "docs/physical_verification/design_manual",
    },
    "drc_deck": {
        "repo": "https://github.com/google/globalfoundries-pdk-libs-gf180mcu_fd_pr",
        "path": "rules/klayout/drc",
    },
}

# The 20 rules selected for the prototype (manual IDs, template rules instantiated for n=1).
TARGET_RULES = [
    "PL.1", "PL.2", "PL.3b", "PL.5b", "PL.8",
    "CO.1", "CO.2a", "CO.3", "CO.6", "CO.11",
    "M1.1", "M1.2a", "M1.2b", "M1.3", "M1.4",
    "V1.1", "V1.3",
    "MT.1", "MT.2a", "MT.2b",
]
