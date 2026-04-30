# Hermes Widget Runtime — Plan Index

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement these plans task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Source design:** [`00-design.md`](./00-design.md). Read it first — it captures the architecture decisions and answers the §11 open considerations from `plans/hermes-widget-render-spec.md`.

**Source spec:** [`../hermes-widget-render-spec.md`](../hermes-widget-render-spec.md). The wire contract in §3 is shared verbatim with the Tauri side and is not redesigned in any of these plans.

**Tauri-side handoff:** [`../widget-runtime/hermes-handoff.md`](../widget-runtime/hermes-handoff.md). Cross-machine alignment tests are routed to specific plans.

## Plan map

| # | Title | Critical path | Demoable end-state |
|---|---|---|---|
| 01 | [Capability negotiation, tool scaffolding, system-prompt addendum](./01-capability-negotiation-and-tool-scaffolding.md) | Yes | Agent sees the six widget tools and the addendum only when the connected client advertised `widget.render` in `client.hello`. |
| 02 | [WidgetRegistry, render/update/message/dispose lifecycle, inbound event dispatch](./02-widget-registry-and-lifecycle.md) | Yes — milestone | Agent renders and disposes a widget end-to-end against the Tauri client. |
| 03 | [ApiCallRegistry + async `widget.api_call`/`widget.api_response` with 32 KiB cap](./03-api-call-registry-and-async.md) | Yes | Card calls `canvasAPI.hermes.ask` and the response lands in the iframe; oversized responses produce a clean 4106 rejection. |
| 04 | [`widget.api_cancel` both directions](./04-api-cancel-flows.md) | Yes | Closing a card mid-`hermes.ask` cleanly cancels the underlying btw without phantom response arrival. |
| 05 | [Example tools, starter examples, sync script](./05-examples-and-sync.md) | No | Agent calls `list_widget_examples()` then `read_widget_example("form-with-hermes-ask")` before authoring a widget. |

Plans 01 → 02 are the demoable milestone. 03 unlocks `hermes.ask`. 04 closes the cancel path. 05 makes widget authoring effective.

## Sequencing

Plans 01–04 should land in order. Plan 05 can land any time after Plan 01 (it touches `assets/widget_prompts/` and `tools/widget_tools.py` only — no overlap with 02–04 once the tool-scaffolding stubs are in place).

## Conventions used in every plan

- **Tests** run via `scripts/run_tests.sh` (CI-parity wrapper). The CLAUDE.md is firm on this — do not call raw `pytest`.
- **Commits** do not include `Co-Authored-By` trailers.
- **No version-stage framing.** Features are in scope or out of scope; nothing is "v1" or "v2".
- **No internal-process markers** in code or tests (no Phase numbers, no TDD/characterization framing, no personal names in fixtures — use neutral names like `client-a`, `tauri-test-client`).

## Cross-machine alignment tests

Each is in the plan listed:

- **`client.hello` capability bundle** — all six widget tools register iff `widget.render` advertised; none otherwise. Plan 01.
- **Outbound client events accepted** — gateway dispatcher routes `widget.mounted` / `widget.error` / `widget.disposed` / `widget.api_cancel` event-shape messages. Plan 02.
- **Card ID format** — server allocator produces ids matching `/^wgt_[0-9a-f]{6}$/`. Plan 02.
- **`widget.api_response` 32 KiB cap** — server-side enforcement before emit, error code 4106 with actual size + cap in message. Plan 03.
- **`widget.api_cancel` envelope** — client → server message carries exactly `{correlation_id, card_id, reason}`. Plan 04.
- **Error code table** — codes 4101–4107 + 5101–5103 match the Tauri-side codebook. Spread across plans 02–04 as each becomes reachable.

## Out of scope (will not appear in any plan)

- Streaming render (`widget.render.chunk`). Namespace reserved per spec §6.2.
- Widget persistence across `session.resume`. Sessions stay ephemeral per spec §11.4.
- Per-call cancellation API on `canvasAPI` (Tauri-side concern). Card-level disposal cancellation is in scope.
- Hard-kill timer for runaway `prompt.btw` after cancel. Best-effort `agent.interrupt()` with observability is the chosen behavior; if production data shows runaway btws are common, that's a follow-up.
- Per-call user approval of `hermes.ask`. The capability-declaration manifest in `widget.render` is the user-visible gate; the 32 KiB cap bounds the worst case.
