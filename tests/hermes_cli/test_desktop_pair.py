"""Tests for ``hermes desktop pair`` / ``list`` / ``revoke``."""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# pair — mints a token, prints/returns the plaintext once, persists hash.
# ---------------------------------------------------------------------------


class TestPair:
    def test_returns_plaintext_token(self, tmp_path):
        from hermes_cli.desktop_app import pair

        token = pair("client-a", token_file=tmp_path / "tokens.json")
        assert isinstance(token, str)
        # 32 raw bytes hex-encoded → 64 chars; we accept any length ≥ 32
        # so the implementation has freedom to choose the encoding.
        assert len(token) >= 32

    def test_does_not_persist_plaintext(self, tmp_path):
        from hermes_cli.desktop_app import pair

        f = tmp_path / "tokens.json"
        token = pair("client-a", token_file=f)
        assert token not in f.read_text()

    def test_token_verifies_against_store(self, tmp_path):
        from gateway.platforms.desktop_app_auth import TokenStore
        from hermes_cli.desktop_app import pair

        f = tmp_path / "tokens.json"
        token = pair("client-a", token_file=f)

        # A fresh store reading from disk should accept the minted token.
        assert TokenStore(f).verify(token) == "client-a"

    def test_rejects_duplicate_client_name(self, tmp_path):
        from hermes_cli.desktop_app import DuplicateClientError, pair

        f = tmp_path / "tokens.json"
        pair("client-a", token_file=f)
        with pytest.raises(DuplicateClientError):
            pair("client-a", token_file=f)


# ---------------------------------------------------------------------------
# list_clients — read-only enumeration; never returns plaintext tokens.
# ---------------------------------------------------------------------------


class TestListClients:
    def test_empty_when_no_pairs(self, tmp_path):
        from hermes_cli.desktop_app import list_clients

        assert list_clients(token_file=tmp_path / "tokens.json") == []

    def test_returns_paired_client_names(self, tmp_path):
        from hermes_cli.desktop_app import list_clients, pair

        f = tmp_path / "tokens.json"
        pair("client-a", token_file=f)
        pair("client-b", token_file=f)
        names = sorted(r.name for r in list_clients(token_file=f))
        assert names == ["client-a", "client-b"]

    def test_does_not_expose_plaintext_token(self, tmp_path):
        from hermes_cli.desktop_app import list_clients, pair

        f = tmp_path / "tokens.json"
        token = pair("client-a", token_file=f)
        records = list_clients(token_file=f)
        # ClientRecord exposes name + hash, never plaintext.
        for rec in records:
            assert token not in str(rec)
            assert not hasattr(rec, "token") or getattr(rec, "token", None) is None


# ---------------------------------------------------------------------------
# revoke — removes a client and persists; unknown name returns False.
# ---------------------------------------------------------------------------


class TestRevoke:
    def test_revokes_paired_client(self, tmp_path):
        from gateway.platforms.desktop_app_auth import TokenStore
        from hermes_cli.desktop_app import pair, revoke

        f = tmp_path / "tokens.json"
        token = pair("client-a", token_file=f)
        assert revoke("client-a", token_file=f) is True

        # Persisted: a fresh store from disk no longer accepts the token.
        assert TokenStore(f).verify(token) is None

    def test_unknown_client_returns_false(self, tmp_path):
        from hermes_cli.desktop_app import pair, revoke

        f = tmp_path / "tokens.json"
        pair("client-a", token_file=f)
        assert revoke("never-paired", token_file=f) is False
