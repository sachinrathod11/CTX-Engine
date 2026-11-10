# Evaluation: flask

- Repository: `~/code/flask` (216 files indexed, 244,194 tokens if pasted whole)
- Index build: 0.1s · LLM: off · vectors: hash-512 · token counter: estimate
- Questions: 14 · max files per prompt: 6 · budget: 3500

## File selection (ablation)

| Pipeline | Recall | Hit@1 |
|---|---|---|
| keywords only | 93% | 64% |
| + query expansion | 93% | 71% |
| + vectors | 93% | 71% |
| + graph | 93% | 71% |

## Prompt size and speed (full pipeline)

- Median prompt: 3,466 tokens (max 3,494) vs 244,194 for the whole repo → 98.6% smaller
- Median build time: 0.04s (max 0.07s)
- Peak memory of this process: 63 MB (Ollama models run in their own process)

## Misses in the full pipeline

- how does the test client make requests in tests? → missing src/flask/testing.py
