# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Canonical dev guide

`AGENTS.md` at the repo root is the long-form development guide and is kept up to date by maintainers. Read it before non-trivial work. This file only surfaces the highest-leverage facts and points to AGENTS.md for the rest.

## Commands

Activate the venv first (`source .venv/bin/activate` or `source venv/bin/activate`).

```bash
# Tests — ALWAYS use the wrapper, not raw pytest
scripts/run_tests.sh                                    # full suite, CI-parity
scripts/run_tests.sh tests/gateway/                     # one directory
scripts/run_tests.sh tests/agent/test_foo.py::test_x    # one test
scripts/run_tests.sh -v --tb=long                       # pass-through pytest flags

# Install / run
uv pip install -e ".[all,dev]"   # full dev install (uses uv + Python 3.11)
./hermes                          # local launcher; auto-detects venv
hermes --tui                      # Ink (React) TUI instead of classic CLI
hermes doctor                     # diagnostics

# TUI (ui-tui/)
cd ui-tui && npm install
npm run dev          # watch mode (rebuilds hermes-ink + tsx --watch)
npm run build        # full build
npm run type-check   # tsc --noEmit
npm run lint         # eslint
npm run fmt          # prettier
npm test             # vitest

# Release
scripts/release.py
```

`scripts/run_tests.sh` enforces CI parity (unsets `*_API_KEY`/`*_TOKEN`/etc., `TZ=UTC`, `LANG=C.UTF-8`, `-n 4` xdist workers). Calling `pytest` directly on a multi-core dev box with API keys set is the leading cause of "works locally, fails in CI" incidents — and the reverse. `tests/conftest.py` enforces the same hermetic env as an autouse fixture, but use the wrapper.

## Architecture, big picture

Two long-lived "god" modules sit at the center, plus a self-registering tool layer and a multi-platform messaging gateway:

- **`run_agent.py`** — `AIAgent` class. Synchronous tool-calling loop in `run_conversation()`: call provider → if `tool_calls`, dispatch each via `handle_function_call()` → append tool result messages → repeat until `max_iterations` or no tool calls. Messages are OpenAI-format. Reasoning lives in `assistant_msg["reasoning"]`. `__init__` takes ~60 params; only touch what you need.
- **`cli.py`** — `HermesCLI` orchestrator. Rich for panels, prompt_toolkit for input. `process_command()` dispatches slash commands by canonical name resolved through the central registry (`hermes_cli/commands.py`).
- **`model_tools.py` + `tools/registry.py` + `toolsets.py`** — every `tools/*.py` calls `registry.register()` at import time. Importing `model_tools` triggers tool discovery and plugin discovery (`discover_plugins()`). Tools must return JSON strings. Adding a tool = create `tools/your_tool.py` with a `registry.register()` call + add the name to `_HERMES_CORE_TOOLS` (or a new toolset) in `toolsets.py`. No manual import list.
- **`gateway/`** — `gateway/run.py` runs the messaging gateway. Per-platform adapters in `gateway/platforms/` (telegram, discord, slack, whatsapp, signal, matrix, email, sms, dingtalk, feishu, qqbot, webhook, api_server, homeassistant, ...). Two-guard model: `gateway/platforms/base.py` queues messages while a session is active; `gateway/run.py` intercepts control commands (`/stop`, `/new`, `/queue`, `/status`, `/approve`, `/deny`) before they reach `running_agent.interrupt()`. Any new command that must reach the runner while the agent is blocked must bypass BOTH guards.
- **`ui-tui/` (Ink/React) + `tui_gateway/` (Python)** — `hermes --tui` spawns Node + Python over newline-delimited JSON-RPC on stdio. TS owns the screen; Python owns sessions, tools, models, slash logic. Most slash commands route through `slash.exec` to a persistent `_SlashWorker`. The `hermes dashboard` web UI embeds the real `hermes --tui` over a PTY WebSocket (`hermes_cli/pty_bridge.py` + `@app.websocket("/api/pty")` in `hermes_cli/web_server.py`) — **do not reimplement the chat surface in React**; extend Ink and it shows up automatically.
- **Plugins (`plugins/`)** — two systems: (1) general lifecycle/tool/CLI plugins via `hermes_cli/plugins.py` and `register(ctx)`; (2) memory-provider plugins under `plugins/memory/<name>/` orchestrated by `agent/memory_manager.py`. **Rule: plugins MUST NOT modify core files** (`run_agent.py`, `cli.py`, `gateway/run.py`, `hermes_cli/main.py`). Expand the generic plugin surface instead.
- **Skills** — `skills/` (built-in, on by default) vs `optional-skills/` (shipped but opt-in via `hermes skills install official/<category>/<skill>`). Slash skills are injected as **user messages**, not system prompts, to preserve prompt caching.
- **Profiles** — `_apply_profile_override()` in `hermes_cli/main.py` sets `HERMES_HOME` before any module imports. Per-profile config/state lives under `~/.hermes/profiles/<name>/`. Profile listing operations stay HOME-anchored, not HERMES_HOME-anchored.

## Slash command registry

All slash commands are defined once in `COMMAND_REGISTRY` (`hermes_cli/commands.py`). CLI dispatch, gateway dispatch, gateway help, Telegram BotCommand menu, Slack subcommand routing, autocomplete, and `/help` categories are all derived from this list. Adding an alias = one tuple entry; adding a command = registry entry + a `process_command()` branch (CLI) and optionally a branch in `gateway/run.py`. `gateway_config_gate` lets a `cli_only` command become available in the gateway when a config dotpath is truthy.

## Critical policies

These are non-obvious and break things if violated. Full rationale in AGENTS.md.

- **Prompt caching must not break.** Do NOT alter past context, change toolsets, reload memory, or rebuild the system prompt mid-conversation. The only sanctioned in-conversation mutation is context compression. Slash commands that change system-prompt state must default to deferred invalidation with an opt-in `--now`. See `/skills install --now`.
- **Profile-safe paths.** Use `get_hermes_home()` (from `hermes_constants`) for state paths and `display_hermes_home()` for user-facing messages. Never `Path.home() / ".hermes"`. Module-level constants are fine — they cache `get_hermes_home()` after the profile override has already run.
- **Tests must not write to `~/.hermes/`.** The `_isolate_hermes_home` autouse fixture in `tests/conftest.py` redirects `HERMES_HOME`. Profile tests must also `monkeypatch.setattr(Path, "home", lambda: tmp_path)` so `_get_profiles_root()` resolves into the temp dir.
- **No `simple_term_menu` for new menus.** Use `hermes_cli/curses_ui.py` (canonical pattern: `hermes_cli/tools_config.py`). Existing call sites are legacy fallback only.
- **No `\033[K` in spinner/display code.** Leaks as literal `?[K` under prompt_toolkit's `patch_stdout`. Use space-padding: `f"\r{line}{' ' * pad}"`.
- **No cross-tool references in tool schema descriptions.** A tool may be unavailable (missing key, disabled toolset) and the model will hallucinate calls. If you need a cross-reference, inject it dynamically in `get_tool_definitions()` in `model_tools.py` (see the `browser_navigate` / `execute_code` post-processing).
- **Don't write change-detector tests.** Don't assert specific catalog entries, version literals, or enumeration counts that update routinely. Assert relationships and invariants instead (see AGENTS.md "Don't write change-detector tests" for examples).
- **Squash merges from stale branches silently revert main.** Before squash-merging a PR, fast-forward the branch over `main` first; verify with `git diff HEAD~1..HEAD` that no unrelated deletions slipped in.

## Config

- User settings live in `~/.hermes/config.yaml`; secrets only in `~/.hermes/.env`. Non-secret settings go in YAML, not `.env`.
- Three config loaders exist — `load_cli_config()` (cli.py), `load_config()` (`hermes_cli/config.py`, used by most subcommands), and direct YAML reads in the gateway (`gateway/run.py` + `gateway/config.py`). If a new key shows up in CLI but not gateway (or vice versa), you are on the wrong loader. `DEFAULT_CONFIG` in `hermes_cli/config.py` is authoritative.
- Bump `_config_version` only for active migrations (renames, structural changes). New keys in existing sections are handled by deep-merge automatically.
- New `.env` vars get an `OPTIONAL_ENV_VARS` entry in `hermes_cli/config.py` with `description`/`prompt`/`url`/`password`/`category`.
- Working directory: CLI uses `os.getcwd()`; messaging uses `terminal.cwd` from YAML (bridged to `TERMINAL_CWD` for child tools). `MESSAGING_CWD` is removed; `TERMINAL_CWD` in `.env` is deprecated — config it via `terminal.cwd`.

## Logs

`~/.hermes/logs/` — `agent.log` (INFO+), `errors.log` (WARNING+), `gateway.log` when the gateway runs. Browse with `hermes logs [--follow] [--level ...] [--session ...]`.
