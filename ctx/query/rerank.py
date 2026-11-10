"""One local-LLM call that (1) keeps only the candidate files that help answer the
question and (2) rewrites the question so it names the right parts of the project.
Falls back to score-based selection and a template rewrite when Ollama is off."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from ..llm import LLMUnavailable, Ollama
from .retriever import Candidate

SYSTEM = (
    "You are the retrieval step of a code assistant. You see a developer's question, a project overview and "
    "numbered candidate files with summaries. Do two things:\n"
    "1. Pick the files a developer must read to answer the question, most important first (usually 2-6). "
    "Prefer files containing the actual logic over config, docs or tests.\n"
    "2. Rewrite the question so it is specific to this project: name the layers, files or functions involved "
    "and what to check. Do not guess the answer or invent facts; keep the user's intent. Max 70 words.\n"
    "Return JSON: {\"files\": [numbers], \"question\": \"rewritten question\"}. Only output JSON."
)


@dataclass
class RerankResult:
    selected: List[Candidate]
    rewritten: str
    source: str  # "llm" | "heuristic"


def heuristic_select(cands: List[Candidate], max_files: int) -> List[Candidate]:
    if not cands:
        return []
    top = cands[0].score
    keep = [c for c in cands if c.score >= top * 0.3]
    return keep[:max_files]


def heuristic_rewrite(question: str, selected: List[Candidate], stack: List[str]) -> str:
    q = question.strip().rstrip("?.")
    if not selected:
        return question.strip()
    names = ", ".join(f"`{c.path}`" for c in selected[:4])
    where = f" ({', '.join(stack[:4])})" if stack else ""
    return (f"{q[:1].upper() + q[1:]}? In this project{where}, the relevant code appears to be in {names}. "
            f"Using the code below, find the cause and suggest a concrete fix.")


def rerank(llm: Ollama, question: str, overview: str, cands: List[Candidate], summaries: dict,
           max_files: int, stack: List[str], use_llm: bool = True) -> RerankResult:
    if use_llm and llm.can_generate and cands:
        lines = []
        for i, c in enumerate(cands, 1):
            why = f" [{'; '.join(c.reasons[:2])}]" if c.reasons else ""
            lines.append(f"{i}. {c.path} - {summaries.get(c.file_id, '')[:260]}{why}")
        prompt = (f"Project overview:\n{overview}\n\nQuestion: {question}\n\nCandidate files:\n" + "\n".join(lines)
                  + f"\n\nPick at most {max_files} files.")
        try:
            data = llm.generate_json(prompt, system=SYSTEM, max_tokens=260)
            picks = []
            for n in data.get("files", []):
                try:
                    k = int(n) - 1
                except (TypeError, ValueError):
                    continue
                if 0 <= k < len(cands) and cands[k] not in picks:
                    picks.append(cands[k])
            rq = str(data.get("question", "")).strip()
            if picks:
                return RerankResult(picks[:max_files], rq if len(rq) > 10 else heuristic_rewrite(question, picks, stack), "llm")
        except LLMUnavailable:
            pass
    sel = heuristic_select(cands, max_files)
    return RerankResult(sel, heuristic_rewrite(question, sel, stack), "heuristic")
