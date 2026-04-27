# Agent Canvas Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Tauri-side `AgentCanvas` that consumes the Hermes `DesktopAppAdapter` over WSS — a single-session agent chat UI with side-card observability, slash commands, approvals, save-as-note, OS notifications, and an eager-on-app-start connection.

**Architecture:** Rust owns the WSS connection, JSON-RPC tracking, reconnect, and keyring. A TypeScript service wraps `invoke('hermes_call', …)` and `listen('hermes:event', …)` so React components and hooks see a normal service with mock fallback. The canvas composes existing primitives (`FloatingPanel`, `useSliceGesture`, `DotGridCanvas`, `NoiseOverlay`, `VisorFrame`) without refactoring `NodeEditorCanvasDark`.

**Tech Stack:** Rust + `tokio-tungstenite` + `keyring` (already present); TypeScript + React 19 + TanStack Query + Zustand + Vitest + Playwright.

**Reference docs (read before starting):**
- Design spec: [agent-canvas-design.md](./agent-canvas-design.md)
- Wire protocol: [tauri-client-contract.md](./tauri-client-contract.md)
- Server side: [desktop-app-adaptor.md](./desktop-app-adaptor.md)
- Existing keyring pattern to mirror: [src-tauri/src/commands/session_credentials.rs](../../src-tauri/src/commands/session_credentials.rs)
- Reused primitives: [FloatingPanel.tsx](../../src/components/node-editor/components/FloatingPanel.tsx), [useSliceGesture.ts](../../src/components/node-editor/hooks/useSliceGesture.ts)

**Conventions used throughout:**
- Tests run via `bun run test` (NOT `bun test`). Rust via `cd src-tauri && cargo test`.
- TS path alias: `@/` = `src/`.
- Coverage threshold 80% for new TS modules.
- Each task ends with a commit. Commit subject is what changed; body explains why if non-obvious.
- Co-author trailer: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>` on every commit.

---

## File Structure

### New TypeScript files

```
src/
├── components/
│   ├── agent-canvas/
│   │   ├── AgentCanvas.tsx                       — top-level shell, composes primitives
│   │   ├── AgentCanvas.test.tsx                  — shell render + zone integration
│   │   ├── BottomCommandBar.tsx                  — single input, slash, mic, model
│   │   ├── BottomCommandBar.test.tsx
│   │   ├── ThreadsPanel.tsx                      — toggleable session list
│   │   ├── ThreadsPanel.test.tsx
│   │   ├── ActiveChatCard.tsx                    — transcript-only main card
│   │   ├── ActiveChatCard.test.tsx
│   │   ├── ApprovalModal.tsx                     — focused-window approval
│   │   ├── ApprovalModal.test.tsx
│   │   ├── side-cards/
│   │   │   ├── ToolProgressCard.tsx
│   │   │   ├── ToolProgressCard.test.tsx
│   │   │   ├── ArtifactCard.tsx                  — save-as-note, pin, dismiss
│   │   │   ├── ArtifactCard.test.tsx
│   │   │   ├── SubagentThreadCard.tsx
│   │   │   └── SubagentThreadCard.test.tsx
│   │   ├── transcript/
│   │   │   ├── TranscriptStream.tsx              — message + reasoning + inline tools
│   │   │   ├── TranscriptStream.test.tsx
│   │   │   ├── InlineToolBlock.tsx
│   │   │   ├── InlineToolBlock.test.tsx
│   │   │   ├── InlineApprovalCard.tsx
│   │   │   └── InlineApprovalCard.test.tsx
│   │   ├── slash/
│   │   │   ├── SlashCompletionMenu.tsx
│   │   │   ├── SlashCompletionMenu.test.tsx
│   │   │   ├── ModelPickerDropdown.tsx
│   │   │   └── ModelPickerDropdown.test.tsx
│   │   ├── connection/
│   │   │   ├── HermesConnectModal.tsx            — first-run pairing UI
│   │   │   ├── HermesConnectModal.test.tsx
│   │   │   ├── HermesStatusIndicator.tsx
│   │   │   └── HermesStatusIndicator.test.tsx
│   │   └── types.ts                              — AgentCanvas-local types
│   └── preferences/panes/
│       ├── HermesPane.tsx                        — connection status, re-pair, revoke
│       └── HermesPane.test.tsx
├── services/
│   ├── hermesService.ts                          — TS facade
│   ├── hermesService.test.ts
│   ├── mockHermes.ts                             — scripted dev/test server
│   └── mockHermes.test.ts
├── hooks/
│   ├── useHermesConnection.ts
│   ├── useHermesConnection.test.ts
│   ├── useChatSession.ts
│   ├── useChatSession.test.ts
│   ├── useStreamingTurn.ts
│   ├── useStreamingTurn.test.ts
│   ├── useSlashCompletion.ts
│   ├── useSlashCompletion.test.ts
│   ├── useHermesNotifications.ts
│   └── useHermesNotifications.test.ts
├── stores/
│   ├── agentCanvasStore.ts                       — Zustand+Immer
│   └── agentCanvasStore.test.ts
└── utils/
    ├── sideCardHeuristics.ts
    └── sideCardHeuristics.test.ts

tests/
├── e2e/
│   └── agent-canvas.spec.ts                      — Playwright happy path
└── fixtures/
    └── hermes-events.ts                          — typed event factories
```

### New Rust files

```
src-tauri/src/
├── hermes/
│   ├── mod.rs                                    — module root + state struct
│   ├── client.rs                                 — tokio-tungstenite WS connection
│   ├── rpc.rs                                    — JSON-RPC id tracker
│   ├── reconnect.rs                              — state machine + backoff
│   ├── notification.rs                           — OS notification dispatcher
│   ├── token.rs                                  — keyring wrapper
│   └── test_server.rs                            — #[cfg(test)] fake WS server
└── commands/
    └── hermes.rs                                 — Tauri command surface
```

### Modified files

| File | Change |
|---|---|
| `src-tauri/Cargo.toml` | add `tokio-tungstenite` |
| `src-tauri/src/lib.rs` | register hermes commands; spawn client on setup |
| `src-tauri/tauri.conf.json` | ensure `notification` permission listed |
| `src/components/preferences/PreferencesDialog.tsx` | register `HermesPane` |
| `src/components/preferences/FullScreenPreferencesDialog.tsx` | register `HermesPane` |
| `src/components/node-editor/components/CanvasBoard.tsx` | wire `AgentCanvas` zone (chord `AD`) |
| `src/components/node-editor/components/WorkspaceCanvas.tsx` *(see note)* | wire same |

> Note: the spec references `WorkspaceCanvas.tsx`. If that file's exact path differs, locate via `bun run typecheck` errors after wiring `CanvasBoard.tsx` first — both wrappers must surface the new zone.

---

## Phase A — Rust client + auth + status

**Goal at end of Phase A:** From a Rust unit test, `hermes_call("client.hello", …)` succeeds against the fake WS server; intentional drop triggers `Reconnecting` → `Connected`; status events emit on transitions.

### Task A1: Add `tokio-tungstenite` dependency

**Files:**
- Modify: `src-tauri/Cargo.toml`

- [ ] **Step 1: Add dependency line**

Edit `src-tauri/Cargo.toml`, in the `[dependencies]` block add:

```toml
tokio-tungstenite = { version = "0.21", features = ["rustls-tls-webpki-roots"] }
```

- [ ] **Step 2: Verify it builds**

```bash
cd src-tauri && cargo check
```

Expected: builds clean. If `rustls-tls-webpki-roots` feature is unavailable, fall back to `rustls-tls-native-roots`.

- [ ] **Step 3: Commit**

```bash
git add src-tauri/Cargo.toml src-tauri/Cargo.lock
git commit -m "$(cat <<'EOF'
build: add tokio-tungstenite for Hermes WS client

Foundation for the new hermes module that owns the WSS connection.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task A2: Hermes module skeleton + token storage

Mirror [session_credentials.rs](../../src-tauri/src/commands/session_credentials.rs) for the Hermes bearer token, host, port. Token never leaves Rust.

**Files:**
- Create: `src-tauri/src/hermes/mod.rs`
- Create: `src-tauri/src/hermes/token.rs`
- Modify: `src-tauri/src/lib.rs`

- [ ] **Step 1: Write the failing test for token storage**

Create `src-tauri/src/hermes/token.rs`:

```rust
//! Hermes bearer-token + host + port storage via OS keychain.
//! Mirrors commands/session_credentials.rs.
//! Token NEVER leaves Rust — TS only sees masked metadata.

const KEYRING_SERVICE: &str = "anandia-workspace-hermes";
const KEY_TOKEN: &str = "hermes-desktop-token";
const KEY_HOST: &str = "hermes-desktop-host";
const KEY_PORT: &str = "hermes-desktop-port";

#[derive(Debug, Clone, serde::Serialize)]
pub struct TokenMetadata {
    pub paired: bool,
    pub host: Option<String>,
    pub port: Option<u16>,
    pub token_suffix: Option<String>,
}

fn entry(user: &str) -> Result<keyring::Entry, String> {
    keyring::Entry::new(KEYRING_SERVICE, user)
        .map_err(|e| format!("keyring entry: {}", e))
}

pub fn set(token: &str, host: &str, port: u16) -> Result<(), String> {
    entry(KEY_TOKEN)?.set_password(token).map_err(|e| e.to_string())?;
    entry(KEY_HOST)?.set_password(host).map_err(|e| e.to_string())?;
    entry(KEY_PORT)?.set_password(&port.to_string()).map_err(|e| e.to_string())?;
    Ok(())
}

pub fn get_token() -> Result<Option<String>, String> {
    match entry(KEY_TOKEN)?.get_password() {
        Ok(t) => Ok(Some(t)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(e.to_string()),
    }
}

pub fn get_metadata() -> Result<TokenMetadata, String> {
    let token = get_token()?;
    let host = match entry(KEY_HOST)?.get_password() {
        Ok(h) => Some(h),
        Err(keyring::Error::NoEntry) => None,
        Err(e) => return Err(e.to_string()),
    };
    let port = match entry(KEY_PORT)?.get_password() {
        Ok(p) => p.parse::<u16>().ok(),
        Err(keyring::Error::NoEntry) => None,
        Err(e) => return Err(e.to_string()),
    };
    Ok(TokenMetadata {
        paired: token.is_some(),
        host,
        port,
        token_suffix: token.as_ref().map(|t| {
            let n = t.len();
            if n >= 4 { t[n-4..].to_string() } else { "*".repeat(n) }
        }),
    })
}

pub fn clear() -> Result<(), String> {
    for key in [KEY_TOKEN, KEY_HOST, KEY_PORT] {
        match entry(key)?.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => {}
            Err(e) => return Err(e.to_string()),
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    // NOTE: keyring tests require a real OS credential store; skip in CI Docker.
    // Run locally: `cargo test --package anandia_workspace_lib hermes::token -- --ignored`

    #[test]
    #[ignore]
    fn token_metadata_masks_to_last_four() {
        let _ = clear();
        set("supersecret-abcdef1234", "100.1.2.3", 8645).unwrap();
        let md = get_metadata().unwrap();
        assert_eq!(md.paired, true);
        assert_eq!(md.host.as_deref(), Some("100.1.2.3"));
        assert_eq!(md.port, Some(8645));
        assert_eq!(md.token_suffix.as_deref(), Some("1234"));
        clear().unwrap();
    }
}
```

- [ ] **Step 2: Create `mod.rs`**

Create `src-tauri/src/hermes/mod.rs`:

```rust
pub mod token;
```

- [ ] **Step 3: Register module in `lib.rs`**

Edit `src-tauri/src/lib.rs`. Find the `mod` declarations near the top and add:

```rust
mod hermes;
```

- [ ] **Step 4: Build and run the ignored test locally**

```bash
cd src-tauri
cargo build
cargo test hermes::token -- --ignored
```

Expected: PASS on Windows / macOS. CI without a credential store will skip via `--ignored`.

- [ ] **Step 5: Commit**

```bash
git add src-tauri/src/hermes/ src-tauri/src/lib.rs
git commit -m "$(cat <<'EOF'
feat(hermes): add keyring-backed token storage

Mirrors commands/session_credentials.rs. Token, host, and port live in the
OS credential vault under service "anandia-workspace-hermes". Token never
leaves Rust; TS gets only a 4-char suffix via TokenMetadata.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task A3: JSON-RPC id tracker

A small, testable map of pending request ids → oneshot senders. Pure logic, no IO.

**Files:**
- Create: `src-tauri/src/hermes/rpc.rs`
- Modify: `src-tauri/src/hermes/mod.rs`

- [ ] **Step 1: Write the failing tests**

Create `src-tauri/src/hermes/rpc.rs`:

```rust
//! JSON-RPC 2.0 request id tracker.
//! Caller registers (id → oneshot::Sender); resolver delivers to it.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use tokio::sync::oneshot;

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(untagged)]
pub enum RpcOutcome {
    Result(serde_json::Value),
    Error { code: i64, message: String, data: Option<serde_json::Value> },
}

pub struct RpcTracker {
    next_id: AtomicU64,
    pending: parking_lot::Mutex<HashMap<u64, oneshot::Sender<RpcOutcome>>>,
}

impl RpcTracker {
    pub fn new() -> Self {
        Self {
            next_id: AtomicU64::new(1),
            pending: parking_lot::Mutex::new(HashMap::new()),
        }
    }

    pub fn register(&self) -> (u64, oneshot::Receiver<RpcOutcome>) {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let (tx, rx) = oneshot::channel();
        self.pending.lock().insert(id, tx);
        (id, rx)
    }

    /// Resolve a pending request. Silent no-op if id is unknown
    /// (server replied to a request we already gave up on).
    pub fn resolve(&self, id: u64, outcome: RpcOutcome) {
        if let Some(tx) = self.pending.lock().remove(&id) {
            let _ = tx.send(outcome);
        }
    }

    /// Drop all pending — used on disconnect so callers see "connection_lost".
    pub fn drain(&self, reason: &str) {
        let mut map = self.pending.lock();
        for (_, tx) in map.drain() {
            let _ = tx.send(RpcOutcome::Error {
                code: -32603,
                message: reason.to_string(),
                data: None,
            });
        }
    }

    pub fn pending_count(&self) -> usize {
        self.pending.lock().len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn register_and_resolve_returns_result() {
        let t = RpcTracker::new();
        let (id, rx) = t.register();
        t.resolve(id, RpcOutcome::Result(serde_json::json!({"ok": true})));
        let out = rx.await.unwrap();
        match out {
            RpcOutcome::Result(v) => assert_eq!(v["ok"], true),
            _ => panic!("expected Result"),
        }
    }

    #[tokio::test]
    async fn drain_returns_connection_lost_to_pending_callers() {
        let t = RpcTracker::new();
        let (_id, rx) = t.register();
        t.drain("connection_lost");
        match rx.await.unwrap() {
            RpcOutcome::Error { message, .. } => assert_eq!(message, "connection_lost"),
            _ => panic!("expected Error"),
        }
        assert_eq!(t.pending_count(), 0);
    }

    #[tokio::test]
    async fn resolve_unknown_id_is_silent_noop() {
        let t = RpcTracker::new();
        t.resolve(9999, RpcOutcome::Result(serde_json::Value::Null));
        // no panic, no effect
        assert_eq!(t.pending_count(), 0);
    }
}
```

- [ ] **Step 2: Add to `mod.rs`**

Edit `src-tauri/src/hermes/mod.rs`:

```rust
pub mod rpc;
pub mod token;
```

- [ ] **Step 3: Run the tests**

```bash
cd src-tauri && cargo test hermes::rpc
```

Expected: 3 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add src-tauri/src/hermes/rpc.rs src-tauri/src/hermes/mod.rs
git commit -m "$(cat <<'EOF'
feat(hermes): JSON-RPC id tracker with drain-on-disconnect

Pure logic: id allocation, oneshot map, drain returns connection_lost
to pending callers. No IO, fully unit-tested.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task A4: Connection state + status broadcast

Define the state enum and a status broadcaster. Tested before any IO is added.

**Files:**
- Create: `src-tauri/src/hermes/reconnect.rs`
- Modify: `src-tauri/src/hermes/mod.rs`

- [ ] **Step 1: Write the failing tests**

Create `src-tauri/src/hermes/reconnect.rs`:

```rust
//! Connection state machine + exponential backoff schedule.

use std::time::Duration;

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ConnState {
    Disconnected,
    Connecting,
    Connected,
    Reconnecting,
    Error,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct ConnStatus {
    pub state: ConnState,
    pub last_error: Option<String>,
    pub paired_host: Option<String>,
    pub protocol_version: Option<u32>,
}

impl ConnStatus {
    pub fn disconnected() -> Self {
        Self { state: ConnState::Disconnected, last_error: None, paired_host: None, protocol_version: None }
    }
}

/// Exponential backoff: 1s, 2s, 4s, 8s, 16s, capped at 30s. Resets on success.
pub struct Backoff {
    attempt: u32,
}

impl Backoff {
    pub fn new() -> Self { Self { attempt: 0 } }
    pub fn next_delay(&mut self) -> Duration {
        let secs = 1u64 << self.attempt.min(5); // 1,2,4,8,16,32 → cap to 30
        self.attempt = self.attempt.saturating_add(1);
        Duration::from_secs(secs.min(30))
    }
    pub fn reset(&mut self) { self.attempt = 0; }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_grows_then_caps_at_30s() {
        let mut b = Backoff::new();
        assert_eq!(b.next_delay(), Duration::from_secs(1));
        assert_eq!(b.next_delay(), Duration::from_secs(2));
        assert_eq!(b.next_delay(), Duration::from_secs(4));
        assert_eq!(b.next_delay(), Duration::from_secs(8));
        assert_eq!(b.next_delay(), Duration::from_secs(16));
        assert_eq!(b.next_delay(), Duration::from_secs(30));
        assert_eq!(b.next_delay(), Duration::from_secs(30));
    }

    #[test]
    fn backoff_reset_returns_to_one_second() {
        let mut b = Backoff::new();
        for _ in 0..5 { b.next_delay(); }
        b.reset();
        assert_eq!(b.next_delay(), Duration::from_secs(1));
    }

    #[test]
    fn conn_status_disconnected_default() {
        let s = ConnStatus::disconnected();
        assert_eq!(s.state, ConnState::Disconnected);
        assert!(s.last_error.is_none());
    }
}
```

- [ ] **Step 2: Add to `mod.rs`**

```rust
pub mod reconnect;
pub mod rpc;
pub mod token;
```

- [ ] **Step 3: Run tests**

```bash
cd src-tauri && cargo test hermes::reconnect
```

Expected: 3 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add src-tauri/src/hermes/reconnect.rs src-tauri/src/hermes/mod.rs
git commit -m "$(cat <<'EOF'
feat(hermes): conn state + exponential backoff

ConnState enum (Disconnected/Connecting/Connected/Reconnecting/Error),
ConnStatus payload, and a 1/2/4/8/16/30s backoff schedule that resets
on success.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task A5: Fake WS test server fixture

Before writing the real client, build the test fixture it'll be tested against. Listens on `127.0.0.1:0`, scripts a few RPC responses.

**Files:**
- Create: `src-tauri/src/hermes/test_server.rs`
- Modify: `src-tauri/src/hermes/mod.rs`

- [ ] **Step 1: Write the test server**

Create `src-tauri/src/hermes/test_server.rs`:

```rust
//! Fake WS server for testing the hermes client.
//! Scripted: replies to client.hello and a small set of RPC methods,
//! emits scripted events on demand.

#![cfg(test)]

use std::net::SocketAddr;
use std::sync::Arc;
use tokio::net::TcpListener;
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::Message;
use futures_util::{SinkExt, StreamExt};

pub struct FakeServer {
    pub addr: SocketAddr,
    pub control: mpsc::UnboundedSender<ServerCmd>,
    _task: tokio::task::JoinHandle<()>,
}

#[derive(Debug)]
pub enum ServerCmd {
    /// Send a raw JSON frame to the next connected client.
    Send(serde_json::Value),
    /// Drop the active connection (simulate network loss).
    Drop,
}

impl FakeServer {
    pub async fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let (tx, mut rx) = mpsc::unbounded_channel::<ServerCmd>();

        let task = tokio::spawn(async move {
            while let Ok((stream, _)) = listener.accept().await {
                let mut ws = tokio_tungstenite::accept_async(stream).await.unwrap();

                // Send gateway.ready immediately per contract §3.
                let ready = serde_json::json!({
                    "jsonrpc":"2.0","method":"event",
                    "params":{"type":"gateway.ready","payload":{"skin":{}}}
                });
                ws.send(Message::Text(ready.to_string())).await.unwrap();

                loop {
                    tokio::select! {
                        cmd = rx.recv() => match cmd {
                            Some(ServerCmd::Send(v)) => {
                                ws.send(Message::Text(v.to_string())).await.ok();
                            }
                            Some(ServerCmd::Drop) => { let _ = ws.close(None).await; break; }
                            None => break,
                        },
                        msg = ws.next() => match msg {
                            Some(Ok(Message::Text(t))) => {
                                let req: serde_json::Value = serde_json::from_str(&t).unwrap();
                                let id = req.get("id").cloned();
                                let method = req.get("method").and_then(|m| m.as_str()).unwrap_or("");
                                let result = match method {
                                    "client.hello" => serde_json::json!({
                                        "server_version":"hermes-fake",
                                        "protocol_version":1,
                                        "capabilities":["session.list","prompt.submit"],
                                        "client_id":"test","client_version":"0.0.0",
                                        "client_capabilities":[]
                                    }),
                                    "session.create" => serde_json::json!({
                                        "session_id":"sess-fake-1","info":{}
                                    }),
                                    _ => serde_json::json!({"echoed_method":method})
                                };
                                if let Some(id) = id {
                                    let resp = serde_json::json!({
                                        "jsonrpc":"2.0","id":id,"result":result
                                    });
                                    ws.send(Message::Text(resp.to_string())).await.ok();
                                }
                            }
                            Some(Ok(Message::Close(_))) | None => break,
                            _ => continue,
                        }
                    }
                }
            }
        });

        Self { addr, control: tx, _task: task }
    }

    pub fn url(&self) -> String { format!("ws://{}/ws", self.addr) }
}
```

- [ ] **Step 2: Add to `mod.rs` (cfg-gated)**

Edit `src-tauri/src/hermes/mod.rs`:

```rust
pub mod reconnect;
pub mod rpc;
pub mod token;

#[cfg(test)]
pub mod test_server;
```

- [ ] **Step 3: Verify it builds in test mode**

```bash
cd src-tauri && cargo check --tests
```

Expected: builds clean. The fixture has no test of its own — it's exercised by Task A6 onwards.

- [ ] **Step 4: Commit**

```bash
git add src-tauri/src/hermes/test_server.rs src-tauri/src/hermes/mod.rs
git commit -m "$(cat <<'EOF'
test(hermes): fake WS server fixture

cfg(test)-only fixture that binds 127.0.0.1:0, sends gateway.ready on
connect, scripts client.hello / session.create responses, and accepts
control messages to inject events or drop the connection.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task A6: WS client — connect, handshake, dispatch

The first IO-bearing module. Connects to the fake server, sends `client.hello`, accepts request dispatch, fans events.

**Files:**
- Create: `src-tauri/src/hermes/client.rs`
- Modify: `src-tauri/src/hermes/mod.rs`

- [ ] **Step 1: Write the failing integration test**

Create `src-tauri/src/hermes/client.rs`:

```rust
//! WSS client for the Hermes DesktopAppAdapter.

use std::sync::Arc;
use std::time::Duration;
use tokio::sync::{broadcast, Mutex};
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::http::Request;
use futures_util::{SinkExt, StreamExt};

use super::reconnect::{ConnState, ConnStatus};
use super::rpc::{RpcOutcome, RpcTracker};

pub const PROTOCOL_VERSION: u32 = 1;
pub const EVENT_CHANNEL_CAPACITY: usize = 256;

#[derive(Clone)]
pub struct HermesClient {
    inner: Arc<Inner>,
}

struct Inner {
    rpc: RpcTracker,
    events: broadcast::Sender<serde_json::Value>,
    status: broadcast::Sender<ConnStatus>,
    state: Mutex<ConnStatus>,
    sender: Mutex<Option<tokio::sync::mpsc::UnboundedSender<Message>>>,
}

impl HermesClient {
    pub fn new() -> Self {
        let (events, _) = broadcast::channel(EVENT_CHANNEL_CAPACITY);
        let (status, _) = broadcast::channel(16);
        Self {
            inner: Arc::new(Inner {
                rpc: RpcTracker::new(),
                events,
                status,
                state: Mutex::new(ConnStatus::disconnected()),
                sender: Mutex::new(None),
            }),
        }
    }

    pub fn subscribe_events(&self) -> broadcast::Receiver<serde_json::Value> {
        self.inner.events.subscribe()
    }

    pub fn subscribe_status(&self) -> broadcast::Receiver<ConnStatus> {
        self.inner.status.subscribe()
    }

    pub async fn current_status(&self) -> ConnStatus {
        self.inner.state.lock().await.clone()
    }

    /// One-shot connect: open WS, send client.hello, run read loop.
    /// On disconnect, returns; caller decides whether to reconnect.
    pub async fn connect_once(
        &self,
        url: &str,
        token: Option<&str>,
    ) -> Result<(), String> {
        self.set_state(ConnState::Connecting, None).await;

        let mut req = Request::builder().uri(url).body(()).map_err(|e| e.to_string())?;
        if let Some(t) = token {
            req.headers_mut().insert(
                "Authorization",
                format!("Bearer {}", t).parse().map_err(|_: tokio_tungstenite::tungstenite::http::header::InvalidHeaderValue| "bad token".to_string())?,
            );
        }

        let (ws, _resp) = connect_async(req).await.map_err(|e| e.to_string())?;
        let (mut write, mut read) = ws.split();
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<Message>();
        *self.inner.sender.lock().await = Some(tx);

        // Send client.hello.
        let (id, hello_rx) = self.inner.rpc.register();
        let hello = serde_json::json!({
            "jsonrpc":"2.0","id":id,"method":"client.hello",
            "params":{"client_id":"anandia-workspace","client_version":"0.1.0",
                      "capabilities":["voice.in","voice.out","attach.image"]}
        });
        write.send(Message::Text(hello.to_string())).await.map_err(|e| e.to_string())?;

        // Spawn writer pump.
        tokio::spawn(async move {
            while let Some(msg) = rx.recv().await {
                if write.send(msg).await.is_err() { break; }
            }
        });

        // Wait for hello response with a 10s deadline.
        let hello_outcome = tokio::time::timeout(Duration::from_secs(10), hello_rx)
            .await
            .map_err(|_| "client.hello timeout".to_string())?
            .map_err(|_| "client.hello channel closed".to_string())?;

        let proto = match &hello_outcome {
            RpcOutcome::Result(v) => v.get("protocol_version").and_then(|p| p.as_u64()).map(|p| p as u32),
            RpcOutcome::Error { message, .. } => return Err(format!("client.hello error: {}", message)),
        };
        if proto != Some(PROTOCOL_VERSION) {
            return Err(format!("protocol_version mismatch: got {:?}, want {}", proto, PROTOCOL_VERSION));
        }

        self.set_state_with_proto(ConnState::Connected, None, proto).await;

        // Read loop.
        let inner = self.inner.clone();
        tokio::spawn(async move {
            while let Some(frame) = read.next().await {
                match frame {
                    Ok(Message::Text(t)) => {
                        if let Ok(v) = serde_json::from_str::<serde_json::Value>(&t) {
                            if let Some(id) = v.get("id").and_then(|i| i.as_u64()) {
                                if let Some(result) = v.get("result") {
                                    inner.rpc.resolve(id, RpcOutcome::Result(result.clone()));
                                } else if let Some(err) = v.get("error") {
                                    inner.rpc.resolve(id, RpcOutcome::Error {
                                        code: err.get("code").and_then(|c| c.as_i64()).unwrap_or(-32603),
                                        message: err.get("message").and_then(|m| m.as_str()).unwrap_or("").to_string(),
                                        data: err.get("data").cloned(),
                                    });
                                }
                            } else if v.get("method").and_then(|m| m.as_str()) == Some("event") {
                                let _ = inner.events.send(v);
                            }
                        }
                    }
                    Ok(Message::Close(_)) | Err(_) => break,
                    _ => continue,
                }
            }
            // Connection closed.
            inner.rpc.drain("connection_lost");
        });

        Ok(())
    }

    pub async fn call(&self, method: &str, params: serde_json::Value) -> RpcOutcome {
        let (id, rx) = self.inner.rpc.register();
        let frame = serde_json::json!({
            "jsonrpc":"2.0","id":id,"method":method,"params":params
        });
        let sender_guard = self.inner.sender.lock().await;
        if let Some(tx) = sender_guard.as_ref() {
            if tx.send(Message::Text(frame.to_string())).is_err() {
                return RpcOutcome::Error { code: -32603, message: "no connection".into(), data: None };
            }
        } else {
            return RpcOutcome::Error { code: -32603, message: "no connection".into(), data: None };
        }
        drop(sender_guard);
        rx.await.unwrap_or(RpcOutcome::Error {
            code: -32603, message: "channel closed".into(), data: None,
        })
    }

    async fn set_state(&self, state: ConnState, err: Option<String>) {
        self.set_state_with_proto(state, err, None).await;
    }
    async fn set_state_with_proto(&self, state: ConnState, err: Option<String>, proto: Option<u32>) {
        let mut s = self.inner.state.lock().await;
        s.state = state;
        s.last_error = err;
        if let Some(p) = proto { s.protocol_version = Some(p); }
        let _ = self.inner.status.send(s.clone());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hermes::test_server::FakeServer;

    #[tokio::test]
    async fn connects_and_handshakes() {
        let srv = FakeServer::start().await;
        let url = srv.url();
        let client = HermesClient::new();
        client.connect_once(&url, None).await.expect("connect");
        let s = client.current_status().await;
        assert_eq!(s.state, ConnState::Connected);
        assert_eq!(s.protocol_version, Some(1));
    }

    #[tokio::test]
    async fn call_returns_result() {
        let srv = FakeServer::start().await;
        let client = HermesClient::new();
        client.connect_once(&srv.url(), None).await.unwrap();
        match client.call("session.create", serde_json::json!({})).await {
            RpcOutcome::Result(v) => assert_eq!(v["session_id"], "sess-fake-1"),
            other => panic!("unexpected: {:?}", other),
        }
    }
}
```

- [ ] **Step 2: Add to `mod.rs`**

```rust
pub mod client;
pub mod reconnect;
pub mod rpc;
pub mod token;

#[cfg(test)]
pub mod test_server;
```

- [ ] **Step 3: Run tests**

```bash
cd src-tauri && cargo test hermes::client
```

Expected: 2 tests PASS. If `connect_async` complains about `tokio-tungstenite` URL parsing, ensure the URL is `ws://...` (not `wss://`) — TLS lands in Phase D.

- [ ] **Step 4: Commit**

```bash
git add src-tauri/src/hermes/client.rs src-tauri/src/hermes/mod.rs
git commit -m "$(cat <<'EOF'
feat(hermes): WS client with handshake + RPC dispatch

connect_once opens the WS, sends Authorization bearer header, performs
the client.hello handshake with protocol_version check, then runs a read
loop that resolves RPC ids and fans events to a broadcast channel.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task A7: Reconnect supervisor

Wraps `connect_once` in a loop with backoff. Drives state transitions including `Reconnecting`.

**Files:**
- Modify: `src-tauri/src/hermes/client.rs` (add `run_supervised`)

- [ ] **Step 1: Add the test**

Append to `src-tauri/src/hermes/client.rs` `tests` module:

```rust
#[tokio::test]
async fn reconnects_after_drop() {
    let srv = FakeServer::start().await;
    let url = srv.url();
    let client = HermesClient::new();
    let mut status_rx = client.subscribe_status();

    let c2 = client.clone();
    let supervisor = tokio::spawn(async move {
        c2.run_supervised(url, None).await;
    });

    // Wait for first Connected.
    let mut saw_connected = false;
    for _ in 0..20 {
        if let Ok(Ok(s)) = tokio::time::timeout(Duration::from_millis(500), status_rx.recv()).await {
            if s.state == ConnState::Connected { saw_connected = true; break; }
        }
    }
    assert!(saw_connected, "did not reach Connected");

    // Drop the connection.
    srv.control.send(crate::hermes::test_server::ServerCmd::Drop).unwrap();

    // Expect a Reconnecting transition.
    let mut saw_reconnecting = false;
    for _ in 0..20 {
        if let Ok(Ok(s)) = tokio::time::timeout(Duration::from_secs(2), status_rx.recv()).await {
            if s.state == ConnState::Reconnecting { saw_reconnecting = true; break; }
        }
    }
    assert!(saw_reconnecting, "did not see Reconnecting after drop");

    supervisor.abort();
}
```

- [ ] **Step 2: Add `run_supervised`**

In `client.rs`, add after `connect_once`:

```rust
impl HermesClient {
    /// Long-running supervisor: connect, watch for disconnect, backoff, retry.
    /// Exits only when the caller drops it (cancellation token can be added later).
    pub async fn run_supervised(&self, url: String, token: Option<String>) {
        let mut backoff = super::reconnect::Backoff::new();
        loop {
            match self.connect_once(&url, token.as_deref()).await {
                Ok(()) => {
                    backoff.reset();
                    // Wait for the connection to drop (sender cleared in read loop end).
                    self.wait_for_disconnect().await;
                    self.set_state(ConnState::Reconnecting, Some("disconnected".into())).await;
                }
                Err(e) => {
                    self.set_state(ConnState::Reconnecting, Some(e)).await;
                }
            }
            tokio::time::sleep(backoff.next_delay()).await;
        }
    }

    async fn wait_for_disconnect(&self) {
        loop {
            tokio::time::sleep(Duration::from_millis(200)).await;
            let sender_present = self.inner.sender.lock().await.is_some();
            // The read loop drops the sender when it exits; check by trying to send a noop.
            // Cleaner: have the read loop notify via a watch channel — refactor in Phase D.
            if !sender_present { break; }
            // Heuristic: if pending count is high or events stalled — skip; just poll the closed sender.
            let s = self.inner.sender.lock().await;
            if s.is_none() { break; }
            // If the sender closed (read loop exited it via drain), treat as disconnect.
            if s.as_ref().map(|tx| tx.is_closed()).unwrap_or(true) { break; }
        }
        *self.inner.sender.lock().await = None;
    }
}
```

> Note for the implementer: the disconnect-detection above is intentionally pragmatic for v1. A proper notify channel from the read loop is a clean-up follow-up; add it during Phase D polish.

- [ ] **Step 3: Run tests**

```bash
cd src-tauri && cargo test hermes::client
```

Expected: 3 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add src-tauri/src/hermes/client.rs
git commit -m "$(cat <<'EOF'
feat(hermes): reconnect supervisor with backoff

run_supervised loops connect_once with the 1/2/4/8/16/30s backoff,
emitting Reconnecting on each retry. Resets backoff on successful
connect.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task A8: Notification dispatcher scaffold

Subscribes to events, ready to fire OS notifications. UI consumer wired in Phase C.

**Files:**
- Create: `src-tauri/src/hermes/notification.rs`
- Modify: `src-tauri/src/hermes/mod.rs`

- [ ] **Step 1: Write the test + scaffold**

Create `src-tauri/src/hermes/notification.rs`:

```rust
//! OS notification dispatcher. Subscribes to hermes events and fires a
//! notification when window is unfocused for approval.request and
//! message.complete.

use tokio::sync::broadcast;
use tracing::{debug, warn};

pub struct NotificationDispatcher {
    pub focused: std::sync::atomic::AtomicBool,
}

impl NotificationDispatcher {
    pub fn new() -> Self {
        Self { focused: std::sync::atomic::AtomicBool::new(true) }
    }

    pub fn set_focused(&self, focused: bool) {
        self.focused.store(focused, std::sync::atomic::Ordering::SeqCst);
    }

    pub fn should_notify(&self, event_type: &str) -> bool {
        if self.focused.load(std::sync::atomic::Ordering::SeqCst) { return false; }
        matches!(event_type, "approval.request" | "message.complete")
    }

    pub async fn run(self: std::sync::Arc<Self>, mut rx: broadcast::Receiver<serde_json::Value>) {
        while let Ok(env) = rx.recv().await {
            let event_type = env.pointer("/params/type").and_then(|v| v.as_str()).unwrap_or("");
            if self.should_notify(event_type) {
                debug!("hermes notification: {}", event_type);
                // Phase C wires this to tauri-plugin-notification.
                // For now we just log — keeps the dispatcher behavior testable.
            }
        }
        warn!("notification dispatcher: event channel closed");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn should_notify_only_when_unfocused() {
        let d = NotificationDispatcher::new();
        d.set_focused(true);
        assert!(!d.should_notify("approval.request"));
        d.set_focused(false);
        assert!(d.should_notify("approval.request"));
        assert!(d.should_notify("message.complete"));
        assert!(!d.should_notify("message.delta"));
    }
}
```

- [ ] **Step 2: Add to `mod.rs`**

```rust
pub mod client;
pub mod notification;
pub mod reconnect;
pub mod rpc;
pub mod token;

#[cfg(test)]
pub mod test_server;
```

- [ ] **Step 3: Run tests**

```bash
cd src-tauri && cargo test hermes::notification
```

Expected: 1 test PASS.

- [ ] **Step 4: Commit**

```bash
git add src-tauri/src/hermes/notification.rs src-tauri/src/hermes/mod.rs
git commit -m "$(cat <<'EOF'
feat(hermes): notification dispatcher scaffold

NotificationDispatcher tracks window focus and decides which event
types deserve an OS notification. Wiring to tauri-plugin-notification
lands in Phase C alongside the UI focus listener.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task A9: Tauri command surface

Expose `hermes_call`, `hermes_status`, `hermes_set_token`, `hermes_get_token_metadata`, `hermes_clear_token`, `hermes_health_check`.

**Files:**
- Create: `src-tauri/src/commands/hermes.rs`
- Modify: `src-tauri/src/lib.rs`

- [ ] **Step 1: Create commands file**

Create `src-tauri/src/commands/hermes.rs`:

```rust
//! Tauri command surface for the Hermes adapter.
//! TS facade: invoke('hermes_call', …), invoke('hermes_status'), etc.

use std::sync::Arc;
use tauri::State;

use crate::hermes::client::HermesClient;
use crate::hermes::reconnect::ConnStatus;
use crate::hermes::rpc::RpcOutcome;
use crate::hermes::token::{self, TokenMetadata};

pub struct HermesState {
    pub client: HermesClient,
}

#[derive(Debug, serde::Serialize)]
#[serde(untagged)]
pub enum CallResponse {
    Result { result: serde_json::Value },
    Error { error: ErrorBody },
}

#[derive(Debug, serde::Serialize)]
pub struct ErrorBody { pub code: i64, pub message: String, pub data: Option<serde_json::Value> }

#[tauri::command]
pub async fn hermes_call(
    state: State<'_, Arc<HermesState>>,
    method: String,
    params: Option<serde_json::Value>,
) -> Result<CallResponse, String> {
    let outcome = state.client.call(&method, params.unwrap_or(serde_json::Value::Null)).await;
    Ok(match outcome {
        RpcOutcome::Result(v) => CallResponse::Result { result: v },
        RpcOutcome::Error { code, message, data } =>
            CallResponse::Error { error: ErrorBody { code, message, data } },
    })
}

#[tauri::command]
pub async fn hermes_status(state: State<'_, Arc<HermesState>>) -> Result<ConnStatus, String> {
    Ok(state.client.current_status().await)
}

#[tauri::command]
pub async fn hermes_set_token(
    state: State<'_, Arc<HermesState>>,
    token: String,
    host: String,
    port: u16,
) -> Result<(), String> {
    if token.len() < 32 {
        return Err("token must be at least 32 chars".into());
    }
    token::set(&token, &host, port)?;
    // Trigger reconnect by calling run_supervised again would require more state;
    // for v1 we just store. The supervisor task running in lib.rs setup picks up
    // the new token on its next cycle (will be addressed in Phase A wiring).
    let _ = state.client.current_status().await;
    Ok(())
}

#[tauri::command]
pub fn hermes_get_token_metadata() -> Result<TokenMetadata, String> {
    token::get_metadata()
}

#[tauri::command]
pub fn hermes_clear_token() -> Result<(), String> {
    token::clear()
}

#[tauri::command]
pub async fn hermes_health_check(host: String, port: u16) -> Result<serde_json::Value, String> {
    let url = format!("http://{}:{}/health", host, port);
    let resp = reqwest::get(&url).await.map_err(|e| e.to_string())?;
    let json = resp.json::<serde_json::Value>().await.map_err(|e| e.to_string())?;
    Ok(json)
}
```

- [ ] **Step 2: Register in `lib.rs`**

Edit `src-tauri/src/lib.rs`. Find the `tauri::Builder` setup and add the commands:

```rust
// Inside the .invoke_handler(tauri::generate_handler![...]) list, add:
crate::commands::hermes::hermes_call,
crate::commands::hermes::hermes_status,
crate::commands::hermes::hermes_set_token,
crate::commands::hermes::hermes_get_token_metadata,
crate::commands::hermes::hermes_clear_token,
crate::commands::hermes::hermes_health_check,
```

In the `.setup()` closure, spawn the supervisor:

```rust
.setup(|app| {
    // ... existing setup ...

    let client = crate::hermes::client::HermesClient::new();
    let state = std::sync::Arc::new(crate::commands::hermes::HermesState {
        client: client.clone(),
    });
    app.manage(state.clone());

    // Wire status events out to TS.
    let status_rx = client.subscribe_status();
    let app_handle = app.handle().clone();
    tokio::spawn(async move {
        let mut rx = status_rx;
        while let Ok(s) = rx.recv().await {
            let _ = app_handle.emit("hermes:status", &s);
        }
    });

    // Wire generic events out to TS.
    let evt_rx = client.subscribe_events();
    let app_handle2 = app.handle().clone();
    tokio::spawn(async move {
        let mut rx = evt_rx;
        while let Ok(env) = rx.recv().await {
            let _ = app_handle2.emit("hermes:event", &env);
        }
    });

    // Spawn supervisor if a token is paired.
    if let Ok(meta) = crate::hermes::token::get_metadata() {
        if meta.paired {
            if let (Some(host), Some(port), Ok(Some(tok))) =
                (meta.host.clone(), meta.port, crate::hermes::token::get_token())
            {
                let url = format!("ws://{}:{}/ws", host, port);
                let c = client.clone();
                tokio::spawn(async move { c.run_supervised(url, Some(tok)).await; });
            }
        }
    }

    Ok(())
})
```

- [ ] **Step 3: Add `commands::hermes` to the commands module**

Edit `src-tauri/src/commands/mod.rs` (or wherever the commands are listed) and add:

```rust
pub mod hermes;
```

- [ ] **Step 4: Build and run all hermes tests**

```bash
cd src-tauri && cargo build && cargo test hermes
```

Expected: build succeeds; all hermes-module tests pass.

- [ ] **Step 5: Commit**

```bash
git add src-tauri/src/commands/hermes.rs src-tauri/src/commands/mod.rs src-tauri/src/lib.rs
git commit -m "$(cat <<'EOF'
feat(hermes): Tauri command surface + setup hook

Exposes hermes_call, hermes_status, hermes_set_token,
hermes_get_token_metadata, hermes_clear_token, hermes_health_check.
Setup hook spawns the supervisor when a paired token is in keyring,
and forwards hermes:status / hermes:event to the webview.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

**Phase A complete.** Acceptance check before moving to Phase B:

```bash
cd src-tauri && cargo test hermes
```

Expected: all hermes tests pass. Run the app once with `bun run tauri dev` and verify the build succeeds — no functional UI yet.

---

## Phase B — TS service, hooks, store, mock

**Goal at end of Phase B:** From a Vitest-rendered test, a fake `BottomCommandBar` can submit a prompt against the mock and see deltas accumulate in `agentCanvasStore`. No real UI yet.

### Task B1: Test fixtures for Hermes events

Typed factory functions used everywhere downstream.

**Files:**
- Create: `tests/fixtures/hermes-events.ts`

- [ ] **Step 1: Write the fixtures**

Create `tests/fixtures/hermes-events.ts`:

```ts
import type { HermesEventEnvelope, MessageDeltaEvent, ToolStartEvent, ToolCompleteEvent, ApprovalRequestEvent, MessageCompleteEvent } from '@/components/agent-canvas/types';

export function makeEnvelope<P>(type: string, sessionId: string, payload: P): HermesEventEnvelope {
  return { jsonrpc: '2.0', method: 'event', params: { type, session_id: sessionId, payload: payload as Record<string, unknown> } };
}

export const makeMessageStart = (sessionId = 'sess-1') =>
  makeEnvelope('message.start', sessionId, {});
export const makeMessageDelta = (text: string, sessionId = 'sess-1'): HermesEventEnvelope =>
  makeEnvelope('message.delta', sessionId, { text });
export const makeMessageComplete = (sessionId = 'sess-1', usage = { tokens: 0 }): HermesEventEnvelope =>
  makeEnvelope('message.complete', sessionId, { usage });
export const makeToolStart = (name: string, input: object = {}, toolCallId = `tc-${Date.now()}`, sessionId = 'sess-1'): HermesEventEnvelope =>
  makeEnvelope('tool.start', sessionId, { name, input, tool_call_id: toolCallId });
export const makeToolProgress = (toolCallId: string, preview: string, sessionId = 'sess-1'): HermesEventEnvelope =>
  makeEnvelope('tool.progress', sessionId, { tool_call_id: toolCallId, preview });
export const makeToolComplete = (toolCallId: string, output: string, durationMs = 1500, status: 'ok'|'error' = 'ok', sessionId = 'sess-1'): HermesEventEnvelope =>
  makeEnvelope('tool.complete', sessionId, { tool_call_id: toolCallId, output, duration_ms: durationMs, status });
export const makeApprovalRequest = (tool: string, args: object = {}, requestId = `ar-${Date.now()}`, sessionId = 'sess-1'): HermesEventEnvelope =>
  makeEnvelope('approval.request', sessionId, { request_id: requestId, tool, args, choices: ['allow-once', 'always', 'deny'] });
export const makeGatewayReady = (): HermesEventEnvelope =>
  ({ jsonrpc: '2.0', method: 'event', params: { type: 'gateway.ready', session_id: '', payload: { skin: {} } } });
```

- [ ] **Step 2: Sketch the types file (will be expanded in later tasks)**

Create `src/components/agent-canvas/types.ts`:

```ts
export interface HermesEventEnvelope {
  jsonrpc: '2.0';
  method: 'event';
  params: {
    type: string;
    session_id: string;
    payload: Record<string, unknown>;
  };
}

export interface MessageDeltaEvent { text: string }
export interface MessageCompleteEvent { usage: { tokens: number } }
export interface ToolStartEvent { name: string; input: object; tool_call_id: string }
export interface ToolProgressEvent { tool_call_id: string; preview: string }
export interface ToolCompleteEvent { tool_call_id: string; output: string; duration_ms: number; status: 'ok' | 'error' }
export interface ApprovalRequestEvent { request_id: string; tool: string; args: object; choices: ('allow-once'|'always'|'deny')[] }
export interface ApprovalRespondParams { request_id: string; decision: 'allow-once'|'always'|'deny' }

export type ConnState = 'disconnected' | 'connecting' | 'connected' | 'reconnecting' | 'error';
export interface ConnStatus { state: ConnState; last_error?: string; paired_host?: string; protocol_version?: number }

export type SideCardKind = 'tool-progress' | 'artifact' | 'subagent-thread';
export interface SideCard {
  id: string;
  kind: SideCardKind;
  title: string;
  toolCallId?: string;
  content: string;
  pinned: boolean;
  status: 'running' | 'complete' | 'error';
  createdAt: number;
  completedAt?: number;
}

export type TranscriptEntry =
  | { kind: 'message'; role: 'user'|'assistant'; text: string; id: string; ts: number }
  | { kind: 'reasoning'; text: string; id: string; ts: number }
  | { kind: 'tool-inline'; name: string; preview: string; id: string; toolCallId: string; status: 'running'|'complete'|'error'; ts: number }
  | { kind: 'approval-inline'; requestId: string; tool: string; args: object; decision: 'pending'|'allow-once'|'always'|'deny'; id: string; ts: number };
```

- [ ] **Step 3: Run typecheck**

```bash
bun run typecheck
```

Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/hermes-events.ts src/components/agent-canvas/types.ts
git commit -m "$(cat <<'EOF'
test(agent-canvas): event fixtures and core types

Typed factory functions for every event in the contract §5, plus the
shared types used across service, store, hooks, and components.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task B2: `sideCardHeuristics` utility (pure)

Decides whether a tool event spawns a side-card vs renders inline.

**Files:**
- Create: `src/utils/sideCardHeuristics.ts`
- Create: `src/utils/sideCardHeuristics.test.ts`

- [ ] **Step 1: Write the failing tests**

Create `src/utils/sideCardHeuristics.test.ts`:

```ts
import { describe, it, expect } from 'vitest';
import { shouldSpawnCard, OUTPUT_INLINE_THRESHOLD, LONG_RUNNING_TOOL_ALLOWLIST } from './sideCardHeuristics';

describe('sideCardHeuristics', () => {
  it('spawns a card for tools in the long-running allowlist', () => {
    expect(shouldSpawnCard({ name: 'web_research', input: {}, hasProgressEvents: false, outputSize: 0 })).toBe(true);
  });
  it('spawns a card when progress events arrived', () => {
    expect(shouldSpawnCard({ name: 'read_file', input: {}, hasProgressEvents: true, outputSize: 100 })).toBe(true);
  });
  it('spawns a card when output exceeds inline threshold', () => {
    expect(shouldSpawnCard({ name: 'read_file', input: {}, hasProgressEvents: false, outputSize: OUTPUT_INLINE_THRESHOLD + 1 })).toBe(true);
  });
  it('renders inline for short non-allowlisted tools', () => {
    expect(shouldSpawnCard({ name: 'read_file', input: {}, hasProgressEvents: false, outputSize: 200 })).toBe(false);
  });
  it('allowlist contains expected entries', () => {
    expect(LONG_RUNNING_TOOL_ALLOWLIST).toContain('web_research');
    expect(LONG_RUNNING_TOOL_ALLOWLIST).toContain('generate_image');
  });
});
```

- [ ] **Step 2: Run tests — should fail**

```bash
bun run test src/utils/sideCardHeuristics.test.ts
```

Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

Create `src/utils/sideCardHeuristics.ts`:

```ts
export const OUTPUT_INLINE_THRESHOLD = 800;

export const LONG_RUNNING_TOOL_ALLOWLIST = new Set([
  'web_research',
  'web_search',
  'generate_image',
  'file_write_large',
  'browser_navigate',
  'browser_extract',
]);

export interface ToolHeuristicInput {
  name: string;
  input: object;
  hasProgressEvents: boolean;
  outputSize: number;
}

export function shouldSpawnCard(t: ToolHeuristicInput): boolean {
  if (LONG_RUNNING_TOOL_ALLOWLIST.has(t.name)) return true;
  if (t.hasProgressEvents) return true;
  if (t.outputSize > OUTPUT_INLINE_THRESHOLD) return true;
  return false;
}
```

> Heuristic note: the Set type comparison `LONG_RUNNING_TOOL_ALLOWLIST.has(...)` requires the test to compare with `.has()` instead of `.includes()`. Update the assertion if needed.

Adjust the test assertion:

```ts
expect(LONG_RUNNING_TOOL_ALLOWLIST.has('web_research')).toBe(true);
expect(LONG_RUNNING_TOOL_ALLOWLIST.has('generate_image')).toBe(true);
```

- [ ] **Step 4: Re-run tests**

```bash
bun run test src/utils/sideCardHeuristics.test.ts
```

Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/utils/sideCardHeuristics.ts src/utils/sideCardHeuristics.test.ts
git commit -m "$(cat <<'EOF'
feat(agent-canvas): side-card spawn heuristics

Pure utility deciding whether a tool event spawns a side-card or
renders inline. Allowlist + has-progress + output-size threshold.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task B3: `agentCanvasStore` (Zustand + Immer)

Single source of truth for transcript, side-cards, approvals, threads.

**Files:**
- Create: `src/stores/agentCanvasStore.ts`
- Create: `src/stores/agentCanvasStore.test.ts`

- [ ] **Step 1: Write the failing tests**

Create `src/stores/agentCanvasStore.test.ts`:

```ts
import { describe, it, expect, beforeEach } from 'vitest';
import { useAgentCanvasStore } from './agentCanvasStore';

describe('agentCanvasStore', () => {
  beforeEach(() => useAgentCanvasStore.getState().reset());

  it('appends user message on prompt submit', () => {
    useAgentCanvasStore.getState().setActiveSession('sess-1');
    useAgentCanvasStore.getState().appendUserMessage('hello');
    const t = useAgentCanvasStore.getState().transcript;
    expect(t).toHaveLength(1);
    expect(t[0]).toMatchObject({ kind: 'message', role: 'user', text: 'hello' });
  });

  it('accumulates assistant deltas into a single message', () => {
    useAgentCanvasStore.getState().setActiveSession('sess-1');
    useAgentCanvasStore.getState().applyAssistantDelta('Hel');
    useAgentCanvasStore.getState().applyAssistantDelta('lo!');
    const t = useAgentCanvasStore.getState().transcript;
    expect(t).toHaveLength(1);
    expect(t[0]).toMatchObject({ kind: 'message', role: 'assistant', text: 'Hello!' });
  });

  it('finalizes assistant message on message.complete', () => {
    useAgentCanvasStore.getState().setActiveSession('sess-1');
    useAgentCanvasStore.getState().applyAssistantDelta('done');
    useAgentCanvasStore.getState().completeAssistantMessage();
    const t = useAgentCanvasStore.getState().transcript;
    expect(t[0].kind).toBe('message');
    expect(useAgentCanvasStore.getState().pendingTurn).toBe(false);
  });

  it('spawns and updates a side-card by toolCallId', () => {
    useAgentCanvasStore.getState().spawnSideCard({
      id: 'sc-1', kind: 'tool-progress', title: 'web_research', toolCallId: 'tc-1',
      content: '', pinned: false, status: 'running', createdAt: Date.now(),
    });
    useAgentCanvasStore.getState().updateSideCardByToolCall('tc-1', { content: 'progress…' });
    expect(useAgentCanvasStore.getState().sideCards[0].content).toBe('progress…');
  });

  it('queues an approval request and resolves it', () => {
    useAgentCanvasStore.getState().queueApproval({ requestId: 'ar-1', tool: 'bash', args: {}, sessionId: 'sess-1' });
    expect(useAgentCanvasStore.getState().approvalQueue).toHaveLength(1);
    useAgentCanvasStore.getState().resolveApproval('ar-1', 'allow-once');
    expect(useAgentCanvasStore.getState().approvalQueue).toHaveLength(0);
  });

  it('reset clears everything', () => {
    useAgentCanvasStore.getState().setActiveSession('sess-1');
    useAgentCanvasStore.getState().appendUserMessage('hi');
    useAgentCanvasStore.getState().reset();
    expect(useAgentCanvasStore.getState().activeSessionId).toBeNull();
    expect(useAgentCanvasStore.getState().transcript).toHaveLength(0);
  });
});
```

- [ ] **Step 2: Write the store**

Create `src/stores/agentCanvasStore.ts`:

```ts
import { create } from 'zustand';
import { immer } from 'zustand/middleware/immer';
import type { TranscriptEntry, SideCard } from '@/components/agent-canvas/types';

interface PendingApproval {
  requestId: string;
  tool: string;
  args: object;
  sessionId: string;
}

interface AgentCanvasState {
  activeSessionId: string | null;
  transcript: TranscriptEntry[];
  sideCards: SideCard[];
  approvalQueue: PendingApproval[];
  pendingTurn: boolean;

  // actions
  setActiveSession(id: string | null): void;
  appendUserMessage(text: string): void;
  applyAssistantDelta(text: string): void;
  completeAssistantMessage(): void;
  appendToolInline(name: string, preview: string, toolCallId: string): void;
  updateToolInline(toolCallId: string, preview: string, status?: 'complete'|'error'): void;
  spawnSideCard(card: SideCard): void;
  updateSideCardByToolCall(toolCallId: string, patch: Partial<SideCard>): void;
  removeSideCard(id: string): void;
  pinSideCard(id: string, pinned: boolean): void;
  queueApproval(a: PendingApproval): void;
  resolveApproval(requestId: string, decision: 'allow-once'|'always'|'deny'): void;
  reset(): void;
}

const initial = (): Pick<AgentCanvasState, 'activeSessionId'|'transcript'|'sideCards'|'approvalQueue'|'pendingTurn'> => ({
  activeSessionId: null,
  transcript: [],
  sideCards: [],
  approvalQueue: [],
  pendingTurn: false,
});

export const useAgentCanvasStore = create<AgentCanvasState>()(immer((set) => ({
  ...initial(),

  setActiveSession: (id) => set((s) => { s.activeSessionId = id; }),

  appendUserMessage: (text) => set((s) => {
    s.transcript.push({ kind: 'message', role: 'user', text, id: `u-${Date.now()}-${Math.random()}`, ts: Date.now() });
    s.pendingTurn = true;
  }),

  applyAssistantDelta: (text) => set((s) => {
    const last = s.transcript[s.transcript.length - 1];
    if (last && last.kind === 'message' && last.role === 'assistant') {
      last.text += text;
    } else {
      s.transcript.push({ kind: 'message', role: 'assistant', text, id: `a-${Date.now()}-${Math.random()}`, ts: Date.now() });
    }
  }),

  completeAssistantMessage: () => set((s) => { s.pendingTurn = false; }),

  appendToolInline: (name, preview, toolCallId) => set((s) => {
    s.transcript.push({ kind: 'tool-inline', name, preview, toolCallId, status: 'running', id: `t-${Date.now()}-${Math.random()}`, ts: Date.now() });
  }),

  updateToolInline: (toolCallId, preview, status) => set((s) => {
    const e = s.transcript.find((e) => e.kind === 'tool-inline' && e.toolCallId === toolCallId) as Extract<TranscriptEntry, {kind: 'tool-inline'}> | undefined;
    if (e) { e.preview = preview; if (status) e.status = status; }
  }),

  spawnSideCard: (card) => set((s) => { s.sideCards.push(card); }),

  updateSideCardByToolCall: (toolCallId, patch) => set((s) => {
    const c = s.sideCards.find((c) => c.toolCallId === toolCallId);
    if (c) Object.assign(c, patch);
  }),

  removeSideCard: (id) => set((s) => {
    s.sideCards = s.sideCards.filter((c) => c.id !== id);
  }),

  pinSideCard: (id, pinned) => set((s) => {
    const c = s.sideCards.find((c) => c.id === id);
    if (c) c.pinned = pinned;
  }),

  queueApproval: (a) => set((s) => {
    s.approvalQueue.push(a);
    s.transcript.push({ kind: 'approval-inline', requestId: a.requestId, tool: a.tool, args: a.args, decision: 'pending', id: `ap-${a.requestId}`, ts: Date.now() });
  }),

  resolveApproval: (requestId, decision) => set((s) => {
    s.approvalQueue = s.approvalQueue.filter((a) => a.requestId !== requestId);
    const e = s.transcript.find((e) => e.kind === 'approval-inline' && e.requestId === requestId) as Extract<TranscriptEntry, {kind: 'approval-inline'}> | undefined;
    if (e) e.decision = decision;
  }),

  reset: () => set(() => ({ ...initial() })),
})));
```

- [ ] **Step 3: Run tests**

```bash
bun run test src/stores/agentCanvasStore.test.ts
```

Expected: 6 PASS.

- [ ] **Step 4: Commit**

```bash
git add src/stores/agentCanvasStore.ts src/stores/agentCanvasStore.test.ts
git commit -m "$(cat <<'EOF'
feat(agent-canvas): Zustand store for transcript, side-cards, approvals

Single source of truth driving the AgentCanvas. Append-style transcript
with delta accumulation, side-card pool indexed by toolCallId, approval
queue with paired inline transcript entries.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task B4: `mockHermes` scripted in-memory server

Lets the UI run end-to-end without the Mac mini.

**Files:**
- Create: `src/services/mockHermes.ts`
- Create: `src/services/mockHermes.test.ts`

- [ ] **Step 1: Write the failing tests**

Create `src/services/mockHermes.test.ts`:

```ts
import { describe, it, expect, vi } from 'vitest';
import { createMockHermes } from './mockHermes';

describe('mockHermes', () => {
  it('responds to client.hello with protocol_version 1', async () => {
    const m = createMockHermes();
    const res = await m.call('client.hello', { client_id: 't', client_version: '0', capabilities: [] });
    expect(res.protocol_version).toBe(1);
  });

  it('emits message deltas after prompt.submit', async () => {
    const m = createMockHermes();
    const seen: string[] = [];
    m.on((env) => { if (env.params.type === 'message.delta') seen.push(String(env.params.payload.text)); });
    await m.call('session.create', {});
    await m.call('prompt.submit', { session_id: 'sess-mock-1', text: 'hello' });
    await new Promise((r) => setTimeout(r, 50));
    expect(seen.length).toBeGreaterThanOrEqual(3);
  });

  it('returns a fake session list', async () => {
    const m = createMockHermes();
    const res = await m.call('session.list', {});
    expect(res.sessions).toHaveLength(2);
  });
});
```

- [ ] **Step 2: Implement mockHermes**

Create `src/services/mockHermes.ts`:

```ts
import type { HermesEventEnvelope } from '@/components/agent-canvas/types';
import {
  makeMessageStart, makeMessageDelta, makeMessageComplete,
  makeGatewayReady,
} from '../../tests/fixtures/hermes-events';

type EventHandler = (env: HermesEventEnvelope) => void;

export interface MockHermes {
  call(method: string, params: object): Promise<any>;
  on(handler: EventHandler): () => void;
  /** Inject any event externally — used by tests for tool / approval flows. */
  inject(env: HermesEventEnvelope): void;
}

const MOCK_DELTAS = ['Sure', ' — ', 'this is the ', 'mock ', 'agent ', 'replying.'];

export function createMockHermes(): MockHermes {
  const handlers = new Set<EventHandler>();
  const fan = (env: HermesEventEnvelope) => handlers.forEach((h) => h(env));

  // Initial gateway.ready on construction (mirrors real adapter).
  setTimeout(() => fan(makeGatewayReady()), 0);

  return {
    on(handler) {
      handlers.add(handler);
      return () => handlers.delete(handler);
    },

    inject(env) { fan(env); },

    async call(method, params: any) {
      switch (method) {
        case 'client.hello':
          return {
            server_version: 'mock-hermes',
            protocol_version: 1,
            capabilities: ['session.list','session.resume','prompt.submit','slash.exec','model.options','approval'],
            client_id: params.client_id,
            client_version: params.client_version,
            client_capabilities: params.capabilities,
          };
        case 'session.list':
          return {
            sessions: [
              { session_id: 'sess-mock-1', title: 'Mock session 1', model: 'alex', last_active_at: Date.now()-60000, message_count: 4 },
              { session_id: 'sess-mock-2', title: 'Mock session 2', model: 'echo', last_active_at: Date.now()-3600000, message_count: 12 },
            ],
          };
        case 'session.create':
          return { session_id: 'sess-mock-1', info: { model: 'alex' } };
        case 'session.close':
          return { closed: true };
        case 'session.resume':
          return { session_id: params.session_id, info: { model: 'alex', history: [] } };
        case 'prompt.submit': {
          const sid = String(params.session_id);
          fan(makeMessageStart(sid));
          for (let i = 0; i < MOCK_DELTAS.length; i++) {
            await new Promise((r) => setTimeout(r, 80));
            fan(makeMessageDelta(MOCK_DELTAS[i], sid));
          }
          fan(makeMessageComplete(sid, { tokens: 24 }));
          return { ok: true };
        }
        case 'model.options':
          return { models: [
            { id: 'alex', label: 'Alex', description: 'Fast' },
            { id: 'echo', label: 'Echo', description: 'Creative' },
          ]};
        case 'complete.slash':
          return { suggestions: ['/personality','/sessions','/skills','/cron','/model'].filter((s) => s.startsWith(String(params.prefix ?? '')))};
        case 'approval.respond':
        case 'slash.exec':
          return { ok: true };
        default:
          return { echoed_method: method };
      }
    },
  };
}
```

- [ ] **Step 3: Run tests**

```bash
bun run test src/services/mockHermes.test.ts
```

Expected: 3 PASS.

- [ ] **Step 4: Commit**

```bash
git add src/services/mockHermes.ts src/services/mockHermes.test.ts
git commit -m "$(cat <<'EOF'
feat(agent-canvas): mockHermes scripted in-memory server

Enables UI end-to-end runs without the Mac mini: scripted client.hello,
session lifecycle, streaming prompt.submit, model.options. Tests and
dev-mode service fall back to this when Tauri invoke is unavailable.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task B5: `hermesService.ts` (TS facade)

Wraps Tauri `invoke` + `listen`; falls back to `mockHermes` when invoke is unavailable.

**Files:**
- Create: `src/services/hermesService.ts`
- Create: `src/services/hermesService.test.ts`

- [ ] **Step 1: Write the failing tests**

Create `src/services/hermesService.test.ts`:

```ts
import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock the Tauri shim used elsewhere in the codebase.
vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn() }));
vi.mock('@tauri-apps/api/event', () => ({ listen: vi.fn(async () => () => {}) }));

import { invoke } from '@tauri-apps/api/core';
import { hermesService } from './hermesService';

describe('hermesService', () => {
  beforeEach(() => vi.resetAllMocks());

  it('routes call() to invoke("hermes_call") with method+params', async () => {
    (invoke as any).mockResolvedValue({ result: { ok: true } });
    const res = await hermesService.call('session.create', {});
    expect(invoke).toHaveBeenCalledWith('hermes_call', { method: 'session.create', params: {} });
    expect(res).toEqual({ ok: true });
  });

  it('rejects when result has error body', async () => {
    (invoke as any).mockResolvedValue({ error: { code: 4001, message: 'missing param' } });
    await expect(hermesService.call('prompt.submit', {})).rejects.toMatchObject({ code: 4001, message: 'missing param' });
  });

  it('falls back to mock when invoke unavailable', async () => {
    (invoke as any).mockRejectedValue(new Error('no Tauri runtime'));
    const res = await hermesService.call('session.list', {});
    expect((res as any).sessions).toHaveLength(2); // mock returns two sessions
  });
});
```

- [ ] **Step 2: Implement the service**

Create `src/services/hermesService.ts`:

```ts
import { invoke } from '@tauri-apps/api/core';
import { listen, type UnlistenFn } from '@tauri-apps/api/event';
import type { HermesEventEnvelope, ConnStatus } from '@/components/agent-canvas/types';
import { createMockHermes, type MockHermes } from './mockHermes';

export interface HermesError { code: number; message: string; data?: unknown }

let mock: MockHermes | null = null;
function getMock(): MockHermes {
  if (!mock) mock = createMockHermes();
  return mock;
}

async function tryInvoke<T>(method: string, params: object): Promise<T> {
  const resp = await invoke<{ result?: T; error?: HermesError }>('hermes_call', { method, params });
  if (resp && typeof resp === 'object' && 'error' in resp && resp.error) {
    throw resp.error as HermesError;
  }
  // resp may be { result } or already the unwrapped value depending on serde shape.
  return (resp as { result: T }).result ?? (resp as unknown as T);
}

export const hermesService = {
  async call<T = unknown>(method: string, params: object): Promise<T> {
    try {
      return await tryInvoke<T>(method, params);
    } catch (err) {
      // Distinguish "real RPC error" (HermesError shape) from "Tauri unavailable" (Error).
      if (err && typeof err === 'object' && 'code' in (err as object)) {
        throw err;
      }
      // Mock fallback.
      return await getMock().call(method, params) as T;
    }
  },

  /** Subscribe to all hermes events. Returns an unlisten function. */
  async onEvent(handler: (env: HermesEventEnvelope) => void): Promise<UnlistenFn> {
    try {
      return await listen<HermesEventEnvelope>('hermes:event', (e) => handler(e.payload));
    } catch {
      const off = getMock().on(handler);
      return async () => off();
    }
  },

  async onStatus(handler: (s: ConnStatus) => void): Promise<UnlistenFn> {
    try {
      return await listen<ConnStatus>('hermes:status', (e) => handler(e.payload));
    } catch {
      // mock has no status; emit "connected" once for parity.
      handler({ state: 'connected', protocol_version: 1 });
      return async () => {};
    }
  },

  async getStatus(): Promise<ConnStatus> {
    try { return await invoke<ConnStatus>('hermes_status'); }
    catch { return { state: 'connected', protocol_version: 1 }; }
  },

  async setToken(token: string, host: string, port: number): Promise<void> {
    await invoke('hermes_set_token', { token, host, port });
  },

  async getTokenMetadata(): Promise<{ paired: boolean; host?: string; port?: number; token_suffix?: string }> {
    try { return await invoke('hermes_get_token_metadata'); }
    catch { return { paired: false }; }
  },

  async clearToken(): Promise<void> { await invoke('hermes_clear_token'); },

  async healthCheck(host: string, port: number): Promise<unknown> {
    return await invoke('hermes_health_check', { host, port });
  },
};
```

- [ ] **Step 3: Run tests**

```bash
bun run test src/services/hermesService.test.ts
```

Expected: 3 PASS.

- [ ] **Step 4: Commit**

```bash
git add src/services/hermesService.ts src/services/hermesService.test.ts
git commit -m "$(cat <<'EOF'
feat(agent-canvas): hermesService facade with mock fallback

Wraps Tauri invoke('hermes_call') and listen('hermes:event'); falls
back to mockHermes when no Tauri runtime is present (vitest, web
preview). RPC errors propagate as the contract HermesError shape.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task B6: `useHermesConnection` hook

Tracks connection state; exposes a "needs pairing" flag.

**Files:**
- Create: `src/hooks/useHermesConnection.ts`
- Create: `src/hooks/useHermesConnection.test.ts`

- [ ] **Step 1: Write the test**

Create `src/hooks/useHermesConnection.test.ts`:

```ts
import { describe, it, expect, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { useHermesConnection } from './useHermesConnection';
import { hermesService } from '@/services/hermesService';

describe('useHermesConnection', () => {
  it('reports paired:false when no token metadata', async () => {
    vi.spyOn(hermesService, 'getTokenMetadata').mockResolvedValue({ paired: false });
    const { result } = renderHook(() => useHermesConnection());
    await waitFor(() => expect(result.current.paired).toBe(false));
  });

  it('reports paired:true and host suffix when paired', async () => {
    vi.spyOn(hermesService, 'getTokenMetadata').mockResolvedValue({ paired: true, host: '100.1.2.3', port: 8645, token_suffix: 'cdef' });
    const { result } = renderHook(() => useHermesConnection());
    await waitFor(() => expect(result.current.paired).toBe(true));
    expect(result.current.metadata?.host).toBe('100.1.2.3');
  });
});
```

- [ ] **Step 2: Implement the hook**

Create `src/hooks/useHermesConnection.ts`:

```ts
import { useEffect, useState } from 'react';
import { hermesService } from '@/services/hermesService';
import type { ConnStatus } from '@/components/agent-canvas/types';

export interface HermesConnectionView {
  paired: boolean;
  status: ConnStatus;
  metadata: { paired: boolean; host?: string; port?: number; token_suffix?: string } | null;
  refresh: () => Promise<void>;
}

export function useHermesConnection(): HermesConnectionView {
  const [status, setStatus] = useState<ConnStatus>({ state: 'disconnected' });
  const [metadata, setMetadata] = useState<HermesConnectionView['metadata']>(null);

  const refresh = async () => {
    const [s, m] = await Promise.all([hermesService.getStatus(), hermesService.getTokenMetadata()]);
    setStatus(s);
    setMetadata(m);
  };

  useEffect(() => {
    refresh();
    let off: (() => void) | undefined;
    hermesService.onStatus(setStatus).then((u) => { off = () => u(); });
    return () => { off?.(); };
  }, []);

  return { paired: !!metadata?.paired, status, metadata, refresh };
}
```

- [ ] **Step 3: Run tests**

```bash
bun run test src/hooks/useHermesConnection.test.ts
```

Expected: 2 PASS.

- [ ] **Step 4: Commit**

```bash
git add src/hooks/useHermesConnection.ts src/hooks/useHermesConnection.test.ts
git commit -m "$(cat <<'EOF'
feat(agent-canvas): useHermesConnection hook

Reads token metadata + live ConnStatus; subscribes to hermes:status
events. Powers the first-run modal trigger and status indicator.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task B7: `useChatSession` and `useStreamingTurn` hooks

Wire the event stream into the store.

**Files:**
- Create: `src/hooks/useChatSession.ts`
- Create: `src/hooks/useChatSession.test.ts`
- Create: `src/hooks/useStreamingTurn.ts`
- Create: `src/hooks/useStreamingTurn.test.ts`

- [ ] **Step 1: Test the chat session hook**

Create `src/hooks/useChatSession.test.ts`:

```ts
import { describe, it, expect, vi } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { useChatSession } from './useChatSession';
import { hermesService } from '@/services/hermesService';
import { useAgentCanvasStore } from '@/stores/agentCanvasStore';

describe('useChatSession', () => {
  it('createSession sets activeSessionId', async () => {
    vi.spyOn(hermesService, 'call').mockImplementation(async (m: string) => {
      if (m === 'session.create') return { session_id: 'sess-x', info: {} };
      return {};
    });
    useAgentCanvasStore.getState().reset();
    const { result } = renderHook(() => useChatSession());
    await act(async () => { await result.current.createSession(); });
    await waitFor(() => expect(useAgentCanvasStore.getState().activeSessionId).toBe('sess-x'));
  });

  it('submit calls prompt.submit with active session id', async () => {
    const spy = vi.spyOn(hermesService, 'call').mockResolvedValue({ ok: true });
    useAgentCanvasStore.getState().reset();
    useAgentCanvasStore.getState().setActiveSession('sess-y');
    const { result } = renderHook(() => useChatSession());
    await act(async () => { await result.current.submit('hello'); });
    expect(spy).toHaveBeenCalledWith('prompt.submit', expect.objectContaining({ session_id: 'sess-y', text: 'hello' }));
  });
});
```

- [ ] **Step 2: Implement `useChatSession`**

Create `src/hooks/useChatSession.ts`:

```ts
import { useCallback } from 'react';
import { hermesService } from '@/services/hermesService';
import { useAgentCanvasStore } from '@/stores/agentCanvasStore';

export function useChatSession() {
  const setActive = useAgentCanvasStore((s) => s.setActiveSession);
  const append = useAgentCanvasStore((s) => s.appendUserMessage);
  const activeId = useAgentCanvasStore((s) => s.activeSessionId);

  const createSession = useCallback(async () => {
    const r = await hermesService.call<{ session_id: string }>('session.create', {});
    setActive(r.session_id);
    return r.session_id;
  }, [setActive]);

  const resumeSession = useCallback(async (sessionId: string) => {
    await hermesService.call('session.resume', { session_id: sessionId });
    setActive(sessionId);
  }, [setActive]);

  const closeSession = useCallback(async () => {
    if (!activeId) return;
    await hermesService.call('session.close', { session_id: activeId });
    setActive(null);
  }, [activeId, setActive]);

  const submit = useCallback(async (text: string) => {
    let sid = activeId;
    if (!sid) {
      const r = await hermesService.call<{ session_id: string }>('session.create', {});
      sid = r.session_id;
      setActive(sid);
    }
    append(text);
    await hermesService.call('prompt.submit', { session_id: sid, text });
  }, [activeId, append, setActive]);

  return { createSession, resumeSession, closeSession, submit };
}
```

- [ ] **Step 3: Test the streaming hook**

Create `src/hooks/useStreamingTurn.test.ts`:

```ts
import { describe, it, expect, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useStreamingTurn } from './useStreamingTurn';
import { hermesService } from '@/services/hermesService';
import { useAgentCanvasStore } from '@/stores/agentCanvasStore';
import { makeMessageDelta, makeMessageComplete, makeApprovalRequest, makeToolStart, makeToolComplete } from '../../tests/fixtures/hermes-events';

describe('useStreamingTurn', () => {
  it('appends assistant deltas into store', async () => {
    let injected: any;
    vi.spyOn(hermesService, 'onEvent').mockImplementation(async (h) => { injected = h; return async () => {}; });
    useAgentCanvasStore.getState().reset();
    useAgentCanvasStore.getState().setActiveSession('sess-1');
    renderHook(() => useStreamingTurn());
    injected(makeMessageDelta('hi ', 'sess-1'));
    injected(makeMessageDelta('there', 'sess-1'));
    injected(makeMessageComplete('sess-1'));
    const t = useAgentCanvasStore.getState().transcript;
    expect(t[0]).toMatchObject({ kind: 'message', role: 'assistant', text: 'hi there' });
    expect(useAgentCanvasStore.getState().pendingTurn).toBe(false);
  });

  it('queues approval requests', async () => {
    let injected: any;
    vi.spyOn(hermesService, 'onEvent').mockImplementation(async (h) => { injected = h; return async () => {}; });
    useAgentCanvasStore.getState().reset();
    useAgentCanvasStore.getState().setActiveSession('sess-1');
    renderHook(() => useStreamingTurn());
    injected(makeApprovalRequest('bash', { cmd: 'ls' }, 'ar-1', 'sess-1'));
    expect(useAgentCanvasStore.getState().approvalQueue).toHaveLength(1);
  });

  it('spawns side-card for long-running tool', async () => {
    let injected: any;
    vi.spyOn(hermesService, 'onEvent').mockImplementation(async (h) => { injected = h; return async () => {}; });
    useAgentCanvasStore.getState().reset();
    useAgentCanvasStore.getState().setActiveSession('sess-1');
    renderHook(() => useStreamingTurn());
    injected(makeToolStart('web_research', {}, 'tc-1', 'sess-1'));
    injected(makeToolComplete('tc-1', 'big payload here', 1500, 'ok', 'sess-1'));
    expect(useAgentCanvasStore.getState().sideCards).toHaveLength(1);
    expect(useAgentCanvasStore.getState().sideCards[0].kind).toBe('tool-progress');
  });
});
```

- [ ] **Step 4: Implement `useStreamingTurn`**

Create `src/hooks/useStreamingTurn.ts`:

```ts
import { useEffect } from 'react';
import { hermesService } from '@/services/hermesService';
import { useAgentCanvasStore } from '@/stores/agentCanvasStore';
import { shouldSpawnCard } from '@/utils/sideCardHeuristics';
import type { HermesEventEnvelope, ToolStartEvent, ToolProgressEvent, ToolCompleteEvent, ApprovalRequestEvent, MessageDeltaEvent } from '@/components/agent-canvas/types';

interface ToolTracker { name: string; toolCallId: string; sessionId: string; hasProgress: boolean; spawnedCard: boolean; sideCardId?: string }

export function useStreamingTurn() {
  useEffect(() => {
    const trackers = new Map<string, ToolTracker>();

    const handler = (env: HermesEventEnvelope) => {
      const type = env.params.type;
      const sid = env.params.session_id;
      const active = useAgentCanvasStore.getState().activeSessionId;
      if (sid && active && sid !== active) return;

      const store = useAgentCanvasStore.getState();
      const payload = env.params.payload as any;

      switch (type) {
        case 'message.delta':
          store.applyAssistantDelta((payload as MessageDeltaEvent).text);
          break;
        case 'message.complete':
          store.completeAssistantMessage();
          break;
        case 'tool.start': {
          const p = payload as ToolStartEvent;
          trackers.set(p.tool_call_id, { name: p.name, toolCallId: p.tool_call_id, sessionId: sid, hasProgress: false, spawnedCard: false });
          if (shouldSpawnCard({ name: p.name, input: p.input, hasProgressEvents: false, outputSize: 0 })) {
            const cardId = `sc-${p.tool_call_id}`;
            store.spawnSideCard({
              id: cardId, kind: 'tool-progress', title: p.name, toolCallId: p.tool_call_id,
              content: '', pinned: false, status: 'running', createdAt: Date.now(),
            });
            const t = trackers.get(p.tool_call_id)!;
            t.spawnedCard = true; t.sideCardId = cardId;
          } else {
            store.appendToolInline(p.name, '', p.tool_call_id);
          }
          break;
        }
        case 'tool.progress': {
          const p = payload as ToolProgressEvent;
          const t = trackers.get(p.tool_call_id);
          if (!t) break;
          t.hasProgress = true;
          if (t.spawnedCard) {
            store.updateSideCardByToolCall(p.tool_call_id, { content: p.preview });
          } else {
            // Promote inline → side-card now that progress arrived.
            const cardId = `sc-${p.tool_call_id}`;
            store.spawnSideCard({
              id: cardId, kind: 'tool-progress', title: t.name, toolCallId: p.tool_call_id,
              content: p.preview, pinned: false, status: 'running', createdAt: Date.now(),
            });
            t.spawnedCard = true; t.sideCardId = cardId;
          }
          break;
        }
        case 'tool.complete': {
          const p = payload as ToolCompleteEvent;
          const t = trackers.get(p.tool_call_id);
          const outputSize = p.output?.length ?? 0;
          const shouldCard = t?.spawnedCard
            || shouldSpawnCard({ name: t?.name ?? '', input: {}, hasProgressEvents: !!t?.hasProgress, outputSize });
          if (shouldCard) {
            if (!t?.spawnedCard) {
              const cardId = `sc-${p.tool_call_id}`;
              store.spawnSideCard({
                id: cardId, kind: outputSize > 1000 ? 'artifact' : 'tool-progress',
                title: t?.name ?? 'tool', toolCallId: p.tool_call_id,
                content: p.output, pinned: false, status: p.status === 'error' ? 'error' : 'complete', createdAt: Date.now(), completedAt: Date.now(),
              });
            } else {
              store.updateSideCardByToolCall(p.tool_call_id, {
                content: p.output, status: p.status === 'error' ? 'error' : 'complete', completedAt: Date.now(),
                kind: outputSize > 1000 ? 'artifact' : 'tool-progress',
              });
            }
          } else {
            store.updateToolInline(p.tool_call_id, p.output, p.status === 'error' ? 'error' : 'complete');
          }
          trackers.delete(p.tool_call_id);
          break;
        }
        case 'approval.request': {
          const p = payload as ApprovalRequestEvent;
          store.queueApproval({ requestId: p.request_id, tool: p.tool, args: p.args, sessionId: sid });
          break;
        }
        default:
          break;
      }
    };

    let off: (() => void) | undefined;
    hermesService.onEvent(handler).then((u) => { off = () => u(); });
    return () => { off?.(); };
  }, []);
}
```

- [ ] **Step 5: Run tests**

```bash
bun run test src/hooks/useChatSession.test.ts src/hooks/useStreamingTurn.test.ts
```

Expected: 5 PASS.

- [ ] **Step 6: Commit**

```bash
git add src/hooks/useChatSession.ts src/hooks/useChatSession.test.ts src/hooks/useStreamingTurn.ts src/hooks/useStreamingTurn.test.ts
git commit -m "$(cat <<'EOF'
feat(agent-canvas): chat session + streaming turn hooks

useChatSession owns create/resume/close/submit. useStreamingTurn is
the event router: tracks per-tool state, applies the side-card
heuristic, populates the store with deltas, tool spans, and approvals.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task B8: `useSlashCompletion` hook

TanStack Query wrapper over `complete.slash` (debounced).

**Files:**
- Create: `src/hooks/useSlashCompletion.ts`
- Create: `src/hooks/useSlashCompletion.test.ts`

- [ ] **Step 1: Write the test**

Create `src/hooks/useSlashCompletion.test.ts`:

```ts
import { describe, it, expect, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';
import { useSlashCompletion } from './useSlashCompletion';
import { hermesService } from '@/services/hermesService';

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: qc }, children);
}

describe('useSlashCompletion', () => {
  it('returns suggestions for a prefix', async () => {
    vi.spyOn(hermesService, 'call').mockResolvedValue({ suggestions: ['/personality','/sessions'] });
    const { result } = renderHook(() => useSlashCompletion('/p'), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.suggestions.length).toBeGreaterThan(0));
    expect(result.current.suggestions).toContain('/personality');
  });
  it('returns empty when prefix is empty', () => {
    const { result } = renderHook(() => useSlashCompletion(''), { wrapper: wrapper() });
    expect(result.current.suggestions).toEqual([]);
  });
});
```

- [ ] **Step 2: Implement the hook**

Create `src/hooks/useSlashCompletion.ts`:

```ts
import { useQuery } from '@tanstack/react-query';
import { hermesService } from '@/services/hermesService';

export function useSlashCompletion(prefix: string) {
  const enabled = prefix.length > 0;
  const q = useQuery({
    queryKey: ['hermes', 'complete.slash', prefix],
    queryFn: async () => {
      const r = await hermesService.call<{ suggestions: string[] }>('complete.slash', { prefix });
      return r.suggestions;
    },
    enabled,
    staleTime: 30_000,
  });
  return { suggestions: q.data ?? [], isLoading: q.isLoading };
}
```

- [ ] **Step 3: Run tests + commit**

```bash
bun run test src/hooks/useSlashCompletion.test.ts
git add src/hooks/useSlashCompletion.ts src/hooks/useSlashCompletion.test.ts
git commit -m "feat(agent-canvas): useSlashCompletion via TanStack Query

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

Expected: 2 PASS.

---

### Task B9: `useHermesNotifications` hook

Wires window-focus state to the Rust dispatcher (via a Tauri command — added inline here).

**Files:**
- Create: `src/hooks/useHermesNotifications.ts`
- Create: `src/hooks/useHermesNotifications.test.ts`
- Modify: `src-tauri/src/commands/hermes.rs` (add `hermes_set_focused`)

- [ ] **Step 1: Add the Rust command**

Edit `src-tauri/src/commands/hermes.rs`. Add:

```rust
use std::sync::Arc;

#[tauri::command]
pub async fn hermes_set_focused(
    notif: State<'_, Arc<crate::hermes::notification::NotificationDispatcher>>,
    focused: bool,
) -> Result<(), String> {
    notif.set_focused(focused);
    Ok(())
}
```

Edit `src-tauri/src/lib.rs` setup:

```rust
let dispatcher = std::sync::Arc::new(crate::hermes::notification::NotificationDispatcher::new());
app.manage(dispatcher.clone());

let evt_rx2 = client.subscribe_events();
let d2 = dispatcher.clone();
let app_handle3 = app.handle().clone();
tokio::spawn(async move {
    let mut rx = evt_rx2;
    while let Ok(env) = rx.recv().await {
        let event_type = env.pointer("/params/type").and_then(|v| v.as_str()).unwrap_or("").to_string();
        if d2.should_notify(&event_type) {
            // Phase C wires tauri-plugin-notification here.
            let _ = app_handle3.emit("hermes:notify", &env);
        }
    }
});
```

Add `hermes_set_focused` to `tauri::generate_handler![...]` list.

- [ ] **Step 2: Write the TS test**

Create `src/hooks/useHermesNotifications.test.ts`:

```ts
import { describe, it, expect, vi } from 'vitest';
vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn() }));
import { invoke } from '@tauri-apps/api/core';
import { renderHook } from '@testing-library/react';
import { useHermesNotifications } from './useHermesNotifications';

describe('useHermesNotifications', () => {
  it('reports focus to Rust on mount', () => {
    Object.defineProperty(document, 'hasFocus', { value: () => true, configurable: true });
    renderHook(() => useHermesNotifications());
    expect(invoke).toHaveBeenCalledWith('hermes_set_focused', { focused: true });
  });
});
```

- [ ] **Step 3: Implement the hook**

Create `src/hooks/useHermesNotifications.ts`:

```ts
import { useEffect } from 'react';
import { invoke } from '@tauri-apps/api/core';

export function useHermesNotifications() {
  useEffect(() => {
    const report = (focused: boolean) => { invoke('hermes_set_focused', { focused }).catch(() => {}); };
    report(typeof document !== 'undefined' && document.hasFocus());
    const onFocus = () => report(true);
    const onBlur = () => report(false);
    window.addEventListener('focus', onFocus);
    window.addEventListener('blur', onBlur);
    return () => {
      window.removeEventListener('focus', onFocus);
      window.removeEventListener('blur', onBlur);
    };
  }, []);
}
```

- [ ] **Step 4: Run tests + commit**

```bash
bun run test src/hooks/useHermesNotifications.test.ts
cd src-tauri && cargo test hermes && cd ..
git add src/hooks/useHermesNotifications.ts src/hooks/useHermesNotifications.test.ts src-tauri/src/commands/hermes.rs src-tauri/src/lib.rs
git commit -m "$(cat <<'EOF'
feat(agent-canvas): focus reporting + notification routing

Window focus is reported to the Rust NotificationDispatcher via
hermes_set_focused. The dispatcher decides whether each event
deserves an OS notification; emits hermes:notify when so.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: TS + Rust tests pass.

---

**Phase B complete.** Acceptance check:

```bash
bun run test src/services src/hooks src/stores src/utils
bun run typecheck
cd src-tauri && cargo test hermes
```

Expected: every test passes; no TS errors. Set `HERMES_USE_MOCK=1` (later: a runtime flag) to verify mock fallback works in `bun run dev`.

---

## Phase C — Canvas shell, transcript, bottom bar, approvals

**Goal at end of Phase C:** Open the canvas with chord `AD` → first-run modal → paste token → ask "explain X" → see streaming response. Approve a tool call. Slice the chat → session ends. UI is functional against a real Mac mini.

### Task C1: `HermesStatusIndicator`

Small dot showing connection state in the canvas chrome.

**Files:**
- Create: `src/components/agent-canvas/connection/HermesStatusIndicator.tsx`
- Create: `src/components/agent-canvas/connection/HermesStatusIndicator.test.tsx`

- [ ] **Step 1: Write the test**

```ts
// HermesStatusIndicator.test.tsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { HermesStatusIndicator } from './HermesStatusIndicator';
import { hermesService } from '@/services/hermesService';

vi.spyOn(hermesService, 'getStatus').mockResolvedValue({ state: 'connected' as const, protocol_version: 1 });
vi.spyOn(hermesService, 'getTokenMetadata').mockResolvedValue({ paired: true });

describe('HermesStatusIndicator', () => {
  it('renders the connection state label', async () => {
    render(<HermesStatusIndicator />);
    expect(await screen.findByText(/connected/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Implement**

```tsx
// HermesStatusIndicator.tsx
import { useHermesConnection } from '@/hooks/useHermesConnection';

const COLORS: Record<string, string> = {
  connected: '#4ade80', connecting: '#fbbf24', reconnecting: '#fbbf24',
  disconnected: 'rgba(255,255,255,0.3)', error: '#ef4444',
};

export function HermesStatusIndicator() {
  const { status, metadata } = useHermesConnection();
  const color = COLORS[status.state] ?? '#999';
  return (
    <div role="status" aria-live="polite" style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 11, color: 'rgba(255,255,255,0.6)' }}>
      <span style={{ width: 8, height: 8, borderRadius: '50%', backgroundColor: color, boxShadow: `0 0 6px ${color}` }} />
      <span>{status.state}</span>
      {metadata?.host && <span style={{ opacity: 0.5 }}>· {metadata.host}</span>}
    </div>
  );
}
```

- [ ] **Step 3: Run tests + commit**

```bash
bun run test src/components/agent-canvas/connection/HermesStatusIndicator.test.tsx
git add src/components/agent-canvas/connection/HermesStatusIndicator.tsx src/components/agent-canvas/connection/HermesStatusIndicator.test.tsx
git commit -m "feat(agent-canvas): HermesStatusIndicator

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task C2: `HermesConnectModal` (first-run pairing)

**Files:**
- Create: `src/components/agent-canvas/connection/HermesConnectModal.tsx`
- Create: `src/components/agent-canvas/connection/HermesConnectModal.test.tsx`

- [ ] **Step 1: Write the test**

```tsx
// HermesConnectModal.test.tsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { HermesConnectModal } from './HermesConnectModal';
import { hermesService } from '@/services/hermesService';

describe('HermesConnectModal', () => {
  it('rejects tokens shorter than 32 chars', async () => {
    render(<HermesConnectModal onClose={() => {}} />);
    fireEvent.change(screen.getByLabelText(/token/i), { target: { value: 'short' } });
    fireEvent.click(screen.getByRole('button', { name: /connect/i }));
    expect(await screen.findByText(/at least 32 characters/i)).toBeInTheDocument();
  });

  it('calls hermesService.setToken on valid input', async () => {
    const spy = vi.spyOn(hermesService, 'setToken').mockResolvedValue();
    const onClose = vi.fn();
    render(<HermesConnectModal onClose={onClose} />);
    fireEvent.change(screen.getByLabelText(/token/i), { target: { value: 'a'.repeat(40) } });
    fireEvent.change(screen.getByLabelText(/host/i), { target: { value: '100.1.2.3' } });
    fireEvent.change(screen.getByLabelText(/port/i), { target: { value: '8645' } });
    fireEvent.click(screen.getByRole('button', { name: /connect/i }));
    await waitFor(() => expect(spy).toHaveBeenCalledWith('a'.repeat(40), '100.1.2.3', 8645));
    expect(onClose).toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Implement**

```tsx
// HermesConnectModal.tsx
import { useState } from 'react';
import { hermesService } from '@/services/hermesService';

interface Props { onClose: () => void }

export function HermesConnectModal({ onClose }: Props) {
  const [token, setToken] = useState('');
  const [host, setHost] = useState('');
  const [port, setPort] = useState('8645');
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setErr(null);
    if (token.length < 32) { setErr('Token must be at least 32 characters'); return; }
    if (!host.trim()) { setErr('Host is required'); return; }
    const p = parseInt(port, 10);
    if (!Number.isFinite(p) || p <= 0 || p > 65535) { setErr('Port must be 1–65535'); return; }
    setBusy(true);
    try {
      await hermesService.setToken(token, host.trim(), p);
      onClose();
    } catch (e: any) {
      setErr(typeof e === 'string' ? e : (e?.message ?? 'Pairing failed'));
    } finally { setBusy(false); }
  };

  return (
    <div role="dialog" aria-modal="true" aria-labelledby="hermes-pair-title" style={{ position: 'fixed', inset: 0, backgroundColor: 'rgba(0,0,0,0.7)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 9999 }}>
      <div style={{ width: 480, padding: 24, backgroundColor: '#111', borderRadius: 12, border: '1px solid rgba(255,255,255,0.08)' }}>
        <h2 id="hermes-pair-title" style={{ margin: 0, fontSize: 16, fontWeight: 600 }}>Pair with Hermes</h2>
        <p style={{ fontSize: 13, color: 'rgba(255,255,255,0.6)', lineHeight: 1.5 }}>
          On the Mac mini, run:<br/>
          <code style={{ display: 'block', padding: 8, marginTop: 8, backgroundColor: 'rgba(255,255,255,0.04)', borderRadius: 4 }}>
            hermes desktop pair --client-name tony-windows-laptop
          </code>
          Copy the printed token here.
        </p>
        <label style={{ display: 'block', fontSize: 12, marginTop: 12 }}>
          Token
          <input aria-label="token" value={token} onChange={(e) => setToken(e.target.value)} style={{ width: '100%', marginTop: 4, padding: 8, backgroundColor: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: 4, color: '#fff' }} />
        </label>
        <label style={{ display: 'block', fontSize: 12, marginTop: 12 }}>
          Host (Tailscale IP or hostname)
          <input aria-label="host" value={host} onChange={(e) => setHost(e.target.value)} style={{ width: '100%', marginTop: 4, padding: 8, backgroundColor: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: 4, color: '#fff' }} />
        </label>
        <label style={{ display: 'block', fontSize: 12, marginTop: 12 }}>
          Port
          <input aria-label="port" value={port} onChange={(e) => setPort(e.target.value)} style={{ width: 100, marginTop: 4, padding: 8, backgroundColor: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: 4, color: '#fff' }} />
        </label>
        {err && <div role="alert" style={{ marginTop: 12, color: '#ef4444', fontSize: 12 }}>{err}</div>}
        <div style={{ marginTop: 20, display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <button type="button" onClick={onClose} disabled={busy}>Cancel</button>
          <button type="button" onClick={submit} disabled={busy}>Connect</button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Run tests + commit**

```bash
bun run test src/components/agent-canvas/connection/HermesConnectModal.test.tsx
git add src/components/agent-canvas/connection/HermesConnectModal.tsx src/components/agent-canvas/connection/HermesConnectModal.test.tsx
git commit -m "feat(agent-canvas): HermesConnectModal first-run pairing

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task C3: `HermesPane` (Preferences)

**Files:**
- Create: `src/components/preferences/panes/HermesPane.tsx`
- Create: `src/components/preferences/panes/HermesPane.test.tsx`
- Modify: `src/components/preferences/PreferencesDialog.tsx` (register the pane)
- Modify: `src/components/preferences/FullScreenPreferencesDialog.tsx` (register the pane)

- [ ] **Step 1: Write the test**

```tsx
// HermesPane.test.tsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { HermesPane } from './HermesPane';
import { hermesService } from '@/services/hermesService';

describe('HermesPane', () => {
  it('shows paired host suffix when paired', async () => {
    vi.spyOn(hermesService, 'getTokenMetadata').mockResolvedValue({ paired: true, host: '100.1.2.3', port: 8645, token_suffix: 'cdef' });
    vi.spyOn(hermesService, 'getStatus').mockResolvedValue({ state: 'connected', protocol_version: 1 });
    render(<HermesPane />);
    expect(await screen.findByText(/100\.1\.2\.3:8645/)).toBeInTheDocument();
    expect(await screen.findByText(/…cdef/)).toBeInTheDocument();
  });

  it('clears the token via revoke button', async () => {
    vi.spyOn(hermesService, 'getTokenMetadata').mockResolvedValue({ paired: true, host: '1.1.1.1', port: 8645, token_suffix: '0000' });
    vi.spyOn(hermesService, 'getStatus').mockResolvedValue({ state: 'connected', protocol_version: 1 });
    const clearSpy = vi.spyOn(hermesService, 'clearToken').mockResolvedValue();
    render(<HermesPane />);
    fireEvent.click(await screen.findByRole('button', { name: /revoke locally/i }));
    await waitFor(() => expect(clearSpy).toHaveBeenCalled());
  });
});
```

- [ ] **Step 2: Implement**

```tsx
// HermesPane.tsx
import { useState } from 'react';
import { useHermesConnection } from '@/hooks/useHermesConnection';
import { hermesService } from '@/services/hermesService';
import { HermesConnectModal } from '@/components/agent-canvas/connection/HermesConnectModal';

export function HermesPane() {
  const conn = useHermesConnection();
  const [showModal, setShowModal] = useState(false);
  const [healthResult, setHealthResult] = useState<unknown>(null);

  const handleHealth = async () => {
    if (!conn.metadata?.host || !conn.metadata?.port) return;
    try { setHealthResult(await hermesService.healthCheck(conn.metadata.host, conn.metadata.port)); }
    catch (e) { setHealthResult({ error: String(e) }); }
  };

  const handleRevoke = async () => {
    await hermesService.clearToken();
    await conn.refresh();
  };

  return (
    <div style={{ padding: 16 }}>
      <h2 style={{ fontSize: 14, marginBottom: 12 }}>Hermes connection</h2>
      <div style={{ fontSize: 12, color: 'rgba(255,255,255,0.7)' }}>
        Status: <strong>{conn.status.state}</strong>
      </div>
      {conn.metadata?.paired ? (
        <div style={{ marginTop: 8, fontSize: 12 }}>
          <div>Paired host: <code>{conn.metadata.host}:{conn.metadata.port}</code></div>
          <div>Token suffix: <code>…{conn.metadata.token_suffix}</code></div>
        </div>
      ) : (
        <div style={{ marginTop: 8, fontSize: 12 }}>Not paired.</div>
      )}
      <div style={{ marginTop: 16, display: 'flex', gap: 8 }}>
        <button type="button" onClick={() => setShowModal(true)}>Re-pair</button>
        <button type="button" onClick={handleRevoke} disabled={!conn.metadata?.paired}>Revoke locally</button>
        <button type="button" onClick={handleHealth} disabled={!conn.metadata?.host}>Health check</button>
      </div>
      {healthResult !== null && (
        <pre style={{ marginTop: 12, padding: 8, fontSize: 11, background: 'rgba(255,255,255,0.04)', borderRadius: 4, overflow: 'auto' }}>
          {JSON.stringify(healthResult, null, 2)}
        </pre>
      )}
      <p style={{ fontSize: 11, color: 'rgba(255,255,255,0.4)', marginTop: 16 }}>
        To revoke server-side, run on the Mac mini: <code>hermes desktop revoke &lt;client-name&gt;</code>
      </p>
      {showModal && <HermesConnectModal onClose={() => { setShowModal(false); conn.refresh(); }} />}
    </div>
  );
}
```

- [ ] **Step 3: Register in both Preferences dialogs**

Edit `src/components/preferences/PreferencesDialog.tsx`. Find the pane registry and add:

```tsx
import { HermesPane } from './panes/HermesPane';

// In the panes array / map:
{ id: 'hermes', label: 'Hermes', component: HermesPane },
```

Repeat the same in `FullScreenPreferencesDialog.tsx`.

- [ ] **Step 4: Run tests + commit**

```bash
bun run test src/components/preferences/panes/HermesPane.test.tsx
bun run typecheck
git add src/components/preferences/panes/HermesPane.tsx src/components/preferences/panes/HermesPane.test.tsx src/components/preferences/PreferencesDialog.tsx src/components/preferences/FullScreenPreferencesDialog.tsx
git commit -m "feat(agent-canvas): HermesPane in preferences

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task C4: `SlashCompletionMenu`

Dropdown anchored to the bottom command bar.

**Files:**
- Create: `src/components/agent-canvas/slash/SlashCompletionMenu.tsx`
- Create: `src/components/agent-canvas/slash/SlashCompletionMenu.test.tsx`

- [ ] **Step 1: Write the test**

```tsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';
import { SlashCompletionMenu } from './SlashCompletionMenu';
import { hermesService } from '@/services/hermesService';

const wrap = (n: React.ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{n}</QueryClientProvider>;
};

describe('SlashCompletionMenu', () => {
  it('renders suggestions and selects on click', async () => {
    vi.spyOn(hermesService, 'call').mockResolvedValue({ suggestions: ['/personality', '/sessions'] });
    const onPick = vi.fn();
    render(wrap(<SlashCompletionMenu prefix="/p" onPick={onPick} />));
    const item = await screen.findByText('/personality');
    fireEvent.click(item);
    expect(onPick).toHaveBeenCalledWith('/personality');
  });
});
```

- [ ] **Step 2: Implement**

```tsx
// SlashCompletionMenu.tsx
import { useSlashCompletion } from '@/hooks/useSlashCompletion';

interface Props { prefix: string; onPick: (suggestion: string) => void }

export function SlashCompletionMenu({ prefix, onPick }: Props) {
  const { suggestions } = useSlashCompletion(prefix);
  if (suggestions.length === 0) return null;
  return (
    <div role="listbox" style={{ position: 'absolute', bottom: '100%', left: 0, marginBottom: 4, padding: 4, backgroundColor: '#1a1a1a', border: '1px solid rgba(255,255,255,0.08)', borderRadius: 6, minWidth: 240, zIndex: 50 }}>
      {suggestions.map((s) => (
        <button key={s} role="option" type="button" onClick={() => onPick(s)} style={{ display: 'block', width: '100%', textAlign: 'left', padding: '6px 10px', background: 'transparent', border: 'none', color: '#fcfcfc', cursor: 'pointer', fontSize: 12 }}>
          {s}
        </button>
      ))}
    </div>
  );
}
```

- [ ] **Step 3: Run tests + commit**

```bash
bun run test src/components/agent-canvas/slash/SlashCompletionMenu.test.tsx
git add src/components/agent-canvas/slash/SlashCompletionMenu.tsx src/components/agent-canvas/slash/SlashCompletionMenu.test.tsx
git commit -m "feat(agent-canvas): SlashCompletionMenu

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task C5: `ModelPickerDropdown`

**Files:**
- Create: `src/components/agent-canvas/slash/ModelPickerDropdown.tsx`
- Create: `src/components/agent-canvas/slash/ModelPickerDropdown.test.tsx`

- [ ] **Step 1: Test**

```tsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';
import { ModelPickerDropdown } from './ModelPickerDropdown';
import { hermesService } from '@/services/hermesService';

const wrap = (n: React.ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{n}</QueryClientProvider>;
};

describe('ModelPickerDropdown', () => {
  it('selects a model and calls config.set', async () => {
    vi.spyOn(hermesService, 'call').mockImplementation(async (m: string) => {
      if (m === 'model.options') return { models: [{ id: 'alex', label: 'Alex' }, { id: 'echo', label: 'Echo' }] };
      return {};
    });
    const onClose = vi.fn();
    render(wrap(<ModelPickerDropdown onClose={onClose} />));
    fireEvent.click(await screen.findByText('Echo'));
    await waitFor(() => expect(hermesService.call).toHaveBeenCalledWith('config.set', { key: 'model', value: 'echo' }));
  });
});
```

- [ ] **Step 2: Implement**

```tsx
// ModelPickerDropdown.tsx
import { useQuery } from '@tanstack/react-query';
import { hermesService } from '@/services/hermesService';

interface Model { id: string; label: string; description?: string }
interface Props { onClose: () => void }

export function ModelPickerDropdown({ onClose }: Props) {
  const q = useQuery({
    queryKey: ['hermes', 'model.options'],
    queryFn: async () => (await hermesService.call<{ models: Model[] }>('model.options', {})).models,
    staleTime: 60_000,
  });

  const pick = async (id: string) => {
    await hermesService.call('config.set', { key: 'model', value: id });
    onClose();
  };

  return (
    <div role="listbox" style={{ position: 'absolute', bottom: '100%', right: 0, marginBottom: 4, padding: 4, backgroundColor: '#1a1a1a', border: '1px solid rgba(255,255,255,0.08)', borderRadius: 6, minWidth: 220, zIndex: 50 }}>
      {(q.data ?? []).map((m) => (
        <button key={m.id} type="button" role="option" onClick={() => pick(m.id)} style={{ display: 'block', width: '100%', textAlign: 'left', padding: '8px 10px', background: 'transparent', border: 'none', color: '#fcfcfc', cursor: 'pointer', fontSize: 12 }}>
          <div style={{ fontWeight: 500 }}>{m.label}</div>
          {m.description && <div style={{ fontSize: 10, color: 'rgba(255,255,255,0.4)' }}>{m.description}</div>}
        </button>
      ))}
    </div>
  );
}
```

- [ ] **Step 3: Run tests + commit**

```bash
bun run test src/components/agent-canvas/slash/ModelPickerDropdown.test.tsx
git add src/components/agent-canvas/slash/ModelPickerDropdown.tsx src/components/agent-canvas/slash/ModelPickerDropdown.test.tsx
git commit -m "feat(agent-canvas): ModelPickerDropdown over model.options

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task C6: `BottomCommandBar`

The single input.

**Files:**
- Create: `src/components/agent-canvas/BottomCommandBar.tsx`
- Create: `src/components/agent-canvas/BottomCommandBar.test.tsx`

- [ ] **Step 1: Test**

```tsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';
import { BottomCommandBar } from './BottomCommandBar';
import { hermesService } from '@/services/hermesService';
import { useAgentCanvasStore } from '@/stores/agentCanvasStore';

const wrap = (n: React.ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{n}</QueryClientProvider>;
};

describe('BottomCommandBar', () => {
  it('submits text via prompt.submit', async () => {
    const spy = vi.spyOn(hermesService, 'call').mockImplementation(async (m: string) => {
      if (m === 'session.create') return { session_id: 'sess-z' };
      return { ok: true };
    });
    useAgentCanvasStore.getState().reset();
    render(wrap(<BottomCommandBar />));
    const input = screen.getByPlaceholderText(/type \/ for commands/i);
    fireEvent.change(input, { target: { value: 'hello' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(spy).toHaveBeenCalledWith('prompt.submit', expect.objectContaining({ text: 'hello' })));
  });

  it('routes /commands through slash.exec', async () => {
    const spy = vi.spyOn(hermesService, 'call').mockResolvedValue({ ok: true });
    useAgentCanvasStore.getState().reset();
    useAgentCanvasStore.getState().setActiveSession('sess-1');
    render(wrap(<BottomCommandBar />));
    const input = screen.getByPlaceholderText(/type \/ for commands/i);
    fireEvent.change(input, { target: { value: '/personality echo' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(spy).toHaveBeenCalledWith('slash.exec', { session_id: 'sess-1', command: '/personality echo' }));
  });
});
```

- [ ] **Step 2: Implement**

```tsx
// BottomCommandBar.tsx
import { useState, useRef } from 'react';
import { useChatSession } from '@/hooks/useChatSession';
import { useAgentCanvasStore } from '@/stores/agentCanvasStore';
import { hermesService } from '@/services/hermesService';
import { SlashCompletionMenu } from './slash/SlashCompletionMenu';
import { ModelPickerDropdown } from './slash/ModelPickerDropdown';

export function BottomCommandBar() {
  const [text, setText] = useState('');
  const [showModel, setShowModel] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);
  const { submit } = useChatSession();
  const activeId = useAgentCanvasStore((s) => s.activeSessionId);
  const pendingApprovals = useAgentCanvasStore((s) => s.approvalQueue.length);

  const send = async () => {
    const t = text.trim();
    if (!t) return;
    setText('');
    if (t.startsWith('/')) {
      // Ensure session exists for slash routing.
      let sid = activeId;
      if (!sid) {
        const r = await hermesService.call<{ session_id: string }>('session.create', {});
        sid = r.session_id;
        useAgentCanvasStore.getState().setActiveSession(sid);
      }
      await hermesService.call('slash.exec', { session_id: sid, command: t });
    } else {
      await submit(t);
    }
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  };

  const showSlashMenu = text.startsWith('/') && !text.includes(' ');

  return (
    <div style={{ position: 'sticky', bottom: 0, padding: 12, borderTop: '1px solid rgba(255,255,255,0.06)', backgroundColor: 'rgba(0,0,0,0.5)', backdropFilter: 'blur(8px)' }}>
      <div style={{ position: 'relative', display: 'flex', gap: 8, alignItems: 'flex-end' }}>
        <textarea
          ref={ref}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Type / for commands"
          aria-label="Hermes command input"
          rows={1}
          style={{ flex: 1, padding: 10, background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: 8, color: '#fcfcfc', resize: 'none', maxHeight: 200, fontSize: 13 }}
        />
        <button type="button" onClick={send} style={{ padding: '8px 14px', borderRadius: 8, background: '#ff4d00', color: '#fff', border: 'none' }}>↑</button>
        {showSlashMenu && <SlashCompletionMenu prefix={text} onPick={(s) => setText(s + ' ')} />}
      </div>
      <div style={{ marginTop: 8, display: 'flex', alignItems: 'center', gap: 12, fontSize: 11, color: 'rgba(255,255,255,0.5)' }}>
        <span>Ask permissions {pendingApprovals > 0 && <strong style={{ color: '#fbbf24' }}>({pendingApprovals})</strong>}</span>
        <button type="button" disabled aria-label="attachments" title="Attachments (coming soon)">+</button>
        <button type="button" disabled aria-label="microphone" title="Voice (coming soon)">🎙</button>
        <div style={{ marginLeft: 'auto', position: 'relative' }}>
          <button type="button" onClick={() => setShowModel((p) => !p)}>Model ▾</button>
          {showModel && <ModelPickerDropdown onClose={() => setShowModel(false)} />}
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Run tests + commit**

```bash
bun run test src/components/agent-canvas/BottomCommandBar.test.tsx
git add src/components/agent-canvas/BottomCommandBar.tsx src/components/agent-canvas/BottomCommandBar.test.tsx
git commit -m "feat(agent-canvas): BottomCommandBar single-input

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task C7: `InlineToolBlock` and `InlineApprovalCard`

Two transcript-bubble flavors.

**Files:**
- Create: `src/components/agent-canvas/transcript/InlineToolBlock.tsx`
- Create: `src/components/agent-canvas/transcript/InlineToolBlock.test.tsx`
- Create: `src/components/agent-canvas/transcript/InlineApprovalCard.tsx`
- Create: `src/components/agent-canvas/transcript/InlineApprovalCard.test.tsx`

- [ ] **Step 1: Tool block test + impl**

```tsx
// InlineToolBlock.test.tsx
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { InlineToolBlock } from './InlineToolBlock';

describe('InlineToolBlock', () => {
  it('renders tool name and preview, badges status', () => {
    render(<InlineToolBlock name="read_file" preview="ok" status="complete" />);
    expect(screen.getByText('read_file')).toBeInTheDocument();
    expect(screen.getByText(/complete/i)).toBeInTheDocument();
  });
});
```

```tsx
// InlineToolBlock.tsx
interface Props { name: string; preview: string; status: 'running'|'complete'|'error' }

const STATUS_COLOR: Record<Props['status'], string> = {
  running: '#fbbf24', complete: '#4ade80', error: '#ef4444',
};

export function InlineToolBlock({ name, preview, status }: Props) {
  return (
    <div role="article" aria-label={`Tool ${name}`} style={{ padding: 8, borderRadius: 6, background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.06)', fontSize: 12, fontFamily: 'monospace' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ width: 6, height: 6, borderRadius: '50%', background: STATUS_COLOR[status] }} />
        <strong>{name}</strong>
        <span style={{ fontSize: 10, color: 'rgba(255,255,255,0.4)' }}>{status}</span>
      </div>
      {preview && <div style={{ marginTop: 6, color: 'rgba(255,255,255,0.6)', whiteSpace: 'pre-wrap' }}>{preview}</div>}
    </div>
  );
}
```

- [ ] **Step 2: Approval card test + impl**

```tsx
// InlineApprovalCard.test.tsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { InlineApprovalCard } from './InlineApprovalCard';
import { hermesService } from '@/services/hermesService';
import { useAgentCanvasStore } from '@/stores/agentCanvasStore';

describe('InlineApprovalCard', () => {
  it('calls approval.respond and resolves the queue on click', async () => {
    const spy = vi.spyOn(hermesService, 'call').mockResolvedValue({ ok: true });
    useAgentCanvasStore.getState().reset();
    useAgentCanvasStore.getState().queueApproval({ requestId: 'ar-1', tool: 'bash', args: { cmd: 'ls' }, sessionId: 'sess-1' });
    render(<InlineApprovalCard requestId="ar-1" tool="bash" args={{ cmd: 'ls' }} decision="pending" />);
    fireEvent.click(screen.getByRole('button', { name: /allow once/i }));
    expect(spy).toHaveBeenCalledWith('approval.respond', { request_id: 'ar-1', decision: 'allow-once' });
  });
});
```

```tsx
// InlineApprovalCard.tsx
import { hermesService } from '@/services/hermesService';
import { useAgentCanvasStore } from '@/stores/agentCanvasStore';

interface Props { requestId: string; tool: string; args: object; decision: 'pending'|'allow-once'|'always'|'deny' }

export function InlineApprovalCard({ requestId, tool, args, decision }: Props) {
  const respond = async (d: 'allow-once'|'always'|'deny') => {
    await hermesService.call('approval.respond', { request_id: requestId, decision: d });
    useAgentCanvasStore.getState().resolveApproval(requestId, d);
  };
  return (
    <div role="article" aria-label="Approval request" style={{ padding: 12, borderRadius: 8, background: 'rgba(251,191,36,0.05)', border: '1px solid rgba(251,191,36,0.3)', fontSize: 13 }}>
      <div style={{ fontWeight: 600 }}>Tool wants to run: {tool}</div>
      <pre style={{ marginTop: 6, fontSize: 11, color: 'rgba(255,255,255,0.5)', whiteSpace: 'pre-wrap' }}>{JSON.stringify(args, null, 2)}</pre>
      {decision === 'pending' ? (
        <div style={{ marginTop: 8, display: 'flex', gap: 8 }}>
          <button type="button" onClick={() => respond('allow-once')}>Allow once</button>
          <button type="button" onClick={() => respond('always')}>Always</button>
          <button type="button" onClick={() => respond('deny')}>Deny</button>
        </div>
      ) : (
        <div style={{ marginTop: 8, fontSize: 11, color: 'rgba(255,255,255,0.5)' }}>Decision: {decision}</div>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Run tests + commit**

```bash
bun run test src/components/agent-canvas/transcript
git add src/components/agent-canvas/transcript/
git commit -m "feat(agent-canvas): inline tool block + approval card

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task C8: `TranscriptStream`

Renders transcript entries in order, auto-scrolls.

**Files:**
- Create: `src/components/agent-canvas/transcript/TranscriptStream.tsx`
- Create: `src/components/agent-canvas/transcript/TranscriptStream.test.tsx`

- [ ] **Step 1: Test**

```tsx
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { TranscriptStream } from './TranscriptStream';
import { useAgentCanvasStore } from '@/stores/agentCanvasStore';

describe('TranscriptStream', () => {
  it('renders user, assistant, tool, approval entries in order', () => {
    useAgentCanvasStore.getState().reset();
    useAgentCanvasStore.getState().setActiveSession('s');
    useAgentCanvasStore.getState().appendUserMessage('hi');
    useAgentCanvasStore.getState().applyAssistantDelta('hello back');
    useAgentCanvasStore.getState().appendToolInline('read_file', '', 'tc-1');
    useAgentCanvasStore.getState().queueApproval({ requestId: 'ar-1', tool: 'bash', args: {}, sessionId: 's' });
    render(<TranscriptStream />);
    expect(screen.getByText('hi')).toBeInTheDocument();
    expect(screen.getByText('hello back')).toBeInTheDocument();
    expect(screen.getByText('read_file')).toBeInTheDocument();
    expect(screen.getByText(/tool wants to run/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Implement**

```tsx
// TranscriptStream.tsx
import { useEffect, useRef } from 'react';
import { useAgentCanvasStore } from '@/stores/agentCanvasStore';
import { InlineToolBlock } from './InlineToolBlock';
import { InlineApprovalCard } from './InlineApprovalCard';

export function TranscriptStream() {
  const transcript = useAgentCanvasStore((s) => s.transcript);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [transcript.length]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: 12, overflowY: 'auto', flex: 1 }}>
      {transcript.map((e) => {
        if (e.kind === 'message') {
          return (
            <div key={e.id} style={{ display: 'flex', justifyContent: e.role === 'user' ? 'flex-end' : 'flex-start' }}>
              <div style={{ maxWidth: '80%', padding: '8px 12px', borderRadius: 12, background: e.role === 'user' ? '#1a1a1a' : '#0a0a0a', border: `1px solid ${e.role === 'user' ? 'rgba(255,77,0,0.2)' : 'rgba(255,255,255,0.06)'}`, fontSize: 13, whiteSpace: 'pre-wrap' }}>
                {e.text}
              </div>
            </div>
          );
        }
        if (e.kind === 'tool-inline') return <InlineToolBlock key={e.id} name={e.name} preview={e.preview} status={e.status} />;
        if (e.kind === 'approval-inline') return <InlineApprovalCard key={e.id} requestId={e.requestId} tool={e.tool} args={e.args} decision={e.decision} />;
        if (e.kind === 'reasoning') return (
          <div key={e.id} style={{ padding: 8, fontSize: 11, color: 'rgba(255,255,255,0.4)', fontStyle: 'italic' }}>
            {e.text}
          </div>
        );
        return null;
      })}
      <div ref={endRef} />
    </div>
  );
}
```

- [ ] **Step 3: Run tests + commit**

```bash
bun run test src/components/agent-canvas/transcript/TranscriptStream.test.tsx
git add src/components/agent-canvas/transcript/TranscriptStream.tsx src/components/agent-canvas/transcript/TranscriptStream.test.tsx
git commit -m "feat(agent-canvas): TranscriptStream

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task C9: `ActiveChatCard`

Wraps `TranscriptStream` in a `FloatingPanel`. Slice = end session.

**Files:**
- Create: `src/components/agent-canvas/ActiveChatCard.tsx`
- Create: `src/components/agent-canvas/ActiveChatCard.test.tsx`

- [ ] **Step 1: Test**

```tsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ActiveChatCard } from './ActiveChatCard';
import { useAgentCanvasStore } from '@/stores/agentCanvasStore';

describe('ActiveChatCard', () => {
  it('renders nothing when no active session', () => {
    useAgentCanvasStore.getState().reset();
    const { container } = render(<ActiveChatCard />);
    expect(container.firstChild).toBeNull();
  });
  it('renders the transcript when a session is active', () => {
    useAgentCanvasStore.getState().reset();
    useAgentCanvasStore.getState().setActiveSession('sess-1');
    useAgentCanvasStore.getState().appendUserMessage('Hi');
    render(<ActiveChatCard />);
    expect(screen.getByText('Hi')).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Implement**

```tsx
// ActiveChatCard.tsx
import { useAgentCanvasStore } from '@/stores/agentCanvasStore';
import { useChatSession } from '@/hooks/useChatSession';
import { FloatingPanel } from '@/components/node-editor/components/FloatingPanel';
import { TranscriptStream } from './transcript/TranscriptStream';
import { DEFAULT_CONFIG } from '@/components/node-editor/constants';

export function ActiveChatCard() {
  const activeId = useAgentCanvasStore((s) => s.activeSessionId);
  const { closeSession } = useChatSession();
  if (!activeId) return null;
  return (
    <FloatingPanel
      id={`active-chat-${activeId}`}
      initialX={typeof window !== 'undefined' ? (window.innerWidth - 600) / 2 : 100}
      initialY={typeof window !== 'undefined' ? (window.innerHeight - 500) / 2 : 100}
      initialWidth={600}
      initialHeight={500}
      config={DEFAULT_CONFIG}
      onPositionChange={() => {}}
      onSizeChange={() => {}}
      onBounce={() => {}}
      onDragStart={() => {}}
      onDragEnd={() => {}}
      onDismiss={() => { closeSession(); }}
      onConnectionDragStart={() => {}}
      onConnectionDragMove={() => {}}
      onConnectionDragEnd={() => {}}
      onConnectionDelete={() => {}}
      hideConnectionButton
      theme="dark"
      topBoundary={0}
    >
      <div style={{ display: 'flex', flexDirection: 'column', height: '100%', background: '#0a0a0a' }}>
        <TranscriptStream />
      </div>
    </FloatingPanel>
  );
}
```

> Note for the implementer: the `FloatingPanel` props above mirror what `NodeEditorCanvasDark` passes; trim to whatever the actual prop signature exposes. Consult [FloatingPanel.tsx](../../src/components/node-editor/components/FloatingPanel.tsx) for the canonical surface.

- [ ] **Step 3: Run tests + commit**

```bash
bun run test src/components/agent-canvas/ActiveChatCard.test.tsx
git add src/components/agent-canvas/ActiveChatCard.tsx src/components/agent-canvas/ActiveChatCard.test.tsx
git commit -m "feat(agent-canvas): ActiveChatCard with slice = end session

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task C10: `ApprovalModal`

Pops over the chat when window is focused and approvals are queued.

**Files:**
- Create: `src/components/agent-canvas/ApprovalModal.tsx`
- Create: `src/components/agent-canvas/ApprovalModal.test.tsx`

- [ ] **Step 1: Test**

```tsx
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ApprovalModal } from './ApprovalModal';
import { useAgentCanvasStore } from '@/stores/agentCanvasStore';

describe('ApprovalModal', () => {
  it('renders when an approval is queued', () => {
    useAgentCanvasStore.getState().reset();
    useAgentCanvasStore.getState().queueApproval({ requestId: 'ar-1', tool: 'bash', args: {}, sessionId: 's' });
    render(<ApprovalModal />);
    expect(screen.getByText(/approve tool/i)).toBeInTheDocument();
  });
  it('hides when queue is empty', () => {
    useAgentCanvasStore.getState().reset();
    const { container } = render(<ApprovalModal />);
    expect(container.firstChild).toBeNull();
  });
});
```

- [ ] **Step 2: Implement**

```tsx
// ApprovalModal.tsx
import { useAgentCanvasStore } from '@/stores/agentCanvasStore';
import { hermesService } from '@/services/hermesService';

export function ApprovalModal() {
  const next = useAgentCanvasStore((s) => s.approvalQueue[0]);
  if (!next) return null;
  const respond = async (d: 'allow-once'|'always'|'deny') => {
    await hermesService.call('approval.respond', { request_id: next.requestId, decision: d });
    useAgentCanvasStore.getState().resolveApproval(next.requestId, d);
  };
  return (
    <div role="dialog" aria-modal="true" aria-labelledby="approval-title" style={{ position: 'fixed', inset: 0, backgroundColor: 'rgba(0,0,0,0.4)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 9000 }}>
      <div style={{ width: 420, padding: 20, background: '#111', borderRadius: 12, border: '1px solid rgba(251,191,36,0.4)' }}>
        <h2 id="approval-title" style={{ margin: 0, fontSize: 14 }}>Approve tool: {next.tool}</h2>
        <pre style={{ marginTop: 8, fontSize: 11, color: 'rgba(255,255,255,0.6)', whiteSpace: 'pre-wrap' }}>{JSON.stringify(next.args, null, 2)}</pre>
        <div style={{ marginTop: 16, display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button type="button" onClick={() => respond('allow-once')}>Allow once</button>
          <button type="button" onClick={() => respond('always')}>Always</button>
          <button type="button" onClick={() => respond('deny')}>Deny</button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Run tests + commit**

```bash
bun run test src/components/agent-canvas/ApprovalModal.test.tsx
git add src/components/agent-canvas/ApprovalModal.tsx src/components/agent-canvas/ApprovalModal.test.tsx
git commit -m "feat(agent-canvas): ApprovalModal

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task C11: `AgentCanvas` shell + zone integration

The top-level component, plus chord `AD` wiring.

**Files:**
- Create: `src/components/agent-canvas/AgentCanvas.tsx`
- Create: `src/components/agent-canvas/AgentCanvas.test.tsx`
- Modify: `src/components/node-editor/components/CanvasBoard.tsx`
- Modify: `src/components/node-editor/components/WorkspaceCanvas.tsx`

- [ ] **Step 1: Test the shell**

```tsx
// AgentCanvas.test.tsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';
import { AgentCanvas } from './AgentCanvas';
import { hermesService } from '@/services/hermesService';

vi.spyOn(hermesService, 'getTokenMetadata').mockResolvedValue({ paired: true });
vi.spyOn(hermesService, 'getStatus').mockResolvedValue({ state: 'connected' as const, protocol_version: 1 });
vi.spyOn(hermesService, 'onEvent').mockResolvedValue(async () => {});
vi.spyOn(hermesService, 'onStatus').mockResolvedValue(async () => {});

const wrap = (n: React.ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{n}</QueryClientProvider>;
};

describe('AgentCanvas', () => {
  it('renders the bottom command bar', () => {
    render(wrap(<AgentCanvas />));
    expect(screen.getByPlaceholderText(/type \/ for commands/i)).toBeInTheDocument();
  });
  it('shows the connect modal when not paired', async () => {
    (hermesService.getTokenMetadata as any).mockResolvedValueOnce({ paired: false });
    render(wrap(<AgentCanvas />));
    expect(await screen.findByRole('dialog', { name: /pair with hermes/i })).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Implement the shell**

```tsx
// AgentCanvas.tsx
import { useState, useEffect } from 'react';
import { useHermesConnection } from '@/hooks/useHermesConnection';
import { useStreamingTurn } from '@/hooks/useStreamingTurn';
import { useHermesNotifications } from '@/hooks/useHermesNotifications';
import { useAgentCanvasStore } from '@/stores/agentCanvasStore';
import { BottomCommandBar } from './BottomCommandBar';
import { ActiveChatCard } from './ActiveChatCard';
import { ApprovalModal } from './ApprovalModal';
import { HermesConnectModal } from './connection/HermesConnectModal';
import { HermesStatusIndicator } from './connection/HermesStatusIndicator';

export function AgentCanvas() {
  const conn = useHermesConnection();
  useStreamingTurn();
  useHermesNotifications();
  const sideCards = useAgentCanvasStore((s) => s.sideCards);
  const [showConnect, setShowConnect] = useState(false);

  useEffect(() => {
    if (conn.metadata && !conn.metadata.paired) setShowConnect(true);
  }, [conn.metadata]);

  return (
    <div style={{ position: 'relative', height: '100vh', display: 'flex', flexDirection: 'column', background: '#0a0a0a', color: '#fcfcfc' }}>
      <div style={{ position: 'absolute', top: 12, right: 12, zIndex: 100 }}>
        <HermesStatusIndicator />
      </div>
      <div style={{ flex: 1, position: 'relative', overflow: 'hidden' }}>
        <ActiveChatCard />
        {/* Side-cards rendered in Phase D — placeholder so the shell composes correctly. */}
        {sideCards.length > 0 && (
          <div data-testid="side-card-pool" style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }} />
        )}
      </div>
      <BottomCommandBar />
      <ApprovalModal />
      {showConnect && <HermesConnectModal onClose={() => { setShowConnect(false); conn.refresh(); }} />}
    </div>
  );
}
```

- [ ] **Step 3: Wire the chord into `CanvasBoard.tsx`**

Open `src/components/node-editor/components/CanvasBoard.tsx`. Find the existing zone registry (likely an array or object mapping chord → component, similar to GameHQ). Add:

```tsx
import { AgentCanvas } from '@/components/agent-canvas/AgentCanvas';

// In the zone registry/array, add:
{ id: 'agent', label: 'Agent', chord: 'AD', component: AgentCanvas },
```

> If the chord registry shape differs, mirror exactly the GameHQ wiring (per CLAUDE.md memory: `gameExplorer` chord). The two integrations should look the same.

- [ ] **Step 4: Same wiring in `WorkspaceCanvas.tsx`**

Repeat the same change in `WorkspaceCanvas.tsx` (its location may differ; locate via `grep -r "gameExplorer\|game-explorer" src/`).

- [ ] **Step 5: Run all canvas tests + typecheck**

```bash
bun run test src/components/agent-canvas
bun run typecheck
```

Expected: all PASS.

- [ ] **Step 6: Manual smoke test**

```bash
bun run tauri dev
```

Expected:
- Press the `AD` chord → AgentCanvas opens.
- First-run modal appears (no token paired).
- Cancel modal → bottom command bar visible.
- Settings → Hermes pane visible.

- [ ] **Step 7: Commit**

```bash
git add src/components/agent-canvas/AgentCanvas.tsx src/components/agent-canvas/AgentCanvas.test.tsx src/components/node-editor/components/CanvasBoard.tsx src/components/node-editor/components/WorkspaceCanvas.tsx
git commit -m "$(cat <<'EOF'
feat(agent-canvas): AgentCanvas shell + zone wiring

Top-level shell composes BottomCommandBar, ActiveChatCard, ApprovalModal,
HermesStatusIndicator, HermesConnectModal. Wired into CanvasBoard and
WorkspaceCanvas as the AD-chord zone, matching the GameHQ pattern.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task C12: E2E happy path

**Files:**
- Create: `tests/e2e/agent-canvas.spec.ts`

- [ ] **Step 1: Write the spec**

```ts
import { test, expect } from '@playwright/test';

test.describe('AgentCanvas happy path (mock backend)', () => {
  test('open canvas → submit prompt → see deltas', async ({ page }) => {
    // The test relies on hermesService falling back to mockHermes when
    // running in the browser preview (no Tauri runtime).
    await page.goto('/');
    // Trigger AD chord — adapt to whatever input mechanism CanvasBoard uses.
    await page.keyboard.press('a');
    await page.keyboard.press('d');
    const input = page.getByPlaceholder(/type \/ for commands/i);
    await expect(input).toBeVisible();
    await input.fill('hello agent');
    await input.press('Enter');
    // Mock streams 6 deltas concatenating to "Sure — this is the mock agent replying."
    await expect(page.getByText(/mock agent replying/)).toBeVisible({ timeout: 5000 });
  });
});
```

- [ ] **Step 2: Run E2E**

```bash
bun run test:e2e --project=chromium tests/e2e/agent-canvas.spec.ts
```

Expected: PASS. If the chord trigger differs, adapt the keypresses to match `CanvasBoard.tsx` actual handling.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/agent-canvas.spec.ts
git commit -m "test(agent-canvas): E2E happy-path smoke

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

**Phase C complete.** Acceptance check:

```bash
bun run typecheck
bun run test src/components/agent-canvas src/components/preferences
bun run test:e2e --project=chromium tests/e2e/agent-canvas.spec.ts
cd src-tauri && cargo test
```

All green. Manual smoke against a real Mac mini: pair, ask "explain X", watch deltas, approve a tool, slice the chat.

---

## Phase D — Side-cards, threads panel, save-as-note, polish

**Goal at end of Phase D:** Full v1 spec parity. Research-style prompt produces an artifact card; save-as-note creates a real note. Threads panel toggles via button + `Ctrl+T`. Tool-progress cards auto-fade. Periodic `/health` probe surfaces server-side revocations.

### Task D1: `ToolProgressCard` and `SubagentThreadCard`

**Files:**
- Create: `src/components/agent-canvas/side-cards/ToolProgressCard.tsx`
- Create: `src/components/agent-canvas/side-cards/ToolProgressCard.test.tsx`
- Create: `src/components/agent-canvas/side-cards/SubagentThreadCard.tsx`
- Create: `src/components/agent-canvas/side-cards/SubagentThreadCard.test.tsx`

- [ ] **Step 1: Test ToolProgressCard**

```tsx
// ToolProgressCard.test.tsx
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ToolProgressCard } from './ToolProgressCard';

describe('ToolProgressCard', () => {
  it('renders title, status, and content', () => {
    render(<ToolProgressCard title="web_research" content="searching…" status="running" pinned={false} onTogglePin={() => {}} onDismiss={() => {}} />);
    expect(screen.getByText('web_research')).toBeInTheDocument();
    expect(screen.getByText(/searching/)).toBeInTheDocument();
    expect(screen.getByText(/running/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Implement**

```tsx
// ToolProgressCard.tsx
interface Props {
  title: string;
  content: string;
  status: 'running'|'complete'|'error';
  pinned: boolean;
  onTogglePin: () => void;
  onDismiss: () => void;
}

const STATUS_COLOR: Record<Props['status'], string> = { running: '#fbbf24', complete: '#4ade80', error: '#ef4444' };

export function ToolProgressCard({ title, content, status, pinned, onTogglePin, onDismiss }: Props) {
  return (
    <div role="article" aria-label={`Tool ${title}`} style={{ padding: 12, background: '#0a0a0a', border: '1px solid rgba(255,255,255,0.08)', borderRadius: 8, fontSize: 12, height: '100%', display: 'flex', flexDirection: 'column' }}>
      <header style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ width: 8, height: 8, borderRadius: '50%', background: STATUS_COLOR[status] }} />
        <strong>{title}</strong>
        <span style={{ fontSize: 10, color: 'rgba(255,255,255,0.5)' }}>{status}</span>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 4 }}>
          <button type="button" onClick={onTogglePin} aria-pressed={pinned} title={pinned ? 'Unpin' : 'Pin'}>{pinned ? '📌' : '📍'}</button>
          <button type="button" onClick={onDismiss} title="Dismiss">×</button>
        </div>
      </header>
      <pre style={{ marginTop: 8, fontSize: 11, color: 'rgba(255,255,255,0.7)', whiteSpace: 'pre-wrap', overflow: 'auto', flex: 1 }}>{content}</pre>
    </div>
  );
}
```

- [ ] **Step 3: Test SubagentThreadCard**

```tsx
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SubagentThreadCard } from './SubagentThreadCard';

describe('SubagentThreadCard', () => {
  it('renders messages in order', () => {
    render(<SubagentThreadCard title="background task" messages={[{ id: '1', text: 'starting' }, { id: '2', text: 'done' }]} status="complete" onDismiss={() => {}} />);
    expect(screen.getByText('starting')).toBeInTheDocument();
    expect(screen.getByText('done')).toBeInTheDocument();
  });
});
```

- [ ] **Step 4: Implement**

```tsx
// SubagentThreadCard.tsx
interface Message { id: string; text: string }
interface Props {
  title: string;
  messages: Message[];
  status: 'running'|'complete'|'error';
  onDismiss: () => void;
}

export function SubagentThreadCard({ title, messages, status, onDismiss }: Props) {
  return (
    <div role="article" aria-label={`Subagent ${title}`} style={{ padding: 12, background: '#0a0a0a', border: '1px solid rgba(255,255,255,0.08)', borderRadius: 8, fontSize: 12, height: '100%', display: 'flex', flexDirection: 'column' }}>
      <header style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <strong>{title}</strong>
        <span style={{ fontSize: 10, color: 'rgba(255,255,255,0.5)' }}>{status}</span>
        <button type="button" onClick={onDismiss} style={{ marginLeft: 'auto' }}>×</button>
      </header>
      <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 4, overflow: 'auto', flex: 1 }}>
        {messages.map((m) => (
          <div key={m.id} style={{ padding: '4px 8px', background: 'rgba(255,255,255,0.03)', borderRadius: 4 }}>{m.text}</div>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Run tests + commit**

```bash
bun run test src/components/agent-canvas/side-cards/ToolProgressCard.test.tsx src/components/agent-canvas/side-cards/SubagentThreadCard.test.tsx
git add src/components/agent-canvas/side-cards/ToolProgressCard.tsx src/components/agent-canvas/side-cards/ToolProgressCard.test.tsx src/components/agent-canvas/side-cards/SubagentThreadCard.tsx src/components/agent-canvas/side-cards/SubagentThreadCard.test.tsx
git commit -m "feat(agent-canvas): tool progress + subagent thread cards

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task D2: `ArtifactCard` with save-as-note

**Files:**
- Create: `src/components/agent-canvas/side-cards/ArtifactCard.tsx`
- Create: `src/components/agent-canvas/side-cards/ArtifactCard.test.tsx`

- [ ] **Step 1: Test**

```tsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ArtifactCard } from './ArtifactCard';
import * as noteService from '@/services/noteService';

describe('ArtifactCard', () => {
  it('saves the content as a note via noteService', async () => {
    const spy = vi.spyOn(noteService, 'createNote' as any).mockResolvedValue({ id: 'note-1' });
    render(<ArtifactCard title="Research X" content="# X\n\nbody" pinned={false} onTogglePin={() => {}} onDismiss={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: /save as note/i }));
    await waitFor(() => expect(spy).toHaveBeenCalled());
  });
});
```

- [ ] **Step 2: Implement**

```tsx
// ArtifactCard.tsx
import { useState } from 'react';
import * as noteService from '@/services/noteService';

interface Props {
  title: string;
  content: string;
  pinned: boolean;
  onTogglePin: () => void;
  onDismiss: () => void;
}

export function ArtifactCard({ title, content, pinned, onTogglePin, onDismiss }: Props) {
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);

  const save = async () => {
    setBusy(true);
    try {
      // The exact noteService API depends on the existing service; pass title + markdown.
      await (noteService as any).createNote({ title, content, source: 'hermes-artifact' });
      setSaved(true);
    } finally { setBusy(false); }
  };

  return (
    <div role="article" aria-label={`Artifact ${title}`} style={{ padding: 12, background: '#0a0a0a', border: '1px solid rgba(255,255,255,0.08)', borderRadius: 8, fontSize: 12, height: '100%', display: 'flex', flexDirection: 'column' }}>
      <header style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <strong>{title}</strong>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 4 }}>
          <button type="button" onClick={save} disabled={busy || saved} title="Save as note">{saved ? '✓ saved' : '💾 Save as note'}</button>
          <button type="button" onClick={onTogglePin} aria-pressed={pinned}>{pinned ? '📌' : '📍'}</button>
          <button type="button" onClick={onDismiss}>×</button>
        </div>
      </header>
      <pre style={{ marginTop: 8, fontSize: 12, lineHeight: 1.5, color: 'rgba(255,255,255,0.85)', whiteSpace: 'pre-wrap', overflow: 'auto', flex: 1 }}>{content}</pre>
    </div>
  );
}
```

> Note for the implementer: confirm the existing `noteService` export name (`createNote`, `create`, etc.) by reading [src/services/noteService.ts](../../src/services/noteService.ts) and adjust the import.

- [ ] **Step 3: Run tests + commit**

```bash
bun run test src/components/agent-canvas/side-cards/ArtifactCard.test.tsx
git add src/components/agent-canvas/side-cards/ArtifactCard.tsx src/components/agent-canvas/side-cards/ArtifactCard.test.tsx
git commit -m "feat(agent-canvas): ArtifactCard with save-as-note

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task D3: Side-card pool render in `AgentCanvas`

Wire the cards into the canvas, each in a `FloatingPanel`. Auto-fade for completed tool-progress cards.

**Files:**
- Modify: `src/components/agent-canvas/AgentCanvas.tsx`

- [ ] **Step 1: Replace the side-card placeholder**

Edit `AgentCanvas.tsx`. Replace the previous placeholder div with a real renderer:

```tsx
import { ToolProgressCard } from './side-cards/ToolProgressCard';
import { ArtifactCard } from './side-cards/ArtifactCard';
import { SubagentThreadCard } from './side-cards/SubagentThreadCard';
import { FloatingPanel } from '@/components/node-editor/components/FloatingPanel';
import { DEFAULT_CONFIG } from '@/components/node-editor/constants';

// Add inside AgentCanvas component, replacing the placeholder side-card-pool div:
{sideCards.map((card, i) => (
  <FloatingPanel
    key={card.id}
    id={card.id}
    initialX={120 + (i % 3) * 360}
    initialY={120 + Math.floor(i / 3) * 320}
    initialWidth={340}
    initialHeight={300}
    config={DEFAULT_CONFIG}
    onPositionChange={() => {}}
    onSizeChange={() => {}}
    onBounce={() => {}}
    onDragStart={() => {}}
    onDragEnd={() => {}}
    onDismiss={() => useAgentCanvasStore.getState().removeSideCard(card.id)}
    onConnectionDragStart={() => {}}
    onConnectionDragMove={() => {}}
    onConnectionDragEnd={() => {}}
    onConnectionDelete={() => {}}
    hideConnectionButton
    theme="dark"
    topBoundary={0}
  >
    {card.kind === 'artifact' && (
      <ArtifactCard title={card.title} content={card.content} pinned={card.pinned}
        onTogglePin={() => useAgentCanvasStore.getState().pinSideCard(card.id, !card.pinned)}
        onDismiss={() => useAgentCanvasStore.getState().removeSideCard(card.id)} />
    )}
    {card.kind === 'tool-progress' && (
      <ToolProgressCard title={card.title} content={card.content} status={card.status} pinned={card.pinned}
        onTogglePin={() => useAgentCanvasStore.getState().pinSideCard(card.id, !card.pinned)}
        onDismiss={() => useAgentCanvasStore.getState().removeSideCard(card.id)} />
    )}
    {card.kind === 'subagent-thread' && (
      <SubagentThreadCard title={card.title} messages={[]} status={card.status}
        onDismiss={() => useAgentCanvasStore.getState().removeSideCard(card.id)} />
    )}
  </FloatingPanel>
))}
```

- [ ] **Step 2: Add auto-fade effect for completed unpinned tool-progress cards**

Inside `AgentCanvas`, add:

```tsx
useEffect(() => {
  const timers = sideCards
    .filter((c) => c.kind === 'tool-progress' && c.status === 'complete' && !c.pinned)
    .map((c) => {
      const elapsed = c.completedAt ? Date.now() - c.completedAt : 0;
      const remaining = Math.max(0, 5000 - elapsed);
      return setTimeout(() => useAgentCanvasStore.getState().removeSideCard(c.id), remaining);
    });
  return () => { timers.forEach(clearTimeout); };
}, [sideCards]);
```

- [ ] **Step 3: Run tests, manual smoke**

```bash
bun run test src/components/agent-canvas/AgentCanvas.test.tsx
bun run tauri dev
```

In the running app, dispatch a mock long-running tool to confirm a card spawns; wait 5s after complete to confirm auto-fade (pin to verify it stays).

- [ ] **Step 4: Commit**

```bash
git add src/components/agent-canvas/AgentCanvas.tsx
git commit -m "$(cat <<'EOF'
feat(agent-canvas): side-card pool + auto-fade

Side-cards render in FloatingPanels arranged in a 3-column grid.
Completed unpinned tool-progress cards auto-dismiss 5s after
completion. Artifacts and subagent threads stick until dismissed.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task D4: `ThreadsPanel` (toggle button + `Ctrl+T` shortcut)

**Files:**
- Create: `src/components/agent-canvas/ThreadsPanel.tsx`
- Create: `src/components/agent-canvas/ThreadsPanel.test.tsx`
- Modify: `src/components/agent-canvas/AgentCanvas.tsx`

- [ ] **Step 1: Test**

```tsx
// ThreadsPanel.test.tsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';
import { ThreadsPanel } from './ThreadsPanel';
import { hermesService } from '@/services/hermesService';
import { useAgentCanvasStore } from '@/stores/agentCanvasStore';

const wrap = (n: React.ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{n}</QueryClientProvider>;
};

describe('ThreadsPanel', () => {
  it('lists sessions and resumes one on click', async () => {
    vi.spyOn(hermesService, 'call').mockImplementation(async (m: string) => {
      if (m === 'session.list') return { sessions: [{ session_id: 'sess-A', title: 'Alpha', model: 'alex', last_active_at: Date.now(), message_count: 3 }] };
      if (m === 'session.resume') return { session_id: 'sess-A' };
      return {};
    });
    useAgentCanvasStore.getState().reset();
    render(wrap(<ThreadsPanel onClose={() => {}} />));
    fireEvent.click(await screen.findByText('Alpha'));
    await waitFor(() => expect(useAgentCanvasStore.getState().activeSessionId).toBe('sess-A'));
  });
});
```

- [ ] **Step 2: Implement**

```tsx
// ThreadsPanel.tsx
import { useQuery } from '@tanstack/react-query';
import { hermesService } from '@/services/hermesService';
import { useChatSession } from '@/hooks/useChatSession';

interface Session {
  session_id: string;
  title: string;
  model: string;
  last_active_at: number;
  message_count: number;
  platform?: string;
}

interface Props { onClose: () => void }

export function ThreadsPanel({ onClose }: Props) {
  const { resumeSession } = useChatSession();
  const q = useQuery({
    queryKey: ['hermes', 'session.list'],
    queryFn: async () => (await hermesService.call<{ sessions: Session[] }>('session.list', {})).sessions,
    staleTime: 15_000,
  });

  return (
    <aside aria-label="Threads" style={{ position: 'absolute', left: 0, top: 0, bottom: 0, width: 280, background: '#0a0a0a', borderRight: '1px solid rgba(255,255,255,0.06)', padding: 12, overflowY: 'auto', zIndex: 50 }}>
      <header style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <strong style={{ fontSize: 12 }}>Threads</strong>
        <button type="button" onClick={onClose}>×</button>
      </header>
      <div style={{ marginTop: 12, display: 'flex', flexDirection: 'column', gap: 4 }}>
        {(q.data ?? []).map((s) => (
          <button key={s.session_id} type="button" onClick={() => { resumeSession(s.session_id); onClose(); }}
            style={{ padding: 8, textAlign: 'left', background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.06)', borderRadius: 6, color: '#fff', cursor: 'pointer' }}>
            <div style={{ fontSize: 12, fontWeight: 500 }}>{s.title}</div>
            <div style={{ fontSize: 10, color: 'rgba(255,255,255,0.4)', marginTop: 2 }}>
              {s.model} · {s.message_count} msg
              {s.platform && s.platform !== 'desktop_app' && <span style={{ marginLeft: 4, padding: '0 4px', background: 'rgba(255,255,255,0.06)', borderRadius: 2 }}>{s.platform}</span>}
            </div>
          </button>
        ))}
      </div>
    </aside>
  );
}
```

- [ ] **Step 3: Wire toggle into `AgentCanvas`**

Edit `AgentCanvas.tsx`:

```tsx
import { ThreadsPanel } from './ThreadsPanel';

// Inside the component:
const [threadsOpen, setThreadsOpen] = useState(false);

useEffect(() => {
  const onKey = (e: KeyboardEvent) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 't') {
      e.preventDefault();
      setThreadsOpen((p) => !p);
    }
  };
  window.addEventListener('keydown', onKey);
  return () => window.removeEventListener('keydown', onKey);
}, []);

// In JSX, near the top-left chrome:
<button type="button" onClick={() => setThreadsOpen((p) => !p)} aria-pressed={threadsOpen}
  style={{ position: 'absolute', top: 12, left: 12, zIndex: 60 }} title="Threads (Ctrl+T)">
  ☰
</button>
{threadsOpen && <ThreadsPanel onClose={() => setThreadsOpen(false)} />}
```

- [ ] **Step 4: Run tests + commit**

```bash
bun run test src/components/agent-canvas/ThreadsPanel.test.tsx src/components/agent-canvas/AgentCanvas.test.tsx
git add src/components/agent-canvas/ThreadsPanel.tsx src/components/agent-canvas/ThreadsPanel.test.tsx src/components/agent-canvas/AgentCanvas.tsx
git commit -m "feat(agent-canvas): ThreadsPanel + Ctrl+T toggle

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task D5: Periodic `/health` probe (detect server-side revocation)

**Files:**
- Modify: `src/hooks/useHermesConnection.ts` (add periodic probe)

- [ ] **Step 1: Add the probe**

Edit `src/hooks/useHermesConnection.ts`. Inside the `useEffect`, after the initial `refresh()` call:

```ts
const probeId = setInterval(async () => {
  if (!metadata?.host || !metadata?.port) return;
  try { await hermesService.healthCheck(metadata.host, metadata.port); }
  catch {
    // 401 from /health (or any failure) means server-side revocation or outage.
    // Refresh status so UI shows degraded state.
    refresh();
  }
}, 30_000);

return () => { off?.(); clearInterval(probeId); };
```

- [ ] **Step 2: Quick test for the probe**

Append to `src/hooks/useHermesConnection.test.ts`:

```ts
it('probes /health every 30s when paired', async () => {
  vi.useFakeTimers();
  const probe = vi.spyOn(hermesService, 'healthCheck').mockResolvedValue({ ok: true });
  vi.spyOn(hermesService, 'getTokenMetadata').mockResolvedValue({ paired: true, host: '1.1.1.1', port: 8645 });
  renderHook(() => useHermesConnection());
  await vi.advanceTimersByTimeAsync(31_000);
  expect(probe).toHaveBeenCalled();
  vi.useRealTimers();
});
```

- [ ] **Step 3: Run tests + commit**

```bash
bun run test src/hooks/useHermesConnection.test.ts
git add src/hooks/useHermesConnection.ts src/hooks/useHermesConnection.test.ts
git commit -m "feat(agent-canvas): periodic /health probe

Detects server-side hermes desktop revoke (token removed but socket
not forcibly closed) by polling /health every 30s and refreshing
connection status on failure.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task D6: Wire OS notifications via `tauri-plugin-notification`

The Phase A scaffold logs only — finalize the wiring.

**Files:**
- Modify: `src-tauri/src/lib.rs` (use the notification plugin)

- [ ] **Step 1: Confirm the plugin is registered**

Open `src-tauri/src/lib.rs`. Verify the builder includes:

```rust
.plugin(tauri_plugin_notification::init())
```

If absent, add it.

- [ ] **Step 2: Replace the stub emitter with a real notification call**

Find the spawn that filters events for notification (added in Task B9 / A8). Replace the body with:

```rust
let event_type_str = env.pointer("/params/type").and_then(|v| v.as_str()).unwrap_or("").to_string();
if d2.should_notify(&event_type_str) {
    use tauri_plugin_notification::NotificationExt;
    let title = match event_type_str.as_str() {
        "approval.request" => "Hermes — approval requested",
        "message.complete" => "Hermes — turn complete",
        _ => "Hermes",
    };
    let body = env.pointer("/params/payload").and_then(|p| serde_json::to_string(p).ok())
        .unwrap_or_else(|| "(no body)".into());
    let _ = app_handle3.notification().builder()
        .title(title)
        .body(body.chars().take(140).collect::<String>())
        .show();
}
```

- [ ] **Step 3: Manual smoke**

```bash
bun run tauri dev
```

Steps:
1. Open AgentCanvas, ensure paired.
2. Send a prompt that triggers an approval-required tool.
3. While the request is pending, alt-tab away from the window.
4. Expect an OS notification: "Hermes — approval requested".

- [ ] **Step 4: Commit**

```bash
git add src-tauri/src/lib.rs src-tauri/tauri.conf.json
git commit -m "$(cat <<'EOF'
feat(agent-canvas): wire OS notifications via plugin

Notification dispatcher now calls tauri-plugin-notification when the
window is unfocused for approval.request and message.complete. Title
varies by event type; body is the payload truncated to 140 chars.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task D7: Radial menu power options on side-cards

Reuse [InlineRadialMenu.tsx](../../src/components/node-editor/components/InlineRadialMenu.tsx) for right-click on side-cards.

**Files:**
- Modify: `src/components/agent-canvas/AgentCanvas.tsx`

- [ ] **Step 1: Add right-click handler to the side-card FloatingPanels**

Edit the side-card mapping in `AgentCanvas.tsx`. Wrap each card child in a div with `onContextMenu`:

```tsx
const [radialMenu, setRadialMenu] = useState<{ x: number; y: number; cardId: string } | null>(null);

// On each side-card FloatingPanel child:
<div onContextMenu={(e) => { e.preventDefault(); setRadialMenu({ x: e.clientX, y: e.clientY, cardId: card.id }); }}>
  {/* existing card content */}
</div>
```

Render the menu:

```tsx
import { InlineRadialMenu } from '@/components/node-editor/components/InlineRadialMenu';

{radialMenu && (
  <InlineRadialMenu
    config={{
      rootLabel: 'CARD',
      items: [
        { id: 'pin', label: 'Pin', action: () => { useAgentCanvasStore.getState().pinSideCard(radialMenu.cardId, true); } },
        { id: 'unpin', label: 'Unpin', action: () => { useAgentCanvasStore.getState().pinSideCard(radialMenu.cardId, false); } },
        { id: 'dismiss', label: 'Dismiss', action: () => { useAgentCanvasStore.getState().removeSideCard(radialMenu.cardId); } },
      ],
    }}
    position={{ x: radialMenu.x, y: radialMenu.y }}
    onClose={() => setRadialMenu(null)}
  />
)}
```

- [ ] **Step 2: Manual smoke**

```bash
bun run tauri dev
```

Right-click a side-card → radial menu appears with Pin / Unpin / Dismiss.

- [ ] **Step 3: Commit**

```bash
git add src/components/agent-canvas/AgentCanvas.tsx
git commit -m "feat(agent-canvas): radial menu on side-cards

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task D8: Final E2E coverage

**Files:**
- Modify: `tests/e2e/agent-canvas.spec.ts`

- [ ] **Step 1: Add tests for side-cards and threads panel**

Append to `tests/e2e/agent-canvas.spec.ts`:

```ts
test('threads panel toggles via Ctrl+T', async ({ page }) => {
  await page.goto('/');
  await page.keyboard.press('a');
  await page.keyboard.press('d');
  await page.keyboard.press('Control+t');
  await expect(page.getByRole('complementary', { name: /threads/i })).toBeVisible();
  await page.keyboard.press('Control+t');
  await expect(page.getByRole('complementary', { name: /threads/i })).toBeHidden();
});

test('save artifact as note triggers noteService', async ({ page }) => {
  // This relies on a mock or hook to inject a tool.complete with large output.
  // For E2E, set HERMES_USE_MOCK=1 and the mock injects an artifact card after a prompt.
  await page.goto('/?hermes_mock_artifact=1');
  await page.keyboard.press('a');
  await page.keyboard.press('d');
  await page.getByPlaceholder(/type \/ for commands/i).fill('research X');
  await page.keyboard.press('Enter');
  const saveBtn = page.getByRole('button', { name: /save as note/i });
  await expect(saveBtn).toBeVisible({ timeout: 7_000 });
  await saveBtn.click();
  await expect(page.getByText(/saved/i)).toBeVisible();
});
```

> Implementer note: the `?hermes_mock_artifact=1` flag is something the mock should honor. Add to `mockHermes.ts` if not present — read `window.location.search` once on construction to inject a long artifact `tool.complete` after the first `prompt.submit`.

- [ ] **Step 2: Run E2E**

```bash
bun run test:e2e --project=chromium
```

Expected: all PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/agent-canvas.spec.ts src/services/mockHermes.ts
git commit -m "test(agent-canvas): E2E for threads panel + save-as-note

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task D9: Coverage gate + final cleanup

**Files:**
- Possibly: `vitest.config.ts` (if coverage thresholds are configured per-path)
- Cleanup: legacy `ChatPanel.tsx` deletion can wait for a follow-up — don't touch unless `NodeEditorCanvasDark` no longer imports it.

- [ ] **Step 1: Run coverage**

```bash
bun run test:coverage
```

Expected: ≥80% for `src/services/hermesService.ts`, `src/hooks/useHermes*.ts`, `src/stores/agentCanvasStore.ts`, `src/utils/sideCardHeuristics.ts`, `src/components/agent-canvas/**`.

- [ ] **Step 2: Plug coverage gaps**

Any module under threshold gets a focused test added. Target gaps over wholesale rewrites — re-check with `bun run test:coverage` after each addition.

- [ ] **Step 3: Full CI check**

```bash
bun run typecheck
bun run test:coverage
cd src-tauri && cargo test && cd ..
bun run test:e2e --project=chromium
bun run build
```

All green.

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
chore(agent-canvas): coverage gates + Phase D wrap-up

Phase D complete. AgentCanvas v1 is feature-complete per
docs/plans/agent-canvas-design.md.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

**Phase D complete — v1 done.** What ships:

- Single active session UI with sliceable, draggable transcript card.
- Bottom command bar = single input, slash autocomplete, model picker.
- Inline rendering for short tool calls; side-card spawn for long-running, large-output, or progress-emitting tools; auto-fade for completed unpinned tool-progress cards.
- Approvals as inline transcript cards + modal when focused + OS notification when unfocused.
- Threads panel toggleable via button + `Ctrl+T`; cross-platform sessions visible.
- ArtifactCard save-as-note path integrated with existing `noteService`.
- Pairing + revoke + health-check via Preferences pane.
- Eager-on-app-start WSS connection with reconnect, heartbeat-via-server, pong-miss timeout, periodic `/health` probe.
- Token confined to Rust + OS keyring; never crosses IPC.

Out of scope (revisit per [agent-canvas-design.md §Open considerations](./agent-canvas-design.md)): voice in/out, generic file attachments, structured-data slash command pickers, admin command gating, live event re-attach, multi-window.

---

## Plan self-review

Spec coverage check (each spec section maps to a task):

| Spec section | Tasks |
|---|---|
| Architecture overview / 6 layers | A1–A9, B1–B9 |
| File layout (TS) | covered file-by-file across B and C |
| File layout (Rust) | A2–A8 |
| BottomCommandBar UX | C6 |
| ThreadsPanel | D4 |
| ActiveChatCard | C9 |
| Side-card flavors | B2, D1, D2, D3 |
| Approvals (inline + modal + OS notify) | B7, C7, C10, D6 |
| HermesConnectModal | C2 |
| HermesPane | C3 |
| Rust ↔ TS contract (commands + events) | A8, A9, B5 |
| Hermes client state machine | A4, A6, A7 |
| Auth flow + keyring | A2, C2, C3 |
| Mock / dev mode | B4, B5 |
| Tests at all layers | every task |
| Phased delivery | A, B, C, D |
| Open considerations / out of scope | acknowledged in summary |

No gaps found.

Placeholder scan: no "TBD", "implement later", or untyped pseudo-code. The few "implementer note" call-outs are concrete (signature confirmation) not placeholders.

Type/name consistency check:
- `RpcOutcome::{Result, Error}` is used identically in `rpc.rs` (Task A3), `client.rs` (Task A6), and `commands/hermes.rs` (Task A9). ✅
- `ConnState` / `ConnStatus` defined in `reconnect.rs` (Task A4) and reused unchanged in `client.rs`, `commands/hermes.rs`. ✅
- `HermesEventEnvelope` is the same shape in `types.ts` (Task B1), `mockHermes.ts` (Task B4), `hermesService.ts` (Task B5), `useStreamingTurn.ts` (Task B7). ✅
- `SideCard.kind` values (`'tool-progress' | 'artifact' | 'subagent-thread'`) match between `types.ts`, `agentCanvasStore.ts`, `useStreamingTurn.ts`, and the `AgentCanvas.tsx` switch in Task D3. ✅
- `ApprovalRespondParams.decision` literal union matches `'allow-once' | 'always' | 'deny'` everywhere (`InlineApprovalCard`, `ApprovalModal`, `agentCanvasStore.resolveApproval`). ✅
- `hermesService.call` signature is consistent across all hook tests and component tests. ✅

No inconsistencies found.

---

## Execution Handoff

Plan complete and saved to [agent-canvas-implementation-plan.md](./agent-canvas-implementation-plan.md). Two execution options:

**1. Subagent-Driven (recommended)** — Dispatch a fresh subagent per task; review between tasks; fast iteration. Best for a feature this size where each task is well-scoped and the per-task context is small.

**2. Inline Execution** — Execute tasks in this session via `executing-plans`; batch with checkpoints for review. Better if you want to drive it interactively and intervene per-step.

Which approach?
