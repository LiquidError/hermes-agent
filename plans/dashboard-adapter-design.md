# Hermes Dashboard Adapter — Design

This document captures the design for connecting the Tauri app to Hermes' dashboard surface (sessions, config, skills, env, jobs) over a TLS-terminated, token-authenticated REST channel. It runs alongside the existing `tui_gateway` WebSocket adapter that powers chat and the widget runtime — same Tauri app, second outbound connection, same auth posture.

The wire contract for the dashboard's REST surface is upstream Hermes' to define. This doc covers what the Tauri side and the Hermes side must agree on, what gets patched on the Hermes fork, and how the connection is modelled in the app.

## Scope

In scope:

- A `DashboardAdapter` in the Rust side of the Tauri app, paralleling `GatewayAdapter`. Owns one HTTPS client per active connection profile, holds bearer token in OS keyring, exposes typed Tauri commands to the React side.
- TLS + bearer-token patch on the Hermes dashboard backend, mirroring the gateway. TLS terminates in-process using the **Let's Encrypt cert provisioned via `tailscale cert`** (the same cert the gateway already uses), so both surfaces present a publicly-trusted, hostname-validated cert without operating any private CA. Bearer auth **reuses the gateway's existing `API_SERVER_KEY`** — single secret, single rotation.
- A `ConnectionProfile` domain model covering both surfaces (gateway WS + dashboard REST) under one identity, so users switch from "mini Mac" to "VPS" in one place rather than two.
- Capability probe on connect: read advertised endpoints, version, and feature flags from a single `GET /v1/meta` endpoint. Tauri stores the capability set per profile and the React side feature-flags pages off it.
- React pages for Sessions, Profiles, Skills/Toolsets, Config, Keys, Cron, Logs, Models. Reuses the existing app's design tokens; mounts under a new `/dashboard/*` route inside the Tauri shell.
- **Plugin pages: Achievements and Kanban as first-class destinations**, not afterthoughts. These are a primary reason to build this — they're where the agent's autonomous work surfaces — and they need their own data models, not a generic "plugin viewer" pane. The "Example" plugin is treated as a reference implementation, useful for verifying plugin discovery but not getting its own UI.
- Settings → Connections page that lets the user add, edit, switch, and remove `ConnectionProfile`s without restarting the app. Re-probes capabilities on save.

Deferred:

- Analytics. Aggregations over usage/cost are derivable from sessions + runs but want their own design pass.
- Embedded chat in the dashboard. Chat lives in the existing tui_gateway adapter; the dashboard surface is read-mostly management plus plugin views. No need to fork the chat path.
- Multi-user dashboards. The shared bearer is one `API_SERVER_KEY` per Hermes instance. Per-user identity, audit logging, and RBAC are deferred until there's a real second user.
- Direct file/SQLite reads from `~/.hermes/`. Tempting locally, useless for the VPS profile, and a coupling-to-internals risk. Everything goes through the REST surface.
- A generic plugin-viewer framework. The set of plugins worth surfacing is small enough today (Achievements, Kanban) that bespoke pages are cheaper than abstraction. If a third or fourth plugin lands and they share enough shape, factor then.

## Spec-vs-codebase landmarks

The design accounts for the actual upstream landscape and the existing fork:

- Hermes ships **two** localhost services. Gateway at `:8642` is OpenAI-compatible runs + SSE, gated on `API_SERVER_ENABLED`. Dashboard backend at `:9119` is FastAPI/Uvicorn (the `web` extra), serves the management REST. They are independent processes; either can be off.
- Dashboard upstream binds `127.0.0.1` and ships **no auth**. The fork already added `API_SERVER_KEY` bearer auth to the gateway; this spec extends the same middleware to the dashboard so a single rotated secret governs both surfaces.
- **Transport security is real TLS terminated in-process by Hermes, using a Let's Encrypt cert provisioned through `tailscale cert`.** Tailscale's role here is *cert issuance*, not transport — the resulting cert at `~/.hermes/tls/<host>.ts.net.{crt,key}` is a normal LE cert that chains to ISRG Root X1 and validates against any client's standard CA store, including `webpki-roots` on the Tauri side. This works whether the client is on the tailnet (resolves the hostname via MagicDNS to a CGNAT IP), on the LAN (resolves via a hosts-file alias to a `192.168.x.x` IP), or via any other path that produces the right hostname for the TLS handshake. Same cert, same hostname, every transport path validates cleanly. No self-signed pinning, no manual trust install on Windows.
- The dashboard endpoints documented upstream (`/health`, `/sessions`, `/config`, `/config/schema`, `/env`, etc.) are stable enough to model against. New endpoints we want (a single `GET /v1/meta` for capability probing, plus the plugin endpoints for Achievements and Kanban) land in the same fork patch.
- The Tauri app's existing `GatewayAdapter` already establishes the pattern: connect URL + bearer in keyring, reconnect-on-disconnect, structured error → typed Tauri command results. The new adapter clones the shape, swaps WS for HTTPS+SSE, lives in `src-tauri/src/adapters/dashboard.rs`, and shares the keyring entry with the gateway adapter (one token, one rotation point).
- Hermes profiles (`hermes -p alice`) are independent agent instances with independent ports and config. The Tauri side already has the concept of a profile in the gateway adapter; we extend it rather than introducing a parallel notion.

## Domain model

The dashboard's bounded context is **management** — read and edit the agent's static configuration, browse what it has done, toggle what it can do. The runtime conversation lives in the gateway adapter and isn't modelled here.

The entities, in roughly the order a user encounters them:

**ConnectionProfile** is the user-side identity for a Hermes instance. Holds `name`, `base_url` (always a hostname matching the cert's SAN, never a bare IP), a single `hermes_token` reference in keyring (shared between the gateway adapter and the dashboard adapter — one secret per Hermes), an `active` flag. One profile per Hermes instance — "mini Mac local", "VPS prod". Profile is the unit of switching. No TLS-pinning fields: the cert is publicly-trusted via Let's Encrypt, so standard CA validation is sufficient.

**Capabilities** is the per-profile result of the meta probe. Records the Hermes version, which optional services are up (`gateway`, `dashboard`), which dashboard endpoints exist (so the React side can grey out pages on older versions), which plugins are installed and enabled, and which Hermes profiles the dashboard exposes (Hermes-side `-p alice` profiles, not to be confused with `ConnectionProfile`s — see naming note below).

**HermesProfile** (Hermes-side, server multi-tenancy) is the agent's notion of a profile: a named bundle of config, sessions, skills, memory, persona. The dashboard surfaces a list of these and lets the user switch the *active* one server-side. From the Tauri app's perspective these are read/scoped via the dashboard's existing `?profile=alice` query convention; we don't manage them, we surface them.

**Session** is one conversation with the agent. Has `id`, `title`, `source_platform` (cli / telegram / discord / api / cron), `model`, `message_count`, `tool_call_count`, `last_active_at`, `is_live`. Backed by Hermes' SQLite + FTS5. Detail view shows messages, tool calls, costs.

**Skill** is a capability bundle the agent can load (`agentskills.io` standard). Has `name`, `category`, `description`, `enabled`, `source` (built-in / community / user-authored). Catalogued under `~/.hermes/skills/`.

**Toolset** is a built-in tool bundle (file ops, web browsing, code exec). Has `name`, `tools[]`, `enabled`, `setup_required` flag with reason if any. Distinct from skills — toolsets are wired in code, skills are markdown-driven.

**ConfigField** is one row in the rendered config form. Has `path` (e.g. `model.provider`), `type` (string/int/bool/select), `description`, `category`, `default`, `current`, `options[]` for selects. Driven by the upstream `/config/schema` endpoint so the form auto-renders new fields without a frontend change.

**EnvKey** is one row in the rendered keys form. Has `name`, `category` (LLM Providers / Tool API Keys / Messaging / Agent Settings), `description`, `is_set`, `redacted_value`, `is_advanced`. Never returns the raw secret to the frontend. Edits go back through the dashboard's `/env` endpoint.

**Model** is a configured inference target: `provider`, `model_id`, `base_url`, `is_active`. Switching models is a config write under the hood, but it's enough of its own affordance to deserve a page.

**CronJob** is a scheduled task the agent will run. Has `id`, `cron_expression`, `prompt_or_skill_invocation`, `delivery_target` (which platform/session to post to), `next_run_at`, `last_run_status`.

**Run** is a single agent execution (used for live status during chat or cron). Has `id`, `session_id`, `status`, `usage`, `output`. Streamed over SSE for live views.

**LogEntry** is one line of structured output from the gateway or dashboard process. Has `ts`, `level`, `component`, `message`, optional `trace_id`. Tail-followable.

**Achievement** is one entry in the agent's autonomous accomplishment log — the plugin records milestones, tool-use firsts, learning events, and skill-creation moments. Has `id`, `kind` (e.g. `skill_authored`, `cross_session_recall`, `tool_first_use`), `title`, `description`, `earned_at`, `session_id` (often), `metadata` (open-ended JSON the plugin emits). Read-mostly from the dashboard's perspective — the user inspects, occasionally pins or annotates; the agent does the writing.

**KanbanBoard** is a named container for cards. Has `id`, `title`, `tenant_id` (if the plugin is multi-tenant), `lanes[]` with the lane definitions (Triage / Todo / Ready / In Progress / Blocked / Done in the default board, but the plugin allows custom lanes). The screenshot's "NEW BOARD" affordance hints that boards are first-class — plural per Hermes, not a single global Kanban.

**KanbanCard** is one task. Has `id`, `board_id`, `lane`, `title`, `body` (markdown), `assignee_profile` (which Hermes agent profile owns it — distinct from human assignment), `tenant`, `created_at`, `archived`, `dependencies[]` (other card ids), and lifecycle metadata that drives the lane the dispatcher places it in. The lanes' subtitles in the screenshot ("raw ideas — a specifier will flesh out the spec", "claimed by a worker — in-flight", etc.) are the state machine: cards move between lanes by dispatcher tick or manual drag, and "Nudge Dispatcher" is a manual kick. Worth modelling those state transitions explicitly rather than treating lane as a free-text string — the agent's autonomous moves should be auditable.

A naming note worth catching at source-pass time: `ConnectionProfile` (Tauri-side, "which Hermes") and `HermesProfile` (server-side, "which agent identity within one Hermes") are different concepts. The existing screenshot's "PROFILES : MULTI AGENTS" sidebar item refers to the latter. Worth making the distinction crisp in code (`tauri::ConnectionProfile`, `hermes::AgentProfile`?) to keep them from blurring.

## Architecture

```
Tauri App
│
├── src-tauri/ (Rust)
│   ├── adapters/
│   │   ├── gateway.rs        existing — WS to tui_gateway, TLS + token
│   │   └── dashboard.rs      NEW — HTTPS + SSE to dashboard backend
│   ├── connection_profile.rs NEW — owns the list, persists to Tauri config dir
│   ├── keyring.rs            existing — extended for dashboard_token
│   └── commands/
│       └── dashboard.rs      NEW — typed Tauri commands the React side calls
│
└── src/ (React)
    ├── routes/
    │   ├── chat/             existing — gateway-backed
    │   └── dashboard/        NEW — sessions, profiles, skills, config, keys...
    └── lib/
        └── dashboard-client.ts  NEW — typed wrapper over invoke()

Hermes (forked)
│
├── hermes/dashboard/
│   ├── server.py             PATCHED — TLS context, bearer middleware, /v1/meta
│   └── routes/               existing endpoints, now auth-required
│
└── hermes/gateway/           already patched in this fork
```

### Connection lifecycle

1. User opens Settings → Connections, adds a profile: name, base URL, Hermes token (paste once, lands in OS keyring; this is the same token the gateway adapter uses).
2. Tauri's `DashboardAdapter::probe(profile)` does `GET {base_url}/v1/meta` with bearer header. Returns version, available endpoints, advertised Hermes agent profiles, installed plugins.
3. The probe result is cached on the `ConnectionProfile` and pushed to the React side via Tauri event. Pages feature-flag off the cached capability set — including plugin pages, which only render if the corresponding plugin is in the installed list.
4. Pages call typed Tauri commands (`dashboard_list_sessions`, `dashboard_get_config_schema`, `dashboard_kanban_list_boards`, `dashboard_achievements_list`, etc.) that proxy through the adapter. The React side never holds the bearer token, never knows the base URL, never opens a `fetch` to Hermes directly.
5. On switch (`set_active_profile`), the adapter swaps the active client. No restart. Re-probes capabilities on switch in case the target version differs or has different plugins enabled.
6. Long-lived streams (run SSE for live session, log tail, kanban dispatcher events) flow through Tauri events, not direct EventSource. Same pattern as chat tokens come over the gateway adapter today.

### Security model

Two layers, separated cleanly:

- **Transport: TLS in process, cert issued by Let's Encrypt via `tailscale cert`.** The dashboard process loads `~/.hermes/tls/<host>.ts.net.{crt,key}` (the same files the gateway already loads — same hostname, different port, single SAN covers both) and terminates TLS itself. The cert is signed by Let's Encrypt's intermediate, chains to ISRG Root X1, and validates against any client's standard CA trust store. The Tauri client uses `webpki-roots` and does standard hostname validation; no pin field on `ConnectionProfile`, no manual trust install per client device.
- **Application auth: bearer token.** The fork's existing `API_SERVER_KEY` is the same bearer for both gateway and dashboard. Sent as `Authorization: Bearer …` on every request. Rejected requests return `401` with no information leak in the body. Token lives in the OS keyring on the Tauri side; the React layer never sees it.

These layers are orthogonal, not redundant. TLS protects the bytes on the wire (including the bearer token in the `Authorization` header) regardless of whether the client reaches Hermes via tailnet, LAN, or another path. The bearer authenticates the *application* once the bytes arrive. Either layer alone is insufficient: TLS without the token means anyone who can route to Hermes can use it; the token without TLS means anyone on the path can lift the token from a request and replay it.

How clients reach the cert's hostname is a deployment-side concern, not a protocol concern. Three working paths today:

- **Tailnet client** (other Mac, iPhone with Tailscale running) — Tailscale's MagicDNS resolves `<host>.ts.net` to the CGNAT IP. Standard.
- **LAN client without Tailscale** (current Windows situation) — `/etc/hosts` or its OS equivalent maps `<host>.ts.net` → the LAN `192.168.x.x` IP. The TLS handshake validates against the hostname requested, so the cert remains valid even though the bytes flow over LAN.
- **LAN-wide DNS override** — a custom DNS record on the home router pointing `<host>.ts.net` to the LAN IP, so every device on the LAN resolves it without per-device hosts edits. Preferable to per-device hosts files when there are more than two clients.

Bind defaults the fork enforces:

- Default bind: `127.0.0.1`. Loopback only — local processes only, no network exposure at all.
- Bind to `0.0.0.0` is allowed when TLS is configured (which is the default for this fork). The dashboard listens on the LAN/tailnet/whatever-the-Mac-can-be-reached-on, presents the LE cert, and rejects anything without the bearer. This is the configuration the user is actually running.
- Bind to `0.0.0.0` *without* TLS configured requires `HERMES_ALLOW_INSECURE_BIND=1`. The Hermes process logs a warning on every startup with this set, and on every request arriving on a non-loopback interface. This is intentionally annoying — the right answer is to provision a cert.

What this collapses out: there's no cert pinning in `ConnectionProfile`, no parallel rotation paths for two different secrets, no self-signed CA management. The gateway's existing token plumbing is the dashboard's token plumbing; the gateway's existing cert files are the dashboard's cert files.

### Operations: cert renewal

`tailscale cert` does not auto-renew. Let's Encrypt certs are valid for 90 days; on day 91 every Tauri client starts failing TLS handshake with `cert expired`. This is a deployment requirement, not a protocol concern, but it's the kind of thing that bites silently and badly so it lives in the doc.

The fork should ship a documented renewal procedure rather than try to automate it inside Hermes:

- A monthly `launchd` (macOS) or `systemd` timer that runs `tailscale cert --cert-file=… --key-file=… <host>.ts.net` and signals Hermes to reload. SIGHUP if the gateway and dashboard support hot reload of their TLS context, otherwise process restart.
- A startup-time check inside Hermes that reads the cert's `notAfter` and emits a structured warning to logs if the cert expires within 14 days. This makes monitoring trivial — the warning is an early-trigger alarm long before anything breaks.
- Optionally, a `/v1/meta` field surfacing `tls.expires_at` so the Tauri client can show a banner before things break. Not required, but cheap to add.

The Tauri side does not handle renewal; if the cert is expired or about to expire, the adapter surfaces a typed error to the React side and the user sees a clear "your Hermes cert is expiring, renew with `tailscale cert …`" prompt. The user does the renewal on the Hermes host, not via the Tauri app.

### Capability probing

`GET /v1/meta` returns:

```json
{
  "hermes_version": "0.12.0",
  "services": {
    "gateway":   { "available": true,  "url": "wss://…/gateway" },
    "dashboard": { "available": true,  "endpoints": ["/sessions", "/config", "/env", "/skills", "/cron", "/models", "/logs"] }
  },
  "plugins": [
    { "name": "achievements", "enabled": true,  "endpoints": ["/achievements"] },
    { "name": "kanban",       "enabled": true,  "endpoints": ["/kanban/boards", "/kanban/cards"] },
    { "name": "example",      "enabled": false, "endpoints": [] }
  ],
  "agent_profiles": ["default", "alice", "bob"],
  "active_profile": "default"
}
```

This single endpoint replaces the multi-probe dance hermes-workspace ended up with. The Tauri side reads it once on connect, caches it, and every other call is a cheap typed proxy. New endpoints land in `endpoints[]`, new plugins land in `plugins[]`, and the React side flips the relevant page from "this Hermes doesn't have it" to live without a frontend release.

## Design decisions

### Single base URL with internal routing

The fork patch adds a thin reverse-proxy mode to the dashboard process: when `HERMES_UNIFIED_BASE=1` is set, dashboard mounts at `/v1/dashboard/*` and proxies `/v1/gateway/*` through to the gateway port internally. The Tauri side configures one `base_url` per `ConnectionProfile` and never has to think about which port serves which capability.

Reasons:

- The hermes-workspace two-URL model was a footgun. The earliest published version had silent degradation when one URL was set and the other wasn't. They consolidated; we should start consolidated.
- One TLS cert, one token, one CORS policy. Three config knobs collapse to one.
- Users who don't run the dashboard process (e.g. headless server with chat only) get a clean 404 for `/v1/dashboard/*` rather than a "did you forget to set HERMES_DASHBOARD_URL" mystery.

**But if** the unified-base proxy adds enough latency or complexity to be irritating, we keep the patch minimal: dashboard process embeds a one-line Starlette mount that 307-redirects `/v1/gateway/*` to the gateway port on the same host. The Tauri client follows redirects within the same origin only. Costs ~one extra round-trip on first call, then cache.

**But if** users run gateway and dashboard on different hosts (split deployment, per the upstream "gateway proxy mode" docs), the unified-base mode is opt-out and `ConnectionProfile` falls back to two URLs with the silent-failure case caught at probe time and surfaced as a hard error in the UI.

### Capability probing over feature negotiation

`GET /v1/meta` returns a static-shaped capability document. The Tauri side reads it as data, not as protocol negotiation. There is no client→server "I want feature X" handshake.

Reasons:

- Matches the existing `client.hello` pattern in tui_gateway: client advertises, server gates. But in the dashboard direction the relationship reverses — server advertises, client gates UI. No need for two-way negotiation; the dashboard's job is to be honest about what it offers.
- Keeps the wire contract a plain HTTP GET. Trivial to debug with curl, no special tooling.
- Frontend feature flags become "is this endpoint in the capability list" which is one of the cheapest checks in software.

### TLS in process via `tailscale cert`-issued LE cert

Hermes terminates TLS itself, loading cert + key from `~/.hermes/tls/<host>.ts.net.{crt,key}`. The cert is issued by Let's Encrypt via Tailscale's ACME-DNS-01 flow. Both the gateway and the dashboard load the same cert; both run on the same hostname; clients reach them on different ports. The Tauri client does standard `webpki-roots` validation, no pin.

Reasons:

- The hostname in the cert's SAN (`<host>.ts.net`) is reachable from any client that can resolve the name and route packets to a host that presents the cert. Tailscale clients use MagicDNS; non-Tailscale clients use a hosts-file alias or LAN DNS. The cert remains valid because TLS validates against the hostname requested, not the underlying transport.
- Earlier drafts of this spec went back and forth on cert pinning vs pure-Tailscale-transport. Both were wrong. Pinning was wrong because the cert isn't self-signed — it's a real LE cert. Pure-Tailscale-transport was wrong because the user has clients (Windows) that aren't on the tailnet, so Tailscale isn't actually doing transport for them. The TLS-via-LE-cert model handles both cases with one pattern.
- Standard CA validation is the most boring, most well-understood security primitive. No ceremony for the user, no special-case code in the Tauri client.

**But if** the cert hostname is unreachable from a particular client (e.g. a teammate not on the tailnet, with no LAN access, and no DNS override available), the answer is *not* to add a second cert or a pinning bypass. It's to put a real reverse proxy with a different cert in front, exposed via Cloudflare Tunnel or similar. That's a deployment-shape change, not a protocol change.

**But if** the user moves Hermes off the Mac entirely (VPS scenario), they run `tailscale cert` on the VPS for whatever its `*.ts.net` hostname is, drop the cert files in the same path, and the existing config keeps working. The `ConnectionProfile` for the VPS holds a different `base_url` and a different bearer in keyring; everything else is identical.

### Single token for gateway and dashboard

The dashboard middleware reads the same `API_SERVER_KEY` env var the gateway already uses. One token, one rotation, one keyring entry on the Tauri side. The `ConnectionProfile.hermes_token` is shared between both adapters.

Reasons:

- Solo deployments have a single trust boundary. Splitting into two tokens implies a separation that doesn't exist — a compromised gateway token gives the attacker code execution via tool calls anyway, so a separate dashboard token doesn't help.
- Hermes-workspace had `HERMES_API_TOKEN` and `HERMES_DASHBOARD_TOKEN` as separate vars and ended up with a "legacy HTML-scrape fallback" because the dashboard's auth surface wasn't always clean. Reusing the gateway token from the start sidesteps that mess; clean bearer middleware on the dashboard, identical shape to the gateway.
- One rotation path. When the user runs `openssl rand -hex 32` and updates the env var, both surfaces update together. No drift.

**But if** there's a future use case for a dashboard-only token (e.g. a read-only audit reader that shouldn't be able to invoke runs through the gateway), introduce it then as a *second* accepted token rather than splitting the existing one. The middleware can accept a list and tag each token with a scope. This is a cheap extension of the single-token base, not a change to it.

### Patch the dashboard with TLS + bearer middleware, mirror of the gateway

We add TLS termination and bearer-token middleware to the dashboard's Starlette/FastAPI app, sharing both the gateway's TLS context (same cert/key files) and `API_SERVER_KEY`. The dashboard fork patch is a near-copy of whatever the gateway's TLS-loading code already does.

Reasons:

- Symmetry with the gateway patch. Same cert files, same env var, same middleware shape, same rotation procedure. One mental model for both surfaces.
- The fork already has the cert-loading code working in the gateway. Cloning it for the dashboard is mechanical, not architectural.
- The user already has the renewal procedure (whatever it is) for the gateway's cert. By using the same files, the dashboard inherits that procedure for free.

**But if** the dashboard and gateway turn out to live in genuinely separate Python processes (worth confirming at source-pass time — see open questions), the patch needs to be a small TLS-loading helper module they both import, rather than copying code between them. Same files, shared loader.

**But if** the user later wants to terminate TLS in front of Hermes via a real reverse proxy (Caddy, Cloudflare Tunnel) — for instance to expose the agent to a teammate not on the tailnet — the in-process TLS becomes redundant but harmless. The dashboard can keep terminating TLS internally and the proxy passes through, or the proxy strips TLS and the dashboard switches to `bind=127.0.0.1` HTTP behind it. The middleware doesn't care; the bearer keeps working. Out of scope for this design but compatible with it.

### Settings UI re-probes on save, no restart

The Connections page has an Edit form for each profile. Hitting Save triggers `DashboardAdapter::reconnect(profile)` which closes the old client, opens a new one, and re-runs `GET /v1/meta`. The pages re-render off the new capability set. No app restart, no reload.

Reasons:

- Hermes-workspace specifically called this out as an improvement over their initial design. It's the difference between "I tweaked the URL and now everything is broken until I reopen" and "I tweaked the URL and the UI rerendered with the new state". The latter is non-negotiable for a daily-driver tool.
- The capability set is the single source of truth for which pages are reachable. Re-probing on reconnect means stale features disappear immediately when the user points at an older Hermes.

## Cross-machine alignment tests

These are the contracts the Tauri side and the Hermes fork must agree on, and that should land as tests on both sides:

- **`/v1/meta` shape.** Server returns the schema above (services, plugins, agent_profiles, version); client parses without crashing on unknown extra fields. Forward-compat by construction.
- **Bearer rejected = 401, not 403/404.** Tauri side distinguishes "wrong token" from "endpoint missing" from "permission denied" and surfaces different UI. Server must use 401 for token problems.
- **Same token works for gateway and dashboard.** A request to the gateway and a request to the dashboard with the same `Authorization: Bearer …` both succeed. The fork's middleware is shared.
- **Same cert covers both surfaces.** Both gateway and dashboard ports present a cert whose SAN includes the configured `<host>.ts.net`. Test by connecting on each port and verifying the cert chain back to ISRG Root X1.
- **Hostname validation strict.** Tauri client refuses to connect if the server presents a cert whose SAN doesn't include the hostname in `ConnectionProfile.base_url`. No "skip verify" path.
- **Cert near-expiry surfaces in `/v1/meta`.** When the loaded cert is within 14 days of `notAfter`, `/v1/meta` includes a `tls.expires_soon: true` flag. (Optional — if the fork chooses not to surface this, the doc is wrong, not the test.)
- **Insecure non-loopback bind without TLS refused without escape hatch.** `host=0.0.0.0` with no TLS configured and `HERMES_ALLOW_INSECURE_BIND` unset → server fails to start with a clear error. With TLS configured (the default) → starts cleanly.
- **Plugin gating round-trips.** Pages that depend on `plugins[].enabled` render only when the plugin is enabled. Enabling Achievements server-side surfaces in the UI on the next reconnect with no client release.
- **Capability gating round-trips.** Pages that depend on `endpoints[]` containing a path render only when the path is present. Adding a new endpoint server-side surfaces in the UI on the next reconnect with no client release.

## Open questions
