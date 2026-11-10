# Cloud development setup

Use the existing `/workspace/CTX-Engine` checkout. Do not create a Git worktree unless explicitly requested.

## Install and test

Python 3.12 was used successfully. The project requires Python >=3.9 and has no dependency lockfile.

```bash
cd /workspace/CTX-Engine
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
```

## Start in offline mode

```bash
cd /workspace/CTX-Engine
CTX_HOME=/workspace/CTX-Engine/.ctx/home CTX_USE_LLM=false \
  .venv/bin/ctx serve --no-open --add examples/demo-shop
```

Keep the server process running. Runtime state and indexes are in ignored `.ctx` directories. Processes must be restarted in new cloud tasks.

Validate locally with `curl -fsS http://127.0.0.1:7777/api/health`. Fetch `/api/projects`, then `/api/projects/{id}` to confirm indexing completes. POST `/api/projects/{id}/prompt` with JSON `{"question":"why is the login button not working","budget":3000,"use_llm":false}` and verify a prompt containing code is returned.

Validated: 23 tests passed, dependency checks passed, the web page and health API responded, and the demo's 20 files produced a 1,830-token prompt. One dependency deprecation warning was emitted by the test client.

Ollama and model downloads are optional and were not installed. The demo shop is an indexing fixture; its separate Flask/React servers are not required for the engine.

The cloud configuration service could not confirm saving install/start instructions during setup. These commands are retained here, but environment configuration publication and fresh-task restoration have not been verified.
