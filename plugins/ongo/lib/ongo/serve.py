#!/usr/bin/env python3
"""`ongo site serve` — serve a generated Ongo site over HTTP.

Thin wrapper around `python3 -m http.server` rooted at the site directory.
For production, run this behind a reverse proxy (nginx/caddy) and/or under
systemd (see deploy/ongo-site.service and deploy/README.md); the user points
DNS (ongo.ergodic.xyz) at their server MANUALLY — this script never touches
DNS or deploys anything.

Usage:
    ongo site serve [--dir DIR] [--host HOST] [--port PORT]

Defaults: --dir ./site  --host 0.0.0.0  --port 80
"""

import argparse
import os
import sys

from .errors import OngoArgumentParser, OngoError


def main(argv=None):
    p = OngoArgumentParser(
        prog="ongo site serve",
        description="Serve a generated Ongo site directory over HTTP.",
    )
    p.add_argument("--dir", default="./site", help="site directory")
    p.add_argument("--host", default="0.0.0.0", help="bind host")
    p.add_argument("--port", type=int, default=80, help="bind port")
    args = p.parse_args(argv)

    site_dir = os.path.abspath(args.dir)
    if not os.path.isdir(site_dir):
        raise OngoError(
            "site directory does not exist; run `ongo site build` first",
            code="site-directory-missing",
            exit_code=3,
            details={"path": site_dir},
        )

    from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
    from functools import partial

    handler = partial(SimpleHTTPRequestHandler, directory=site_dir)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    sys.stdout.write(
        f"ongo site serve: serving {site_dir} at "
        f"http://{args.host}:{args.port}/ (Ctrl-C to stop)\n"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stdout.write("\nongo site serve: stopped\n")
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
