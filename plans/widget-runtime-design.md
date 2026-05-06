# Hermes Widget Runtime — Design

This document captures the agent-host architecture decisions behind the widget runtime. The wire contract itself lives in [widget-render-spec.md](./widget-render-spec.md) and is not redesigned here.

## Scope

In scope:

- Six new agent-facing tools: `render_widget`, `widget_update`, `widget_message`, `widget_dispose`, `list_widget_examples`, `read_widget_example`.
- Per-session `WidgetRegistry` and `ApiCallRegistry` with idempotent disposal on both sides.
- Async `widget.api_call` accept/correlate/respond pattern with a hard 32 KiB cap on `widget.api_response` enforced server-side before emit.
- `widget.api_cancel` flows in both directions, best-effort cancellation of `prompt.btw`.
- Lean system-prompt addendum (~30 lines, heuristics only) gated on the client advertising `widget.render` in `client.hello`.
- Starter widget examples in `assets/widget_prompts/examples/`, plus `list_widget_examples` / `read_widget_example` tools that read them on demand.
- Sync script that pulls examples from the desktop client's `contracts/examples/` source of truth.
- Small typed-event dispatch path on `tui_gateway/server.py` for inbound client `widget.*` events.

Deferred:

- Streaming render (`widget.render.chunk`). Namespace reserved.
- Widget persistence across `session.resume`. Sessions stay ephemeral.
- Per-call cancellation API on `canvasAPI` (client-side concern; card-level disposal cancellation is in scope).
- Hard-kill timer for runaway `prompt.btw` after cancel. Best-effort interrupt with observability is the chosen behavior.

## Spec-vs-codebase landmarks

The spec assumes some structures that don't exist in the upstream repo. The design accounts for the actual landscape:

- `gateway/system_prompts/` does not exist. Addendum and example assets live at `assets/widget_prompts/` instead. The reading code mirrors `agent/prompt_builder.py:MEMORY_GUIDANCE` and friends.
- `client.hello` is registered in `gateway/platforms/desktop_app.py`. It echoes client capabilities but does not record them server-side; the runtime extends it to bind capabilities into the session for tool gating.
- `tui_gateway/server.py:dispatch()` only routes `method != "event"` requests. Inbound `event`-shape messages from clients fall through and produce a spurious `-32601 unknown method: event`. A small typed-event dispatch map closes that gap; same envelope as outbound events, no `id`.
- `prompt.btw` is registered in `tui_gateway/server.py` and runs on a `threading.Thread` with `max_iterations=8`, emitting `btw.complete`. Today fire-and-forget with no cancel hook. The runtime adds a registry that holds the agent reference so `widget.api_cancel` can call `agent.interrupt()` (interrupt support already exists in `tools/interrupt.py`).
- Tool registration is module-import-time via `registry.register()`. Per-tool availability gating uses `check_fn() -> bool` (no session arg today). The session-context machinery used by `prompt.btw` (`_set_session_context`) is the right substrate for adding a session-scoped flag the widget-tool `check_fn`s can read.

## Design decisions

### System-prompt addendum location

`assets/widget_prompts/addendum.md` — a single markdown fragment loaded once at process import into a module-level constant `WIDGET_AUTHOR_GUIDANCE` (mirrors `MEMORY_GUIDANCE` in `agent/prompt_builder.py`). `_build_system_prompt` appends it iff `"render_widget" in self.valid_tool_names`.

Reasons:

- The lean addendum (~30 lines of heuristics) lives in the system prompt only when the widget tools are registered, which is gated on capability negotiation. Sessions without a canvas client see neither the tools nor the addendum and pay zero context cost.
- Reading once at import keeps disk I/O off the prompt-build path; conditional append keeps the system prompt cache stable.
- Size matches existing per-tool guidance blocks (`MEMORY_GUIDANCE` ~18 lines, `SKILLS_GUIDANCE` ~6 lines, `TOOL_USE_ENFORCEMENT_GUIDANCE` ~50 lines). This is best-practice for the codebase, not a new pattern.
- Markdown source is independently iterable as a diff so prompt tuning doesn't tangle with code review.

The heavy content (primitives types, the starter `.tsx` files) is **never** in the system prompt — pulled in only when the agent calls `read_widget_example(name)`.

### Example-file sync

`scripts/sync_widget_examples.py` — manual-run script that copies `.tsx` files from the desktop client's `contracts/examples/` into `assets/widget_prompts/examples/`. The client-side contracts pipeline is the source of truth; this side mirrors.

Reasons:

- Examples are small text files. A script makes the sync explicit and greppable; `git diff` after sync surfaces drift.
- Submodules add ceremony (`--recurse-submodules`, separate clone discipline) for ~6 small files. Not worth the overhead.
- Manual copy without a script is fine but rots quietly. A script is the smallest enforceable improvement.
- The client-repo source path is parameterized so the script works whether the two repos are siblings, in a fixed layout, or mounted at a non-default path.

### Approval gating

No new approval surface. `notes.save` is client-side and never reaches Hermes. `hermes.ask` runs as a `prompt.btw`, which is the established unapproved side-channel pattern — adding per-call approval here would be inconsistent with how btw works elsewhere.

The user-visible gate is the `capabilities` array in `widget.render`: the agent declares upfront what the card *can* do, and the user sees that manifest when the card mounts. The 32 KiB response cap bounds the worst case for `hermes.ask` output.

If approval becomes a need (e.g. cards starting to abuse `hermes.ask`), it can be added without re-architecting.

### Client-emitted event dispatch

Extend `tui_gateway/server.py:dispatch()` with a typed-event dispatch path: if `req["method"] == "event"`, look up `params["type"]` in a `_event_handlers: dict[str, Callable]` map and invoke it. No `id` to respond to; handlers return `None`.

Same envelope as outbound events. `widget.mounted`, `widget.error`, `widget.disposed`, `widget.api_cancel` register handlers that mutate per-session registry state and resolve any pending tool-side futures.

### Tool-bundle visibility

All six widget tools register together as a bundle, gated by the same capability check_fn. No reveal-after-render machinery.

Reasons:

- The Hermes registry is stable-per-session by design. Mutating it mid-session breaks the prompt cache.
- The schema cost of two extra tools is small — roughly 60 tokens per turn — compared to the addendum (~300 tokens). Not worth introducing reveal infrastructure to save it.

### Cancellation hard-kill

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
  ├─ _event_handlers                   inbound widget.* event routing
  └─ existing dispatch + prompt.btw

gateway/platforms/desktop_app.py
  └─ client.hello extended
       ├─ adds "widget.render" to server-advertised caps
       └─ records client capabilities into the session for tool gating
```

### Capability gating

1. The desktop client connects and sends `client.hello` with `widget.render` in its capabilities.
2. The handler in `desktop_app.py` records `widget.render` into the connection-bound session-creation context (the WS-connection scope object that `session.create` reads at agent construction).
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
Client broker --widget.api_call(correlation_id, card_id, capability, args)--> server
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

`widget.mounted`, `widget.error`, `widget.disposed`, `widget.api_cancel` register their handlers in `tui_gateway/widget_runtime.py` at import time, looking up the per-session registry by `params["session_id"]`.

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

- Takes a path to the desktop client's repo (CLI flag, env var, or default sibling layout).
- Copies `contracts/examples/*.tsx` into `assets/widget_prompts/examples/`.
- Idempotent: re-running with no upstream changes is a no-op (verifiable via `git diff`).
- Reports drift summary on completion.

## Cross-machine alignment tests

These domain tests are required to keep the two sides in sync:

- **Card ID format.** Server allocator produces ids matching `/^wgt_[0-9a-f]{6}$/`.
- **`widget.api_response` 32 KiB cap.** Server-side enforcement before emit, error code 4106 carrying actual size + cap.
- **`widget.api_cancel` envelope.** Client → server message carries exactly `{correlation_id, card_id, reason}`.
- **`client.hello` capability bundle.** All six widget tools register together iff `widget.render` is advertised; none register otherwise.
- **Outbound client events accepted.** Gateway dispatcher accepts `widget.mounted`, `widget.error`, `widget.disposed`, `widget.api_cancel` as event-shape messages with no `id` and routes them.
- **Error code table 4101–4107 + 5101–5103.** Matches the client-side codebook exactly.
