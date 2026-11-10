"""Stage 2 – LLM-based requirement extraction.

For each target rule the pipeline
  1. builds a *retrieval context*: the rule's table row, the table's column header,
     the section prose + notes, Appendix-B "not coded" rows and any rows in other
     sections that override the rule inside a marker layer (SRAM, YMTP_MK, OTP_MK…);
  2. renders the prompt in prompts/extract_rule.md;
  3. calls an LLM backend and parses the JSON it returns.

Backends
  anthropic : live call through the Anthropic SDK (needs ANTHROPIC_API_KEY)
  replay    : loads a recorded response from data/llm_responses/<rule>.json.
              Each recording stores the SHA of the context it was produced from, so a
              change in the manual marks the recording STALE instead of silently reusing it.

The LLM is only allowed to cite chunk_ids; file paths, line numbers and hashes are
attached afterwards by the pipeline from the ingest index, so provenance can't be
hallucinated. Quotes are verified later by validate.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from .config import LLM_RESPONSES, OUT, ROOT, TARGET_RULES

PROMPT = (ROOT / "prompts" / "extract_rule.md").read_text()
PROMPT_SHA = hashlib.sha256(PROMPT.encode()).hexdigest()[:16]

TEMPLATES = {  # instantiated rule id prefix -> (manual template prefix, binding)
    "M1.": ("Mn.", {"n": 1}),
    "V1.": ("Vn.", {"n": 1}),
}


def manual_id(rule_id: str):
    for inst, (tpl, binding) in TEMPLATES.items():
        if rule_id.startswith(inst):
            return tpl + rule_id[len(inst):], binding
    return rule_id, None


def load_chunks():
    return [json.loads(l) for l in open(OUT / "doc_chunks.jsonl")]


def _norm_id(s: str | None) -> str:
    return (s or "").replace("\\", "").replace("*", "").strip()


def build_context(rule_id: str, chunks: list[dict]):
    mid, binding = manual_id(rule_id)
    row = next(c for c in chunks if c["kind"] == "table_row" and _norm_id(c["rule_id"]) == mid
               and not c["file"].endswith("70_not_coded_rules_187.csv"))
    ctx = [row]
    same_table = [c for c in chunks if c["file"] == row["file"]]
    hdr = [c for c in same_table if c["kind"] == "table_row" and c["cells"] and c["cells"][0].strip() == "Layer"]
    ctx += hdr
    ctx += [c for c in chunks if c["section"] == row["section"] and c["kind"] in ("section_text", "note")]
    base = re.escape(mid)
    alt = re.escape(rule_id)
    rx_nc = re.compile(rf"^({base}|{alt})(i+|$)")
    ctx += [c for c in chunks if c["file"].endswith("70_not_coded_rules_187.csv")
            and rx_nc.match(_norm_id(c["cells"][0]) if c.get("cells") else "")]
    rx_ov = re.compile(rf"^[A-Z]+\.\s?({base}|{alt})(_|$|i)")
    over = [c for c in chunks if c["kind"] == "table_row" and c.get("cells")
            and rx_ov.match(_norm_id(c["cells"][0]))]
    ctx += over
    # the section intro of each overriding section explains the marker layer
    for o in over:
        ctx += [c for c in chunks if c["section"] == o["section"] and c["kind"] == "section_text"][:1]
    seen, out = set(), []
    for c in ctx:
        if c["chunk_id"] not in seen:
            seen.add(c["chunk_id"])
            out.append(c)
    return mid, binding, out


def render(rule_id, mid, binding, ctx):
    body = "\n".join(f"[{c['chunk_id']}] ({c['kind']}, §{c['section']})"
                     + (f" header={c['header']}" if c.get('header') else "")
                     + f"\n{c['text']}" for c in ctx)
    prompt = (PROMPT.replace("{{rule_id}}", rule_id).replace("{{manual_rule_id}}", mid)
              .replace("{{template_binding}}", json.dumps(binding)).replace("{{context}}", body))
    return prompt, hashlib.sha256(body.encode()).hexdigest()[:16]


class ReplayBackend:
    name = "replay"

    def __init__(self):
        self.session = json.loads((LLM_RESPONSES / "session.json").read_text())

    def __call__(self, rule_id, prompt, context_sha):
        per_rule = LLM_RESPONSES / f"{rule_id}.json"   # live-recorded responses take precedence
        if per_rule.exists():
            rec = json.loads(per_rule.read_text())
            resp, model, rec_sha = rec["response"], rec.get("model"), rec.get("context_sha")
        else:
            resp = self.session["responses"][rule_id]
            model = self.session["model"]
            rec_sha = self.session.get("context_shas", {}).get(rule_id)
        return resp, {"model": model, "recorded_at": self.session.get("recorded_at"),
                      "stale": rec_sha != context_sha, "recorded_context_sha": rec_sha}


class AnthropicBackend:
    name = "anthropic"

    def __init__(self, model="claude-opus-5-5"):
        import anthropic  # noqa: lazy import
        self.client = anthropic.Anthropic()
        self.model = model

    def __call__(self, rule_id, prompt, context_sha):
        msg = self.client.messages.create(model=self.model, max_tokens=4000,
                                          messages=[{"role": "user", "content": prompt}])
        text = msg.content[0].text
        resp = json.loads(text[text.find("{"): text.rfind("}") + 1])
        LLM_RESPONSES.mkdir(parents=True, exist_ok=True)
        (LLM_RESPONSES / f"{rule_id}.json").write_text(json.dumps(
            {"rule_id": rule_id, "model": self.model, "context_sha": context_sha,
             "prompt_sha": PROMPT_SHA, "response": resp}, indent=2, ensure_ascii=False))
        return resp, {"model": self.model, "stale": False}


def get_backend():
    if os.environ.get("AI_PDK_BACKEND") == "anthropic" or (
            os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("AI_PDK_BACKEND") != "replay"):
        return AnthropicBackend()
    return ReplayBackend()


def dump_contexts():
    """Write the rendered prompts so a reviewer (or an offline LLM run) can see exactly what was asked."""
    chunks = load_chunks()
    d = OUT / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    for r in TARGET_RULES:
        mid, binding, ctx = build_context(r, chunks)
        prompt, csha = render(r, mid, binding, ctx)
        (d / f"{r}.prompt.md").write_text(prompt)
        print(r, csha, [c["chunk_id"] for c in ctx])


def stamp_session():
    """Record which context each recorded response was produced from."""
    chunks = load_chunks()
    p = LLM_RESPONSES / "session.json"
    sess = json.loads(p.read_text())
    sess["prompt_sha"] = PROMPT_SHA
    sess["context_shas"] = {}
    for r in TARGET_RULES:
        mid, binding, ctx = build_context(r, chunks)
        sess["context_shas"][r] = render(r, mid, binding, ctx)[1]
    p.write_text(json.dumps(sess, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    import sys
    stamp_session() if "--stamp" in sys.argv else dump_contexts()
