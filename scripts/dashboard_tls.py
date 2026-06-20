#!/usr/bin/env python3
"""Serve `hermes dashboard` over HTTPS without modifying core.

Patches uvicorn.run to add ssl_certfile/ssl_keyfile, then runs the real
`hermes dashboard` CLI path (full startup: profile, .env, auth-provider
discovery). Run this instead of `hermes dashboard`.

  python scripts/dashboard_tls.py --host <host> --port 9119 --cert <host>.crt --key <host>.key

A non-loopback bind engages the auth gate; keep dashboard.basic_auth configured,
do not pass --insecure.
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
