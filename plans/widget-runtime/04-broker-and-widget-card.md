# Implementation Plan 04: Capability Broker + Real AgentWidgetCard

> **Plan 04 of 8** in the widget-runtime sequence. Read `00-index.md` first. Depends on Plans 01, 02, 03 (all three must be complete and green).

## Context
This plan stitches Plans 01–03 together into the first **demoable milestone**: an agent emits `widget.render` with real JSX, the user sees a rendered React tree inside a sandboxed iframe, the card calls `canvasAPI.hermes.ask`, the response round-trips through `ApiCallRegistry`, and disposal is clean. After Plan 04 you can demonstrate the feature end-to-end. After Plan 04, the remaining work (Plans 05–08) is additive — not foundational.

Two new pieces here:
- **Capability broker** (host-side): receives `api.call` postMessages from iframes, validates against the card's allowlist, dispatches via a typed table. Hermes round-trips go through `ApiCallRegistry` (Plan 02). Local capabilities (`notes.save`, `storage.*`, `card.*`, `os.*`) are dispatched to host-side handlers.
- **Real `<AgentWidgetCard>`**: replaces the Plan 01 stub. Creates a fresh iframe per mount (no pool — Plan 07), loads the bootstrap from Plan 03, runs the init handshake, wires lifecycle to the broker.

Source spec: §6 (broker), §6.7 (idempotent disposal both sides), §8 (capability surface), §10 (`<AgentWidgetCard>`), §16.3 / §16.4 / §16.5 / §16.6 / §16.13 / §16.14 (test scenarios).

## Domain Model

```
CapabilityBroker (host-side singleton at @/runtime/agent-widgets/broker) {
  receive(messageEvent: MessageEvent, ctx: { cardId, sessionId, allowlist }): void
    // 1. verify event.source matches the card's iframe contentWindow
    // 2. parse data; if not api.call shape → drop
    // 3. if data.capability ∉ allowlist → reply api.result with error 4104,
    //    AND emit widget.error phase='capability'
    // 4. validate data.args against the capability's Zod schema → 4002 on fail
    // 5. dispatch via dispatch table; await result; reply api.result

  dispatch table:
    'hermes.ask'        → apiCallRegistry.invoke(sessionId, cardId, 'hermes.ask', args, msgId)
    'notes.save'        → noteService.save(args)  // Tauri-side existing service
    'storage.get'       → cardStorage.get(cardId, args.key)
    'storage.set'       → cardStorage.set(cardId, args.key, args.value)
    'storage.keys'      → cardStorage.keys(cardId)
    'card.resize'       → agentCanvasStore.resizeWidgetCard(cardId, args)
    'card.set_title'    → agentCanvasStore.setWidgetCardTitle(cardId, args.title)
    'card.close'        → agentCanvasStore.disposeWidgetCard({card_id: cardId, reason: 'card_closed_self'})
                           AND emitEvent('widget.disposed', sessionId, {card_id, reason})
    'os.notify'         → tauriInvoke('os_notify', args)         — stub OK if not implemented
    'os.copy_clipboard' → tauriInvoke('os_copy_clipboard', args) — stub OK if not implemented

  INVARIANT: every dispatch is per-card; cardId is taken from the registered iframe context, NOT from any field in the postMessage payload (the iframe could lie)
  INVARIANT: undeclared capability calls do NOT round-trip to Hermes (rejected locally with 4104)
  INVARIANT: malformed args (zod fail) reject with code 4002 before any dispatcher runs
}

CardStorage (host-side, browser localStorage with namespacing) {
  get(cardId, key): unknown | null
  set(cardId, key, value): void
  keys(cardId): string[]
  clearCard(cardId): void   // called on widget.disposed

  storage key prefix: `widget:${cardId}:${key}`
  size cap per key: 256 KB (validated; reject with 4002)
  total per card: 4 MB (track via JSON.stringify length sum)

  INVARIANT: card A cannot read card B's keys (prefix isolation enforced in `keys()`)
  INVARIANT: clearCard removes ALL keys with the card's prefix
}

AgentWidgetCard (component, replacing Plan 01 stub) {
  props: { cardId, source, capabilities, title, sessionId, onDispose }

  on mount:
    1. create iframe element with sandbox="allow-scripts", srcDoc=bootstrapHtml
    2. attach a postMessage listener filtered by event.source === iframe.contentWindow
    3. wait for {kind:'bootstrap.ready'} → post init with source/capabilities/cardId/themeTokens
    4. on {kind:'widget.mounted'} → emitEvent('widget.mounted', sessionId, {card_id, compile_ms, compiled_size})
    5. on {kind:'widget.error'} → emitEvent('widget.error', sessionId, {card_id, phase, kind, message, stack})
    6. on {kind:'api.call'} → broker.receive(event, {cardId, sessionId, allowlist: capabilities})
    7. on {kind:'widget.disposed'} → emitEvent('widget.disposed', sessionId, {card_id, reason})

  on widget.update for this cardId (via store subscription):
    - post {kind:'source.update', source, capabilities} to iframe
    - apiCallRegistry.cancelByCard(cardId, 'card_updated') — already done in useAgentWidgets per Plan 01

  on widget.message for this cardId:
    - post {kind:'message.push', payload} to iframe

  on unmount:
    1. apiCallRegistry.cancelByCard(cardId, 'card_disposed')
    2. cardStorage.clearCard(cardId)
    3. post {kind:'dispose', reason} to iframe (best-effort, no wait)
    4. emitEvent('widget.disposed', sessionId, {card_id, reason}) — only if not already emitted by iframe
    5. remove iframe element from DOM

  INVARIANT: idempotent disposal — if an outbound widget.disposed has already been emitted for this cardId in this lifecycle, do NOT re-emit (per spec §3.4 / §16.13)
  INVARIANT: iframe is created fresh per mount; on disposal the iframe is destroyed (no pool yet — Plan 07)
}
```

## Domain Tests

### A. Broker dispatch — happy path & allowlist (vitest, mocked iframe + mocked apiCallRegistry)

A1. GIVEN broker registered for cardId `wgt_aaa` with allowlist `['notes.save']`
    WHEN a synthetic `api.call` message arrives `{ id: 'm1', capability: 'notes.save', args: { title: 't', body: 'b' } }`
    THEN `noteService.save` is called once with `{ title: 't', body: 'b' }`
    AND on resolve `{ note_id: 'n1' }`, `iframe.contentWindow.postMessage` is called with `{ kind: 'api.result', id: 'm1', result: { note_id: 'n1' } }`
    WHY: spec §16.4 — broker is the sole path between iframe and services; routing must hit the right dispatcher and reply with the right id.

A2. GIVEN broker registered for cardId `wgt_aaa` with allowlist `['notes.save']` (NO `'hermes.ask'`)
    WHEN `api.call` arrives with `capability: 'hermes.ask'`
    THEN NO call is made to `apiCallRegistry.invoke`
    AND `iframe.postMessage` is called with `{ kind: 'api.result', id: <msgid>, error: { code: 4104, message: ... } }`
    AND `hermesService.emitEvent('widget.error', sessionId, { card_id, phase: 'capability', ... })` is called once
    WHY: spec §16.3 / §16.4 — undeclared calls are rejected LOCALLY; they must NEVER round-trip to Hermes; that's the whole point of declaration.

A3. GIVEN allowlist `['notes.save']` and a malformed `args` payload missing `body`
    WHEN `api.call` arrives
    THEN broker rejects with `{ code: 4002, message: <zod issue> }` BEFORE any dispatcher runs
    AND `noteService.save` is NOT called
    WHY: spec §6.3 — argument validation is a trust boundary; bad args must not reach service code.

A4. GIVEN broker registered with allowlist `['hermes.ask']`
    WHEN `api.call` arrives with `capability: 'hermes.ask'`, `args: { prompt: 'q' }`
    THEN `apiCallRegistry.invoke(sessionId, cardId, 'hermes.ask', { prompt: 'q' }, msgId)` is called
    AND when the registry's Promise later resolves with `{ answer: 'A' }`, `iframe.postMessage` is called with `api.result` carrying that result
    WHY: spec §16.5 — the async pattern is broker → registry → server → response → registry → iframe; this asserts the broker's role in the chain.

A5. GIVEN broker is processing a message
    WHEN the messageEvent.source does NOT match the registered iframe's contentWindow
    THEN the message is dropped silently
    AND no dispatcher runs
    WHY: spec §6.1 — origin verification is the trust boundary on the host side.

### B. Per-card storage (vitest)

B6. GIVEN cardStorage and cardId `wgt_aaa`
    WHEN `set('wgt_aaa', 'k', { x: 1 })` then `get('wgt_aaa', 'k')` is called
    THEN `get` returns `{ x: 1 }`
    WHY: round-trip works.

B7. GIVEN two cards `wgt_aaa` and `wgt_bbb` with values set under the same key `'k'`
    WHEN `keys('wgt_aaa')` is called
    THEN it returns only `['k']` for `wgt_aaa`'s namespace (not `wgt_bbb`'s)
    WHY: spec §8.4–§8.6 — cards must not see each other's storage.

B8. GIVEN `wgt_aaa` with 3 keys set
    WHEN `clearCard('wgt_aaa')` is called
    THEN `keys('wgt_aaa')` returns `[]`
    AND `wgt_bbb`'s keys (if any) are unaffected
    WHY: disposal must clean up storage; cross-card spillover is a memory leak.

B9. GIVEN a value `{ data: '<300 KB string>' }` (over 256 KB serialized)
    WHEN `set('wgt_aaa', 'k', value)` is called
    THEN it throws / rejects with code 4002
    AND no localStorage write happens
    WHY: spec §8.4 — per-key cap protects against agent dumping bloat.

### C. Real AgentWidgetCard — iframe lifecycle (vitest with happy-dom or jsdom; iframe behavior is shallow here, deep in Plan 03 e2e)

C10. GIVEN the component renders with valid props
     WHEN it mounts
     THEN exactly one `<iframe>` element is created with `sandbox="allow-scripts"` and `srcDoc` set to the imported `bootstrapHtml`
     AND a postMessage listener is registered
     WHY: minimum-viable mount sanity.

C11. GIVEN a mounted card and a synthetic `bootstrap.ready` message from its iframe
     WHEN the listener processes it
     THEN `iframe.contentWindow.postMessage` is called with `{ kind: 'init', source, capabilities, card_id, theme_tokens }`
     WHY: handshake — bootstrap won't compile until init arrives.

C12. GIVEN a mounted card
     WHEN a `widget.mounted` message arrives from its iframe
     THEN `hermesService.emitEvent('widget.mounted', sessionId, { card_id, compile_ms, compiled_size })` is called once
     WHY: server needs the mounted signal to resolve the agent's `render_widget` tool call.

C13. GIVEN a mounted card
     WHEN a `widget.error` (phase='compile') arrives from its iframe
     THEN `hermesService.emitEvent('widget.error', sessionId, { card_id, phase, kind, message, stack })` is called
     WHY: agent needs structured error to self-correct.

C14. GIVEN a mounted card
     WHEN the component unmounts (parent removes it)
     THEN `apiCallRegistry.cancelByCard(cardId, 'card_disposed')` is called
     AND `cardStorage.clearCard(cardId)` is called
     AND the iframe element is removed from the DOM
     AND `emitEvent('widget.disposed', sessionId, {card_id, reason})` is called exactly once
     WHY: spec §10.2 / §16.14 — disposal cleanup must run in the right order; storage cleanup matters.

C15. GIVEN a mounted card whose iframe has ALREADY emitted `widget.disposed` (e.g., card.close from inside)
     WHEN the component then unmounts
     THEN `emitEvent('widget.disposed', ...)` is NOT called a second time
     WHY: spec §3.4 / §16.13 — idempotent both sides; double-emit creates phantom server state.

### D. End-to-end demo (Playwright e2e — `tests/e2e/widget-end-to-end.spec.ts`)

D16. GIVEN a Tauri app session with a fake Hermes (mockHermes-driven) emitting `widget.render` with source that calls `canvasAPI.hermes.ask` from a button click and renders the answer
     AND the mock responds to `widget.api_call` after 200 ms with `{ result: { answer: 'mock answer' } }`
     WHEN the user clicks the button rendered inside the card
     THEN within 1 s the card's DOM shows the text `mock answer`
     WHY: this is THE end-to-end demo of the feature — round-trip iframe → broker → registry → mock server → response → iframe → render.

D17. GIVEN the same setup as D16 but the mock responds with error code 4106
     WHEN the user clicks the button
     THEN the card displays a card-author-defined error UI (e.g., "Response too long")
     AND the host page is not affected
     WHY: spec §16.16 — 4106 surfaces as a structured rejection; card UX handles it.

D18. GIVEN a mounted card with a pending `hermes.ask` call (correlation_id `corr_xyz`)
     WHEN the user closes the card via FloatingPanel's close button
     THEN within 200 ms `widget.api_cancel` for `corr_xyz` is observed in the mock's emitted-event log
     AND `widget.disposed` for the cardId is observed
     AND the iframe element is removed from the DOM
     WHY: spec §16.14 — disposal mid-flight cancels in-flight calls actively.

### E. Update preserves position, resets state, cancels old correlations (spec §16.6)

E19. GIVEN a mounted card at FloatingPanel position (x, y) with an in-flight `hermes.ask` (`corr_old`)
     WHEN a `widget.update` event arrives for this cardId with new source
     THEN within 100 ms `widget.api_cancel` for `corr_old` is emitted
     AND `source.update` postMessage is sent to the iframe
     AND the FloatingPanel's position is unchanged (the parent layout array reuses the entry)
     AND any new `widget.api_response` for `corr_old` arriving later is silently dropped
     WHY: spec §10.4 / §16.6 — position is the user's; state belongs to the card; old promises must be cancelled.

## Implementation Order

### Step 1 — Test files (RED)
- `apps/desktop/anandia-workspace/src/runtime/agent-widgets/broker.test.ts` — A1–A5
- `apps/desktop/anandia-workspace/src/runtime/agent-widgets/cardStorage.test.ts` — B6–B9
- Update `apps/desktop/anandia-workspace/src/components/agent-canvas/side-cards/AgentWidgetCard.test.tsx` — replace Plan 01 stub assertions with C10–C15
- `apps/desktop/anandia-workspace/tests/e2e/widget-end-to-end.spec.ts` — D16–D18 (extend the e2e fixture from Plan 03)
- Add E19 to either the AgentWidgetCard test or the useAgentWidgets test (split: emitEvent emission goes in AgentWidgetCard test; cancelByCard call already covered by Plan 01 B2)

Run; new tests fail. Plan 01–03 tests stay green.

Commit: `test(broker-widget-card): plan 04 failing test scaffolding`.

### Step 2 — Capability schemas (Zod)
Create `src/runtime/agent-widgets/capability-schemas.ts`:
- One Zod schema per capability matching spec §8 args. Examples:
  - `'hermes.ask'` → `z.object({ prompt: z.string().min(1) })`
  - `'notes.save'` → `z.object({ title: z.string().min(1), body: z.string(), tags: z.array(z.string()).optional() })`
  - `'storage.get'` → `z.object({ key: z.string().min(1) })`
  - `'storage.set'` → `z.object({ key: z.string().min(1), value: z.unknown() })`
  - `'card.resize'` → `z.object({ w: z.number().int().positive(), h: z.number().int().positive() })`
  - etc.
- Export a `CAPABILITY_SCHEMAS: Record<Capability, ZodSchema>` lookup.

Commit: `feat(broker): zod schemas for capability args`.

### Step 3 — `cardStorage` module
Create `src/runtime/agent-widgets/cardStorage.ts`:
- `get/set/keys/clearCard` over `localStorage` with `widget:${cardId}:${key}` prefix.
- Validate per-key serialized size ≤ 256 KB; throw structured error on overflow.
- Track per-card total size: maintain a metadata key `widget:${cardId}:__meta__` with running byte total; reject set if would exceed 4 MB.

Tests B6–B9 → green.

Commit: `feat(card-storage): per-card kv with prefix isolation and size caps`.

### Step 4 — Capability broker
Create `src/runtime/agent-widgets/broker.ts`:
- Export `createBroker({ apiCallRegistry, noteService, cardStorage, agentCanvasStore, hermesService, tauriInvoke })`.
- Method `handleApiCall(messageEvent, { cardId, sessionId, allowlist, iframe })`:
  - Verify `messageEvent.source === iframe.contentWindow`; drop otherwise.
  - Allowlist check → reply 4104.
  - Zod validate → reply 4002 on fail.
  - Dispatch via switch:
    - `'hermes.ask'` → `apiCallRegistry.invoke(...)`
    - `'notes.save'` → `noteService.save(...)` (use existing service if present; else stub that returns `{ note_id: 'stub' }` and a TODO log)
    - `'storage.*'` → `cardStorage.*`
    - `'card.resize'` → `agentCanvasStore.getState().resizeWidgetCard(cardId, args)` — needs new store action; add it.
    - `'card.set_title'` → similar new store action
    - `'card.close'` → `agentCanvasStore.getState().disposeWidgetCard({ card_id: cardId, reason: 'card_closed_self' })` + emitEvent
    - `'os.notify'` / `'os.copy_clipboard'` → `tauriInvoke('os_notify' / 'os_copy_clipboard', args)` — stub if Rust commands don't exist; mark as TODO
  - Resolve to `api.result` postMessage with `result` or `error`.

Tests A1–A5 → green.

For `noteService` and `os.*` tauri commands: if not present in repo today, stub the dispatcher and add a TODO comment + a unit test that the stub is called. Do not invent new Rust services in this plan.

Commit: `feat(broker): capability dispatch with allowlist + zod validation`.

### Step 5 — Store actions for card.resize / card.set_title
Edit `src/stores/agentCanvasStore.ts`:
- Add `resizeWidgetCard(cardId, { w, h })` and `setWidgetCardTitle(cardId, title)`.
- These mutate the WidgetSideCard variant in place via Immer; AgentCanvas re-renders consume them.
- For now, `card.resize` updates a `size?: { w, h }` field on the WidgetSideCard (add to type if not present from Plan 01); the layout array in AgentCanvas honors it.

Commit: `feat(store): widget card resize/title actions`.

### Step 6 — Real `AgentWidgetCard`
Replace `src/components/agent-canvas/side-cards/AgentWidgetCard.tsx`:
- `useEffect` on mount: create iframe; attach postMessage listener; track ready/disposed flags via `useRef`.
- Listener body: switch on `data.kind` per the postMessage protocol from Plan 03; route `api.call` to `broker.handleApiCall`; route lifecycle events to `hermesService.emitEvent`.
- Subscribe to store updates for the cardId — react to `widget.update` (post `source.update`) and `widget.message` (post `message.push`).
- On unmount: cancellation + storage clear + dispose post + emitEvent + iframe removal, in the order from §10.2.
- Track an `alreadyEmittedDisposed` flag to skip the second emit if iframe got there first.
- Theme tokens: read from existing theme system (whichever store / context the canvas already uses); if none exists, hardcode default tokens with a TODO.

Tests C10–C15 → green.

Commit: `feat(side-cards): real AgentWidgetCard with iframe lifecycle and broker wiring`.

### Step 7 — Wire AgentCanvas layout to widget cards
Edit `src/components/agent-canvas/AgentCanvas.tsx`:
- The layout array (lines ~92–117) was extended in Plan 01 to include widget cards. Update it to honor the optional `size?: { w, h }` from Step 5: `width: card.size?.w ?? card.initialSize?.w ?? SIDE_CARD_WIDTH`, etc.
- The dispatch branch (Plan 01) already routes `card.kind === 'widget'` to `<AgentWidgetCard>`; verify it now passes `sessionId` (read from `useAgentCanvasStore.getState().activeSessionId`) — add the prop if Plan 01 didn't.

Commit: `feat(agent-canvas): honor widget card size from store + pass sessionId`.

### Step 8 — End-to-end Playwright tests
Extend `tests/e2e/widget-end-to-end.spec.ts` to drive the full mock flow per D16–D18 and E19:
- Set up a test page that mounts `<AgentCanvas>` with `mockHermes` driving widget events.
- Use the Plan 02 + 03 e2e infrastructure as a base.

Tests D16–D18, E19 → green.

Commit: `test(e2e): widget end-to-end demo flow`.

### Step 9 — Verification
Run:
- `bun run typecheck`
- `bun run test`
- `cd src-tauri && cargo test`
- `bun run test:e2e --project=chromium`

Manual smoke: launch the app in Vite-mode (`bun run dev`); verify mockHermes-driven widget.render mounts a real iframe with a real React tree; click a button; verify hermes.ask round-trips; close the card; verify clean teardown.

Commit: `chore(widget-runtime): plan 04 verification clean`.

## Acceptance Criteria
- [ ] All A1–A5, B6–B9, C10–C15, D16–D18, E19 domain tests pass
- [ ] `bun run typecheck`, `bun run test`, `cargo test`, `bun run test:e2e --project=chromium` all pass
- [ ] Coverage on `src/runtime/agent-widgets/broker.ts`, `cardStorage.ts`, `AgentWidgetCard.tsx` ≥ 80%
- [ ] **Demoable end-to-end**: in Vite-mode, mockHermes-driven widget.render mounts real JSX; hermes.ask round-trips; disposal is clean; no console errors
- [ ] Plan 01 stub `AgentWidgetCard` is fully replaced
- [ ] Plan 02 `apiCallRegistry` is wired through the broker for `hermes.ask`
- [ ] Plan 03 bootstrap is loaded by the real component (not just by e2e fixtures)
- [ ] No iframe pool — fresh iframe per mount (Plan 07 polish)
- [ ] No primitives library in use yet (Plan 03 stub still active; Plans 05/06 fill it in)

## Out of Scope
- Iframe pool / warm-up / recycling — Plan 07
- Eager primitives library — Plan 05
- Heavy primitives lazy chunks — Plan 06
- Contracts pipeline + .d.ts generation — Plan 08
- Real `os.notify` / `os.copy_clipboard` Rust commands (stub OK; may need their own micro-plan if the user wants them in scope)
- Real `notes.save` Rust integration (stub OK if not present; existing Tauri note service used if available)
- Per-call cancellation API on canvasAPI (deferred per spec §15.12)

---

## Claude Code Handoff (paste this prompt to execute)

```
Read the implementation plan in .anandia/plans/widget-runtime/04-broker-and-widget-card.md.

Plans 01, 02, AND 03 must all be complete and green. Verify by running `bun run test`, `cd src-tauri && cargo test`, and `bun run test:e2e --project=chromium` first. Tell me which red tests block before you start.

Your task:
1. FIRST: Write the test files from the "Domain Tests" section (Step 1). All new tests must FAIL (red); Plans 01-03 tests must remain GREEN.
2. THEN: Implement Steps 2-8 in order. Run tests after each step. The Playwright e2e in Step 8 is the demoable milestone — get it green before claiming done.
3. After all tests pass, run the full verification (Step 9), then do a manual smoke in Vite-mode (`bun run dev`).
4. Each step ends with a commit using the message provided in the plan plus the Co-Authored-By trailer per CLAUDE.md.

Rules:
- Do NOT modify test assertions to make them pass. Fix the implementation.
- The broker is the trust boundary for capability calls. Do NOT loosen the allowlist check or skip Zod validation. If a capability seems to need different semantics, stop and tell me.
- For dispatchers backed by services that don't exist yet (notes.save Rust impl, os.* commands), stub them with a TODO and a unit test asserting the stub is called — do NOT invent new Rust services in this plan.
- For TS: bun, NOT npm.
- After Plan 04, you have the demoable end-to-end milestone. The remaining plans (05-08) are additive: primitives library, heavy primitives, iframe pool, contracts pipeline.
```
