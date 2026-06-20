# Hermes Standalone Desktop

Remote-only desktop client for a [Hermes Agent](../../README.md) backend running on another machine. It runs no agent and no local backend — it connects to a `hermes dashboard` over the network (JSON-RPC over `/api/ws` + REST). Stripped fork of [`apps/desktop`](../desktop/README.md); for the self-installing app with a bundled local backend, use that instead.

## Prerequisites

- A `hermes dashboard` already running and reachable on the backend machine — this client attaches to it, it does not start it.
- Bound to a non-loopback address (so the chat WebSocket is reachable). This engages the auth gate.
- An auth provider configured (`dashboard.basic_auth` or OAuth). Do not use `--insecure` for remote access.

## Run the backend

```bash
pip install 'hermes-agent[web,pty]'        # plus Node.js for the chat surface
```

Set auth in `~/.hermes/config.yaml` (hash, no plaintext at rest):

```bash
python -c "from plugins.dashboard_auth.basic import hash_password; from getpass import getpass; print(hash_password(getpass('Password: ')))"
```

```yaml
dashboard:
  basic_auth:
    username: admin
    password_hash: "scrypt$..."
    secret: "<32+ random bytes>"   # openssl rand -base64 32 — stable secret survives restarts
```

HTTP:

```bash
hermes dashboard --no-open --host <host> --port 9119
```

HTTPS (upstream dashboard is HTTP-only; this loader adds TLS without touching core):

```bash
tailscale cert <host>              # or any cert/key pair; gives a trusted cert
python scripts/dashboard_tls.py --host <host> --port 9119 --cert <host>.crt --key <host>.key
```

Verify the gate: `curl -s http(s)://<host>:9119/api/status | jq '.auth_required, .auth_providers'` → `true` and `["basic"]`.

## Connect the client

Settings → Gateway → Remote gateway → Remote URL `http(s)://<host>:9119` → Sign in. (The `HERMES_DESKTOP_REMOTE_URL` env var is token-auth only; use the in-app sign-in for basic auth.)

For HTTPS, connect by the exact hostname the cert is for, and the client must resolve it.

## Build & run from source

Detached from the repo workspace: its own `node_modules` + `package-lock.json`, with `@hermes/shared` vendored under `./shared`.

```powershell
cd apps/standalone-desktop
npm ci --no-workspaces --ignore-scripts
node node_modules/electron/install.js      # fetch Electron binary (scripts are off)
npm run dev                                # dev
npm run dist:win:nsis                      # installer → release/  (also dist:win:msi, dist:mac, dist:linux)
```

If `ELECTRON_RUN_AS_NODE=1` is set in your shell, unset it (Electron crashes on `app.isPackaged` otherwise).

## Reference docs

- Remote backend, auth providers, WS close-code triage: [web-dashboard.md](../../website/docs/user-guide/features/web-dashboard.md)
- Desktop remote-backend walkthrough: [desktop.md](../../website/docs/user-guide/desktop.md)
- Env vars: [environment-variables.md](../../website/docs/reference/environment-variables.md)

## Differences from `apps/desktop`

No bootstrap or local backend (`startHermes` resolves a remote or errors); isolated install; `@hermes/shared` vendored; first run opens the remote-gateway connection screen instead of a provider wizard.

## Troubleshooting

- Crash on `app.isPackaged` → `ELECTRON_RUN_AS_NODE=1` is set; unset it.
- "No remote Hermes backend configured" → set the Remote URL in Settings → Gateway.
- No response on `https://` → upstream dashboard is HTTP-only; use `scripts/dashboard_tls.py` or `http://`.
- Chat WS won't connect → `desktop.log` close code: `4403` Host/peer mismatch, `4401` auth ticket.
- Signed out on restart → set `dashboard.basic_auth.secret`.
- Installer fails with `EPERM`/`EBUSY` on `release\win-unpacked\...app.asar` → Windows file lock (Defender/indexer/IDE); close the IDE, delete `release\`, rebuild.

## License

MIT — see [LICENSE](../../LICENSE). Derived from Hermes Desktop by Nous Research.
