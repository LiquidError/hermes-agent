# Hermes Widget Render — Wire Spec

**Goal:** extend Hermes so the agent can render arbitrary React/JSX cards onto a desktop client's canvas. The agent decides *what* the card should look like (writes JSX); the client renders it inside a sandboxed iframe with a brokered capability surface. This spec covers the agent-host half: a `widget.*` RPC namespace on `tui_gateway`, a `render_widget` tool the agent can invoke, lifecycle events flowing back, and the contract for `canvasAPI` calls that need round-trips to the agent.

**Out of scope:** the client-side iframe sandbox, JSX compile pipeline, capability broker, and iframe pool. Those are the desktop client's concerns. This document defines only what the two sides agree on at the wire.

**Reference:** [desktop-app-adapter.md](./desktop-app-adapter.md) — the `DesktopAppAdapter` that exposes this surface to a network client.

---

## 1. Domain model

Three nouns, kept distinct on purpose:

**Source.** A string of JSX/JavaScript the agent generates. It is text, nothing more — the agent never executes it. It exports a single default React component. The agent is the *author*.

**Card.** A long-lived rendered instance of source on the client canvas. A card has an id, a position, a size, a state, and a capability allowlist. Multiple cards can exist concurrently; the same source can be rendered as multiple cards. The client is the *renderer*.

**Capability.** A named permission the card needs to call back to the world — `notes.save`, `hermes.ask`, `storage.set`, `os.notify`. The agent declares them up front when it emits source. The client broker is the *gatekeeper*.

> **Mental model.** Hermes is a writer who hands typed pages to a printer. The printer assembles the pages into a book, locks the book in a glass display case, and gives the reader a numbered list of buttons that *can* reach the writer. The reader can press a button to ask the writer something; the writer can never reach inside the case to change the book directly — only print a new one and have the printer swap the display.

**Why this split matters.** Each noun has a different lifetime, owner, and threat profile. Source is cheap and ephemeral (regenerate any time). Cards are durable and stateful (user expects them to stay open). Capabilities are the trust surface (every entry point reviewed). Conflating them produces the kind of "the agent has full DOM access" mistake we want to avoid.

---

## 2. Architecture summary

```
Hermes agent (Python, on the agent host)
   │
   │  agent invokes `render_widget` tool with (source, capabilities)
   │  ↓
   │  tui_gateway emits `widget.render` event over the active session
   │
   ▼
DesktopAppAdapter ──── WSS (existing tui_gateway transport) ────▶ Desktop client
                                                                     │
                                                                     │  spawns sandboxed iframe,
                                                                     │  brokers postMessage capability calls,
                                                                     │  emits lifecycle events back
                                                                     ▼
                       Hermes ◀──── widget.* events / api_call requests ────
```

The agent-host side adds:

- a new RPC topic (`widget.*`) registered on `tui_gateway/server.py`
- six new agent-facing tools (`render_widget`, `widget_update`, `widget_message`, `widget_dispose`, `list_widget_examples`, `read_widget_example`)
- a per-session `WidgetRegistry` tracking live cards by id with idempotent disposal semantics
- a per-session `ApiCallRegistry` tracking in-flight `widget.api_call` correlations
- handlers for `widget.api_call` requests from cards calling `canvasAPI.hermes.*`, with async response delivery via `widget.api_response`
- handlers for `widget.api_cancel` events to abort pending calls when cards are disposed mid-flight

Nothing else in Hermes changes. The persisted `state.db` schema is unaffected; widgets are session-scoped and not persisted across `session.resume`.

---

## 3. Wire contract additions

All additions are additive on top of the base wire protocol; the `client.hello` `protocol_version` does not bump.

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
| `capabilities` | yes | Subset of the capability surface (§4). Empty array means the card is purely presentational. Unknown capabilities cause the client to refuse mount and emit `widget.error`. |
| `title` | optional | Human-readable card title. Used for window-chrome / threads panel. Default: "Untitled card". |
| `initial_size` | optional | Suggested initial dimensions in canvas units. Client may clamp. Default: 400×280. |
| `trace_id` | optional | The originating tool-call id. Lets the client correlate this card with its tool-progress entry. |

The card is mounted *eagerly* — the user does not approve its appearance, the same way they don't approve a tool-progress card. (Capability calls *can* be approved per-call; see §4.) If a global "always confirm new agent cards" preference is wanted, that's a client-side concern, not a wire-contract concern.

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

The `message` shape is opaque to Hermes and the client — both sides treat it as JSON the card understands. It's the agent's job to keep the shape consistent with what the card it wrote knows how to handle. Max payload size: 256 KiB (same limit as `source`, for the same reason — large data should be paginated, not crammed into a single message).

### 3.4 New event: `widget.dispose` (server → client)

Agent wants to close the card. The client unmounts the iframe and emits `widget.disposed` back as confirmation.

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

**Why async.** `hermes.ask` can take seconds or minutes (research, code gen, multi-step reasoning). A synchronous JSON-RPC request would either block the WebSocket awareness layer or hit standard request timeouts (~30s) and fail spuriously even though the agent is still working. The async pattern below ensures the call survives long latencies and the connection stays clearly responsive.

#### 3.5.1 Request

When a card calls `canvasAPI.hermes.ask(...)` (or any other capability that requires server round-trip — currently the `hermes.*` family), the client broker sends this message:

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
- The session belongs to this connection.

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

On error: the payload includes `{ "correlation_id": "...", "card_id": "...", "error": { "code": ..., "message": "..." } }` instead of `result`. The client broker resolves or rejects the iframe's pending Promise accordingly, then removes the correlation from its in-flight map.

**Response size cap.** The total payload of `widget.api_response` (specifically the serialized `result` field) is hard-capped at 32 KiB. If a capability's natural result exceeds this (e.g., a `hermes.ask` answer of 50 KiB markdown), the server MUST emit an error response with code `4106` instead of the oversized result. The agent should split the answer (use `widget.message` for paginated chunks) or pre-process before returning. See §4.1 for the per-capability rationale.

**Response size means: response is not exfiltration.** A 32 KiB cap is large enough for normal answers and small enough that a card can render it without UI degradation. It also bounds the worst case for the iframe's `<Text>` / `<MarkdownView>` rendering. This is a wire-level constraint, not a guideline — the agent cannot opt around it.

Capabilities that do *not* require server round-trip (`notes.save`, `storage.*`, `os.*`, `card.*`) are handled entirely by the client broker and never reach Hermes. They remain synchronous client-side; no correlation needed.

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

`reason` values: `card_disposed`, `card_updated` (source replaced — old promises abandoned), `user_cancelled` (explicit UI affordance for "stop this question"). On receipt, the server attempts to cancel the in-flight work and removes the correlation from the registry. No `widget.api_response` will be emitted for that correlation.

If the work is already complete and the response is already in the queue when the cancel arrives, the server SHOULD drop the response rather than emit it. Either way, the client treats no-response as the success criterion of cancellation.

**Triggered by the server (rarer): session ending mid-flight.** If a session is ended or disconnected while a `widget.api_call` is in flight, the server emits `widget.api_cancel` with `reason: "session_ended"` for each pending correlation as part of cleanup. (In practice the connection drop usually beats this, but specifying it ensures the registry is always cleared cleanly.)

**Cancellation is best-effort.** A `prompt.btw` that has already produced a response by the time the cancel is processed cannot be un-done; in that case the server simply drops the response. There's no acknowledgment of cancellation — fire-and-forget by design.

### 3.6 Lifecycle events: `widget.mounted`, `widget.error`, `widget.disposed` (client → server)

These flow client → server as `event` messages on the WebSocket. They use the same envelope shape as server-emitted events but originate from the client.

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

`phase` is one of `validate` (server rejected before mount), `compile` (client compile error), `mount` (React threw on first render), `runtime` (component threw later), `capability` (card called something it wasn't allowed to). `kind` is a short stable string suitable for branching; `message` is human-readable.

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

## 4. The `canvasAPI` capability surface

This is the shared vocabulary. Cards compose from these; the agent's `render_widget` invocation declares which it needs; the client broker enforces. Adding a new capability requires updating both sides.

### 4.1 `hermes.ask` — round-trip to the agent

Card calls `canvasAPI.hermes.ask(prompt: string): Promise<string>`. The client broker sends a `widget.api_call` request (see §3.5 async pattern). The Hermes server runs it as a `prompt.btw` (side-channel question that doesn't pollute main session history) and returns the response text.

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

### 4.2 `hermes.stream` — streaming round-trip (reserved)

Same as `hermes.ask` but the response streams as `widget.message` events with `kind: "hermes.stream.delta"`. Not currently implemented; mention here so the namespace is reserved.

### 4.3 `notes.save` — client-side, no Hermes involvement

Card calls `canvasAPI.notes.save({ title, body, tags })`. The client broker calls the existing note service and returns `{ note_id }`. Hermes never sees this.

### 4.4 `storage.get` / `storage.set` / `storage.keys` — per-card kv

Each card has a private kv namespace keyed by `card_id`, persisted in the client's local storage. The card can read and write its own state across re-mounts (e.g., user pinned a row, expanded a section). Hermes does not see this and cannot read another card's storage.

### 4.5 `card.resize`, `card.set_title`, `card.close` — self-management

The card can request size/title changes or close itself. Acts on `self` only. Hermes does not see these calls but does receive `widget.disposed` if the card calls `card.close`.

### 4.6 `os.notify`, `os.copy_clipboard` — OS bridge (client-side)

Each is a deliberate grant. `os.notify({ text })` produces an OS notification. `os.copy_clipboard({ text })` copies to clipboard. Both are fire-and-forget; both require explicit capability declaration; neither involves Hermes.

### 4.7 Capabilities deliberately excluded

For reference, things the spec excludes so the agent doesn't reach for them speculatively:

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
    Close a live card. The client unmounts the iframe.
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

### 5.2 When the agent should call `render_widget`

A system prompt addendum (`assets/widget_prompts/addendum.md`) instructs the agent on heuristics. To minimize token cost, the addendum is kept lean — it describes *when* to render widgets and gives the tool signatures, but does NOT inline the primitives types or examples:

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

These tools read from `assets/widget_prompts/examples/`. The agent is expected to call them *just before* calling `render_widget`, not at every turn. This keeps the system prompt small (~30 lines of heuristics) and moves the ~500+ lines of types and examples to on-demand fetch, saving tokens in every turn that doesn't produce a widget.

### 5.3 What the agent should write

The system prompt addendum describes the import surface (but not the full types — those are fetched on demand via `read_widget_example`):

- `React` and its hooks (`useState`, `useEffect`, `useRef`, `useMemo`, `useCallback`) are global.
- `canvasAPI` is global. Methods correspond to declared capabilities; calling an undeclared method raises.
- A primitives library is available at `import { Card, Field, Button, Text, Stack, Row, Chart, ... } from 'canvas-primitives'`. (The client-side bootstrap ships these; the example files show the import surface.)
- No other imports. No CDN. No fetch.

Examples of well-written cards live in `assets/widget_prompts/examples/` as a directory of `.tsx` files. The agent discovers and reads them via `list_widget_examples()` and `read_widget_example(name)`. Starter examples cover: a static info card, a form with hermes.ask, a list with storage persistence, a chart.

### 5.4 The agent's mental model for widget lifecycle

The addendum encodes:

- Widgets stay until disposed. Don't render a card for transient acknowledgements.
- If you produce a card and then realize it's wrong, prefer `widget_update()` over disposing and re-rendering — it preserves position and feels less jarring.
- Use `widget_message()` for incremental updates the card can absorb without remount (data refreshes, status pings, large data deliveries).
- Dispose explicitly when the task that motivated the card is done — don't leak cards across topics.
- The `card_id` is returned as a plain string from `render_widget`. Store it in your message history; on the next turn, re-read it and pass to `widget_update`, `widget_message`, or `widget_dispose`.
- If `widget_update` or `widget_dispose` returns `card_gone: True` or `already_disposed: True`, the user closed the card. Don't treat that as an error; use it as signal that the user has moved on, and decide whether to re-render or just continue.

---

## 6. Streaming and timing

### 6.1 Atomic source

The agent sends complete source in one `widget.render`. No partial streaming inside the JSX. The client shows a brief spinner during compile (typically 5–30ms with a warm iframe pool) and then mounts.

The `render_widget` tool resolves when `widget.mounted` arrives. While the tool is in-flight, the standard `tool.start` / `tool.complete` events fire as for any other tool — the existing tool-progress card shows "Rendering widget…" the whole time.

### 6.2 Streaming render (reserved, not implemented)

A future extension can let the agent stream JSX as it generates it, with the client showing the card materializing live. Reserving the namespace: `widget.render.chunk` events would carry partial source, with a final `widget.render.complete` to finalize. Not currently implemented.

### 6.3 Timeouts

- `render_widget` tool: 10s default before raising. Configurable per-call. Client should mount-or-error within 2s under normal conditions; 10s exists for cold-start cases.
- `hermes.ask` from a card: uses standard `prompt.btw` timeouts (no new constraint). Long calls don't block other messages thanks to the §3.5 async pattern.
- `widget_update`, `widget_message`, `widget_dispose`: these don't wait for client confirmation — they fire-and-forget over the wire. The tool returns based on registry state, not client ack. Effective timeout: zero (the call resolves immediately based on what the server knows).

---

## 7. Versioning and feature negotiation

The base contract uses `client.hello` capability negotiation. This spec adds two new capabilities:

- Server-advertised: `widget.render` — server can render widgets in this session.
- Client-advertised: `widget.render` — client can mount widgets and run the iframe pool.

If either side is missing the capability, the six widget tools are *not registered* in this session — the agent doesn't see them and can't call them. The agent gracefully falls back to text-only output.

This means the client team can ship the iframe sandbox progressively without breaking older Hermes builds, and Hermes can ship widget tools to non-canvas front-ends (which simply won't render anything) without errors.

---

## 8. Error handling

In addition to the base error codes:

| Code | Meaning |
|---|---|
| `4101` | Unknown capability declared in `widget.render`. |
| `4102` | Source exceeds size limit (256 KiB). |
| `4103` | Card id unknown for `widget.update` / `widget.message` / `widget.dispose`. |
| `4104` | Capability not declared but called via `widget.api_call`. |
| `4106` | `widget.api_response` payload exceeds 32 KiB cap. The agent should split the response via `widget.message`. |
| `4107` | `widget.message` payload exceeds 256 KiB cap. |
| `5101` | Client refused to mount (compile or validate failure). Carries inner error in payload. |
| `5102` | `render_widget` tool timed out waiting for `widget.mounted`. |
| `5103` | `widget.api_call` correlation expired (no response within capability-specific timeout). |

---

## 9. Per-connection isolation

Widgets are session-scoped, and sessions are connection-scoped. Practical implications:

- A widget rendered from session A on connection X is not visible to session B or connection Y.
- `session.resume` (which builds a fresh agent) does *not* re-attach to widgets from the previous in-flight agent. The widgets are gone with that connection.
- `session.branch` does not duplicate live widgets; the new branch starts with no cards.
- All in-flight `widget.api_call` correlations for a session are cancelled (per §3.5.4) when the session ends or disconnects.

This matches how tool-progress cards and approval modals already behave.

---

## 10. Domain test scenarios

Behavior tests in GIVEN/WHEN/THEN form. WHY fields encode the *intent* — implementations should preserve the spirit, not just the letter.

### 10.1 Happy path: agent renders a card and the user sees it

```
GIVEN a connected client with widget.render in capabilities
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
WHY older clients (or non-canvas clients) must keep working with no observable change.
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
 THEN the client broker rejects locally (does NOT round-trip to Hermes)
  AND the card sees a Promise rejection with an "undeclared capability" error
  AND a widget.error event with phase="capability" is emitted to Hermes
WHY undeclared calls should never reach Hermes — that's the whole point of declaration. The agent learns of the failure via the error event and can update the card.
```

### 10.5 Source compile error

```
GIVEN the agent invokes render_widget with malformed JSX
 WHEN the client fails to compile
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
 THEN the client broker sends widget.api_call with capability="hermes.ask" and a fresh correlation_id
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
 THEN the session is unregistered
  AND the WidgetRegistry for that session is cleared
  AND the ApiCallRegistry has the in-flight correlation cancelled (or attempted)
  AND if the same session is later resumed via session.resume
  THEN the new in-flight agent has zero live widgets
  AND no card_ids from the previous session are valid
WHY consistent with how tool-progress cards and approvals behave. No special-casing for widgets.
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

## 11. Reference: minimal happy-path flow

1. **Client connects.** Sends `client.hello` with `widget.render` in capabilities.
2. **Server registers tools.** `render_widget`, `widget_update`, `widget_message`, `widget_dispose`, `list_widget_examples`, and `read_widget_example` join this session's tool registry.
3. **Agent prepares.** Agent calls `list_widget_examples()` and `read_widget_example("form-with-hermes-ask")` to get the primitives API surface and a reference pattern.
4. **User asks for something widget-y.** "Make me a draft form for the Q3 retro."
5. **Agent invokes `render_widget`.** Source is JSX with a form, declares `["hermes.ask", "notes.save"]`.
6. **Server emits `widget.render`.** With fresh `card_id`, source, and capabilities. Card registered in `WidgetRegistry`.
7. **Client compiles, mounts, emits `widget.mounted`.** Tool resolves; agent gets `card_id` string back.
8. **User edits the form, clicks "ask Hermes to fill in known fields".** Card calls `canvasAPI.hermes.ask("Fill in the known retro items from this quarter")`.
9. **Client broker sends `widget.api_call`** with `correlation_id: "corr_abc"`. Server acknowledges immediately with `{ accepted: true }`. Server starts `prompt.btw` in the background. Correlation registered in `ApiCallRegistry`.
10. **`prompt.btw` completes.** Server measures result (4 KiB — well under cap), emits `widget.api_response` with the answer.
11. **Card updates form fields with the answer.** User reviews, clicks save.
12. **Card calls `canvasAPI.notes.save({title, body, tags})`.** Client broker handles entirely client-side; no Hermes involvement. Note is created.
13. **Card calls `canvasAPI.card.close()`.** `widget.disposed` fires server-side (reason="agent_initiated"). Server clears the card from the registry; cancels any pending correlations (none, in this case).
14. **Agent's next turn sees the disposal** and calls `widget_dispose(card_id)` — which returns `{ disposed: False, already_disposed: True }`. Agent writes a confirmation reply.
