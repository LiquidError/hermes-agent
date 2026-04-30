# Hermes Desktop App — Tauri client contract

This document specifies the wire protocol between a remote chat client (the
Tauri desktop app, or any other implementor) and a Hermes host running
`DesktopAppAdapter`. It is the cross-repo contract; pin against it when
building the client.

The server-side implementation lives in:
- `gateway/platforms/desktop_app.py` — adapter, auth, lifecycle
- `gateway/platforms/desktop_app_auth.py` — `TokenStore` (hashed at rest)
- `tui_gateway/server.py` — JSON-RPC dispatcher, all RPC methods + events
- `tui_gateway/ws.py` — WebSocket transport
- `hermes_cli/desktop_app.py` — `hermes desktop pair / list / revoke` CLI

---

## 1. Connection

### 1.1 URL

```
ws://<host>:<port>/ws
wss://<host>:<port>/ws    # when TLS is configured
```

Default `<port>` is `8645`. The host is whatever the Hermes operator binds
to; typical options are `127.0.0.1` (loopback only), a Tailscale IP, or any
LAN address. A `GET /health` endpoint is exposed on the same host:port (see
§9).

### 1.2 TLS

The server may serve WSS when configured with a cert + key pair (typically
issued by `tailscale cert`). Clients should:
- Try the configured scheme first.
- Validate the certificate chain unless connecting to a personal Tailscale
  endpoint where self-signed is expected — in that case prompt the user
  to accept the fingerprint and pin it.

### 1.3 Bearer-token authentication

Every connection (whether loopback or remote, when any client has been
paired) MUST include:

```http
Authorization: Bearer <token>
```

The token is opaque to the client; the server compares its SHA-256 against
a per-host hash registry. Missing, malformed, or unknown tokens are
rejected with HTTP `401 Unauthorized` and a `WWW-Authenticate: Bearer
realm="hermes-desktop"` header. Rejection happens before the WebSocket
upgrade, so no RPC method (not even `client.hello`) executes
unauthenticated.

A loopback bind with an empty token store is the only configuration that
permits unauthenticated connections. This is for local development only;
non-loopback binds without paired clients are refused at server startup.

---

## 2. Wire protocol

JSON-RPC 2.0, framed as newline-delimited JSON in both directions. UTF-8.

### 2.1 Request

```json
{"jsonrpc": "2.0", "id": 1, "method": "session.create", "params": {}}
```

### 2.2 Response

Successful:

```json
{"jsonrpc": "2.0", "id": 1, "result": {"session_id": "ab12cd34"}}
```

Failed:

```json
{"jsonrpc": "2.0", "id": 1, "error": {"code": 4001, "message": "..."}}
```

### 2.3 Event (server → client only, no `id`)

```json
{
  "jsonrpc": "2.0",
  "method": "event",
  "params": {
    "type": "<event-name>",
    "session_id": "ab12cd34",
    "payload": { /* event-specific */ }
  }
}
```

### 2.4 Ordering

- Requests with a given `id` get exactly one response (`result` xor
  `error`).
- Events arrive interleaved with responses; clients MUST not assume any
  particular ordering between responses and events.
- A few methods (`slash.exec`, `cli.exec`, `shell.exec`, `session.resume`,
  `session.branch`, `skills.manage`) are dispatched on a thread pool and
  may complete out of order with respect to other concurrent requests.

---

## 3. Handshake

Immediately after the WebSocket opens the server emits one event:

```json
{
  "jsonrpc": "2.0",
  "method": "event",
  "params": {
    "type": "gateway.ready",
    "payload": {"skin": { /* current skin config */ }}
  }
}
```

The client SHOULD then send `client.hello` to negotiate protocol version
and capabilities:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "client.hello",
  "params": {
    "client_id": "tauri-desktop",
    "client_version": "0.1.0",
    "capabilities": ["voice.in", "voice.out", "attach.image"]
  }
}
```

Response:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "server_version": "hermes-agent",
    "protocol_version": 1,
    "capabilities": [
      "voice", "tts", "approval", "skills", "insights",
      "session.list", "session.resume",
      "slash.exec", "complete.slash", "model.options",
      "image.attach", "attachment.upload", "config.reveal_secret",
      "message.complete", "tool.complete"
    ],
    "client_id": "tauri-desktop",
    "client_version": "0.1.0",
    "client_capabilities": ["voice.in", "voice.out", "attach.image"]
  }
}
```

Clients SHOULD refuse to operate when `protocol_version` does not match
the version they were built against — see §12.

---

## 4. RPC method index

All methods below are dispatched through `tui_gateway`'s registry and are
forwarded verbatim by the desktop adapter. Methods are grouped by topic
for readability; the actual dispatch is flat.

### Sessions

| Method | Purpose |
|---|---|
| `session.create` | Allocate an in-flight session; returns `{session_id, info}`. The agent build runs in the background; emit `session.info` arrives when ready. |
| `session.list` | List persisted sessions (state.db). Cross-platform — telegram/cli/etc. sessions appear too. |
| `session.resume` | Reload a persisted session into a fresh in-flight slot. |
| `session.close` | Drop the in-flight slot. Returns `{closed: bool}`; `false` means the caller's connection didn't own that session_id (see §10 for the isolation model). |
| `session.branch` | Fork a session at the current point. |
| `session.interrupt` | Interrupt the active prompt for a session. |
| `session.compress` | Force a context-compression pass. |
| `session.history` | Return the message log for a session. |
| `session.title` | Get / set the human-readable title. |
| `session.usage` | Token / cost stats. |
| `session.save` | Commit pending in-flight state to state.db. |
| `session.steer` | Inject a steering message mid-stream. |
| `session.undo` | Pop the last user/assistant turn pair. |

### Prompts

| Method | Purpose |
|---|---|
| `prompt.submit` | Send a user message; emits `message.start` → many `message.delta` → `message.complete`, plus tool events. |
| `prompt.background` | Fire-and-forget background task; emits `btw.complete` when done. |
| `prompt.btw` | Side-channel question that doesn't pollute the main session history. |

### Approvals & interactive prompts

These are paired event/method flows. The server emits an event, the client
displays UI, the client sends the corresponding `*.respond`.

| Event (server → client) | Response method | Use |
|---|---|---|
| `approval.request` | `approval.respond` | Tool-call approval (allow-once / always / deny). |
| `clarify.request` | `clarify.respond` | Agent asks a multiple-choice clarifying question. |
| `sudo.request` | `sudo.respond` | Tool needs the sudo password. |
| `secret.request` | `secret.respond` | Tool needs an opaque secret value at runtime. |

### Slash commands & autocomplete

| Method | Purpose |
|---|---|
| `slash.exec` | Run any `/<command>` (e.g. `/personality`, `/skills`, `/model`). Result is the formatted text the TUI would show. |
| `command.dispatch` | Lower-level command dispatch (used internally; prefer `slash.exec`). |
| `command.resolve` | Resolve an alias / partial command name. |
| `commands.catalog` | List all available slash commands with descriptions. |
| `complete.slash` | Autocomplete partial slash-command input. |
| `complete.path` | Autocomplete a filesystem path (for `@file` references etc.). |

### Model & config

| Method | Purpose |
|---|---|
| `model.options` | List available models — structured, suitable for a picker UI. |
| `config.get` | Read a config key. With `key="full"` returns the full config dict; values matching `*_api_key`, `*_token`, `*_secret`, `*_password`, `*_credentials`, `*_bearer` are masked (e.g. `sk-1...cdef`). |
| `config.set` | Write a config key. |
| `config.reveal_secret` | Return the unmasked plaintext for a single dotted key path. **Rate-limited globally**: max 5 calls per 30s window across all connections; over the limit returns `{code: 429}`. |
| `config.show` | Render the config in a human-readable shape. |

### Attachments

| Method | Purpose |
|---|---|
| `image.attach` | Attach an image **already on the server's filesystem** by path. Suitable for the local TUI; not useful for a remote client. |
| `attachment.upload` | Upload bytes from the client. See §7 for the contract. |
| `clipboard.paste` | Read an image from the server's clipboard. (Limited utility for a remote client; mostly used by the local TUI.) |
| `paste.collapse` | Collapse a long paste into a placeholder. |
| `input.detect_drop` | Detect file-drop semantics from a raw input string. |

### Voice

| Method | Purpose |
|---|---|
| `voice.toggle` | Enable / disable voice mode. |
| `voice.record` | Start / stop microphone capture. |
| `voice.tts` | Synthesize text. Emits `voice.transcript` events with audio data. |

### Tools

| Method | Purpose |
|---|---|
| `tools.list` | List available tools. |
| `tools.show` | Detail for a single tool. |
| `tools.configure` | Persist tool-specific settings. |
| `toolsets.list` | List toolsets (CLI / telegram / etc.). |

### Skills

| Method | Purpose |
|---|---|
| `skills.manage` | Install / uninstall / enable / disable skills. (Long-running; dispatched on the pool.) |

### Cron / scheduled tasks

| Method | Purpose |
|---|---|
| `cron.manage` | Add / list / remove cron jobs. |

### Spawn trees & subagents

| Method | Purpose |
|---|---|
| `agents.list` | List active agents. |
| `subagent.interrupt` | Interrupt a delegated subagent. |
| `delegation.status` | Status of a delegate run. |
| `delegation.pause` | Pause a delegate run. |
| `spawn_tree.list` / `.load` / `.save` | Inspect / persist spawn trees. |

### Rollback & versioning

| Method | Purpose |
|---|---|
| `rollback.list` | List rollback points. |
| `rollback.diff` | Diff against a rollback point. |
| `rollback.restore` | Restore to a rollback point. |

### Insights & misc

| Method | Purpose |
|---|---|
| `insights.get` | Fetch usage / cost / activity insights. |
| `setup.status` | First-run setup state. |
| `plugins.list` | List installed plugins. |
| `reload.mcp` | Reload MCP servers. |
| `process.stop` | Stop a tracked background process. |
| `terminal.resize` | Notify server of terminal size change. |
| `cli.exec` / `shell.exec` | Run an arbitrary CLI / shell command (long; pool-dispatched). |
| `browser.manage` | Browser-automation skill management. |

### Adapter-specific (added by `desktop_app`)

| Method | Purpose |
|---|---|
| `client.hello` | See §3. |

---

## 5. Event index

All events follow the envelope in §2.3.

| Event | Emitted when | Notable payload |
|---|---|---|
| `gateway.ready` | Connection accepted. | `{skin}` — current skin config. |
| `session.info` | Session is built and ready (after `session.create` / `.resume`). | Model, tools, skills, cwd. |
| `message.start` | Agent begins assembling a response. | — |
| `message.delta` | Streaming text chunk. | `{text}` |
| `message.complete` | Final response committed. | Includes usage stats. |
| `reasoning.delta` | Streaming reasoning chunk (for reasoning-capable models). | `{text}` |
| `reasoning.available` | A complete reasoning block is available to expand. | `{text}` (preview) |
| `thinking.delta` | Free-form "thinking" text from the model. | `{text}` |
| `tool.start` | Tool call dispatched. | `{name, input}` |
| `tool.generating` | Tool name resolved but args still streaming. | `{name}` |
| `tool.progress` | Tool reports interim progress. | `{name, preview}` |
| `tool.complete` | Tool call finished. | `{name, output, duration_ms, status}` |
| `approval.request` | Tool needs user approval. | `{tool, args, choices: [allow-once, always, deny]}` |
| `clarify.request` | Agent asks a multiple-choice question. | `{question, choices}` |
| `sudo.request` | Tool needs the sudo password. | — |
| `secret.request` | Tool needs an opaque secret. | `{label}` |
| `status.update` | General-purpose status notice. | `{kind, text}` (e.g. `kind=context_pressure`). |
| `voice.status` | Voice subsystem state changed. | `{state}` |
| `voice.transcript` | Voice transcription / TTS chunk. | `{text}` (or audio) |
| `btw.complete` | A `prompt.background` or `prompt.btw` finished. | `{text}` |
| `skin.changed` | The active skin was switched. | New skin config. |
| `error` | Something went wrong inside an in-flight session. | `{message}` |

### 5.1 Notification triggers

For OS-level notifications when the desktop window is unfocused, the
client should subscribe to:

- `approval.request` — definite "user attention needed."
- `message.complete` — the agent finished a long turn while you were
  away.

The server makes no per-event "user attention" annotation; the client
decides locally based on its own focus state and which session originated
the event.

---

## 6. Auth flow

### 6.1 Pairing

On the Hermes host:

```bash
hermes desktop pair --client-name <friendly-name>
```

Prints the bearer token **once** to stdout and persists its SHA-256 hash
to `<HERMES_HOME>/desktop_app_tokens.json`. The plaintext is unrecoverable
after this step.

The Tauri client should:
1. Prompt the user to paste the token on first launch.
2. Store it in the OS credential vault (Windows Credential Manager,
   macOS Keychain, libsecret on Linux) — never in plain config files.
3. Send it on every connection in the `Authorization: Bearer <token>`
   header.

### 6.2 Listing & revocation

```bash
hermes desktop list                  # show paired client names
hermes desktop revoke <name>         # remove a paired client
```

Revocation takes effect on the next connection attempt. Existing
WebSocket connections are not forcibly closed when their token is
revoked (see §13.4 for follow-up).

### 6.3 Multiple paired clients

Multiple devices can pair against the same Hermes host (e.g. laptop +
tablet). Each gets a distinct token. Sessions persisted to `state.db`
are visible to every paired client via `session.list` — that's the
cross-platform continuity feature. In-flight runtime state is per
connection (see §10).

### 6.4 Secret reveal

`config.reveal_secret` returns the unmasked plaintext for a single key
path. It is rate-limited globally to 5 calls per 30s window; over the
limit returns:

```json
{"jsonrpc":"2.0","id":...,"error":{"code":429,"message":"too many reveal requests; try again shortly"}}
```

---

## 7. Attachments

Two methods, two purposes:

### 7.1 `image.attach` — local file by path

For images **already present on the server's filesystem**. The client
sends:

```json
{"jsonrpc":"2.0","id":1,"method":"image.attach",
 "params":{"session_id":"...", "path":"/abs/path/to/image.png"}}
```

Useful for a TUI running on the same host. Less useful for a remote
desktop client.

### 7.2 `attachment.upload` — bytes from the client

For any file the client wants the agent to read:

```json
{"jsonrpc":"2.0","id":1,"method":"attachment.upload",
 "params":{
   "session_id":"...",
   "filename":"report.pdf",
   "data":"<base64-encoded bytes>"
 }}
```

Response:

```json
{"jsonrpc":"2.0","id":1,
 "result":{
   "path":"/Users/.../desktop_app_attachments/a1b2c3d4_report.pdf",
   "filename":"report.pdf",
   "size":12345
 }}
```

The returned `path` is the server-side location the agent will read.
The client typically follows up with a `prompt.submit` that references
the file (e.g. "summarize the file at <path>").

**Constraints:**
- `filename` must be a single basename — no `/`, `\`, `..`, NUL, or `.`.
- `data` must be valid base64.
- Size limit defaults to 25 MiB. Over-limit returns error code `4004`.

The cache directory is `<HERMES_HOME>/desktop_app_attachments/` and is
not garbage-collected by the server. Hermes operators are expected to
clean it periodically; clients should not rely on attachments persisting
beyond a few hours.

---

## 8. Connection lifecycle

### 8.1 Disconnect cleanup

When the WebSocket closes:
- All sessions created on this connection are unregistered from the
  cross-thread routing map.
- Slash-worker subprocesses owned by this connection are terminated.
- In-flight agent threads continue until they naturally finish; any
  events they emit after disconnect are silently dropped.

### 8.2 Reconnect

A reconnecting client should:
1. Open a new WebSocket, send `client.hello`.
2. Either start fresh (`session.create`) or pick up a persisted
   conversation (`session.list` → `session.resume`).

`session.resume` does **not** re-attach to a still-running in-flight
agent on the server. It reads the persisted history from `state.db` and
spins up a fresh agent. Anything that was streaming when the client
disconnected is lost — but the persisted history is intact, so the
visible conversation continues seamlessly.

If the client wants stronger continuity (e.g. "subscribe to events for
session_X regardless of which connection started it"), that's a future
extension.

### 8.3 Heartbeats

The server sends WebSocket-protocol pings every 30 seconds. The client
should respond per the WS spec; aiohttp / native WS libraries do this
automatically.

---

## 9. Health endpoint

`GET /health` on the same host:port returns connection-status info
without requiring a WebSocket upgrade or authentication:

```json
{
  "platform": "desktop_app",
  "state": "connected",
  "protocol_version": 1,
  "host": "127.0.0.1",
  "port": 8645,
  "paired_clients": [
    {"name": "laptop", "last_seen_at": 1714123456.789},
    {"name": "tablet", "last_seen_at": null}
  ]
}
```

Useful for the Tauri side to render "connected to Hermes" status,
detect server outages, and surface paired-client info in a settings
panel. `last_seen_at` is a Unix timestamp; `null` means the client has
never connected since pairing.

The endpoint reads the token file fresh on every request, so external
`hermes desktop pair / revoke` operations are reflected without a
gateway restart.

---

## 10. Per-connection isolation

Two paired clients connecting concurrently each get their own dispatcher
state. Practical implications:

- `session.create` made by client A registers the session in A's state
  only. Client B cannot see or affect A's in-flight session.
- `session.close(sid)` and `session.interrupt(sid)` against another
  connection's session_id return `{closed: false}` / silently no-op
  rather than disrupting the other client.
- `session.list` and `session.resume` work against the shared
  `state.db`, so persisted conversations are visible to every paired
  client. This is intentional — it's how cross-platform continuity
  works.

The client doesn't need to do anything to opt into isolation; it's
automatic.

---

## 11. Error codes

| Code | Meaning |
|---|---|
| `-32700` | Parse error (invalid JSON). |
| `-32601` | Unknown method. |
| `-32000` | Generic handler error. |
| `4001` | Required parameter missing. |
| `4002` | Parameter validation failed (e.g. bad filename). |
| `4003` | Invalid encoding (e.g. bad base64). |
| `4004` | Resource exceeded (size limit, missing key for `reveal_secret`). |
| `429` | Rate limit exceeded. |
| `5006` | `state.db` unavailable. |
| `5013` | Provider resolution failed. |
| `5027` | Underlying handler raised an unexpected exception. |
| `5032` | Agent initialization timed out. |

Codes outside this list are uncommon but possible for handler-specific
errors; clients should treat any `error` response as fatal for the
specific request without disconnecting.

---

## 12. Versioning

`protocol_version` is currently `1`. The server will bump it for any
breaking change to the wire shape (renamed methods, removed fields,
changed event payload structure). Additive changes (new methods, new
optional fields, new event types) do **not** bump the version.

Clients should:
- Pin against an exact `protocol_version` they were built against.
- Refuse to start if `client.hello` returns a different value.
- Treat unknown method errors (`-32601`) as "this server is older than
  expected"; treat unknown event types as "this server is newer than
  expected" and ignore them gracefully.

When upstream `hermes-agent` evolves and the desktop adapter inherits
new methods automatically, those land as additive — `protocol_version`
stays the same. The Tauri client can opt into them by adding new
capabilities to its `client.hello` and dispatching the new methods.

---

## 13. Open considerations

These are documented gaps, not bugs. They're places the contract may
extend in future versions.

### 13.1 Slash-command output is text

`slash.exec` returns formatted strings the TUI would render. Some
commands (`/personality`, `/sessions`, `/skills`, `/cron`) would be
nicer as structured JSON for native picker UIs. `model.options` already
is. The right time to widen the structured-data return surface is when
the Tauri client knows which pickers it actually renders.

### 13.2 Admin commands

`/restart`, `/update`, and similar gateway-administration commands are
currently forwarded verbatim by `slash.exec`. The Tauri client may want
to hide them from its UI or gate them behind a separate admin token.
The server makes no distinction today.

### 13.3 Live event re-attach

`session.resume` builds a fresh agent rather than re-attaching to a
still-running one. If a client wants stronger continuity it would need
a new `session.observe` method that subscribes to events for an
existing in-flight session_id without owning it.

### 13.4 Forced disconnect on revoke

Calling `hermes desktop revoke <name>` removes the token from the
store but does not forcibly close existing WebSocket connections that
were opened with that token. Affected connections continue working
until the client disconnects naturally; the next reconnect attempt
fails with `401`. A client UI can periodically re-authenticate against
`/health` to detect a server-side revocation.

### 13.5 Generic file-attachment metadata

`attachment.upload` returns `{path, filename, size}`. It does not
return MIME type, sniffed file kind, or rendered preview metadata. The
agent reads the file directly when the user references it in a
`prompt.submit`. If the Tauri side wants client-rendered previews
without round-tripping through the agent, the client renders them
locally before uploading.

### 13.6 Notification metadata

The server emits `approval.request`, `message.complete`, `tool.complete`
without per-event "deserves an OS notification" annotation. The client
decides based on window focus, session id, and its own preferences.

---

## 14. Reference: minimal happy-path flow

1. **Pair**: operator runs `hermes desktop pair --client-name desktop`.
2. **Connect**: client opens WSS, sends `Authorization: Bearer <token>`.
3. **Handshake**: receive `gateway.ready`, send `client.hello`, receive
   capabilities + protocol_version.
4. **Create session**: send `session.create`, receive `{session_id}`,
   wait for `session.info` event.
5. **Submit prompt**: send `prompt.submit` with text. Receive
   `message.start` → many `message.delta` (and possibly `tool.start`,
   `tool.complete`, `approval.request` etc.) → `message.complete`.
6. **Render**: stream deltas into the UI; render tool spans inline;
   show approval cards as modals; play TTS audio if `voice.tts` was
   triggered.
7. **Close**: send `session.close` when the user is done, or just
   close the WebSocket — disconnect cleanup runs server-side.

---

## 15. Widget render

Wire-level surface for the widget runtime — six events server→client, one method client→server, four event-shape messages client→server. Implemented on the Tauri side per `widget-runtime/`, on the Hermes side per `hermes-widget-runtime/`. Full per-side specs:

- [hermes-widget-render-spec.md](./hermes-widget-render-spec.md) — Hermes-side spec (event shapes, tools, registries, server-side cap enforcement).
- [tauri-agent-widget-runtime-spec.md](./tauri-agent-widget-runtime-spec.md) — Tauri-side spec (iframe sandbox, esbuild-wasm pipeline, capability broker, iframe pool).

This section consolidates the wire surface so a future reader doesn't need to triangulate between the two implementation specs.

### 15.1 Capability negotiation

The server advertises `widget.render` in its `client.hello` capabilities array. A client opts in by including `widget.render` in its own `capabilities`. Both sides must advertise it for the runtime to be active. When either side is missing the capability, the six widget tools are not registered for that session and the agent has no widget surface.

### 15.2 Server → client events

```json
{ "jsonrpc": "2.0", "method": "event", "params": {
  "type": "widget.render",
  "session_id": "ab12cd34",
  "payload": {
    "card_id": "wgt_8a3f9c",
    "source": "export default function Card() { ... }",
    "capabilities": ["hermes.ask", "notes.save"],
    "title": "Quarterly notes draft",
    "initial_size": { "w": 480, "h": 320 },
    "trace_id": "tool_call_42"
  }
}}
```

`widget.update` carries `{card_id, source, capabilities?}` — replace source on a live card; React state resets, position preserved.

`widget.message` carries `{card_id, message}` — push a structured payload (≤256 KiB) into a live card without remount.

`widget.dispose` carries `{card_id, reason}` — close a live card. Idempotent both sides.

`widget.api_response` carries `{correlation_id, card_id, result}` on success or `{correlation_id, card_id, error: {code, message}}` on error. Resolves the iframe's pending `canvasAPI.hermes.ask` Promise.

`widget.api_cancel` (server-emitted) carries `{correlation_id, card_id, reason}` — abort an in-flight `widget.api_call`. Reasons include `card_disposed`, `session_ended`.

### 15.3 Client → server method

```json
{ "jsonrpc": "2.0", "id": 17, "method": "widget.api_call", "params": {
  "card_id": "wgt_8a3f9c",
  "session_id": "ab12cd34",
  "correlation_id": "corr_a1b2c3",
  "capability": "hermes.ask",
  "args": { "prompt": "What was Q3 revenue?" }
}}
```

Server responds synchronously with `{accepted: true, correlation_id}` after validation; the actual answer arrives later as a `widget.api_response` event keyed by `correlation_id`. Validation errors return a JSON-RPC error with one of the codes in §15.6.

### 15.4 Client → server events

These use the same envelope shape as server→client events but originate from the client and have no `id` field.

`widget.mounted` — `{card_id, compiled_size, compile_ms}`.

`widget.error` — `{card_id, phase, kind, message, stack}`. `phase` ∈ `validate`, `compile`, `mount`, `runtime`, `capability`.

`widget.disposed` — `{card_id, reason}`. Reasons from the client side: `user_closed`, `agent_disposed`, `superseded`, `error`, `session_ended`.

`widget.api_cancel` (client-emitted) — `{correlation_id, card_id, reason}`. Reasons: `card_disposed`, `card_updated`, `user_cancelled`.

### 15.5 Caps and limits

- `widget.render.payload.source`: max 256 KiB.
- `widget.message.payload`: max 256 KiB.
- `widget.api_response.result`: hard cap 32 KiB. Server-side enforcement before emit; over-cap responses are converted to error 4106.
- Card ID format: `wgt_<6 lowercase hex>`. Server allocates; client validates against `/^wgt_[0-9a-f]{6}$/`.

### 15.6 Error codes

| Code | Meaning |
|---|---|
| 4101 | Unknown capability declared in `widget.render` or called via `widget.api_call`. |
| 4102 | Source exceeds 256 KiB cap. |
| 4103 | Card id unknown for `widget.update` / `widget.message` / `widget.dispose` / `widget.api_call`. |
| 4104 | Capability not declared in card's manifest. |
| 4105 | Card capability call rejected by user (reserved for a future approval gate). |
| 4106 | `widget.api_response` payload exceeds 32 KiB cap. |
| 4107 | `widget.message` payload exceeds 256 KiB cap. |
| 5101 | Tauri client refused to mount (compile or validate failure). Carries inner error in payload. |
| 5102 | `render_widget` tool timed out waiting for `widget.mounted`. |
| 5103 | `widget.api_call` correlation expired or worker crashed. |

### 15.7 Idempotency and races

- Both server-initiated `widget.dispose` and client-initiated `widget.disposed` can fire for the same card simultaneously. Each side treats its own action as canonical, drops the incoming, and converges on the same end state.
- When a card is disposed mid-`widget.api_call`, the server cancels the correlation, attempts to interrupt the underlying work, and emits `widget.api_cancel`. Late-arriving results are dropped without emitting `widget.api_response`.
- Session disconnect cancels every in-flight `widget.api_call` correlation with `reason: "session_ended"`.

### 15.8 Persistence

Widgets are session-scoped. They do not survive `session.resume`. `session.branch` does not duplicate live widgets; the new branch starts with no cards.
