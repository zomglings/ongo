#!/usr/bin/env python3
"""`ongo site serve` — serve a generated Ongo site over HTTP.

Thin wrapper around `python3 -m http.server` rooted at the site directory.
For production, run this behind a reverse proxy (nginx/caddy) and/or under
systemd (see deploy/ongo-site.service and deploy/README.md); the user points
DNS (ongo.ergodic.xyz) at their server MANUALLY — this script never touches
DNS or deploys anything.

Usage:
    ongo site serve [--dir DIR] [--host HOST] [--port PORT] [--access-log PATH]

Defaults: --dir ./site  --host 0.0.0.0  --port 80  (requests logged to stderr)

With --access-log, each request is appended to PATH in Combined Log Format
(client, UTC time, request line, status, bytes, referer, user agent) instead
of stderr. The file is reopened automatically after logrotate moves it.
"""

import argparse
import logging
import logging.handlers
import os
import sys
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from .errors import OngoArgumentParser, OngoError


def _escape(value):
    """Escape a client-controlled value for one quoted access-log field."""
    out = []
    for char in value:
        if char in ('"', "\\"):
            out.append("\\" + char)
        elif " " <= char <= "~":
            out.append(char)
        else:
            out.append("\\x%02x" % ord(char) if ord(char) < 0x100 else "\\u%04x" % ord(char))
    return "".join(out)


class AccessLogHandler(SimpleHTTPRequestHandler):
    """Static file handler that writes Combined Log Format lines to a logger.

    The line is written once the request finishes, so the byte count comes
    from the Content-Length that was actually sent.
    """

    def __init__(self, *args, access_logger, **kwargs):
        self.access_logger = access_logger
        super().__init__(*args, **kwargs)

    def handle_one_request(self):
        self._access_status = None
        self._access_bytes = "-"
        try:
            super().handle_one_request()
        finally:
            if self._access_status is not None:
                self._write_access_line()

    def log_request(self, code="-", size="-"):
        self._access_status = getattr(code, "value", code)

    def log_error(self, format, *args):
        # send_error() reports every 4xx/5xx this way; the access line already
        # records the status, so keep stderr for genuine errors only.
        if format == "code %d, message %s":
            return
        super().log_error(format, *args)

    def send_header(self, keyword, value):
        if keyword.lower() == "content-length" and getattr(self, "command", None) != "HEAD":
            self._access_bytes = str(value)
        super().send_header(keyword, value)

    def _write_access_line(self):
        now = time.gmtime()
        stamp = "%02d/%s/%04d:%02d:%02d:%02d +0000" % (
            now.tm_mday, self.monthname[now.tm_mon], now.tm_year,
            now.tm_hour, now.tm_min, now.tm_sec,
        )
        headers = getattr(self, "headers", None)
        referer = headers.get("Referer", "-") if headers else "-"
        agent = headers.get("User-Agent", "-") if headers else "-"
        self.access_logger.info(
            '%s - - [%s] "%s" %s %s "%s" "%s"',
            self.client_address[0],
            stamp,
            _escape(getattr(self, "requestline", "")),
            self._access_status,
            self._access_bytes,
            _escape(referer),
            _escape(agent),
        )


def open_access_log(path):
    """Return a logger appending to `path`, reopened if logrotate moves it."""
    try:
        handler = logging.handlers.WatchedFileHandler(path, encoding="utf-8")
    except OSError as exc:
        raise OngoError(
            f"cannot open access log: {exc.strerror}",
            code="access-log-unwritable",
            exit_code=3,
            details={"path": path},
        )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger(f"ongo.access.{id(handler)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    return logger


def make_server(site_dir, host, port, access_log=None):
    """Build (but do not start) the HTTP server for `site_dir`."""
    if access_log:
        handler = partial(
            AccessLogHandler,
            directory=site_dir,
            access_logger=open_access_log(access_log),
        )
    else:
        handler = partial(SimpleHTTPRequestHandler, directory=site_dir)
    return ThreadingHTTPServer((host, port), handler)


def main(argv=None):
    p = OngoArgumentParser(
        prog="ongo site serve",
        description="Serve a generated Ongo site directory over HTTP.",
    )
    p.add_argument("--dir", default="./site", help="site directory")
    p.add_argument("--host", default="0.0.0.0", help="bind host")
    p.add_argument("--port", type=int, default=80, help="bind port")
    p.add_argument(
        "--access-log",
        metavar="PATH",
        help="append requests to PATH in Combined Log Format instead of stderr",
    )
    args = p.parse_args(argv)

    site_dir = os.path.abspath(args.dir)
    if not os.path.isdir(site_dir):
        raise OngoError(
            "site directory does not exist; run `ongo site build` first",
            code="site-directory-missing",
            exit_code=3,
            details={"path": site_dir},
        )

    access_log = os.path.abspath(args.access_log) if args.access_log else None
    httpd = make_server(site_dir, args.host, args.port, access_log)
    sys.stdout.write(
        f"ongo site serve: serving {site_dir} at "
        f"http://{args.host}:{args.port}/ (Ctrl-C to stop)\n"
    )
    if access_log:
        sys.stdout.write(f"ongo site serve: access log {access_log}\n")
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stdout.write("\nongo site serve: stopped\n")
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
