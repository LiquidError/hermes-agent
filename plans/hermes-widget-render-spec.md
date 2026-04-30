# Hermes Widget Render — Spec for Claude Code (Mac mini side) — v2

> **For agentic workers:** this is a *spec*, not a plan. Hand it to the planning skill and run domain discovery before producing tasks. The wire-shape is committed; the Hermes-side architecture is open for the planner to refine.

**v2 changelog (vs v1).** Five gaps surfaced by Hermes during a round of self-review, plus four refinements layered on top:

- **Gap 1 (resolved):** `WidgetHandle` removed. `render_widget` returns a plain `card_id` string; companion stateless tools `widget_update`, `widget_message`, `widget_dispose` interact with live cards. Survives serialization across agent turns. (§5.1, §5.2)
- **Gap 2 (resolved):** `widget.api_call` now uses an async accept/correlate/respond pattern instead of a synchronous JSON-RPC request, so long `hermes.ask` calls don't time out or look like they're blocking the WebSocket. (§3.5)
- **Gap 3 (resolved):** Disposal is idempotent on both sides (server *and* client), with explicit race handling. (§3.4, §10.8)
- **Gap 4 (resolved with refinement):** Response size for `hermes.ask` is now a **hard cap of 32 KiB at the wire layer** with error code `4106`. v1 prefer-pattern guidance (use `widget.message` for large structured data) is documented but not load-bearing — the cap enforces it. (§4.1, §8)
- **Gap 5 (resolved):** Examples and primitives docs are fetched on demand via `list_widget_examples()` and `read_widget_example(name)` tools. The system prompt addendum stays lean. (§5.3, §5.4)
- **Refinement A:** Cancellation. New event `widget.api_cancel` (server → client, can also originate client → server) cancels a pending `widget.api_call` correlation. Triggered automatically on card disposal mid-call. (§3.5.4, §10.11)
- **Refinement B:** `widget_dispose` returns a structured dict (`{ disposed, already_disposed }`) instead of `None`, giving the agent useful signal about race outcomes. (§5.1)
- **Refinement C:** §11.3 (handle survival) and §11.6 (where types ship) are closed by Gaps 1 and 5; moved to a new §15 "Resolved decisions" section so the rationale is preserved.
- **Refinement D:** §10 gains a new test (10.12) for the response-size cap and an updated 10.11 for cancellation.

---

**Audience:** Claude Code running on the Mac mini, with read-write access to the Hermes Agent source (`gateway/`, `tui_gateway/`, `hermes_cli/`).

**Goal:** Extend Hermes so the agent can render arbitrary React/JSX cards into the Tauri canvas. The agent decides *what* the card should look like (writes JSX); the Tauri host renders it inside a sandboxed iframe with a brokered capability surface. This spec covers the Mac-mini half: a new `widget.*` RPC namespace on `tui_gateway`, a `render_widget` tool the agent can invoke, lifecycle events flowing back, and the contract for `canvasAPI` calls that need round-trips to the agent.

**Out of scope:** the Tauri-side iframe sandbox, esbuild-wasm pipeline, capability broker, and iframe pool. Those live in the companion Tauri-side spec. This document defines only what the two sides agree on at the wire.

**Reference docs:**
- [tauri-client-contract.md](./tauri-client-contract.md) — base wire protocol this spec extends
- [desktop-app-adaptor.md](./desktop-app-adaptor.md) — server-side `DesktopAppAdapter` architecture
- [agent-canvas-design.md](./agent-canvas-design.md) — Tauri-side canvas where rendered cards live
- [agent-canvas-implementation-plan.md](./agent-canvas-implementation-plan.md) — existing card types this slots alongside
- [tauri-agent-widget-runtime-spec.md](./tauri-agent-widget-runtime-spec.md) — the Tauri-side companion spec

---

## 1. Domain model

Three nouns, kept distinct on purpose:

**Source.** A string of JSX/JavaScript the agent generates. It is text, nothing more — the agent never executes it. It exports a single default React component. The agent is the *author*.

**Card.** A long-lived rendered instance of source on the Tauri canvas. A card has an id, a position, a size, a state, and a capability allowlist. Multiple cards can exist concurrently; the same source can be rendered as multiple cards. The Tauri host is the *renderer*.

**Capability.** A named permission the card needs to call back to the world — `notes.save`, `hermes.ask`, `storage.set`, `os.notify`. The agent declares them up front when it emits source. The Tauri broker is the *gatekeeper*.

> **Mental model.** Hermes is a writer who hands typed pages to a printer. The printer assembles the pages into a book, locks the book in a glass display case, and gives the reader a numbered list of buttons that *can* reach the writer. The reader can press a button to ask the writer something; the writer can never reach inside the case to change the book directly — only print a new one and have the printer swap the display.

**Why this split matters.** Each noun has a different lifetime, owner, and threat profile. Source is cheap and ephemeral (regenerate any time). Cards are durable and stateful (user expects them to stay open). Capabilities are the trust surface (every entry point reviewed). Conflating them produces the kind of "the agent has full DOM access" mistake we want to avoid.

---

## 2. Architecture summary

```
Hermes agent (Mac mini, Python)
   │
   │  agent invokes `render_widget` tool with (source, capabilities)
   │  ↓
   │  tui_gateway emits `widget.render` event over the active session
   │
   ▼
DesktopAppAdapter ──── WSS (existing tui_gateway transport) ────▶ Tauri host
                                                                     │
                                                                     │  spawns sandboxed iframe,
                                                                     │  brokers postMessage capability calls,
                                                                     │  emits lifecycle events back
                                                                     ▼
                       Hermes ◀──── widget.* events / api_call requests ────
```

The Mac-mini side adds:
- a new RPC topic (`widget.*`) registered on `tui_gateway/server.py`
- six new agent-facing tools (`render_widget`, `widget_update`, `widget_message`, `widget_dispose`, `list_widget_examples`, `read_widget_example`)
- a per-session `WidgetRegistry` tracking live cards by id with idempotent disposal semantics
- a per-session `ApiCallRegistry` tracking in-flight `widget.api_call` correlations
- handlers for `widget.api_call` requests from cards calling `canvasAPI.hermes.*`, with async response delivery via `widget.api_response`
- handlers for `widget.api_cancel` events to abort pending calls when cards are disposed mid-flight

Nothing else in Hermes changes. The persisted `state.db` schema is unaffected; widgets are session-scoped and not persisted across `session.resume`. (See §11.4 for why, and §12 for the future-extension path if persistence is wanted later.)

---

## 3. Wire contract additions

This section extends `tauri-client-contract.md` §4. All additions are additive; `protocol_version` does not bump (per §12 of the base contract).

### 3.1 New event: `widget.render` (server → client)

Emitted when the agent invokes `render_widget`. The agent has decided to put a custom card on the canvas.

```json
{
  "jsonrpc": "2.0",
  "method": "event",
  "params": {
    "type": "widget.render",
    "session_id": "ab12cd34",
    "payload": {
      "card_id": "wgt_8a3f9c",
      "source": "export default function Card() { return <div>...</div> }",
      "capabilities": ["hermes.ask", "notes.save", "storage.set", "storage.get"],
      "title": "Quarterly notes draft",
      "initial_size": { "w": 480, "h": 320 },
      "trace_id": "tool_call_42"
    }
  }
}
```

Field semantics:

| Field | Required | Meaning |
|---|---|---|
| `card_id` | yes | Server-generated unique id. Format: `wgt_` + 6 lowercase hex chars. Stable for the card's lifetime. |
| `source` | yes | Full JSX source string. Must export a default component. UTF-8. Max 256 KiB. |
| `capabilities` | yes | Subset of the v1 capability surface (§4). Empty array means the card is purely presentational. Unknown capabilities cause the client to refuse mount and emit `widget.error`. |
| `title` | optional | Human-readable card title. Used for window-chrome / threads panel. Default: "Untitled card". |
| `initial_size` | optional | Suggested initial dimensions in canvas units. Client may clamp. Default: 400×280. |
| `trace_id` | optional | The originating tool-call id. Lets the client correlate this card with its tool-progress entry. |

The card is mounted *eagerly* — the user does not approve its appearance, the same way they don't approve a `<ToolProgressCard>`. (Capability calls *can* be approved per-call; see §4.) If the user has set a global "always confirm new agent cards" preference, that is a Tauri-side concern, not a wire-contract concern.

### 3.2 New event: `widget.update` (server → client)

Replace the source of an existing card. Same `card_id` survives; React state is reset (the iframe re-mounts). Used when the agent has improved its design or fixed an error.

```json
{
  "jsonrpc": "2.0",
  "method": "event",
  "params": {
    "type": "widget.update",
    "session_id": "ab12cd34",
    "payload": {
      "card_id": "wgt_8a3f9c",
      "source": "...",
      "capabilities": ["hermes.ask", "notes.save"]
    }
  }
}
```

If `card_id` does not refer to a live card, the client responds with a `widget.error` event of type `not_found` and the agent should treat the card as gone. The agent MAY emit a fresh `widget.render` if it wants the card back. Server-side: `widget_update(card_id=...)` for an unknown id raises with error code `4103` *and* the tool result tells the agent the card is gone (so it can decide whether to re-render).

### 3.3 New event: `widget.message` (server → client)

Push a structured message *into* a mounted card. The card's React tree receives it via `canvasAPI.onMessage(handler)`. Used when the agent has asynchronously computed something and wants to update the card without re-rendering source.

```json
{
  "type": "widget.message",
  "session_id": "ab12cd34",
  "payload": {
    "card_id": "wgt_8a3f9c",
    "message": { "kind": "data.refresh", "rows": [...] }
  }
}
```

The `message` shape is opaque to Hermes and the Tauri host — both sides treat it as JSON the card understands. It's the agent's job to keep the shape consistent with what the card it wrote knows how to handle. Max payload size: 256 KiB (same limit as `source`, for the same reason — large data should be paginated, not crammed into a single message).

### 3.4 New event: `widget.dispose` (server → client)

Agent wants to close the card. The Tauri host unmounts the iframe and emits `widget.disposed` back as confirmation.

```json
{
  "type": "widget.dispose",
  "session_id": "ab12cd34",
  "payload": { "card_id": "wgt_8a3f9c", "reason": "task_complete" }
}
```

`reason` is a free-form short string for observability. Suggested values: `task_complete`, `superseded`, `error`, `agent_initiated`.

**Idempotent disposal — both sides.** Both the server (via `widget.dispose`) and the client (user closing a card) can trigger disposal of the same card simultaneously. The race must not produce errors:

- **Server side:** the handler for `widget.dispose` is idempotent. If the card is already disposed or in the process of being disposed (the registry has been cleared because `widget.disposed` arrived from the client), the dispose is silently treated as already-done. The agent's `widget_dispose` tool returns `{ disposed: false, already_disposed: true }`.
- **Client side:** if the client has already torn down the card (because the user clicked X), and a `widget.dispose` event arrives from the server for the same `card_id`, the client silently no-ops (does NOT re-emit `widget.disposed`, since it has already done so or is about to).
- **Both-in-flight:** if `widget.disposed` (client → server) and `widget.dispose` (server → client) cross on the wire, both sides defensively treat their own action as the canonical disposal, drop the incoming, and converge on the same end state: card gone, registry cleared.

Any pending `widget.api_call` correlations for the disposed card are cancelled per §3.5.4.

### 3.5 New method: `widget.api_call` (client → server) — async pattern

**WHY async.** `hermes.ask` can take seconds or minutes (research, code gen, multi-step reasoning). A synchronous JSON-RPC request would either block the WebSocket awareness layer or hit standard request timeouts (~30s) and fail spuriously even though the agent is still working. The async pattern below ensures the call survives long latencies and the connection stays clearly responsive.

#### 3.5.1 Request

When a card calls `canvasAPI.hermes.ask(...)` (or any other capability that requires server round-trip; v1 is just the `hermes.*` family), the Tauri broker sends this message:

```json
{
  "jsonrpc": "2.0",
  "id": 17,
  "method": "widget.api_call",
  "params": {
    "card_id": "wgt_8a3f9c",
    "session_id": "ab12cd34",
    "correlation_id": "corr_a1b2c3",
    "capability": "hermes.ask",
    "args": { "prompt": "What was Q3 revenue?" }
  }
}
```

`correlation_id` is a client-generated unique id for this specific call. Used to correlate the eventual response back to the iframe's Promise. Format: `corr_` + 6 lowercase hex chars. The server registers this in its `ApiCallRegistry` keyed by `(session_id, correlation_id)`.

#### 3.5.2 Acknowledgment

The server responds immediately — not with the result, but with an acknowledgment:

```json
{
  "jsonrpc": "2.0",
  "id": 17,
  "result": {
    "accepted": true,
    "correlation_id": "corr_a1b2c3"
  }
}
```

The server validates synchronously before acking:
- The card is live in the named session.
- The capability was declared in the card's manifest.
- The session belongs to this connection (per §10 of the base contract).

If validation fails, the acknowledgment response carries an error instead (standard error codes; see §8). The card's Promise rejects on this error and no further `widget.api_response` is emitted.

#### 3.5.3 Response delivery: `widget.api_response` (server → client)

When the capability result is ready, the server emits this event:

```json
{
  "jsonrpc": "2.0",
  "method": "event",
  "params": {
    "type": "widget.api_response",
    "session_id": "ab12cd34",
    "payload": {
      "correlation_id": "corr_a1b2c3",
      "card_id": "wgt_8a3f9c",
      "result": { "answer": "Q3 revenue was $4.2M, up 18% YoY." }
    }
  }
}
```

On error: the payload includes `{ "correlation_id": "...", "card_id": "...", "error": { "code": ..., "message": "..." } }` instead of `result`. The Tauri broker resolves or rejects the iframe's pending Promise accordingly, then removes the correlation from its in-flight map.

**Response size cap.** The total payload of `widget.api_response` (specifically the serialized `result` field) is hard-capped at 32 KiB. If a capability's natural result exceeds this (e.g., a `hermes.ask` answer of 50 KiB markdown), the server MUST emit an error response with code `4106` instead of the oversized result. The agent should split the answer (use `widget.message` for paginated chunks) or pre-process before returning. See §4.1 for the per-capability rationale.

**Response size means: response is not exfiltration.** A 32 KiB cap is large enough for normal answers and small enough that a card can render it without UI degradation. It also bounds the worst case for the iframe's `<Text>` / `<MarkdownView>` rendering. This is a wire-level constraint, not a guideline — the agent cannot opt around it.

Capabilities that do *not* require server round-trip (`notes.save`, `storage.*`, `os.*`, `card.*`) are handled entirely by the Tauri broker and never reach Hermes. They remain synchronous client-side; no correlation needed.

#### 3.5.4 Cancellation: `widget.api_cancel`

A pending `widget.api_call` may need to be aborted. Two triggers:

**Triggered by the client (most common): card disposal mid-flight.** When the user closes a card or `widget.dispose` arrives from the server, any `correlation_id` associated with that `card_id` is implicitly cancelled. The client SHOULD send an explicit `widget.api_cancel` event so the server can stop the underlying work (e.g., interrupt a `prompt.btw` that hasn't completed):

```json
{
  "type": "widget.api_cancel",
  "session_id": "ab12cd34",
  "payload": {
    "correlation_id": "corr_a1b2c3",
    "card_id": "wgt_8a3f9c",
    "reason": "card_disposed"
  }
}
```

`reason` values: `card_disposed`, `card_updated` (source replaced — old promises abandoned), `user_cancelled` (future: explicit UI affordance for "stop this question"). On receipt, the server attempts to cancel the in-flight work and removes the correlation from the registry. No `widget.api_response` will be emitted for that correlation.

If the work is already complete and the response is already in the queue when the cancel arrives, the server SHOULD drop the response rather than emit it. Either way, the client treats no-response as the success criterion of cancellation.

**Triggered by the server (rarer): session ending mid-flight.** If a session is ended or disconnected while a `widget.api_call` is in flight, the server emits `widget.api_cancel` with `reason: "session_ended"` for each pending correlation as part of cleanup. (In practice the connection drop usually beats this, but specifying it ensures the registry is always cleared cleanly.)

**Cancellation is best-effort.** A `prompt.btw` that has already produced a response by the time the cancel is processed cannot be un-done; in that case the server simply drops the response. There's no acknowledgment of cancellation — fire-and-forget by design.

### 3.6 Lifecycle events: `widget.mounted`, `widget.error`, `widget.disposed` (client → server)

These flow client → server as `event` messages on the WebSocket. They use the same envelope shape as server-emitted events but originate from the client. (This is a small extension of the base contract, which today only emits server → client events. See §11.5 for why.)

```json
{
  "type": "widget.mounted",
  "session_id": "ab12cd34",
  "payload": { "card_id": "wgt_8a3f9c", "compiled_size": 4823, "compile_ms": 12 }
}
```

```json
{
  "type": "widget.error",
  "session_id": "ab12cd34",
  "payload": {
    "card_id": "wgt_8a3f9c",
    "phase": "compile",
    "kind": "syntax_error",
    "message": "Unexpected token at line 8",
    "stack": "..."
  }
}
```

`phase` is one of `validate` (server rejected before mount), `compile` (esbuild-wasm error), `mount` (React threw on first render), `runtime` (component threw later), `capability` (card called something it wasn't allowed to). `kind` is a short stable string suitable for branching; `message` is human-readable.

```json
{
  "type": "widget.disposed",
  "session_id": "ab12cd34",
  "payload": { "card_id": "wgt_8a3f9c", "reason": "user_closed" }
}
```

Reasons from the client side: `user_closed`, `agent_disposed`, `superseded`, `error`, `session_ended`.

The agent's tool-call result for `render_widget` resolves on receipt of `widget.mounted` *or* `widget.error` for the matching `card_id` — whichever arrives first. (See §6.1 for the resolution semantics.)

---

## 4. The `canvasAPI` capability surface (v1)

This is the shared vocabulary. Cards compose from these; the agent's `render_widget` invocation declares which it needs; the Tauri broker enforces. Adding a new capability requires updating both sides.

### 4.1 `hermes.ask` — round-trip to the agent

Card calls `canvasAPI.hermes.ask(prompt: string): Promise<string>`. The Tauri broker sends a `widget.api_call` request (see §3.5 async pattern). The Hermes server runs it as a `prompt.btw` (side-channel question that doesn't pollute main session history) and returns the response text.

Response payload (delivered via `widget.api_response`):
```json
{ "result": { "answer": "Q3 revenue was $4.2M, up 18% YoY." } }
```

**Response size: hard-capped at 32 KiB.** The serialized `result` field of `widget.api_response` cannot exceed 32 KiB. If `prompt.btw` returns more, the server MUST truncate-and-error or refuse-and-error — it MUST NOT emit a successful response over the cap. Specifically:

1. The server runs `prompt.btw`.
2. Before constructing the `widget.api_response`, the server measures the serialized response size.
3. If size > 32 KiB, the server emits an error response with code `4106` ("response too large") instead. The error message includes the actual size and the cap, so the agent can see the overflow and react.
4. The card's Promise rejects with a structured error.

**Why a hard cap and not a soft suggestion.** Cards rendering 50 KiB of markdown in a `<Text>` component are sluggish; large responses also encourage the agent to cram structured data into prose instead of using `widget.message` with proper structure. Making the cap a wire-level constraint forces the right pattern via the type system, not via documentation the agent might or might not follow. Hard caps are honest with the agent: it gets a clean rejection it can reason about, not a card that renders slowly and looks broken.

**The right pattern for large data.** The agent should `render_widget` once with empty placeholder source, then push data via one or more `widget_message` calls (each capped at 256 KiB) that the card aggregates. This works because the card author (the agent) knows the data shape and the card can paginate / virtualize / progressively render. `hermes.ask` is for short answers; `widget_message` is for structured data.

Errors use the standard error codes (§8). Card sees a Promise rejection with error code and message in the rejection value.

### 4.2 `hermes.stream` — streaming round-trip (deferred to v2)

Same as `hermes.ask` but the response streams as `widget.message` events with `kind: "hermes.stream.delta"`. **Not in v1**; mention here so the namespace is reserved.

### 4.3 `notes.save` — Tauri-side, no Hermes involvement

Card calls `canvasAPI.notes.save({ title, body, tags })`. The Tauri broker calls the existing note service and returns `{ note_id }`. Hermes never sees this.

### 4.4 `storage.get` / `storage.set` / `storage.keys` — per-card kv

Each card has a private kv namespace keyed by `card_id`, persisted in the Tauri host's local storage. The card can read and write its own state across re-mounts (e.g., user pinned a row, expanded a section). Hermes does not see this and cannot read another card's storage.

### 4.5 `card.resize`, `card.set_title`, `card.close` — self-management

The card can request size/title changes or close itself. Acts on `self` only. Hermes does not see these calls but does receive `widget.disposed` if the card calls `card.close`.

### 4.6 `os.notify`, `os.copy_clipboard` — OS bridge (Tauri-side)

Each is a deliberate grant. `os.notify({ text })` produces an OS notification. `os.copy_clipboard({ text })` copies to clipboard. Both are fire-and-forget; both require explicit capability declaration; neither involves Hermes.

### 4.7 Capabilities NOT in v1

For reference, things the spec *deliberately* excludes from v1 so Claude Code doesn't add them speculatively:

- File system access (read or write)
- Network calls of any kind from inside the card
- Cross-card communication (cards talking to other cards directly)
- Direct access to the Hermes session history
- Any OS dialog (file picker, alert, confirm)
- Any access to the host's IPC channel

If the agent appears to need any of these, that is a signal to re-route through `hermes.ask` (let the agent do it server-side and `widget.message` the result in) — not to add a new capability.

---

## 5. The widget tools

Hermes is an agent; it does work by calling tools. Six new tools are registered in the standard tool registry, conditional on the client advertising `widget.render` capability in `client.hello`:

- `render_widget` — create a card.
- `widget_update` — replace the source of a live card.
- `widget_message` — push data into a live card without remount.
- `widget_dispose` — close a live card.
- `list_widget_examples` — discover available example patterns.
- `read_widget_example` — fetch a specific example with documentation.

All six tools are stateless — they take a `card_id` (a plain string) where applicable and don't return objects with methods. This matches how every other Hermes tool works and is what allows the agent's tool history to survive serialization across turns.

### 5.1 Tool signatures

```python
@tool
def render_widget(
    source: str,
    capabilities: list[str],
    title: str | None = None,
    initial_size: dict | None = None,
) -> str:
    """
    Render a custom React card on the user's canvas.

    Args:
        source: JSX source. Must export a default React component.
                Available globals: React (with hooks), canvasAPI, primitives library.
                See list_widget_examples / read_widget_example for the import surface.
        capabilities: Names of canvasAPI methods the card will call.
                      Subset of: hermes.ask, notes.save, storage.get, storage.set,
                      storage.keys, card.resize, card.set_title, card.close,
                      os.notify, os.copy_clipboard.
        title: Human-readable card title shown in window chrome.
        initial_size: { "w": int, "h": int } in canvas units.

    Returns:
        The card_id string (e.g. "wgt_8a3f9c"). The agent stores this in
        its turn history and passes it to widget_update, widget_message,
        or widget_dispose to interact with the live card later.

    Raises:
        WidgetMountError: if the client failed to compile or mount the card.
                          Carries the underlying error message and phase
                          ("compile", "mount") so the agent can fix and retry.
        WidgetTimeoutError: if 10 seconds elapse without widget.mounted
                            or widget.error from the client.
    """
```

```python
@tool
def widget_update(
    card_id: str,
    source: str,
    capabilities: list[str] | None = None,
) -> dict:
    """
    Replace the source of a live card. Card position is preserved;
    React state is reset (the iframe re-mounts). Any pending hermes.ask
    promises from the old version are cancelled (see §3.5.4).

    Args:
        card_id: The card_id returned by render_widget.
        source: New JSX source.
        capabilities: Optional updated capability allowlist.
                      If omitted, the original allowlist is preserved.

    Returns:
        { "updated": bool, "card_gone": bool }
        - updated=True, card_gone=False: success.
        - updated=False, card_gone=True: the card was disposed before
          this update (e.g. user closed it). The agent can decide to
          re-render via render_widget if it still wants the card.
    """
```

```python
@tool
def widget_message(
    card_id: str,
    payload: dict,
) -> dict:
    """
    Push a structured message into a live card.
    The card's React tree receives it via canvasAPI.onMessage(handler).

    Args:
        card_id: The card_id returned by render_widget.
        payload: Arbitrary JSON-serializable data the card understands.
                 Max 256 KiB serialized.

    Returns:
        { "delivered": bool, "card_gone": bool }
        - delivered=True, card_gone=False: payload posted to the iframe.
        - delivered=False, card_gone=True: the card was disposed before
          delivery. Message is dropped.
    """
```

```python
@tool
def widget_dispose(
    card_id: str,
    reason: str = "task_complete",
) -> dict:
    """
    Close a live card. The Tauri host unmounts the iframe.
    Idempotent — calling dispose on an already-disposed card returns
    cleanly with `already_disposed=True`.

    Args:
        card_id: The card_id returned by render_widget.
        reason: Short observability string. Suggested: task_complete,
                superseded, error, agent_initiated.

    Returns:
        { "disposed": bool, "already_disposed": bool }
        - disposed=True, already_disposed=False: this call closed it.
        - disposed=False, already_disposed=True: the card was already
          gone (e.g. user closed it earlier). The agent can use this
          to know whether the user got there first.
    """
```

### 5.2 Why no `WidgetHandle` object

**Resolved decision.** The v0 spec defined a `WidgetHandle` Python object returned from `render_widget` with `.update()`, `.message()`, `.dispose()` methods. Hermes' self-review surfaced that this does not work: tool results are serialized as JSON messages, and a Python object with methods cannot survive serialization. The agent is stateless — every turn is a fresh reconstruction. So:

- `render_widget` returns a plain `card_id` string.
- The agent stores `card_id` in the conversation (tool result messages persist naturally).
- On the next turn, the agent reads the `card_id` from its message history and calls `widget_update`, `widget_message`, or `widget_dispose`.
- The server maintains a session-scoped `WidgetRegistry` that maps `card_id` → metadata for validation.

The agent's mental model is "I called a tool that produced a card with id X, and I can pass X to other tools" — not "I have a handle object I can call methods on." The network plumbing remains invisible, but the programming model matches how all other Hermes tools work.

### 5.3 When the agent should call `render_widget`

A system prompt addendum (a new fragment in `gateway/system_prompts/`) instructs the agent on heuristics. To minimize token cost, the addendum is kept lean — it describes *when* to render widgets and gives the tool signatures, but does NOT inline the primitives types or examples:

- The user's task produces a *bounded artifact* with state — a draft, a form, a chart, a tracker, a comparison view — and the user will benefit from interacting with it rather than reading a wall of text.
- The information has a structure plain prose flattens — a small dataset, a comparison matrix, a plan with checkboxes, a configuration the user will tweak.
- The user explicitly asked to *see* something, *try* something, or *adjust* something.

The addendum also instructs the agent *not* to render a widget for short factual answers, conversational replies, or content that's purely textual narrative. Default to prose; reach for widgets when prose is the wrong shape.

**Primitives types and examples are fetched on demand** via two additional tools:

```python
@tool
def list_widget_examples() -> list[dict]:
    """
    List available widget example files.

    Returns: A list of { "name": str, "summary": str } objects.
    Each summary is a one-line description ("Form with hermes.ask",
    "Chart of arbitrary data", etc.) so the agent can pick the most
    relevant example without reading every file.

    Call this first to see what's available, then call
    read_widget_example(name) for the one(s) you want.
    """
```

```python
@tool
def read_widget_example(name: str) -> str:
    """
    Read a specific widget example .tsx file and its inline
    documentation. Use this when you're about to render a widget
    and need a reference for a pattern.

    Args:
        name: Example name (without .tsx extension).
              Call list_widget_examples() first to see available names.

    Returns: The full .tsx file content with JSDoc comments showing
             which primitives, capabilities, and patterns are used.
    """
```

These tools read from `gateway/system_prompts/widget_examples/`. The agent is expected to call them *just before* calling `render_widget`, not at every turn. This keeps the system prompt small (~30 lines of heuristics) and moves the ~500+ lines of types and examples to on-demand fetch, saving tokens in every turn that doesn't produce a widget.

The exact phrasing of the addendum is left for Claude Code to draft during planning. It should be reviewable as a separate diff so the project owner can iterate on the prompt independently of the wire code.

### 5.4 What the agent should write

The system prompt addendum describes the import surface (but not the full types — those are fetched on demand via `read_widget_example`):

- `React` and its hooks (`useState`, `useEffect`, `useRef`, `useMemo`, `useCallback`) are global.
- `canvasAPI` is global. Methods correspond to declared capabilities; calling an undeclared method raises.
- A primitives library is available at `import { Card, Field, Button, Text, Stack, Row, Chart, ... } from 'canvas-primitives'`. (The Tauri-side bootstrap ships these. Their TypeScript declarations live in the contracts pipeline — see §11.6 — and are surfaced to the agent via the example files and the `read_widget_example` tool.)
- No other imports. No CDN. No fetch.

Examples of well-written cards live in `gateway/system_prompts/widget_examples/` as a directory of `.tsx` files. The agent discovers and reads them via `list_widget_examples()` and `read_widget_example(name)`. Claude Code should produce 4-6 starter examples alongside the addendum: a static info card, a form with hermes.ask, a list with storage persistence, a chart, an interactive editor with notes.save. (These same files are generated by the Tauri-side contracts pipeline; see §11.6.)

### 5.5 The agent's mental model for widget lifecycle

The addendum should also encode this:

- Widgets stay until disposed. Don't render a card for transient acknowledgements.
- If you produce a card and then realize it's wrong, prefer `widget_update()` over disposing and re-rendering — it preserves position and feels less jarring.
- Use `widget_message()` for incremental updates the card can absorb without remount (data refreshes, status pings, large data deliveries).
- Dispose explicitly when the task that motivated the card is done — don't leak cards across topics.
- The `card_id` is returned as a plain string from `render_widget`. Store it in your message history; on the next turn, re-read it and pass to `widget_update`, `widget_message`, or `widget_dispose`.
- If `widget_update` or `widget_dispose` returns `card_gone: True` or `already_disposed: True`, the user closed the card. Don't treat that as an error; use it as signal that the user has moved on, and decide whether to re-render or just continue.

---

## 6. Streaming and timing

### 6.1 v1: atomic source

The agent sends complete source in one `widget.render`. No partial streaming inside the JSX. The Tauri host shows a brief spinner during compile (typically 5-30ms with a warm iframe pool) and then mounts.

The `render_widget` tool resolves when `widget.mounted` arrives. While the tool is in-flight, the standard `tool.start` / `tool.complete` events fire as for any other tool — the existing `<ToolProgressCard>` shows "Rendering widget…" the whole time. This is the simplest behavior and the right v1.

### 6.2 v2 (deferred): chunked streaming

A future extension can let the agent stream JSX as it generates it, with the Tauri host showing the card materializing live. Reserving the namespace: `widget.render.chunk` events would carry partial source, with a final `widget.render.complete` to finalize. Out of scope for v1.

### 6.3 Timeouts

- `render_widget` tool: 10s default before raising. Configurable per-call. Tauri side should mount-or-error within 2s under normal conditions; 10s exists for cold-start cases.
- `hermes.ask` from a card: uses standard `prompt.btw` timeouts (no new constraint). Long calls don't block other messages thanks to the §3.5 async pattern.
- `widget_update`, `widget_message`, `widget_dispose`: these don't wait for client confirmation — they fire-and-forget over the wire. The tool returns based on registry state, not client ack. Effective timeout: zero (the call resolves immediately based on what the server knows).

---

## 7. Versioning and feature negotiation

The base contract uses `client.hello` capability negotiation. This spec adds two new capabilities:

- Server-advertised: `widget.render` — server can render widgets in this session.
- Client-advertised: `widget.render` — client can mount widgets and run the iframe pool.

If either side is missing the capability, the six widget tools are *not registered* in this session — the agent doesn't see them and can't call them. The agent gracefully falls back to text-only output.

This means the Tauri team can ship the iframe sandbox progressively without breaking older Hermes builds, and Hermes can ship widget tools to non-Tauri front-ends (which simply won't render anything) without errors.

---

## 8. Error handling

In addition to the base error codes:

| Code | Meaning |
|---|---|
| `4101` | Unknown capability declared in `widget.render`. |
| `4102` | Source exceeds size limit (256 KiB). |
| `4103` | Card id unknown for `widget.update` / `widget.message` / `widget.dispose`. |
| `4104` | Capability not declared but called via `widget.api_call`. |
| `4105` | Card capability call rejected by user (if per-call approval is enabled). |
| `4106` | `widget.api_response` payload exceeds 32 KiB cap. The agent should split the response via `widget.message`. |
| `4107` | `widget.message` payload exceeds 256 KiB cap. |
| `5101` | Tauri client refused to mount (compile or validate failure). Carries inner error in payload. |
| `5102` | `render_widget` tool timed out waiting for `widget.mounted`. |
| `5103` | `widget.api_call` correlation expired (no response within capability-specific timeout). |

---

## 9. Per-connection isolation

Widgets are session-scoped, and sessions are connection-scoped per §10 of the base contract. Practical implications:

- A widget rendered from session A on connection X is not visible to session B or connection Y.
- `session.resume` (which builds a fresh agent) does *not* re-attach to widgets from the previous in-flight agent. The widgets are gone with that connection.
- `session.branch` does not duplicate live widgets; the new branch starts with no cards.
- All in-flight `widget.api_call` correlations for a session are cancelled (per §3.5.4) when the session ends or disconnects.

This matches how `<ToolProgressCard>` and `<ApprovalModal>` already behave. It's the right default. (See §12 for the future extension if persistence-across-resume is wanted.)

---

## 10. Domain test scenarios

Behavior tests in GIVEN/WHEN/THEN form. WHY fields encode the *intent* — Claude Code should preserve the spirit, not just the letter.

### 10.1 Happy path: agent renders a card and the user sees it

```
GIVEN a connected Tauri client with widget.render in capabilities
  AND a session with the render_widget tool registered
 WHEN the agent invokes render_widget with valid source and ["hermes.ask"]
 THEN tui_gateway emits widget.render with a fresh card_id
  AND the tool blocks until widget.mounted arrives
  AND the tool returns the card_id string (e.g. "wgt_8a3f9c")
  AND the agent's next turn can call widget_update / widget_dispose with that card_id
WHY the agent's mental model is "I called a tool that produced a card with id X, and I can pass X to other tools" — the network plumbing is invisible and the handle-less design survives serialization across turns.
```

### 10.2 Capability negotiation absent

```
GIVEN a connected client that did NOT advertise widget.render
 WHEN a session is created
 THEN none of the six widget tools are in the agent's tool registry for that session
  AND prompts that previously would have rendered widgets fall back to prose
WHY older clients (or non-Tauri clients) must keep working with no observable change.
```

### 10.3 Capability declared but not called

```
GIVEN a card declared ["hermes.ask", "notes.save"]
  AND the card never calls notes.save during its lifetime
 WHEN the card is disposed
 THEN no notes.save calls occurred
  AND the card mounted and ran normally
WHY declaring a capability is a permission grant, not a mandate. Cards declare the union of what they MIGHT do.
```

### 10.4 Capability called but not declared

```
GIVEN a card declared ["notes.save"]
 WHEN the card calls canvasAPI.hermes.ask("...")
 THEN the Tauri broker rejects locally (does NOT round-trip to Hermes)
  AND the card sees a Promise rejection with an "undeclared capability" error
  AND a widget.error event with phase="capability" is emitted to Hermes
WHY undeclared calls should never reach Hermes — that's the whole point of declaration. The agent learns of the failure via the error event and can update the card.
```

### 10.5 Source compile error

```
GIVEN the agent invokes render_widget with malformed JSX
 WHEN the Tauri client fails to compile via esbuild-wasm
 THEN widget.error fires with phase="compile" and a useful message
  AND the render_widget tool raises WidgetMountError
  AND the exception carries the phase and underlying error message
  AND the agent's reasoning loop can read the error and retry with fixed source
WHY compile errors should be self-correcting in one or two retries; the error has to be specific enough for the agent to act on it.
```

### 10.6 Card update preserves position, resets state

```
GIVEN a mounted card at canvas position (x, y) with internal React state
  AND a pending hermes.ask call from the card with correlation_id "corr_abc"
 WHEN the agent calls widget_update with new source
 THEN tui_gateway emits widget.update with the new source
  AND the iframe re-mounts with new source
  AND the card's canvas position is unchanged
  AND the card's React state is reset to initial
  AND widget.api_cancel for "corr_abc" with reason="card_updated" is emitted
  AND any later widget.api_response for "corr_abc" is dropped on arrival
  AND the tool returns { "updated": True, "card_gone": False }
WHY position is the user's; state is the card's. Updating means "new version of the same thing" — moving the card would feel jarring; preserving stale state would feel buggy. Pending capability calls from the old version must be cancelled to avoid the new version receiving stale data.
```

### 10.7 hermes.ask round-trip (async pattern)

```
GIVEN a mounted card declared ["hermes.ask"]
 WHEN the card calls canvasAPI.hermes.ask("What's my Q3 revenue?")
 THEN the Tauri broker sends widget.api_call with capability="hermes.ask" and a fresh correlation_id
  AND the server responds immediately with { accepted: true, correlation_id: "corr_..." }
  AND tui_gateway dispatches it as a prompt.btw to the same session (non-blocking for other messages)
  WHEN prompt.btw completes
  THEN the server emits widget.api_response with the correlation_id and { result: { answer: "..." } }
  AND the broker resolves the iframe's pending Promise with the answer
  AND the main session's history is NOT polluted with the btw exchange
  AND the WebSocket remained responsive for other messages during the entire round-trip
WHY cards asking questions shouldn't clutter the main thread or block the connection. The async pattern ensures long-running hermes.ask calls don't time out spuriously or freeze the session.
```

### 10.8 Disposal cleanup with race condition (idempotent both sides)

```
GIVEN a mounted card with a pending hermes.ask call in flight
 WHEN the user closes the card at roughly the same moment the agent calls widget_dispose
 THEN both paths converge on a clean teardown:
  - Client emits widget.disposed with reason="user_closed"
  - Server's widget.dispose handler sees the card already disposed, no-ops
  - widget_dispose tool returns { "disposed": False, "already_disposed": True }
  - widget.api_cancel is emitted by the client for the pending correlation
  - If widget.api_response arrives later, it is dropped (registry lookup fails)
  AND no error events fire from the race
WHY closing a card should never produce phantom updates or zombie state. Both actors can trigger disposal; the system handles the race without error and gives the agent useful signal about who got there first.
```

```
GIVEN a disposed card that the agent's history references by id
 WHEN the agent calls widget_dispose(card_id="wgt_8a3f9c") on the next turn
 THEN no widget.dispose event is emitted (card is already gone)
  AND the tool returns { "disposed": False, "already_disposed": True }
  AND no error is raised
WHY agent-side dispose is idempotent and informative. The agent learns the card was already closed and can skip cleanup on its end without an error path.
```

### 10.9 Session disconnect

```
GIVEN a session with three live widgets and one in-flight hermes.ask
 WHEN the WebSocket disconnects
 THEN per base contract §8.1, the session is unregistered
  AND the WidgetRegistry for that session is cleared
  AND the ApiCallRegistry has the in-flight correlation cancelled (or attempted)
  AND if the same session is later resumed via session.resume
  THEN the new in-flight agent has zero live widgets
  AND no card_ids from the previous session are valid
WHY consistent with how ToolProgressCard and approvals behave today. No special-casing for widgets.
```

### 10.10 Source size limit

```
GIVEN the agent invokes render_widget with source > 256 KiB
 WHEN tui_gateway validates the payload
 THEN the tool raises with error code 4102
  AND no widget.render event is emitted to the client
WHY oversized payloads are usually a sign the agent inlined data that should have come through hermes.ask or widget_message. The error nudges it toward the right pattern.
```

### 10.11 Cancellation on card disposal mid-flight

```
GIVEN a mounted card with a hermes.ask call in flight (correlation_id="corr_xyz")
  AND the underlying prompt.btw is still in progress
 WHEN the user closes the card
 THEN the client emits widget.disposed with reason="user_closed"
  AND the client emits widget.api_cancel for "corr_xyz" with reason="card_disposed"
  AND the server attempts to cancel the underlying prompt.btw
  AND the correlation is removed from the ApiCallRegistry
  AND if the prompt.btw produces a response after cancellation, the response is dropped (not emitted as widget.api_response)
WHY a disposed card should not produce phantom updates after closing. Cancellation is best-effort but prevents the worst case (zombie response landing in the void after the card is gone).
```

### 10.12 Response size cap on hermes.ask

```
GIVEN a card calls canvasAPI.hermes.ask(...) with a prompt that the agent answers in 50 KiB of markdown
 WHEN the server is preparing widget.api_response
 THEN the server measures the serialized result size, sees it > 32 KiB
  AND the server emits widget.api_response with error code 4106 instead of result
  AND the error message includes both the actual size and the 32 KiB cap
  AND the card's Promise rejects with the structured error
  AND the agent observing the error can split via widget_message + a smaller hermes.ask
WHY a hard cap forces the right pattern (paginate via widget_message) at the wire layer instead of relying on the agent reading guidance. The error is specific enough to be self-correcting.
```

### 10.13 Idempotency on update with disposed card

```
GIVEN a card with card_id "wgt_8a3f9c" was disposed
 WHEN the agent calls widget_update(card_id="wgt_8a3f9c", source="...")
 THEN no widget.update event is emitted
  AND the tool returns { "updated": False, "card_gone": True }
  AND no error is raised
WHY the agent should be able to attempt updates without error-handling boilerplate. The "card_gone" signal is honest and actionable — the agent can decide to re-render via render_widget.
```

---

## 11. Open considerations for the planner

Decisions Claude Code should surface and propose during domain discovery, not silently resolve.

### 11.1 Where the system prompt addendum lives

The current `gateway/system_prompts/` structure should be inspected. The widget-author addendum is small now (~30 lines of heuristics) thanks to on-demand example fetching, but should it be a single file, a directory of fragments composed conditionally, or a skill? The right answer depends on conventions already in use.

### 11.2 The starter `canvas-primitives` library reference

The Tauri-side bootstrap ships `<Card>`, `<Field>`, `<Button>`, etc. The agent learns about them via the example files (§5.3). The example files themselves are *generated* by the Tauri-side contracts pipeline (per the Tauri spec §11) and copied to `gateway/system_prompts/widget_examples/`. Open question: how does the sync happen mechanically? Manual copy (current state), git submodule, or a sync script? Pick one explicitly during planning.

### 11.3 Approval gating

v1 mounts cards eagerly — no per-card approval. But `hermes.ask` and `notes.save` are real actions; should they get the standard `approval.request` flow? Suggest: gated by an existing tool-approval policy (if `notes.save` is a registered tool, its approval policy applies; if `hermes.ask` is a btw, btws are usually unapproved). Verify against current policy and propose.

### 11.4 Persistence

v1: widgets do not survive `session.resume`. This matches existing ephemeral surfaces (tool progress, approvals). If we ever want widgets to survive — e.g., a long-lived dashboard the user wants to come back to tomorrow — the path is a new `widget.persist` capability that snapshots `(source, capabilities, last storage state)` to `state.db` and replays on resume. **Not v1**; mentioned so the namespace is reserved.

### 11.5 Client-emitted events as a contract addition

The base contract today only has server → client events. Adding client → server events (`widget.mounted`, `widget.error`, `widget.disposed`, `widget.api_cancel`) is a small but real extension. Claude Code should verify the `tui_gateway` dispatcher can handle them cleanly and propose whether they need their own envelope shape or can re-use the existing `event` envelope with an originating-side flag.

### 11.6 Observability

`tool.start` / `tool.complete` exist for `render_widget`. Should there also be a `widget.api_call` log entry visible to the user? Helpful for debugging cards. Potentially noisy. Suggest: behind a "show widget activity" preference, off by default. Same for `widget.api_cancel` and `widget.api_response`.

### 11.7 Example tool visibility

`list_widget_examples` and `read_widget_example` are registered alongside the four core widget tools. Should they always be visible whenever widget rendering is available, or should they only become visible after the agent has committed to rendering (e.g. via a "tool revealing" mechanism if Hermes has one)? The first is simpler; the second saves a few tokens of "tool exists" overhead per turn. Propose during planning based on what's idiomatic in the Hermes tool registry.

### 11.8 Cancellation timeouts

§3.5.4 specifies cancellation is best-effort with no acknowledgment. Should there be a server-side cap on how long a cancelled `prompt.btw` is allowed to keep running before it's hard-killed? Today, agentic work that doesn't check for cancellation may run to completion regardless. v1 can ignore this; v2 might want to track "cancelled but still running" for resource accounting.

---

## 12. But if… — alternative paths to consider

The shape above is a recommendation. Reasonable alternatives the planner should weigh and reject (or adopt) explicitly:

**But if the agent shouldn't be writing JSX at all,** the alternative is a *descriptor-driven* model: the agent emits a JSON tree of typed primitives + RPC bindings, and the Tauri host renders that. Safer (no code execution at all), more bounded (the agent can only build what the descriptor schema allows), but caps the ceiling on what the agent can invent. This spec assumes the user has already chosen code-as-payload.

**But if streaming render is a hard requirement,** v1 should support `widget.render.chunk` from day one rather than deferring. The complication is that partial JSX is invalid until complete, so the host needs a buffer-then-compile strategy and a placeholder-during-buffer UX. Adds maybe 30% to the v1 scope. Worth it if "the card materializing as the agent thinks" is a defining feature; not worth it if it's a nice-to-have.

**But if widgets should persist across sessions,** §11.4's snapshot model needs to be v1, not v2. The complication is that storage state can get arbitrarily large and the source can drift relative to the storage shape. Punting is the safer call unless persistence is a known user need.

**But if 32 KiB is the wrong response cap,** the planner can argue for 16 KiB (tighter, forces structured data sooner) or 64 KiB (more permissive, fewer 4106 errors). 32 KiB is the v2 author's pick; the wire constant should be a single named value (`HERMES_ASK_RESPONSE_CAP_BYTES`) so it can be tuned without spec-rewrites.

**But if the planner sees a cleaner factoring that splits `render_widget` into `widget.create` + `widget.update_source` + …,** that's allowed — the wire shape is the contract, the agent-facing tool surface is open for the planner to refine.

---

## 13. Acceptance criteria

The Hermes-side work is done when:

1. `tui_gateway/server.py` registers all `widget.*` events and methods per §3, including the async `widget.api_call` / `widget.api_response` pattern and `widget.api_cancel`.
2. `gateway/platforms/desktop_app.py` surfaces them to the desktop adapter without modification (they should ride the existing dispatcher).
3. The six widget tools (`render_widget`, `widget_update`, `widget_message`, `widget_dispose`, `list_widget_examples`, `read_widget_example`) are registered conditionally on the client advertising `widget.render` capability. Tool return types match §5.1 (informative dicts where applicable, not bare `None`).
4. A `WidgetRegistry` per-session tracks live cards and is wired to disconnect/resume cleanup. Disposal is idempotent on both sides (server *and* client races handled per §3.4 and tested per §10.8).
5. An `ApiCallRegistry` per-session tracks in-flight `widget.api_call` correlations. Card disposal triggers cancellation of associated correlations (§3.5.4, tested per §10.11).
6. A system prompt addendum exists (lean — heuristics only, ~30 lines) and is loaded into the agent's context when `render_widget` is available.
7. At least 4 starter widget examples exist in `gateway/system_prompts/widget_examples/`, discoverable via `list_widget_examples()` and readable via `read_widget_example(name)`.
8. `hermes.ask` round-trips use the async pattern: `widget.api_call` → immediate ack → `widget.api_response` on completion. The WebSocket is not blocked during processing, and long-running calls do not hit JSON-RPC timeouts.
9. `widget.api_response` payload size is enforced at the wire layer with hard cap 32 KiB and error code 4106. Tested per §10.12.
10. All test scenarios in §10 pass against a mock client.
11. The base wire contract (`tauri-client-contract.md`) gets a `§N. Widget render` section appended that mirrors §3 here, with the canonical version living in the contract doc going forward.

The Tauri side acceptance lives in the separate Tauri-side spec and includes the iframe sandbox, esbuild-wasm pipeline, capability broker, iframe pool, and `<AgentWidgetCard>` integration into the existing canvas.

---

## 14. Reference: minimal happy-path flow

1. **Client connects.** Tauri sends `client.hello` with `widget.render` in capabilities.
2. **Server registers tools.** `render_widget`, `widget_update`, `widget_message`, `widget_dispose`, `list_widget_examples`, and `read_widget_example` join this session's tool registry.
3. **Agent prepares.** Agent calls `list_widget_examples()` and `read_widget_example("form-with-hermes-ask")` to get the primitives API surface and a reference pattern.
4. **User asks for something widget-y.** "Make me a draft form for the Q3 retro."
5. **Agent invokes `render_widget`.** Source is JSX with a form, declares `["hermes.ask", "notes.save"]`.
6. **Server emits `widget.render`.** With fresh `card_id`, source, and capabilities. Card registered in `WidgetRegistry`.
7. **Client compiles, mounts, emits `widget.mounted`.** Tool resolves; agent gets `card_id` string back.
8. **User edits the form, clicks "ask Hermes to fill in known fields".** Card calls `canvasAPI.hermes.ask("Fill in the known retro items from this quarter")`.
9. **Tauri broker sends `widget.api_call`** with `correlation_id: "corr_abc"`. Server acknowledges immediately with `{ accepted: true }`. Server starts `prompt.btw` in the background. Correlation registered in `ApiCallRegistry`.
10. **`prompt.btw` completes.** Server measures result (4 KiB — well under cap), emits `widget.api_response` with the answer.
11. **Card updates form fields with the answer.** User reviews, clicks save.
12. **Card calls `canvasAPI.notes.save({title, body, tags})`.** Tauri broker handles entirely client-side; no Hermes involvement. Note is created.
13. **Card calls `canvasAPI.card.close()`.** `widget.disposed` fires server-side (reason="agent_initiated"). Server clears the card from the registry; cancels any pending correlations (none, in this case).
14. **Agent's next turn sees the disposal** and calls `widget_dispose(card_id)` — which returns `{ disposed: False, already_disposed: True }`. Agent writes a confirmation reply.

---

## 15. Resolved decisions (history)

These were open questions in v0/v1 that have been closed during the spec's evolution. Kept here so the rationale is preserved for future readers; not actionable for the planner.

### 15.1 Resolved: handle survival across reasoning loops (was §11.3)

**The question:** how does a `WidgetHandle` Python object survive serialization across agent turns?

**The answer:** it doesn't. Tools return plain `card_id` strings; companion tools take `card_id` as input. This matches every other Hermes tool. See §5.2 for the full rationale.

### 15.2 Resolved: where canvas-primitives types ship to the agent (was §11.6)

**The question:** inline types in the system prompt (always-on, expensive), fetch on demand (extra tool calls), or load via skill?

**The answer:** fetch on demand via `list_widget_examples` and `read_widget_example`. The system prompt stays lean (~30 lines); types and example code are pulled into context only when the agent commits to rendering. See §5.3.

### 15.3 Resolved: synchronous vs async `widget.api_call` (was implicit)

**The question:** can a `hermes.ask` round-trip be a normal JSON-RPC request/response?

**The answer:** no — long-running calls would hit JSON-RPC timeouts and at minimum confuse the user about whether the connection is responsive. The async ack/correlate/respond pattern (§3.5) is the v1 baseline.

### 15.4 Resolved: response size discipline (was implicit)

**The question:** how do we keep the agent from cramming large data into `hermes.ask` answers?

**The answer:** a hard 32 KiB wire cap with error code 4106 (§3.5.3, §4.1). Discipline-via-types beats discipline-via-documentation.
