# Implementation Plan 01: Wire-Contract Foundation

> **Plan 01 of 8** in the widget-runtime sequence. Read `00-index.md` first.

## Context
First plan in the widget-runtime sequence. Lands the TS event types, Rust `event`-emit path, store/hook integration, and mockHermes parity that every later plan assumes. **No iframe, no broker, no primitives** — just the wire-shape in place and a stub widget card that proves end-to-end routing works.

Existing code consumed:
- `apps/desktop/anandia-workspace/src/services/hermesService.ts` — single `onEvent`, `call`
- `apps/desktop/anandia-workspace/src/services/mockHermes.ts` — browser-mode dev fallback
- `apps/desktop/anandia-workspace/src/components/agent-canvas/types.ts` — `SideCard`, `SideCardKind`, `HermesEventEnvelope`
- `apps/desktop/anandia-workspace/src/stores/agentCanvasStore.ts` — Zustand+Immer
- `apps/desktop/anandia-workspace/src/components/agent-canvas/AgentCanvas.tsx` — renders by `card.kind`
- `apps/desktop/anandia-workspace/src-tauri/src/hermes/client.rs` — WS loop, JSON-RPC request send
- `apps/desktop/anandia-workspace/src-tauri/src/commands/hermes.rs` — Tauri commands
- `apps/desktop/anandia-workspace/src-tauri/src/lib.rs` — `invoke_handler!` registration

## Domain Model

```
WidgetSideCard extends SideCard {
  kind: 'widget'
  cardId: string            // wgt_<6 hex>; distinct from store id (uuid)
  source: string            // JSX text from agent
  capabilities: string[]    // declared capability allowlist
  initialSize?: { w, h }    // optional Hermes hint

  INVARIANT: cardId matches /^wgt_[0-9a-f]{6}$/
  INVARIANT: capabilities is a subset of CAPABILITIES
  INVARIANT: store-issued id (uuid) is independent of cardId — both addressable
}

WidgetEvent (extends HermesEventEnvelope) {
  params.type ∈ {
    'widget.render', 'widget.update', 'widget.message',
    'widget.dispose', 'widget.api_response', 'widget.api_cancel'
  }
}

OutboundClientEvent (NEW envelope path) {
  jsonrpc: '2.0'
  method:  'event'
  params:  { type, session_id, payload }

  INVARIANT: no `id` field — events are not requests
  INVARIANT: emitted via hermes_emit_event Tauri command, NOT hermes_call
  INVARIANT: outbound types are 'widget.mounted' | 'widget.error'
                              | 'widget.disposed' | 'widget.api_cancel'
}
```

## Domain Tests

### A. Store extension — widget card lifecycle

**Invariants**
- Spawning a widget card with an existing `cardId` is rejected.
- Disposing by `cardId` removes exactly one registry entry.
- `hasWidgetCard(cardId)` returns false after disposal and for unknown ids.

**Behavior tests**

A1. GIVEN an empty `useAgentCanvasStore`
    WHEN `spawnWidgetCard({ cardId: 'wgt_abc123', source: '...', capabilities: ['hermes.ask'], title: 'T' })` is called
    THEN `sideCards` contains one entry with `kind === 'widget'`, the right `cardId`, `source`, `capabilities`, `title`
    AND `hasWidgetCard('wgt_abc123')` returns true
    WHY: widget cards must be addressable by their wire `cardId`, not the store-issued uuid.

A2. GIVEN a widget card with cardId `wgt_abc123` and source `"export default function() { return <div>old</div> }"`
    WHEN `updateWidgetCard({ cardId: 'wgt_abc123', source: 'export default function() { return <div>new</div> }', capabilities: [...] })` is called
    THEN that entry's `source` is the new string
    AND its `id` (uuid) is unchanged
    AND its store-array index is unchanged
    WHY: spec §10.4 — widget.update preserves user-driven layout; only source content swaps.

A3. GIVEN a widget card with cardId `wgt_abc123`
    WHEN `disposeWidgetCard({ cardId: 'wgt_abc123' })` is called
    THEN `sideCards` no longer contains an entry with that cardId
    AND `hasWidgetCard('wgt_abc123')` returns false
    WHY: disposal removes the registry entry; idempotent semantics depend on this.

A4. GIVEN no widget card exists with cardId `wgt_zzz999`
    WHEN `disposeWidgetCard({ cardId: 'wgt_zzz999' })` is called
    THEN no error is thrown
    AND no other side-cards are affected
    WHY: spec §16.18 — `widget.dispose` for unknown cardId is a silent no-op.

**Negative tests**

A5. INPUT: `spawnWidgetCard({ cardId: 'invalid', ... })`
    EXPECT: validation error — cardId must match `/^wgt_[0-9a-f]{6}$/`
    WHY: card-id format is part of the wire contract; bad ids must fail loud.

A6. INPUT: `spawnWidgetCard({ cardId: 'wgt_abc123', capabilities: ['unknown.cap'], ... })`
    EXPECT: validation error — unknown capability
    WHY: spec §3.1 — unknown capabilities cause refuse-mount with code 4101.

A7. INPUT: `spawnWidgetCard` called twice with the same `cardId`
    EXPECT: second call throws / the store state is not mutated by the second call
    WHY: duplicate cardIds break correlation routing in the broker.

### B. `useAgentWidgets` hook — event routing

**Invariants**
- Every event with `params.type` starting with `widget.` is handled.
- Events with other `params.type` values are NOT touched (no-op).

**Behavior tests** (vitest, with mocked `hermesService.onEvent` and a stub `apiCallRegistry`)

B1. GIVEN the hook is mounted
    WHEN a `widget.render` event is dispatched
    THEN `useAgentCanvasStore.getState().spawnWidgetCard` is called once with the payload
    WHY: spec §12.3 — render is the spawn signal.

B2. GIVEN a widget card with cardId `wgt_abc123`
    WHEN a `widget.update` event arrives for `wgt_abc123`
    THEN `apiCallRegistry.cancelByCard('wgt_abc123', 'card_updated')` is called BEFORE `updateWidgetCard`
    WHY: spec §16.6 — the new mount must not receive responses destined for the old source.

B3. GIVEN a widget card with cardId `wgt_abc123`
    WHEN a `widget.dispose` event arrives for `wgt_abc123`
    THEN `apiCallRegistry.cancelByCard('wgt_abc123', 'card_disposed')` is called BEFORE `disposeWidgetCard`
    WHY: spec §16.14 — disposal cancels in-flight calls actively, not passively.

B4. GIVEN no widget card with cardId `wgt_zzz999` exists
    WHEN a `widget.dispose` event arrives for `wgt_zzz999`
    THEN `apiCallRegistry.cancelByCard` is NOT called
    AND `disposeWidgetCard` is NOT called
    WHY: spec §16.18 — dispatcher checks `hasWidgetCard` first; unknown cardIds are silent no-ops.

B5. GIVEN a `widget.api_response` event arrives with `correlation_id: 'corr_xyz'`
    WHEN the hook routes it
    THEN `apiCallRegistry.handleResponse(payload)` is called once
    AND no store action runs
    WHY: api_response routes to the registry for promise resolution, not to the store.

B6. GIVEN a `widget.api_cancel` event (server-initiated) with `correlation_id: 'corr_xyz'`
    WHEN the hook routes it
    THEN `apiCallRegistry.cancelByCorrelation('corr_xyz', payload.reason)` is called
    AND no store action runs
    WHY: spec §16.15 — server-initiated cancels route to the registry alone.

B7. GIVEN a `tool.start` event arrives (non-widget event)
    WHEN the hook routes it
    THEN no widget action runs
    AND no apiCallRegistry method is called
    WHY: this hook owns only the `widget.*` namespace; bleed-through breaks separation.

### C. Canvas branch — widget kind renders

**UI contract tests** (vitest + @testing-library/react)

C1. RENDER: `<AgentCanvas>` with one widget card in store (`kind: 'widget'`, `cardId: 'wgt_abc123'`, `title: 'Test'`)
    MUST contain: an `<AgentWidgetCard>` for that cardId (use `data-testid="agent-widget-card"` + cardId attr)
    MUST NOT contain: an `<ArtifactCard>`, `<ToolProgressCard>`, or `<SubagentThreadCard>` for that cardId
    WHY: kind discriminator must route correctly so the right component receives the props.

C2. RENDER: `<AgentCanvas>` with a widget card and an artifact card both present
    MUST contain: both card types, each in its own `FloatingPanel`
    WHY: the widget branch must coexist with existing branches, not replace them.

C3. RENDER: `<AgentWidgetCard cardId="wgt_abc123" source="..." capabilities={[]} title="T" />` (stub — placeholder div)
    MUST contain: text or `data-card-id` attribute identifying `wgt_abc123`
    MUST NOT contain: any `<iframe>` element (real iframe lifecycle lands in Plan 04)
    WHY: this plan stubs the component — Plan 04 replaces the body with the real iframe lifecycle.

### D. Rust `hermes_emit_event` command

**Invariants**
- `hermes_emit_event` only writes to the WS sink when state is connected.
- Each event written matches the JSON-RPC envelope `{ jsonrpc: "2.0", method: "event", params: { type, session_id, payload } }` with **no `id` field**.

**Behavior tests** (cargo test, mock WS sink)

D1. GIVEN the hermes client is in `Connected` state with a mock sink
    WHEN `client.emit_event("widget.mounted", "ab12cd34", json!({"card_id":"wgt_abc123","compile_ms":12}))` is invoked
    THEN one frame is written to the sink
    AND the frame parses as JSON with exactly `{jsonrpc: "2.0", method: "event", params: {type, session_id, payload}}`
    AND no `id` key is present at any level of the envelope
    WHY: spec §3.6 — client-emitted events use the same envelope as server events but without an id.

D2. GIVEN the hermes client is in `Disconnected` state
    WHEN `client.emit_event(...)` is invoked
    THEN it returns `Err(...)` with a meaningful error variant (e.g., `EmitError::NotConnected`)
    AND no frame is written
    WHY: events sent to a closed sink can't be retried meaningfully; surface deterministically.

D3. GIVEN the Tauri command `hermes_emit_event` is registered
    WHEN invoked from the frontend with `{ type: "widget.mounted", session_id: "ab12", payload: {...} }`
    THEN it routes to `client.emit_event` and returns `Ok(())`
    AND on error, returns `Err(String)` matching the Tauri command convention (`Result<(), String>`)
    WHY: matches the existing `hermes_call` / `hermes_status` Rust↔TS contract per CLAUDE.md "Type Safety" pattern.

### E. mockHermes parity

**Behavior tests** (vitest)

E1. GIVEN browser-mode dev (`isTauri()` returns false)
    WHEN test calls `mockHermes.emitWidgetRender({ cardId: 'wgt_abc123', source: '...', capabilities: [] })`
    THEN any subscriber registered via `mockHermes.on(handler)` receives a synthetic envelope
    AND the envelope has `params.type === 'widget.render'`, `params.payload.card_id === 'wgt_abc123'`
    WHY: developers must be able to exercise the widget UI without a Hermes connection.

E2. GIVEN browser-mode dev
    WHEN code calls `hermesService.call('widget.api_call', { correlation_id: 'corr_x', card_id: 'wgt_abc123', capability: 'hermes.ask', args: {prompt: 'q'} })`
    THEN it returns `{ accepted: true, correlation_id: 'corr_x' }` synchronously
    AND after a configurable delay (default ≤ 200 ms in tests), a `widget.api_response` envelope is emitted with the matching correlation_id and a stub `result.answer`
    WHY: the async pattern (spec §3.5) must work in browser mode for hooks/registry development.

E3. GIVEN browser-mode dev
    WHEN `hermesService.emitEvent('widget.mounted', 'ab12', { card_id: 'wgt_abc123' })` is called
    THEN `mockHermes` records the emitted event in an inspectable buffer (`mockHermes.emittedEvents`)
    WHY: tests need to assert outbound emissions in browser mode.

## Implementation Order

### Step 1 — Test files (RED)
Write empty failing tests for each section A–E. **Run them; all must fail.**

- `apps/desktop/anandia-workspace/src/stores/agentCanvasStore.widget.test.ts` (A1–A7)
- `apps/desktop/anandia-workspace/src/hooks/useAgentWidgets.test.ts` (B1–B7)
- `apps/desktop/anandia-workspace/src/components/agent-canvas/AgentCanvas.widget.test.tsx` (C1–C2)
- `apps/desktop/anandia-workspace/src/components/agent-canvas/side-cards/AgentWidgetCard.test.tsx` (C3)
- `apps/desktop/anandia-workspace/src-tauri/src/hermes/client.rs` — append `#[cfg(test)] mod emit_event_tests` (D1–D2)
- `apps/desktop/anandia-workspace/src-tauri/src/commands/hermes.rs` — append `#[cfg(test)] mod hermes_emit_event_tests` (D3)
- `apps/desktop/anandia-workspace/src/services/mockHermes.widget.test.ts` (E1–E3)

Commit: `test(widget-runtime): plan 01 failing test scaffolding`.

### Step 2 — TS types
Edit `src/components/agent-canvas/types.ts`:
- Add `WidgetRenderPayload`, `WidgetUpdatePayload`, `WidgetMessagePayload`, `WidgetDisposePayload`, `WidgetApiResponsePayload`, `WidgetApiCancelPayload` interfaces (per spec §3.1–§3.6).
- Add `'widget'` to `SideCardKind`.
- Convert `SideCard` to a discriminated union; add `WidgetSideCard` variant: `{ kind: 'widget'; cardId: string; source: string; capabilities: readonly string[]; initialSize?: { w: number; h: number }; ... shared SideCard fields }`.
- Export `CAPABILITIES` constant: readonly tuple `['hermes.ask', 'notes.save', 'storage.get', 'storage.set', 'storage.keys', 'card.resize', 'card.set_title', 'card.close', 'os.notify', 'os.copy_clipboard']` (per spec §4 / §8).
- Export `CARD_ID_REGEX = /^wgt_[0-9a-f]{6}$/`.

Commit: `feat(types): widget event/payload types and SideCard discriminated union`.

### Step 3 — Store actions
Edit `src/stores/agentCanvasStore.ts`:
- Implement `spawnWidgetCard(payload: WidgetRenderPayload)`, `updateWidgetCard(payload: WidgetUpdatePayload)`, `pushWidgetMessage(payload: WidgetMessagePayload)`, `disposeWidgetCard(payload: WidgetDisposePayload)`, `hasWidgetCard(cardId: string): boolean`.
- Validate `cardId` against `CARD_ID_REGEX` and capabilities against `CAPABILITIES` in `spawnWidgetCard`. Throw on invalid input.
- Reject duplicate cardIds in `spawnWidgetCard`.
- Keep store-array index stable on update (find by cardId; mutate in place via Immer).
- Tests A1–A7 → green.

Commit: `feat(store): widget card lifecycle actions with cardId validation`.

### Step 4 — Stub `AgentWidgetCard`
Create `src/components/agent-canvas/side-cards/AgentWidgetCard.tsx`:
- Props: `{ cardId: string; source: string; capabilities: readonly string[]; title: string; onDispose: (reason: string) => void }`.
- Body: placeholder `<div data-testid="agent-widget-card" data-card-id={cardId}>Widget {cardId} (stub)</div>`. NO iframe.
- Test C3 → green.

Commit: `feat(side-cards): stub AgentWidgetCard placeholder`.

### Step 5 — Canvas dispatch branch
Edit `src/components/agent-canvas/AgentCanvas.tsx`:
- In the render-by-`card.kind` block (lines ~379–405), add `if (card.kind === 'widget') return <AgentWidgetCard cardId={card.cardId} source={card.source} capabilities={card.capabilities} title={card.title} onDispose={(reason) => disposeWidgetCard({ card_id: card.cardId, reason })} />`.
- The `floatingPanels` layout array (lines ~92–117) needs `card.kind === 'widget'` to opt into the same grid; honor `initialSize` if present (override `width` / `height` defaults), otherwise use the existing `SIDE_CARD_WIDTH` / `SIDE_CARD_HEIGHT`.
- Tests C1–C2 → green.

Commit: `feat(agent-canvas): route widget kind to AgentWidgetCard`.

### Step 6 — `useAgentWidgets` hook
Create `src/hooks/useAgentWidgets.ts`:
- Single `useEffect` mounting at root of `AgentCanvas.tsx`.
- Subscribes via `hermesService.onEvent`; switches on `env.params.type`; calls `useAgentCanvasStore.getState().X` (avoid render cascades — CLAUDE.md "Performance" pattern).
- Uses a stub `apiCallRegistry` import: `import { apiCallRegistry } from '@/runtime/agent-widgets/apiCallRegistry'` — create as `export const apiCallRegistry = { cancelByCard: vi.fn? } no — make it a real object with no-op methods that are spy-able`. Implementation:

```ts
// src/runtime/agent-widgets/apiCallRegistry.ts (stub for Plan 01; real impl in Plan 02)
export const apiCallRegistry = {
  cancelByCard: (_cardId: string, _reason: string) => {},
  cancelByCorrelation: (_correlationId: string, _reason: string) => {},
  handleResponse: (_payload: unknown) => {},
}
```

- Mount the hook from `AgentCanvas.tsx`: `useAgentWidgets()` near where existing hooks are called (e.g. at top of the component body).
- Tests B1–B7 → green.

Commit: `feat(hooks): useAgentWidgets event router with stub apiCallRegistry`.

### Step 7 — Rust `hermes_emit_event`
Edit `src-tauri/src/hermes/client.rs`:
- Add `pub async fn emit_event(&self, type_: &str, session_id: &str, payload: serde_json::Value) -> Result<(), EmitError>`.
- Construct envelope: `serde_json::json!({"jsonrpc": "2.0", "method": "event", "params": {"type": type_, "session_id": session_id, "payload": payload}})`.
- Match current state; if `Connected`, write to the sink; otherwise return `Err(EmitError::NotConnected)`.
- Add a new error enum `EmitError` (or reuse an existing one if there's a fitting variant).
- Tests D1–D2 → green.

Edit `src-tauri/src/commands/hermes.rs`:
- Add `#[tauri::command] async fn hermes_emit_event(state: tauri::State<...>, type_: String, session_id: String, payload: serde_json::Value) -> Result<(), String>`.
- Map `EmitError` → `String`.
- Test D3 → green.

Edit `src-tauri/src/lib.rs`:
- Append `hermes_emit_event` to the `invoke_handler!` macro list.

Commit: `feat(hermes-rust): hermes_emit_event command for client-originated events`.

### Step 8 — TS-side `emitEvent` wrapper
Edit `src/services/hermesService.ts`:
- Add `async emitEvent(type: string, sessionId: string, payload: unknown): Promise<void>` that calls `tauriInvoke('hermes_emit_event', { type, sessionId, payload })` in Tauri mode and `getMock().emitEvent(type, sessionId, payload)` otherwise.
- The Tauri command takes `type_` (snake) but Tauri's Rust↔TS bridge auto-maps camel `type` to `type_` — verify by inspecting existing commands. If not, pass via `{ type: ..., session_id: ..., payload: ... }` with whatever convention is already used.

Commit: `feat(hermes-service): emitEvent wrapper for outbound widget events`.

### Step 9 — mockHermes widget extensions
Edit `src/services/mockHermes.ts`:
- Add `emitWidgetRender(payload)`, `emitWidgetUpdate(payload)`, `emitWidgetMessage(payload)`, `emitWidgetDispose(payload)` helpers that synthesize `HermesEventEnvelope`s and fan out to subscribers.
- In `call(method, params)`, intercept `method === 'widget.api_call'`: return `{ accepted: true, correlation_id: params.correlation_id }`; schedule `setTimeout(() => emit synthetic widget.api_response with result.answer = 'mock answer for: ' + params.args.prompt, 100)`.
- Add `emitEvent(type, sessionId, payload)` that pushes to a `mockHermes.emittedEvents` array (inspectable in tests).
- Tests E1–E3 → green.

Commit: `feat(mock-hermes): widget event/api_call parity for browser-mode dev`.

### Step 10 — Verification
Run, in order:
- `bun run typecheck`
- `bun run test`
- `cd src-tauri && cargo test && cargo check && cd ..`
- `bun run test:e2e --project=chromium` (smoke; should still pass — no new e2e flow yet)

Fix red. Confirm acceptance criteria below all check off.

Commit: `chore(widget-runtime): plan 01 verification clean`.

## Acceptance Criteria
- [ ] All A1–A7, B1–B7, C1–C3, D1–D3, E1–E3 domain tests pass
- [ ] `bun run typecheck` passes
- [ ] `cargo check` and `cargo test` pass
- [ ] `bun run test:e2e --project=chromium` passes with no new failures
- [ ] Coverage on new TS modules ≥ 80% (`bun run test:coverage`)
- [ ] **Browser-mode demo**: a developer can call `mockHermes.emitWidgetRender({...})` from a Vite-mode test page and see the placeholder card mount in `AgentCanvas`
- [ ] **Tauri-mode**: `hermes_emit_event` writes the right envelope to the WS sink (Rust integration test)
- [ ] No iframe code introduced (placeholder only)
- [ ] No primitives library code introduced
- [ ] No real `ApiCallRegistry` (stub only)
- [ ] No regressions in existing `agent-canvas` tests

## Out of Scope
- Iframe runtime, esbuild-wasm, capability broker → Plans 03–04
- `ApiCallRegistry` real implementation → Plan 02
- Eager / heavy primitives library → Plans 05–06
- Iframe pool → Plan 07
- Contracts pipeline → Plan 08
- Per-card `position` / `size` persistence in store — using path (a) from spec §10.4: layout calculator handles `initialSize`, FloatingPanel owns user-driven dimensions
- Widget persistence across `session.resume` → Hermes spec §11.4 (deferred)

---

## Claude Code Handoff (paste this prompt to execute the plan)

```
Read the implementation plan in .anandia/plans/widget-runtime/01-wire-contract-foundation.md.

Your task:
1. FIRST: Write the test files from the "Domain Tests" section (Step 1 in Implementation Order). Each test group becomes its own test file. Use Vitest for TS, Cargo's #[cfg(test)] for Rust. Do NOT implement any production code yet.
2. Run the tests: `bun run test` and `cd src-tauri && cargo test`. They should all FAIL (red). If any pass, the test isn't testing anything meaningful — rewrite it.
3. THEN: Implement Steps 2-9 in order. Run tests after each step. Stop and verify green before moving on.
4. After all tests pass, run the full verification (Step 10): `bun run typecheck`, `bun run test`, `cargo test`, `cargo check`, `bun run test:e2e --project=chromium`.
5. Each step ends with a commit using the message provided in the plan and the Co-Authored-By trailer per CLAUDE.md.

Rules:
- Do NOT modify test assertions to make them pass. Fix the implementation instead.
- Do NOT skip tests. If a test seems wrong, tell me which one and why.
- The WHY comments encode domain intent — read them. They explain why the test exists, not just what it asserts.
- For TS: bun, NOT npm. `bun run test`, NOT `bun test`.
- This plan ends with a stub AgentWidgetCard and a stub apiCallRegistry. The real iframe and registry come in Plans 03 / 02 respectively. Do not pre-implement them.
```
