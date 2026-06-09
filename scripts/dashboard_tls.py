#!/usr/bin/env python3
"""Serve `hermes dashboard` over HTTPS without modifying core.

Wraps uvicorn.run to add ssl_certfile/ssl_keyfile, then calls upstream
start_server() unchanged. Run instead of `hermes dashboard`.

  python scripts/dashboard_tls.py --host <host> --port 9119 --cert <host>.crt --key <host>.key

Bind to the hostname the cert is for (or 0.0.0.0). A non-loopback bind engages
the auth gate; keep dashboard.basic_auth configured, do not pass --insecure.
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

    _run = uvicorn.run

    def _tls(app, **kw):
        kw["ssl_certfile"] = cert
        kw["ssl_keyfile"] = key
        kw["proxy_headers"] = False  # direct terminator, not behind a proxy
        return _run(app, **kw)

    uvicorn.run = _tls  # start_server resolves uvicorn.run at call time

    # Register DashboardAuthProvider plugins (basic, nous, …) before the gate
    # check — `hermes dashboard` does this; the bare start_server() does not.
    try:
        from hermes_cli.plugins import discover_plugins

        discover_plugins()
    except Exception as exc:
        print(f"warning: plugin discovery failed: {exc}", file=sys.stderr)

    from hermes_cli.web_server import start_server

    start_server(host=args.host, port=args.port, open_browser=False, allow_public=args.insecure)


if __name__ == "__main__":
    main()
