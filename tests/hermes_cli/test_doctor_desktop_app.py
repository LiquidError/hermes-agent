"""``hermes doctor`` surfaces DesktopAppAdapter status."""

from __future__ import annotations

import contextlib
import io
from argparse import Namespace

from gateway.platforms.desktop_app_auth import TokenStore


def _run_doctor() -> str:
    """Run doctor with side effects suppressed; return captured stdout."""
    import hermes_cli.doctor as doctor

    args = Namespace(fix=False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            doctor.run_doctor(args)
        except SystemExit:
            pass
    return buf.getvalue()


class TestDoctorDesktopAppBlock:
    def test_section_hidden_when_unconfigured(self, monkeypatch):
        monkeypatch.delenv("DESKTOP_APP_ENABLED", raising=False)
        out = _run_doctor()
        assert "DesktopApp" not in out and "Desktop App" not in out

    def test_section_appears_when_enabled(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DESKTOP_APP_ENABLED", "true")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        out = _run_doctor()
        assert "Desktop App" in out

    def test_lists_paired_clients_with_last_seen(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DESKTOP_APP_ENABLED", "true")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        store = TokenStore(tmp_path / "desktop_app_tokens.json")
        store.add("laptop", "tok-1")
        store.touch("laptop")
        store.add("tablet", "tok-2")
        store.save()

        out = _run_doctor()
        assert "laptop" in out
        assert "tablet" in out
        # The unconnected client surfaces as "never" (or similar) rather
        # than a misleading timestamp.
        assert "never" in out.lower()

    def test_reports_no_clients_when_unpaired(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DESKTOP_APP_ENABLED", "true")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        out = _run_doctor()
        assert "no paired clients" in out.lower()
