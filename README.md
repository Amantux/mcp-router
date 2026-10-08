# MCP Router — Intelligent Routing Platform for MCP

Local-first platform that discovers, catalogs, classifies, deduplicates and
intelligently routes MCP servers/tools to AI agents — exposing each agent a
small, relevant, authorized tool subset instead of the whole catalog.

- **Decision engine**: [Laya](https://huggingface.co/convaiinnovations/laya)
  (typed Choice/Score/Noul questions, calibrated probabilities) + BGE-small
  embeddings, sharing one GPU (RTX 4060 8GB target) with full CPU fallback.
- **Deterministic security**: policy engine and execution manager are plain
  code; model scores can never widen access.
- **Stack**: FastAPI · SQLAlchemy 2 · PostgreSQL+pgvector · MCP Python SDK 2.x
  · React+Vite+Fluent UI v9 · Docker Compose.

Spec: `docs/SPEC.md` · Scope: `docs/scoping.md` · Conventions: `CLAUDE.md`

## Quickstart (dev)
```bash
docker compose up -d db
uv venv --python 3.12 .venv && uv pip install -e '.[dev]'
.venv/bin/uvicorn --factory mcprouter.api.app:create_app --port 8400
```
