"""TLS / WSS support for DesktopAppAdapter.

The adapter accepts a cert + key pair via ``platforms.desktop_app.tls``
in config or ``DESKTOP_APP_TLS_CERT`` / ``DESKTOP_APP_TLS_KEY`` env
vars and serves WSS instead of plain WS.
"""

from __future__ import annotations

import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.desktop_app import DesktopAppAdapter


@pytest.fixture
def self_signed(tmp_path) -> tuple[Path, Path]:
    """Return (cert_path, key_path) for a freshly minted self-signed pair."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "hermes-test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class TestBuildSslContext:
    def test_no_tls_config_returns_none(self):
        adapter = DesktopAppAdapter(PlatformConfig(enabled=True))
        assert adapter._build_ssl_context() is None

    def test_only_cert_without_key_raises(self, self_signed):
        cert_path, _ = self_signed
        adapter = DesktopAppAdapter(
            PlatformConfig(
                enabled=True,
                extra={"tls": {"cert_file": str(cert_path)}},
            ),
        )
        with pytest.raises(ValueError, match="key_file"):
            adapter._build_ssl_context()

    def test_only_key_without_cert_raises(self, self_signed):
        _, key_path = self_signed
        adapter = DesktopAppAdapter(
            PlatformConfig(
                enabled=True,
                extra={"tls": {"key_file": str(key_path)}},
            ),
        )
        with pytest.raises(ValueError, match="cert_file"):
            adapter._build_ssl_context()

    def test_loads_cert_and_key(self, self_signed):
        cert_path, key_path = self_signed
        adapter = DesktopAppAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "tls": {
                        "cert_file": str(cert_path),
                        "key_file": str(key_path),
                    }
                },
            ),
        )
        ctx = adapter._build_ssl_context()
        assert isinstance(ctx, ssl.SSLContext)

    def test_env_vars_used_when_extra_absent(self, self_signed, monkeypatch):
        cert_path, key_path = self_signed
        monkeypatch.setenv("DESKTOP_APP_TLS_CERT", str(cert_path))
        monkeypatch.setenv("DESKTOP_APP_TLS_KEY", str(key_path))

        adapter = DesktopAppAdapter(PlatformConfig(enabled=True))
        ctx = adapter._build_ssl_context()
        assert isinstance(ctx, ssl.SSLContext)

    def test_missing_file_raises_on_load(self, tmp_path):
        adapter = DesktopAppAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "tls": {
                        "cert_file": str(tmp_path / "missing-cert.pem"),
                        "key_file": str(tmp_path / "missing-key.pem"),
                    }
                },
            ),
        )
        with pytest.raises((FileNotFoundError, ssl.SSLError)):
            adapter._build_ssl_context()
