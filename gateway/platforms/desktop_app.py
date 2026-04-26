"""DesktopAppAdapter — exposes the tui_gateway JSON-RPC dispatcher over WebSocket.

A single aiohttp web app accepts a WebSocket connection at ``/ws`` and
hands it to ``tui_gateway.ws.handle_ws`` — the same dispatcher the
Hermes TUI uses, exposing the full RPC surface (sessions, prompts,
slash commands, approvals, voice, attachments, model switching) over
the wire instead of stdio.

Connections require a bearer token from ``hermes desktop pair`` once
any client has been paired; loopback binds with an empty token store
remain open for local development.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket as _socket
import ssl
from pathlib import Path
from typing import Any, Optional

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    is_network_accessible,
)
from gateway.platforms.desktop_app_auth import TokenStore
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8645
WS_PATH = "/ws"
PROTOCOL_VERSION = 1


def check_desktop_app_requirements() -> bool:
    """Factory-side dependency check (mirrors api_server's pattern)."""
    return AIOHTTP_AVAILABLE


def _default_token_file() -> Path:
    return Path(get_hermes_home()) / "desktop_app_tokens.json"


# ---------------------------------------------------------------------------
# client.hello — registered into tui_gateway's dispatcher. Idempotent so
# multiple adapter instances or reloads don't double-register.
# ---------------------------------------------------------------------------

_HELLO_REGISTERED = False


def _register_client_hello() -> None:
    global _HELLO_REGISTERED
    if _HELLO_REGISTERED:
        return
    try:
        from tui_gateway import server as _tg_server
    except ImportError as exc:
        logger.error("[desktop_app] cannot register client.hello: %s", exc)
        return

    if "client.hello" in _tg_server._methods:
        # Already registered (e.g. by a previous adapter instance); nothing to do.
        _HELLO_REGISTERED = True
        return

    @_tg_server.method("client.hello")
    def _client_hello(rid, params: dict) -> dict:
        client_id = (params or {}).get("client_id") or "unknown"
        client_version = (params or {}).get("client_version") or "unknown"
        client_caps = (params or {}).get("capabilities") or []
        try:
            from hermes_cli.banner import HERMES_VERSION as _HV
            server_version = _HV
        except Exception:
            server_version = "hermes-agent"

        result = {
            "server_version": server_version,
            "protocol_version": PROTOCOL_VERSION,
            "capabilities": [
                "voice",
                "tts",
                "approval",
                "skills",
                "insights",
                "session.list",
                "session.resume",
                "slash.exec",
                "complete.slash",
                "model.options",
                "image.attach",
                "attachment.upload",
                "config.reveal_secret",
                # Event types the client can subscribe to. The agent
                # already emits these; listing them here lets the Tauri
                # side pick the right ones for OS-notification triggers
                # (e.g. approval.request when unfocused, message.complete
                # when a long turn finishes off-screen).
                "message.complete",
                "tool.complete",
            ],
            "client_id": client_id,
            "client_version": client_version,
            "client_capabilities": client_caps,
        }
        return _tg_server._ok(rid, result)

    _HELLO_REGISTERED = True


# ---------------------------------------------------------------------------
# WebSocket shim — adapts aiohttp.WebSocketResponse to the starlette-style
# interface tui_gateway.ws.handle_ws expects (accept/send_text/receive_text/
# close, with disconnect raising starlette's WebSocketDisconnect).
# ---------------------------------------------------------------------------

try:
    from starlette.websockets import WebSocketDisconnect as _WSDisc  # type: ignore[import-not-found]
except ImportError:
    class _WSDisc(Exception):  # type: ignore[no-redef]
        def __init__(self, code: int = 1000, reason: str = "") -> None:
            super().__init__(f"ws disconnect code={code} reason={reason!r}")
            self.code = code
            self.reason = reason


def _verify_bearer(request: "web.Request", tokens: TokenStore) -> Optional[str]:
    """Extract `Authorization: Bearer <token>` and return the client
    name on a hit, or None if missing/malformed/unknown/revoked.
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    presented = header[len("Bearer ") :].strip()
    if not presented:
        return None
    return tokens.verify(presented)


class _AioHttpWsShim:
    """Minimal duck-typed wrapper so handle_ws works against aiohttp."""

    def __init__(self, ws: "web.WebSocketResponse") -> None:
        self._ws = ws

    async def accept(self) -> None:
        # aiohttp ws is already prepared in the route handler before we wrap
        # it; this is a no-op stub for handle_ws's call site.
        return None

    async def send_text(self, text: str) -> None:
        await self._ws.send_str(text)

    async def receive_text(self) -> str:
        while True:
            msg = await self._ws.receive()
            if msg.type == web.WSMsgType.TEXT:
                return msg.data
            if msg.type in (
                web.WSMsgType.CLOSE,
                web.WSMsgType.CLOSING,
                web.WSMsgType.CLOSED,
            ):
                raise _WSDisc(code=1000, reason="client closed")
            if msg.type == web.WSMsgType.ERROR:
                raise _WSDisc(code=1011, reason=str(self._ws.exception() or "ws error"))
            # BINARY / PING / PONG — ignore, keep waiting.
            continue

    async def close(self) -> None:
        try:
            await self._ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# The adapter itself.
# ---------------------------------------------------------------------------


class DesktopAppAdapter(BasePlatformAdapter):
    """Network exposure of tui_gateway for full-feature desktop chat clients.

    Inbound user messages do NOT flow through ``BasePlatformAdapter.handle_message``
    — they're handled directly by tui_gateway's ``prompt.submit`` handler, which
    drives ``AIAgent.run_conversation`` and emits ``message.delta`` /
    ``message.complete`` events back over the same WebSocket. Subclassing
    ``BasePlatformAdapter`` is therefore mostly bookkeeping: it gives us a
    ``Platform`` enum entry, lifecycle (connect/disconnect), runtime status
    reporting, and visibility in ``/platforms``.
    """

    def __init__(self, config: PlatformConfig) -> None:
        super().__init__(config, Platform.DESKTOP_APP)
        extra = config.extra or {}
        self._host: str = str(
            extra.get("host", os.getenv("DESKTOP_APP_HOST", DEFAULT_HOST))
        )
        self._port: int = int(
            extra.get("port", os.getenv("DESKTOP_APP_PORT", str(DEFAULT_PORT)))
        )
        self._token_file: Path = Path(
            extra.get(
                "token_file",
                os.getenv("DESKTOP_APP_TOKEN_FILE", str(_default_token_file())),
            )
        )
        self._tokens: TokenStore = TokenStore(self._token_file)

        self._app: Optional["web.Application"] = None
        self._runner: Optional["web.AppRunner"] = None
        self._site: Optional["web.TCPSite"] = None

    # ------------------------------------------------------------------
    # BasePlatformAdapter lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        if not AIOHTTP_AVAILABLE:
            logger.warning("[%s] aiohttp not installed", self.platform.value)
            return False

        # Make sure tui_gateway is importable before we accept connections.
        try:
            from tui_gateway.ws import handle_ws  # noqa: F401  (used in handler)
        except ImportError as exc:
            logger.error("[%s] tui_gateway.ws unavailable: %s", self.platform.value, exc)
            return False

        _register_client_hello()

        # Refuse to start network-accessible without at least one paired
        # client. Mirrors gateway/platforms/api_server.py:2602.
        if is_network_accessible(self._host) and not self._has_any_token():
            logger.error(
                "[%s] Refusing to start: binding to %s requires a paired client. "
                "Run `hermes desktop pair --client-name <name>` first, "
                "or use the default 127.0.0.1.",
                self.platform.value,
                self._host,
            )
            return False

        # Port-conflict probe — fail fast if the port is already in use.
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
                s.settimeout(1)
                s.connect(("127.0.0.1", self._port))
            logger.error(
                "[%s] Port %d already in use. Set platforms.desktop_app.port "
                "in config.yaml or DESKTOP_APP_PORT.",
                self.platform.value,
                self._port,
            )
            return False
        except (ConnectionRefusedError, OSError):
            pass  # port is free

        async def _ws_route(request: "web.Request"):
            # An empty store means no clients have been paired; the
            # network-bind guard above forced loopback in that case, so
            # leaving the gate open here is safe and lets local dev work
            # without a token.
            if not self._tokens.is_empty():
                if _verify_bearer(request, self._tokens) is None:
                    return web.Response(
                        status=401,
                        headers={"WWW-Authenticate": 'Bearer realm="hermes-desktop"'},
                        text="unauthorized",
                    )

            # Each WS connection gets its own dispatcher state so two
            # paired clients can't see or interrupt each other's
            # in-flight sessions. Persisted sessions in state.db remain
            # shared across connections — that's the cross-platform
            # continuity feature.
            from tui_gateway import server as _tg
            from tui_gateway.ws import handle_ws as _handle_ws

            ws = web.WebSocketResponse(heartbeat=30.0)
            await ws.prepare(request)
            conn_state = _tg._DispatcherState()
            conn_state.redact_secrets = True
            state_token = _tg._state_var.set(conn_state)

            try:
                await _handle_ws(_AioHttpWsShim(ws))
            except _WSDisc:
                # Normal client disconnect; handle_ws's own loop catches this
                # too, but if our shim raises before handle_ws's first read we
                # land here. Fine to swallow.
                pass
            except Exception as exc:
                logger.exception("[%s] ws session crashed: %s", self.platform.value, exc)
            finally:
                # Best-effort cleanup of any sessions left open on this
                # connection — close slash workers and drop the global
                # session→state registry entries.
                for sid, sess in list(conn_state.sessions.items()):
                    if (worker := sess.get("slash_worker")) is not None:
                        try:
                            worker.close()
                        except Exception:
                            pass
                    _tg._unregister_session(sid)
                _tg._state_var.reset(state_token)
                if not ws.closed:
                    try:
                        await ws.close()
                    except Exception:
                        pass
            return ws

        async def _handle_health(_request: "web.Request") -> "web.Response":
            return web.json_response(
                {
                    "platform": self.platform.value,
                    "state": "connected" if self._running else "disconnected",
                    "protocol_version": PROTOCOL_VERSION,
                    "host": self._host,
                    "port": self._port,
                }
            )

        try:
            self._app = web.Application()
            self._app.router.add_get(WS_PATH, _ws_route)
            self._app.router.add_get("/health", _handle_health)

            ssl_context = self._build_ssl_context()
            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(
                self._runner, self._host, self._port, ssl_context=ssl_context
            )
            await self._site.start()
        except Exception as exc:
            logger.error("[%s] Failed to start: %s", self.platform.value, exc)
            return False

        self._mark_connected()
        scheme = "wss" if ssl_context else "ws"
        logger.info(
            "[%s] DesktopAppAdapter listening on %s://%s:%d%s",
            self.platform.value,
            scheme,
            self._host,
            self._port,
            WS_PATH,
        )
        if not is_network_accessible(self._host):
            logger.warning(
                "[%s] Loopback bind active — only this host can connect. "
                "For remote access set platforms.desktop_app.host (e.g. a "
                "Tailscale IP) and pair a client first.",
                self.platform.value,
            )
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()
        if self._site:
            try:
                await self._site.stop()
            finally:
                self._site = None
        if self._runner:
            try:
                await self._runner.cleanup()
            finally:
                self._runner = None
        self._app = None

    # ------------------------------------------------------------------
    # BasePlatformAdapter.send — unused on this adapter.
    #
    # All outbound traffic for desktop_app rides on tui_gateway's event emit
    # (message.delta, tool.start, approval.request, …) over the same WS, so
    # the ``send`` path is never the right destination for this platform.
    # We log loudly the first time anything tries it so misrouted cron/delivery
    # code surfaces instead of silently no-oping.
    # ------------------------------------------------------------------

    _SEND_WARNED = False

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> SendResult:
        if not DesktopAppAdapter._SEND_WARNED:
            logger.warning(
                "[%s] send() invoked but desktop_app routes through tui_gateway; "
                "ignoring chat_id=%r (further calls suppressed)",
                self.platform.value,
                chat_id,
            )
            DesktopAppAdapter._SEND_WARNED = True
        return SendResult(success=False, message_id=None, error="send_not_supported_on_desktop_app")

    def _build_ssl_context(self) -> Optional[ssl.SSLContext]:
        """Build an SSL context from configured cert+key paths, or
        return None when TLS is unconfigured. Raises ValueError when
        only one half of the pair is supplied.
        """
        extra = self.config.extra or {}
        tls = extra.get("tls") or {}
        cert_file = tls.get("cert_file") or os.getenv("DESKTOP_APP_TLS_CERT")
        key_file = tls.get("key_file") or os.getenv("DESKTOP_APP_TLS_KEY")

        if not cert_file and not key_file:
            return None
        if cert_file and not key_file:
            raise ValueError("desktop_app TLS: cert_file set without key_file")
        if key_file and not cert_file:
            raise ValueError("desktop_app TLS: key_file set without cert_file")

        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
        return ctx

    async def get_chat_info(self, chat_id: str) -> dict:
        """Minimal chat info — the desktop adapter has no per-chat metadata
        (each WS connection is its own session), so we surface the bind
        address for ``/platforms`` visibility.
        """
        return {
            "name": "Desktop App",
            "type": "desktop",
            "host": self._host,
            "port": self._port,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _has_any_token(self) -> bool:
        """Return True if the TokenStore has at least one paired client."""
        return not self._tokens.is_empty()
