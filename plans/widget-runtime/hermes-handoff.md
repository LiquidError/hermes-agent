# Hermes-Side Planning Handoff

> **Note for the future Claude session** that will produce the Hermes (Mac mini) implementation plan from `/plans/hermes-widget-render-spec.md`.
>
> This handoff was written by the same model in a previous session, after producing the Tauri-side plan. Read it before invoking the planning skill on the Hermes side so scope and wire-shape decisions stay aligned.

## What's already done on the Tauri side

A multi-plan TDD sequence was produced at `/plans/widget-runtime/`:
- `00-index.md` — sub-plan map, sequencing, scope decisions, conventions
- `01-wire-contract-foundation.md` — wire-contract types, store/hook integration, Rust `hermes_emit_event` command, mockHermes parity, stub `<AgentWidgetCard>`
- `02-api-call-registry.md` — host-side `ApiCallRegistry` with cancellation paths and 4106 surfacing
- `03-bootstrap-iframe.md` — bootstrap HTML asset, esbuild-wasm pipeline, `canvasAPI` proxy, error boundary, postMessage protocol
- `04-broker-and-widget-card.md` — capability broker + real `<AgentWidgetCard>` with single iframe; **demoable end-to-end milestone**
- Plans 05–08 (eager primitives, heavy primitives, iframe pool, contracts pipeline) are stubbed in the index. The index records concrete reasons for each (Plan 05 is fleshable now; Plan 06 needs a `<Chart>` lazy-load PoC; Plan 07 needs latency/memory measurements that don't exist yet; Plan 08 mechanically depends on 05 + 06 being stable). Do NOT defer them all to a generic "wait for feedback" — read the index.

After Plan 04 the Tauri side can demo a real widget end-to-end via mockHermes (no Hermes connection needed). The Hermes side has no critical-path dependency on Tauri Plans 02–04 — it can develop in parallel after Tauri Plan 01.

## Spec source

`/plans/hermes-widget-render-spec.md` (v2).

The wire contract in §3 is **shared verbatim** with the Tauri side. Do not redesign it. Both specs were patched on 2026-04-29 to match actual repo state and to fix a stale `-v2` filename reference (only in the Tauri spec).

## Scope — match the Tauri side exactly

The Tauri plan defers these (per spec §17 alternatives):
- **Streaming render** (`widget.render.chunk`) — namespace reserved (Hermes spec §6.2 — already resolved).
- **Widget persistence across `session.resume`** — Hermes spec §11.4 already defers this.
- **Per-call cancellation API on `canvasAPI`** — Tauri spec §15.12. Card-level disposal/update cancellation IS in scope.
- **Heavy primitives are a Tauri-side bundle decision** — Hermes side reads example `.tsx` files via `read_widget_example`; no Hermes-side scope decision needed.

The Hermes plan SHOULD include:
- All six widget tools per Hermes spec §5: `render_widget`, `widget_update`, `widget_message`, `widget_dispose`, `list_widget_examples`, `read_widget_example`.
- `WidgetRegistry` per session + `ApiCallRegistry` per session with idempotent disposal both sides.
- Hard 32 KiB cap on `widget.api_response` with error code `4106` enforced server-side BEFORE the response is emitted.
- `widget.api_cancel` cancellation flows (client → server and server → client; spec §3.5.4).
- Lean system-prompt addendum (~30 lines, heuristics only) loaded conditionally on the client advertising `widget.render` capability.
- At least 4 starter widget example `.tsx` files in `gateway/system_prompts/widget_examples/`.

## Open considerations the user should answer during your discovery phase

These are flagged in Hermes spec §11 as "for the planner to surface, not silently resolve". Ask the user about each before producing test specs:

1. **§11.1** — system prompt addendum location: single file vs directory of fragments composed conditionally vs skill. Depends on `gateway/system_prompts/` conventions in the Hermes repo.
2. **§11.2** — sync mechanism for `gateway/system_prompts/widget_examples/` from the Tauri-side `contracts/examples/`. Options: manual copy (current), git submodule, sync script. Pick during planning.
3. **§11.3** — approval gating for `hermes.ask` and `notes.save`. Verify against the existing tool-approval policy. Probably: `notes.save` follows tool-approval policy; `hermes.ask` is btw and unapproved.
4. **§11.5** — confirm that the existing `tui_gateway` dispatcher can absorb client-emitted events (`widget.mounted`, `widget.error`, `widget.disposed`, `widget.api_cancel`) using the same envelope shape as server-originated events with no `id` field. The Tauri side has already committed the wire shape (envelope `{ jsonrpc: "2.0", method: "event", params: { type, session_id, payload } }`).
5. **§11.7** — example tool visibility: always-on vs reveal-after-render. Depends on Hermes' tool-revealing mechanism.
6. **§11.8** — server-side cap on cancelled-but-still-running `prompt.btw` work. Probably out of scope.

## Cross-machine alignment checks the Hermes plan MUST include

Add a domain test for each (these are the same wire-shape both sides must agree on):
- **Error-code table 4101–4107 + 5101–5103** matches Tauri side exactly (Hermes spec §8).
- **Card ID format** `wgt_<6 hex>` is the same validator both sides — server allocates, client validates. Test that the server's allocator produces ids matching `/^wgt_[0-9a-f]{6}$/`.
- **`widget.api_response` 32 KiB cap** is enforced server-side BEFORE the response is emitted. The Tauri side has tests (Plan 02) that assume it never receives an oversized payload.
- **`widget.api_cancel`** (client → server) carries exactly `{ correlation_id, card_id, reason }`.
- **`client.hello` capability negotiation** registers / unregisters all six widget tools as a bundle conditional on `widget.render` capability — not individually.
- **Outbound client events from Tauri** (`widget.mounted`, `widget.error`, `widget.disposed`, `widget.api_cancel`) arrive at the gateway as `event`-shape messages with no `id` field. Test that the gateway accepts and routes them; this closes Hermes spec §11.5.

## Useful Tauri-side context for Hermes planning

- The Tauri side is the **source of truth** for `canvasAPI.d.ts` and `canvas-primitives.d.ts` (Tauri Plan 08). The Hermes side reads these via `read_widget_example`; sync is a build-time concern flagged in §11.2 above.
- **Card IDs are server-generated** (Hermes spec §3.1). Tauri validates format but does not allocate.
- **`hermes_emit_event` Tauri command** is the channel for client-originated `widget.*` events. Tauri Plan 01 implements this. The gateway must accept these as valid event-shape messages from the client side.
- The Tauri side's mockHermes (`apps/desktop/anandia-workspace/src/services/mockHermes.ts`) provides browser-mode parity: it can fake the Hermes side entirely. **The Tauri Plan 01 can ship and be demoed before any Hermes-side code lands.** This means the Hermes side has no critical-path dependency on Tauri Plan 04 — both can develop in parallel after Tauri Plan 01.

## Recommended Hermes-side plan-split structure

Mirror the Tauri side's foundation-first sequence:

| # | Purpose |
|---|---------|
| 01 | Tool registry + system-prompt addendum + capability negotiation. Register six widget tools conditional on `widget.render` capability advertisement; load lean prompt addendum. |
| 02 | `WidgetRegistry` + handler scaffolding for `render_widget` / `widget_update` / `widget_message` / `widget_dispose` tools, with idempotent disposal both sides. |
| 03 | `ApiCallRegistry` + async `widget.api_call` handler — accept/correlate/respond pattern, response-size cap with code `4106`. |
| 04 | `widget.api_cancel` flows — both directions, best-effort cancellation of `prompt.btw`. |
| 05 | `list_widget_examples` + `read_widget_example` tools + 4+ starter examples synced from Tauri-side `contracts/examples/`. |
| 06 | Client-emitted event acceptance — gateway dispatcher accepts `widget.mounted` / `widget.error` / `widget.disposed` / `widget.api_cancel` envelopes from clients; close §11.5. |

Each plan independently testable. Plans 01 → 02 → 03 → 04 are the critical path for "an agent can render and dispose a widget end-to-end with the Tauri client."

## How to invoke the planning skill on the Hermes side

```
Use the planning skill. The user wants a TDD implementation plan for the Hermes (Mac mini) side of the agent-widget runtime, sourced from apps/desktop/anandia-workspace/docs/plans/hermes-widget-render-spec.md.

Read .anandia/plans/widget-runtime/hermes-handoff.md first — it documents the scope alignment with the Tauri side, the open considerations to surface during discovery, and the cross-machine alignment tests to include.

Phase 1 (discovery): focus on the §11 open considerations listed in the handoff. Skip re-discovery of the wire shape — it's committed in §3 and shared with the Tauri side.

Output: write plans to .anandia/plans/hermes-widget-runtime/ following the same multi-plan structure the Tauri side used (00-index.md + 01-... + handoff back if useful). Match the scope decisions exactly.
```

## Final note

The Tauri side's spec was reviewed against the actual codebase (commit `367bbb8f`) on 2026-04-29 and patched in several places (FloatingPanel API, hermesService dispatch model, store-architecture decision, position/size storage model, Rust client-emitted-event path). The Hermes spec did NOT need patches — it doesn't make assumptions about Tauri internals, and its file-link paths are valid.

If the Hermes spec turns out to need updates against the Hermes/`gateway` codebase, do that pass FIRST (same way the Tauri side did) before producing plans. A spec that's wrong about its target repo will produce plans that don't apply.
