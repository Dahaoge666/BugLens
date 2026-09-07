# 0002 — Model configuration / strategy decoupling

Status: implemented (2026-09-07)

## Context

Before this change every node carried a bare model name (`NodePolicy.model: str`)
and a single OpenAI client was configured globally via `configure_openai_provider`.
That meant all nodes had to share one endpoint and one set of credentials, and
there was no first-class way to run different nodes against different providers.

## Decision

Decouple **what models exist** from **which node uses which model**.

1. A named `ModelConfig` registry (`profiles.<name>.models`) holds each model's
   `model`, `base_url`, `api_key`, `timeout` and `streaming`.
2. `NodePolicy.model` becomes a **reference name** (key into the registry) with a
   bare-name back-compat fallback for profiles that pre-date the registry.
3. `RuntimePolicy.model(name)` resolves a name to a `ModelConfig`, falling back to
   a bare model name → a `ModelConfig` with only the model field.
4. `OpenAINodeRunner` builds (and caches) one `OpenAIChatCompletionsModel` per
   `ModelConfig`, binding a dedicated `AsyncOpenAI` client so each endpoint gets
   its own credentials/timeout. Streaming is decided **per model**, not globally.
5. The `api_key` is masked in admin API responses and excluded from the config
   snapshot hash; a masked/blank submitted key is treated as "keep existing".

## Changes

- `app/config.py`: `ModelConfig`; `NodePolicy.model` is now a reference name;
  `RuntimePolicy.models` + `RuntimePolicy.model()`; `DEFAULT_PROFILE` ships a
  `default` model; `snapshot_id` excludes `api_key`.
- `app/agents.py`: `NodeRuntimeContext.model_config`; `OpenAINodeRunner._model_instance`
  / `_model_arg`; `default_streaming` with `streaming` back-compat alias; per-model
  streaming override in `run`.
- `app/graph.py`: `_runtime_for_config` resolves and passes `model_config`.
- `app/bootstrap.py`: runner constructed with `default_streaming` (global client
  still configured for the bare-name fallback path).
- `app/application.py`: `_check_model_credentials` accepts env vars **or** a model
  with credentials in the default profile's registry.
- `app/admin.py`: `config()` masks `api_key`; `apply_config` preserves existing
  keys on blank/masked submission; `health()`/`bootstrap_status()` credential
  check covers the models registry; `logging.exception` on runtime failure.
- `frontend/src/App.tsx`: SettingsPage split into a **模型管理** panel
  (per-model CRUD: name / model / base_url / api_key / timeout / streaming) and a
  **节点策略** panel where each node selects its model via a dropdown of registry keys.

## Verification

- `ruff` clean; 29 unit tests pass; `pnpm typecheck` clean.
- Real end-to-end run on the MaaS GLM 5.2 gateway: `analyze` + `summarize` nodes
  executed via a dedicated `glm-52` client (custom `base_url` + `api_key` +
  `streaming: true`) → `completed / inconclusive`, no `runtime_failed`.
- `GET /v1/admin/config` returns `api_key: "****"` and the node `model` is the
  registry reference (`glm-52`); `GET /v1/admin/health` reports
  `model_credentials: ok` from the registry credentials.

## Known follow-up (tracked in TODO.md)

When `max_clarification_rounds` is exhausted and the analyze node is re-entered,
a non-`NodeExecutionError` can surface as `runtime_failed`. This is pre-existing
graph/clarifier interaction, unrelated to this decoupling; left as a TODO item.
