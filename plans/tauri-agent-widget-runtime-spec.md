# Tauri Agent Widget Runtime — Spec for Claude Code (Tauri side) — v2

> **For agentic workers:** this is a *spec*, not a plan. Hand it to the planning skill and run domain discovery before producing tasks. The wire shape is committed and shared with the Hermes side; the runtime architecture and primitives library are the real engineering work this spec describes.

**v2 changelog (vs v1).** Tracking changes to keep both sides in lockstep. Hermes-side v2 introduced four refinements layered on top of its five gap fixes; this Tauri-side v2 mirrors them as the *consumer* of every shape:

- **Cancellation (new §6.5, §6.6, §16.15):** The broker now maintains an `ApiCallRegistry` keyed by `correlation_id` and emits `widget.api_cancel` to the server when an in-flight call's card is disposed or updated. Iframe-side Promises are rejected with a structured cancellation error so cards see clean rejections, not pending-forever leaks.
- **Response size cap, hard (updated §8.1, §16.16):** The Hermes-side spec v2 enforces a 32 KiB hard cap on `widget.api_response` payloads with error code `4106`. The Tauri broker surfaces 4106 errors directly to the card's Promise as a structured rejection — no truncation attempts, no silent fallbacks.
- **Idempotent disposal — both sides (clarified §3 table, §6.7):** v1 specified server-side idempotency. v2 makes client-side idempotency explicit too: if `widget.dispose` arrives from the server for a card the client has already disposed, the client silently no-ops without re-emitting `widget.disposed`.
- **`widget.update` cancels in-flight calls (updated §16.6):** When a card's source is replaced, all pending `widget.api_call` correlations for that card are cancelled with reason `card_updated` — so the new mount doesn't receive stale data destined for the old one.
- **Resolved §11.5 / §15.x:** The contracts-pipeline question (Tauri as source of truth for types, generated to a contracts directory) is now closed by the corresponding fix on the Hermes side. Moved to new §20 "Resolved decisions."
- **Minor:** wire contract table fixed (column count), §16.14 typo fixed, postMessage protocol type updated to reflect cancellation shape.

---

**Audience:** Claude Code with read-write access to the Tauri canvas repo (where `agent-canvas-design.md`, `agent-canvas-implementation-plan.md`, and `tauri-client-contract.md` live).

**Goal:** Build the client-side runtime that consumes `widget.render` events from Hermes and renders agent-authored React/JSX cards on the canvas. The runtime mounts each card in a sandboxed iframe, brokers a capped capability surface (`canvasAPI`), pools iframes for fast mount, and ships a generous primitives library the agent can compose against. The runtime is the *renderer* and the *gatekeeper*; Hermes is the *author*.

**Companion doc:** [hermes-widget-render-spec.md](./hermes-widget-render-spec.md) — the Mac-mini side. The two specs share §3 of the wire contract verbatim; the Tauri side additionally owns the runtime, the primitives library, and the type-contract generation pipeline.

**Reference docs:**
- [agent-canvas-design.md](./agent-canvas-design.md) — existing canvas this slots into
- [agent-canvas-implementation-plan.md](./agent-canvas-implementation-plan.md) — existing card types, conventions, file layout
- [tauri-client-contract.md](./tauri-client-contract.md) — base wire protocol; this spec extends §4 with the `widget.*` namespace
- [hermes-widget-render-spec.md](./hermes-widget-render-spec.md) — companion Hermes-side spec (v2)

**Conventions inherited from the existing implementation plan:**
- Tests run via `bun run test` (NOT `bun test`). Rust via `cd src-tauri && cargo test`.
- TS path alias: `@/` = `src/`.
- Coverage threshold 80% for new TS modules.
- Each task ends with a commit. Commit subject is what changed; body explains why if non-obvious.
- Co-author trailer: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>` on every commit.

---

## 1. Domain model

Five nouns, kept distinct on purpose:

**Source.** A string of JSX/JavaScript the Hermes agent emitted. The runtime never trusts it.

**Bootstrap.** A small HTML document the runtime ships, loaded into every iframe. It contains React, esbuild-wasm, the primitives library, the postMessage client, and an error boundary. The agent never sees or modifies the bootstrap — it's the trusted container the untrusted source runs inside.

**Card.** A live mounted instance: an iframe + a position + a size + a state machine + a per-card capability allowlist. The user sees a card; the runtime manages it.

**Capability.** A named permission the card declared. The broker enforces. v1 surface lives in §8.

**Primitive.** A typed React component the agent can compose: `<Card>`, `<Chart>`, `<DnDList>`, `<RichTextEditor>`, etc. Primitives ship inside the bootstrap; the agent's source `import`s them. They are how the runtime gives the agent power *without* giving the agent freedom.

> **Mental model.** Restaurant kitchen brigade. The agent (chef de cuisine) writes a dish on a ticket. The bootstrap (the kitchen) has all the stations and tools. The capability broker (the expediter) decides which orders leave the kitchen and reach the dining room. The user (the diner) eats the result. The agent never enters the kitchen; the diner never enters the kitchen; the kitchen is the trust boundary.

---

## 2. Architecture summary

```
Hermes agent (Mac mini)
   │ widget.render { card_id, source, capabilities, ... }
   │ ↓ over WSS
   ▼
Tauri host webview (TRUSTED)
   │
   │  ┌─ RPC receiver ──┐
   │  │ validate payload │
   │  └────────┬─────────┘
   │           ▼
   │  ┌─ Iframe pool ───────────────────┐
   │  │ warm: [iframe, iframe, iframe]  │
   │  │ leases one to <AgentWidgetCard> │
   │  └────────┬────────────────────────┘
   │           │ postMessage (host → iframe)
   │           ▼   { kind: "init", source, capabilities }
   │  ┌───────────────────────── trust boundary ─────────────────────────┐
   │  │ Sandboxed iframe (UNTRUSTED)                                     │
   │  │  ┌─ Bootstrap ──────────────────────────────────────────┐         │
   │  │  │  React, esbuild-wasm, primitives lib,                │         │
   │  │  │  capabilityAPI proxy, error boundary                 │         │
   │  │  └────────────┬─────────────────────────────────────────┘         │
   │  │               ▼ compile JSX → ESM → mount component               │
   │  │       <Card> / <Chart> / etc. + agent-authored JSX                │
   │  │               ▲                                                   │
   │  │               │ canvasAPI.notes.save({...}) / hermes.ask(...)     │
   │  │               │ → posts { id, capability, args } to host          │
   │  └───────────────┼───────────────────────────────────────────────────┘
   │                  │ postMessage (iframe → host)
   │           ┌──────▼─────────────────────────┐
   │           │ Capability broker              │
   │           │  - allowlist check             │
   │           │  - capability dispatcher       │
   │           │  - ApiCallRegistry             │ ← new in v2: tracks
   │           │    (correlation_id → Promise)  │   in-flight async calls
   │           │  - cancellation on disposal    │   for cancellation
   │           └──────┬─────────────────────────┘
   │                  │
   │       ┌──────────┴──────────┐
   │       ▼                     ▼
   │  Local services        Hermes RPC
   │  (notes, storage,      widget.api_call
   │   os bridge, kv)       widget.api_cancel
   ▼
Existing canvas (FloatingPanel, useSliceGesture, ZoneOverlay, ...)
   - <AgentWidgetCard> is a new card type that wraps the iframe lease
   - Lives alongside <ToolProgressCard>, <ArtifactCard>, <SubagentThreadCard>
```

Three new TypeScript subsystems:

1. `agent-widgets/runtime/` — iframe pool, broker, ApiCallRegistry, postMessage protocol, lifecycle.
2. `agent-widgets/primitives/` — the generous primitives library (§9).
3. `agent-widgets/contracts/` — the type-generation pipeline that publishes `.d.ts` for the Hermes side.

Plus minor changes:
- A new card type `<AgentWidgetCard>` in `components/agent-canvas/side-cards/`.
- A new dispatcher entry in `services/hermesService.ts` for `widget.*` events and the `widget.api_call` / `widget.api_cancel` request methods.
- A new state slice (Zustand) for the live-cards registry.

---

## 3. Wire contract — pointer

The wire-shape is fully specified in §3 of [hermes-widget-render-spec.md](./hermes-widget-render-spec.md). This Tauri-side spec implements the *consumer* of every event documented there:

| Event / method | Direction | Tauri-side responsibility |
|---|---|---|
| `widget.render` | server → client | Validate, lease iframe, mount, emit `widget.mounted` or `widget.error`. |
| `widget.update` | server → client | Find card by id, replace source, re-mount, preserve canvas position. **Cancel any in-flight `widget.api_call` correlations** for the card with reason `card_updated`. |
| `widget.message` | server → client | Forward payload via postMessage to the iframe; the card's `canvasAPI.onMessage` handler receives it. |
| `widget.dispose` | server → client | Tear down the iframe, return it to the pool, emit `widget.disposed`. **Idempotent both sides:** if the card is already being disposed (because `widget.disposed` was already sent), silently no-op without re-emitting. |
| `widget.api_call` | client → server | Emitted when a card calls `canvasAPI.hermes.*`. Server acknowledges immediately, processes async, delivers result via `widget.api_response`. |
| `widget.api_response` | server → client | Delivers the result of an async `widget.api_call`. Correlated by `correlation_id`. **May carry error code `4106` if response exceeded the 32 KiB cap** — surface as a structured rejection to the card. |
| `widget.api_cancel` | client → server | **New in v2.** Emitted when the broker abandons an in-flight `widget.api_call` correlation (card disposed mid-flight, source updated, etc.). Server attempts to cancel the underlying work; no response is delivered. |
| `widget.api_cancel` | server → client | Rare. Server-initiated cancellation, e.g. session ending. Broker drops the correlation and rejects the iframe's pending Promise. |
| `widget.mounted` | client → server | Emitted after successful compile + first render. |
| `widget.error` | client → server | Emitted on any failure with `phase` field per spec. |
| `widget.disposed` | client → server | Emitted on teardown, regardless of cause. Once sent, any incoming `widget.dispose` for the same card_id is a no-op. |

Error codes `4101–4107` and `5101–5103` per the Hermes-side spec v2 §8 apply on both sides. The Tauri runtime maps internal errors to these codes when emitting `widget.error`, and surfaces incoming codes (especially `4106`) as structured Promise rejections to cards.

---

## 4. The bootstrap iframe

The bootstrap is the runtime's single trusted asset shipped *into* the untrusted iframe. It is small, audited, and ships verbatim with the Tauri app.

### 4.1 What the bootstrap contains

Bundled into a single HTML document (no network fetches at runtime):

- **React 19 + ReactDOM** — UMD or ESM, whichever the build pipeline prefers.
- **esbuild-wasm** — for compiling agent JSX → ESM at runtime. ~2 MB. Loaded once per iframe; cached in the pool.
- **Primitives library** — eager bundle: `Card`, `Field`, `Button`, `Text`, `Stack`, `Row`, `Spacer`, `Divider`, `Badge`, `Tag`, plus form primitives (§9). ~80 KB.
- **Heavy primitives loader** — lazy-loaded on first use: `Chart` (recharts), `Table` (TanStack Table), `DnDList` (dnd-kit), `RichTextEditor` (tiptap), `CodeEditor` (CodeMirror 6). Stored as separate chunks; loaded via dynamic import handled by the bootstrap (not by the agent's source).
- **canvasAPI proxy** — the `window.canvasAPI` object with one method per capability. Each method posts a structured message to the host and returns a Promise.
- **Error boundary** — wraps the agent's mounted component. Catches render-phase and lifecycle errors, posts `widget.error` to the host, displays a fallback inside the iframe.
- **Bootstrap controller** — handles the `init` message from host, runs the compile pipeline, mounts the component, posts `widget.mounted`.

### 4.2 Bootstrap loading sequence

1. Host creates `<iframe sandbox="allow-scripts">` with `srcdoc` set to the bootstrap HTML.
2. Iframe parses, bootstrap script runs, esbuild-wasm initializes (~50ms cold; reused thereafter via pool warmup).
3. Bootstrap posts `bootstrap.ready` to parent.
4. Host posts `init` with `{ source, capabilities, card_id, theme_tokens, initial_message? }`.
5. Bootstrap compiles source, mounts component into root, posts `widget.mounted`.

### 4.3 Sandbox attributes

```html
<iframe sandbox="allow-scripts"></iframe>
```

Only `allow-scripts`. No `allow-same-origin` (so the iframe is a null origin and can't read parent), no `allow-forms`, no `allow-popups`, no `allow-modals`, no `allow-top-navigation`. CSP inside the iframe additionally restricts: no `connect-src`, no `img-src` outside `data:`, no `font-src` outside `data:`, no external `script-src`. The iframe is fully offline once loaded.

### 4.4 Theme propagation

The agent's cards must visually match the canvas. Solution: the host injects design tokens as CSS variables in the bootstrap on init.

```ts
host.postMessage({
  kind: "init",
  source,
  capabilities,
  card_id,
  theme_tokens: {
    "--color-text-primary": "#1a1a1a",
    "--color-background-primary": "#ffffff",
    "--font-sans": "Inter, sans-serif",
    // ... full set
  }
})
```

The bootstrap applies them on the iframe's root element. Primitives reference the variables, so they auto-theme without the agent doing any work. Theme changes (light → dark) post a `theme.update` message; the bootstrap re-applies.

---

## 5. The esbuild-wasm pipeline

### 5.1 Why esbuild-wasm

The agent writes JSX. The browser cannot execute JSX. Options considered:

- `React.createElement` instead of JSX — agent writes uglier code, smaller bootstrap. Rejected: agent fluency in JSX is a real factor in card quality.
- Server-side compile in Hermes — moves the dependency to Python land. Rejected: ties Hermes versions to compile-tool versions; cross-machine debugging worse.
- Sucrase/SWC-wasm — smaller than esbuild but less robust on edge cases. Reasonable v2 if size becomes a problem.

esbuild-wasm is the v1 pick. It compiles JSX, supports TS syntax (so the agent can write TS in source if it wants), and is a single ~2MB file.

### 5.2 Compile contract

The compiler runs *inside* the iframe — not in the host. This is deliberate: a compile-time bug in the agent's source can't crash the host. Compile pipeline:

1. Receive source string.
2. Wrap in a synthetic module: `import * as primitives from 'canvas-primitives'; ${source}; export { default } from './source';`
3. Resolve imports via a virtual filesystem in esbuild-wasm: `canvas-primitives` → the primitives bundle, `react` → the React UMD already loaded, anything else → resolution error (caught and surfaced as `widget.error` with `phase: "compile"`, `kind: "unknown_import"`).
4. esbuild compiles to ESM string.
5. Wrap in a Blob, get a Blob URL, dynamic-`import()`.
6. Module loaded; `default` export is the component.
7. Mount with `ReactDOM.createRoot(rootEl).render(<ErrorBoundary><Component /></ErrorBoundary>)`.
8. Post `widget.mounted` with compile timing for observability.

### 5.3 Compile budget

Cold first compile: 30-100ms. Warm (esbuild-wasm already initialized in pool): 5-30ms. The first card the user ever renders may take 100-200ms total (cold pool); subsequent cards in a session are sub-50ms. These are guidelines, not hard SLAs.

### 5.4 Heavy primitive lazy loading

Recharts, tiptap, CodeMirror, dnd-kit are large. They are NOT in the eager primitives bundle. Strategy:

- Each heavy primitive ships as a separate chunk inside the bootstrap.
- The agent's source `import { Chart } from 'canvas-primitives'` — the import resolves to a *proxy* component.
- On first render of the proxy, the bootstrap dynamic-imports the chunk, swaps in the real component, re-renders.
- User sees a brief skeleton (managed by the proxy) during chunk load.

This means the eager bundle stays under ~150 KB while still letting the agent compose against the full primitives surface.

---

## 6. The capability broker

The broker is the gatekeeper between the iframe and everything else (local services, Hermes RPC, OS bridge). It runs in the *host* (trusted) context, not in the iframe.

### 6.1 postMessage protocol

All iframe ↔ host messages use a discriminated-union shape:

```ts
type IframeToHost =
  | { kind: "bootstrap.ready" }
  | { kind: "widget.mounted"; compile_ms: number; compiled_size: number }
  | { kind: "widget.error"; phase: ErrorPhase; kind_: string; message: string; stack?: string }
  | { kind: "widget.disposed"; reason: string }
  | { kind: "api.call"; id: string; capability: string; args: unknown }

type HostToIframe =
  | { kind: "init"; source: string; capabilities: string[]; card_id: string; theme_tokens: Record<string, string>; initial_message?: unknown }
  | { kind: "source.update"; source: string; capabilities: string[] }
  | { kind: "message.push"; payload: unknown }
  | { kind: "api.result"; id: string; result?: unknown; error?: { code: number; message: string } }
  | { kind: "theme.update"; theme_tokens: Record<string, string> }
  | { kind: "dispose"; reason: string }
  | { kind: "card.disposed.ack" }  // host confirms iframe has been removed from registry

type HostFromHermes =
  | { kind: "widget.api_response"; correlation_id: string; card_id: string; result?: unknown; error?: { code: number; message: string } }
  | { kind: "widget.api_cancel"; correlation_id: string; card_id: string; reason: string }

type HostToHermes =
  | { kind: "widget.api_call"; correlation_id: string; card_id: string; capability: string; args: unknown }
  | { kind: "widget.api_cancel"; correlation_id: string; card_id: string; reason: string }
```

Every message is JSON-serializable. No transferable objects in v1 (keeps the protocol simple). Origin and source checks: every incoming message is verified against the mounted iframe's `contentWindow` reference; messages from any other source are dropped with a console warning.

**Note on iframe-to-host cancellation.** Cards do not get an explicit cancellation API in v1 (no `canvasAPI.hermes.ask().cancel()`). When a card is disposed or its source replaced, *all* in-flight calls for that card are cancelled by the broker — the card itself doesn't trigger cancellation. v2 may add per-call cancellation if the use case emerges.

### 6.2 The broker's job

For each `api.call` from an iframe:

1. Find the card's allowlist by `card_id` (broker keeps a map).
2. If `capability` is not in the allowlist → respond with error code `4104`.
3. Look up the capability in the dispatch table.
4. For local capabilities: run the dispatcher, await the result, post `api.result` to the iframe.
5. For Hermes round-trip capabilities (`hermes.*`): generate a fresh `correlation_id`, register the pending Promise in `ApiCallRegistry`, send `widget.api_call` to Hermes, await ack; later, when `widget.api_response` arrives with the matching `correlation_id`, resolve or reject the Promise and post `api.result` to the iframe.

The dispatch table maps each capability to an implementation:

```ts
const dispatchers: Record<string, Dispatcher> = {
  "hermes.ask":   ({ args, card_id, session_id }) => apiCallRegistry.invoke(session_id, card_id, "hermes.ask", args),
  "notes.save":   ({ args }) => noteService.save(args),
  "storage.get":  ({ args, card_id }) => storage.get(card_id, args.key),
  "storage.set":  ({ args, card_id }) => storage.set(card_id, args.key, args.value),
  // ... etc
}
```

Capabilities that round-trip to Hermes (`hermes.*`) go through `ApiCallRegistry.invoke` (§6.5), which handles the async pattern, correlation tracking, and cancellation.

### 6.3 Argument validation

Every dispatcher MUST validate its arguments. This is a trust boundary — bad args from a malicious card cannot crash the host. Use Zod schemas (or equivalent) per capability; reject mismatches with code `4002`.

### 6.4 Concurrency

Cards can issue capability calls in parallel. The broker correlates by message `id` (for local calls) or `correlation_id` (for Hermes round-trips). There's no global queue — the broker is fire-and-forget per call, with response routing back to the iframe by id. Long-running capabilities (`hermes.ask`) MUST NOT block other calls — they use the async pattern in §3.5 of the Hermes-side spec, where the server acknowledges immediately and delivers the result later via `widget.api_response`.

### 6.5 ApiCallRegistry — tracking in-flight Hermes round-trips

The `ApiCallRegistry` is a per-broker singleton (or per-session, depending on factoring) that owns the lifecycle of every pending `widget.api_call` correlation.

```ts
type PendingCall = {
  correlation_id: string
  card_id: string
  session_id: string
  capability: string
  iframe_message_id: string  // the original `api.call` id from the iframe
  promise: { resolve: (v: unknown) => void; reject: (e: unknown) => void }
  started_at: number
}

class ApiCallRegistry {
  // invoke: send widget.api_call, register the pending Promise, await widget.api_response
  invoke(session_id: string, card_id: string, capability: string, args: unknown): Promise<unknown>

  // resolve: called when widget.api_response arrives with matching correlation_id
  resolve(correlation_id: string, result: unknown): void

  // reject: called when widget.api_response arrives with an error payload
  reject(correlation_id: string, error: { code: number; message: string }): void

  // cancelByCard: called when a card is disposed or updated; cancels all its calls
  cancelByCard(card_id: string, reason: string): void

  // cancelBySession: called on session disconnect
  cancelBySession(session_id: string): void

  // cancelByCorrelation: called when the server sends widget.api_cancel
  cancelByCorrelation(correlation_id: string, reason: string): void
}
```

Mechanics of `cancelByCard`:

1. Look up all `PendingCall` entries with matching `card_id`.
2. For each: emit `widget.api_cancel` to Hermes with `{ correlation_id, card_id, reason }`.
3. Reject each Promise with `{ code: "cancelled", message: "Card was {disposed|updated}", reason }`.
4. Post `api.result` to the iframe with the rejection (only meaningful if the iframe is still mounted — for `card_disposed`, it isn't, and the message is simply not delivered).
5. Remove all matched entries from the registry.

Mechanics of `resolve` / `reject` when the response carries error code `4106` (response too large):
- Treat as a standard rejection. The iframe Promise rejects with `{ code: 4106, message: "Response exceeded 32 KiB cap" }` and the card's try/catch sees a structured error.
- The card SHOULD display this as a "response too large — try a more specific question" or similar UX, depending on what the card does. The broker doesn't try to truncate or fall back; that's not its job.

### 6.6 Cancellation flow — three trigger paths

**Path A: Card disposed mid-flight (most common).**

```
[user clicks X on card] OR [server sends widget.dispose]
      ↓
<AgentWidgetCard> unmounts
      ↓
broker.disposeCard(card_id, reason)
      ↓
apiCallRegistry.cancelByCard(card_id, reason="card_disposed")
      ↓
emit widget.api_cancel for each pending correlation_id
reject each pending Promise (no longer reachable; iframe is gone)
remove entries from registry
      ↓
emit widget.disposed to Hermes (if not already)
return iframe to pool
```

**Path B: Card source updated mid-flight.**

```
widget.update arrives
      ↓
broker.updateCard(card_id, source, capabilities)
      ↓
apiCallRegistry.cancelByCard(card_id, reason="card_updated")
      ↓
emit widget.api_cancel for each pending correlation_id
reject each pending Promise (iframe will re-mount; old promises dead)
remove entries from registry
      ↓
post source.update to iframe (re-mount)
```

**Path C: Server-initiated cancellation.**

```
widget.api_cancel arrives from server (e.g. session ending)
      ↓
apiCallRegistry.cancelByCorrelation(correlation_id, reason)
      ↓
reject the pending Promise
post api.result with rejection to the iframe (if still mounted)
remove entry from registry
```

**No acknowledgment of cancellation is required.** Cancellation is fire-and-forget; both sides converge on "the correlation no longer exists in any registry." If a `widget.api_response` for a cancelled correlation arrives anyway (race: server finished work just before processing the cancel), the broker drops it silently — registry lookup fails, and there's nowhere to deliver it.

### 6.7 Idempotent disposal — both sides

The Tauri side defends against the same race the Hermes side does:

- **`widget.dispose` arrives for a card already torn down.** Either the user closed it 50ms ago and `widget.disposed` was already emitted, or another `widget.dispose` already triggered teardown. The broker silently no-ops: does NOT re-emit `widget.disposed`, does NOT throw, does NOT touch the iframe pool.
- **`widget.dispose` and user-close happen near-simultaneously.** Whichever path enters teardown first wins. The second is detected by registry lookup (entry already removed) and silently no-ops.
- **`widget.disposed` from the client and `widget.dispose` from the server cross on the wire.** Both sides treat their own action as canonical. The client doesn't care that a `widget.dispose` arrives after it has already disposed; the server doesn't care that a `widget.disposed` arrives for a card it was about to dispose. They converge.

The lookup primitive: `liveCardsRegistry.has(card_id)`. If it's false, every disposal-related event for that `card_id` is a no-op until the next `widget.render`.

---

## 7. The iframe pool

Pool exists for two reasons: cold-mount latency would be ~150ms without it, and esbuild-wasm initialization is the dominant cost.

### 7.1 Pool sizing

- **Warm pool target:** 2 ready iframes at all times (configurable).
- **Burst capacity:** 8 iframes (one warm pool slot + transient creation).
- **Idle timeout:** an iframe in the warm pool that hasn't been used for 10 minutes is reclaimed.

### 7.2 Lifecycle

```
[create] → [bootstrap loads] → [esbuild ready] → [WARM]
   ↓                                                ↓ lease
[discard]                                       [LEASED]
                                                    ↓ dispose
                                              [recycling] → [WARM]
                                                    ↓ if recycle fails
                                                [discard]
```

### 7.3 Recycling vs. discarding

When a card is disposed, the iframe enters `recycling`. The host posts `dispose` to the iframe, which:
1. Calls `ReactDOM.unmountComponentAtNode(root)`.
2. Clears any timers, intervals, and event listeners it tracked.
3. Resets `canvasAPI` proxy state.
4. Resets the theme.
5. Posts `recycle.ready`.

If `recycle.ready` arrives within 1 second, the iframe returns to the warm pool. Otherwise it's discarded and a new one created. Aggressive: any error during the iframe's lifetime sets a "do not recycle" flag — better to spawn a fresh iframe than risk poisoning the pool.

### 7.4 Pool warmup

On Tauri app start, after the WSS connects, the pool eagerly creates its target warm count in the background. The first `widget.render` after app start lands on a warm iframe. Without warmup the first card costs ~150ms; with warmup it's ~30ms.

### 7.5 Why pool size matters

User opens 8 cards rapidly. Pool starts at 2 warm. After the first 2 mounts, the next 6 are cold (~100ms each). To avoid this perceived lag, the pool *replenishes asynchronously* — every time a lease happens, a background creation is kicked off so the pool returns to its target size. Trading background CPU for foreground latency.

### 7.6 Memory budget

Each iframe consumes ~5-10 MB of memory (React + esbuild-wasm + primitives). 8 cards = 40-80 MB. The pool size cap is the main lever; expose it as a Tauri setting if the user runs many cards.

---

## 8. The `canvasAPI` capability surface (v1)

The vocabulary is shared with the Hermes-side spec §4. This section specifies the *Tauri-side implementation* of each.

### 8.1 `hermes.ask(prompt: string): Promise<string>`

Implementation: sends a `widget.api_call` RPC to Hermes with capability `hermes.ask` and a fresh `correlation_id`. The server acknowledges immediately; `ApiCallRegistry` stashes the pending Promise keyed by `correlation_id`. When the server emits `widget.api_response` with the matching `correlation_id`, the registry resolves the Promise with `result.answer`. Rejects on RPC error.

**Response size: enforced server-side at 32 KiB hard cap.** The Hermes-side spec v2 §3.5.3 / §4.1 specifies that the server measures response size *before* emitting `widget.api_response`. If the response would exceed 32 KiB, the server emits an error response with code `4106` instead. The Tauri broker:

1. Receives the `widget.api_response` with `error.code = 4106`.
2. Looks up the pending Promise by `correlation_id`.
3. Rejects the Promise with `{ code: 4106, message: "Response exceeded 32 KiB cap", actual_size: ... }` (the server includes actual size in the error message).
4. Posts `api.result` to the iframe with the rejection.

The card's try/catch sees a structured `4106` error and can:
- Display a card-appropriate error ("response too long; try narrowing the question").
- Optionally call `canvasAPI.hermes.ask` again with a more specific prompt.
- The agent observing the error event can reformulate and `widget_message` the data structurally.

**The broker does NOT attempt to truncate, retry, or work around the cap.** That's not its job — surfacing the structured error is. Card authors and the agent handle the UX.

**Card disposal during flight.** If the card is disposed before `widget.api_response` arrives, `ApiCallRegistry.cancelByCard` is called. The broker emits `widget.api_cancel` for each pending correlation; pending Promises are rejected; if `widget.api_response` arrives later for a cancelled correlation, it is silently dropped. See §6.6 path A.

**Source update during flight.** If `widget.update` arrives while a `hermes.ask` is pending, `ApiCallRegistry.cancelByCard` is called with `reason="card_updated"`. Same dynamics as disposal, except the iframe is being re-mounted rather than torn down — the new mount starts fresh with no pending calls inherited from the old. See §6.6 path B.

### 8.2 `hermes.stream` — deferred to v2.

### 8.3 `notes.save(args: { title, body, tags?[] }): Promise<{ note_id }>`

Implementation: calls existing note service. No Hermes round-trip.

### 8.4 `storage.get(key: string): Promise<unknown | null>`
### 8.5 `storage.set(key: string, value: unknown): Promise<void>`
### 8.6 `storage.keys(): Promise<string[]>`

Per-card kv. Backed by Tauri's local storage namespaced by `card_id`. Values JSON-serialized; size cap 256 KB per key, 4 MB per card. Storage persists across re-mounts within a session; cleared on `widget.disposed`.

### 8.7 `card.resize({ w: number, h: number }): Promise<void>`
### 8.8 `card.set_title(title: string): Promise<void>`
### 8.9 `card.close(reason?: string): Promise<void>`

Self-management. `close` triggers `widget.disposed` with the given reason.

### 8.10 `os.notify({ title?, body }): Promise<void>`
### 8.11 `os.copy_clipboard(text: string): Promise<void>`

Tauri-side OS bridge.

### 8.12 `onMessage(handler: (payload) => void): () => void`

Subscribe to host-pushed messages (from `widget.message` events). Returns an unsubscribe function. Allows the agent to push data into the card without remounting. **Recommended pattern** for delivering structured data larger than the 32 KiB `hermes.ask` cap — the agent uses `widget_message` for the data and `hermes.ask` only for short conversational answers.

### 8.13 Capability NOT in v1 (per Hermes-side spec)

File system, network, cross-card communication, OS dialogs, host IPC. Spec these refusals in tests.

---

## 9. The primitives library — generous v1

The agent imports primitives via `import { ... } from 'canvas-primitives'`. Every primitive is themed via the design tokens injected at bootstrap. Every primitive is dark-mode aware. Every primitive is keyboard-accessible.

### 9.1 Layout primitives (eager bundle)

| Primitive | Purpose | Notes |
|---|---|---|
| `<Card>` | The default container with title, body, optional footer. | Most cards start here. |
| `<Stack>` | Vertical stack with gap prop. | Default vertical layout. |
| `<Row>` | Horizontal stack with gap, align, justify props. | Side-by-side layouts. |
| `<Spacer>` | Flexible space in a Stack/Row. | |
| `<Divider>` | Horizontal or vertical separator. | |
| `<Section>` | Titled subsection inside a card. | |

### 9.2 Text primitives (eager)

| Primitive | Purpose |
|---|---|
| `<Text>` | Body text with size, weight, muted props. |
| `<Heading>` | h1/h2/h3 with consistent sizing. |
| `<Code>` | Inline monospace. |
| `<Pre>` | Block monospace with optional copy button. |

### 9.3 Form primitives (eager)

| Primitive | Purpose |
|---|---|
| `<Field>` | Label + input + helper/error. The form atom. |
| `<TextInput>` | Standard text input. |
| `<TextArea>` | Multi-line input with auto-resize option. |
| `<Select>` | Native or custom select with options array. |
| `<Combobox>` | Searchable select. |
| `<Checkbox>` | Single checkbox with label. |
| `<CheckboxGroup>` | Multi-select via checkboxes. |
| `<RadioGroup>` | Single-select via radio buttons. |
| `<Toggle>` | On/off switch. |
| `<Slider>` | Range with optional min/max/step. |
| `<NumberInput>` | Numeric input with up/down. |
| `<DatePicker>` | Date with calendar. |
| `<Form>` | Wrapper providing `useForm()` context for declarative forms. |

### 9.4 Action primitives (eager)

| Primitive | Purpose |
|---|---|
| `<Button>` | Variants: primary, secondary, ghost, danger. |
| `<IconButton>` | Square button with icon only. |
| `<Link>` | Styled anchor. Opens via Tauri's link handler. |
| `<Menu>` | Dropdown menu with items. |
| `<Popover>` | Anchored floating panel. |

### 9.5 Feedback primitives (eager)

| Primitive | Purpose |
|---|---|
| `<Badge>` | Small status pill. |
| `<Tag>` | Removable label. |
| `<Spinner>` | Loading indicator. |
| `<Skeleton>` | Loading placeholder. |
| `<Toast>` | Self-dismissing notification (renders inside the card; not OS notification). |
| `<Alert>` | Inline info/warning/danger banner. |
| `<EmptyState>` | "No items yet" pattern with title, description, optional action. |

### 9.6 Display primitives (eager)

| Primitive | Purpose |
|---|---|
| `<Avatar>` | Circle with initials or image. |
| `<Tooltip>` | Hover/focus tooltip. |
| `<Progress>` | Linear progress bar. |
| `<Stat>` | Large metric with label and optional delta. |

### 9.7 Heavy primitives (lazy-loaded chunks)

| Primitive | Backed by | Capability requirements |
|---|---|---|
| `<Chart>` | recharts | None. Variants: `<Chart kind="line" />`, `kind="bar"`, `kind="area"`, `kind="pie"`, `kind="scatter"`, `kind="combo">`. |
| `<Table>` | TanStack Table | None. Sortable columns, optional pagination, optional row-click handler. |
| `<DnDList>` | dnd-kit | None. Sortable list with item-level customization. |
| `<KanbanBoard>` | dnd-kit | None. Multi-column DnD; built on top of `<DnDList>`. |
| `<RichTextEditor>` | tiptap | None for read-only; `notes.save` recommended for write. |
| `<CodeEditor>` | CodeMirror 6 | None. Language prop for syntax highlighting. |
| `<MarkdownView>` | marked + sanitize | None. Renders sanitized markdown. |

Each heavy primitive ships as a separate ~50-200 KB chunk, loaded on first render only. The eager bundle stays small.

### 9.8 Hooks (eager)

| Hook | Purpose |
|---|---|
| `useStorage(key, initial)` | Reactive `useState` backed by `canvasAPI.storage`. |
| `useHermes()` | Returns `{ ask, streaming }`. Throws if `hermes.ask` capability not declared. |
| `useMessage(handler)` | Sugar for `canvasAPI.onMessage`. |
| `useTheme()` | Returns the current theme tokens reactively. |
| `useCardSize()` | Returns the live size; updates if the user resizes. |

### 9.9 Why generous

A minimal primitives set forces the agent to compose at the wrong level — every card becomes a manual layout, every form is hand-rolled. A generous set means the agent's job is *composition*, not *implementation*. The cost is bootstrap weight; the lazy-loading mitigates this for heavy primitives, and React + design tokens for everything else.

The rule for adding to the primitives library is: a primitive earns its place either by being clearly common across card types (form fields, charts) or by enabling a class of widgets that would otherwise be impossible (DnD lists enable kanban / sortable / drag-rearrangeable).

---

## 10. The `<AgentWidgetCard>` component

The runtime needs a host-side React component that:
- Takes a `card_id` from the live-cards registry.
- Leases an iframe from the pool.
- Renders the iframe inside the existing canvas card chrome (`FloatingPanel`).
- Forwards lifecycle events to the broker.
- Disposes on unmount, triggering `ApiCallRegistry.cancelByCard`.

### 10.1 File location

```
src/components/agent-canvas/side-cards/
  AgentWidgetCard.tsx
  AgentWidgetCard.test.tsx
```

Per the existing implementation plan, this lives alongside `<ToolProgressCard>`, `<ArtifactCard>`, `<SubagentThreadCard>`. It's a new card type; the canvas dispatcher routes `widget.render` events to mount one.

### 10.2 Composition

`<AgentWidgetCard>` follows the existing side-card pattern: `FloatingPanel` chrome is owned by the parent `AgentCanvas`, and side-card components render as **children** of a FloatingPanel — they never wrap themselves in chrome. The actual `FloatingPanel` (at `src/components/node-editor/components/FloatingPanel.tsx`) takes `id`, `initialX`, `initialY`, `initialWidth`, `initialHeight`, `config`, `onDismiss`, `onPositionChange`, `onSizeChange` props. `AgentCanvas.tsx` builds a `floatingPanels` array and routes by `card.kind`, so the integration is:

```tsx
// In AgentCanvas.tsx, alongside the existing branches for 'artifact'/'tool-progress'/'subagent-thread':
if (card.kind === 'widget') {
  return (
    <AgentWidgetCard
      cardId={card.cardId}
      source={card.source}
      capabilities={card.capabilities}
      title={card.title}
      onDispose={(reason) => disposeCard(card.cardId, reason)}
    />
  )
}

// AgentWidgetCard.tsx — renders only the iframe + lifecycle wiring:
export function AgentWidgetCard({ cardId, source, capabilities, onDispose }: Props) {
  const iframeRef = useRef<HTMLIFrameElement>(null)
  // lease iframe from pool, post `init`, listen for mounted/error/api.call,
  // dispatch to broker. On unmount, trigger ApiCallRegistry.cancelByCard,
  // return iframe to pool, emit widget.disposed.
  return (
    <iframe
      ref={iframeRef}
      sandbox="allow-scripts"
      srcDoc={bootstrapHtml}
      style={{ width: '100%', height: '100%', border: 0 }}
    />
  )
}
```

The component manages: leasing the iframe on mount, posting `init`, listening for `mounted` / `error` / `api.call` messages, dispatching to the broker, and on unmount: calling `disposeCard` which triggers `ApiCallRegistry.cancelByCard`, returns the iframe to the pool, and emits `widget.disposed` to Hermes. The user-close button comes for free from `FloatingPanel.onDismiss` in the parent.

### 10.3 Resize behavior

When the user resizes the FloatingPanel, the new size is posted to the iframe as `card.size.update`. The `useCardSize()` hook in the bootstrap is reactive. When the agent calls `canvasAPI.card.resize`, the host resizes the FloatingPanel.

### 10.4 Position is the user's

Per the Hermes-side spec §10.6: `widget.update` re-mounts but preserves position.

**Reality check on storage.** Today the existing canvas does NOT store per-card position/size in the store. `useAgentCanvasStore.sideCards` (at `src/stores/agentCanvasStore.ts`) tracks only `id`, `kind`, `title`, `content`, `pinned`, `status`, timestamps. `AgentCanvas.tsx` computes positions per-render from a 3-column grid (`x: 60 + (i % 3) * (SIDE_CARD_WIDTH + 24)`). User-driven position/size are owned by `FloatingPanel` itself via internal state and surfaced through `onPositionChange` / `onSizeChange` callbacks.

For widget cards specifically, the runtime needs Hermes-driven `initial_size` to be honored on first mount, and user-driven position/size to survive a `widget.update` re-mount. Two viable paths the planner should pick between:

- **(a) Extend the layout calculator only.** Keep position/size out of the store; let `FloatingPanel` keep ownership; pass `initial_size` from the `widget.render` payload through to the `<FloatingPanel>` `initialWidth`/`initialHeight` props for widget kinds. Update-driven re-mounts preserve position/size naturally because the FloatingPanel doesn't unmount — only the `<AgentWidgetCard>` child re-renders with new source.
- **(b) Add per-card position/size to the widget variant of `SideCard`.** Persist `{ position: { x, y }, size: { w, h } }` keyed by `cardId` and feed `onPositionChange` / `onSizeChange` callbacks back into the store. Required if widgets need to survive remount across a tree-level component change, or if persistence (Hermes-side §11.4) becomes a v2 ask.

(a) is the simpler v1; (b) is what the original spec text assumed. Pick during planning. Either way, `widget.update` cancels any in-flight `widget.api_call` correlations for the old source — see §6.6 path B.

---

## 11. The contracts pipeline

This is the cross-machine glue. The Tauri side is the source of truth for `canvasAPI` and primitives types. The Hermes side consumes generated `.d.ts` files plus a hand-maintained wire-protocol doc and the example `.tsx` files (which Hermes serves to the agent on demand via `read_widget_example` per the v2 Hermes-side spec).

### 11.1 Source-of-truth files

```
src/agent-widgets/
  primitives/
    index.ts              ← exports all primitives
    types.ts              ← shared prop types
  canvas-api/
    index.ts              ← canvasAPI implementation (host-side dispatchers)
    types.ts              ← canvasAPI public type definitions
    capabilities.ts       ← capability metadata (name, args type, return type)
```

### 11.2 Generation script

```
scripts/generate-contracts.ts
```

Run via `bun run contracts:generate`. Steps:

1. `tsc --emitDeclarationOnly --declarationDir build/contracts/` over `src/agent-widgets/primitives/index.ts` and `src/agent-widgets/canvas-api/types.ts`.
2. Bundle the resulting `.d.ts` files into two single files: `canvas-primitives.d.ts` and `canvas-api.d.ts`.
3. Strip internal-use types (anything tagged `@internal`).
4. Copy to `contracts/` (the local mirror of the contracts repo).
5. Validate by type-checking the example `.tsx` files in `contracts/examples/` against the generated types.

### 11.3 Contracts repo layout

```
contracts/
  canvas-primitives.d.ts       ← generated
  canvas-api.d.ts              ← generated
  wire-protocol.md             ← hand-maintained (== both specs' §3)
  capabilities.md              ← hand-maintained list with descriptions
  examples/
    static-info.tsx
    form-with-hermes-ask.tsx
    list-with-storage.tsx
    chart.tsx
    dnd-kanban.tsx
    rich-text-with-save.tsx    ← demonstrates lazy-loaded primitive
    ... (~6-10 examples)
```

### 11.4 Versioning

Each generated `.d.ts` includes a version comment: `// generated from canvas-primitives@<git-sha>`. The Hermes side records the version it was built against; if Hermes detects a mismatch on connection (via `client.hello`), it logs a warning.

### 11.5 How Hermes consumes (resolved)

The Hermes side (per spec v2 §15.2) consumes types and examples on demand via the agent tools `list_widget_examples()` and `read_widget_example(name)`, which read from `gateway/system_prompts/widget_examples/`. The Tauri side ships the canonical example files from `contracts/examples/` and copies them across.

Hand-off mechanism (the user is currently solving manually):
- Tauri side commits generated files to `contracts/`.
- The user copies `contracts/examples/` and the `.d.ts` files to the Hermes side's `gateway/system_prompts/widget_examples/` and matching path.
- Eventually: contracts repo as a git submodule on both sides; Hermes runs `git submodule update` to sync. Or Tailscale Drive / iCloud sync of the `contracts/` directory.

### 11.6 Examples are the agent's training material

The `contracts/examples/*.tsx` files double as the on-demand reference material the Hermes agent fetches via `read_widget_example`. They're real, type-checked, runnable JSX that demonstrates each primitive and capability in context. Each file should include:

- A JSDoc summary at the top (one-line description used by `list_widget_examples`).
- The full component source.
- Inline comments explaining which primitives, capabilities, and patterns are used.
- A note about which capabilities the example *requires* in its `capabilities` array.

These three signals give the agent enough to compose new widgets by reference without needing the full `.d.ts` content inlined into the system prompt.

---

## 12. Integration with the existing canvas

### 12.1 What's already there

Confirmed against the merged hermes-agent canvas code (commit `367bbb8f`):

- **`FloatingPanel`** at `src/components/node-editor/components/FloatingPanel.tsx`. Generic draggable/resizable chrome with physics. Props: `id`, `initialX`, `initialY`, `initialWidth`, `initialHeight`, `config`, `onDismiss`, `onPositionChange`, `onSizeChange`, plus optional `onFullscreen` / `onEdit` / `topBoundary` / `autoHeight`. Reused as-is — `<AgentWidgetCard>` is rendered as a child, not a wrapper.
- **`useSliceGesture`** — gesture handler for the canvas. No change.
- **`DotGridCanvas`, `NoiseOverlay`, `VisorFrame`** — visual canvas layer. No change.
- **Existing side cards** at `src/components/agent-canvas/side-cards/`: `<ToolProgressCard>`, `<ArtifactCard>`, `<SubagentThreadCard>`. Plain functional components that take `title`/`content`/`status`/`pinned` + callbacks and render inline-styled divs — they don't import `FloatingPanel`. `<AgentWidgetCard>` joins this family.
- **`AgentCanvas.tsx`** at `src/components/agent-canvas/AgentCanvas.tsx`. Builds a `floatingPanels` array via `useMemo` and renders each with a `<FloatingPanel>`; routes by `card.kind` (current branches: `'artifact'` / `'tool-progress'` / `'subagent-thread'`). The widget runtime adds a `'widget'` branch.
- **`useAgentCanvasStore`** at `src/stores/agentCanvasStore.ts`. Zustand + Immer. State includes `sideCards: SideCard[]` keyed by uuid, with `SideCardKind = 'tool-progress' | 'artifact' | 'subagent-thread'`. Actions: `spawnSideCard`, `updateSideCardByToolCall`, `removeSideCard`, `pinSideCard`, etc. No persistence (resets on reload).
- **`hermesService`** at `src/services/hermesService.ts`. Surface:
  - `call<T>(method, params): Promise<T>` — outbound JSON-RPC via the `hermes_call` Tauri command (or mock when `!isTauri()`).
  - `onEvent(handler): Promise<UnlistenFn>` — subscribes to **all** inbound events via Tauri's `listen<HermesEventEnvelope>('hermes:event', …)`. There is no per-type dispatch in the service; routing happens in hooks.
  - `onStatus`, `getStatus`, `forceReconnect`, `setToken`, `getTokenMetadata`, `clearToken`, `healthCheck`.
- **`mockHermes`** at `src/services/mockHermes.ts`. Browser-mode dev fallback, used when `isTauri()` returns false. Provides `.call()` and `.on()` parity with the real service.
- **Rust side** at `src-tauri/src/hermes/`: `client.rs` runs the WebSocket loop and broadcasts inbound JSON via a Tokio broadcast channel; `rpc.rs` tracks request-id → oneshot for outbound calls; `notification.rs` filters events for OS notifications; `commands/hermes.rs` exposes `hermes_call`, `hermes_status`, `hermes_force_reconnect`, etc. as Tauri commands. **The current Rust client only sends JSON-RPC requests** (`{ jsonrpc, id, method, params }`); it has no path for sending bare `event`-shape messages. The widget runtime needs to add one — see §12.2 and §18.

### 12.2 What changes

- **A new hook** `useAgentWidgets` (e.g. `src/hooks/useAgentWidgets.ts`) subscribes via `hermesService.onEvent(env => …)` and switches on `env.params.type` to route the `widget.*` event family. This matches how `useStreamingTurn` and `useHermesNotifications` consume events today; **`hermesService.ts` itself does not change.**
- **`useAgentCanvasStore` is extended** rather than forked into a parallel store. Add `'widget'` to `SideCardKind`; widen the `SideCard` discriminated union with widget-only fields (`cardId: string` in `wgt_<6 hex>` format, `source: string`, `capabilities: string[]`, optional `initialSize: { w, h }`). New actions: `spawnWidgetCard`, `updateWidgetCard`, `pushWidgetMessage`, `disposeWidgetCard`, `hasWidgetCard`. Rationale: shared lifecycle with other side cards (single registry, single layout pass, single render path in `AgentCanvas.tsx`) and the existing `kind`-discriminator pattern is already exactly what we need. **Alternative for the planner:** a separate `useAgentWidgetsStore` if widget persistence (Hermes-side §11.4) lands in v2 and diverges sharply from other side cards — but for v1 the lifecycle is identical.
- **A new singleton** `ApiCallRegistry` (per §6.5). It calls `hermesService.call('widget.api_call', …)` for outbound async requests and consumes the immediate `{ accepted, correlation_id }` ack. Inbound `widget.api_response` and `widget.api_cancel` events are routed to it from `useAgentWidgets`.
- **`AgentCanvas.tsx` adds a `'widget'` branch** alongside the existing `if (card.kind === 'artifact')` / `'tool-progress'` / `'subagent-thread'` branches (see lines ~107–117 for the layout array build, ~379–405 for the render dispatch).
- **Rust-side event-emit path** in `src-tauri/src/hermes/client.rs` and a matching Tauri command (e.g. `hermes_emit_event`). Client-originated events (`widget.mounted`, `widget.error`, `widget.disposed`, `widget.api_cancel`) carry `{ jsonrpc: "2.0", method: "event", params: { type, session_id, payload } }` — i.e. the same envelope shape as inbound events but flowing the other way. The current `hermes_call` is JSON-RPC-request-only; we need a sibling that takes a fully-formed event envelope and writes it to the WSS sink.
- **`mockHermes` extension** to emit synthetic `widget.render` / `widget.update` / `widget.message` / `widget.dispose` events and respond to outbound `widget.api_call` with realistic delays + a stub answer. This is required for browser-mode dev parity with the existing mock (the project's CLAUDE.md mandates browser-mode workability).
- **No refactor** of existing cards or primitives. Existing side cards keep their uuid ids — widget cards use `cardId` in `wgt_<6 hex>` format alongside the store-issued uuid `id`. The `kind` discriminator absorbs the heterogeneity cleanly.

### 12.3 The agent canvas dispatcher

`hermesService` exposes a single `onEvent(handler)` that delivers every inbound event envelope. Per-type routing happens in hooks (matching the existing `useStreamingTurn` / `useHermesNotifications` pattern). The new `useAgentWidgets` hook does the widget-side dispatch:

```ts
// src/hooks/useAgentWidgets.ts (new)
useEffect(() => {
  let unlisten: (() => void) | undefined
  hermesService.onEvent(env => {
    const { type, payload } = env.params
    switch (type) {
      case 'widget.render':
        useAgentCanvasStore.getState().spawnWidgetCard(payload as WidgetRenderPayload)
        break
      case 'widget.update':
        apiCallRegistry.cancelByCard(payload.card_id, 'card_updated')
        useAgentCanvasStore.getState().updateWidgetCard(payload as WidgetUpdatePayload)
        break
      case 'widget.message':
        useAgentCanvasStore.getState().pushWidgetMessage(payload as WidgetMessagePayload)
        break
      case 'widget.dispose':
        if (!useAgentCanvasStore.getState().hasWidgetCard(payload.card_id)) return  // idempotent: already gone
        apiCallRegistry.cancelByCard(payload.card_id, 'card_disposed')
        useAgentCanvasStore.getState().disposeWidgetCard(payload as WidgetDisposePayload)
        break
      case 'widget.api_response':
        apiCallRegistry.handleResponse(payload as WidgetApiResponsePayload)
        break
      case 'widget.api_cancel':
        apiCallRegistry.cancelByCorrelation(payload.correlation_id, payload.reason)
        break
      // events outside the widget.* namespace fall through and are handled by other hooks
    }
  }).then(off => { unlisten = off })
  return () => { unlisten?.() }
}, [])
```

Notes that match existing conventions:
- Use `getState()` inside the handler closure to avoid re-subscribing whenever store fields change (per the project's established pattern — see CLAUDE.md and `useStreamingTurn`).
- The hook mounts once at the top of the agent-canvas tree (e.g. inside `AgentCanvas.tsx` itself, near the existing event-consuming hooks).
- The store backs `<AgentWidgetCard>` instances via the `'widget'` discriminator; the rest of the runtime hangs off there.

---

## 13. Performance budgets

Targets the runtime should meet on a baseline laptop (M1 Air or equivalent Windows hardware):

| Metric | Target | Hard ceiling |
|---|---|---|
| Cold pool warmup (app start to 2 warm iframes) | < 500 ms | 1 s |
| Card mount (warm pool) | < 30 ms | 100 ms |
| Card mount (cold) | < 150 ms | 300 ms |
| Capability call latency (local) | < 5 ms | 20 ms |
| Capability call latency (`hermes.ask` overhead) | runtime overhead < 10 ms (excludes server-side processing) | n/a |
| Cancellation propagation (card unmount → widget.api_cancel emitted) | < 5 ms | 50 ms |
| Memory per warm iframe | < 10 MB | 20 MB |
| Eager bootstrap size (excluding esbuild-wasm) | < 200 KB | 400 KB |
| Heavy primitive chunk | < 200 KB each | 400 KB |

Failures of hard ceilings are bugs; failures of targets are warnings the planner should track.

---

## 14. Error handling and UX

### 14.1 Compile errors

The bootstrap catches them, posts `widget.error` with `phase: "compile"`. The host displays a fallback inside the card chrome — the iframe is replaced with a small panel: "This card couldn't render" + the error message + a "Retry" button (which posts `init` again with the same source) + a "Tell agent" button (which sends a btw to Hermes with the error context).

### 14.2 Runtime errors

The bootstrap's error boundary catches them. Same UX as compile errors but `phase: "runtime"`. The error is also posted up so Hermes can react.

### 14.3 Capability denials

A card calling an undeclared capability sees a Promise rejection. The card's own UX handles it (the agent should write try/catch). The host also emits `widget.error` with `phase: "capability"` for observability.

### 14.4 Iframe pool exhaustion

If 8 cards are open and a 9th `widget.render` arrives: pool spawns a fresh iframe (cold mount). If memory pressure forces refusal, the runtime emits `widget.error` with `kind: "pool_exhausted"`. The agent can dispose other cards and retry.

### 14.5 Iframe crash

If an iframe stops responding to ping (heartbeat every 5s), the host kills it, emits `widget.error` with `kind: "iframe_crashed"`, and removes the card from the registry. Any in-flight `widget.api_call` correlations for that card are cancelled (via `cancelByCard`) before the registry entry is removed. The user sees the card replaced with a fallback and a "Retry" button.

### 14.6 Response too large (4106)

When `widget.api_response` arrives carrying error code `4106`, the broker rejects the iframe's pending Promise with a structured error: `{ code: 4106, message: "...", actual_size: ... }`. The card receives this in its try/catch around `canvasAPI.hermes.ask(...)`. Recommended card UX: a small inline alert ("Response was too long; try asking more specifically") rather than letting the rejection bubble to the error boundary. The agent observing the error event in its main loop should pre-pivot to `widget_message` for structured data on the next attempt.

### 14.7 Cancelled call

When a Promise is rejected via cancellation (card disposed, source updated, server-initiated cancel), the rejection carries `{ code: "cancelled", reason: "card_disposed" | "card_updated" | ... }`. Card code typically won't see this — for `card_disposed`, the iframe is being unmounted; for `card_updated`, the iframe is being re-mounted with new source. But if the card has installed a `.catch` handler that fires synchronously before tear-down, the rejection shape is structured and self-describing.

---

## 15. Open considerations for the planner

Decisions Claude Code should surface during domain discovery, not silently resolve.

### 15.1 Existing primitive collisions

The repo currently has one host-side primitives library at `src/components/gameHQ/primitives/` (`Card`, `CardHeader`, `Bar`, `Badge`, `SciFiPanel`) used by the GameHQ surface. The agent-canvas side cards (`<ToolProgressCard>`, `<ArtifactCard>`, `<SubagentThreadCard>`) do NOT use a shared primitives library — they're inline-styled functional components.

Practically there's no collision risk: the agent-widgets primitives ship inside the iframe as a separate bundle and the host-side GameHQ primitives never enter that bundle. The decision the planner still has to make is whether the two libraries should share design tokens (so a card and a HQ panel feel like the same product) and whether anything from `gameHQ/primitives/` should be lifted into a shared foundation. Probably yes for tokens, no for components — the audiences and theme intent diverge.

### 15.2 Tiptap vs. Lexical for `<RichTextEditor>`

Tiptap is more popular and easier to embed; Lexical is more powerful and Facebook-maintained. Picker should evaluate both for iframe-friendliness and bundle size. If you already have one in the chat UI, prefer that for consistency.

### 15.3 Recharts vs. Visx vs. Chart.js for `<Chart>`

Recharts is React-idiomatic and ~100 KB minified. Visx is Airbnb's lower-level toolkit. Chart.js is imperative. For agent ergonomics, Recharts is the strong default — agents are well-trained on its API.

### 15.4 dnd-kit vs. react-beautiful-dnd

react-beautiful-dnd is unmaintained as of 2024. dnd-kit is the modern choice. Confirm.

### 15.5 CodeMirror 6 vs. Monaco for `<CodeEditor>`

Monaco is heavier (~3 MB) and provides more features. CodeMirror 6 is ~150 KB and tree-shakeable. For iframe context, CodeMirror 6 is the strong default.

### 15.6 Where does the bootstrap HTML live?

Three options:
- Bundled as a string literal in `agent-widgets/runtime/bootstrap.ts` (built by Vite as a separate entry).
- Stored as a static asset under `src-tauri/icons/` or similar Tauri-assets path.
- Generated at build time and stamped into the binary.

The first option is simplest; the third gives strongest integrity guarantees. Pick during planning.

### 15.7 What does the user see while a card is mounting?

Cold mount is 30-150ms. Anything longer feels broken. Suggest: a skeleton card chrome with a subtle shimmer, replaced by the iframe content the moment `widget.mounted` arrives. Don't show a spinner unless mount exceeds 300ms.

### 15.8 Multi-card layout

The existing canvas has a layout strategy (per `agent-canvas-design.md`). Open question: does a `widget.render` card pick its own position via `initial_size`, or does the canvas auto-place it? Probably auto-place with a hint. Verify against the existing layout system.

### 15.9 Can the user "save" a card?

Today the canvas has `<ArtifactCard>` with save-as-note. Should `<AgentWidgetCard>` also have a save action? Probably yes — "save this card so I can come back to it" is a real user request. v2 plumbs this into widget persistence (Hermes-side spec §11.4).

### 15.10 Card-level theming overrides

The bootstrap injects theme tokens. Should the agent be allowed to override them per-card (e.g., a "warning" themed card with red accents)? Suggest: yes via a `theme` prop on `<Card>` that locally overrides specific tokens. Adds a small surface to the primitives library.

### 15.11 Primitives library version negotiation

If the Tauri side ships v1.2 of primitives but the Hermes agent's example files (read on demand via `read_widget_example`) were generated against v1.0, the agent might import a primitive that doesn't exist in this build. Solution: include the primitives library version in `client.hello`; Hermes invalidates its example cache if the version differs. Open question: how often will this realistically happen, and is the complexity worth it for v1? With on-demand fetching (per Hermes v2 spec §15.2), version mismatch only affects the next `read_widget_example` call, so the blast radius is small.

### 15.12 Per-call cancellation

v1 cancels all calls for a card on disposal/update — there's no per-call cancellation API exposed to the iframe (no `canvasAPI.hermes.ask().cancel()`). If a card legitimately wants to cancel an in-flight `hermes.ask` (e.g., user clicks a "stop" button while the ask is processing), the only path today is to dispose the card or update its source. v2 may add an explicit cancellation API. Verify whether this is needed early.

### 15.13 ApiCallRegistry persistence

If the Tauri app crashes mid-call, the in-flight correlations are lost on the client side. The server may still process and emit `widget.api_response` events that arrive at a fresh client with no matching correlation — those will be dropped silently. Open question: is this acceptable for v1, or should the client persist `ApiCallRegistry` to local storage and replay on reconnect? Probably acceptable for v1; the cards are session-scoped anyway.

---

## 16. Domain test scenarios

GIVEN/WHEN/THEN with WHY fields. The Tauri side has more surface to test than the Hermes side; this list is illustrative, not exhaustive.

### 16.1 Iframe pool warmup

```
GIVEN the Tauri app starts up and connects to Hermes
 WHEN 500 ms have passed since the WSS connected
 THEN the iframe pool contains exactly 2 warm iframes
  AND each has esbuild-wasm initialized
  AND each has the primitives bundle loaded
WHY first-card mount latency dominates user perception of the feature.
```

### 16.2 Warm-mount latency

```
GIVEN a warm iframe pool with at least one iframe
 WHEN a widget.render event arrives with valid 1 KB source
 THEN the card is mounted within 50 ms
  AND widget.mounted is emitted with compile_ms recorded
WHY warm-pool mounts must feel instant.
```

### 16.3 Capability allowlist enforcement

```
GIVEN a card declared ["notes.save"]
 WHEN the card calls canvasAPI.hermes.ask("...")
 THEN the broker rejects locally without round-tripping to Hermes
  AND the card sees a Promise rejection with code 4104
  AND widget.error is emitted with phase="capability"
WHY undeclared calls must never reach Hermes; that's the entire purpose of declaration.
```

### 16.4 Capability call dispatch

```
GIVEN a card declared ["notes.save"]
 WHEN the card calls canvasAPI.notes.save({ title: "x", body: "y" })
 THEN the broker validates args via Zod
  AND calls noteService.save
  AND posts api.result to the iframe
  AND the card's Promise resolves with { note_id }
WHY the broker is the only path between iframe and services; it must be reliable.
```

### 16.5 hermes.ask round-trip (async)

```
GIVEN a card declared ["hermes.ask"]
 WHEN the card calls canvasAPI.hermes.ask("What's Q3 revenue?")
 THEN the broker generates a fresh correlation_id
  AND ApiCallRegistry registers a pending Promise keyed by correlation_id
  AND the broker sends widget.api_call to the server
  AND the server acknowledges immediately with { accepted: true, correlation_id }
  AND the broker does NOT block — it returns to the event loop
  AND other messages on the WebSocket are processed during the wait
  WHEN widget.api_response arrives with matching correlation_id and { result: { answer: ... } }
  THEN ApiCallRegistry resolves the pending Promise
  AND the broker posts api.result to the iframe
  AND the card's Promise resolves with { answer: ... }
  AND the registry entry is removed
WHY this is the cross-machine async flow; both ends must agree on shape, timing, and correlation. The WebSocket must remain responsive during long-running calls.
```

### 16.6 widget.update preserves position, resets state, cancels old correlations

```
GIVEN a card mounted at (x=100, y=200) with internal React state set to "edited"
  AND a pending hermes.ask call from the card with correlation_id "corr_abc"
 WHEN widget.update arrives with new source
 THEN apiCallRegistry.cancelByCard is called with reason="card_updated"
  AND widget.api_cancel is emitted to Hermes for "corr_abc"
  AND the pending Promise from the old mount is rejected with { code: "cancelled", reason: "card_updated" }
  AND the iframe re-mounts with the new source
  AND the FloatingPanel position is still (100, 200)
  AND the React state is reset to initial
  AND if widget.api_response for "corr_abc" arrives later, it is dropped (registry entry already removed)
WHY position belongs to the user; state belongs to the card. Old Promises must be cancelled so the new mount doesn't receive stale data destined for the old.
```

### 16.7 Iframe recycling

```
GIVEN a card is disposed cleanly
 WHEN the host posts dispose to the iframe
 THEN within 1 second the iframe posts recycle.ready
  AND the iframe returns to the warm pool
  AND a subsequent widget.render mounts on the recycled iframe
  AND no React state from the previous card is observable
WHY recycling is the key to the pool's working set staying small.
```

### 16.8 Iframe recycling on error

```
GIVEN a card threw a runtime error during its lifetime
 WHEN the card is disposed
 THEN the iframe is NOT recycled
  AND the iframe is discarded
  AND the pool spawns a replacement
WHY one bad iframe shouldn't poison the pool.
```

### 16.9 Heavy primitive lazy load

```
GIVEN a card mounts that imports <Chart>
 WHEN the <Chart> component first renders
 THEN the recharts chunk is dynamic-imported
  AND a skeleton renders during load
  AND the actual chart replaces the skeleton on chunk-ready
  AND subsequent <Chart> mounts in this iframe load instantly
WHY the eager bundle stays small; heavy primitives pay only when used.
```

### 16.10 Theme propagation

```
GIVEN a card is mounted with light-mode theme tokens
 WHEN the user switches the host to dark mode
 THEN every live card receives a theme.update message
  AND the iframe re-applies tokens
  AND the card's primitives reflect dark mode within 100ms
WHY card visual consistency with the canvas is a real UX bar.
```

### 16.11 Source size limit

```
GIVEN widget.render arrives with source > 256 KB
 WHEN the runtime validates
 THEN widget.error is emitted with code 4102
  AND no iframe is leased
WHY oversized payloads usually indicate the agent inlined data; the error nudges it toward hermes.ask or widget_message.
```

### 16.12 Sandbox escape attempt

```
GIVEN a card's source attempts to access window.parent
 WHEN the card runs
 THEN the sandbox attribute "allow-scripts" without "allow-same-origin" produces a SecurityError
  AND the card's error boundary catches it
  AND widget.error is emitted with phase="runtime"
  AND the host is unaffected
WHY this is the security claim of the entire design; it must hold under hostile cards.
```

### 16.13 Simultaneous dispose race — both sides idempotent

```
GIVEN a card that the agent is concurrently calling widget_dispose on
  AND the user simultaneously clicks the close button
 WHEN both dispose signals are in flight
 THEN whichever arrives first at the broker takes effect
  AND the second arrival is detected by registry lookup (entry already removed)
  AND the second arrival silently no-ops
  AND the iframe pool receives exactly one dispose signal — no double-free
  AND no widget.error is emitted (the race is handled gracefully)
  AND ApiCallRegistry.cancelByCard is called exactly once
WHY two actors can trigger the same state change; the system must handle the race without errors or leaks. Idempotent both sides means the broker's state machine converges regardless of arrival order.
```

### 16.14 hermes.ask disposal during flight — cancellation emitted

```
GIVEN a card with a pending hermes.ask call (correlation_id "corr_xyz")
 WHEN the card is disposed (user closes it)
 THEN apiCallRegistry.cancelByCard is called with reason="card_disposed"
  AND widget.api_cancel is emitted to Hermes for "corr_xyz"
  AND the pending Promise is rejected with { code: "cancelled", reason: "card_disposed" }
  AND the rejection is NOT delivered to the iframe (iframe is being torn down)
  AND if widget.api_response for "corr_xyz" arrives later, it is silently dropped
  AND the iframe is recycled or discarded per §7.3 rules
WHY closing a card with in-flight requests should never produce phantom updates or cross-card state corruption. Active cancellation (vs. passive abandonment) lets the server stop wasting work.
```

### 16.15 Server-initiated cancellation

```
GIVEN a card with a pending hermes.ask call (correlation_id "corr_xyz")
 WHEN widget.api_cancel arrives from the server with correlation_id="corr_xyz" and reason="session_ended"
 THEN apiCallRegistry.cancelByCorrelation is called
  AND the pending Promise is rejected with { code: "cancelled", reason: "session_ended" }
  AND if the iframe is still mounted, api.result with the rejection is posted to it
  AND the registry entry is removed
  AND if widget.api_response arrives later, it is silently dropped
WHY rare but real: sessions can end while calls are in flight. The runtime must clean up promptly without leaving zombie Promises.
```

### 16.16 Response size cap (4106)

```
GIVEN a card calls canvasAPI.hermes.ask("...") and the agent's answer exceeds 32 KiB
 WHEN widget.api_response arrives with error code 4106
 THEN the broker looks up the pending Promise by correlation_id
  AND rejects it with { code: 4106, message: "...", actual_size: ... }
  AND posts api.result to the iframe with the rejection
  AND the card's try/catch catches the structured error
  AND no truncation, retry, or fallback is attempted by the broker
  AND the card can display a card-appropriate error UX
WHY the 32 KiB cap is enforced server-side; the broker's job is faithful surfacing of the structured error, not workarounds. Card authors and the agent handle the UX.
```

### 16.17 ApiCallRegistry leak resistance

```
GIVEN ApiCallRegistry has 5 pending correlations across 3 cards
 WHEN all 3 cards are disposed and the WebSocket disconnects
 THEN cancelByCard is called for each of the 3 cards
  AND every pending Promise is rejected
  AND every entry is removed from the registry
  AND the registry is empty
  AND no Promise references remain in memory after a GC pass
WHY the registry is a long-lived singleton. Leaked correlations are a memory leak that compounds over a session.
```

### 16.18 widget.dispose for unknown card_id is a no-op

```
GIVEN a card with card_id "wgt_xxx" was never mounted (or was already disposed)
 WHEN widget.dispose arrives from the server with card_id="wgt_xxx"
 THEN the broker registry lookup returns nothing
  AND no widget.disposed is emitted
  AND no error is thrown
  AND the iframe pool is not touched
WHY client-side idempotency: server retries or duplicate dispose events must not produce zombie state on the client.
```

---

## 17. But if… — alternative paths to consider

**But if `<RichTextEditor>` and `<CodeEditor>` add too much weight,** v1 ships without them and adds in v2. The agent gracefully falls back to `<TextArea>` for editing. Worth measuring the bundle impact during planning before committing.

**But if the iframe pool feels over-engineered for v1,** start with a single iframe (no pool, ~150ms cold mount per card) and add pooling in v2 once the rest of the runtime is stable. Trade: feature ships sooner; first-card UX is noticeably slower.

**But if generated `.d.ts` files prove fiddly,** an alternative is to *hand-maintain* the canvas-primitives.d.ts and canvas-api.d.ts as the source of truth, and have both Tauri and Hermes import from there. Tauri's runtime implements against the types; Hermes's prompt addendum reads them. The cost: type drift if the Tauri implementation diverges. The benefit: simpler tooling, faster iteration.

**But if you want to go further with isolation,** each card could run in a separate Web Worker that does the JSX compile, with the iframe used only for rendering. Two layers of isolation. Overkill for v1; reasonable v3 hardening if the runtime ever needs to render *third-party* cards (e.g., shared from another user).

**But if streaming render is a v1 must-have,** the iframe needs a buffer-then-compile mode that holds partial source until a `widget.render.complete` event arrives, displaying a skeleton during buffering. Adds maybe 20% to runtime scope. Worth it only if "card materializing as the agent thinks" is a defining UX moment for you.

**But if the 32 KiB response cap feels wrong,** the wire-level constant `HERMES_ASK_RESPONSE_CAP_BYTES` is shared with the Hermes-side spec. Argue with the planner there if you want it tighter (16 KiB, forcing structured data sooner) or looser (64 KiB, fewer 4106 errors). The Tauri side just consumes whatever the server enforces.

**But if `ApiCallRegistry` should be persistent (survive Tauri app restart),** v1 punts because cards are session-scoped anyway and the server cleans up on disconnect. v2 can add LocalStorage-backed registry if the use case emerges. See §15.13.

**But if you want the contracts pipeline to be nicer than copy-paste,** set up the contracts repo (or a folder synced via Tailscale Drive / iCloud / Dropbox / a self-hosted git-over-ssh) at the start of planning, and let both Claude Code sessions read from it. The user friction this avoids compounds across every contract change.

---

## 18. Acceptance criteria

The Tauri-side work is done when:

1. The iframe pool warmup target (§13) is met on a baseline machine.
2. All capability dispatchers (§8) are implemented and Zod-validated.
3. The eager primitives bundle (§9.1–9.6) is shipping at ≤ 200 KB (excluding esbuild-wasm).
4. Heavy primitives (§9.7) lazy-load on first use.
5. `<AgentWidgetCard>` integrates into the existing canvas without refactoring other card types.
6. The contracts pipeline (§11) generates and ships `canvas-primitives.d.ts` + `canvas-api.d.ts` to a known location.
7. At least 6 example `.tsx` files exist in `contracts/examples/` and type-check against the generated types. Each file has a JSDoc summary suitable for `list_widget_examples()` consumption on the Hermes side.
8. The broker implements the async `widget.api_call` → ack → `widget.api_response` pattern for `hermes.ask` round-trips, including correlation by `correlation_id` and zombie-Promise cleanup on card disposal.
9. **`ApiCallRegistry` (§6.5) is implemented** with `invoke`, `resolve`, `reject`, `cancelByCard`, `cancelBySession`, `cancelByCorrelation`, and emits `widget.api_cancel` to the server on cancellation.
10. **Cancellation paths A, B, C from §6.6 are all implemented and tested** (§16.6, §16.14, §16.15).
11. **`widget.api_response` with error code 4106 (§14.6, §16.16) is surfaced as a structured Promise rejection to the card** — no truncation, no retry, no silent fallback.
12. Disposal is idempotent on both sides: the race where the user closes a card while the agent is also disposing it is handled without errors, double-frees, or state corruption (§16.13). `widget.dispose` for an unknown `card_id` is a silent no-op (§16.18).
13. All test scenarios in §16 pass.
14. The wire contract (§3) is implemented bidirectionally, including the new `widget.api_response` event and `widget.api_cancel` events (both directions).
15. The base contract (`tauri-client-contract.md`) gets a `§N. Widget render` section appended with the canonical wire shape.
16. **`useAgentCanvasStore` is extended** (or a parallel store introduced — the choice is documented in the implementation plan) with a `'widget'` `SideCardKind` and the widget-specific fields per §12.2. `AgentCanvas.tsx` gets a corresponding render branch.
17. **`src-tauri/src/hermes/client.rs` gains a path to emit client-originated `event`-shape messages** over the WSS sink (current code only sends JSON-RPC requests). Surfaced via a Tauri command (e.g. `hermes_emit_event`) and consumed by the runtime to send `widget.mounted`, `widget.error`, `widget.disposed`, and `widget.api_cancel`. This closes the open consideration in the Hermes-side spec §11.5.
18. **`mockHermes` provides browser-mode parity** for the widget runtime: emits synthetic `widget.render` / `widget.update` / `widget.message` / `widget.dispose` events, and responds to outbound `widget.api_call` with realistic delays + a stub `widget.api_response`. This preserves the project's existing browser-mode dev pattern.
19. **The new `useAgentWidgets` hook** routes `widget.*` events via `hermesService.onEvent` + a switch on `env.params.type`, mirroring how `useStreamingTurn` and `useHermesNotifications` handle their own event families. `hermesService` itself is not modified.

The Hermes-side acceptance lives in [hermes-widget-render-spec.md](./hermes-widget-render-spec.md) §13.

---

## 19. Reference: minimal happy-path flow (Tauri-side perspective)

1. **App starts.** WSS connects. Iframe pool spins up 2 warm iframes in the background (~500ms). `ApiCallRegistry` initializes empty.
2. **`widget.render` arrives** for `wgt_8a3f9c` with 4 KB of source declaring `["hermes.ask", "notes.save"]`.
3. **Runtime validates.** Source size OK, capabilities all known.
4. **Pool leases an iframe.** Warm; ready immediately.
5. **Host posts `init`.** Iframe receives, compiles in 12ms, mounts the component, posts `widget.mounted`.
6. **`<AgentWidgetCard>` renders** the iframe inside a `FloatingPanel` at the auto-placed position. Card registered in `agentWidgetsStore`.
7. **User types into the form, clicks "ask Hermes".** Card calls `canvasAPI.hermes.ask(...)`. Iframe posts `api.call`; broker generates `correlation_id="corr_a1b2"`, registers the pending Promise in `ApiCallRegistry`, sends `widget.api_call`; server acknowledges immediately; broker returns to event loop. Other messages flow freely during the wait.
8. **Server emits `widget.api_response`** with `correlation_id="corr_a1b2"` and the answer. Broker resolves the registered Promise, posts `api.result` to the iframe, removes the registry entry. UI updates.
9. **User clicks "save as note".** Card calls `canvasAPI.notes.save(...)`. Broker dispatches locally to `noteService.save`; `api.result` returns with `{ note_id }`; card shows a confirmation toast.
10. **User closes the card.** `<AgentWidgetCard>` unmounts triggers `disposeCard("wgt_8a3f9c", "user_closed")`. `apiCallRegistry.cancelByCard` runs (no pending calls in this case, but the path is exercised). Host posts `dispose` to the iframe. Iframe cleans up, posts `recycle.ready` in 80ms, returns to the pool. Host emits `widget.disposed` to Hermes.
11. **Pool replenishes** any below-target slots in the background.
12. **(If a `widget.dispose` from the server arrives later for the same `card_id`)** registry lookup fails; broker silently no-ops.

---

## 20. Resolved decisions (history)

These were open questions in v0/v1 that have been closed during the spec's evolution. Kept here so the rationale is preserved for future readers; not actionable for the planner.

### 20.1 Resolved: contracts pipeline ownership and direction (was §11.5)

**The question:** does the type contract live in the Tauri repo, the Hermes repo, or a separate contracts repo? Who generates, who consumes?

**The answer:** Tauri side is the source of truth (TypeScript compiles down to `.d.ts` for primitives and canvasAPI). Examples are also Tauri-authored `.tsx` files. Generated artifacts go to `contracts/`; copied manually today, eventually synced via a contracts repo / git submodule. Hermes consumes on demand via `list_widget_examples` and `read_widget_example` tools (per Hermes v2 spec §15.2).

### 20.2 Resolved: synchronous vs async `widget.api_call` (was implicit)

**The question:** can a `hermes.ask` round-trip be a normal JSON-RPC request/response?

**The answer:** no — long-running calls would hit JSON-RPC timeouts and confuse observability. The async ack/correlate/respond pattern with `correlation_id` is the v1 baseline. The `ApiCallRegistry` (§6.5) owns this on the Tauri side.

### 20.3 Resolved: cancellation semantics (was implicit)

**The question:** what happens when a card is disposed or updated while a `hermes.ask` is in flight?

**The answer:** active cancellation via `widget.api_cancel`. The broker emits the cancellation to the server, rejects the pending Promise, and removes the correlation from the registry. The server attempts to cancel the underlying work (best-effort). See §6.6.

### 20.4 Resolved: response size discipline (was implicit)

**The question:** how do we keep the agent from cramming large data into `hermes.ask` answers?

**The answer:** a hard 32 KiB wire cap with error code 4106 (per Hermes v2 §3.5.3 / §4.1). The Tauri broker surfaces the structured error directly to the card's Promise; no truncation, no fallback. See §8.1, §14.6, §16.16.
