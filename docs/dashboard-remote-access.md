# Dashboard remote access

Operator setup for reaching `hermes dashboard` from a remote client (Tauri desktop app, second laptop, phone). Covers zero-config local, off-loopback HTTPS via Tailscale, env-var + flag reference, troubleshooting.

For the protocol contract and design rationale, see `plans/dashboard-fork-spec.md`. This doc is the "I want to actually run it" side.

## TL;DR

```
# Local browser only — zero config:
hermes dashboard
# → http://127.0.0.1:9119

# Remote Tauri / phone / second laptop — TLS + bearer:
tailscale cert myhost.ts.net
mkdir -p ~/.hermes/tls && mv myhost.ts.net.{crt,key} ~/.hermes/tls/

# API_SERVER_KEY is shared with the gateway. If it's already set
# (for the gateway), the dashboard reuses it — same value, same env var,
# one rotation. Only generate a new one if it's not yet set:
grep -q '^API_SERVER_KEY=' ~/.hermes/.env \
  || echo "API_SERVER_KEY=$(openssl rand -hex 32)" >> ~/.hermes/.env

hermes dashboard --host 0.0.0.0
# → https://myhost.ts.net:9119  (paste API_SERVER_KEY into the Tauri client)
```

## Zero-config local

```
hermes dashboard
# Hermes Web UI → http://127.0.0.1:9119
```

Loopback bind, plaintext HTTP, ephemeral session token injected into the SPA. Browser opens automatically. No env vars to set, no certs needed.

This is the right setup when you're using the SPA on the same machine that runs Hermes. The ephemeral token lives in memory only — it dies when the dashboard process exits, so there's no secret to manage.

## Off-loopback HTTPS via Tailscale

The dashboard binds to `127.0.0.1` by default. Going off-loopback requires both TLS and a bearer token; the bind guard refuses any other configuration unless you explicitly opt out.

### One-time setup

```
# 1. Provision a Let's Encrypt cert via Tailscale
tailscale cert myhost.ts.net
# Writes ./myhost.ts.net.{crt,key} to the current directory.

# 2. Move the pair into ~/.hermes/tls/
mkdir -p ~/.hermes/tls
mv myhost.ts.net.crt myhost.ts.net.key ~/.hermes/tls/

# 3. Set the API key. Same key for gateway + dashboard — one bearer,
#    one rotation. If it's already set (for the gateway), reuse it.
grep -q '^API_SERVER_KEY=' ~/.hermes/.env \
  || echo "API_SERVER_KEY=$(openssl rand -hex 32)" >> ~/.hermes/.env
```

### Run

```
hermes dashboard --host 0.0.0.0
# Hermes Web UI → https://0.0.0.0:9119
# Reach it from any client at https://myhost.ts.net:9119
```

The dashboard auto-discovers the single `*.ts.net.{crt,key}` pair in `~/.hermes/tls/` and terminates TLS. Every `/api/*` request now requires `Authorization: Bearer <API_SERVER_KEY>`.

### Reaching the dashboard

The cert's SAN is `myhost.ts.net`. Validation passes only when the client requests that hostname.

| Client | URL |
|---|---|
| Same machine | `https://myhost.ts.net:9119` (Tailscale resolves on-host) or `https://localhost:9119` |
| Tailnet device | `https://myhost.ts.net:9119` (MagicDNS resolves to CGNAT IP) |
| LAN device, no Tailscale | Add `<LAN-IP> myhost.ts.net` to `/etc/hosts`, or override on the home router's DNS |

Hitting the bare LAN IP (`https://192.168.x.y:9119`) fails hostname validation. Use `-k` with curl for testing only.

## Env-var reference

| Var | Role | When to set |
|---|---|---|
| `API_SERVER_KEY` | Bearer token shared by gateway + dashboard for every authenticated route. **One key, both surfaces** — set once, used everywhere. | Required for any non-loopback bind. If unset, `openssl rand -hex 32`; if you already use the gateway, reuse its value. |
| `HERMES_TLS_CERT` | Explicit cert file path (overrides auto-discovery) | Cert lives outside `~/.hermes/tls/` |
| `HERMES_TLS_KEY` | Explicit key file path | Pair with `HERMES_TLS_CERT` |
| `HERMES_TLS_HOST` | Tailscale hostname → `~/.hermes/tls/<HERMES_TLS_HOST>.{crt,key}` | Multiple `*.ts.net.*` files in `~/.hermes/tls/` and you need to pick |
| `HERMES_ALLOW_INSECURE_BIND` | `1` = "trust this LAN" mode. Bind off-loopback without TLS, skip TLS auto-discovery (plaintext on the wire), and apply loopback-style auth: SPA bootstrap + ephemeral session token works in the browser. | Trusted-LAN browser access without going through the Tauri client. Logs a loud warning every request — intentionally annoying. |
| `HERMES_DASHBOARD_TUI` | `1` = enable embedded chat tab + WebSocket endpoints | Optional, also set by `--tui` |
| `HERMES_HOME` | Profile directory override | Existing, see profile docs; not new in this patch |

### Cert path resolution priority

The dashboard's `_resolve_tls_paths(host)` walks four steps:

1. **`HERMES_TLS_CERT` + `HERMES_TLS_KEY`** — explicit override. Always honored, regardless of bind.
2. **`HERMES_TLS_HOST`** → `~/.hermes/tls/<HERMES_TLS_HOST>.{crt,key}`. Always honored, regardless of bind.
3. **Glob auto-discovery** — `~/.hermes/tls/*.ts.net.{crt,key}` if exactly one match exists. **Off-loopback only** — loopback binds stay plaintext even if cert files are sitting on disk.
4. **None** — `_TLS_CONTEXT` stays unset; bind guard refuses non-loopback binds without `HERMES_ALLOW_INSECURE_BIND=1`.

The gateway uses the simpler version: only step 1 (explicit env vars). For multi-surface deployments, set `HERMES_TLS_CERT` + `HERMES_TLS_KEY` once and both surfaces pick them up.

## CLI flags

```
hermes dashboard [--host HOST] [--port PORT] [--no-open] [--insecure] [--tui]
```

| Flag | Default | Effect |
|---|---|---|
| `--host` | `127.0.0.1` | Bind interface |
| `--port` | `9119` | Bind port |
| `--no-open` | open | Skip browser auto-open |
| `--insecure` | off | Equivalent to `HERMES_ALLOW_INSECURE_BIND=1`. "Trust this LAN" mode: plaintext bind, skip TLS auto-discovery, loopback-style auth (SPA + ephemeral token work) |
| `--tui` | off | Enable embedded chat tab (WebSocket endpoints `/api/ws`, `/api/pty`, etc.) |

## Surfaces and where each key lives

Hermes ships **three** network surfaces on this branch, with **two** auth systems. Worth nailing down because the confusion is real if you've been using the chat adapter and now set up the dashboard.

| Surface | Default port | Auth scheme | Server-side storage |
|---|---|---|---|
| **Gateway** (OpenAI-compat HTTP+SSE — `gateway/platforms/api_server.py`) | `8642` | Single shared bearer | `API_SERVER_KEY` env (e.g. `~/.hermes/.env`) |
| **Dashboard** (mgmt REST + embedded SPA — `hermes_cli/web_server.py`, this patch) | `9119` | Single shared bearer (**same `API_SERVER_KEY`**) | `~/.hermes/.env` |
| **DesktopAppAdapter** (chat WebSocket — `gateway/platforms/desktop_app.py`) | `8645` | **Per-client paired tokens**, revocable per name | `~/.hermes/desktop_app_tokens.json` (SHA-256 hashes only) |

Gateway + dashboard share one bearer because they're the same trust boundary (admin access to your agent). The DesktopAppAdapter has its own per-client token system because the chat surface is per-client identifiable — pair `tauri-windows`, pair `tauri-mac`, revoke either independently.

### Inspect what you have

```
# Dashboard / gateway shared key:
grep '^API_SERVER_KEY=' ~/.hermes/.env

# DesktopAppAdapter paired clients (hashes; plaintext is gone):
cat ~/.hermes/desktop_app_tokens.json
```

### Hermes-side: how each gets set

**`API_SERVER_KEY`** — generate once, write to env, both gateway and dashboard read it:

```
echo "API_SERVER_KEY=$(openssl rand -hex 32)" >> ~/.hermes/.env
```

If a value is already there (e.g. you set up the gateway earlier), keep it — the dashboard reuses the same value. Don't append a second `API_SERVER_KEY=...` line; depending on `.env` parsing, one silently wins.

**Desktop-adapter pairing** — uses a separate CLI; mints a one-time plaintext you paste into the client:

```
hermes desktop pair --client-name tauri-windows
# → prints a token (shown once, not recoverable). Hash lands in
#   ~/.hermes/desktop_app_tokens.json. Paste plaintext into the client.

hermes desktop list           # show paired clients
hermes desktop revoke tauri-windows   # remove one
```

### Client side

| Client → server | What you send |
|---|---|
| Browser / curl / Tauri *DashboardAdapter* (deferred) → dashboard `:9119` | `Authorization: Bearer <API_SERVER_KEY>` |
| Custom OpenAI-compat client → gateway `:8642` | `Authorization: Bearer <API_SERVER_KEY>` (same value) |
| Tauri chat client → DesktopAppAdapter `:8645` | `Authorization: Bearer <paired-plaintext-from-`hermes desktop pair`>` (different value) |

The Tauri-side `DashboardAdapter` itself is **deferred** (see `plans/dashboard-fork-spec.md` §1) — it lives in the desktop app repo and isn't implemented on this branch. So there's no dashboard keyring entry on a stock Tauri install yet. The `tauri-windows` paired token you already have in your keyvault is for the chat WS surface (`:8645`), not the dashboard. When the dashboard adapter lands client-side, it'll add a second keyvault entry for `API_SERVER_KEY`. Two surfaces, two stored values — by design.

For testing the dashboard today (no Tauri DashboardAdapter yet): paste `API_SERVER_KEY` into curl's `Authorization: Bearer` header, or whichever HTTP client you use.

## Auth surface — what's gated, when

The dashboard's auth model is bind-aware. **Strict off-loopback** (default for non-loopback binds) requires a bearer on every path, including `/`, `/docs`, `/openapi.json`, and static assets — there's no unauthenticated surface on the network. **`--insecure` (or `HERMES_ALLOW_INSECURE_BIND=1`) explicitly relaxes this back to loopback semantics** for trusted-LAN browser access.

| Route class | Loopback bind | Off-loopback bind |
|---|---|---|
| `/api/status`, `/api/config/schema`, `/api/model/info`, `/api/dashboard/themes`, `/api/dashboard/plugins`, `/api/dashboard/plugins/rescan`, `/api/config/defaults` (the `_PUBLIC_API_PATHS` list) | **Public** — no bearer needed (SPA bootstrap) | **Bearer required** — exemption is loopback-only |
| All other `/api/*` routes (sessions, env, cron, plugins/*, etc.) | Bearer required (ephemeral session token or `API_SERVER_KEY`) | Bearer required (`API_SERVER_KEY` only — ephemeral rejected because it leaks via the served SPA HTML) |
| WebSocket upgrades (`/api/ws`, `/api/pty`, `/api/pub`, `/api/events`) | Bearer required (`Authorization` / `X-Hermes-Session-Token` / legacy `?token=`) | Bearer required (`API_SERVER_KEY` only) |
| `/` SPA HTML | Public (the SPA needs to render to bootstrap) | **Bearer required** — the HTML embeds the loopback ephemeral token; serving it on the network leaks the format |
| `/docs`, `/redoc`, `/openapi.json` (FastAPI auto-docs) | Public (dev convenience) | **Bearer required** — `/docs` enumerates every endpoint + schema, a reconnaissance leak |
| `/static/*`, `/assets/*`, JS/CSS chunks, fonts | Public | **Bearer required** — reduces fingerprinting; SPA-over-network isn't a supported use case |

Off-loopback rejections always return `401 Unauthorized` (never 403, never 404), so the Tauri side can distinguish "wrong token" from "endpoint missing" cleanly.

### Why "browser SPA over network" isn't supported

Off-loopback strict mode means a browser hitting `https://myhost.ts.net:9119/` over the network gets `401` — the SPA HTML never loads. This is intentional:

- The SPA can't natively send `Authorization: Bearer ...` headers from a fresh-tab navigation. Browser auth flows (cookies, basic auth) aren't wired here.
- The intended off-loopback consumer is the Tauri `DashboardAdapter`, which holds the bearer in keyvault and presents it on every request.
- For phone or remote-machine browser use: tunnel `localhost:9119` over Tailscale (`tailscale serve` or SSH-port-forward), so the browser hits the loopback path. Or wait for the Tauri DashboardAdapter to land.

If you genuinely need browser-SPA-over-network — e.g. a kiosk on the LAN — `--insecure` is the supported escape hatch. It does three things together:

1. Allows the off-loopback bind without TLS (the bind guard accepts plaintext).
2. Skips the TLS auto-discovery so cert files lying around in `~/.hermes/tls/` don't silently flip the bind to HTTPS — the printed scheme stays `http://`.
3. Restores loopback-style auth: the `_PUBLIC_API_PATHS` exemption applies, the SPA HTML at `/` is served unauthenticated so the browser can bootstrap, and the ephemeral session token injected into that HTML works for `/api/*` calls.

You're explicitly opting into "trust this LAN" — every request logs a warning. Use it for kiosks, LAN-trusted setups, or quick "I want to poke at the dashboard from my phone over Wi-Fi" workflows. Don't use it on networks you don't trust.

The alternative for trust-skeptical setups is to tunnel loopback: `tailscale serve` or `ssh -L 9119:localhost:9119` from the kiosk to the Hermes host, so the browser hits a true loopback path on its own machine. That keeps strict mode active server-side while giving the browser its SPA bootstrap.

## Tauri client side

The Tauri desktop adapter is the consumer of the contract documented in `plans/dashboard-fork-spec.md` §5 (`/api/meta` shape) and §6 (auth model).

Per `ConnectionProfile`, the Tauri side needs:

- **Base URL** — `https://myhost.ts.net:9119` (must match cert SAN).
- **Bearer token** — paste the same `API_SERVER_KEY` once; lands in OS keyring (Keychain / Credential Manager / Secret Service).
- **Optional gateway URL** — `https://myhost.ts.net:8642` if you also run the gateway. Same `API_SERVER_KEY`, same cert files, same env vars.

The Tauri side does standard webpki-roots TLS validation against ISRG Root X1. No pinning, no per-device trust install. The Tauri side does **not** read its own env vars — config lives entirely in the `ConnectionProfile` UI.

On connect, the adapter does `GET /api/meta` once with the bearer header, caches the capability set, and feature-flags pages off the result.

## Cert renewal

`tailscale cert` does not auto-renew. Let's Encrypt certs expire every 90 days. The fork ships no auto-renewal — see `plans/dashboard-fork-spec.md` §7 "Renewal".

The dashboard surfaces `tls.expires_soon: true` in `/api/meta` when `not_after` is within 14 days. The Tauri side can render a banner on this signal. Same warning fires in the dashboard's startup log.

### macOS — sample launchd plist

Drop into `~/Library/LaunchAgents/com.local.hermes-cert-renew.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.local.hermes-cert-renew</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-c</string>
    <string>cd ~/.hermes/tls &amp;&amp; tailscale cert myhost.ts.net</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Day</key><integer>1</integer>
    <key>Hour</key><integer>3</integer>
  </dict>
</dict>
</plist>
```

`launchctl load ~/Library/LaunchAgents/com.local.hermes-cert-renew.plist` to enable. Renewal runs the 1st of every month at 03:00. After renewal, restart `hermes dashboard` manually (the cert is re-read at startup).

### Linux — sample systemd timer

`/etc/systemd/system/hermes-cert-renew.service`:

```
[Unit]
Description=Renew Tailscale-issued cert for Hermes dashboard

[Service]
Type=oneshot
WorkingDirectory=/home/USER/.hermes/tls
ExecStart=/usr/bin/tailscale cert myhost.ts.net
```

`/etc/systemd/system/hermes-cert-renew.timer`:

```
[Unit]
Description=Monthly Hermes cert renewal

[Timer]
OnCalendar=monthly
Persistent=true

[Install]
WantedBy=timers.target
```

`systemctl enable --now hermes-cert-renew.timer`.

## Troubleshooting

### `ERR_EMPTY_RESPONSE` / `Empty reply from server`

The dashboard is serving HTTPS but your client is sending HTTP. Check the startup line:

```
Hermes Web UI → https://0.0.0.0:9119
```

If it says `https`, switch your client to `https://` URLs. From the browser, use the cert's SAN hostname (`https://myhost.ts.net:9119`); from curl, `curl -k https://...` for IP-based testing.

### `Refusing to start: binding to 0.0.0.0 requires TLS`

Off-loopback bind with no cert. Either:

- Run `tailscale cert myhost.ts.net`, move the files into `~/.hermes/tls/`.
- Or set `HERMES_ALLOW_INSECURE_BIND=1` (don't — your traffic is plaintext on the LAN, every request logs a warning).

### `Refusing to start: binding to 0.0.0.0 requires API_SERVER_KEY`

Cert exists but no bearer key. `export API_SERVER_KEY=$(openssl rand -hex 32)` and add it to `~/.hermes/.env`.

### "I already paired my Tauri client (chat) — isn't that the dashboard key?"

No, different surface. The token you generated via `hermes desktop pair --client-name <X>` and pasted into your Tauri client is for the **DesktopAppAdapter** chat WebSocket on port `8645`. Hashed in `~/.hermes/desktop_app_tokens.json`. The plaintext only exists in your keyvault now.

The **dashboard** (port `9119`, this patch) uses a different auth system: a single shared `API_SERVER_KEY` env var, the same one the gateway uses. If you don't already have it set, generate one separately:

```
grep -q '^API_SERVER_KEY=' ~/.hermes/.env \
  || echo "API_SERVER_KEY=$(openssl rand -hex 32)" >> ~/.hermes/.env
```

When the Tauri-side DashboardAdapter ships (it's deferred — lives in the desktop app repo, not on this branch), it'll store `API_SERVER_KEY` in your keyvault as a second entry alongside the chat token.

### "I already had an API_SERVER_KEY for the gateway — do I need a new one for the dashboard?"

No. Gateway + dashboard share `API_SERVER_KEY`. Set once in `~/.hermes/.env`, both surfaces use it. Don't append a second `API_SERVER_KEY=...` line — `.env` parsing usually has one silently winning. Run `grep '^API_SERVER_KEY=' ~/.hermes/.env` to see what's currently set.

### `Refusing to start: API_SERVER_KEY is set to a placeholder value`

Your key is too short, repetitive, or matches a known placeholder (`changeme`, `xxx`, etc.). Generate a real one: `openssl rand -hex 32`.

### `401 Unauthorized` on every request

The Tauri side is sending the wrong bearer. Double-check:

- The token in the Tauri keyring matches `API_SERVER_KEY` exactly (no trailing newline if you copied from `.env`).
- Off-loopback rejects the ephemeral session token even if presented — only `API_SERVER_KEY` works off-loopback.
- The bearer goes in `Authorization: Bearer <key>`; the legacy `?token=` query param works for WebSocket upgrades but not HTTP routes.

### TLS certificate hostname mismatch

The cert's SAN is `myhost.ts.net` only. Reaching the dashboard via `https://192.168.x.y:9119` makes the browser refuse the cert. Either:

- Use the hostname (configure DNS, hosts-file, or rely on Tailscale MagicDNS).
- For curl testing only: `-k` to skip validation, or `--resolve myhost.ts.net:9119:192.168.x.y` to map the hostname to the LAN IP.

### Cert expired

`https://...:9119/api/status` returns a TLS error mentioning `cert expired`. Re-run `tailscale cert myhost.ts.net`, drop the new files in `~/.hermes/tls/`, restart `hermes dashboard`. The dashboard reloads the cert on startup.

The 14-day expiry warning fires in both the startup log and `/api/meta.tls.expires_soon`. If the Tauri client sees `expires_soon: true` it should surface a banner.

### `hermes dashboard` opens browser to `http://127.0.0.1:9119` but you wanted HTTPS

Default loopback binds stay plaintext even with cert files on disk. To get HTTPS on loopback (rare — loopback is already private), set `HERMES_TLS_HOST=myhost.ts.net` (or `HERMES_TLS_CERT/KEY`) and `API_SERVER_KEY`, and the explicit env-var path overrides the loopback skip.

## Related docs

- `plans/dashboard-fork-spec.md` — the protocol contract (endpoint shapes, auth model, plugin surface, alignment tests).
- `plans/desktop-app-adapter.md` — companion plan for the Tauri-side WebSocket chat surface (separate from this REST dashboard).
