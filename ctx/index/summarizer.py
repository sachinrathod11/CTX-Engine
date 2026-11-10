"""Short natural-language summaries of each file (used by the reranker and in prompts)."""
from __future__ import annotations

from typing import List

from ..llm import LLMUnavailable, Ollama

SYSTEM = ("You summarize source files for a code search index. Reply with 2 sentences, at most 60 words, "
          "plain text. Say what the file is responsible for and name its key functions/classes. "
          "No preamble, no markdown.")

SUMMARIZABLE = {"python", "javascript", "typescript", "java", "kotlin", "go", "rust", "csharp", "php",
                "ruby", "c", "cpp", "swift", "dart", "scala", "vue", "svelte", "sql", "shell", "html"}


def heuristic_summary(path: str, lang: str, doc: str, symbols: List[dict], routes: List[dict], imports: List[str]) -> str:
    parts = []
    if doc:
        parts.append(doc.rstrip(".") + ".")
    defined = [s for s in symbols if s["kind"] != "section"]
    if defined:
        names = []
        for s in defined[:10]:
            n = s["name"]
            names.append(f"class {n}" if s["kind"] == "class" else (n + "()" if s["kind"] in ("function", "method", "component") else n))
        parts.append("Defines " + ", ".join(names) + (" and more." if len(defined) > 10 else "."))
    elif symbols:
        parts.append("Sections: " + ", ".join(s["name"] for s in symbols[:8]) + ".")
    serve = sorted({f"{r['method']} {r['path']}" for r in routes if r["role"] == "defines"})
    call = sorted({f"{r['method']} {r['path']}" for r in routes if r["role"] == "calls"})
    if serve:
        parts.append("Handles " + ", ".join(serve[:6]) + ".")
    if call:
        parts.append("Calls " + ", ".join(call[:6]) + ".")
    if imports:
        parts.append("Uses " + ", ".join(sorted(set(imports))[:6]) + ".")
    if not parts:
        parts.append(f"{lang} file.")
    return " ".join(parts)[:500]


def llm_summary(llm: Ollama, path: str, text: str, heuristic: str) -> str:
    body = text if len(text) < 6000 else text[:4500] + "\n...\n" + text[-1200:]
    prompt = f"File: {path}\nOutline: {heuristic}\n\n```\n{body}\n```\n\nSummary:"
    out = llm.generate(prompt, system=SYSTEM, max_tokens=140)
    out = " ".join(out.split())
    if len(out) < 15:
        raise LLMUnavailable("empty summary")
    return out[:600]
