# Architecture

## Goal

A general chatbot answers code questions badly when it can't see the code, and pasting a whole repository is too big, slow and risky. ctx sits between the project folder and *any* chatbot. It picks the few pieces of the project that matter for one question and packages them as a single self-contained prompt.

```
 project folder ──► ctx (on the laptop) ──► prompt ──► user pastes into ChatGPT / Gemini / Claude / CLI bot
```

Because a browser chatbot can't ask follow-up questions of the tool, the prompt has to be complete on its own. That makes **retrieval quality** the core problem.

## Components

```
            ┌──────────────────────── core engine (Python library) ───────────────────────┐
            │  index/   scanner → parser → chunker → graph → summarizer → embedder → store │
            │  query/   expand → retriever (search + graph) → rerank                        │
            │  prompt/  snippets → builder (packing) → redactor                             │
            └───────────────────────────────▲──────────────────────────▲──────────────────┘
                                            │                          │
                         manager.py + server.py (FastAPI, 127.0.0.1)   cli.py (Typer)
                                            │
                                 web app (HTML/JS, no build step)
```

All interfaces call the same core. A new interface (browser extension, desktop window, MCP server) only needs a thin layer on top.

## Indexing

| Module | Responsibility |
|---|---|
| `scanner.py` | Walks the folder. Honours nested `.gitignore` and `.ctxignore`; skips dependency/build folders, binaries, minified files and >400 KB files. **Never reads** `.env*`, keys, keystores or credential files. |
| `parser.py` | Per-language extraction of symbols (name, kind, line range, signature, docstring), imports, and HTTP routes. Python uses `ast`. Brace languages use regexes plus a string- and comment-aware brace matcher. Route prefixes (`Blueprint(url_prefix=)`, `APIRouter(prefix=)`) are applied. |
| `chunker.py` | Turns symbols into search chunks that follow code structure. A symbol up to 80 lines is one chunk; bigger ones are split by their children (methods), then into 60-line windows; module-level code is grouped separately. |
| `graph.py` | Resolves import specs to files (Python relative/absolute, JS relative/aliases, Java/Kotlin packages, Go modules, Rust `mod`/`use`, C includes). Links client calls to server routes by path pattern and method, with a tail-match fallback for routers mounted under a prefix. |
| `summarizer.py` | Writes a heuristic summary straight away (docstring + symbols + routes + imports), then upgrades it to a 2-sentence summary from `qwen3:4b` with thinking turned off. |
| `embedder.py` | `embeddinggemma` through Ollama using its retrieval prompts (`task: code retrieval | query:` / `title: none | text:`). Offline fallback: a 512-dim signed feature-hashing vector of identifier sub-words and bigrams. |
| `store.py` | One SQLite file: `files`, `symbols`, `chunks` (+ `vec` BLOB), `imports`, `routes`, `edges`, `meta`, and an FTS5 table with the porter stemmer. WAL mode lets the web app read while the indexer writes. |
| `indexer.py` | Incremental updates by SHA-1 content hash. A full refresh re-processes only changed files; the watcher passes exact paths. If the embedding model changes, all vectors are rebuilt so they're never mixed. |
| `watcher.py` | `watchdog` observer; events are batched for 1.5 s, deleted folders remove their files, then one `update(paths=…)` runs. |

## Prompt pipeline

1. **Expand** (`expand.py`). The question's own terms (identifier-split, stop-words removed), plus related terms from the local LLM (`{"keywords": [...]}`) merged with a hand-written dictionary of ~60 dev concepts. Quoted strings and error messages become exact-phrase searches; file names in the question get a direct boost.
2. **Search** (`retriever.search`). Four signals over chunks or files:
   - BM25 on the question terms, with 6-char prefix queries so "configured" finds `config`
   - BM25 on the expanded terms (weight 0.5)
   - cosine similarity on embeddings (weight 1.0; 0.4 for the offline hash vectors)
   - file-path word match (weight 0.7)

   Each signal is normalized to [0, 1] per file and combined as a weighted sum plus a small reciprocal-rank term (CombSUM + RRF). Pure RRF was tried first, but it threw away how much better a rare-word match was. Priors: docs, config and data ×0.5, tests ×0.6, examples ×0.75, unless the question asks about them.
3. **Graph** (`retriever.expand_graph`). The six top files pull in their neighbours. API links get weight 0.75; imports get 0.5 (outgoing) or 0.35 (incoming), divided by log₂(2 + in-degree) so hubs like `__init__.py`, `typing.py` or a shared `utils` don't flood the list. Each file is boosted once, by its strongest neighbour.
4. **Rerank + rewrite** (`rerank.py`). One JSON call to `qwen3:4b` with the overview and the ~15 candidates' cached summaries returns `{"files": [...], "question": "..."}`. Fallback: keep files scoring ≥30% of the top, plus a template rewrite naming them.
5. **Snippets** (`snippets.py`). Files of ≤90 lines go in whole. For larger files, chunks are ranked by their retrieval score plus query-word overlap, adjacent ranges are merged, and up to 4 are kept per file.
6. **Pack** (`builder.py`). Order: instructions → question (rewritten + original) → overview → connections between the chosen files → file list with summaries → code. Code blocks are added in rank order until the token budget runs out; a block that doesn't fit is trimmed by lines. If the header alone takes >45% of a small budget, the overview is dropped.
7. **Redact** (`redactor.py`). Run per snippet (to report file and line) and once over the whole prompt. Covers private keys, AWS, GitHub, OpenAI/Anthropic, Stripe, Slack, Google, SendGrid, Twilio, JWTs, credentials in URLs, bearer tokens, and secret-named assignments. Placeholders, plain-English messages and already-redacted text are left alone.

## Design decisions

- **Prompt builder, not a chatbot plugin.** It works with every chatbot, including browser ones, with no integration needed.
- **Small local models.** The target machine is an 8 GB MacBook Air. `qwen3:4b` (2.5 GB) and `embeddinggemma` (622 MB) leave room for the editor and browser. The LLM is used only where language understanding matters (expansion, summaries, rerank, rewrite); everything else is deterministic and fast.
- **SQLite + NumPy instead of a vector database.** A few thousand chunks fit in memory as one matrix; one file is simpler to ship and inspect.
- **Offline fallback for every LLM step**, so the tool never blocks on Ollama and the ablation study can measure each component's contribution.
- **Local-only server** (bound to 127.0.0.1, with a Host-header check), because it can read source code.
