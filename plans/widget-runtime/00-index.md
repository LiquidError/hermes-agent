# Tauri Agent Widget Runtime — Implementation Plan Index

## Source spec
`/plans/tauri-agent-widget-runtime-spec.md` (v2). Authoritative for wire shape, domain model, and behavior tests in §16. The spec was reviewed against actual repo state (commit `367bbb8f` "feat: added hermes agent canvas features") on 2026-04-29 and patched to match what's there.

## Why a multi-plan
The spec describes ~3000 LOC of new code across 8 logical surfaces (bootstrap iframe, esbuild-wasm pipeline, broker, pool, primitives, widget card, contracts pipeline, wire integration). The planning skill caps individual plans at ~500 lines. Each sub-plan below is independently testable and commits-as-you-go.

## Scope boundary
Decisions made up front (per spec §17 alternatives — override here if needed):
- **Single iframe per card** to start. Warm pool deferred to Plan 07 once the rest of the runtime is stable.
- **Heavy primitives in scope** as lazy-loaded chunks (Chart, Table, DnDList, RichTextEditor, CodeEditor, MarkdownView).
- **Streaming render** (`widget.render.chunk`) deferred — namespace reserved (spec §6.2).
- **Widget persistence across `session.resume`** deferred (Hermes spec §11.4).
- **Per-call cancellation API** on `canvasAPI` deferred (spec §15.12). Card-level disposal/update cancellation is in.

## Sub-plan sequence

| # | File | Status | Purpose | Critical-path? |
|---|------|--------|---------|---|
| 01 | `01-wire-contract-foundation.md` | **fleshed** | TS event types + store extension + `useAgentWidgets` hook + canvas dispatch branch + Rust `hermes_emit_event` command + mockHermes parity. Stub `AgentWidgetCard`. | Yes — unblocks every other plan |
| 02 | `02-api-call-registry.md` | **fleshed** | `ApiCallRegistry` host-side singleton: invoke / resolve / reject / cancelByCard / cancelBySession / cancelByCorrelation. Pure logic; no iframe. | Yes |
| 03 | `03-bootstrap-iframe.md` | **fleshed** | Bootstrap HTML asset: React + esbuild-wasm + `canvasAPI` proxy + error boundary + theme propagation. Compile pipeline + postMessage protocol. | Yes |
| 04 | `04-broker-and-widget-card.md` | **fleshed** | Capability broker (host-side dispatch, Zod validation) + real `<AgentWidgetCard>` lifecycle with single iframe (no pool). Replaces stub from Plan 01. | Yes |
| 05 | `05-eager-primitives.md` | **stub — OK to flesh now** | Layout / text / form / action / feedback / display primitives (~30 components, eager bundle ≤ 200 KB). | No |
| 06 | `06-heavy-primitives.md` | stub — flesh after a `<Chart>` lazy-load PoC inside a real iframe | `<Chart>` (recharts), `<Table>` (TanStack Table), `<DnDList>` / `<KanbanBoard>` (dnd-kit), `<RichTextEditor>` (tiptap), `<CodeEditor>` (CodeMirror 6), `<MarkdownView>` (marked + sanitize). Lazy chunks ≤ 200 KB each. | No |
| 07 | `07-iframe-pool.md` | stub — DO NOT flesh until you have measurements | Warm pool, recycling, error-discard, warmup target < 500 ms. Replaces single-iframe path from Plan 04. | No (perf polish) |
| 08 | `08-contracts-pipeline.md` | stub — flesh once Plans 05 + 06 are merged and stable | `bun run contracts:generate` script + 6 starter `.tsx` examples + version-comment in generated `.d.ts`. Hands off to Hermes side. | No (handoff) |

**Critical path** for the **demoable end-to-end milestone** ("agent emits widget.render with real JSX, the user sees a rendered React tree in a sandboxed iframe, hermes.ask round-trips, disposal is clean"): **01 → 02 → 03 → 04**. Plans 02 and 03 are independent of each other — after Plan 01 is done, they can be developed in parallel branches.

### Why each unfleshed plan is unfleshed (don't drift to "wait for feedback" as a reflex)

- **Plan 05 — OK to flesh now.** The case to wait is weak. Each primitive is testable in isolation against well-defined props. The only risk of pre-planning is edit churn as agent usage reveals prop gaps (e.g. `<Button loading>`) or dead-weight primitives. That churn is cheap.
- **Plan 06 — wait for a concrete measurement.** Lazy-loading dynamic `import()` inside a sandboxed iframe with `srcDoc` has real engineering unknowns: chunk URL resolution under null origin, CSP blob: allowance, skeleton-during-load latency. Run a 1-day `<Chart>` lazy-load PoC against the Plan 03 bootstrap before writing the plan. Also: the scope question (drop tiptap/CodeMirror?) is a real product call that needs agent-attempt evidence, not a coin flip.
- **Plan 07 — needs numbers, not specs.** Spec §17 itself defers this. Pool sizing, recycle heuristics, and memory budgets are measurement-driven. Single-iframe per card from Plan 04 ships fine; pool tuning happens once you can record cold-mount latency under multi-card bursts and per-iframe RSS.
- **Plan 08 — mechanical dependency.** The contracts pipeline generates `.d.ts` from the primitives library. You can't ship a generator against a moving target. Wait until 05 and 06 are merged.

If a future session is told "should I plan 05 now?" the answer is **yes, plan it**. The reflex "wait for execution feedback" is wrong here.

## Conventions (from CLAUDE.md)
- **Tests**: `bun run test` (NOT `bun test`). Watch: `bun run test:watch`. Coverage: `bun run test:coverage` (80% threshold).
- **Type check**: `bun run typecheck`.
- **Rust**: `cd src-tauri && cargo test` and `cargo check`.
- **E2E**: `bun run test:e2e --project=chromium`.
- **Path alias**: `@/` = `apps/desktop/anandia-workspace/src/`.
- **Path-validation, error handling, accessibility patterns**: see CLAUDE.md "Established Patterns" section. Apply to all new code.

## TDD discipline (read the handoff prompt at the bottom of every plan)
1. Write the test files from the "Domain Tests" section first. Each test group → its own file. **All tests must fail (red) before any production code is written.**
2. If a test passes before implementation, the test is wrong — rewrite it to actually exercise the assertion.
3. Implement following "Implementation Order". Run tests after each step.
4. Do NOT modify test assertions to make them pass. Fix the implementation.
5. Run `bun run typecheck` + `cargo check` + `bun run test` + `bun run test:e2e --project=chromium` after each plan; commit when green.

## Hermes-side handoff
`hermes-handoff.md` — note for the future Claude session that will plan from `hermes-widget-render-spec.md`. Read it before producing the Hermes-side plans so scope and wire-shape decisions stay aligned.
