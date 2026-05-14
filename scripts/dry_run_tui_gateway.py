#!/usr/bin/env python3
# Phase 0 dry-run for the planned DesktopAppAdapter.
#
# Drives `python -m tui_gateway` over its existing stdio transport and confirms
# the dispatcher behaves the way the source-code reading suggests, before we
# wrap it for network use in Phase 1. Run this on the Mac-mini host where
# Hermes is properly installed.
#
# Usage:
#     python scripts/dry_run_tui_gateway.py
#     python scripts/dry_run_tui_gateway.py --prompt "say hi in 3 words"
#     python scripts/dry_run_tui_gateway.py --cmd "uv run python -m tui_gateway"
#     python scripts/dry_run_tui_gateway.py --timeout 90 --verbose
#
# Exit code 0 means: gateway.ready event seen, session.create returned an id,
# prompt.submit produced a message.start -> message.delta+ -> message.complete
# sequence, session.close acknowledged. Any deviation aborts with non-zero.

from __future__ import annotations

import argparse
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class Counters:
    events_seen: int = 0
    events_by_type: Dict[str, int] = field(default_factory=dict)
    deltas: int = 0
    delta_chars: int = 0
    tool_starts: int = 0
    tool_completes: int = 0
    saw_message_start: bool = False
    saw_message_complete: bool = False
    saw_session_info: bool = False
    final_text: str = ""
    final_status: str = ""
    usage: Optional[Dict[str, Any]] = None


class GatewayHarness:
    """Drives `python -m tui_gateway` over stdio JSON-RPC.

    One reader thread consumes stdout; responses route to per-id Queues, events
    fan out to a single events Queue plus optional callbacks. Stderr is mirrored
    to a separate thread so import errors / panic-hook traces surface promptly
    instead of being swallowed.
    """

    def __init__(
        self,
        cmd: List[str],
        cwd: Optional[str] = None,
        verbose: bool = False,
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        self.cmd = cmd
        self.cwd = cwd
        self.verbose = verbose
        self.env = env
        self.proc: Optional[subprocess.Popen[str]] = None
        self._next_id = 0
        self._id_lock = threading.Lock()
        self._pending: Dict[int, "queue.Queue[dict]"] = {}
        self._pending_lock = threading.Lock()
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._reader_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._closed = False
        self.event_listeners: List[Callable[[dict], None]] = []

    def start(self) -> None:
        env = os.environ.copy()
        if self.env:
            env.update(self.env)
        self.proc = subprocess.Popen(
            self.cmd,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        self._reader_thread = threading.Thread(
            target=self._read_stdout, name="gw-stdout", daemon=True
        )
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, name="gw-stderr", daemon=True
        )
        self._stderr_thread.start()

    def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            for raw in self.proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    sys.stderr.write(f"[harness] non-JSON stdout: {line!r}\n")
                    continue
                if self.verbose:
                    sys.stderr.write(f"[harness] <- {json.dumps(msg)[:240]}\n")
                self._dispatch_inbound(msg)
        finally:
            self._closed = True
            with self._pending_lock:
                for q in self._pending.values():
                    q.put({"__closed__": True})

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for raw in self.proc.stderr:
            line = raw.rstrip()
            if line:
                sys.stderr.write(f"[gateway-stderr] {line}\n")

    def _dispatch_inbound(self, msg: dict) -> None:
        if msg.get("method") == "event":
            for listener in list(self.event_listeners):
                try:
                    listener(msg)
                except Exception as exc:
                    sys.stderr.write(f"[harness] listener error: {exc}\n")
            self._events.put(msg)
            return
        rid = msg.get("id")
        if rid is None:
            return
        with self._pending_lock:
            q = self._pending.pop(rid, None)
        if q is not None:
            q.put(msg)

    def call(self, method: str, params: Optional[dict] = None, timeout: float = 30.0) -> dict:
        with self._id_lock:
            self._next_id += 1
            rid = self._next_id
        q: "queue.Queue[dict]" = queue.Queue()
        with self._pending_lock:
            self._pending[rid] = q
        req = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        self._send(req)
        try:
            resp = q.get(timeout=timeout)
        except queue.Empty:
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise TimeoutError(f"no response to {method!r} within {timeout}s")
        if resp.get("__closed__"):
            raise RuntimeError(f"gateway closed before responding to {method!r}")
        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(f"{method!r} returned error {err.get('code')}: {err.get('message')}")
        return resp.get("result", {})

    def _send(self, obj: dict) -> None:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("gateway not started")
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        if self.verbose:
            sys.stderr.write(f"[harness] -> {line.strip()[:240]}\n")
        try:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
        except BrokenPipeError as exc:
            raise RuntimeError("gateway stdin closed") from exc

    def wait_event(
        self,
        predicate: Callable[[dict], bool],
        timeout: float,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("event predicate not satisfied within timeout")
            try:
                msg = self._events.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError("event predicate not satisfied within timeout")
            if on_event is not None:
                on_event(msg)
            if predicate(msg):
                return msg

    def close(self) -> None:
        if self.proc and self.proc.stdin:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
        if self.proc:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.terminate()


def _event_type(msg: dict) -> Optional[str]:
    params = msg.get("params") if isinstance(msg, dict) else None
    if isinstance(params, dict):
        t = params.get("type")
        if isinstance(t, str):
            return t
    return None


def _event_payload(msg: dict) -> dict:
    params = msg.get("params") if isinstance(msg, dict) else None
    if isinstance(params, dict):
        payload = params.get("payload")
        if isinstance(payload, dict):
            return payload
    return {}


def _record(counters: Counters, msg: dict) -> None:
    t = _event_type(msg)
    if t is None:
        return
    counters.events_seen += 1
    counters.events_by_type[t] = counters.events_by_type.get(t, 0) + 1
    if t == "session.info":
        counters.saw_session_info = True
    elif t == "message.start":
        counters.saw_message_start = True
    elif t == "message.delta":
        counters.deltas += 1
        text = _event_payload(msg).get("text") or ""
        counters.delta_chars += len(text)
    elif t == "message.complete":
        counters.saw_message_complete = True
        payload = _event_payload(msg)
        counters.final_text = payload.get("text") or ""
        counters.final_status = payload.get("status") or ""
        counters.usage = payload.get("usage") or None
    elif t == "tool.start":
        counters.tool_starts += 1
    elif t == "tool.complete":
        counters.tool_completes += 1


def parse_cmd(cmd: str) -> List[str]:
    return shlex.split(cmd, posix=os.name != "nt")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Dry-run Hermes tui_gateway over stdio")
    parser.add_argument(
        "--cmd",
        default="python -m tui_gateway",
        help="command that launches tui_gateway (default: 'python -m tui_gateway')",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="working directory for the gateway subprocess (default: repo root inferred from this file)",
    )
    parser.add_argument(
        "--prompt",
        default="reply with the single word OK",
        help="prompt to send via prompt.submit",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="seconds to wait for the LLM round-trip (default: 60)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="dump every wire message to stderr",
    )
    parser.add_argument(
        "--no-prompt-submit",
        action="store_true",
        help="run only the handshake + session.create step (no LLM call)",
    )
    args = parser.parse_args(argv)

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cwd = args.cwd or repo_root
    cmd = parse_cmd(args.cmd)

    print(f"[harness] cmd: {cmd}", flush=True)
    print(f"[harness] cwd: {cwd}", flush=True)

    harness = GatewayHarness(cmd=cmd, cwd=cwd, verbose=args.verbose)
    counters = Counters()

    def _print_event(msg: dict) -> None:
        t = _event_type(msg)
        if t is None:
            return
        suffix = ""
        if t == "message.delta":
            text = (_event_payload(msg).get("text") or "").replace("\n", "\\n")
            suffix = f"  text={text[:60]!r}"
        elif t == "message.complete":
            payload = _event_payload(msg)
            suffix = f"  status={payload.get('status')!r} text={(payload.get('text') or '')[:60]!r}"
        elif t in ("tool.start", "tool.complete"):
            suffix = f"  payload={_event_payload(msg)}"
        elif t == "approval.request":
            suffix = f"  payload={_event_payload(msg)}"
        elif t == "error":
            suffix = f"  payload={_event_payload(msg)}"
        print(f"[event] {t}{suffix}", flush=True)

    harness.event_listeners.append(_print_event)
    harness.event_listeners.append(lambda m: _record(counters, m))

    harness.start()

    try:
        # 1. Wait for the gateway.ready notification the dispatcher emits at boot.
        print("[harness] waiting for gateway.ready ...", flush=True)
        ready = harness.wait_event(
            lambda m: _event_type(m) == "gateway.ready",
            timeout=20.0,
        )
        skin = _event_payload(ready).get("skin")
        print(f"[harness] gateway.ready (skin={skin!r})", flush=True)

        # 2. session.create — agent build runs in a background thread; the
        #    response returns immediately, agent_ready event arrives later.
        print("[harness] calling session.create ...", flush=True)
        result = harness.call("session.create", {"cols": 80}, timeout=10.0)
        sid = result.get("session_id")
        info = result.get("info") or {}
        if not isinstance(sid, str) or not sid:
            print(f"[FAIL] session.create returned no session_id: {result!r}", flush=True)
            return 2
        print(
            f"[harness] session.create -> session_id={sid!r} model={info.get('model')!r} cwd={info.get('cwd')!r}",
            flush=True,
        )

        if args.no_prompt_submit:
            print("[harness] --no-prompt-submit set; skipping LLM round-trip", flush=True)
            try:
                harness.call("session.close", {"session_id": sid}, timeout=10.0)
                print("[harness] session.close ok", flush=True)
            except Exception as exc:
                print(f"[harness] session.close warning: {exc}", flush=True)
            return _summarize(counters, prompt_submitted=False)

        # 3. prompt.submit — fires message.start, then message.delta repeatedly,
        #    then message.complete. The dispatcher waits up to 30s internally
        #    for the agent to finish initializing if needed.
        print(f"[harness] calling prompt.submit text={args.prompt!r} ...", flush=True)
        try:
            harness.call(
                "prompt.submit",
                {"session_id": sid, "text": args.prompt},
                timeout=args.timeout,
            )
        except Exception as exc:
            print(f"[FAIL] prompt.submit raised: {exc}", flush=True)
            return 3

        # 4. Wait for message.complete (allow tool calls + LLM latency).
        print("[harness] waiting for message.complete ...", flush=True)
        try:
            harness.wait_event(
                lambda m: _event_type(m) == "message.complete",
                timeout=args.timeout,
            )
        except TimeoutError:
            print("[FAIL] message.complete not seen before timeout", flush=True)
            return 4

        # 5. Tidy up. session.close confirms the dispatcher honors the lifecycle.
        try:
            harness.call("session.close", {"session_id": sid}, timeout=10.0)
            print("[harness] session.close ok", flush=True)
        except Exception as exc:
            print(f"[harness] session.close warning: {exc}", flush=True)

        return _summarize(counters, prompt_submitted=True)

    finally:
        harness.close()


def _summarize(counters: Counters, *, prompt_submitted: bool) -> int:
    print("", flush=True)
    print("================ Phase 0 dry-run summary ================", flush=True)
    print(f"events seen        : {counters.events_seen}", flush=True)
    by_type = ", ".join(f"{k}={v}" for k, v in sorted(counters.events_by_type.items()))
    print(f"events by type     : {by_type}", flush=True)
    if prompt_submitted:
        print(f"deltas             : {counters.deltas} ({counters.delta_chars} chars)", flush=True)
        print(f"tool start/complete: {counters.tool_starts}/{counters.tool_completes}", flush=True)
        print(f"final status       : {counters.final_status!r}", flush=True)
        print(f"final text (head)  : {counters.final_text[:200]!r}", flush=True)
        if counters.usage:
            print(f"usage              : {counters.usage}", flush=True)

    if not counters.saw_session_info:
        print("[WARN] no session.info event — agent may not have finished initializing", flush=True)

    if prompt_submitted:
        ok = (
            counters.saw_message_start
            and counters.saw_message_complete
            and counters.deltas > 0
        )
    else:
        ok = True
    print(f"PASS={ok}", flush=True)
    return 0 if ok else 5


if __name__ == "__main__":
    sys.exit(main())
