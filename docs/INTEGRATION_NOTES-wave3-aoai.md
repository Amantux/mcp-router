# Wave-3: Azure OpenAI backends — integration notes

## Verified wire shapes (2026-10-08)
Source: https://raw.githubusercontent.com/Azure/azure-rest-api-specs/main/specification/ai/data-plane/OpenAI.v1/azure-v1-v1-generated.json
and https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle
- Base `{endpoint}/openai/v1`, no `api-version`; auth header `api-key`.
- `POST /chat/completions`: `model`=deployment, `temperature`, `max_completion_tokens`
  (`max_tokens` deprecated), `response_format={"type":"json_schema","json_schema":{"name","schema","strict":true}}`.
- `POST /embeddings`: `{model, input:[str], dimensions}`; `dimensions` "only supported in
  text-embedding-3 and later models"; response `data[i] = {index, embedding, object}`.

## Believed, not verified
- Strict mode requires `additionalProperties:false` + all properties required, and forbids
  open-keyed objects; so choice `probabilities` is an ARRAY aligned with the options order
  (deviation from the brief's "object"). Strict-mode enum size limits may reject very large
  option lists (HTTP 400 -> DecisionRuntimeError -> deterministic fallback).

## Public API (wiring run)
- `mcprouter.settings.AoaiSettings.from_env()` (endpoint, api_key, chat_deployment,
  embedding_deployment, max_retries).
- `mcprouter.inference.aoai.build_aoai_decision_model(cfg, settings.decision_timeout_s) -> ValidatedDecisionModel`
  (name `aoai:<chat_deployment>`; has `score_batch`, sequential ~N requests).
- `mcprouter.inference.aoai.AoaiEmbeddingBackend(cfg)` (name `aoai:<embedding_deployment>`, 384-dim, L2-normalized, 64/request, 30s timeout).
- Config errors raise `AoaiConfigError` (subclass of `ModelUnavailableError`) at construction.
- Map `MCPR_DECISION_BACKEND=aoai` / `MCPR_EMBEDDING_BACKEND=aoai` in engine.py (not done here).

## Follow-ups
- Unify `_validate_aoai_endpoint` with executor A's `inference/urlcheck.py`.
- No overall deadline across retries (per-attempt timeout only); the engine's per-call deadline must bound it.
- `trust_env` left at httpx default (proxy env honoured).
