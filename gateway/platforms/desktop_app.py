"""DesktopAppAdapter — exposes the tui_gateway JSON-RPC dispatcher over WebSocket.

This is the Mac-mini-side of the planned Tauri desktop chat. A single aiohttp
web app accepts a WebSocket connection at ``/ws`` and hands it to
``tui_gateway.ws.handle_ws`` — the same dispatcher the Hermes TUI uses,
exposing all 60+ RPC methods (sessions, prompts, slash commands, approvals,
voice, attachments, model switching) over the wire instead of stdio.

Phase 1 (this file): loopback bind only, no auth, single shared dispatcher.
Phase 2 will add bearer-token auth at the WS handshake plus per-connection
dispatcher isolation. Phase 3 will add ``attachment.upload`` and structured
picker queries.

See docs/architecture/integration-overview.md (planned for Phase 4) for the
full protocol + cross-repo contract used by the Tauri client.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket as _socket
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
# client.hello — registered into tui_gateway's dispatcher on first import.
#
# We add it idempotently so multiple imports / reloads don't double-register.
# The handler returns the negotiated capabilities + protocol version; this is
# the only "new" RPC method we add on top of the existing 60+ in tui_gateway.
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
            # Server capabilities the desktop client can rely on. Phase 1 lists
            # the dispatcher capabilities every TUI session has today; later
            # phases extend this (e.g. "attachment.upload", "config.reveal_secret").
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
            ],
            "client_id": client_id,
            "client_version": client_version,
            "client_capabilities": client_caps,
        }
        return _tg_server._ok(rid, result)

    _HELLO_REGISTERED = True


# ---------------------------------------------------------------------------
# WebSocket shim — adapts aiohttp.WebSocketResponse to the starlette-style
# interface tui_gateway.ws.handle_ws expects. Tiny: accept/send_text/
# receive_text/close. Disconnect raises whatever WebSocketDisconnect class
# tui_gateway.ws is currently watching for, so its loop terminates cleanly.
# ---------------------------------------------------------------------------

try:
    from starlette.websockets import WebSocketDisconnect as _WSDisc  # type: ignore[import-not-found]
except ImportError:
    class _WSDisc(Exception):  # type: ignore[no-redef]
        def __init__(self, code: int = 1000, reason: str = "") -> None:
            super().__init__(f"ws disconnect code={code} reason={reason!r}")
            self.code = code
            self.reason = reason


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

        # Register the one new RPC method we own (idempotent).
        _register_client_hello()

        # Phase-1 guard: refuse to start network-accessible without at least
        # one paired client. Mirrors gateway/platforms/api_server.py:2602.
        if is_network_accessible(self._host) and not self._has_any_token():
            logger.error(
                "[%s] Refusing to start: binding to %s requires a paired client. "
                "Run `hermes desktop pair --client-name <name>` (Phase 2) first, "
                "or use the default 127.0.0.1.",
                self.platform.value,
                self._host,
            )
            return False

        # Port-conflict probe — fail fast if 8645 is already in use.
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

        async def _ws_route(request: "web.Request") -> "web.WebSocketResponse":
            ws = web.WebSocketResponse(heartbeat=30.0)
            await ws.prepare(request)
            from tui_gateway.ws import handle_ws as _handle_ws

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

            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()
        except Exception as exc:
            logger.error("[%s] Failed to start: %s", self.platform.value, exc)
            return False

        self._mark_connected()
        logger.info(
            "[%s] DesktopAppAdapter listening on ws://%s:%d%s",
            self.platform.value,
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
        """Return True if the token file exists with non-empty content.

        Phase 1 just checks existence + size; Phase 2 will parse and validate.
        """
        try:
            return self._token_file.is_file() and self._token_file.stat().st_size > 2
        except OSError:
            return False
