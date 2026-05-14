"""hermes desktop — manage paired clients for DesktopAppAdapter.

Usage:
    hermes desktop pair --client-name <name>
    hermes desktop list
    hermes desktop revoke <name>

Tokens persist to ~/.hermes/desktop_app_tokens.json. The plaintext
bearer is shown to the user once at pair time and is not recoverable
afterwards (only the SHA-256 hash is stored).
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import List, Optional

from gateway.platforms.desktop_app_auth import ClientRecord, TokenStore
from hermes_constants import get_hermes_home


class DuplicateClientError(Exception):
    """Raised when pair() is asked to create a client that already exists."""


def _default_token_file() -> Path:
    return Path(get_hermes_home()) / "desktop_app_tokens.json"


def _resolve_path(token_file: Optional[Path]) -> Path:
    return token_file if token_file is not None else _default_token_file()


def pair(client_name: str, *, token_file: Optional[Path] = None) -> str:
    """Mint a new bearer token for *client_name*, persist its hash, and
    return the plaintext token. The plaintext is shown only here.
    """
    path = _resolve_path(token_file)
    store = TokenStore(path)
    if any(r.name == client_name for r in store.list()):
        raise DuplicateClientError(client_name)
    token = secrets.token_hex(32)
    store.add(client_name, token)
    store.save()
    return token


def list_clients(*, token_file: Optional[Path] = None) -> List[ClientRecord]:
    """Return the paired clients. Never includes plaintext tokens."""
    return TokenStore(_resolve_path(token_file)).list()


def revoke(client_name: str, *, token_file: Optional[Path] = None) -> bool:
    """Remove *client_name* from the store. Returns True on hit, False
    if no such client was paired.
    """
    path = _resolve_path(token_file)
    store = TokenStore(path)
    removed = store.revoke(client_name)
    if removed:
        store.save()
    return removed


# ---------------------------------------------------------------------------
# argparse entry — wired from hermes_cli/main.py via cmd_desktop()
# ---------------------------------------------------------------------------


def desktop_command(args) -> int:
    action = getattr(args, "desktop_action", None)
    if action == "pair":
        return _cmd_pair(args)
    if action in ("list", "ls"):
        return _cmd_list(args)
    if action in ("revoke", "rm"):
        return _cmd_revoke(args)
    print("usage: hermes desktop {pair,list,revoke} ...")
    return 2


def _cmd_pair(args) -> int:
    try:
        token = pair(args.client_name)
    except DuplicateClientError:
        print(f"error: a client named {args.client_name!r} is already paired")
        return 1
    print(token)
    print(
        "\nThis token is shown ONCE. Configure your desktop client with it now;\n"
        "it cannot be recovered later. To revoke: hermes desktop revoke "
        f"{args.client_name}",
        flush=True,
    )
    return 0


def _cmd_list(args) -> int:
    records = list_clients()
    if not records:
        print("(no paired clients)")
        return 0
    for r in records:
        print(r.name)
    return 0


def _cmd_revoke(args) -> int:
    if revoke(args.client_name):
        print(f"revoked {args.client_name}")
        return 0
    print(f"error: no client named {args.client_name!r} is paired")
    return 1
