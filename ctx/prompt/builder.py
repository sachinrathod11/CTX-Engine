"""PromptBuilder: question in, project-aware prompt out.

Pipeline: expand query -> hybrid search -> graph expansion -> rerank + rewrite (LLM)
-> snippet selection -> token-budgeted packing -> secret redaction."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from .. import tokens
from ..index.indexer import ProjectIndex
from ..query.expand import heuristic_expand, llm_expand
from ..query.rerank import rerank
from ..query.retriever import Candidate, expand_graph, search
from ..textutil import code_terms
from .redactor import Finding, redact
from .snippets import fence_lang, file_snippets, render

INTRO = ("I'm working on a software project and need help with the question below. The relevant parts of my "
         "codebase were selected automatically and are included after the question. Base your answer on this "
         "code and point to specific files and lines. If something you need is missing, tell me which file to share.")


@dataclass
class FileInfo:
    path: str
    summary: str
    reasons: List[str]
    score: float
    selected: bool
    lines: str = ""          # e.g. "12-48, 60-75" or "whole file"
    tokens: int = 0


@dataclass
class PromptResult:
    prompt: str
    tokens: int
    budget: int
    question: str
    rewritten: str
    files: List[FileInfo]
    connections: List[str]
    redactions: List[Dict]
    timings: Dict[str, float]
    modes: Dict[str, str]
    terms: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class PromptBuilder:
    def __init__(self, index: ProjectIndex):
        self.index = index
        self.cfg = index.cfg
        self.llm = index.llm

    def build(self, question: str, budget: Optional[int] = None, include: Optional[List[str]] = None,
              exclude: Optional[List[str]] = None, use_llm: bool = True, rewrite: bool = True,
              max_files: Optional[int] = None) -> PromptResult:
        question = question.strip()
        if not question:
            raise ValueError("Question is empty")
        budget = int(budget or self.cfg.budget)
        max_files = max_files or self.cfg.max_files
        include, exclude = list(include or []), set(exclude or [])
        use_llm = use_llm and self.cfg.use_llm
        T: Dict[str, float] = {}
        modes: Dict[str, str] = {}
        store = self.index.store
        overview = self.index.overview()
        ov = store.get_meta("overview") or {}

        t = time.time()
        exp = llm_expand(self.llm, question, overview) if use_llm and self.llm.can_generate else heuristic_expand(question)
        T["expand"] = time.time() - t
        modes["expand"] = exp.source

        t = time.time()
        res = search(self.index, question, exp, limit=self.cfg.candidates)
        cands = expand_graph(self.index, res, limit=self.cfg.candidates)
        cands = [c for c in cands if c.path not in exclude]
        T["search"] = time.time() - t
        modes["vector"] = "off" if res.signals.get("vector", 0) <= 0 else (store.get_meta("embed_model") or "")

        summaries = {f["id"]: f["summary"] or "" for f in store.all_files()}
        t = time.time()
        rr = rerank(self.llm, question, overview, cands, summaries, max_files, ov.get("stack", []), use_llm=use_llm)
        T["rerank"] = time.time() - t
        modes["rerank"] = rr.source
        selected = list(rr.selected)
        # user overrides: forced files go first
        for p in reversed(include):
            row = store.file_by_path(p)
            if row is None:
                continue
            selected = [c for c in selected if c.path != p]
            existing = next((c for c in cands if c.path == p), None)
            selected.insert(0, existing or Candidate(row["id"], p, 1.0, ["added by you"]))
        rewritten = rr.rewritten if rewrite else question

        t = time.time()
        prompt, file_infos, connections, findings = self._pack(question, rewritten, overview, selected, cands,
                                                               res.chunk_scores, set(exp.terms + exp.extra), budget,
                                                               summaries)
        T["pack"] = time.time() - t
        return PromptResult(prompt=prompt, tokens=tokens.count(prompt), budget=budget, question=question,
                            rewritten=rewritten, files=file_infos, connections=connections,
                            redactions=[asdict(f) for f in findings],
                            timings={k: round(v, 3) for k, v in T.items()}, modes=modes,
                            terms=exp.terms + exp.extra)

    # ------------------------------------------------------------------ packing
    def _connections(self, selected: List[Candidate]) -> List[str]:
        ids = {c.file_id: c.path for c in selected}
        out = []
        for fid, path in ids.items():
            for e in self.index.store.edges_from(fid):
                if e["dst"] in ids:
                    if e["kind"] == "http":
                        out.append(f"`{path}` calls `{e['detail']}`, handled in `{ids[e['dst']]}`")
                    else:
                        out.append(f"`{path}` imports `{ids[e['dst']]}`")
        return sorted(set(out))

    def _pack(self, question, rewritten, overview, selected, cands, chunk_scores, qwords, budget, summaries):
        store = self.index.store
        findings: List[Finding] = []
        connections = self._connections(selected)

        head = [INTRO, "", "## Question", rewritten]
        if rewritten.strip() != question.strip():
            head.append(f'(My original question: "{question}")')
        head += ["", "## Project overview", overview]
        if connections:
            head += ["", "## How these files connect"] + [f"- {c}" for c in connections]
        file_list = ["", "## Relevant files"]
        for c in selected:
            file_list.append(f"- `{c.path}`: {summaries.get(c.file_id, '')[:220]}")
        header_text = "\n".join(head + file_list)
        used = tokens.count(header_text) + 20
        if used > budget * 0.45:  # very small budgets: drop the overview first
            head = [INTRO, "", "## Question", rewritten, ""] + file_list[1:]
            header_text = "\n".join(head)
            used = tokens.count(header_text) + 20

        code_parts: List[str] = ["", "## Code"]
        infos: List[FileInfo] = []
        remaining = budget - used - tokens.count("\n".join(code_parts))
        for c in selected:
            row = store.file_by_id(c.file_id)
            lines, snips = file_snippets(self.index, c.file_id, c.path, chunk_scores, qwords)
            info = FileInfo(c.path, summaries.get(c.file_id, ""), c.reasons, round(c.score, 4), True)
            infos.append(info)
            if not snips or remaining < 60:
                continue
            lang = fence_lang(c.path, row["lang"] if row else "")
            # take snippets in score order while they fit, then print them in file order
            chosen, cost = [], 0
            for sn in snips:
                text = render(lines, sn)
                k = tokens.count(text) + 12
                if cost + k <= remaining:
                    sn.text, chosen, cost = text, chosen + [sn], cost + k
                elif not chosen and remaining - cost > 80:
                    max_lines = max(5, int((remaining - 40) / max(1, k / max(1, sn.end - sn.start + 1))))
                    sn.text = render(lines, sn, max_lines)
                    chosen, cost = [sn], tokens.count(sn.text) + 12
                    break
                if len(chosen) >= 4:
                    break
            if not chosen:
                continue
            block = []
            for sn in sorted(chosen, key=lambda s: s.start):
                clean, f = redact(sn.text, c.path, sn.start - 1)
                findings += f
                label = "whole file" if sn.name == "whole file" else f"lines {sn.start}-{sn.end}"
                block += [f"### {c.path} ({label})", f"```{lang}", clean, "```", ""]
            text = "\n".join(block)
            used_now = tokens.count(text)
            remaining -= used_now
            info.tokens = used_now
            info.lines = ", ".join("whole file" if s.name == "whole file" else f"{s.start}-{s.end}"
                                   for s in sorted(chosen, key=lambda s: s.start))
            code_parts.append(text)

        sel_ids = {c.file_id for c in selected}
        for c in cands:
            if c.file_id not in sel_ids:
                infos.append(FileInfo(c.path, summaries.get(c.file_id, ""), c.reasons, round(c.score, 4), False))

        prompt = header_text + "\n" + "\n".join(code_parts).rstrip() + "\n"
        prompt, f2 = redact(prompt, "prompt")  # final safety pass over everything
        findings += f2
        return prompt, infos, connections, findings


def whole_repo_tokens(index: ProjectIndex) -> int:
    """Baseline for evaluation: tokens needed to paste every indexed file."""
    total = 0
    for f in index.store.all_files():
        try:
            total += tokens.count((index.root / f["path"]).read_text(errors="replace"))
        except OSError:
            pass
    return total
