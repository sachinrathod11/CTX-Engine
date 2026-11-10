"""Evaluation for the report.

Measures, per question:
  - recall: share of the expected files that ended up in the prompt
  - hit@1: was an expected file ranked first
  - prompt tokens vs. pasting the whole repository
  - build time
and runs an ablation: keyword only -> + query expansion -> + vectors -> + graph -> + local LLM.

Usage:
  python eval/run_eval.py eval/questions/demo-shop.json
  python eval/run_eval.py eval/questions/flask.json --repo ~/code/flask
  python eval/run_eval.py eval/questions/demo-shop.json --answer-sheet out/   # prompts for the ChatGPT A/B test
"""
from __future__ import annotations

import argparse
import csv
import json
import resource
import statistics
import sys
import time
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ctx import tokens  # noqa: E402
from ctx.config import Config  # noqa: E402
from ctx.index.indexer import ProjectIndex  # noqa: E402
from ctx.prompt.builder import PromptBuilder, whole_repo_tokens  # noqa: E402
from ctx.query.expand import Expansion, heuristic_expand, llm_expand  # noqa: E402
from ctx.query.rerank import heuristic_select, rerank  # noqa: E402
from ctx.query.retriever import expand_graph, search  # noqa: E402

VARIANTS = [
    ("keywords only", dict(expand=False, vector=False, graph=False, llm=False)),
    ("+ query expansion", dict(expand=True, vector=False, graph=False, llm=False)),
    ("+ vectors", dict(expand=True, vector=True, graph=False, llm=False)),
    ("+ graph", dict(expand=True, vector=True, graph=True, llm=False)),
    ("+ local LLM (full)", dict(expand=True, vector=True, graph=True, llm=True)),
]


def select(idx: ProjectIndex, q: str, v: dict, max_files: int) -> List[str]:
    if v["llm"]:
        exp = llm_expand(idx.llm, q, idx.overview())
    elif v["expand"]:
        exp = heuristic_expand(q)
    else:
        base = heuristic_expand(q)
        exp = Expansion(base.terms, [], base.phrases, base.mentioned_files, "none")
    res = search(idx, q, exp, limit=idx.cfg.candidates, use_vector=v["vector"])
    cands = expand_graph(idx, res, limit=idx.cfg.candidates) if v["graph"] else res.files
    if v["llm"]:
        summaries = {f["id"]: f["summary"] or "" for f in idx.store.all_files()}
        ov = idx.store.get_meta("overview") or {}
        sel = rerank(idx.llm, q, idx.overview(), cands, summaries, max_files, ov.get("stack", [])).selected
    else:
        sel = heuristic_select(cands, max_files)
    return [c.path for c in sel]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("questions")
    ap.add_argument("--repo", help="Project folder (defaults to the 'repo' field in the questions file)")
    ap.add_argument("--budget", type=int, default=3500)
    ap.add_argument("--max-files", type=int, default=6)
    ap.add_argument("--no-llm", action="store_true", help="Skip the LLM variant even if Ollama is running")
    ap.add_argument("--out", default="eval/results", help="Folder for results.md / results.csv")
    ap.add_argument("--answer-sheet", help="Folder to write plain vs. ctx prompts for the chatbot A/B test")
    args = ap.parse_args()

    spec = json.loads(Path(args.questions).read_text())
    repo = Path(args.repo or spec["repo"]).expanduser()
    cfg = Config.load()
    cfg.max_files = args.max_files
    if args.no_llm:
        cfg.use_llm = False
    idx = ProjectIndex(repo, cfg)

    t = time.time()
    stats = idx.update()
    index_secs = time.time() - t
    llm_on = idx.llm.can_generate
    variants = [v for v in VARIANTS if llm_on or not v[1]["llm"]]
    repo_tokens = whole_repo_tokens(idx)

    rows: List[Dict] = []
    summary: Dict[str, Dict[str, list]] = {name: {"recall": [], "hit1": []} for name, _ in variants}
    for item in spec["questions"]:
        q, exp = item["q"], set(item["expected"])
        for name, v in variants:
            got = select(idx, q, v, args.max_files)
            rec = len(exp & set(got)) / len(exp)
            summary[name]["recall"].append(rec)
            summary[name]["hit1"].append(1.0 if got and got[0] in exp else 0.0)
            rows.append({"question": q, "variant": name, "recall": round(rec, 3), "selected": " | ".join(got),
                         "missing": " | ".join(sorted(exp - set(got)))})

    # full pipeline prompts: size + speed
    builder = PromptBuilder(idx)
    sizes, times = [], []
    sheet = Path(args.answer_sheet) if args.answer_sheet else None
    if sheet:
        sheet.mkdir(parents=True, exist_ok=True)
    sheet_rows = []
    for i, item in enumerate(spec["questions"], 1):
        t = time.time()
        res = builder.build(item["q"], budget=args.budget)
        times.append(time.time() - t)
        sizes.append(res.tokens)
        if sheet:
            (sheet / f"q{i:02d}_plain.md").write_text(item["q"] + "\n")
            (sheet / f"q{i:02d}_ctx.md").write_text(res.prompt)
            sheet_rows.append({"id": f"q{i:02d}", "question": item["q"], "plain_tokens": tokens.count(item["q"]),
                               "ctx_tokens": res.tokens, "plain_score_0to2": "", "ctx_score_0to2": "", "notes": ""})
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 if sys.platform != "darwin" else 1024 * 1024)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    name = Path(args.questions).stem
    with open(out / f"{name}_per_question.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    if sheet:
        with open(sheet / "scores.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(sheet_rows[0]))
            w.writeheader()
            w.writerows(sheet_rows)

    lines = [f"# Evaluation: {name}", "",
             f"- Repository: `{repo}` ({stats['files']} files indexed, {repo_tokens:,} tokens if pasted whole)",
             f"- Index build: {index_secs:.1f}s · LLM: {'on (' + cfg.llm_model + ')' if llm_on else 'off'} · "
             f"vectors: {idx.store.get_meta('embed_model')} · token counter: {tokens.method()}",
             f"- Questions: {len(spec['questions'])} · max files per prompt: {args.max_files} · budget: {args.budget}", "",
             "## File selection (ablation)", "", "| Pipeline | Recall | Hit@1 |", "|---|---|---|"]
    for vname, _ in variants:
        r = summary[vname]
        lines.append(f"| {vname} | {statistics.mean(r['recall']):.0%} | {statistics.mean(r['hit1']):.0%} |")
    lines += ["", "## Prompt size and speed (full pipeline)", "",
              f"- Median prompt: {statistics.median(sizes):,.0f} tokens (max {max(sizes):,}) vs {repo_tokens:,} for the whole repo "
              f"→ {100 * (1 - statistics.median(sizes) / max(repo_tokens, 1)):.1f}% smaller",
              f"- Median build time: {statistics.median(times):.2f}s (max {max(times):.2f}s)",
              f"- Peak memory of this process: {peak_mb:.0f} MB (Ollama models run in their own process)", ""]
    misses = [r for r in rows if r["variant"] == variants[-1][0] and r["recall"] < 1]
    if misses:
        lines += ["## Misses in the full pipeline", ""] + [f"- {r['question']} → missing {r['missing']}" for r in misses]
    report = "\n".join(lines) + "\n"
    (out / f"{name}.md").write_text(report)
    print(report)


if __name__ == "__main__":
    main()
