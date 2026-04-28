# Tauri Agent Widget Runtime — Spec for Claude Code (Tauri side)

> **For agentic workers:** this is a *spec*, not a plan. Hand it to the planning skill and run domain discovery before producing tasks. The wire shape is committed and shared with the Hermes side; the runtime architecture and primitives library are the real engineering work this spec describes.

**Audience:** Claude Code with read-write access to the Tauri canvas repo (where `agent-canvas-design.md`, `agent-canvas-implementation-plan.md`, and `tauri-client-contract.md` live).

**Goal:** Build the client-side runtime that consumes `widget.render` events from Hermes and renders agent-authored React/JSX cards on the canvas. The runtime mounts each card in a sandboxed iframe, brokers a capped capability surface (`canvasAPI`), pools iframes for fast mount, and ships a generous primitives library the agent can compose against. The runtime is the *renderer* and the *gatekeeper*; Hermes is the *author*.

**Companion doc:** [hermes-widget-render-spec.md](./hermes-widget-render-spec.md) — the Mac-mini side. The two specs share §3 of the wire contract verbatim; the Tauri side additionally owns the runtime, the primitives library, and the type-contract generation pipeline.

**Reference docs:**
- [agent-canvas-design.md](./agent-canvas-design.md) — existing canvas this slots into
- [agent-canvas-implementation-plan.md](./agent-canvas-implementation-plan.md) — existing card types, conventions, file layout
- [tauri-client-contract.md](./tauri-client-contract.md) — base wire protocol; this spec extends §4 with the `widget.*` namespace
- [hermes-widget-render-spec.md](./hermes-widget-render-spec.md) — companion Hermes-side spec

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
   │           │  - response correlation        │
   │           └──────┬─────────────────────────┘
   │                  │
   │       ┌──────────┴──────────┐
   │       ▼                     ▼
   │  Local services        Hermes RPC
   │  (notes, storage,      (widget.api_call
   │   os bridge, kv)        for hermes.ask)
   ▼
Existing canvas (FloatingPanel, useSliceGesture, ZoneOverlay, ...)
   - <AgentWidgetCard> is a new card type that wraps the iframe lease
   - Lives alongside <ToolProgressCard>, <ArtifactCard>, <SubagentThreadCard>
```

Three new TypeScript subsystems:

1. `agent-widgets/runtime/` — iframe pool, broker, postMessage protocol, lifecycle.
2. `agent-widgets/primitives/` — the generous primitives library (§9).
3. `agent-widgets/contracts/` — the type-generation pipeline that publishes `.d.ts` for the Hermes side.

Plus minor changes:
- A new card type `<AgentWidgetCard>` in `components/agent-canvas/side-cards/`.
- A new dispatcher entry in `services/hermesService.ts` for `widget.*` events and the `widget.api_call` request method.
- A new state slice (Zustand) for the live-cards registry.

---

## 3. Wire contract — pointer

The wire-shape is fully specified in §3 of [hermes-widget-render-spec.md](./hermes-widget-render-spec.md). This Tauri-side spec implements the *consumer* of every event documented there:

| Event / method | Direction | Tauri-side responsibility |
|---|---|---|---|
| `widget.render` | server → client | Validate, lease iframe, mount, emit `widget.mounted` or `widget.error`. |
| `widget.update` | server → client | Find card by id, replace source, re-mount, preserve canvas position. |
| `widget.message` | server → client | Forward payload via postMessage to the iframe; the card's `canvasAPI.onMessage` handler receives it. |
| `widget.dispose` | server → client | Tear down the iframe, return it to the pool, emit `widget.disposed`. Idempotent: if the card is already being disposed, silently succeed. |
| `widget.api_call` | client → server | Emitted when a card calls `canvasAPI.hermes.*`. Server acknowledges immediately, processes async, delivers result via `widget.api_response`. |
| `widget.api_response` | server → client | Delivers the result of an async `widget.api_call`. Correlated by `correlation_id`. |
| `widget.mounted` | client → server | Emitted after successful compile + first render. |
| `widget.error` | client → server | Emitted on any failure with `phase` field per spec. |
| `widget.disposed` | client → server | Emitted on teardown, regardless of cause. |
| `widget.dispose` | client → server | If the user closed the card, this fires first. Server must handle the race where a server `widget.dispose` is in-flight simultaneously. |

Error codes `4101–4105` and `5101–5102` per the Hermes-side spec §8 apply on both sides. The Tauri runtime maps internal errors to these codes when emitting `widget.error`.

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
```

Every message is JSON-serializable. No transferable objects in v1 (keeps the protocol simple). Origin and source checks: every incoming message is verified against the mounted iframe's `contentWindow` reference; messages from any other source are dropped with a console warning.

### 6.2 The broker's job

For each `api.call`:

1. Find the card's allowlist by `card_id` (broker keeps a map).
2. If `capability` is not in the allowlist → respond with error code `4104`.
3. Look up the capability in the dispatch table.
4. Run the dispatch function with `args`, awaiting if async.
5. Post `api.result` with the result or error.

The dispatch table maps each capability to an implementation:

```ts
const dispatchers: Record<string, Dispatcher> = {
  "hermes.ask":   ({ args, card_id, session_id }) => hermesService.widgetApiCall(session_id, card_id, "hermes.ask", args),
  "notes.save":   ({ args }) => noteService.save(args),
  "storage.get":  ({ args, card_id }) => storage.get(card_id, args.key),
  "storage.set":  ({ args, card_id }) => storage.set(card_id, args.key, args.value),
  // ... etc
}
```

Capabilities that round-trip to Hermes (`hermes.*`) go through the existing `hermesService` and become `widget.api_call` RPC calls per the wire contract. **These use the async pattern** (§3.5 of the Hermes-side spec):

1. Broker sends `widget.api_call` with a `correlation_id` and the capability args.
2. Server responds with an immediate `{ accepted: true, correlation_id }` ack.
3. Broker stashes the iframe's Promise, keyed by `correlation_id`.
4. When the server finishes processing, it emits `widget.api_response` with the matching `correlation_id`.
5. Broker looks up the pending Promise, resolves (or rejects) it, and posts `api.result` to the iframe.

This means `hermes.ask` calls do not block the WebSocket. The service layer emits the request, gets the ack, and returns control to the event loop. Other messages (`widget.render`, `widget.message`, chat) keep flowing while `hermes.ask` is being processed.

The broker MUST handle the case where an iframe is disposed while a `hermes.ask` is in flight — the pending Promise should be abandoned (not resolved), and the `widget.api_response` should be silently dropped when it arrives if the card is no longer in the registry.

Everything else is local.

### 6.3 Argument validation

Every dispatcher MUST validate its arguments. This is a trust boundary — bad args from a malicious card cannot crash the host. Use Zod schemas (or equivalent) per capability; reject mismatches with code `4002`.

### 6.4 Concurrency

Cards can issue capability calls in parallel. The broker correlates by message `id` (for local calls) or `correlation_id` (for Hermes round-trips). There's no global queue — the broker is fire-and-forget per call, with response routing back to the iframe by id. Long-running capabilities (`hermes.ask`) MUST NOT block other calls — they use the async pattern in §3.5 of the Hermes-side spec, where the server acknowledges immediately and delivers the result later via `widget.api_response`.

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

Implementation: sends a `widget.api_call` RPC to Hermes with capability `hermes.ask` and a fresh `correlation_id`. The server acknowledges immediately; the broker stashes the pending Promise keyed by `correlation_id`. When the server emits `widget.api_response` with the matching `correlation_id`, the broker resolves the Promise with `result.answer`. Rejects on RPC error.

**Response size.** The returned string can theoretically be large (up to 64 KiB in v1). The broker does NOT enforce a size limit — that is the card's responsibility. Cards should check `answer.length` and switch to lazy rendering for responses exceeding ~10 KiB.

**Streaming hint.** For long answers, the agent may send incremental data via `widget.message` events before the final `widget.api_response` arrives. Cards can prepare for this by subscribing to `canvasAPI.onMessage` and handling progressive updates. Full streaming (`hermes.stream`) is deferred to v2.

**Card disposal during flight.** If the card is disposed before `widget.api_response` arrives, the broker abandons the pending Promise and does not forward the result to the iframe.

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

Subscribe to host-pushed messages (from `widget.message` events). Returns an unsubscribe function. Allows the agent to push data into the card without remounting.

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
- Disposes on unmount.

### 10.1 File location

```
src/components/agent-canvas/side-cards/
  AgentWidgetCard.tsx
  AgentWidgetCard.test.tsx
```

Per the existing implementation plan, this lives alongside `<ToolProgressCard>`, `<ArtifactCard>`, `<SubagentThreadCard>`. It's a new card type; the canvas dispatcher routes `widget.render` events to mount one.

### 10.2 Composition

```tsx
<FloatingPanel
  title={card.title}
  initialPosition={card.position}
  size={card.size}
  onClose={() => disposeCard(card.id, "user_closed")}
>
  <iframe
    ref={iframeRef}
    sandbox="allow-scripts"
    srcDoc={bootstrapHtml}
    style={{ width: "100%", height: "100%", border: 0 }}
  />
</FloatingPanel>
```

The component manages: leasing the iframe on mount, posting `init`, listening for `mounted` / `error` / `api.call` messages, dispatching to the broker, returning the iframe to the pool on unmount.

### 10.3 Resize behavior

When the user resizes the FloatingPanel, the new size is posted to the iframe as `card.size.update`. The `useCardSize()` hook in the bootstrap is reactive. When the agent calls `canvasAPI.card.resize`, the host resizes the FloatingPanel.

### 10.4 Position is the user's

Per the Hermes-side spec §10.6: `widget.update` re-mounts but preserves position. Implementation: the canvas-state slice keeps `position` and `size` keyed by `card_id`; an update event keeps these and only swaps `source`.

---

## 11. The contracts pipeline

This is the cross-machine glue. The Tauri side is the source of truth for `canvasAPI` and primitives types. The Hermes side consumes generated `.d.ts` files plus a hand-maintained wire-protocol doc.

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

### 11.5 How Hermes consumes

Hand-off mechanism (this is what the user proposed solving manually for now):
- Tauri side commits generated files.
- The user copies them to the Hermes side (today).
- Eventually: contracts repo as a git submodule on both sides; Hermes runs `git submodule update` to sync.

The Hermes-side spec §11.6 calls this out as an open question. This Tauri spec answers: types are generated from Tauri code, mechanism is a contracts repo (eventually).

### 11.6 Examples are the agent's training material

The `contracts/examples/*.tsx` files double as the system-prompt addendum source for Hermes. They're real, type-checked, runnable JSX that demonstrates each primitive and capability in context. The Hermes side reads them at agent-start to populate the system prompt addendum (§5.4 of the Hermes-side spec).

---

## 12. Integration with the existing canvas

### 12.1 What's already there

Per `agent-canvas-implementation-plan.md`:
- `FloatingPanel` — generic resizable card chrome. Reused.
- `useSliceGesture` — gesture handler for the canvas. No change needed.
- `DotGridCanvas`, `NoiseOverlay`, `VisorFrame` — visual canvas layer. No change needed.
- `<ActiveChatCard>`, `<ToolProgressCard>`, `<ArtifactCard>`, `<SubagentThreadCard>` — existing card types. `<AgentWidgetCard>` is new and joins the family.
- `services/hermesService.ts` — TS facade over Tauri's `invoke('hermes_call', …)`.

### 12.2 What changes

- **`hermesService.ts`** gets new event handlers for `widget.render`, `widget.update`, `widget.message`, `widget.dispose`, and a new RPC method emitter for `widget.api_call`.
- **A new Zustand store slice** for the live cards registry: `useAgentWidgetsStore`.
- **The canvas component** that lays out side cards adds `<AgentWidgetCard>` to its render tree, mapping over the live cards registry.
- **No refactor** of existing cards or primitives.

### 12.3 The agent canvas dispatcher

Today, the canvas listens for tool/message events and routes them to the appropriate card type. Add a fourth route:

```ts
on("widget.render", payload => agentWidgetsStore.add(payload))
on("widget.update", payload => agentWidgetsStore.update(payload))
on("widget.message", payload => agentWidgetsStore.pushMessage(payload))
on("widget.dispose", payload => agentWidgetsStore.dispose(payload))
```

The store backs `<AgentWidgetCard>` instances; the rest of the runtime hangs off there.

---

## 13. Performance budgets

Targets the runtime should meet on a baseline laptop (M1 Air or equivalent Windows hardware):

| Metric | Target | Hard ceiling |
|---|---|---|
| Cold pool warmup (app start to 2 warm iframes) | < 500 ms | 1 s |
| Card mount (warm pool) | < 30 ms | 100 ms |
| Card mount (cold) | < 150 ms | 300 ms |
| Capability call latency (local) | < 5 ms | 20 ms |
| Capability call latency (`hermes.ask`) | dominated by network/agent; runtime overhead < 10 ms | n/a |
| Memory per warm iframe | < 10 MB | 20 MB |
| Eager bootstrap size | < 200 KB | 400 KB |
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

If an iframe stops responding to ping (heartbeat every 5s), the host kills it, emits `widget.error` with `kind: "iframe_crashed"`, and removes the card from the registry. The user sees the card replaced with a fallback and a "Retry" button.

---

## 15. Open considerations for the planner

Decisions Claude Code should surface during domain discovery, not silently resolve.

### 15.1 Existing primitive collisions

The Tauri canvas already uses some of these primitives in the chat UI (likely a `<Button>`, possibly `<TextInput>`). The agent-widgets primitives library should NOT share files with the chat UI primitives — they have different audiences and different stability constraints. Open question: do the two libraries share styling tokens / underlying components, or are they fully independent? The right answer depends on how heavily the existing primitives are coupled to the chat UI.

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

If the Tauri side ships v1.2 of primitives but the Hermes agent's system prompt was generated against v1.0, the agent might import a primitive that doesn't exist yet in this build. Solution: include the primitives library version in `client.hello`; Hermes regenerates its addendum if the version differs and re-loads. Open question: how often will this realistically happen, and is the complexity worth it for v1?

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
 THEN the broker sends widget.api_call with a fresh correlation_id
  AND the server acknowledges immediately with { accepted: true, correlation_id }
  AND the broker stashes the Promise keyed by correlation_id
  AND the broker does NOT block — it returns to the event loop
  WHEN widget.api_response arrives with matching correlation_id
  THEN the broker posts api.result to the iframe
  AND the card's Promise resolves with { answer: ... }
  AND other messages on the WebSocket were processed during the wait
WHY this is the cross-machine async flow; both ends must agree on shape, timing, and correlation. The WebSocket must remain responsive during long-running calls.

### 16.6 widget.update preserves position, resets state

```
GIVEN a card mounted at (x=100, y=200) with internal React state set to "edited"
 WHEN widget.update arrives with new source
 THEN the iframe re-mounts with the new source
  AND the FloatingPanel position is still (100, 200)
  AND the React state is reset to initial
  AND any pending capability calls from the old mount are abandoned
WHY position belongs to the user; state belongs to the card.
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
WHY oversized payloads usually indicate the agent inlined data; the error nudges it toward hermes.ask.
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

### 16.13 Simultaneous dispose race

```
GIVEN a card that the agent is concurrently calling widget_dispose on
  AND the user simultaneously clicks the close button
 WHEN both dispose signals are in flight
 THEN whichever arrives first at the server takes effect
  AND the second arrival is treated as a no-op (idempotent disposal)
  AND the iframe pool receives exactly one dispose signal — no double-free
  AND no widget.error is emitted (the race is handled gracefully)
WHY two actors can trigger the same state change; the system must handle the race without errors or leaks.
```

### 16.14 hermes.ask dispossal during flight

```
GIVEN a card with a pending hermes.ask call
 WHEN the card is disposed (user closes it)
 THEN the broker abandons the pending Promise (does not resolve or reject)
  AND if widget.api_response arrives later, it is silently dropped
  AND no zombie callbacks write to the iframe's now-dead postMessage channel
WHY closing a card with in-flight requests should never produce phantom updates or cross-card state corruption.

---

## 17. But if… — alternative paths to consider

**But if `<RichTextEditor>` and `<CodeEditor>` add too much weight,** v1 ships without them and adds in v2. The agent gracefully falls back to `<TextArea>` for editing. Worth measuring the bundle impact during planning before committing.

**But if the iframe pool feels over-engineered for v1,** start with a single iframe (no pool, ~150ms cold mount per card) and add pooling in v2 once the rest of the runtime is stable. Trade: feature ships sooner; first-card UX is noticeably slower.

**But if generated `.d.ts` files prove fiddly,** an alternative is to *hand-maintain* the canvas-primitives.d.ts and canvas-api.d.ts as the source of truth, and have both Tauri and Hermes import from there. Tauri's runtime implements against the types; Hermes's prompt addendum reads them. The cost: type drift if the Tauri implementation diverges. The benefit: simpler tooling, faster iteration.

**But if you want to go further with isolation,** each card could run in a separate Web Worker that does the JSX compile, with the iframe used only for rendering. Two layers of isolation. Overkill for v1; reasonable v3 hardening if the runtime ever needs to render *third-party* cards (e.g., shared from another user).

**But if streaming render is a v1 must-have,** the iframe needs a buffer-then-compile mode that holds partial source until a `widget.render.complete` event arrives, displaying a skeleton during buffering. Adds maybe 20% to runtime scope. Worth it only if "card materializing as the agent thinks" is a defining UX moment for you.

**But if you want the contracts pipeline to be nicer than copy-paste**, set up the contracts repo (or a folder synced via Tailscale Drive / iCloud / Dropbox / a self-hosted git-over-ssh) at the start of planning, and let both Claude Code sessions read from it. The user friction this avoids compounds across every contract change.

---

## 18. Acceptance criteria

The Tauri-side work is done when:

1. The iframe pool warmup target (§13) is met on a baseline machine.
2. All capability dispatchers (§8) are implemented and Zod-validated.
3. The eager primitives bundle (§9.1–9.6) is shipping at ≤ 200 KB.
4. Heavy primitives (§9.7) lazy-load on first use.
5. `<AgentWidgetCard>` integrates into the existing canvas without refactoring other card types.
6. The contracts pipeline (§11) generates and ships `canvas-primitives.d.ts` + `canvas-api.d.ts` to a known location.
7. At least 6 example `.tsx` files exist in `contracts/examples/` and type-check against the generated types.
8. The broker implements the async `widget.api_call` → ack → `widget.api_response` pattern for `hermes.ask` round-trips, including correlation by `correlation_id` and zombie-Promise cleanup on card disposal.
9. Disposal is idempotent: the race where the user closes a card while the agent is also disposing it is handled without errors, double-frees, or state corruption.
10. All test scenarios in §16 pass.
11. The wire contract (§3) is implemented bidirectionally, including the new `widget.api_response` event.
12. The base contract (`tauri-client-contract.md`) gets a `§N. Widget render` section appended with the canonical wire shape.

The Hermes-side acceptance lives in [hermes-widget-render-spec.md](./hermes-widget-render-spec.md) §13.

---

## 19. Reference: minimal happy-path flow (Tauri-side perspective)

1. **App starts.** WSS connects. Iframe pool spins up 2 warm iframes in the background (~500ms).
2. **`widget.render` arrives** for `wgt_8a3f9c` with 4 KB of source declaring `["hermes.ask", "notes.save"]`.
3. **Runtime validates.** Source size OK, capabilities all known.
4. **Pool leases an iframe.** Warm; ready immediately.
5. **Host posts `init`.** Iframe receives, compiles in 12ms, mounts the component, posts `widget.mounted`.
6. **`<AgentWidgetCard>` renders** the iframe inside a `FloatingPanel` at the auto-placed position.
7. **User types into the form, clicks "ask Hermes".** Card calls `canvasAPI.hermes.ask(...)`. Iframe posts `api.call`; broker sends `widget.api_call` with `correlation_id`; server acknowledges immediately; broker returns to event loop. When the server finishes, it emits `widget.api_response`; broker resolves the iframe's pending Promise; UI updates. Other messages flow freely during the wait.
8. **User clicks "save as note".** Card calls `canvasAPI.notes.save(...)`. Broker dispatches locally to `noteService.save`; `api.result` returns with `{ note_id }`; card shows a confirmation toast.
9. **User closes the card.** `<AgentWidgetCard>` unmounts. Host posts `dispose` to the iframe. Iframe cleans up, posts `recycle.ready` in 80ms, returns to the pool. Host emits `widget.disposed` to Hermes.
10. **Pool replenishes** any below-target slots in the background.
