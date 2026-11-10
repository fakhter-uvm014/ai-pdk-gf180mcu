"""Stage 1 – Engineering documentation ingest.

Turns the GF180MCU Design Manual (reStructuredText + csv-table files) into
addressable *chunks*. Every chunk carries exact provenance (file, physical line
span, SHA-256 of the excerpt) so that anything the LLM later says can be traced
back to – and verified against – the source text.

Chunk kinds
  section_text : prose paragraph under a section heading
  table_row    : one rule row of a csv-table (multi-line cells kept together)
  note         : one item of a `.. note::` block (footnotes, rule exceptions)
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from pathlib import Path

from .config import MANUAL_DIR, OUT, PDK_REPO

HEADING_UNDERLINE = re.compile(r"^([=\-~^\"'`#*+])\1{2,}\s*$")
CSV_TABLE = re.compile(r"^\s?\.\.\s+csv-table::\s*(.*)$")
FILE_OPT = re.compile(r"^\s+:file:\s*(\S+)")
NOTE = re.compile(r"^\s?\.\.\s+note::\s*$")
RULE_ID = re.compile(r"^\s*\\?\*?\s*([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+[a-z]*\*?)\s*$")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def rel(p: Path) -> str:
    return str(p.resolve().relative_to(PDK_REPO.resolve()))


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\\)", ")").replace("\\*", "*")).strip()


def parse_csv_table(csv_path: Path, section: str, table_title: str):
    """Yield one chunk per CSV record with the physical line span it occupies."""
    raw = csv_path.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()
    reader = csv.reader(io.StringIO(raw))
    prev_end = 0
    header = None
    for row in reader:
        start, end = prev_end + 1, reader.line_num
        prev_end = end
        if not row or not any(c.strip() for c in row):
            continue
        if header is None and not RULE_ID.match(_clean(row[0]).lstrip("\\").strip()):
            header = [_clean(c) for c in row]
            continue
        header = header or []
        excerpt = "\n".join(lines[start - 1:end])
        rid = _clean(row[0]).lstrip("\\").strip()
        m = RULE_ID.match(rid)
        yield {
            "chunk_id": f"{csv_path.stem}:L{start}",
            "kind": "table_row",
            "section": section,
            "table": table_title,
            "rule_id": m.group(1).strip() if m else None,
            "header": header,
            "cells": [c.strip() for c in row],
            "text": " | ".join(_clean(c) for c in row),
            "file": rel(csv_path),
            "line_start": start,
            "line_end": end,
            "excerpt": excerpt,
            "sha256": sha(excerpt),
        }


def parse_rst(rst_path: Path):
    lines = rst_path.read_text(encoding="utf-8", errors="replace").splitlines()
    section = None
    i = 0
    while i < len(lines):
        line = lines[i]
        # Section heading: text line followed by an underline
        if i + 1 < len(lines) and line.strip() and HEADING_UNDERLINE.match(lines[i + 1]) \
                and not line.startswith(".."):
            section = line.strip()
            i += 2
            continue
        m = CSV_TABLE.match(line)
        if m:
            title = m.group(1).strip()
            j = i + 1
            while j < len(lines) and (lines[j].startswith(" ") or not lines[j].strip()):
                fm = FILE_OPT.match(lines[j])
                if fm:
                    csv_path = (rst_path.parent / fm.group(1)).resolve()
                    if csv_path.exists():
                        yield from parse_csv_table(csv_path, section, title)
                if not lines[j].strip() and j + 1 < len(lines) and not lines[j + 1].startswith(" "):
                    break
                j += 1
            i = j
            continue
        if NOTE.match(line):
            j = i + 1
            block = []
            while j < len(lines) and (lines[j].startswith(" ") or not lines[j].strip()):
                block.append((j + 1, lines[j]))
                j += 1
            # split note block into items (numbered items or single paragraph)
            items, cur = [], []
            for ln, txt in block:
                if re.match(r"^\s+\d+\.\s", txt) and cur:
                    items.append(cur)
                    cur = []
                if txt.strip():
                    cur.append((ln, txt))
            if cur:
                items.append(cur)
            for it in items:
                s, e = it[0][0], it[-1][0]
                excerpt = "\n".join(lines[s - 1:e])
                yield {
                    "chunk_id": f"{rst_path.stem}:L{s}",
                    "kind": "note",
                    "section": section,
                    "rule_id": None,
                    "text": _clean(" ".join(t for _, t in it)),
                    "file": rel(rst_path),
                    "line_start": s,
                    "line_end": e,
                    "excerpt": excerpt,
                    "sha256": sha(excerpt),
                }
            i = j
            continue
        if line.strip() and not line.startswith(("..", " ")) and section:
            s = i + 1
            j = i
            while j < len(lines) and lines[j].strip() and not lines[j].startswith(".."):
                j += 1
            excerpt = "\n".join(lines[s - 1:j])
            yield {
                "chunk_id": f"{rst_path.stem}:L{s}",
                "kind": "section_text",
                "section": section,
                "rule_id": None,
                "text": _clean(excerpt),
                "file": rel(rst_path),
                "line_start": s,
                "line_end": j,
                "excerpt": excerpt,
                "sha256": sha(excerpt),
            }
            i = j
            continue
        i += 1


def ingest(manual_dir: Path = MANUAL_DIR):
    chunks = []
    for rst in sorted(manual_dir.glob("drm_*.rst")):
        chunks.extend(parse_rst(rst))
    return chunks


def main():
    OUT.mkdir(exist_ok=True)
    chunks = ingest()
    with open(OUT / "doc_chunks.jsonl", "w") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    rows = [c for c in chunks if c["kind"] == "table_row" and c["rule_id"]]
    print(f"[ingest] {len(chunks)} chunks, {len(rows)} rule rows from {MANUAL_DIR.name}")
    return chunks


if __name__ == "__main__":
    main()
