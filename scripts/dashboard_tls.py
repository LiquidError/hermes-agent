#!/usr/bin/env python3
"""Serve `hermes dashboard` over HTTPS without modifying core.

Injects ssl_certfile/ssl_keyfile into uvicorn, then runs the real `hermes
dashboard` CLI path (full startup: profile, .env, auth-provider discovery).
Run this instead of `hermes dashboard`.

  python scripts/dashboard_tls.py --host <host> --port 9119 --cert <host>.crt --key <host>.key

A non-loopback bind engages the auth gate; keep dashboard.basic_auth configured,
do not pass --insecure.

NOTE: core's web_server.start_server() starts uvicorn via `uvicorn.Config(...)` +
`uvicorn.Server(config)` (NOT `uvicorn.run()`), so the SSL kwargs MUST be injected
at the Config layer — patching `uvicorn.run` alone is a silent no-op and the
dashboard stays plain HTTP. We patch Config (the one that matters) and also run
(for any back-compat code path). The `Hermes Web UI → http://…` banner is
hardcoded in core and stays http:// even with TLS on — it is NOT an indicator;
verify with `curl https://<host>:<port>/api/status`.
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    p = argparse.ArgumentParser(description="Run `hermes dashboard` over HTTPS.")
    p.add_argument("--host", default=os.environ.get("HERMES_DASHBOARD_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("HERMES_DASHBOARD_PORT", "9119")))
    p.add_argument("--cert", default=os.environ.get("HERMES_DASHBOARD_TLS_CERT"))
    p.add_argument("--key", default=os.environ.get("HERMES_DASHBOARD_TLS_KEY"))
    p.add_argument("--insecure", action="store_true", help="skip the auth gate")
    args = p.parse_args()

    if not args.cert or not args.key:
        sys.exit("error: --cert and --key required (or HERMES_DASHBOARD_TLS_CERT/_KEY)")
    cert = os.path.abspath(os.path.expanduser(args.cert))
    key = os.path.abspath(os.path.expanduser(args.key))
    for path in (cert, key):
        if not os.path.isfile(path):
            sys.exit(f"error: not found: {path}")

    import uvicorn

    def _with_tls(kw):
        kw["ssl_certfile"] = cert
        kw["ssl_keyfile"] = key
        # We are the TLS terminator facing the client directly (no reverse
        # proxy), so the request scheme is natively https and X-Forwarded-*
        # must not be trusted — overrides core's proxy_headers choice.
        kw["proxy_headers"] = False
        return kw

    # Primary: core builds the server with uvicorn.Config(app, ...) +
    # uvicorn.Server(config). Inject SSL into Config so the socket actually
    # terminates TLS. web_server uses `uvicorn.Config` attribute access, so
    # patching the module attribute takes regardless of import timing.
    _Config = uvicorn.Config

    def _tls_config(*args, **kw):
        return _Config(*args, **_with_tls(kw))

    uvicorn.Config = _tls_config

    # Back-compat: also cover any path that still calls uvicorn.run(app, ...).
    _run = uvicorn.run

    def _tls_run(app, **kw):
        return _run(app, **_with_tls(kw))

    uvicorn.run = _tls_run

    # Run the real `hermes dashboard` so its full startup (profile resolution,
    # .env load, auth-provider discovery) happens. Set argv before importing
    # main — its module-level init reads it.
    sys.argv = ["hermes", "dashboard", "--no-open", "--host", args.host, "--port", str(args.port)]
    if args.insecure:
        sys.argv.append("--insecure")

    from hermes_cli.main import main as hermes_main

    hermes_main()


if __name__ == "__main__":
    main()
