"""``attachment.upload`` — receive base64 bytes from a remote client,
persist them under HERMES_HOME, return the server-side path so the
agent can read the file like any other on-disk attachment.
"""

from __future__ import annotations

import base64

import pytest


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@pytest.fixture
def _isolated_session(monkeypatch, tmp_path):
    """Bypass session.create's agent-build path: stash a minimal session
    dict directly so we can call attachment.upload without an LLM.
    """
    from tui_gateway import server

    sid = "test-session"
    server._state().sessions[sid] = {"attached_images": []}
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield sid
    server._state().sessions.pop(sid, None)


def _call(rid: int, params: dict) -> dict:
    from tui_gateway import server

    return server._methods["attachment.upload"](rid, params)


class TestAttachmentUpload:
    def test_writes_bytes_to_disk_and_returns_path(self, _isolated_session, tmp_path):
        from pathlib import Path

        sid = _isolated_session
        payload = b"hello world\nthis is a test attachment"

        resp = _call(
            1,
            {
                "session_id": sid,
                "filename": "notes.txt",
                "data": _b64(payload),
            },
        )

        assert "result" in resp, resp
        path = Path(resp["result"]["path"])
        assert path.is_file()
        assert path.read_bytes() == payload
        assert resp["result"]["size"] == len(payload)
        assert resp["result"]["filename"] == "notes.txt"

    def test_path_is_under_hermes_home(self, _isolated_session, tmp_path):
        from pathlib import Path

        sid = _isolated_session
        resp = _call(2, {"session_id": sid, "filename": "x.txt", "data": _b64(b"x")})
        path = Path(resp["result"]["path"]).resolve()
        assert tmp_path.resolve() in path.parents

    def test_unique_filenames_for_collisions(self, _isolated_session):
        sid = _isolated_session
        a = _call(3, {"session_id": sid, "filename": "same.txt", "data": _b64(b"AAA")})
        b = _call(4, {"session_id": sid, "filename": "same.txt", "data": _b64(b"BBB")})

        assert a["result"]["path"] != b["result"]["path"]

    def test_rejects_path_traversal_filename(self, _isolated_session):
        sid = _isolated_session
        for bad in ("../escape.txt", "/etc/passwd", "subdir/file.txt", "..\\evil.txt"):
            resp = _call(5, {"session_id": sid, "filename": bad, "data": _b64(b"x")})
            assert "error" in resp, f"{bad!r} should be rejected: {resp}"

    def test_rejects_empty_filename(self, _isolated_session):
        sid = _isolated_session
        resp = _call(6, {"session_id": sid, "filename": "", "data": _b64(b"x")})
        assert "error" in resp

    def test_rejects_invalid_base64(self, _isolated_session):
        sid = _isolated_session
        resp = _call(7, {"session_id": sid, "filename": "x.txt", "data": "!!!not base64!!!"})
        assert "error" in resp

    def test_rejects_oversize_upload(self, _isolated_session, monkeypatch):
        from tui_gateway import server

        monkeypatch.setattr(server, "_ATTACHMENT_MAX_BYTES", 16)
        sid = _isolated_session

        resp = _call(8, {"session_id": sid, "filename": "big.bin", "data": _b64(b"X" * 32)})
        assert "error" in resp
        assert "size" in resp["error"]["message"].lower() or resp["error"]["code"] >= 4000

    def test_rejects_unknown_session(self):
        resp = _call(9, {"session_id": "no-such", "filename": "x.txt", "data": _b64(b"x")})
        assert "error" in resp
