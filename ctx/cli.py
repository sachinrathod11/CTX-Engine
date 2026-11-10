"""Command line: `ctx serve` for the web app, plus index/prompt/status/watch/doctor."""
from __future__ import annotations

import json
import sys
import time
import webbrowser
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import __version__, tokens
from .config import HOME_DIR, Config
from .index.indexer import ProjectIndex
from .llm import Ollama

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Local project context engine: turn a question into a project-aware prompt for any chatbot.")
con = Console(stderr=True)


def _index(path: Path, summarize: Optional[bool] = None, quiet: bool = False) -> ProjectIndex:
    idx = ProjectIndex(path)
    if quiet:
        idx.update(summarize=summarize)
        return idx
    with Progress(TextColumn("[bold]{task.fields[stage]:<10}"), BarColumn(), TextColumn("{task.completed}/{task.total}"),
                  TextColumn("[dim]{task.fields[msg]}"), TimeElapsedColumn(), console=con, transient=True) as prog:
        task = prog.add_task("index", total=1, stage="scan", msg="")

        def cb(stage, done, total, msg):
            prog.update(task, completed=done, total=max(total, 1), stage=stage, msg=msg[-50:])
        stats = idx.update(progress=cb, summarize=summarize)
    con.print(f"[green]Indexed[/] {stats['files']} files in {stats['seconds']}s "
              f"({stats['changed']} changed, {stats['removed']} removed, {stats['llm_summaries']} LLM summaries, "
              f"{stats['embedded_chunks']} chunks embedded)")
    return idx


@app.command()
def index(path: Path = typer.Argument(Path("."), help="Project folder"),
          fast: bool = typer.Option(False, "--fast", help="Skip LLM file summaries (much quicker)")):
    """Build or refresh the index for a project (only changed files are re-processed)."""
    _index(path, summarize=False if fast else None)


@app.command()
def prompt(question: str = typer.Argument(..., help="Your question, e.g. \"why is the login button not working\""),
           path: Path = typer.Option(Path("."), "--path", "-p", help="Project folder"),
           budget: Optional[int] = typer.Option(None, "--budget", "-b", help="Max prompt size in tokens"),
           out: Optional[Path] = typer.Option(None, "--out", "-o", help="Also write the prompt to this file"),
           copy: bool = typer.Option(True, "--copy/--no-copy", help="Copy the prompt to the clipboard"),
           use_llm: bool = typer.Option(True, "--llm/--no-llm", help="Use the local LLM for expansion, rerank and rewrite"),
           include: List[str] = typer.Option([], "--include", "-i", help="Force-include a file (repeatable)"),
           exclude: List[str] = typer.Option([], "--exclude", "-x", help="Leave a file out (repeatable)"),
           show: bool = typer.Option(False, "--show", help="Print the prompt to the terminal"),
           as_json: bool = typer.Option(False, "--json", help="Print the full result as JSON")):
    """Build a project-aware prompt for QUESTION and copy it to the clipboard."""
    from .prompt.builder import PromptBuilder
    idx = ProjectIndex(path)
    first = not idx.is_built
    if first:
        con.print("[yellow]No index yet - building one first (use `ctx index` for LLM summaries).[/]")
    _index(path, summarize=False, quiet=not first)  # cheap refresh: only changed files
    t = time.time()
    res = PromptBuilder(idx).build(question, budget=budget, include=include, exclude=exclude, use_llm=use_llm)
    if as_json:
        print(json.dumps(res.to_dict(), indent=2))
        return
    tbl = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    tbl.add_column("File")
    tbl.add_column("Lines", style="dim")
    tbl.add_column("Why", style="dim")
    label = {"bm25": "keywords", "bm25_expanded": "related words", "vector": "meaning", "path": "file name", "phrase": "exact text"}
    for f in res.files:
        if f.selected:
            why = [label.get(r, r) for r in f.reasons]
            tbl.add_row(f.path, f.lines or "-", ", ".join(why[:3])[:70])
    con.print(Panel.fit(res.rewritten, title="Rewritten question", border_style="cyan"))
    con.print(tbl)
    red = f", [yellow]{len(res.redactions)} secret(s) redacted[/]" if res.redactions else ""
    con.print(f"[bold]{res.tokens}[/] tokens (budget {res.budget}){red} · built in {time.time() - t:.1f}s · "
              f"modes: {', '.join(f'{k}={v}' for k, v in res.modes.items())}")
    last = idx.root / ".ctx" / "last_prompt.md"
    last.write_text(res.prompt)
    if out:
        out.write_text(res.prompt)
        con.print(f"Saved to {out}")
    if copy:
        try:
            import pyperclip
            pyperclip.copy(res.prompt)
            con.print("[green]Copied to clipboard.[/] Paste it into ChatGPT, Gemini, Claude or any chatbot.")
        except Exception:
            con.print(f"[yellow]Clipboard not available[/] (Linux: install xclip or wl-clipboard). Prompt saved to {last}")
    if show or not copy and not out:
        print(res.prompt)


@app.command()
def serve(port: Optional[int] = typer.Option(None, help="Port (default 7777)"),
          host: Optional[str] = typer.Option(None, help="Interface to bind (default 127.0.0.1; Docker uses 0.0.0.0)"),
          open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the web app in your browser"),
          add: Optional[Path] = typer.Option(None, "--add", help="Add this project folder on start")):
    """Start the local web app (http://127.0.0.1:7777)."""
    import uvicorn
    from .server import create_app
    cfg = Config.load()
    port = port or cfg.server_port
    host = host or cfg.server_host
    web = create_app(cfg)
    if add:
        web.state.manager.add(str(add))
    url = f"http://127.0.0.1:{port}"
    con.print(f"[bold green]ctx[/] web app running at [link={url}]{url}[/link]  (Ctrl+C to stop)")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    uvicorn.run(web, host=host, port=port, log_level="warning")


@app.command()
def status(path: Path = typer.Argument(Path("."))):
    """Show what's in a project's index."""
    idx = ProjectIndex(path)
    s = idx.status()
    if not s["indexed_at"]:
        con.print("Not indexed yet. Run `ctx index`.")
        raise typer.Exit(1)
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["indexed_at"]))
    con.print(f"[bold]{idx.root}[/]\n  files {s['files']} · chunks {s['chunks']} (embedded {s['embedded_chunks']} with {s['embed_model']})"
              f"\n  LLM summaries {s['llm_summaries']}/{s['files']} · import links {s['import_edges']} · API links {s['http_edges']}"
              f"\n  last indexed {when}")


@app.command()
def overview(path: Path = typer.Argument(Path("."))):
    """Print the short project overview that goes at the top of every prompt."""
    idx = ProjectIndex(path)
    if not idx.is_built:
        _index(path, summarize=False)
    print(idx.overview())


@app.command()
def watch(path: Path = typer.Argument(Path("."))):
    """Keep the index fresh while you code (Ctrl+C to stop)."""
    from .index.watcher import IndexWatcher
    idx = _index(path)

    def report(stats):
        con.print(f"[dim]{time.strftime('%H:%M:%S')}[/] updated: {stats}")
    w = IndexWatcher(idx, on_update=report).start()
    con.print(f"Watching {idx.root} ...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        w.stop()


@app.command()
def doctor():
    """Check Ollama, models, clipboard and tokenizer."""
    cfg = Config.load()
    st = Ollama(cfg).status()
    ok = lambda b: "[green]ok[/]" if b else "[red]missing[/]"
    con.print(f"ctx {__version__} · Python {sys.version.split()[0]} · config {HOME_DIR / 'config.json'}")
    con.print(f"Ollama at {cfg.ollama_url}: {ok(st['ollama_running'])}")
    con.print(f"  LLM {cfg.llm_model}: {ok(st['llm_ready'])}" + ("" if st["llm_ready"] else f"  ->  ollama pull {cfg.llm_model}"))
    con.print(f"  embeddings {cfg.embed_model}: {ok(st['embed_ready'])}" + ("" if st["embed_ready"] else f"  ->  ollama pull {cfg.embed_model}"))
    if not st["ollama_running"]:
        con.print("  [yellow]Without Ollama ctx still works in offline mode (keyword search, no rewrite).[/]")
    try:
        import pyperclip
        pyperclip.paste()
        con.print(f"Clipboard: {ok(True)}")
    except Exception:
        con.print("Clipboard: [yellow]not available[/] (Linux: sudo apt install xclip  or  wl-clipboard)")
    con.print(f"Token counting: {tokens.method()}")


@app.command()
def config(set_: List[str] = typer.Option([], "--set", help="key=value, e.g. --set llm_model=qwen3:1.7b")):
    """Show or change settings (saved in ~/.ctx/config.json)."""
    cfg = Config.load()
    for kv in set_:
        k, _, v = kv.partition("=")
        if not hasattr(cfg, k):
            con.print(f"[red]Unknown setting {k}[/]")
            raise typer.Exit(1)
        cur = getattr(cfg, k)
        setattr(cfg, k, v.lower() in ("1", "true", "yes") if isinstance(cur, bool) else type(cur)(v) if not isinstance(cur, list) else v.split(","))
    if set_:
        con.print(f"Saved {cfg.save()}")
    from dataclasses import asdict
    print(json.dumps(asdict(cfg), indent=2))


@app.command()
def version():
    """Print the version."""
    print(__version__)


if __name__ == "__main__":
    app()
