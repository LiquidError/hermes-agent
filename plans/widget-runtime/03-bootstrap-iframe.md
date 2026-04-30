# Implementation Plan 03: Bootstrap Iframe + esbuild-wasm + canvasAPI proxy

> **Plan 03 of 8** in the widget-runtime sequence. Read `00-index.md` first. Independent of Plan 02 — both can be developed in parallel after Plan 01.

## Context
Plans 01–02 built the host-side wiring. This plan ships the **iframe-side trust container**: a single HTML document that loads React + esbuild-wasm + a `canvasAPI` proxy + an error boundary, compiles agent JSX at runtime, and mounts it. The bootstrap is the only trusted asset shipped *into* the untrusted sandbox; it's audited and vendored verbatim.

This plan does NOT integrate with the host yet. It builds and tests the bootstrap as an isolated artifact: given an `init` postMessage, it compiles, mounts, and posts back lifecycle events. The real `<AgentWidgetCard>` host component that uses this bootstrap lands in Plan 04.

**Test split:** unit tests (vitest) for controller logic that doesn't need a real iframe; e2e tests (playwright) for behavior that requires a real browser iframe + real esbuild-wasm execution. The project already has Playwright in `tests/e2e/`.

Source spec: §4 (bootstrap), §5 (esbuild-wasm pipeline), §6.1 (postMessage protocol), §16.2 / §16.10 / §16.11 / §16.12 (test scenarios).

## Domain Model

```
Bootstrap (single HTML document, vendored as a string literal in src/runtime/agent-widgets/bootstrap.ts) {
  contains:
    - React 19 + ReactDOM (eager, ESM build)
    - esbuild-wasm (~2 MB; loaded once on first compile, reused thereafter)
    - capabilityAPI proxy generator
    - error boundary component
    - bootstrap controller (postMessage handler + lifecycle)
    - empty primitives stub at virtual import 'canvas-primitives' (Plans 05/06 fill this)

  INVARIANT: sandbox attribute on the host iframe is exactly "allow-scripts" — no other tokens
  INVARIANT: CSP forbids connect-src, external script-src, external img-src/font-src
  INVARIANT: postMessage origin is verified against the parent window ref; foreign messages logged + dropped
  INVARIANT: source > 256 KiB is rejected before compile (code 4102)
}

CanvasAPIProxy (window.canvasAPI inside iframe, generated per-card from declared capabilities) {
  hermes.ask(prompt: string): Promise<string>
  notes.save({title, body, tags?}): Promise<{note_id}>
  storage.get(key): Promise<unknown | null>
  storage.set(key, value): Promise<void>
  storage.keys(): Promise<string[]>
  card.resize({w, h}): Promise<void>
  card.set_title(title: string): Promise<void>
  card.close(reason?: string): Promise<void>
  os.notify({title?, body}): Promise<void>
  os.copy_clipboard(text: string): Promise<void>
  onMessage(handler): () => void

  INVARIANT: every method posts a uniquely-id'd `api.call` message and returns a Promise resolved on `api.result`
  INVARIANT: calling an UNDECLARED capability rejects locally with `{code: 4104, message: 'undeclared capability'}` — does NOT post to host
  INVARIANT: window.canvasAPI is frozen; agent source cannot replace methods
}

CompilePipeline (inside iframe) {
  input: source string (≤ 256 KiB)
  output: ES Module string (or compile error)
  steps:
    1. Wrap: `import * as primitives from 'canvas-primitives'; ${source}; export { default } from './source'`
    2. Resolve: 'canvas-primitives' → primitives bundle, 'react' → React UMD, anything else → unknown_import error
    3. esbuild compile to ESM
    4. Blob URL + dynamic import
    5. Mount with ReactDOM.createRoot inside the error boundary
    6. Post widget.mounted

  INVARIANT: compile errors and runtime errors both produce widget.error with the right phase
  INVARIANT: heavy primitive imports (Chart, RichTextEditor, etc.) resolve to lazy proxies — Plans 06 wire the real chunks
}

PostMessage protocol (per spec §6.1) {
  iframe → host:
    | { kind: 'bootstrap.ready' }
    | { kind: 'widget.mounted'; compile_ms: number; compiled_size: number }
    | { kind: 'widget.error'; phase, kind_, message, stack? }
    | { kind: 'widget.disposed'; reason: string }
    | { kind: 'api.call'; id: string; capability: string; args: unknown }
    | { kind: 'recycle.ready' }            // Plan 07 — currently emitted but ignored

  host → iframe:
    | { kind: 'init'; source, capabilities, card_id, theme_tokens, initial_message? }
    | { kind: 'source.update'; source, capabilities }
    | { kind: 'message.push'; payload }
    | { kind: 'api.result'; id; result? | error? }
    | { kind: 'theme.update'; theme_tokens }
    | { kind: 'dispose'; reason }
}
```

## Domain Tests

### A. Bootstrap controller — postMessage shape (vitest, no real iframe)

A1. GIVEN the bootstrap controller module is imported in isolation (extracted into a testable function `createBootstrapController(window)`)
    WHEN the controller is initialized
    THEN it posts `{ kind: 'bootstrap.ready' }` to its parent window via `parent.postMessage`
    WHY: the host must receive a deterministic ready signal before posting init.

A2. GIVEN the controller has received `bootstrap.ready` ack and the parent window posts `{ kind: 'init', source: '...', capabilities: ['hermes.ask'], card_id: 'wgt_abc', theme_tokens: {...} }`
    WHEN the message is processed
    THEN the source string is forwarded to a compile function (mocked in this test)
    AND the capabilities array is forwarded to a canvasAPI proxy generator (mocked)
    WHY: init is the trigger for the entire compile/mount/proxy pipeline.

A3. GIVEN a postMessage from a window OTHER than the registered parent (e.g., a dummy window)
    WHEN the message is received
    THEN it is silently dropped (logged as a warning, no compile, no mount)
    WHY: spec §6.1 — origin verification is the trust boundary; foreign messages must not enter the controller.

### B. CanvasAPI proxy — capability-aware generation (vitest)

B4. GIVEN `createCanvasAPI({ capabilities: ['hermes.ask'], cardId: 'wgt_abc' })`
    WHEN the proxy is queried
    THEN `canvasAPI.hermes.ask` is a callable function
    AND `canvasAPI.notes.save` exists but throws / rejects with `{ code: 4104, message: 'undeclared capability' }` when called
    WHY: spec §3.1 + §16.4 — undeclared calls are rejected LOCALLY at the iframe; never reach host.

B5. GIVEN `createCanvasAPI({ capabilities: ['hermes.ask'], cardId: 'wgt_abc', postCall: spy })`
    WHEN `canvasAPI.hermes.ask('prompt')` is called
    THEN `postCall` is invoked once with `{ kind: 'api.call', id: <unique>, capability: 'hermes.ask', args: { prompt: 'prompt' } }`
    AND a Promise is returned
    WHY: every declared call must post to host with a unique id for response correlation.

B6. GIVEN a pending Promise from `canvasAPI.notes.save(...)` registered via the proxy
    WHEN the proxy receives `{ kind: 'api.result', id: <matching>, result: { note_id: 'n1' } }`
    THEN the Promise resolves with `{ note_id: 'n1' }`
    AND the internal pending-call map for that id is cleared
    WHY: Promise leak inside the iframe = card-side memory leak; map cleanup matters.

B7. GIVEN a pending Promise from `canvasAPI.hermes.ask(...)`
    WHEN the proxy receives `{ kind: 'api.result', id: <matching>, error: { code: 4106, message: 'too large', actual_size: 50432 } }`
    THEN the Promise rejects with an error carrying `code: 4106`
    WHY: 4106 must surface to the card author's try/catch as a structured error (spec §14.6).

B8. GIVEN `createCanvasAPI({...})` is called twice (e.g. on source.update remount)
    WHEN the second instance is generated
    THEN any pending Promises from the first instance are rejected with `{code: 'cancelled', reason: 'remount'}` (or just abandoned to GC if the iframe re-mounts wholesale — pick one and assert it; spec §10.4 says state resets on remount)
    WHY: stale Promises from the previous mount must not deliver their results into the new mount's state.

### C. Compile pipeline — vitest unit tests (no real esbuild call; mock the wasm module)

C9. GIVEN a stub esbuild that returns ESM `'export default function() { return null; }'`
    AND source `'export default function() { return <div>x</div> }'`
    WHEN the compile pipeline runs
    THEN the wrapper is constructed correctly (synthetic module structure with primitives import)
    AND esbuild is invoked with the wrapped source
    AND a Blob URL is created from the result
    WHY: pipeline ordering is part of the contract — e.g., the primitives import must come before user source so user JSX can reference it.

C10. GIVEN source `'export default fn() { broken }'` (syntax error)
     AND a stub esbuild that throws `{ message: 'Unexpected token at line 1' }`
     WHEN the pipeline runs
     THEN no Blob URL is created
     AND a `widget.error` postMessage is emitted with `phase: 'compile'`, `kind: 'syntax_error'`, and the message
     WHY: spec §16.5 — compile errors must be self-correcting via the agent observing structured error reporting.

C11. GIVEN source > 256 KiB
     WHEN the pipeline validates input size
     THEN no compile attempt is made
     AND `widget.error` is emitted with `phase: 'validate'`, code or kind referencing 4102
     WHY: spec §16.11 — oversized payloads are usually agent inlining data; reject early without spending compile budget.

C12. GIVEN source that imports an unknown module (`import x from 'unknown-pkg'`)
     WHEN the pipeline tries to resolve
     THEN compile fails with `phase: 'compile'`, `kind: 'unknown_import'` and a useful message naming the module
     WHY: only `react` and `canvas-primitives` are resolvable; agent attempts to import elsewhere should be self-correcting.

### D. Theme propagation (vitest)

D13. GIVEN the bootstrap is initialized with `theme_tokens: { '--color-text-primary': '#1a1a1a' }`
     WHEN the controller applies them
     THEN the CSS variable `--color-text-primary` on the iframe's document.documentElement is `#1a1a1a`
     WHY: primitives reference these vars; auto-theming depends on them being present.

D14. GIVEN tokens already applied
     WHEN a `{ kind: 'theme.update', theme_tokens: { '--color-text-primary': '#fff' } }` arrives
     THEN the variable is updated to `#fff` within the same task
     WHY: spec §16.10 — light↔dark switches must propagate to live cards within 100 ms.

### E. End-to-end iframe behavior (Playwright e2e — `tests/e2e/widget-bootstrap.spec.ts`)

E15. GIVEN a real iframe loaded with the bootstrap srcDoc
     WHEN the page posts `{ kind: 'init', source: 'export default function() { return <div data-testid="hello">hi</div> }', capabilities: [], card_id: 'wgt_e2e01', theme_tokens: {} }`
     THEN within 500 ms the iframe posts `{ kind: 'widget.mounted', compile_ms: <number>, compiled_size: <number> }`
     AND the iframe's DOM contains an element with `data-testid="hello"` and text `hi`
     WHY: this is the smallest meaningful proof that the trust boundary works end-to-end with real esbuild + real React.

E16. GIVEN a real iframe with bootstrap loaded
     WHEN init is posted with source `'export default function() { return <div>{undef.x}</div> }'` (runtime ReferenceError)
     THEN the iframe mounts the error-boundary fallback (visible)
     AND posts `{ kind: 'widget.error', phase: 'runtime', kind_: <error name>, message: <message>, stack: <stack> }`
     WHY: error boundary catches lifecycle errors and reports up so Hermes can react.

E17. GIVEN a real iframe with bootstrap loaded
     WHEN init is posted with source attempting `window.parent.location` (sandbox-escape attempt)
     THEN the iframe throws SecurityError → caught by error boundary
     AND posts `widget.error` with `phase: 'runtime'`, kind referencing the SecurityError
     AND the host page (parent test page) is unaffected — its location, document, and DOM are unchanged
     WHY: spec §16.12 — this is the security claim of the entire design; it must hold.

E18. GIVEN a real iframe with bootstrap loaded and a card declared `['notes.save']`
     AND the parent test page mocks the broker by listening for `api.call` and replying with `{kind:'api.result', id, result: {note_id:'n_e2e'}}`
     WHEN source-mounted card calls `canvasAPI.notes.save({title:'t',body:'b'})`
     THEN the parent receives `api.call` with the right shape and id
     AND after the parent replies, the card's Promise resolves with `{note_id:'n_e2e'}`
     WHY: round-trip iframe ↔ parent works without the real broker; isolates this plan from Plan 04.

## Implementation Order

### Step 1 — Test files (RED)
- `apps/desktop/anandia-workspace/src/runtime/agent-widgets/bootstrap-controller.test.ts` — A1–A3
- `apps/desktop/anandia-workspace/src/runtime/agent-widgets/canvas-api-proxy.test.ts` — B4–B8
- `apps/desktop/anandia-workspace/src/runtime/agent-widgets/compile-pipeline.test.ts` — C9–C12
- `apps/desktop/anandia-workspace/src/runtime/agent-widgets/theme.test.ts` — D13–D14
- `apps/desktop/anandia-workspace/tests/e2e/widget-bootstrap.spec.ts` — E15–E18

For E15–E18, write a small test page in `tests/e2e/fixtures/widget-bootstrap-host.html` that loads the bootstrap srcDoc into an iframe and exposes message-pump helpers to the test.

Run; all new tests fail (e2e tests skip if Playwright not installed for new file — verify the project's `playwright.config.ts` covers this path).

Commit: `test(widget-bootstrap): plan 03 failing test scaffolding`.

### Step 2 — Module structure + canvasAPI proxy
Create the new directory `src/runtime/agent-widgets/` and these files:
- `canvas-api-proxy.ts` — exports `createCanvasAPI({ capabilities, cardId, postCall })`.
  - Build the full API surface; for undeclared capabilities, replace the method with one that returns a rejected Promise (`code: 4104`).
  - Maintain a `Map<id, { resolve, reject }>` for pending calls.
  - Expose a `handleResult(message)` helper for the controller to invoke when host posts `api.result`.
  - Freeze the returned object so agent source can't replace methods.

Tests B4–B8 → green.

Commit: `feat(canvas-api-proxy): per-card capability-aware proxy with pending-Promise correlation`.

### Step 3 — Theme module
Create `src/runtime/agent-widgets/theme.ts`:
- `applyThemeTokens(rootEl, tokens)` — sets each CSS var via `style.setProperty`.
- Tests D13–D14 → green.

Commit: `feat(widget-runtime): theme-token application + reactive update`.

### Step 4 — Compile pipeline (mocked esbuild for unit tests)
Create `src/runtime/agent-widgets/compile-pipeline.ts`:
- Export `compileSource({ source, esbuild, primitivesBundle }) → Promise<{moduleUrl, compile_ms, compiled_size}>`.
- Validate size ≤ 256 KiB (throw `WidgetError({phase:'validate', code: 4102})` if over).
- Wrap source: `import * as primitives from 'canvas-primitives'; ${source}; export { default } from './source';`. Set up esbuild's virtual filesystem so `'canvas-primitives'` resolves to `primitivesBundle` and `'react'` resolves to a known UMD reference.
- Resolve unknown imports → `WidgetError({phase:'compile', kind:'unknown_import', message: ...})`.
- Compile, blob-URL, return URL + timing + size.

Tests C9–C12 → green (with stub esbuild).

Commit: `feat(compile-pipeline): JSX→ESM with primitives + react virtual modules`.

### Step 5 — Bootstrap controller
Create `src/runtime/agent-widgets/bootstrap-controller.ts`:
- Export `createBootstrapController(window)` — returns `{ start(), receiveMessage(messageEvent), dispose() }`.
- On `start()`: post `bootstrap.ready` to `window.parent`.
- On `receiveMessage`: verify source is the parent (`messageEvent.source === window.parent`); switch on `data.kind`.
- For `init`: instantiate `createCanvasAPI(...)`, attach to `window.canvasAPI` (frozen), apply theme, run `compileSource`, dynamic-import the module URL, mount with `ReactDOM.createRoot(rootEl).render(<ErrorBoundary>...</ErrorBoundary>)`, post `widget.mounted`.
- For `source.update`: tear down current mount (cancel any in-flight canvasAPI Promises per B8), repeat init flow with new source.
- For `message.push`: invoke any registered `onMessage` handlers in the canvasAPI proxy.
- For `api.result`: route to the canvasAPI proxy's `handleResult`.
- For `theme.update`: re-apply tokens.
- For `dispose`: unmount, post `widget.disposed`. (Recycle path is Plan 07.)

Tests A1–A3 → green.

Commit: `feat(bootstrap-controller): postMessage protocol + lifecycle`.

### Step 6 — Bootstrap HTML asset
Create `src/runtime/agent-widgets/bootstrap.ts`:
- Export `bootstrapHtml: string` — a static HTML string with `<head>` (CSP meta tag) + `<body>` (`<div id="root">`) + `<script type="module">` that imports React, ReactDOM, esbuild-wasm, and the bootstrap-controller, then calls `createBootstrapController(window).start()`.
- Bundling strategy: this file is bundled by Vite as a separate entry that produces a single self-contained string. Use Vite's `?raw` import or a small build script — pick whatever's already idiomatic. (If neither is set up, hand-author a minimal HTML and inline the imports as `import.meta.url`-resolved URLs that resolve via Tauri's bundled-asset URL. Document the choice.)
- CSP meta: `default-src 'none'; script-src 'self' 'unsafe-eval' blob:; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:` — `unsafe-eval` is required for esbuild-wasm + `blob:` for the compiled module.

Commit: `feat(bootstrap): vendored HTML asset with CSP and module entry`.

### Step 7 — Empty primitives stub
Create `src/agent-widgets/primitives/index.ts` with a single placeholder export so `import 'canvas-primitives'` resolves cleanly:
- Export an empty object `export const __plan_03_placeholder = true`.
- Document that this file is replaced wholesale in Plans 05 / 06.

Commit: `feat(primitives): stub canvas-primitives import target`.

### Step 8 — Playwright E2E
Add `tests/e2e/fixtures/widget-bootstrap-host.html` and `tests/e2e/widget-bootstrap.spec.ts`:
- Host page sets up an iframe with the bootstrap srcDoc, exposes `window.__sendInit(payload)` and `window.__capturedMessages` for tests.
- Tests E15–E18 → green.

Commit: `test(e2e): widget bootstrap end-to-end iframe behavior`.

### Step 9 — Verification
Run:
- `bun run typecheck`
- `bun run test`
- `cd src-tauri && cargo test`
- `bun run test:e2e --project=chromium`

Confirm: bootstrap.ts size budget per spec §13 (eager bundle ≤ 200 KB excluding esbuild-wasm). Use `bun run build` and inspect output. If over, push primitives stubs / dependencies into lazy chunks.

Commit: `chore(widget-runtime): plan 03 verification clean`.

## Acceptance Criteria
- [ ] All A1–A3, B4–B8, C9–C12, D13–D14, E15–E18 domain tests pass
- [ ] `bun run typecheck` passes
- [ ] `bun run test`, `cargo test`, `bun run test:e2e --project=chromium` pass
- [ ] Coverage on `src/runtime/agent-widgets/` ≥ 80%
- [ ] Bootstrap HTML eager bundle ≤ 200 KB excluding esbuild-wasm (per spec §13)
- [ ] Sandbox attribute exactly `allow-scripts` in test fixture
- [ ] CSP forbids external network — verified via test that loading any non-blob script fails
- [ ] Bootstrap module is self-contained — no runtime fetches
- [ ] Plan 01 + 02 tests still pass

## Out of Scope
- Host-side capability broker (dispatch to local services / `apiCallRegistry`) — Plan 04
- Real `<AgentWidgetCard>` host component using this bootstrap — Plan 04
- Iframe pool — Plan 07
- Eager primitives library — Plan 05
- Heavy primitives lazy chunks — Plan 06
- Recycling protocol (`recycle.ready`) — Plan 07
- esbuild-wasm bundle integrity / SRI — defer to security pass

---

## Claude Code Handoff (paste this prompt to execute)

```
Read the implementation plan in .anandia/plans/widget-runtime/03-bootstrap-iframe.md.

Plan 01 must be complete. Plan 02 is independent — start Plan 03 even if 02 is in flight in another branch. Verify Plan 01 by running `bun run test` and `cargo test` first; tell me if anything is red before you start.

Your task:
1. FIRST: Write the test files from the "Domain Tests" section (Step 1). All new tests must FAIL (red). Plan 01 / 02 tests must remain GREEN.
2. THEN: Implement Steps 2-8 in order. Run tests after each step. The Playwright e2e tests in Step 8 are real-iframe — they need a real browser, so expect a longer feedback loop than unit tests.
3. After all tests pass, run the full verification (Step 9). Inspect bootstrap bundle size; if it exceeds 200 KB excluding esbuild-wasm, restructure (move stuff to lazy chunks) before claiming done.
4. Each step ends with a commit using the message provided in the plan plus the Co-Authored-By trailer per CLAUDE.md.

Rules:
- Do NOT modify test assertions to make them pass. Fix the implementation.
- The bootstrap is the security boundary. Do NOT add allow-same-origin, allow-forms, allow-popups. Do NOT relax CSP. If a feature seems to need them, stop and tell me.
- The primitives bundle in this plan is a STUB. Plans 05/06 fill it. Do not preemptively add primitives.
- For TS: bun, NOT npm.
- This plan ends with a working bootstrap iframe but NO host component using it. End-to-end demo lands in Plan 04.
```
