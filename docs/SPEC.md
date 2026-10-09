# MCP Intelligent Routing Platform — Technical Specification v0.2

> **Proposal v0.2, kept as the design baseline.** Shipped behaviour differs
> in places; see [Deviations](#deviations-from-shipped-behaviour) below and the
> generated [API reference](reference/api.md).
>
> Verbatim-in-substance copy of the proposing spec. Binding scope decisions
> layered on top live in `scoping.md` (which wins where they conflict).

**Status:** Proposed · **Deployment:** local-first, single-GPU laptop ·
**Target:** Surface Laptop Studio 2, RTX 4060 Laptop GPU (8GB VRAM) ·
**Primary model:** Convai Innovations Laya

## 1. Overview
Self-hosted MCP management + intelligent routing platform: automatically
discovers, catalogs, classifies, ranks, and exposes relevant MCP servers and
tools to AI agents. Laya is the low-latency decision engine; embeddings do
semantic retrieval; deterministic policies enforce security and execution.
Runs locally on one laptop GPU; no external inference services.

Primary objectives: auto-discover MCP servers/tools · organize by domain,
capability, operation, relevance · identify duplicate/overlapping tools ·
dynamically expose only relevant tools · minimize context-window consumption ·
multi-server/multi-tool workflows · versioned tool registry · web management
UI · laptop-efficient · CPU fallback when the dGPU is unavailable.

## 2. Hardware
RTX 4060 Laptop 8GB (alt: 4050 6GB) · 16GB RAM min/32 rec · 10GB+ storage ·
Windows 11 WSL2 or Linux · CUDA FP16 · Docker Compose · exactly one GPU ·
must also operate CPU-only at reduced performance.

## 3. Performance targets (engineering targets; validate on target hardware)
100+ servers · 1,000+ indexed tools · warm routing p95 < 150ms · 10–20
retrieval candidates · 3–8 exposed tools/request · <4GB VRAM typical ·
catalog refresh <60s for 1,000 tools · offline operation.

## 4. Architecture
Agents → MCP Gateway API → {Tool Registry, Policy Engine} → Hybrid Retrieval
(vector+keyword) → Laya decision model → ranked candidates → authorization
filter → dynamic tool exposure → MCP server execution → telemetry/feedback.

Components: MCP Gateway · Tool Registry · Embedding Engine · Laya Router ·
Policy Engine · Execution Manager · Management UI.

## 5. Functional requirements
- **FR-01 Discovery**: manual registration; import existing MCP client
  configs; `tools/list` discovery; stdio + Streamable HTTP transports
  (legacy SSE optional); periodic health checks; add/remove/modify detection;
  auto catalog sync; stable internal server ids.
- **FR-02 Catalog**: per tool — ids, name, description, input schema, schema
  fingerprint+version, domain/capabilities, tags, read/write/execute class,
  required permissions, availability/health, usage stats, avg latency,
  embedding. AI-generated metadata reviewable/editable.
- **FR-03 Routing**: NL task in → caller's authorized scope → hybrid
  candidate retrieval → Laya intent classification → ranking → deterministic
  permission/availability filters → most relevant eligible tools → optional
  LLM fallback on low confidence. Laya must not generate arbitrary tool
  names or execute tools.
- **FR-04 Hierarchical classification**: intent → domain (development,
  communication, files, databases, productivity) → operation (search, read,
  write, execute) → server → tool. Never one big classification over the
  whole catalog.
- **FR-05 Dedup**: semantic similarity + descriptions + I/O schemas +
  capability classes + execution behavior → suggest preferred tools
  (reliability, permissions, latency, admin preference). Never auto-delete
  or auto-disable.
- **FR-06 Dynamic exposure**: small relevant subset per agent session;
  configurable max; session-specific visibility; stable tool ids; tool-list
  change notifications where supported; compatibility with clients that
  cache tool definitions; multi-tool workflows; deterministic fallback when
  the model is down.
- **FR-07 Security**: per-agent authorization; per-server/per-tool policies;
  read-only vs state-changing controls; explicit approval for sensitive
  actions; argument schema validation; server-side credential storage;
  secret redaction in logs AND model inputs; execution auditing; rate limits
  and timeouts. Routing scores never override authorization.
- **FR-08 Management UI**: server registration/config, tool browsing,
  search/filter, domain/capability organization, duplicate review,
  classification overrides, routing simulation, model perf metrics, GPU
  utilization, server health, execution history, version/schema tracking.

## 6. Stack
Python/FastAPI · official MCP Python SDK · Laya · PyTorch+CUDA ·
BGE-small/base embeddings · PostgreSQL+pgvector · in-process cache (Redis
optional) · React+Vite · Fluent UI v9 · Docker Compose · OpenTelemetry ·
Prometheus metrics · local API keys (OIDC optional). Modular monolith.

## 7. Inference
One GPU shared by embedding model + Laya. Load once at startup; FP16 where
compatible; inference mode (no grad); batch when beneficial; cache tool
embeddings, recompute only on metadata change; cap concurrent inference;
GPU memory monitoring; CPU fallback; idle/battery mode. Benchmark real VRAM
and latency — never assume.

## 8. Data models (wire shapes)
MCPServer {id, name, transport stdio|streamable-http|sse, endpoint?,
enabled, status healthy|degraded|offline, version, lastDiscoveredAt} ·
MCPTool {id, serverId, name, description, inputSchema, schemaHash,
categories[], tags[], operation read|write|execute|unknown,
requiredScopes[], enabled, version} · RoutingDecision {requestId,
selectedTools[], scores{}, modelVersion, fallbackUsed, latencyMs}. Plus
execution traces, feedback, policy rules, tool versions, model evals.

## 9. API
`POST /api/v1/route` {query, agent_id, max_tools, allowed_servers?} →
{request_id, tools:[{server, tool, score}], fallback_used, latency_ms}.
Also: GET/POST /api/v1/servers · POST /api/v1/servers/{id}/refresh ·
GET /api/v1/tools · POST /api/v1/route/evaluate · GET /api/v1/models/health ·
Prometheus metrics at `/metrics`. The gateway also exposes a standards-compatible MCP
endpoint.

## 10. Evaluation & observability
Track: top-1 accuracy, top-5 recall, wrong-tool rate, no-match accuracy,
latency p50/95/99, GPU memory, CPU-fallback frequency, execution success,
server health, catalog drift, model fallback frequency. Datasets must cover
ambiguous requests, overlapping tools, unavailable servers, unauthorized
operations, multi-step workflows.

**Acceptance targets** (empirical validation required): top-1 ≥90% ·
top-5 recall ≥98% · zero unauthorized executions in security tests · warm
p95 <150ms · typical VRAM <4GB · ≥1,000 tools.

## 11. Deployment
Docker Compose: backend+gateway, inference runtime, pgvector, React UI.
Redis/external LLM fallback/K8s optional. NVIDIA GPU passthrough under
WSL2/Docker. Operating modes: Performance (models loaded, concurrent
inference) · Balanced (reduced concurrency, cached retrieval) · Battery
(CPU or reduced GPU, optional unload after idle, no mandatory GPU polling).

## 12. Phases
1 Registry (registration, discovery, versioned catalog, searchable UI,
schema-change detection) · 2 Intelligent routing (retrieval, Laya,
hierarchical classification, ranking, eval framework) · 3 Gateway & security
(dynamic exposure, per-agent policies, secure execution, audit, multi-tool) ·
4 Optimization (RTX 4060 benchmarking, FP16 tuning, cache optimization,
battery mode, CPU fallback, feedback/fine-tuning).

## 13. Definition of done
≥100 servers / ≥1,000 tools catalogable · auto-classified + searchable ·
Laya routes to relevant eligible tools · agents get dynamically filtered
definitions · execution independently authorized + validated · registry
changes versioned/auditable · runs on one RTX 4060 Laptop GPU · CPU fallback
works · routing quality/latency meet validated thresholds · deployable via
Docker Compose.

**Design principle:** keep it lightweight — embeddings narrow the space,
Laya makes fast decisions, deterministic code enforces security and executes
tools. No unnecessary services, GPU replicas, or large generative models in
the routing path.

## Deviations from shipped behaviour

- **Metrics path.** Prometheus metrics are served at `/metrics`, outside
  `/api/v1`, and from v0.6 need the admin token when one is configured.
- **`agent_id` in `/route`.** Ignored. The agent is always the authenticated
  principal (security-model.md §1).
- **LLM fallback (§5).** Out of scope (scoping.md #2): low confidence falls
  back to deterministic ranking.
- **Response casing.** `/route` keeps the snake_case shape above; the rest of
  the API is camelCase (INSTALL.md, API conventions).
