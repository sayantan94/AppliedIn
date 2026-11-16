"""Check a free-text answer against the candidate's sources before it is typed.

Usage (repo root):
  env PYTHONPATH=src .venv/bin/python src/tools/freetext_check.py '<pk>' /tmp/answer.txt

The operator's legacy path `.local/operator/freetext_check.py` forwards here through the
tracked `scripts/freetext_check_shim.py` template, so there is one rule owner.

Every proper name (capitalised word or phrase not at a sentence start) and every number
in the answer must appear in the row's tailored résumé .tex, resume/base.tex, the facts /
answers files, or the posting text (the employer's own name and JD terms). Prints
"OK" (exit 0) or "UNSUPPORTED: ..." (exit 1). This guards against invented employers
reaching an application.

Second check, "agent tells": phrases that make an application read as filled by an AI agent
(em dash, "N/A", "see my resume", "I am not an AI", "I'm excited", "delve",
short bullet lists, ...).
Each hit prints "TELL: <rule>" and the run exits 1, the same as UNSUPPORTED. The one sentence
in TRANSPARENCY is allowed and is stripped before the tells are checked.

`tells()` is pure and offline. The daemon also calls it at two storage boundaries
(writer answer-bank entries and the tailored résumé save) in `agent/graph.py`.
"""
from __future__ import annotations

import glob
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMMON = {
    "i", "ai", "llm", "llms", "api", "apis", "mcp", "saas", "b2b", "b2c", "pm", "tpm", "ceo",
    "cto", "cfo", "cpo", "ux", "ui", "qa", "kpi", "kpis", "okr", "okrs", "sdlc", "hitl", "rag",
    "yes", "no", "the", "a", "an", "my", "we", "our", "in", "at", "and", "for", "with", "on",
    "english", "russian", "vietnam", "da", "nang", "europe", "uk", "us", "uae", "eu", "emea",
    "apac", "agile", "scrum", "kanban", "jira", "confluence", "python", "typescript", "github",
    "monday", "tuesday", "wednesday", "thursday", "friday", "january", "february", "march",
    "april", "may", "june", "july", "august", "september", "october", "november", "december",
}


TRANSPARENCY = ("Full transparency: my own AI agent filled in this form from my records; "
                "I'm a real person and happy to talk.")

_TELL_PATTERNS: list[tuple[str, str]] = [
    ("em dash", r"\u2014"),
    ("N/A", r"(?<![\w/])n/a(?![\w/])"),
    ("not applicable", r"\bnot applicable\b"),
    ("not specified in my profile", r"\bnot specified in my profile\b"),
    ("not specified in my resume", r"\bnot specified in my resume\b"),
    ("not mentioned in my resume", r"\bnot mentioned in my resume\b"),
    ("see my resume", r"\bsee my resume\b"),
    ("see my cv", r"\bsee my cv\b"),
    ("as per my resume", r"\bas per my resume\b"),
    ("per my resume", r"\bper my resume\b"),
    ("I am not an AI", r"\bi am not an ai\b"),
    ("I'm not an AI", r"\bi'm not an ai\b"),
    ("I am not an LLM", r"\bi am not an llm\b"),
    ("I'm not an LLM", r"\bi'm not an llm\b"),
    ("not a bot", r"\bnot a bot\b"),
    ("as an AI", r"\bas an ai\b"),
    ("as a language model", r"\bas a language model\b"),
    ("I'm excited", r"\bi'm excited\b"),
    ("I am excited", r"\bi am excited\b"),
    ("I'm thrilled", r"\bi'm thrilled\b"),
    ("I am thrilled", r"\bi am thrilled\b"),
    ("passionate about", r"\bpassionate about\b"),
    ("delve", r"\bdelv\w*"),
    ("leverage", r"\bleverag\w*"),
    ("I am writing to", r"\bi am writing to\b"),
]


def tells(text: str) -> list[str]:
    """Return the agent-tell rules the text trips (empty list = clean). Pure, offline."""
    body = text.replace("\u2019", "'").replace(TRANSPARENCY, " ")
    low = body.lower()
    hits = [name for name, pat in _TELL_PATTERNS if re.search(pat, low)]
    if len(body.split()) < 150 and re.search(r"(?m)^\s*(?:[-*\u2022])\s", body):
        hits.append("bullet list in a short answer")
    return hits


def _sources(pk: str) -> str:
    # Imported here so that importing this module for tells() needs no store backend.
    from core.stores import make_stores

    parts = [(ROOT / "resume/base.tex").read_text(errors="ignore")]
    for pattern in (".local/*fact*", ".local/operator/*fact*", ".local/operator/answers.md",
                    ".local/steering.md"):
        for f in glob.glob(str(ROOT / pattern)):
            if Path(f).is_file():
                parts.append(Path(f).read_text(errors="ignore"))
    row = make_stores().tracking.get(pk) or {}
    for key in ("company", "title", "location", "jd_text", "text"):
        parts.append(str(row.get(key) or ""))
    key = str(row.get("resume_s3_key") or "")
    if key:
        tex = ROOT / ".local/artifacts" / (key[:-4] + ".tex" if key.endswith(".pdf") else key)
        if tex.is_file():
            parts.append(tex.read_text(errors="ignore"))
    for f in glob.glob(str(ROOT / ".local/artifacts/resumes" / (pk.split("#")[0] + "#*.tex"))):
        if pk.split("#", 1)[-1][:12] in f:
            parts.append(Path(f).read_text(errors="ignore"))
    return re.sub(r"\\[a-zA-Z]+|[{}]", " ", "\n".join(parts)).lower()


def check(pk: str, text: str) -> list[str]:
    src = _sources(pk)
    bad: list[str] = []
    for sentence in re.split(r"(?<=[.!?:;])\s+|\n+", text):
        words = re.findall(r"[A-Za-z][\w&'.+-]*", sentence)
        for i, w in enumerate(words):
            core = w.strip(".'-")
            if i == 0 or not core[:1].isupper() or core.lower() in COMMON:
                continue
            if core.lower() not in src:
                bad.append(core)
    for n in re.findall(r"\d[\d,.]*\s*%?|\d+\+", text):
        n = n.strip().rstrip(".,")
        if n and n.replace(" ", "") not in src.replace(" ", "") and n.rstrip("%+") not in src:
            bad.append(n)
    return sorted(set(bad))


def main() -> int:
    pk, path = sys.argv[1], sys.argv[2]
    text = Path(path).read_text()
    bad = check(pk, text)
    hits = tells(text)
    if bad:
        print("UNSUPPORTED: " + ", ".join(bad))
        print("Remove or replace each item with a fact from the tailored résumé or facts file.")
    for hit in hits:
        print("TELL: " + hit)
    if hits:
        print("Rewrite the answer in plain first-person prose without these agent tells.")
    if bad or hits:
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
