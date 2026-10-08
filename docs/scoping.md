# Scoping decisions (binding for v0.1)

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
   requires_approval. OIDC is out. Auth disabled only when no keys are
   configured, with a loud startup warning.
7. **Transports**: stdio + streamable-http first-class; legacy SSE
   best-effort (client-side only) behind the same connector interface.
8. **100 servers / 1,000 tools** is the catalog SCALE target, validated with
   the synthetic testbed fleet (we will not run 100 real servers in CI).
9. **UI**: React+Vite+Fluent UI v9, English-only, desktop-first. Talks to the
   REST API only; generated TS types from the OpenAPI schema.
10. **Spec §9 response shape is canonical**; scores in responses are the
    decision model's calibrated probabilities (or fallback heuristic scores,
    flagged by `fallback_used`).
