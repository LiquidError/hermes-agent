# Hermes Widget Render — Spec for Claude Code (Mac mini side)

> **For agentic workers:** this is a *spec*, not a plan. Hand it to the planning skill and run domain discovery before producing tasks. The wire-shape is committed; the Hermes-side architecture is open for the planner to refine.

**Audience:** Claude Code running on the Mac mini, with read-write access to the Hermes Agent source (`gateway/`, `tui_gateway/`, `hermes_cli/`).

**Goal:** Extend Hermes so the agent can render arbitrary React/JSX cards into the Tauri canvas. The agent decides *what* the card should look like (writes JSX); the Tauri host renders it inside a sandboxed iframe with a brokered capability surface. This spec covers the Mac-mini half: a new `widget.*` RPC namespace on `tui_gateway`, a `render_widget` tool the agent can invoke, lifecycle events flowing back, and the contract for `canvasAPI` calls that need round-trips to the agent.

**Out of scope:** the Tauri-side iframe sandbox, esbuild-wasm pipeline, capability broker, and iframe pool. Those live in a separate spec for the Tauri-side worker. This document defines only what the two sides agree on at the wire.

**Reference docs:**
- [tauri-client-contract.md](./tauri-client-contract.md) — base wire protocol this spec extends
- [desktop-app-adaptor.md](./desktop-app-adaptor.md) — server-side `DesktopAppAdapter` architecture
- [agent-canvas-design.md](./agent-canvas-design.md) — Tauri-side canvas where rendered cards live
- [agent-canvas-implementation-plan.md](./agent-canvas-implementation-plan.md) — existing card types this slots alongside

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
- a new agent-facing tool (`render_widget`) registered in the tool registry
- a per-session `WidgetRegistry` tracking live cards by id
- a handler for `widget.api_call` requests that originate from a mounted card calling `canvasAPI.hermes.*`

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

If `card_id` does not refer to a live card, the client responds with a `widget.error` event of type `not_found` and the agent should treat the card as gone. The agent MAY emit a fresh `widget.render` if it wants the card back.

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

The `message` shape is opaque to Hermes and the Tauri host — both sides treat it as JSON the card understands. It's the agent's job to keep the shape consistent with what the card it wrote knows how to handle.

|### 3.4 New event: `widget.dispose` (server → client)

|Agent wants to close the card. The Tauri host unmounts the iframe and emits `widget.disposed` back as confirmation.

|```json
|{
|  "type": "widget.dispose",
|  "session_id": "ab12cd34",
|  "payload": { "card_id": "wgt_8a3f9c", "reason": "task_complete" }
|}
|```

|`reason` is a free-form short string for observability. Suggested values: `task_complete`, `superseded`, `error`, `agent_initiated`.

|**Idempotent disposal.** Both the server (via `widget.dispose`) and the client (user closing a card) can trigger disposal of the same card simultaneously. The server's handler for `widget.dispose` MUST be idempotent — if the card is already disposed or in the process of being disposed (client has already sent `widget.disposed`), the server silently succeeds. Similarly, if the server receives `widget.disposed` from the client for a card_id it was about to dispose, any in-flight `widget.dispose` for that card_id is treated as a no-op. This prevents a double-free race.

### 3.5 New method: `widget.api_call` (client → server) — async pattern

**WHY async.** `hermes.ask` can take seconds or minutes (research, code gen, multi-step reasoning). A synchronous JSON-RPC request would block the WebSocket for the entire duration — no new `widget.render`, `widget.message`, or chat messages. The async pattern below ensures the WebSocket stays responsive.

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

New field `correlation_id`: a client-generated unique id for this specific call. Used to correlate the eventual response back to the iframe's Promise. Format: `corr_` + 6 lowercase hex chars.

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

The server then validates:
- The card is live in the named session.
- The capability was declared in the card's manifest.
- The session belongs to this connection (per §10 of the base contract).

If validation fails, the acknowledgment response carries an error instead (standard error codes). The card's Promise rejects on this error.

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

On error: the payload includes `{ "correlation_id": "...", "card_id": "...", "error": { "code": ..., "message": "..." } }` instead of `result`. The Tauri broker resolves or rejects the iframe's pending Promise accordingly.

**Card disposal during flight.** If the card is disposed before the `widget.api_response` arrives, the server-side handling continues (it's already started), but the response is silently dropped when the card is no longer in the registry. No zombie updates.

Capabilities that do *not* require server round-trip (notes.save, storage.*, os.*, card.resize, etc.) are handled entirely by the Tauri broker and never reach Hermes. They remain synchronous.

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

**Response size bounds.** The `answer` string is unbounded in theory but practically limited to 64 KiB in v1. If the agent's answer exceeds this, it should prefer sending structured data through `widget.message` instead, or break the answer into chunks the card can paginate through. Cards should not assume `answer` fits in a `<Text>` component — check `answer.length` and switch to lazy rendering (virtualized scroll, progressive reveal) for responses exceeding ~10 KiB.

**Streaming hint.** For very long answers, the agent should prefer sending incremental data via `widget.message` events (which the card receives through `canvasAPI.onMessage`) rather than waiting for a single large `hermes.ask` response. This gives the card the option to show a "loading..." pattern that fills in progressively. Full streaming (`hermes.stream`) is deferred to v2 but the namespace is reserved.

Errors use the standard error codes (§11 of base contract). Card sees a Promise rejection.

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

## 5. The `render_widget` agent tool

Hermes is an agent; it does work by calling tools. `render_widget` is a new tool registered in the standard tool registry, available in every session that has a desktop-app client connected with the `widget.render` capability declared in `client.hello`.

### 5.1 Tool signature

`render_widget` returns a plain `card_id` string. The agent uses companion stateless tools — `widget_update`, `widget_message`, `widget_dispose` — to interact with live cards. This avoids the problem of a Python object (`WidgetHandle`) that cannot survive message serialization across agent turns.

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
                See system prompt addendum for the import surface.
        capabilities: Names of canvasAPI methods the card will call.
                      Subset of: hermes.ask, notes.save, storage.get, storage.set,
                      storage.keys, card.resize, card.set_title, card.close,
                      os.notify, os.copy_clipboard.
        title: Human-readable card title shown in window chrome.
        initial_size: { "w": int, "h": int } in canvas units.

    Returns:
        str: The card_id string (e.g. "wgt_8a3f9c"). The agent stores this
        and passes it to widget_update, widget_message, or widget_dispose
        to interact with the live card later in the conversation.
    """
```

```python
@tool
def widget_update(
    card_id: str,
    source: str,
    capabilities: list[str] | None = None,
) -> None:
    """
    Replace the source of a live card. Card position is preserved;
    React state is reset (the iframe re-mounts).

    Args:
        card_id: The card_id returned by render_widget.
        source: New JSX source.
        capabilities: Optional updated capability allowlist.
                      If omitted, the original allowlist is preserved.
    """
```

```python
@tool
def widget_message(
    card_id: str,
    payload: dict,
) -> None:
    """
    Push a structured message into a live card.
    The card's React tree receives it via canvasAPI.onMessage(handler).

    Args:
        card_id: The card_id returned by render_widget.
        payload: Arbitrary JSON-serializable data the card understands.
    """
```

```python
@tool
def widget_dispose(
    card_id: str,
    reason: str = "task_complete",
) -> None:
    """
    Close a live card. The Tauri host unmounts the iframe.
    Idempotent — calling dispose on an already-disposed card is a no-op.

    Args:
        card_id: The card_id returned by render_widget.
        reason: Short observability string. Suggested: task_complete,
                superseded, error, agent_initiated.
    """
```

The `render_widget` tool emits `widget.render` and blocks until the matching `widget.mounted` or `widget.error` event arrives, with a 10-second timeout. On error, the tool raises a structured exception the agent can react to (typically: simplify the source and try again).

### 5.2 No `WidgetHandle` object

**Deliberately removed.** The v0 spec defined a `WidgetHandle` Python object returned from `render_widget` with `.update()`, `.message()`, `.dispose()` methods. This does not work in Hermes: tool results are serialized as JSON messages, and a Python object with methods cannot survive serialization. The agent is stateless — every turn is a fresh reconstruction. Instead:

- `render_widget` returns a plain `card_id` string.
- The agent stores `card_id` in the conversation (tool result messages persist naturally).
- On the next turn, the agent reads the `card_id` from its message history and calls `widget_update`, `widget_message`, or `widget_dispose`.
- The server maintains a session-scoped `WidgetRegistry` (§11.3) that maps `card_id` → metadata for validation.

This means the agent's mental model is "I called a tool that produced a card with id X, and I can pass X to other tools" — not "I have a handle object I can call methods on." The network plumbing remains invisible, but the programming model matches how all other Hermes tools work.

### 5.3 When the agent should call `render_widget`

A system prompt addendum (a new fragment in `gateway/system_prompts/`) instructs the agent on heuristics. To minimize token cost, the addendum is kept lean — it describes *when* to render helpers and gives the tool signatures, but does NOT inline the primitives types or examples:

- The user's task produces a *bounded artifact* with state — a draft, a form, a chart, a tracker, a comparison view — and the user will benefit from interacting with it rather than reading a wall of text.
- The information has a structure plain prose flattens — a small dataset, a comparison matrix, a plan with checkboxes, a configuration the user will tweak.
- The user explicitly asked to *see* something, *try* something, or *adjust* something.

The addendum also instructs the agent to *not* render a widget for short factual answers, conversational replies, or content that's purely textual narrative. Default to prose; reach for widgets when prose is the wrong shape.

**Primitives types and examples are fetched on demand** via two additional tools:

```python
@tool
def list_widget_examples() -> list[str]:
    """
    List available widget example files by name.
    Call this first to see what's available, then
    call read_widget_example(name) for the one you want.
    Returns: list of example names (e.g.
      ["static-info", "form-with-hermes-ask", ...])
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

    Returns: The full .tsx file content with JSDoc comments.
    """
```

These tools read from `gateway/system_prompts/widget_examples/`. The agent is expected to call them *just before* calling `render_widget`, not at every turn. This keeps the system prompt small (~30 lines of heuristics) and moves the ~500+ lines of types and examples to on-demand fetch, saving tokens in every turn that doesn't produce a widget.

The exact phrasing of the addendum is left for Claude Code to draft during planning. It should be reviewable as a separate diff so the project owner can iterate on the prompt independently of the wire code.

### 5.4 What the agent should write

The system prompt addendum describes the import surface (but not the full types — those are fetched on demand via `read_widget_example`):

- `React` and its hooks (`useState`, `useEffect`, `useRef`, `useMemo`, `useCallback`) are global.
- `canvasAPI` is global. Methods correspond to declared capabilities; calling an undeclared method raises.
- A primitives library is available at `import { Card, Field, Button, Text, Stack, Row, Chart } from 'canvas-primitives'`. (The Tauri-side bootstrap ships these. Their TypeScript declarations live in the contracts pipeline — see §11.6 — and are surfaced to the agent via the example files and the `read_widget_example` tool.)
- No other imports. No CDN. No fetch.

Examples of well-written cards live in `gateway/system_prompts/widget_examples/` as a directory of `.tsx` files. The agent discovers and reads them via `list_widget_examples()` and `read_widget_example(name)`. Claude Code should produce 4-6 starter examples alongside the addendum: a static info card, a form with hermes.ask, a list with storage persistence, a chart, an interactive editor with notes.save.

### 5.5 The agent's mental model for widget lifecycle

The addendum should also encode this:

- Widgets stay until disposed. Don't render a card for transient acknowledgements.
- If you produce a card and then realize it's wrong, prefer `widget_update()` over disposing and re-rendering — it preserves position and feels less jarring.
- Use `widget_message()` for incremental updates the card can absorb without remount (data refreshes, status pings).
- Dispose explicitly when the task that motivated the card is done — don't leak cards across topics.
- The card_id is returned as a plain string from `render_widget`. Store it in your message history; on the next turn, re-read it and pass to `widget_update`, `widget_message`, or `widget_dispose`.

---

## 6. Streaming and timing

### 6.1 v1: atomic source

The agent sends complete source in one `widget.render`. No partial streaming inside the JSX. The Tauri host shows a brief spinner during compile (typically 5-30ms with a warm iframe pool) and then mounts.

The `render_widget` tool resolves when `widget.mounted` arrives. While the tool is in-flight, the standard `tool.start` / `tool.complete` events fire as for any other tool — the existing `<ToolProgressCard>` shows "Rendering widget…" the whole time. This is the simplest behavior and the right v1.

### 6.2 v2 (deferred): chunked streaming

A future extension can let the agent stream JSX as it generates it, with the Tauri host showing the card materializing live. Reserving the namespace: `widget.render.chunk` events would carry partial source, with a final `widget.render.complete` to finalize. Out of scope for v1.

### 6.3 Timeouts

- `render_widget` tool: 10s default before raising. Configurable per-call. Tauri side should mount-or-error within 2s under normal conditions; 10s exists for cold-start cases.
- `hermes.ask` from a card: uses standard `prompt.btw` timeouts (no new constraint).

---

## 7. Versioning and feature negotiation

The base contract uses `client.hello` capability negotiation. This spec adds two new capabilities:

- Server-advertised: `widget.render` — server can render widgets in this session.
- Client-advertised: `widget.render` — client can mount widgets and run the iframe pool.

If either side is missing the capability, the `render_widget` tool is *not registered* in this session — the agent doesn't see it and can't call it. The agent gracefully falls back to text-only output.

This means the Tauri team can ship the iframe sandbox progressively without breaking older Hermes builds, and Hermes can ship `render_widget` to non-Tauri front-ends (which simply won't render anything) without errors.

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
| `5101` | Tauri client refused to mount (compile or validate failure). Carries inner error in payload. |
| `5102` | `render_widget` tool timed out waiting for `widget.mounted`. |

---

## 9. Per-connection isolation

Widgets are session-scoped, and sessions are connection-scoped per §10 of the base contract. Practical implications:

- A widget rendered from session A on connection X is not visible to session B or connection Y.
- `session.resume` (which builds a fresh agent) does *not* re-attach to widgets from the previous in-flight agent. The widgets are gone with that connection.
- `session.branch` does not duplicate live widgets; the new branch starts with no cards.

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

### 10.2 Capability negotiation absent

```
GIVEN a connected client that did NOT advertise widget.render
 WHEN a session is created
 THEN render_widget is NOT in the agent's tool registry for that session
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
  AND the render_widget tool raises a structured exception
  AND the agent's reasoning loop can read the error and retry with fixed source
WHY compile errors should be self-correcting in one or two retries; the error has to be specific enough for the agent to act on it.
```

### 10.6 Card update preserves position, resets state

```
GIVEN a mounted card at canvas position (x, y) with internal React state
 WHEN the agent emits widget.update with new source
 THEN the iframe re-mounts with new source
  AND the card's canvas position is unchanged
  AND the card's React state is reset to initial
  AND any open canvasAPI.hermes.ask promises from the old version are abandoned
WHY position is the user's; state is the card's. Updating means "new version of the same thing" — moving the card would feel jarring; preserving stale state would feel buggy.
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
WHY cards asking questions shouldn't clutter the main thread or block the connection. The async pattern ensures long-running hermes.ask calls don't freeze the session.

### 10.8 Disposal cleanup and race condition

```
GIVEN a mounted card with a pending hermes.ask call in flight
 WHEN the user closes the card
 THEN widget.disposed fires from client to server with reason="user_closed"
  AND any in-flight widget.dispose from the server for the same card_id is treated as a no-op
  AND the pending hermes.ask, if it returns, is silently dropped (card no longer in registry)
  AND the agent's next widget_dispose call for this card_id is a no-op (idempotent)
WHY closing a card should never produce phantom updates or zombie state. A disposed card is a closed file. Both actors can trigger disposal; the system handles the race without error.
```

```
GIVEN a disposed card that the agent knows about
 WHEN the agent calls widget_dispose(card_id="wgt_8a3f9c")
 THEN no widget.dispose event is emitted (the card is already gone)
  AND widget_dispose silently returns None
WHY agent-side dispose is idempotent. If the user already closed the card, the agent's cleanup call should not fail or produce an error event.

### 10.9 Session disconnect

```
GIVEN a session with three live widgets
 WHEN the WebSocket disconnects
 THEN per base contract §8.1, the session is unregistered
  AND the WidgetRegistry for that session is cleared
  AND if the same session is later resumed via session.resume
  THEN the new in-flight agent has zero live widgets
  AND any widget handles the previous agent held have been garbage-collected
WHY consistent with how ToolProgressCard and approvals behave today. No special-casing.
```

### 10.10 Source size limit

```
GIVEN the agent invokes render_widget with source > 256 KiB
 WHEN tui_gateway validates the payload
 THEN the tool raises with error code 4102
  AND no widget.render event is emitted to the client
WHY oversized payloads are usually a sign the agent inlined data that should have come through hermes.ask. The error nudges it toward the right pattern.
```

---

## 11. Open considerations for the planner

Decisions Claude Code should surface and propose during domain discovery, not silently resolve.

### 11.1 Where the system prompt addendum lives

The current `gateway/system_prompts/` structure should be inspected. The widget-author guidance is non-trivial (probably 200-400 lines including examples). Should it be a single file, a directory of fragments composed conditionally, or a skill? The right answer depends on conventions already in use.

### 11.2 The starter `canvas-primitives` library

The Tauri-side bootstrap ships `<Card>`, `<Field>`, `<Button>`, etc. The agent needs to know their props. A `canvas-primitives.d.ts` should be authored *somewhere* and fed into the agent's context. Open question: does it live in the Hermes repo (and the Tauri side mirrors it) or in the Tauri repo (and Hermes reads it on startup from a known path / config)? Both have arguments. Pick one explicitly.

### 11.3 How `WidgetHandle` survives across the agent's reasoning loop

In Hermes, tool results are persisted as messages. A `WidgetHandle` is a Python object that needs to call back into `tui_gateway` later. Claude Code should specify how the handle is reconstructed across turns — does the agent see only the `card_id` and re-derive a handle, or is the handle itself stored in some kind of session-scoped slot? Has implications for what the agent can reason about.

### 11.4 Persistence

v1: widgets do not survive `session.resume`. This matches existing ephemeral surfaces (tool progress, approvals). If we ever want widgets to survive — e.g., a long-lived dashboard the user wants to come back to tomorrow — the path is a new `widget.persist` capability that snapshots `(source, capabilities, last storage state)` to `state.db` and replays on resume. **Not v1**; mentioned so the namespace is reserved.

### 11.5 Client-emitted events as a contract addition

The base contract today only has server → client events. Adding client → server events (`widget.mounted`, etc.) is a small but real extension. Claude Code should verify the `tui_gateway` dispatcher can handle them cleanly and propose whether they need their own envelope shape or can re-use the existing `event` envelope with an originating-side flag.

### 11.6 Where the canvas-primitives types ship to the agent

The agent needs the `.d.ts` content in its context to write valid JSX. Three options:
- Inlined into the system prompt addendum (always present, costs tokens every turn)
- Fetched on demand via a tool (`get_canvas_primitives_docs`) when the agent is about to render
- Stored as a skill that auto-loads when render_widget is registered

Each has tradeoffs in token cost vs. discoverability vs. complexity. Pick one.

### 11.7 Approval gating

v1 mounts cards eagerly — no per-card approval. But `hermes.ask` and `notes.save` are real actions; should they get the standard `approval.request` flow? Suggest: gated by an existing tool-approval policy (if `notes.save` is a registered tool, its approval policy applies; if `hermes.ask` is a btw, btws are usually unapproved). Verify against current policy and propose.

### 11.8 Observability

`tool.start` / `tool.complete` exist for `render_widget`. Should there also be a `widget.api_call` log entry visible to the user? Helpful for debugging cards. Potentially noisy. Suggest: behind a "show widget activity" preference, off by default.

---

## 12. But if… — alternative paths to consider

The shape above is a recommendation. Reasonable alternatives the planner should weigh and reject (or adopt) explicitly:

**But if the agent shouldn't be writing JSX at all,** the alternative is a *descriptor-driven* model: the agent emits a JSON tree of typed primitives + RPC bindings, and the Tauri host renders that. Safer (no code execution at all), more bounded (the agent can only build what the descriptor schema allows), but caps the ceiling on what the agent can invent. Possibly the right v1, with code-as-payload as v2. Worth a serious comparison; this spec assumes the user has already chosen code-as-payload.

**But if streaming render is a hard requirement,** v1 should support `widget.render.chunk` from day one rather than deferring. The complication is that partial JSX is invalid until complete, so the host needs a buffer-then-compile strategy and a placeholder-during-buffer UX. Adds maybe 30% to the v1 scope. Worth it if "the card materializing as the agent thinks" is a defining feature; not worth it if it's a nice-to-have.

**But if widgets should persist across sessions,** §11.4's snapshot model needs to be v1, not v2. The complication is that storage state can get arbitrarily large and the source can drift relative to the storage shape. Punting is the safer call unless persistence is a known user need.

**But if the planner sees a cleaner factoring that splits `render_widget` into `widget.create` + `widget.update_source` + …,** that's allowed — the wire shape is the contract, the agent-facing tool surface is open for the planner to refine.

---

## 13. Acceptance criteria

The Hermes-side work is done when:

1. `tui_gateway/server.py` registers all `widget.*` events and methods per §3, including `widget.api_response` for async delivery.
2. `gateway/platforms/desktop_app.py` surfaces them to the desktop adapter without modification (they should ride the existing dispatcher).
3. The `render_widget`, `widget_update`, `widget_message`, `widget_dispose`, `list_widget_examples`, and `read_widget_example` tools are registered conditionally on the client advertising `widget.render` capability.
4. A `WidgetRegistry` per-session tracks live cards and is wired to disconnect/resume cleanup. Disposal is idempotent — race conditions between server- and client-initiated close are handled gracefully.
5. A system prompt addendum exists (lean — heuristics only, ~30 lines) and is loaded into the agent's context when `render_widget` is available.
6. At least 4 starter widget examples exist in `gateway/system_prompts/widget_examples/`, discoverable via `list_widget_examples()`.
7. `hermes.ask` round-trips use the async pattern: `widget.api_call` → immediate ack → `widget.api_response` on completion. The WebSocket is not blocked during processing.
8. All test scenarios in §10 pass against a mock client.
9. The base wire contract (`tauri-client-contract.md`) gets a `§N. Widget render` section appended that mirrors §3 here, with the canonical version living in the contract doc going forward.

The Tauri side acceptance lives in the separate Tauri-side spec and includes the iframe sandbox, esbuild-wasm pipeline, capability broker, iframe pool, and `<AgentWidgetCard>` integration into the existing canvas.

---

## 14. Reference: minimal happy-path flow

1. **Client connects.** Tauri sends `client.hello` with `widget.render` in capabilities.
2. **Server registers tools.** `render_widget`, `widget_update`, `widget_message`, `widget_dispose`, `list_widget_examples`, and `read_widget_example` join this session's tool registry.
3. **Agent prepares.** Agent calls `list_widget_examples()` then `read_widget_example("form-with-hermes-ask")` to get the primitives API surface and a reference pattern.
4. **User asks for something widget-y.** "Make me a draft form for the Q3 retro."
5. **Agent invokes `render_widget`.** Source is JSX with a form, declares `["hermes.ask", "notes.save"]`.
6. **Server emits `widget.render`.** With fresh `card_id`, source, and capabilities.
7. **Client compiles, mounts, emits `widget.mounted`.** Tool resolves, agent gets `card_id` string.
8. **User edits the form, clicks "ask Hermes to fill in known fields".** Card calls `canvasAPI.hermes.ask("Fill in the known retro items from this quarter")`.
9. **Tauri broker sends `widget.api_call`** with `correlation_id`. Server acknowledges immediately. Server runs btw; when done, emits `widget.api_response`.
10. **Card updates form fields with the answer.** User reviews, clicks save.
11. **Card calls `canvasAPI.notes.save({title, body, tags})`.** Tauri broker handles entirely client-side; no Hermes involvement. Note is created.
12. **Card calls `canvasAPI.card.close()`.** `widget.disposed` fires server-side. The server removes the card from the registry.
13. **Agent's next turn sees the disposal** and calls `widget_dispose(card_id)` — which is a no-op (card already gone). Agent writes a confirmation reply.
