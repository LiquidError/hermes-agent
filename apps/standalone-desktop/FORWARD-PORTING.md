# Forward-porting `apps/desktop` releases into `apps/standalone-desktop`

This app is a **stripped, remote-only fork of `apps/desktop`**. It tracks desktop almost 1:1
(it doesn't re-implement anything), so syncing a new upstream desktop release is mostly a
mechanical replay of desktop's diff, plus resolving a small, predictable set of conflicts
where the "strip" overlaps desktop's changes.

Last sync: **desktop 0.15.1 → 0.17.0** (June 2026). Read this whole file before starting —
the gotchas section will save you an hour.

---

## The mental model (why this is easy)

The fork = `apps/desktop` **minus 5 files**, with **~7 files edited**, plus app-root config
(`package.json`, `vite.config.ts`, `tsconfig.json`, vendored `shared/`, `README.md`).

**5 files stripped** (never recreate them — they are the local backend/installer):
- `electron/backend-probes.cjs` (+ `.test.cjs`)
- `electron/bootstrap-runner.cjs` (+ `.test.cjs`)
- `src/components/desktop-install-overlay.tsx`

**Files edited by the strip** (the recurring conflict set — remote-only vs local-backend):
- `electron/main.cjs` — no local backend: `startHermes`/`spawnPoolBackend` resolve a remote or throw; all child-spawn/`resolveHermesBackend`/`ensureRuntime` machinery removed.
- `src/app/desktop-controller.tsx` — remote-gateway-first; no `DesktopInstallOverlay`.
- `src/components/boot-failure-overlay.tsx` — 3-shape remote overlay (needsRemoteSetup / remoteReauth / generic); no repair-install / use-local-gateway.
- `src/i18n/{en,types,zh,ja,zh-hant}.ts` — adds remote-setup keys, drops local keys (`repairInstall`, `repairHint`, `useLocalGateway`).

Because the divergence is tiny, the strategy is: **replay desktop's release diff with the
paths retargeted, let 3-way merge do 95%, hand-resolve the ~4 conflicts, then re-strip any
new local-backend code desktop added.**

---

## Procedure

Assume a branch that already contains the new upstream `apps/desktop` (e.g. you merged
upstream `main` into `feat/standalone-desktop-0NN`). The standalone app on that branch is
still at the *old* desktop level; we forward-port it.

> ⚠️ The Bash tool here is **sandboxed** — each call gets a throwaway overlay, so file
> mutations don't persist between calls. Every step that *writes* must run with the sandbox
> disabled (in Claude Code: `dangerouslyDisableSandbox: true`). Read-only inspection is fine
> sandboxed.

### 1. Find the two endpoints

```bash
# BASE = desktop version the fork currently tracks (the previous sync point).
# First time it was the branch fork point:
MB=$(git merge-base feat/standalone-desktop main)      # -> desktop@0.15.1
# On subsequent syncs, BASE = the desktop commit at the LAST sync. Easiest: the commit
# that bumped apps/standalone-desktop/package.json "version" to the previous release, or
# just diff package.json versions to confirm direction.

H=<this branch>                                         # e.g. feat/standalone-desktop-018
git show $MB:apps/desktop/package.json | grep version  # sanity: old version
git show $H:apps/desktop/package.json  | grep version  # sanity: new version
```

Re-confirm the strip surface still holds (cheap, catches upstream refactors):

```bash
# files only in desktop@BASE under src/electron == the stripped set (expect the 5 above)
comm -13 \
  <(git ls-tree -r --name-only $H apps/standalone-desktop/src apps/standalone-desktop/electron | sed 's#apps/standalone-desktop/##' | sort) \
  <(git ls-tree -r --name-only $MB apps/desktop/src apps/desktop/electron | sed 's#apps/desktop/##' | sort)
```

### 2. Generate the retargeted patch and apply 3-way

Exclude the 5 stripped files **and** every file the release *deletes* (pure deletions can't
3-way merge and will abort the whole `git apply` — see gotchas). Use `--binary` for fonts.

```bash
# list deletions to exclude + handle manually:
git diff --diff-filter=D --name-only $MB..$H -- apps/desktop/src apps/desktop/electron | sed 's#apps/desktop/##'

git diff --binary $MB..$H -- apps/desktop/src apps/desktop/electron \
  ':(exclude)apps/desktop/electron/backend-probes.cjs' \
  ':(exclude)apps/desktop/electron/backend-probes.test.cjs' \
  ':(exclude)apps/desktop/electron/bootstrap-runner.cjs' \
  ':(exclude)apps/desktop/electron/bootstrap-runner.test.cjs' \
  ':(exclude)apps/desktop/src/components/desktop-install-overlay.tsx' \
  $(printf "':(exclude)apps/desktop/%s' " <each-deleted-path>) \
  | sed 's#apps/desktop/#apps/standalone-desktop/#g' > /tmp/fp.patch

# delete the files this release removed (from the standalone copy):
cd apps/standalone-desktop && git rm -q <each-deleted-path-without-prefix> ; cd -

git apply --3way --whitespace=nowarn /tmp/fp.patch
```

Expected: a pile of "Applied … cleanly", a handful of "with conflicts", **zero** "does not
apply". If you see "does not apply", a deletion or binary slipped through — fix the exclude
list and re-run (`git apply` is all-or-nothing; conflicts are fine, hard failures abort
everything).

Verify it actually landed (don't trust the log — see gotcha about staged changes):

```bash
git status --porcelain apps/standalone-desktop | cut -c1-2 | sort | uniq -c   # expect A/M/D/UU counts
```

### 3. Resolve conflicts (the `UU` files)

There are normally ~4, always the same ones. **Principle: keep 0.17's remote/multi-window
improvements; keep stripping local-backend.** For each:

```bash
for f in <conflicted files>; do awk '/^<<<<<<</{p=1} p{print NR": "$0} /^>>>>>>>/{p=0}' "apps/standalone-desktop/$f"; done
```

- **`electron/main.cjs`** — almost always "take **ours**" at every conflict (ours = remote-only),
  EXCEPT the import block: keep any *new wanted* import (e.g. `session-windows.cjs`) while
  dropping imports of stripped modules (`bootstrap-runner`, `backend-probes`).
  Resolver for "take ours" hunks:
  ```bash
  awk '/^<<<<<<< /{c=1;s="ours";next} /^=======$/&&c{s="theirs";next} /^>>>>>>> /&&c{c=0;next} {if(!c||s=="ours")print}' "$f" > "$f.r" && mv "$f.r" "$f"
  ```
- **`desktop-controller.tsx`** — take 0.17's render block but delete `<DesktopInstallOverlay/>`
  and any duplicate `<PersistentTerminal/>` (0.17 hoists it into `mainOverlays`).
- **`boot-failure-overlay.tsx`** — take **ours** (the remote 3-shape ternary). Drop the
  `Wrench`/`AlertTriangle` import if it goes unused.
- **`i18n/zh.ts`** (and check `en.ts`/`types.ts` resolved to the remote keys) — take **ours**.

### 4. Re-strip new local-backend code desktop added

This is the step that's easy to forget. Desktop adds helper *definitions* that merge cleanly,
but their *callers* live in the regions you just stripped → **dead code**. The linter is your
oracle here. Also new language files / new tests carry local-backend assumptions.

```bash
# new local-backend i18n keys in any NEW language file the release added:
grep -rnE 'repairInstall|repairHint|useLocalGateway' apps/standalone-desktop/src/i18n   # delete these keys

# new tests that assert on stripped backend behavior (e.g. windows-child-process.test.cjs,
# anything reading bootstrap-runner.cjs or grepping execFileSync(pyExe)) -> git rm them.

# verify every electron require() target still exists, and no refs to deleted components:
grep -rhoE "require\('\./[a-z0-9_-]+\.cjs'\)" apps/standalone-desktop/electron | sort -u   # spot-check each exists
```

### 5. Reconcile app-root config (not covered by the src/electron patch)

```bash
git diff $MB..$H -- apps/desktop/package.json   # fold in: new runtime deps, dep bumps, electronVersion
git diff $MB..$H -- apps/desktop/vite.config.ts # apply non-test changes (keep standalone's resolve/server block)
git diff $MB..$H -- apps/shared                 # re-sync vendored shared/ (usually trivial / source unchanged)
```

- Add only deps the merged code actually imports:
  `for d in <new dep>; do grep -rl "from '$d'" apps/standalone-desktop/src apps/standalone-desktop/electron; done`
- **Keep** `eslint-plugin-react-compiler` even if desktop drops it — standalone's
  `eslint.config.mjs` still references it.
- Bump `package.json` `"version"` and build `"electronVersion"` to the new release.
- Regenerate the lockfile: `npm install --no-workspaces --ignore-scripts` (run in `apps/standalone-desktop`).
- Update `test:desktop:platforms` to list the electron `*.test.cjs` that exist here (add the
  release's new ones; never list the stripped `backend-probes`/`bootstrap-runner` or removed tests).

### 6. Verify

```bash
cd apps/standalone-desktop
npm run type-check            # must be clean
npm run lint                  # must be 0 errors (warnings OK). Use `npm run lint:fix` for the
                             #   mechanical perfectionist/curly churn from upstream code.
npm run test:desktop:platforms   # node --test, must be all-pass
grep -rl '^<<<<<<<' src electron || echo "no markers"
```

`npm run lint:fix` auto-resolves the large but harmless `perfectionist/sort-*` + `curly`
diff that upstream code triggers under our config — run it, then re-`type-check`.

---

## Gotchas (each one cost real time)

1. **Sandboxed Bash doesn't persist.** Any write step (patch apply, `git rm`, `npm install`,
   awk-in-place) must run with the sandbox disabled, or it silently evaporates and the next
   call sees a clean tree.
2. **`git apply` is all-or-nothing.** One "does not apply" (a deletion or an un-`--binary`'d
   font) rolls back the *entire* patch. Conflicts (`UU`) are NOT failures — they're written
   with markers and kept. Exclude every deleted path and use `--binary`.
3. **Pure deletions can't 3-way.** Handle release-deleted files with `git rm` + exclude from
   the patch (step 2). They're the usual cause of "does not apply".
4. **`git diff --name-only` undercounts after apply.** `--3way` *stages* much of its work, so
   `git diff` (worktree-vs-index) looks tiny. Use `git status --porcelain` to see the truth
   (A/M/D/R/UU).
5. **Dead-code cascade in `main.cjs`.** After taking "ours" for the spawn regions, lint flags
   orphaned helpers (`broadcastBootstrapEvent`, `isBootstrapComplete`, `findPythonForRoot`,
   `handOffWindowsBootstrapRecovery`, `readJson`, `BOOTSTRAP_*`, etc.). Remove them, re-lint,
   repeat until clean — removals cascade one or two rounds. `no-unused-vars` is **on for
   `electron/*.cjs`** (off for `src/`, which uses `unused-imports`).
6. **i18n is type-checked.** Extra keys in a translation file that aren't in `types.ts` fail
   `tsc`. New language files from upstream (ja, zh-hant, …) ship local-backend keys — strip
   them. Missing keys are fine (the catalog type is all-optional).
7. **`scripts/dashboard_tls.py` rides a core internal — re-check it each release.**
   The HTTPS wrapper injects `ssl_certfile`/`ssl_keyfile` by monkeypatching how core
   starts uvicorn. Core uses `uvicorn.Config(...)` + `uvicorn.Server(config)` (see
   `hermes_cli/web_server.py:start_server`), so SSL must be injected at the **Config**
   layer; patching `uvicorn.run` alone is a **silent no-op** → dashboard stays plain HTTP →
   client gets `net::ERR_SSL_PROTOCOL_ERROR` and the server logs `Invalid HTTP request
   received`. If a release changes the serve mechanism, update the patch target. The
   `Hermes Web UI → http://…` banner is hardcoded in core and is NOT a TLS indicator —
   verify with `curl https://<host>:<port>/api/status`.
8. **`npm run test:ui` (vitest) noise is pre-existing, not your merge.** Two standing issues,
   independent of any port: (a) jsdom `window.localStorage.* is not a function` on this setup;
   (b) vitest scans `electron/*.test.cjs` (node:test, not vitest → "No test suite found" /
   "describe is not defined") and gitignored `build/native-deps/**`, because `vite.config.ts`
   has no `test.include`. Don't chase these as merge regressions. (Optional permanent fix: add
   `test: { include: ['src/**/*.{test,spec}.{ts,tsx}'] }` to `vite.config.ts`.)

---

## One-glance checklist

- [ ] Found BASE (prev sync) + HEAD (new release); confirmed the 5-file strip still holds
- [ ] Generated `--binary` patch excluding stripped + deleted files; `git rm` the deletions
- [ ] `git apply --3way` → zero "does not apply"; verified via `git status --porcelain`
- [ ] Resolved the ~4 conflicts (remote keep / local strip)
- [ ] Re-stripped new local-backend: dead `main.cjs` code, i18n keys, backend-asserting tests
- [ ] Reconciled `package.json` (deps, version, electronVersion), `vite.config.ts`, vendored `shared/`
- [ ] `npm install` (lockfile); updated `test:desktop:platforms`
- [ ] `type-check` clean · `lint` 0 errors (`lint:fix` for churn) · `test:desktop:platforms` all-pass · no markers
