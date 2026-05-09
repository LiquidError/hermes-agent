"""Bearer-token registry for DesktopAppAdapter.

Stores hashes only; the plaintext token is shown once at pair time
and is not recoverable from the store. Verification uses a
constant-time comparison.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class ClientRecord:
    name: str
    token_hash: str
    last_seen_at: Optional[float] = None


class TokenStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._records: List[ClientRecord] = []
        # Concurrent WS handler tasks share one TokenStore instance and call
        # verify() / touch() / list() in parallel. RLock keeps mutation atomic
        # so iteration never sees a list mid-resize and last_seen_at writes
        # don't race with verify().
        self._lock = threading.RLock()
        self._load_from_disk()

    def is_empty(self) -> bool:
        with self._lock:
            return not self._records

    def list(self) -> List[ClientRecord]:
        with self._lock:
            return list(self._records)

    def add(self, client_name: str, token_plaintext: str) -> None:
        with self._lock:
            self._records.append(
                ClientRecord(name=client_name, token_hash=_hash(token_plaintext))
            )

    def revoke(self, client_name: str) -> bool:
        with self._lock:
            before = len(self._records)
            self._records = [r for r in self._records if r.name != client_name]
            return len(self._records) != before

    def touch(self, client_name: str) -> None:
        """Mark *client_name* as just-seen. No-op for unknown names."""
        now = time.time()
        with self._lock:
            for rec in self._records:
                if rec.name == client_name:
                    rec.last_seen_at = now
                    return

    def verify(self, token_plaintext: Optional[str]) -> Optional[str]:
        if not token_plaintext:
            return None
        candidate = _hash(token_plaintext)
        with self._lock:
            for rec in self._records:
                if hmac.compare_digest(candidate, rec.token_hash):
                    return rec.name
        return None

    def save(self) -> None:
        with self._lock:
            payload = [
                {
                    "name": r.name,
                    "token_hash": r.token_hash,
                    "last_seen_at": r.last_seen_at,
                }
                for r in self._records
            ]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        # 0o600: auth state (client identity hashes, last-seen timestamps).
        # Inheriting the process umask leaves it group/other-readable on most
        # systems. Open the temp file with explicit perms so neither it nor
        # the renamed final file leaks the store to a co-tenant user.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=2)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        tmp.replace(self._path)

    def _load_from_disk(self) -> None:
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text() or "[]")
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, list):
            return
        for item in data:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            token_hash = item.get("token_hash")
            last_seen = item.get("last_seen_at")
            if isinstance(name, str) and isinstance(token_hash, str):
                self._records.append(
                    ClientRecord(
                        name=name,
                        token_hash=token_hash,
                        last_seen_at=last_seen if isinstance(last_seen, (int, float)) else None,
                    )
                )
