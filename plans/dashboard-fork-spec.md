# Hermes Dashboard Fork Spec

This spec captures the Hermes-side fork patches needed to let a remote Tauri client drive the existing dashboard surface (sessions, config, env, skills, cron, logs, models, profiles, plus the Achievements and Kanban plugins) over a TLS-terminated, token-authenticated channel. It is the source-code-anchored rewrite of `plans/dashboard-adapter-design.md` and supersedes it for any decision that conflicts.

The Tauri-side adapter (`DashboardAdapter`, `ConnectionProfile`, capability caching, page feature-flagging) lives in the desktop app repo and is referenced here only as the consumer of the contract this spec defines.

## Scope

In scope (changes that land on this branch):

- A small TLS-loading helper (`hermes_cli/tls_loader.py`) shared by the gateway and the dashboard. Loads `~/.hermes/tls/<host>.ts.net.{crt,key}` once, exposes a hot-reload hook, exposes `not_after` to callers.
- TLS termination in `hermes_cli/web_server.py` when a cert is loadable and the bind is non-loopback. Reuses the same cert/key files the gateway already loads.
- Bearer-token middleware on the dashboard that accepts **either** `API_SERVER_KEY` (off-loopback) **or** the existing ephemeral `_SESSION_TOKEN` (loopback only). Single env var across gateway and dashboard.
- A new `GET /api/meta` endpoint returning version, services-up, mounted dashboard endpoints, installed plugins, agent profiles, active profile, and TLS not-after when applicable. Replaces the design doc's `/v1/meta` (the fork uses `/api/*` and we keep that prefix).
- Hardened bind guard at startup: loopback by default; non-loopback requires either TLS + `API_SERVER_KEY` or the explicit `HERMES_ALLOW_INSECURE_BIND=1` escape hatch.
- Plugin auth uniformity: dashboard's auth dependency applies to mounted plugin routers (`/api/plugins/<name>/*`) the same way it applies to core routes. No plugin-side patches beyond the mount-time enforcement.
- Documentation of the canonical Kanban `status` values and the canonical Achievements record shape — both already exist on disk; this spec promotes them to a stable contract the Tauri side can rely on.

Deferred:

- The Tauri `DashboardAdapter` itself (commands, profile model, capability caching, page wiring). Lives in the desktop repo.
- Cert auto-renewal inside Hermes. The fork ships a documented manual procedure (launchd / systemd snippet); near-expiry surfaces through `/api/meta` and as a structured log warning. Auto-renewal can land later as its own spec.
- Multi-user dashboards, audit logging, RBAC. The single shared `API_SERVER_KEY` is one trust boundary per Hermes instance; per-user identity is a future spec.
- A generic plugin-viewer framework. The set of plugins worth surfacing is small (Achievements, Kanban, Example as the discovery probe) — bespoke Tauri pages are cheaper than abstraction until a third or fourth plugin lands.
- SSE for `/api/logs`. Today the endpoint is request-response (`web_server.py:2221`). A streaming variant is desirable but not blocking; tracked as an open question below.
- Direct `state.db` reads from the Tauri side. Everything goes through the dashboard REST surface; coupling clients to the SQLite schema is a deployment-shape footgun, especially for the VPS profile.
- Typed enum migration for Kanban `status`. The free-text column stays; the spec documents the canonical values.

## Reality vs the original draft

The source-code pass found these divergences from `plans/dashboard-adapter-design.md`. Where the draft was wrong, this spec wins.

| Area | Draft assumed | Fork reality |
|---|---|---|
| Dashboard location | `hermes/dashboard/` (FastAPI on `:9119`) | `hermes_cli/web_server.py` (FastAPI on `:9119`, launched via `hermes dashboard`) |
| Endpoint prefix | `/v1/*` (`/v1/dashboard/*`, `/v1/gateway/*` in unified mode) | `/api/*` everywhere; gateway's OpenAI-compat surface lives at `/v1/*` (`gateway/platforms/api_server.py`) and is unrelated |
| Dashboard auth | `API_SERVER_KEY` bearer | Ephemeral in-memory token regenerated per `start_server()`, validated via `X-Hermes-Session-Token` or `Authorization: Bearer` (`web_server.py:74,121-130`); localhost-only by default; `--insecure` flag required for non-loopback |
| Capability probe | `GET /v1/meta` with `services`/`plugins`/`agent_profiles` | None today. `/api/status` exists but mixes liveness with shape; this spec adds `/api/meta` |
| Plugin endpoints | "plugin endpoints land in the same fork patch" | Already mounted: Kanban exposes ~25 endpoints (`plugins/kanban/dashboard/plugin_api.py`, `name: "kanban"`), Achievements exposes 6 (`plugins/hermes-achievements/dashboard/plugin_api.py`, `name: "hermes-achievements"`), Example exposes 1 (`plugins/example-dashboard/dashboard/plugin_api.py`, **manifest `name: "example"`** — folder name and mount prefix differ). Dashboard mounts each at `/api/plugins/<manifest.name>/*` (`web_server.py:4006`) |
| Kanban lane state | "typed state machine" | Free-text `status` column in `~/.hermes/kanban.db` (`hermes_cli/kanban_db.py`). Canonical values: `triage`, `todo`, `ready`, `running`, `blocked`, `done`, `archived`. We document these and accept the free-text representation. |
| Profile selection | `?profile=alice` query convention | Process-level only (`HERMES_HOME` set in `_apply_profile_override()` at `hermes_cli/main.py:101`). No per-request switch. Switching the active profile from the dashboard means restarting Hermes. |
| Unified-base proxy | `HERMES_UNIFIED_BASE=1` mounts dashboard at `/v1/dashboard/*`, proxies gateway through | Out of scope. Gateway and dashboard are separate processes today; the dashboard probes gateway health via HTTP (`_probe_gateway_health()` at `web_server.py:481`). One TLS cert and one token are enough symmetry. |
| TLS | "shared cert files, mirror of gateway's TLS-loading code" | Neither surface terminates TLS today. The fork patch adds the TLS path to both via the new `tls_loader.py` helper; same files, different ports. |
| Reveal-secret pattern | Implied as model for redaction | Already implemented at `web_server.py:1259` (`POST /api/env/reveal`, rate-limit 5/30s, audit-logged). Tauri side calls it as-is. |

## Architecture

```
hermes_cli/
├── web_server.py        PATCHED — TLS termination, auth middleware,
│                        /api/meta endpoint, bind guard
├── tls_loader.py        NEW — shared cert/key loader, expiry probe,
│                        SIGHUP-triggered reload
├── main.py              PATCHED — `cmd_dashboard` catches `BindRefused`
│                        from `start_server` and exits 1 with a clean
│                        stderr message (no traceback)
├── kanban_db.py         existing, untouched (canonical status values
│                        documented in this spec only)
└── ...

gateway/platforms/
└── api_server.py        PATCHED — switch its inline cert-loading code
                         (when present) to tls_loader; share key with
                         dashboard

plugins/
├── hermes-achievements/dashboard/plugin_api.py   existing, no patch
├── kanban/dashboard/plugin_api.py                existing, no patch
└── example-dashboard/dashboard/plugin_api.py     existing, no patch
                                                  (used as discovery probe)
```

The dashboard and the gateway remain separate processes. The single piece of shared state is the cert/key pair on disk. Token sharing is via the `API_SERVER_KEY` env var, not a shared file.

### Connection lifecycle (Tauri-side, for context)

1. User adds a `ConnectionProfile`: name, base URL, `API_SERVER_KEY` (paste once → OS keyring).
2. Tauri's `DashboardAdapter::probe(profile)` does `GET {base_url}/api/meta` with bearer header. Returns version, available endpoints, installed plugins, advertised agent profiles.
3. Probe result is cached on the profile and pushed to the React side via Tauri event. Pages feature-flag off the cached capability set, including plugin pages.
4. Pages call typed Tauri commands that proxy through the adapter to the dashboard. The React side never holds the bearer or knows the base URL.
5. On profile switch (`set_active_profile`), the adapter swaps the active client and re-probes capabilities. No Hermes restart required for a profile switch on the **Tauri** side. (Switching the **agent** profile inside Hermes still requires restarting the Hermes process — see Profile surfacing below.)

## Endpoint surface

What's already there and stays as-is:

- `GET /api/status` — gateway/dashboard health, session count (`web_server.py:523`).
- `GET /api/sessions` — session list with search (`web_server.py:761`).
- `GET /api/config`, `GET /api/config/schema` — current config + auto-generated schema with `_SCHEMA_OVERRIDES` (`web_server.py:849`, `:860`).
- `GET /api/model/info` — active model details (`web_server.py:876`).
- `POST /api/env/reveal` — rate-limited, audit-logged secret reveal (`web_server.py:1259`).
- `GET /api/logs` — request-response log tail by file/level/component/search (`web_server.py:2221`).
- `GET|POST|PUT|DELETE /api/cron/jobs` and `POST /api/cron/jobs/{id}/{pause|resume|trigger}` — full cron CRUD (`web_server.py:2290-2354`).
- `GET /api/profiles` — list agent profiles (`web_server.py:2456`).
- `WS /api/pty`, `WS /api/ws`, `WS /api/pub`, `WS /api/events` — embedded chat / event broadcast (`web_server.py:2994`, `:3103`, `:3135`, `:3164`). Gated on `HERMES_DASHBOARD_TUI=1` or `--tui`.
- `/api/plugins/<name>/*` — every router under `plugins/<name>/dashboard/plugin_api.py` (`web_server.py:4006`).

What this spec adds:

- `GET /api/meta` — capability probe; shape below. Auth-gated like every other `/api/*` route.
- Bearer middleware applied uniformly across `/api/*` and the WebSocket upgrade paths. The existing `X-Hermes-Session-Token` / `Authorization: Bearer` validation at `web_server.py:113-168` is the integration point — extended to recognize `API_SERVER_KEY` in addition to the ephemeral `_SESSION_TOKEN`. WS handlers get a parallel pair of helpers (`_ws_request_token`, `_ws_token_valid`) implementing the same bind-aware policy at upgrade time, before `accept()`.
- Plugin auth uniformity via the existing global `auth_middleware`: the previous `not path.startswith("/api/plugins/")` exemption is dropped (one-line change at `web_server.py:228`). Plugin routers continue to mount via `app.include_router(...)` with no per-plugin patch.
- Bind guard inside `start_server()`: refuse non-loopback bind without TLS + `API_SERVER_KEY` unless `HERMES_ALLOW_INSECURE_BIND=1`. Mirror of the gateway's existing guard at `gateway/platforms/api_server.py:3084`. `cmd_dashboard` catches the `BindRefused` and exits 1 with a clean message.

What this spec does **not** add: no new endpoints inside the plugin namespaces; no replacement for `/api/logs` with SSE; no per-request profile selection.

## `/api/meta` shape

```jsonc
{
  "hermes_version": "0.12.0",
  "services": {
    "gateway":   { "available": true,  "url": "https://<host>.ts.net:8642" },
    "dashboard": { "available": true,  "endpoints": [
      "/api/sessions", "/api/config", "/api/config/schema", "/api/env",
      "/api/env/reveal", "/api/cron/jobs", "/api/profiles",
      "/api/model/info", "/api/logs", "/api/status"
    ] }
  },
  "plugins": [
    { "name": "kanban",              "enabled": true,  "prefix": "/api/plugins/kanban" },
    { "name": "hermes-achievements", "enabled": true,  "prefix": "/api/plugins/hermes-achievements" },
    { "name": "example",             "enabled": false, "prefix": "/api/plugins/example" }
  ],
  "agent_profiles": ["default", "alice", "bob"],
  "active_profile": "default",
  "tls": {
    "host": "<host>.ts.net",
    "not_after": "2026-08-04T12:00:00Z",
    "expires_soon": false
  }
}
```

Notes:

- `services.gateway.available` is true if `_probe_gateway_health()` returned successfully on the most recent probe (the function already exists at `web_server.py:481`); the value is cached behind `_cached_gateway_health()` with a 10-second TTL (`_GATEWAY_PROBE_TTL_SECONDS`) so multi-client probes coalesce, and the underlying `urlopen` runs via `loop.run_in_executor` so the FastAPI event loop never blocks on the 3-6s probe timeout.
- `services.gateway.url` is rendered using the same hostname the dashboard's TLS cert covers, so the Tauri client can reach both surfaces with the same base hostname.
- `services.dashboard.endpoints` is introspected from `app.routes` at startup, filtered to the `/api/*` namespace, deduplicated, and sorted. Plugin routes are excluded — they appear under `plugins[]` instead.
- `plugins[].enabled` is true when the plugin's manifest is present and discovered. `enabled: false` entries are still emitted so the Tauri side can render an "install this plugin" affordance. Disabled plugins do not have routes mounted.
- `agent_profiles` is populated by the same discovery `GET /api/profiles` uses (`web_server.py:2456`); the rationale for repeating it inside `/api/meta` is forward-compat: a Tauri client that has cached `/api/meta` can render the profile picker without a second round-trip.
- `tls` is omitted when no cert is loaded (loopback bind without TLS). When present, `expires_soon: true` if `not_after` is within 14 days.

`/api/meta` is auth-gated like every other `/api/*` route — no probe-without-bearer exception. The Tauri side gets the bearer into keyring before the first probe; local-dev callers use the ephemeral token from the SPA dev server or set `API_SERVER_KEY` for the dashboard process.

## Auth model

The dashboard accepts two token paths, chosen by bind:

**Loopback bind (`127.0.0.1:9119`)** — current behavior, unchanged. Server mints `_SESSION_TOKEN = secrets.token_urlsafe(32)` per `start_server()`. The token is injected into the served SPA at boot and accepted via `X-Hermes-Session-Token` or `Authorization: Bearer`. This keeps `hermes dashboard` opens-in-browser working with zero ceremony for solo local use.

**Off-loopback bind** — `API_SERVER_KEY` is required. The middleware accepts `Authorization: Bearer <API_SERVER_KEY>` and rejects any other token, including the ephemeral `_SESSION_TOKEN`. This is the configuration the Tauri client uses across the network.

Both bearer paths return `401` on rejection — never `403` and never `404`. The Tauri side distinguishes "wrong token" from "endpoint missing" from "permission denied" and surfaces different UI for each.

The same `API_SERVER_KEY` is the gateway's bearer (`gateway/platforms/api_server.py:588`). One env var, one rotation point, one keyring entry on the Tauri side. When the user runs `openssl rand -hex 32` and updates the env var, both surfaces update together. No drift.

The embedded chat WebSockets (`/api/ws`, `/api/pub`, `/api/events`, `/api/pty`) currently bridge the ephemeral session token to the `tui_gateway` dispatcher (`web_server.py:3103-3112`). The patch extends this so off-loopback connections present `API_SERVER_KEY` instead — otherwise the in-browser chat tab stops working when the dashboard is reached over the network. Concretely: the WS upgrade handler accepts the token from either `Authorization: Bearer`, `X-Hermes-Session-Token`, or the existing `?token=` query param, and validates against whichever bearer path the bind allows.

What this spec does **not** introduce: a separate `HERMES_DASHBOARD_KEY`, per-client paired tokens, or any pairing flow. Future work can add a second accepted token (e.g. a read-only audit reader token) by extending the middleware to accept a list and tag each token with a scope; that's a cheap extension of the single-token base, not a redesign.

## TLS posture

In-process TLS termination, identical pattern across gateway and dashboard.

**Cert files.** Both surfaces load the same Tailscale-issued Let's Encrypt cert + key. It chains to ISRG Root X1 and validates against any standard CA trust store, including `webpki-roots` on the Rust side. No private CA, no self-signed pinning, no per-device trust install.

The dashboard resolves cert paths via `_resolve_tls_paths(host)` (`web_server.py`), in priority order:

1. **Explicit override** — `HERMES_TLS_CERT` + `HERMES_TLS_KEY` env vars point at any cert/key pair. Highest precedence; intended for non-default deployments (e.g. cert lives outside `~/.hermes/tls/`).
2. **Hostname env** — `HERMES_TLS_HOST=myhost.ts.net` resolves to `~/.hermes/tls/myhost.ts.net.{crt,key}`. The intended path for users who run `tailscale cert myhost.ts.net` once per host.
3. **Glob auto-discovery** — if exactly one `~/.hermes/tls/*.ts.net.crt` exists with a matching `.key`, use that pair. Convenient for single-host deployments; refuses to guess when ambiguous.
4. **None** — `_TLS_CONTEXT` stays unset and the bind guard refuses non-loopback binds without `HERMES_ALLOW_INSECURE_BIND=1`.

The gateway uses a smaller resolver: only the explicit `HERMES_TLS_CERT` + `HERMES_TLS_KEY` env vars (`gateway/platforms/api_server.py`). For multi-surface deployments, set both env vars once and both surfaces pick them up.

**Loader module.** `hermes_cli/tls_loader.py` is new. Single responsibility: load the cert/key pair, return a tuple usable by uvicorn (`ssl_keyfile` + `ssl_certfile`), expose `not_after` as a `datetime`, expose a callable that reloads from disk (so a SIGHUP can trigger reload without a process restart). The gateway's existing cert-loading code (currently inline in `api_server.py` if present, otherwise added by the same patch) imports this helper. Same loader, two callers, one cert.

The helper's API surface is intentionally tiny:

```python
class TLSContext:
    cert_path: Path
    key_path: Path
    not_after: datetime
    expires_soon: bool      # True if not_after within 14 days

def load(cert_path: Path, key_path: Path) -> TLSContext: ...
def expiry_warning(ctx: TLSContext) -> str | None: ...   # for log emission
```

**Bind guard.** `cmd_dashboard()` and `start_server()` enforce the same matrix the gateway does:

| Bind | TLS configured | `API_SERVER_KEY` set | `HERMES_ALLOW_INSECURE_BIND` | Outcome |
|---|---|---|---|---|
| `127.0.0.1` | any | any | any | Start, ephemeral token only |
| Non-loopback | yes | yes (non-`changeme`) | any | Start, TLS terminates, `API_SERVER_KEY` required for all `/api/*` |
| Non-loopback | yes | no or `changeme` | any | Refuse to start; clear error referencing the env var |
| Non-loopback | no | any | unset | Refuse to start; clear error referencing TLS |
| Non-loopback | no | any | `1` | Start with a loud warning on every startup and on every request arriving on a non-loopback interface. Intentionally annoying. |

The existing `--insecure` CLI flag (`web_server.py:4033`) becomes shorthand for `HERMES_ALLOW_INSECURE_BIND=1` and is retained for ergonomics; the env var is the truth.

**Near-expiry warning.** On startup, the loader logs a structured warning at WARNING level if `not_after` is within 14 days. The warning includes the cert path and the expiry timestamp. Same warning is mirrored into `/api/meta.tls.expires_soon`.

**Renewal.** The fork ships a documented manual procedure rather than automating renewal inside Hermes. A monthly `launchd` (macOS) or `systemd` timer runs `tailscale cert --cert-file=… --key-file=… <host>.ts.net` and signals Hermes to reload (`SIGHUP` if the loader's reload hook is wired, otherwise process restart). Sample plist + service unit live in `docs/` alongside the patch.

**The Tauri side does not handle renewal.** If the cert is expired or about to expire, the adapter surfaces a typed error and the user sees a clear "your Hermes cert is expiring, renew with `tailscale cert …`" prompt. The renewal happens on the Hermes host, not in the desktop app.

## Plugin contract

Plugins follow the existing `plugins/<name>/dashboard/plugin_api.py` pattern. Each module exposes a `router: APIRouter`. The dashboard discovers them at startup by walking `~/.hermes/plugins/`, `<repo>/plugins/`, and `./.hermes/plugins/` (`web_server.py:3558`) and mounts each via `app.include_router(router, prefix=f"/api/plugins/{name}")` (`web_server.py:4006`).

The patch adds one line to that mount: the same auth dependency the core routes use is attached to each plugin router. Today plugin routes are unauthenticated by design because the dashboard binds localhost (`plugins/kanban/dashboard/plugin_api.py:16-24` documents this). The off-loopback case must close that hole — without this, anyone reaching the dashboard on the network with a token bypass of the form `/api/plugins/kanban/...` would have CRUD on the user's task DB. The unified middleware fixes it cleanly.

### Achievements (read-mostly, plugin owns writes)

- Mount: `/api/plugins/hermes-achievements/*`.
- Endpoints (existing): `GET /achievements`, `GET /scan-status`, `GET /recent-unlocks`, `GET /sessions/{session_id}/badges`, `POST /rescan`, `POST /reset-state`.
- Storage: JSON files at `~/.hermes/plugins/hermes-achievements/{state.json, scan_snapshot.json, scan_checkpoint.json}`.
- Achievement record shape: `{achievement_id, kind, category, icon, threshold_metric, tiers[], unlocked_at, session_id?, metadata?}`. The plugin defines a hardcoded list of achievements (`plugins/hermes-achievements/dashboard/plugin_api.py:64`); the Tauri side treats the list as data.

### Kanban (full CRUD, multi-board)

- Mount: `/api/plugins/kanban/*`.
- Endpoints (existing, ~25): board read (`GET /board`), task CRUD (`POST/PATCH /tasks`), bulk patch, comments, links, diagnostics, reclaim, reassign, home-channel subscriptions, dispatcher nudge, board CRUD (`/boards`, `/boards/{slug}/switch`), event tail (`WS /events`).
- Storage: SQLite at `~/.hermes/kanban.db` (or per-board subdirs) — `hermes_cli/kanban_db.py`.
- **Canonical `status` values:** `triage` | `todo` | `ready` | `running` | `blocked` | `done` | `archived`. The column is free-text in the schema; this spec promotes the seven values above to the contract. The Tauri side renders an "unknown lane" fallback for any value outside this set rather than crashing. A future spec can migrate to a typed enum; not in this scope.
- Lane assignment is via the `assignee` field (a Hermes agent-profile name). Control-plane lanes like `orion-cc` are terminal labels in the UI, not Hermes profiles (`kanban_db.py:2562`).

### Example dashboard (discovery probe)

- Mount: `/api/plugins/example/*` (manifest `name: "example"`; folder is `plugins/example-dashboard/`).
- Endpoint: `GET /hello` (one route, returns `{"hello": "..."}`).
- Role in this spec: kept as the canonical reference plugin and used to verify discovery + auth-middleware uniformity in tests. Not surfaced as a Tauri page.

## Profile surfacing

Hermes profiles are a process-startup concept. `_apply_profile_override()` (`hermes_cli/main.py:101`) parses `--profile` / `-p` from argv before any module imports and sets `HERMES_HOME`. One Hermes process = one profile.

The dashboard exposes the active profile via `GET /api/profiles` (already exists, `web_server.py:2456`) and via `/api/meta.active_profile` (this spec). It does **not** expose a per-request profile-switch mechanism. The Tauri side renders a profile picker that, when the user switches, prompts: "switching profiles requires restarting Hermes — would you like to restart now?" and either invokes a restart endpoint (out of scope) or lets the user do it manually on the host.

For the VPS profile, the user's existing way to switch is to run `hermes -p alice` in their service manager (launchd / systemd unit). Wiring a "restart Hermes" REST endpoint would be a privileged operation and is intentionally out of scope here.

## Cross-machine alignment tests

These contracts must hold and should land as tests on the fork patch:

- **`/api/meta` shape.** Server returns the document above; client parses without crashing on unknown extra fields. Forward-compat by construction.
- **Bearer rejected = 401, not 403/404.** Middleware returns 401 with no information leak in the body for any token problem (missing, malformed, mismatched).
- **Same `API_SERVER_KEY` works for gateway and dashboard.** A request to the gateway's `/v1/*` and a request to the dashboard's `/api/*` with the same bearer both succeed.
- **Same cert covers both surfaces.** Gateway and dashboard ports present a cert whose SAN includes `<host>.ts.net`; chain validates back to ISRG Root X1 against `webpki-roots`.
- **Hostname validation strict.** Server cert must match the hostname the client requested; clients refuse to connect on SAN mismatch. No "skip verify" path in the Tauri adapter.
- **Loopback ephemeral token rejected off-loopback.** A request to a non-loopback bind with the ephemeral `_SESSION_TOKEN` returns 401, even if the bind has TLS configured.
- **Non-loopback without TLS refused without escape hatch.** `host=0.0.0.0` with no cert and `HERMES_ALLOW_INSECURE_BIND` unset → server fails to start with a clear error referencing the missing cert. With TLS → starts cleanly. With escape hatch → starts with warning.
- **Plugin gating round-trips.** Disabling a plugin server-side surfaces in `/api/meta.plugins[].enabled` and removes its `/api/plugins/<name>/*` routes on next dashboard restart. Tauri page disappears on next reconnect with no client release.
- **Plugin auth uniformity.** Every plugin route accepts the same bearer that core routes accept and rejects the same way. Concretely: `GET /api/plugins/kanban/board` without bearer → 401, with bearer → 200; same for `/api/plugins/hermes-achievements/achievements` and `/api/plugins/example/hello`.
- **`/api/meta.tls.expires_soon` matches log warning.** When the loaded cert is within 14 days of `not_after`, both `/api/meta` reports `expires_soon: true` and the structured warning fires on startup.

## Open questions for the implementation pass

These are deliberately deferred to the implementation plan, not answered here:

- **SSE for `/api/logs`.** Today the endpoint is request-response; a streaming variant matches the design doc's intent for live tail. Decide at implementation time whether to add a sibling SSE endpoint (`GET /api/logs/stream`) or extend the existing endpoint with a `?follow=1` flag. The latter is simpler; the former is cleaner.
- **Reload-on-cert-change semantics.** The loader's reload hook is straightforward; the question is whether SIGHUP reloads everything (config + cert) or just the TLS context. SIGHUP-as-cert-only is the safer default; document explicitly.
- **`HERMES_API_HOST` / `HERMES_API_PORT` for the gateway URL in `/api/meta`.** The dashboard needs to know the gateway's external URL to surface in the meta probe. The gateway's bind config lives in env (`API_SERVER_HOST`, `API_SERVER_PORT`). If the gateway is bound `0.0.0.0`, the dashboard should publish the configured hostname (`<host>.ts.net`), not `0.0.0.0`. Confirm the env-var names and the rendering rule at implementation time.
- **Embedded-chat WS upgrade auth path.** The current handler at `web_server.py:3103-3112` accepts the token via query param. The patch adds `Authorization: Bearer` header support too; settle on header-preferred-with-query-fallback at implementation time, since some browser WS clients can't set headers.
- **WS upgrade host-header validation.** `host_header_middleware` is `@app.middleware("http")` so it does not run for WebSocket upgrades. The bind-aware token policy is the sole defense for WS endpoints today. A future patch should mirror `_is_accepted_host` inline in each WS handler before `accept()` for defense-in-depth against DNS rebinding. Bounded today by: loopback bind hides the ephemeral token cross-origin (SPA HTML unreadable from `evil.test`); off-loopback bind accepts only `API_SERVER_KEY` which is never browser-reachable.
- **Bind-guard wording.** The user-facing error messages for the four refused configurations matter. Draft them in the patch and verify the strings against the gateway's existing messages so they read consistently.
- **Plugin-list source of truth.** `/api/meta.plugins[]` and `/api/plugins` (if it exists, or a sibling endpoint) must agree on shape. Decide whether `/api/meta` is the only listing endpoint or whether to add a dedicated `/api/plugins` for clients that want the plugin list without a full meta probe.
- **Test-host for TLS round-trip.** End-to-end alignment tests need a hostname whose cert validates. Implementation can either (a) generate a throwaway LE cert via Tailscale's staging environment in CI, (b) ship a pre-issued cert for a project-owned tailnet, or (c) gate the cert-chain test behind a manual integration suite. Decide before merging.
