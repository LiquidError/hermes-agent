# Widget example contract

This directory is the **source of truth** for the canonical widget examples that
Hermes ships to agents via `list_widget_examples()` and `read_widget_example(name)`
(see `docs/plans/hermes-widget-runtime/05-examples-and-sync.md` and the Hermes
repo's `tools/widget_tools.py`).

Each `.tsx` file here is a self-contained pattern an agent can read end-to-end
and adapt — not a library to import. The file's first JSDoc block becomes the
`summary` field returned by `list_widget_examples`. The full file body — JSDoc
and source together — is what `read_widget_example` returns.

## Contract

Each example MUST:

- Begin with a JSDoc block whose first line is a one-sentence summary.
- Document its required `Capabilities:` array and `Imports:` line in the JSDoc.
- Export a default React component.
- Use only the `canvas-primitives` and `canvasAPI` surface declared in the
  JSDoc — no `fetch`, no CDN, no third-party imports.
- Be runnable verbatim against the Tauri-side bootstrap (the same pipeline
  that compiles agent-authored sources at runtime).

## Sync to Hermes

Hermes' `scripts/sync_widget_examples.py` (idempotent; reports drift) is the
one-way pull from here into Hermes' `assets/widget_prompts/examples/`:

```bash
# from the Hermes checkout
python scripts/sync_widget_examples.py \
  --source ../anandia-workspace/apps/desktop/anandia-workspace/contracts/examples \
  --target assets/widget_prompts/examples
```

Use `--dry-run` first. Orphans in the target (files present in Hermes but
missing here) are reported but never auto-deleted — remove by hand if intended.

## Adding a new example

1. Author `<your-name>.tsx` in this directory following the Contract above.
2. Run `bun run contracts:check-examples` — every example must type-check
   against the shipped `contracts/canvas-primitives.d.ts` +
   `canvas-primitives-heavy.d.ts` + `canvas-api.d.ts`. The CI ratchet
   enforces this; landing an example that uses a primitive prop the runtime
   doesn't ship will fail there too.
3. Run the sync script in `--dry-run` mode against your local Hermes checkout
   to verify the diff is just your new file.
4. Run for real and commit on the Hermes side.
5. Update Hermes' addendum (`assets/widget_prompts/addendum.md`) only if the
   new pattern introduces a new capability or workflow worth surfacing in the
   system prompt — otherwise the example surfaces itself via the listing tool.

## Why "contracts/" lives at the workspace root

These files describe the cross-repo agreement between the Tauri client and
Hermes. They are intentionally **not** under `src/` because they aren't shipped
into the Tauri bundle — they're consumed as raw text on the Hermes side. Keeping
them at the workspace root signals their dual-repo role and keeps them out of
the React/TS dependency graph (no inadvertent imports).
