"""TLS cert/key loading helper shared by the dashboard and gateway surfaces.

Both ``hermes_cli/web_server.py`` and ``gateway/platforms/api_server.py`` load
the same ``~/.hermes/tls/<host>.ts.net.{crt,key}`` pair. Centralising the load
keeps cert handling consistent and gives one place to add features like
``not_after`` introspection or hot reload.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509

EXPIRY_WARNING_DAYS = 14


@dataclasses.dataclass(frozen=True)
class TLSContext:
    """Result of a successful cert/key load."""

    cert_path: Path
    key_path: Path
    not_after: datetime
    expires_soon: bool


def load(cert_path: Path, key_path: Path) -> TLSContext:
    """Load a cert + key pair.

    Raises ``FileNotFoundError`` if either file is missing. The cert is parsed
    only to extract ``not_after``; uvicorn / aiohttp re-read the files
    themselves at TLS-context construction time.
    """
    cert_path = Path(cert_path)
    key_path = Path(key_path)
    if not cert_path.is_file():
        raise FileNotFoundError(f"TLS cert not found: {cert_path}")
    if not key_path.is_file():
        raise FileNotFoundError(f"TLS key not found: {key_path}")
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    not_after = cert.not_valid_after_utc
    expires_soon = (not_after - datetime.now(timezone.utc)) <= timedelta(days=EXPIRY_WARNING_DAYS)
    return TLSContext(
        cert_path=cert_path,
        key_path=key_path,
        not_after=not_after,
        expires_soon=expires_soon,
    )


def expiry_warning(ctx: TLSContext) -> str | None:
    """Human-readable warning when ``ctx.expires_soon`` is true; ``None`` otherwise."""
    if not ctx.expires_soon:
        return None
    return (
        f"TLS cert at {ctx.cert_path} expires at {ctx.not_after.isoformat()}; "
        f"renew with `tailscale cert` before the date passes."
    )
