# Evaluation: demo-shop

- Repository: `examples/demo-shop` (20 files indexed, 2,448 tokens if pasted whole)
- Index build: 0.0s · LLM: off · vectors: hash-512 · token counter: estimate
- Questions: 12 · max files per prompt: 6 · budget: 3500

## File selection (ablation)

| Pipeline | Recall | Hit@1 |
|---|---|---|
| keywords only | 88% | 75% |
| + query expansion | 88% | 75% |
| + vectors | 96% | 75% |
| + graph | 92% | 75% |

## Prompt size and speed (full pipeline)

- Median prompt: 1,717 tokens (max 2,008) vs 2,448 for the whole repo → 29.9% smaller
- Median build time: 0.01s (max 0.01s)
- Peak memory of this process: 54 MB (Ollama models run in their own process)

## Misses in the full pipeline

- checkout says please log in even after logging in → missing web/src/App.jsx
- user stays logged out after page refresh → missing web/src/hooks/useAuth.js
