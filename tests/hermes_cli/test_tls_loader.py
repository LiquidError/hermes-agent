"""Tests for hermes_cli.tls_loader."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from hermes_cli.tls_loader import TLSContext, expiry_warning, load


def _write_cert(tmp_path: Path, hostname: str, days_valid: int) -> tuple[Path, Path, datetime]:
    """Generate a throwaway self-signed cert + key. Returns (cert, key, not_after)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    not_before = datetime.now(timezone.utc)
    not_after = not_before + timedelta(days=days_valid)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / f"{hostname}.crt"
    key_path = tmp_path / f"{hostname}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path, not_after


def test_load_returns_context_with_paths_and_expiry(tmp_path):
    cert_path, key_path, not_after = _write_cert(tmp_path, "host.ts.net", days_valid=90)
    ctx = load(cert_path, key_path)
    assert isinstance(ctx, TLSContext)
    assert ctx.cert_path == cert_path
    assert ctx.key_path == key_path
    assert abs((ctx.not_after - not_after).total_seconds()) < 2
    assert ctx.expires_soon is False


def test_load_marks_near_expiry(tmp_path):
    cert_path, key_path, _ = _write_cert(tmp_path, "host.ts.net", days_valid=10)
    ctx = load(cert_path, key_path)
    assert ctx.expires_soon is True


def test_load_missing_cert_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load(tmp_path / "missing.crt", tmp_path / "missing.key")


def test_load_missing_key_raises(tmp_path):
    cert_path, _, _ = _write_cert(tmp_path, "host.ts.net", days_valid=90)
    with pytest.raises(FileNotFoundError):
        load(cert_path, tmp_path / "missing.key")


def test_expiry_warning_returns_none_when_far(tmp_path):
    cert_path, key_path, _ = _write_cert(tmp_path, "host.ts.net", days_valid=90)
    ctx = load(cert_path, key_path)
    assert expiry_warning(ctx) is None


def test_expiry_warning_returns_message_when_near(tmp_path):
    cert_path, key_path, _ = _write_cert(tmp_path, "host.ts.net", days_valid=10)
    ctx = load(cert_path, key_path)
    msg = expiry_warning(ctx)
    assert msg is not None
    assert str(cert_path) in msg
    assert "expires" in msg.lower()
