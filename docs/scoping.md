# Scoping decisions (v0.1 baseline; amended)

> **Design baseline, not current behaviour.** These decisions were binding for
> v0.1. Where shipped behaviour moved on, the change is listed under
> [Amendments](#amendments-since-v01) and the item is corrected in place.
> Current operator docs: [deploy.md](deploy.md), [security-model.md](security-model.md).

Reasonable assertions made against spec v0.2 — each reversible later, none
silently:

1. **Phases 1–3 fully built; Phase 4 split.** Software half of phase 4
   (operating modes, caches, CPU fallback, bench harness) ships now; the
   GPU/FP16/VRAM numbers REQUIRE the RTX 4060 target machine and are a
   hardware-validation day, tracked in docs/hardware-validation.md. The dev
   box has no GPU — targets stay "engineering targets" until then.
2. **Laya is a typed decision model** (Choice/Score/Noul, calibrated probs,
   ~33–40ms/question, Apache 2.0, convaiinnovations/laya) — NOT a generative
   LLM. Routing uses: Choice for domain/operation/server, Score for candidate
   ranking, Noul for no-match. The optional "LLM fallback on low confidence"
   from the spec is OUT of v0.1 (deterministic fallback instead); the seam is
   the DecisionModel protocol.
3. **Modular monolith**: one FastAPI process hosts API + gateway + in-process
   inference. No Redis, no sidecars, no Kubernetes. In-process caches.
4. **Sync SQLAlchemy + psycopg3** (threadpool endpoints): latency budget is
   inference-dominated; boring beats async plumbing. Revisit with evidence.
5. **Embeddings**: BGE-small (384-dim) under the optional [inference] extra;
   a deterministic hash-projection backend (same 384 dims) is the always-on
   fallback so the stack runs with zero ML deps. Backend provenance is stored;
   vectors from different backends are never compared.
6. **Auth v1 = local API keys** (agent_id:key pairs, hashed at rest), scope
   model = per-agent PolicyRules with read<write<execute ceilings +
   requires_approval. OIDC is out. *(Amended)* Auth is disabled (dev mode)
   only when there are no agent keys, no admin token **and** zero principals
   in the database, with a loud warning; see security-model.md §1.
7. **Transports**: stdio + streamable-http first-class; legacy SSE
   best-effort (client-side only) behind the same connector interface.
8. **100 servers / 1,000 tools** is the catalog SCALE target, validated with
   the synthetic testbed fleet (we will not run 100 real servers in CI).
9. **UI**: React+Vite+Fluent UI v9, English-only, desktop-first. Talks to the
   REST API only; generated TS types from the OpenAPI schema.
10. **Spec §9 response shape is canonical**; scores in responses are the
    decision model's calibrated probabilities (or fallback heuristic scores,
    flagged by `fallback_used`).

## Amendments since v0.1

- **#1 Deployment.** The router ships as a container image with compose
  files (0.5.0), a zero-ML base and an inference flavor; see deploy.md.
- **#3 Monolith, one worker.** Unchanged, and enforced: only the
  process holding a Postgres advisory lock runs the background loops.
- **#4 Sync SQLAlchemy.** Still true for the REST API and the services; the
  MCP gateway is async (MCP SDK 2.x) and calls the sync services in threads.
- **#6 Auth.** Dev mode needs no keys, no admin token and zero principals.
  The container binds to loopback when no admin token is set, and a
  `Host` allowlist (421) guards every path.
- **#9 UI.** The dashboard is served by the API itself at `/` (0.5.0).
- **Schema.** `create_all` plus additive column fixes until 0.5; Alembic
  migrations from 0.6 (docs/upgrade.md).
