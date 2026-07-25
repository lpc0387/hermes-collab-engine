# Auto Provider Prefix for OpenCode (2026-06-18)

## Problem

When `agent="opencode"`, the worker/leader model needs a `opencode-go/` provider
prefix (e.g. `opencode-go/deepseek-v4-flash`) so OpenCode knows which backend to
use. Without it, opencode raises `ProviderModelNotFoundError`.

Before this fix, users had to manually pass `--leader-model "opencode-go/..." --worker-model "opencode-go/..."` to `hermes-collab run`.

## Three-layer fix

### A — start.py (launcher)

In `start.py`, after reading config and before setting env vars / building CLI
args, if `worker_agent == "opencode"` the leader and worker model values are
prefixed with `"opencode-go/"` unless they already carry it.

Affected scope:
- `ANTHROPIC_MODEL` env var
- `HERMES_COLLAB_LEADER_MODEL` / `HERMES_COLLAB_WORKER_MODEL` env vars
- `--leader-model` / `--worker-model` CLI args passed to the server subprocess

### B — agents.py (engine layer)

New field `AgentBackend.auto_prefix` (default `""`). When set (e.g.
`auto_prefix="opencode-go/"` on the opencode backend), `build_command()`
automatically prepends it to the model if:
- `effective_provider` is `None` (no ProviderProfile configured), AND
- the model doesn't already start with the prefix

This covers:
- Workers spawned by the engine (`_run_worker`)
- Any future code path that calls `backend.build_command()` with a model

### C — Hermes Agent memory

Memory updated to remove the "must pass --leader-model/--worker-model"
instruction. The system now handles this automatically.

## Files changed

- `/root/hermes-collab-engine/start.py` — prefix logic before env/CLI construction
- `/root/hermes-collab-engine/src/hermes_collab_engine/agents.py` — `auto_prefix` field + usage in `build_command` + set on opencode backend
- `/root/.hermes/memories/MEMORY.md` — updated opc entry
