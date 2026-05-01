# Hermes TLS Setup — Handoff for the Mac mini Claude session

> **For the Claude Code session running on the Mac mini that has the
> `hermes-agent` source in `~/.hermes/hermes-agent/` (or wherever the user
> cloned it).**
>
> This handoff was written from the Tauri/desktop side after diagnosing
> exactly why the desktop client stopped connecting on 2026-05-01. Read all
> of it before touching code; the diagnosis matters because it tells you what
> NOT to change.

## What you're solving

The Tauri desktop client is dialing `wss://192.168.32.2:8645/ws` (TLS).
Hermes is listening on `ws://192.168.32.2:8645/ws` (cleartext). Result on
the Hermes side:

```
DEBUG aiohttp.server: Error handling request from 192.168.32.7
aiohttp.http_exceptions.BadHttpMethod: 400, message:
  Invalid method encountered:
    b'\x16\x03\x01'
      ^
INFO aiohttp.access: 192.168.32.7 [...] "UNKNOWN / HTTP/1.0" 400 214 "-" "-"
```

Those bytes `\x16\x03\x01` are the start of a TLS ClientHello record (0x16
= Handshake, 0x0301 = TLS protocol version) — aiohttp's HTTP parser sees
them as garbage. On the Tauri side rustls reads aiohttp's plaintext "400
Bad Request" response and rejects it with `received corrupt message of type
InvalidContentType`.

### What changed

The desktop side commit `afe6b055` (2026-04-29, "refactor: enhance hooks
and services for improved functionality and testing") quietly swapped a
`format!("ws://{}:{}/ws", host, port)` for a new `build_ws_url` helper that
upgrades any non-loopback host to `wss://`. The justification (legitimate)
is that the bearer token must not cross the wire in plaintext.

Hermes was never updated to match. The fix is to add TLS termination on
the Hermes side. The user does NOT want plaintext, including for health
checks. They explicitly said loopback-only-on-the-mac-mini is fine — but
the Hermes server is reached from another machine over the network, so
loopback isn't an option for the WS server. We need real TLS.

## Decision: Tailscale-issued Let's Encrypt certs

The Mac mini and the user's Windows desktop are both on Tailscale (the
desktop dials `192.168.32.2`, which is the Mac mini's Tailscale-assigned
LAN address — confirmed by user). Tailscale issues real Let's Encrypt
certs for `<hostname>.<tailnet>.ts.net` automatically, and those certs are
trusted by the desktop's OS root store + the rustls webpki-roots bundle
the Tauri client uses. **No CA installation work on the desktop.**

The Tauri side already has the right Rust dependency features:

- `tokio-tungstenite` with `rustls-tls-webpki-roots` (Mozilla CA bundle,
  includes Let's Encrypt's ISRG Root X1).
- `reqwest` with `rustls-tls` and `macos-system-configuration` (uses OS
  root store on macOS and Windows).

So once Hermes serves a Tailscale-issued cert, the desktop validates it
with zero code changes on the trust path.

The other Tauri patch (HTTPS health check) is being handled by the
desktop-side Claude session in parallel — you don't have to coordinate
with it.

## Tailscale primer (~2 min read)

Tailscale is a mesh VPN that gives every machine on the user's tailnet a
stable hostname + IP, end-to-end encrypted between peers, transparent NAT
traversal. The user is already running it (the `192.168.32.x` is a
Tailscale-managed range).

Concepts you need:

| Term | What it means |
|---|---|
| **Tailnet** | The user's private mesh. Every Tailscale account has exactly one. Identified by a name like `tail<random>.ts.net` or a chosen name like `acme.ts.net`. |
| **MagicDNS hostname** | Each machine on the tailnet gets a name like `mac-mini.<tailnet>.ts.net`. Resolves only inside the tailnet. |
| **Tailscale cert** | A real Let's Encrypt cert issued by Tailscale's ACME proxy for any MagicDNS hostname. Renewable, browser-trusted. |

### How the user finds their tailnet name

Three ways, easiest first:

1. **Tailscale menu bar icon** (macOS): click the icon → the dropdown shows
   the machine's full MagicDNS name like `mac-mini.taild1234.ts.net`. The
   `taild1234.ts.net` part is the tailnet name.
2. **CLI**:
   ```bash
   tailscale status
   ```
   First output line is `<ip>  <hostname>  <user>  <os>  <conn-status>`,
   and below it is the full machine name.
   ```bash
   tailscale dns status | head -5
   ```
   Shows the MagicDNS suffix explicitly.
3. **Web admin**: <https://login.tailscale.com/admin/dns> — the page
   header reads "Tailnet name: `tail1234.ts.net`" and a "Rename" button.
   The user can rename it to something memorable here (e.g.
   `anandia.ts.net`); the rename takes effect within seconds.

Confirm you have the tailnet name before issuing certs — `tailscale cert`
will fail if MagicDNS or HTTPS are off in the admin console.

### Prerequisites in the Tailscale admin console

Open <https://login.tailscale.com/admin/dns>. Make sure both are enabled:

- [x] **MagicDNS** (top section toggle)
- [x] **HTTPS Certificates** (further down, "Enable HTTPS")

Without HTTPS Certificates enabled, `tailscale cert` returns a clear error
message. If it's off, toggle it on; takes effect immediately.

## Step-by-step on the Mac mini

### 1. Verify Tailscale is up and you have HTTPS enabled

```bash
tailscale status      # confirm machine is connected
tailscale cert --help # confirm the cert subcommand exists (Tailscale ≥ 1.30)
```

If `tailscale cert` complains about HTTPS not being enabled, walk the user
through the admin console toggle above before continuing.

### 2. Get the Mac mini's full MagicDNS hostname

```bash
hostname=$(tailscale status --json | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['Self']['DNSName'].rstrip('.'))")
echo "$hostname"
# example output: mac-mini.taild1234.ts.net
```

Save that — every command below substitutes it.

### 3. Issue the cert

`tailscale cert` writes two PEM files into the current directory:

```bash
mkdir -p ~/.hermes/tls
cd ~/.hermes/tls
sudo tailscale cert "$hostname"
ls -l "$hostname".crt "$hostname".key
```

Output is the cert + private key. **The private key is mode 0600 and owned
by root** because `sudo` ran the command. You have two reasonable options:

**Option A (preferred):** chown the files to the user that runs Hermes,
keep them mode 0600.

```bash
sudo chown buddy:staff "$hostname".crt "$hostname".key
chmod 0600 "$hostname".key
chmod 0644 "$hostname".crt
```

(Replace `buddy:staff` with whatever the Hermes process runs as. If
unsure: `ps -ef | grep hermes` while the agent is running.)

**Option B:** keep root ownership and run Hermes as root (don't).

### 4. Renewal

Tailscale-issued LE certs expire every 90 days. `tailscale cert` is
idempotent — re-running it before expiry rotates the file. Add a
launchd plist or cron entry:

```bash
crontab -e
# add a line:
0 4 * * 0  cd ~/.hermes/tls && /usr/local/bin/tailscale cert <hostname> >/dev/null && /bin/launchctl kickstart -k user/$(id -u)/<hermes-launchd-label>
```

(Adjust the kickstart label to whatever launchd job runs Hermes — or
restart the systemd/whatever-supervisor used.)

If the user runs Hermes from a tmux/screen session manually, the renewal
script can `pkill -HUP hermes-agent` if Hermes handles SIGHUP for cert
reload (most don't — restart is fine for a 90-day cadence).

### 5. Wire into Hermes (aiohttp)

The Hermes adapter currently launches with something like
`web.run_app(app, host="0.0.0.0", port=8645)`. **Find the call site first**
— grep the repo:

```bash
cd ~/.hermes/hermes-agent
grep -rn "run_app\|TCPSite\|AppRunner" gateway/ src/ | head
```

The "DesktopAppAdapter listening on ws://..." line in the user's log comes
from the `gateway.platforms.desktop_app` module, so look there first.

Patch shape (concrete file/line will depend on what grep finds):

```python
import ssl
from pathlib import Path

CERT_DIR = Path.home() / ".hermes" / "tls"
HOSTNAME = "mac-mini.taild1234.ts.net"  # or read from config / TS_CERT_HOSTNAME env

def build_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(
        certfile=str(CERT_DIR / f"{HOSTNAME}.crt"),
        keyfile=str(CERT_DIR / f"{HOSTNAME}.key"),
    )
    # TLS 1.2 minimum — the desktop's rustls speaks TLS 1.2 + 1.3.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx

# Pass to web.run_app or to TCPSite — whichever pattern Hermes uses:
web.run_app(app, host="0.0.0.0", port=8645, ssl_context=build_ssl_context())
# OR for AppRunner-based startup:
# site = web.TCPSite(runner, host="0.0.0.0", port=8645, ssl_context=build_ssl_context())
```

**Don't hardcode the hostname or cert path** — read from env vars with
sensible defaults. Suggested env vars:

```python
import os
HOSTNAME = os.environ.get("HERMES_TLS_HOSTNAME") or _detect_tailscale_hostname()
CERT_DIR = Path(os.environ.get("HERMES_TLS_DIR", str(Path.home() / ".hermes" / "tls")))
```

A `_detect_tailscale_hostname()` helper that shells out to
`tailscale status --json` and reads `Self.DNSName` is a nice touch but
optional — env var is fine.

### 6. Update the startup log line

The current log is misleading because it always claims `ws://`:

```
INFO gateway.platforms.desktop_app: [desktop_app] DesktopAppAdapter listening on ws://192.168.32.2:8645/ws
```

After TLS:

```python
scheme = "wss" if ssl_context else "ws"
log.info("[desktop_app] DesktopAppAdapter listening on %s://%s:%s/ws", scheme, advertised_host, port)
```

Show the MagicDNS hostname, not the IP, so the user knows what to pair
against on the desktop.

### 7. Health endpoint

Hermes also serves an unauthenticated `/health` endpoint that the desktop
hits over HTTPS now. aiohttp serves the same TLS context for HTTP routes
as for WebSocket routes when you pass `ssl_context` once to `run_app` /
`TCPSite` — so flipping WS to TLS automatically makes `/health` HTTPS too.
Nothing extra to do.

## Verification (do these in order)

### A. wscat from the Mac mini itself

```bash
brew install wscat 2>/dev/null || npm i -g wscat
wscat -c "wss://${hostname}:8645/ws"
# expect: 'Connected (press CTRL+C to quit)' and the connection holds open
```

If wscat reports "self-signed certificate" or "unable to verify", the
cert isn't being served — check the aiohttp wiring. If wscat says
"connection refused", Hermes isn't listening — check the port.

### B. From the desktop machine

```bash
# Windows, in a Tailscale-installed shell:
wscat -c "wss://mac-mini.taild1234.ts.net:8645/ws"
```

If this works but the Tauri client doesn't, the failure is desktop-side
(token/host mismatch in pairing) — escalate back to the user.

### C. curl the health endpoint

```bash
curl -v "https://${hostname}:8645/health"
# expect: HTTP/1.1 200 OK and a JSON body. Cert chain verified.
```

### D. Open the Tauri app

User opens the desktop app, goes to Preferences → Hermes, clears the
existing token, and re-pairs using:

- **Host:** `mac-mini.taild1234.ts.net` (the MagicDNS hostname — NOT the
  IP `192.168.32.2`. The cert SAN is the hostname, and rustls validates
  the dial address against the SAN.)
- **Port:** `8645`
- **Token:** whatever the existing pairing token is.

Tauri logs should now show `connected` instead of `received corrupt
message of type InvalidContentType`.

## Why hostnames not IPs

Tailscale's issued cert has the MagicDNS hostname in the SAN list. It
does NOT include `192.168.32.2`. rustls validates the dial address against
the certificate's SANs; an IP dial against a DNS-only cert fails verification
even when the cert itself is valid and trusted.

If the user really wants to keep dialing by IP, they'd need a self-signed
cert with the IP as a SAN, plus root-CA install on the desktop — a lot
more ceremony. Hostnames are cheaper.

## Common failure modes

| Symptom | Diagnosis |
|---|---|
| `tailscale cert: HTTPS is not enabled` | Toggle on at <https://login.tailscale.com/admin/dns>. |
| `tailscale cert: not authorized` | User isn't admin on the tailnet, or the machine isn't tagged for cert issuance. Check ACLs / device tags. |
| `wscat` from desktop: `Hostname/IP doesn't match certificate's altnames` | Pairing was done with the IP; re-pair with the MagicDNS hostname. |
| `wscat` from desktop: `unable to verify the first certificate` | Cert is self-signed (someone bypassed `tailscale cert` and used `mkcert` or openssl). Re-issue with `tailscale cert`. |
| Tauri logs: `connected` then `4011 Unauthorized` | Cert/TLS is fine. Token mismatch — re-pair on desktop. |
| Tauri logs: still `InvalidContentType` after Hermes restart | Hermes didn't actually pick up `ssl_context` — check the patched call site. Verify with `openssl s_client -connect <host>:8645` (should show cert chain, NOT garbage). |

## Out of scope for this handoff

- mkcert / self-signed CA workflow (only relevant if Tailscale isn't an option).
- macOS launchd plist for Hermes auto-start (orthogonal — affects WHEN the
  agent runs, not WHETHER it serves TLS).
- Loopback-only listener (`127.0.0.1:8645`) for local dev — fine to keep
  cleartext but the user said no plaintext, so don't even add this as an
  option without asking.

## What to tell the user when you're done

A short summary:

1. Issued Tailscale cert for `<hostname>` at `~/.hermes/tls/`.
2. Patched `<file>:<line>` to load TLS context into `web.run_app`.
3. Confirmed `wscat -c wss://<hostname>:8645/ws` succeeds locally.
4. User's next step: re-pair the desktop client using the hostname (not
   the IP) under Preferences → Hermes.
5. Renewal cron: every Sunday at 04:00 (or whatever cadence you wired up).
