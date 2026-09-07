# 0003 — Frontend admin request isolation & server binding

Status: implemented (2026-09-07)

## Context

After decoupling models from strategy (0002), the 模型管理 panel stayed empty in
the browser even though `GET /v1/admin/config` returned 200 with the `glm-52`
model. The frontend reported `config=null(未连接)` / `fetchError=Error: 请求失败（400）`.

## Root cause

Two issues, both required to produce the symptom:

1. **Corrupted run in the session DB.** An end-to-end test run (`test-multi-002`)
   had been persisted in a state that violated `DiagnosisState` validation
   ("running and terminal states cannot have pending work"). Every call to
   `GET /v1/admin/runs` and `GET /v1/admin/sessions` therefore threw
   `validation_failed` → HTTP 400 while serializing the list.
2. **`Promise.all` coupling in the frontend.** `sync()` did
   `Promise.all([getAdminRuns(), getAdminSessions(), getAdminConfig()])`. A
   single rejection rejects the whole `Promise.all`, so the 400 from the runs
   list (and sessions list) rejected the entire batch — `setAdminConfig` was
   never called even though the config request itself succeeded. The catch
   block swallowed the real error into a generic "API 尚未连接" notice.

So the model panel was empty not because of the model/strategy decoupling, but
because a broken run list poisoned the config fetch via shared `Promise.all`.

## Changes

- `frontend/src/App.tsx`: wrap each admin request in `safe(p, fallback)` so a
  failed runs/sessions list no longer takes out config (and vice versa). Config
  is now set independently; `fetchError` reflects only the config request status.
  Added a `fetchError` state so failures surface concretely instead of being
  swallowed.
- `frontend/vite.config.ts`: `server.host: true` + proxy target changed to
  `127.0.0.1:8000`; backend now started with `--host 0.0.0.0`. Windows browsers
  reaching the WSL2 Vite can now reliably proxy to the backend bound on all
  interfaces (previously both were `127.0.0.1`-only, which the WSL2 localhost
  forwarder sometimes mishandled).
- Session DB cleared (`data/web.db`) to remove the corrupted run so the list
  endpoints return 200 again.

## Verification

- `pnpm typecheck` clean.
- All four admin endpoints return 200 through the Vite proxy:
  `health`, `version`, `config`, `runs?limit=100`, `sessions?limit=100`.
- In-browser: 模型管理 panel now shows the `glm-52` card with masked key and
  editable fields; node strategy dropdowns list `glm-52`.

## Follow-up (tracked in TODO.md)

- The corrupted-run-400s-the-list problem is a backend robustness gap: a single
  bad `DiagnosisState` row should not 400 the entire list endpoint. Consider
  skipping/flagging unserializable rows instead of failing the whole list.
