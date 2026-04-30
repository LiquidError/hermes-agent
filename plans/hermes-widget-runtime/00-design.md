# Hermes Widget Runtime — Design

> Output of the brainstorming pass over `plans/hermes-widget-render-spec.md` and `plans/widget-runtime/hermes-handoff.md`. This document closes the §11 open considerations from the source spec and locks the architecture before per-plan implementation. The five implementation plans (`01-...md` through `05-...md`) descend from this design.

## Source material

- `plans/hermes-widget-render-spec.md` — the Hermes-side spec; wire contract in §3 is shared verbatim with the Tauri side and not redesigned here.
- `plans/widget-runtime/hermes-handoff.md` — written by the prior session that produced the Tauri-side plans (`plans/widget-runtime/00-index.md` and `01-...md` through `04-...md`). Surfaces the open considerations and cross-machine alignment tests this design must honor.
- `plans/widget-runtime/01-wire-contract-foundation.md` — Tauri side has implemented the wire types, `hermes_emit_event` Rust command, mockHermes parity, and stub widget card. The Hermes side has no critical-path dependency on Tauri Plans 02–04; both can develop in parallel after Tauri Plan 01.

## Scope

In scope:
- Six new agent-facing tools: `render_widget`, `widget_update`, `widget_message`, `widget_dispose`, `list_widget_examples`, `read_widget_example`.
- Per-session `WidgetRegistry` and `ApiCallRegistry` with idempotent disposal both sides.
- Async `widget.api_call` accept/correlate/respond pattern with hard 32 KiB cap on `widget.api_response` enforced server-side before emit.
- `widget.api_cancel` flows in both directions, best-effort cancellation of `prompt.btw`.
- Lean system-prompt addendum (~30 lines, heuristics only) gated on the client advertising `widget.render` in `client.hello`.
- Four starter widget examples in `assets/widget_prompts/examples/`, plus `list_widget_examples` / `read_widget_example` tools that read them on demand.
- Sync script that pulls examples from the Tauri-side `contracts/examples/` source of truth.
- Small typed-event dispatch path on `tui_gateway/server.py` for inbound client `widget.*` events.

Out of scope (explicitly carried as deferred):
- Streaming render (`widget.render.chunk`). Namespace reserved.
- Widget persistence across `session.resume`. Sessions stay ephemeral.
- Per-call cancellation API on `canvasAPI` (Tauri-side concern; card-level disposal cancellation is in scope).
- Heavy primitives bundling decisions (Tauri-side; Hermes only reads example `.tsx` files).
- Hard-kill timer for runaway `prompt.btw` after cancel. Best-effort interrupt with observability is the chosen behavior.

## Spec-vs-codebase landmarks

The source spec assumes some structures that don't exist in this repo. The design accounts for the actual landscape:

- `gateway/system_prompts/` does not exist. Addendum and example assets live at `assets/widget_prompts/` instead. The reading code mirrors `agent/prompt_builder.py:MEMORY_GUIDANCE` and friends.
- `client.hello` is registered in `gateway/platforms/desktop_app.py`. It currently echoes client capabilities but does not record them server-side. Will be extended to bind capabilities into the session for tool gating.
- `tui_gateway/server.py:dispatch()` only routes `method != "event"` requests. Inbound `event`-shape messages from clients fall through and produce a spurious `-32601 unknown method: event`. A small typed-event dispatch map closes that gap; same envelope as outbound events, no `id`.
- `prompt.btw` is registered in `tui_gateway/server.py` and runs on a `threading.Thread` with `max_iterations=8`, emitting `btw.complete`. Today fire-and-forget with no cancel hook. The runtime adds a registry that holds the agent reference so `widget.api_cancel` can call `agent.interrupt()` (interrupt support already exists in `tools/interrupt.py`).
- Tool registration is module-import-time via `registry.register()`. Per-tool availability gating uses `check_fn() -> bool` (no session arg today). The session-context machinery used by `prompt.btw` (`_set_session_context`) is the right substrate for adding a session-scoped flag the widget-tool `check_fn`s can read.

## Decisions resolved during brainstorming

These close the source spec's §11 open considerations.

### System-prompt addendum location (closes §11.1)

`assets/widget_prompts/addendum.md` — a single markdown fragment loaded once at process import into a module-level constant `WIDGET_AUTHOR_GUIDANCE` (mirrors `MEMORY_GUIDANCE` in `agent/prompt_builder.py`). `_build_system_prompt` appends it iff `"render_widget" in self.valid_tool_names`.

Reasons:
- The lean addendum (~30 lines of heuristics) lives in the system prompt only when the widget tools are registered, which is gated on capability negotiation. Sessions without the Tauri client see neither the tools nor the addendum and pay zero context cost.
- Reading once at import keeps disk I/O off the prompt-build path; conditional append keeps the system prompt cache stable.
- Size matches existing per-tool guidance blocks (`MEMORY_GUIDANCE` ~18 lines, `SKILLS_GUIDANCE` ~6 lines, `TOOL_USE_ENFORCEMENT_GUIDANCE` ~50 lines). This is best-practice for the codebase, not a new pattern.
- Markdown source is independently iterable as a diff so prompt tuning doesn't tangle with code review.

The heavy content (primitives types, the 4 starter `.tsx` files) is **never** in the system prompt — pulled in only when the agent calls `read_widget_example(name)`.

### Example-file sync (closes §11.2)

`scripts/sync_widget_examples.py` — manual-run script that copies `.tsx` files from the Tauri repo's `contracts/examples/` into `assets/widget_prompts/examples/`. The Tauri-side contracts pipeline is the source of truth; this side mirrors.

Reasons:
- Examples are small text files. A script makes the sync explicit and greppable; `git diff` after sync surfaces drift.
- Submodules add ceremony (`--recurse-submodules`, separate clone discipline) for ~6 small files. Not worth the overhead.
- Manual copy without a script is fine but rots quietly. A script is the smallest enforceable improvement.
- The Tauri-repo source path is parameterized so the script works whether the two repos are siblings, in a fixed layout, or mounted at a non-default path.

### Approval gating (closes §11.3)

No new approval surface. `notes.save` is Tauri-side and never reaches Hermes. `hermes.ask` runs as a `prompt.btw`, which is the established unapproved side-channel pattern in Hermes — adding per-call approval here would be inconsistent with how btw works elsewhere.

The user-visible gate is the `capabilities` array in `widget.render`: the agent declares upfront what the card *can* do, and the user sees that manifest when the card mounts. The 32 KiB response cap bounds the worst case for `hermes.ask` output.

Carried forward as an explicit deferred decision, not silently chosen — if approval becomes a need (e.g. cards starting to abuse `hermes.ask`), it can be added without re-architecting.

### Client-emitted event dispatch (closes §11.5)

Extend `tui_gateway/server.py:dispatch()` with a typed-event dispatch path: if `req["method"] == "event"`, look up `params["type"]` in a new `_event_handlers: dict[str, Callable]` map and invoke it. No `id` to respond to; handlers return `None`.

Same envelope as outbound events (the Tauri side has already committed to this shape). `widget.mounted`, `widget.error`, `widget.disposed`, `widget.api_cancel` register handlers that mutate per-session registry state and resolve any pending tool-side futures.

This dispatch path lands in plan 02 alongside the registry that consumes its events, not as a standalone trailing plan, because plan 02 cannot demo `render_widget` without inbound `widget.mounted` to resolve on.

### Example-tool visibility (closes §11.7)

All six widget tools register together as a bundle, gated by the same capability check_fn. No reveal-after-render machinery.

Reasons:
- The Hermes registry is stable-per-session by design. Mutating it mid-session breaks the prompt cache (CLAUDE.md "Prompt caching must not break").
- The schema cost of two extra tools is small — roughly 60 tokens per turn — compared to the addendum (~300 tokens). Not worth introducing reveal infrastructure to save it.

### Cancellation hard-kill (closes §11.8)

Best-effort `agent.interrupt()` on cancel; no hard-kill timer. The `ApiCallRegistry` records cancellation timestamps and post-cancel runtime per correlation, readable via debug logs. If production data shows runaway `prompt.btw` after cancel is a real problem, that's a follow-up; the observability lands now so the data exists.

## Architecture

```
agent (run_agent.AIAgent)
  │
  │  tool calls → registry.dispatch()
  │
  ▼
tools/widget_tools.py             ← six new tools, register at module import
  │  reads session ctx →
  │  pushes widget.* events       → tui_gateway._emit(...)
  │  reads from registries
  │
  ▼
tui_gateway/server.py
  ├─ session["widget_registry"]        per-session WidgetRegistry
  ├─ session["api_call_registry"]      per-session ApiCallRegistry
  ├─ _event_handlers (NEW)             inbound widget.* event routing
  └─ existing dispatch + prompt.btw

gateway/platforms/desktop_app.py
  └─ client.hello extended
       ├─ adds "widget.render" to server-advertised caps
       └─ records client capabilities into the session for tool gating
```

### Capability gating

1. Tauri client connects, sends `client.hello` with `widget.render` in its capabilities.
2. The handler in `desktop_app.py` records `widget.render` into the connection-bound session-creation context (plan 01 picks the exact attribute — likely on the WS-connection scope object that `session.create` reads at agent construction).
3. When `AIAgent` is constructed for a session, the session-context binding includes a `widget_render_available: bool` flag.
4. The widget-tool `check_fn`s read that flag and return `True` only when set. Tools register at module import (matches every other Hermes tool); availability is per-session via the existing context machinery.
5. The system-prompt build sees `render_widget` in `valid_tool_names` and appends `WIDGET_AUTHOR_GUIDANCE`.

This avoids any registry mutation per connection, keeps the cached system prompt stable, and matches how `MEMORY_GUIDANCE` already conditions on tool presence.

### `WidgetRegistry` (per session)

```python
class WidgetRegistry:
    cards: dict[str, CardEntry]  # card_id -> entry
    def allocate(self, source, capabilities, title, initial_size, trace_id) -> str: ...
    def get(self, card_id) -> CardEntry | None: ...
    def mark_mounted(self, card_id, compiled_size, compile_ms): ...
    def mark_error(self, card_id, phase, kind, message, stack): ...
    def dispose(self, card_id, reason) -> tuple[disposed: bool, already_disposed: bool]: ...
```

Card IDs allocated server-side as `wgt_<6 hex>` per the cross-machine validator both sides share. Registry stores per-card mount-resolution future for `render_widget` to await on. Disposal is idempotent: `dispose` on an unknown or already-disposed card returns `(False, True)`.

### `ApiCallRegistry` (per session)

```python
class ApiCallRegistry:
    inflight: dict[str, CallEntry]  # correlation_id -> entry
    def register(self, correlation_id, card_id, capability, agent_ref) -> None: ...
    def complete(self, correlation_id, result_or_error) -> CallEntry | None: ...
    def cancel(self, correlation_id, reason) -> CallEntry | None: ...
    def cancel_for_card(self, card_id, reason) -> list[str]: ...
```

Each `CallEntry` holds the `agent_ref` so cancel can call `agent.interrupt()`. Records `created_at`, `cancelled_at`, `completed_at` for observability — post-cancel runtime is `completed_at - cancelled_at` if a cancelled call still produces a (dropped) result.

### `widget.api_call` flow

```
Tauri broker --widget.api_call(correlation_id, card_id, capability, args)--> server
   server validates:
     - card live in this session            → else error 4103
     - capability declared in card manifest  → else error 4104
   server responds {accepted: true, correlation_id}
   server registers correlation in ApiCallRegistry
   server spawns prompt.btw thread on worker pool
prompt.btw completes:
   server measures len(json.dumps(result))
   if > 32 KiB: emit widget.api_response with error 4106 (actual size + cap)
   else:        emit widget.api_response with result
   server removes correlation from registry
```

Cancel paths:

```
client --widget.api_cancel(correlation_id, card_id, reason)--> server
   server: lookup correlation
     if found: agent_ref.interrupt(); record cancelled_at; remove
     if not found (already completed): no-op
server-side card disposal:
   for each correlation associated with card_id:
     emit widget.api_cancel to client (informational)
     interrupt + record + remove
```

### Inbound event dispatch

```python
# tui_gateway/server.py
_event_handlers: dict[str, Callable[[dict], None]] = {}

def event_handler(name: str):
    def dec(fn):
        _event_handlers[name] = fn
        return fn
    return dec

# in dispatch():
if req.get("method") == "event":
    params = req.get("params") or {}
    event_type = params.get("type", "")
    handler = _event_handlers.get(event_type)
    if handler:
        handler(params)
    return None  # no response — events have no id
```

`widget.mounted`, `widget.error`, `widget.disposed`, `widget.api_cancel` register their handlers in `tools/widget_tools.py` (or a new `tui_gateway/widget_runtime.py`) at import time, looking up the per-session registry by `params["session_id"]`.

### System-prompt addendum

```python
# agent/prompt_builder.py — alongside MEMORY_GUIDANCE et al.
WIDGET_AUTHOR_GUIDANCE = (Path(__file__).parent.parent / "assets/widget_prompts/addendum.md").read_text(encoding="utf-8")
```

In `run_agent.py:_build_system_prompt`:

```python
if "render_widget" in self.valid_tool_names:
    prompt_parts.append(WIDGET_AUTHOR_GUIDANCE)
```

The addendum content covers: when to render (bounded artifact, structured data, "see/try/adjust" cues); when not to (short factual answers, prose narrative); the call-`list_widget_examples`-first heuristic; lifecycle do's and don'ts (prefer `widget_update` over re-render; dispose explicitly; treat `card_gone: True` as user signal not error); the import surface (React + hooks, `canvasAPI`, `canvas-primitives`, no fetch/CDN).

### Examples and sync script

`assets/widget_prompts/examples/`:
- `static-info.tsx` — purely presentational card with primitives.
- `form-with-hermes-ask.tsx` — form that calls `canvasAPI.hermes.ask` and updates fields with the answer.
- `list-with-storage.tsx` — list with per-card persisted state via `canvasAPI.storage`.
- `chart.tsx` — `<Chart>` primitive over agent-provided data.

Each starts with a JSDoc summary line read by `list_widget_examples` (returns `[{name, summary}, ...]`).

`scripts/sync_widget_examples.py`:
- Takes a path to the Tauri repo (CLI flag, env var, or default `~/projects/anandia-workspace`).
- Copies `contracts/examples/*.tsx` into `assets/widget_prompts/examples/`.
- Idempotent: re-running with no upstream changes is a no-op (verifiable via `git diff`).
- Reports drift summary on completion.

## Cross-machine alignment tests

These domain tests are required by the handoff and live in plans 02–05 as appropriate:

- **Card ID format.** Server allocator produces ids matching `/^wgt_[0-9a-f]{6}$/`. Plan 02.
- **`widget.api_response` 32 KiB cap.** Server-side enforcement before emit, error code 4106 carrying actual size + cap. Plan 03.
- **`widget.api_cancel` envelope.** Client → server message carries exactly `{correlation_id, card_id, reason}`. Plan 04.
- **`client.hello` capability bundle.** All six widget tools register together iff `widget.render` is advertised; none register otherwise. Plan 01.
- **Outbound client events accepted.** Gateway dispatcher accepts `widget.mounted`, `widget.error`, `widget.disposed`, `widget.api_cancel` as event-shape messages with no `id` and routes them. Plan 02.
- **Error code table 4101–4107 + 5101–5103.** Matches the Tauri-side codebook exactly. Spread across plans 02–04 as each error becomes reachable.

## Plan split

| # | Title | Scope summary | Demoable end-state |
|---|---|---|---|
| 01 | Capability negotiation, tool scaffolding, system-prompt addendum | Extend `client.hello` to record client caps into the session; advertise `widget.render` server-side; create the six widget-tool stubs in `tools/widget_tools.py` (register normally; `check_fn` checks the session-context flag); wire `WIDGET_AUTHOR_GUIDANCE` into `_build_system_prompt`; ship the addendum file. Tools return "not implemented yet" placeholders. | Agent sees the six widget tools and the addendum only when connected via a Tauri client advertising `widget.render`. |
| 02 | WidgetRegistry, render/update/message/dispose lifecycle, inbound event dispatch | Add `_event_handlers` map and dispatch extension. Implement `WidgetRegistry`. Implement `render_widget` (emits `widget.render`, blocks on `widget.mounted`/`widget.error`, returns `card_id` string). Implement `widget_update`, `widget_message`, `widget_dispose` with structured-dict returns. Idempotent disposal both sides. Inbound `widget.mounted`/`error`/`disposed` handlers. | Agent renders and disposes a widget end-to-end against the Tauri Plan-04 client. |
| 03 | ApiCallRegistry + async `widget.api_call`/`widget.api_response` with 32 KiB cap | `ApiCallRegistry` keyed on `(session_id, correlation_id)` storing the agent reference. `widget.api_call` handler validates card-live + capability-declared, acks synchronously, spawns `prompt.btw`, measures serialized result, emits `widget.api_response` (success or 4106). Add error codes 4101–4107. | Card calls `canvasAPI.hermes.ask` and the response lands back in the iframe; oversized responses produce a clean 4106 rejection. |
| 04 | `widget.api_cancel` both directions | Inbound `widget.api_cancel` handler: lookup correlation, call `agent.interrupt()`, remove from registry, drop any in-flight response. Outbound `widget.api_cancel` emission on session disconnect, `session.resume`, and card disposal mid-flight. Lite observability: cancellation timestamps + post-cancel duration recorded in registry. | Closing a card mid-`hermes.ask` cleanly cancels the underlying btw without phantom `widget.api_response` arrival. |
| 05 | Example tools, starter examples, sync script | Implement `list_widget_examples` and `read_widget_example` reading from `assets/widget_prompts/examples/`. Author 4 starter `.tsx` files. Add `scripts/sync_widget_examples.py` with idempotent copy + drift report. | Agent calls `list_widget_examples()` then `read_widget_example("form-with-hermes-ask")` before authoring its own widget. |

Critical path: 01 → 02 is the demoable milestone. 03 unlocks `hermes.ask`. 04 closes the cancel path. 05 makes widget authoring effective.

Each plan is independently testable. Plans 02–05 each carry one cross-machine alignment test from the handoff.

## Acceptance summary

The work is done when:

1. `tui_gateway/server.py` registers all `widget.*` events and methods per spec §3, including the async `widget.api_call`/`widget.api_response` pattern, `widget.api_cancel`, and the inbound event dispatch path.
2. `gateway/platforms/desktop_app.py` advertises `widget.render` and records client-advertised capabilities into the session.
3. The six widget tools are registered conditionally on the session having `widget.render` available. Tool return types match spec §5.1 (informative dicts where applicable).
4. `WidgetRegistry` and `ApiCallRegistry` are per-session, wired to disconnect/resume cleanup. Disposal is idempotent both sides per spec §3.4. Card disposal triggers cancellation of associated correlations.
5. `widget.api_response` payload size is enforced server-side before emit with hard 32 KiB cap and error code 4106.
6. The `WIDGET_AUTHOR_GUIDANCE` system-prompt addendum is appended only when `render_widget` is in `valid_tool_names`.
7. Four starter examples exist in `assets/widget_prompts/examples/`, discoverable via `list_widget_examples()` and readable via `read_widget_example(name)`. `scripts/sync_widget_examples.py` exists and copies idempotently.
8. All test scenarios from spec §10 pass against a mock client.
9. The base wire contract (`plans/tauri-client-contract.md`) gets a "Widget render" section appended that mirrors spec §3, with the canonical version living in the contract doc going forward.
