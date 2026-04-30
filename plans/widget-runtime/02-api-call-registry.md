# Implementation Plan 02: ApiCallRegistry

> **Plan 02 of 8** in the widget-runtime sequence. Read `00-index.md` first. Depends on Plan 01 (uses `useAgentWidgets`, `mockHermes` widget extensions, `hermesService.emitEvent`).

## Context
Plan 01 left a stub `apiCallRegistry` with no-op methods. This plan replaces it with the real implementation: a per-host singleton that owns the lifecycle of every pending `widget.api_call` correlation. Pure host-side logic; no iframe code. After this plan, `hermes.ask` round-trips work through the registry — but you can't *see* them yet because there's no iframe to make the call from. End-to-end demo lands in Plan 04.

Source spec: §6.5 (`ApiCallRegistry`), §6.6 (cancellation paths A/B/C), §8.1 (`hermes.ask` shape), §14.6 (4106 surfacing), §16.5 / §16.14 / §16.15 / §16.16 / §16.17 (test scenarios).

## Domain Model

```
PendingCall {
  correlationId: string    // corr_<6 hex>
  cardId: string           // wgt_<6 hex>
  sessionId: string
  capability: string       // 'hermes.ask' (only hermes.* capability in scope; hermes.stream deferred per spec §4.2)
  iframeMessageId: string  // the original api.call id from the iframe
  promise: { resolve, reject }
  startedAt: number        // ms epoch — for future timeout / observability

  INVARIANT: correlationId is unique across the registry at any moment
  INVARIANT: every pending entry has a corresponding outbound widget.api_call already sent
}

ApiCallRegistry (singleton at @/runtime/agent-widgets/apiCallRegistry) {
  invoke(sessionId, cardId, capability, args, iframeMessageId): Promise<unknown>
    // 1. mint correlation_id (corr_ + 6 hex)
    // 2. create PendingCall, store in map keyed by correlation_id
    // 3. hermesService.call('widget.api_call', { card_id, session_id, correlation_id, capability, args })
    // 4. on synchronous { accepted: false, error }: reject + remove + return
    // 5. on { accepted: true }: return the registered Promise (resolved later)

  resolve(correlationId, result): void
  reject(correlationId, error: { code: number; message: string }): void
  cancelByCard(cardId, reason): void
  cancelBySession(sessionId): void
  cancelByCorrelation(correlationId, reason): void
  handleResponse(payload): void  // dispatches to resolve/reject by inspecting payload
  pendingCount(): number         // for tests + observability
  clear(): void                  // for test setup; not used at runtime

  INVARIANT: cancellation is idempotent — calling cancelByCard twice for the same id is a no-op the second time
  INVARIANT: a late-arriving widget.api_response for a cancelled correlation is silently dropped
  INVARIANT: every cancellation path emits exactly one widget.api_cancel event per pending correlation
  INVARIANT: registry size returns to 0 after every entry is resolved/rejected/cancelled (no leaks)
}

CancellationError (rejection shape for cancelled Promises) {
  code: 'cancelled'           // string sentinel — distinct from numeric error codes 4xxx/5xxx
  reason: 'card_disposed' | 'card_updated' | 'session_ended' | 'server_initiated' | string
  cardId: string
  correlationId: string
}
```

## Domain Tests

### A. invoke happy path

A1. GIVEN an empty registry and `hermesService.call` mocked to return `{ accepted: true, correlation_id: <input> }`
    WHEN `registry.invoke('sess_x', 'wgt_abc123', 'hermes.ask', { prompt: 'q' }, 'iframe-msg-1')` is called
    THEN `hermesService.call` is called once with `'widget.api_call'` and the right shape (matching `{card_id, session_id, correlation_id, capability, args}`)
    AND the call returns a Promise that is still pending after the synchronous turn
    AND `registry.pendingCount()` returns 1
    WHY: invoke must register the entry BEFORE the response can arrive, so handleResponse can find it.

A2. GIVEN a registered PendingCall with `correlation_id='corr_aaa111'`
    WHEN `registry.handleResponse({ correlation_id: 'corr_aaa111', card_id: 'wgt_abc123', result: { answer: 'A' } })` is called
    THEN the originating Promise resolves with `{ answer: 'A' }`
    AND `registry.pendingCount()` returns 0
    WHY: the registry is the single arbiter of correlation→Promise mapping; correctness depends on this exact path.

A3. GIVEN `hermesService.call` mocked to return `{ accepted: false, error: { code: 4104, message: 'undeclared' } }`
    WHEN `registry.invoke(...)` is called
    THEN the returned Promise rejects with `{ code: 4104, message: 'undeclared' }`
    AND `pendingCount()` returns 0 (entry was never persisted, OR persisted-then-removed)
    WHY: server-side validation failure during ack is a synchronous reject; no later widget.api_response can arrive.

### B. error / 4106 rejection

B4. GIVEN a registered PendingCall with `correlation_id='corr_bbb222'`
    WHEN `handleResponse({ correlation_id: 'corr_bbb222', card_id: 'wgt_abc123', error: { code: 4106, message: 'Response exceeded 32 KiB cap', actual_size: 50432 } })` is called
    THEN the originating Promise rejects with an error carrying `code: 4106` AND the message AND `actual_size: 50432`
    AND `pendingCount()` returns 0
    WHY: spec §3.5.3 / §14.6 — 4106 is the wire-level overflow signal; the registry must surface it as a structured rejection, NOT swallow it, NOT truncate, NOT retry.

B5. GIVEN a registered PendingCall
    WHEN `handleResponse` arrives with both `result` and `error` set (malformed payload)
    THEN the Promise rejects with a defensive parse-error
    AND the entry is still removed from the registry
    WHY: ambiguous payload should fail loud; leaving the entry stranded is a leak.

### C. cancellation paths (spec §6.6)

C6. GIVEN 2 pending entries for `card_id='wgt_aaa'` (correlations `corr_1` and `corr_2`) and 1 pending for `wgt_bbb` (`corr_3`)
    WHEN `registry.cancelByCard('wgt_aaa', 'card_updated')` is called
    THEN the Promises for `corr_1` and `corr_2` reject with `CancellationError` (code='cancelled', reason='card_updated', cardId='wgt_aaa')
    AND `corr_3`'s Promise is still pending
    AND `hermesService.emitEvent('widget.api_cancel', sessionId, ...)` was called exactly twice with `corr_1` and `corr_2` in any order
    AND `pendingCount()` returns 1
    WHY: spec §16.6 / §16.14 — disposal/update cancels OWN cards' calls actively, leaves others untouched, and signals the server so it can stop the work.

C7. GIVEN 1 pending entry for `corr_4`
    WHEN `cancelByCorrelation('corr_4', 'server_initiated')` is called
    THEN the Promise rejects with `CancellationError` (reason='server_initiated')
    AND `hermesService.emitEvent('widget.api_cancel', ...)` is NOT called for `corr_4`
    AND `pendingCount()` returns 0
    WHY: spec §6.6 path C — server-initiated cancellation does NOT echo the cancel back to the server (avoid loops).

C8. GIVEN 3 pending entries across 2 sessions (`sess_x` × 2, `sess_y` × 1)
    WHEN `cancelBySession('sess_x', 'session_ended')` is called
    THEN the 2 entries in `sess_x` reject with `CancellationError`
    AND the entry in `sess_y` is still pending
    AND `pendingCount()` returns 1
    WHY: per-session cleanup on disconnect must not affect other sessions.

C9. GIVEN a pending entry that was just cancelled via `cancelByCard`
    WHEN `handleResponse` later arrives with the same correlation_id
    THEN the response is silently dropped
    AND no error is thrown
    AND no Promise resolves (because the original Promise was already rejected)
    WHY: spec §6.6 final paragraph — cancellation is fire-and-forget; late responses must be dropped, not crash.

C10. GIVEN no pending entries for `card_id='wgt_xxx'`
     WHEN `cancelByCard('wgt_xxx', 'card_disposed')` is called
     THEN no error is thrown
     AND `hermesService.emitEvent` is NOT called
     AND `pendingCount()` is unchanged
     WHY: cancellation is idempotent; a no-op on empty input must not produce phantom api_cancel emissions.

C11. GIVEN a pending entry that was cancelled
     WHEN `cancelByCard` is called again for the same cardId
     THEN it is a no-op (no second emission, no double-reject)
     WHY: same idempotency as C10 but for the second-call case.

### D. leak resistance (spec §16.17)

D12. GIVEN 100 invokes (all left pending) followed by `cancelBySession` for the shared sessionId
     WHEN cleanup completes
     THEN `pendingCount()` returns 0
     AND every Promise has settled (resolved or rejected)
     AND no entry remains in the internal map
     WHY: the registry is a long-lived singleton; leaks compound across a session.

D13. GIVEN registry.invoke is called and the Promise is awaited to resolution
     WHEN the entry is resolved
     THEN no internal reference to the resolved value remains in the registry's map
     AND `pendingCount()` returns 0
     WHY: settled entries must be removed, not just marked done. (Test by asserting map size, not by GC.)

### E. Plan 01 wiring (real registry replaces stub)

E14. GIVEN `useAgentWidgets` mounted (Plan 01)
     WHEN a `widget.api_response` event arrives via `hermesService.onEvent`
     THEN `apiCallRegistry.handleResponse(payload)` is called once with the payload
     WHY: Plan 01's stub recorded the call; now the real registry must actually settle the Promise.

E15. GIVEN `useAgentWidgets` mounted with a pending `corr_5` for `wgt_aaa`
     WHEN a `widget.api_cancel` event (server → client) arrives with `correlation_id='corr_5'`
     THEN `apiCallRegistry.cancelByCorrelation('corr_5', payload.reason)` is called
     AND the originating Promise rejects
     WHY: closes the integration loop between Plan 01's hook and the real registry.

E16. GIVEN browser-mode dev (mockHermes)
     WHEN `mockHermes` is configured to respond to `widget.api_call` with an error code 4106 after 100 ms
     AND a card calls `apiCallRegistry.invoke` for that capability
     THEN the returned Promise rejects with `{ code: 4106, ... }` after ~100 ms
     WHY: the mock must support error-shape responses for end-to-end browser-mode testing of 4106 surfacing.

## Implementation Order

### Step 1 — Test files (RED)
- `apps/desktop/anandia-workspace/src/runtime/agent-widgets/apiCallRegistry.test.ts` — tests A1–A3, B4–B5, C6–C11, D12–D13
- Update `apps/desktop/anandia-workspace/src/hooks/useAgentWidgets.test.ts` — add E14, E15 (replace the Plan 01 spy assertions with real-registry behavior assertions)
- Update `apps/desktop/anandia-workspace/src/services/mockHermes.widget.test.ts` — add E16

Run; confirm all new tests fail. Existing Plan 01 tests must still pass since the registry is additive at this point.

Commit: `test(api-call-registry): plan 02 failing test scaffolding`.

### Step 2 — Real `ApiCallRegistry`
Replace `src/runtime/agent-widgets/apiCallRegistry.ts` (currently the Plan 01 stub):
- Define `PendingCall`, `CancellationError`, `CapabilityErrorPayload` types.
- Implement `invoke` using `hermesService.call('widget.api_call', ...)`. Mint `correlation_id` via a small helper (`'corr_' + 6 hex chars` from `crypto.getRandomValues`).
- Map storage: `Map<correlationId, PendingCall>`. Index by `cardId` and `sessionId` either via secondary maps OR via linear scan in `cancelByCard` / `cancelBySession` (linear scan is fine — the spec performance budget §13 has no SLA on cancellation; pendingCount is bounded by number of widgets × in-flight asks).
- `handleResponse(payload)` dispatches to `resolve` or `reject` based on `result` vs `error`.
- `cancelBy*` methods iterate, reject Promises with `CancellationError`, emit `widget.api_cancel` (except in `cancelByCorrelation` per C7), remove entries.

Tests A1–A3, B4–B5, C6–C11, D12–D13 → green.

Commit: `feat(api-call-registry): real implementation with correlation tracking and cancellation paths`.

### Step 3 — `useAgentWidgets` integration
Edit `src/hooks/useAgentWidgets.ts`: the `apiCallRegistry` import is the same path; just remove the no-op stub now that the real impl lives there. Verify the hook's `widget.api_response` and `widget.api_cancel` branches call into the real registry methods.

Tests E14, E15 → green.

Commit: `feat(useAgentWidgets): wire real apiCallRegistry`.

### Step 4 — `mockHermes` 4106 support
Edit `src/services/mockHermes.ts`:
- Extend the `widget.api_call` interceptor: accept an optional configuration that lets a test request a synthetic error response (e.g. `mockHermes.configureWidgetApiCall({ errorCode: 4106, errorMessage: '...', actual_size: 50432 })`) for the next call.
- After the configured delay, emit a `widget.api_response` envelope with `error` set instead of `result`.

Test E16 → green.

Commit: `feat(mock-hermes): support synthetic error responses for widget.api_call`.

### Step 5 — Verification
Run:
- `bun run typecheck`
- `bun run test`
- `cd src-tauri && cargo test`
- `bun run test:e2e --project=chromium`

Coverage on `apiCallRegistry.ts` ≥ 80%. Confirm acceptance criteria.

Commit: `chore(widget-runtime): plan 02 verification clean`.

## Acceptance Criteria
- [ ] All A1–A3, B4–B5, C6–C11, D12–D13, E14–E16 domain tests pass
- [ ] `bun run typecheck` passes
- [ ] `bun run test`, `cargo test`, `bun run test:e2e --project=chromium` pass with no new failures
- [ ] Coverage on `apiCallRegistry.ts` ≥ 80%
- [ ] No iframe code introduced
- [ ] Plan 01's stub `apiCallRegistry` is fully replaced; Plan 01 tests still pass
- [ ] `pendingCount()` is exposed and returns 0 after every test cleanup
- [ ] No memory leaks: stress test (D12) shows registry size returns to 0 after 100-invoke cancellation cycle

## Out of Scope
- Iframe code — Plan 03 / 04
- Capability broker (host-side dispatch table) — Plan 04
- `hermes.stream` capability (deferred per spec §4.2)
- Per-call cancellation API exposed to iframe (deferred per spec §15.12)
- Persistence of `ApiCallRegistry` across Tauri-app crash (deferred per spec §15.13)
- Server-side cancellation timeout for `prompt.btw` (Hermes spec §11.8 — out of repo)

---

## Claude Code Handoff (paste this prompt to execute)

```
Read the implementation plan in .anandia/plans/widget-runtime/02-api-call-registry.md.

Plan 01 is already complete; verify by running `bun run test` first — Plan 01's tests must all pass before you start. If they don't, stop and tell me which ones are red.

Your task:
1. FIRST: Write the test files from the "Domain Tests" section (Step 1). All NEW tests must FAIL (red); Plan 01 tests must remain GREEN.
2. THEN: Implement Steps 2-4 in order. Run tests after each step.
3. After all tests pass, run the full verification (Step 5): typecheck, test, cargo test, e2e.
4. Each step ends with a commit using the message provided in the plan plus the Co-Authored-By trailer per CLAUDE.md.

Rules:
- Do NOT modify test assertions to make them pass. Fix the implementation.
- Do NOT modify Plan 01 tests; if a Plan 01 test goes red, that's a regression — investigate.
- The WHY comments encode domain intent. Read them.
- For TS: bun, NOT npm. `bun run test`, NOT `bun test`.
- This plan ends with a working ApiCallRegistry but NO iframe yet. The end-to-end demo lands in Plan 04.
```
