#!/usr/bin/env python3
"""A dev server for the player: serves the page you are editing, with a real stream behind it.

    python3 scripts/devserver.py                 # http://127.0.0.1:8099/
    python3 scripts/devserver.py --upstream http://<host>:8101 --port 8099

Why it exists: the page is normally served by the daemon, so every CSS tweak meant a deploy to the
headset. Here the HTML comes from this checkout (reload = see it), while `/status`, the HLS segments and
`/api/settings` GET are proxied from a real daemon, so the overlays sit on a real picture.

`POST /api/settings` is **not** proxied: it answers locally against an in-memory copy, so poking the panel
while developing cannot change the stream someone is watching. Everything else that is not the page is
passed through, with Range support, so the video element behaves as it does in the real thing.
"""

import argparse
import json
import os
import socketserver
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "..", "server", "web", "index.html")

# settings the daemon cannot change without restarting ffmpeg — mirrors server/settings.py
RESTART_FIELDS = {"bitrate", "fps", "layout", "threads", "nice", "adapt", "record", "record_time"}


class DevServer(ThreadingHTTPServer):
    """HTTPServer.server_bind() resolves the host name, which hangs for ~35 s on a machine whose own name
    does not resolve. The page never needs the FQDN, so bind without the lookup."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = str(self.server_address[0]), int(self.server_address[1])


class Dev(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = "http://127.0.0.1:8101"
    whep = "http://127.0.0.1:8889/vr/whep"      # MediaMTX behind the same host; proxied to stay same-origin
    fake = {}          # the last settings the panel was shown; only this process ever writes it

    def log_message(self, format, *args):        # noqa: A002 - the stdlib spells it this way
        sys.stderr.write(f"  {self.address_string()} {format % args}\n")

    # ------------------------------------------------------------------ helpers
    def _send(self, code, body: bytes, ctype: str, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, payload):
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _proxy(self, method="GET"):
        url = self.upstream + self.path
        req = urllib.request.Request(url, method=method)
        for header in ("Range", "If-Range", "Accept"):
            if self.headers.get(header):
                req.add_header(header, self.headers[header])
        try:
            with urllib.request.urlopen(req, timeout=20) as up:
                body = b"" if method == "HEAD" else up.read()
                extra = {}
                for header in ("Content-Range", "Accept-Ranges", "Cache-Control"):
                    if up.headers.get(header):
                        extra[header] = up.headers[header]
                self._send(up.status, body, up.headers.get("Content-Type", "application/octet-stream"), extra)
        except urllib.error.HTTPError as err:
            body = err.read()
            self._send(err.code, body, err.headers.get("Content-Type", "text/plain"))
        except Exception as err:                                   # upstream down
            self._json(502, {"error": f"upstream {self.upstream} unreachable: {err}"})

    def _whep(self, method):
        """Proxy the WebRTC handshake too, so the dev page is same-origin and needs no CORS luck."""
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(self.whep, data=body, method=method)
        if self.headers.get("Content-Type"):
            req.add_header("Content-Type", self.headers["Content-Type"])
        try:
            with urllib.request.urlopen(req, timeout=20) as up:
                extra = {}
                if up.headers.get("Location"):
                    extra["Location"] = up.headers["Location"]
                self._send(up.status, up.read(), up.headers.get("Content-Type", "application/sdp"), extra)
        except urllib.error.HTTPError as err:
            self._send(err.code, err.read(), err.headers.get("Content-Type", "text/plain"))
        except Exception as err:
            self._json(502, {"error": f"WHEP upstream {self.whep} unreachable: {err}"})

    # ------------------------------------------------------------------ routes
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            with open(PAGE, "rb") as fh:
                html = fh.read()
            # what the daemon substitutes when it serves the page
            html = (html.replace(b"__TOKEN__", b"")
                        .replace(b'"__WEBRTC__"', b'(location.origin + "/vr")')
                        .replace(b"__LAYOUT__", b"mono-flat"))
            return self._send(200, html, "text/html; charset=utf-8")
        if path == "/api/settings" and self.fake:
            return self._json(200, self.fake)
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        return self._proxy()

    do_HEAD = do_GET

    def do_DELETE(self):
        if urllib.parse.urlparse(self.path).path == "/vr/whep":
            return self._whep("DELETE")
        self._json(404, {"error": "nothing here"})

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain", {"Allow": "GET, HEAD, POST, DELETE, OPTIONS"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/vr/whep":
            return self._whep("POST")
        if path != "/api/settings":
            return self._json(404, {"error": "the dev server only fakes /api/settings"})
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            change = json.loads(raw or b"{}")
        except ValueError:
            return self._json(400, {"error": "body is not JSON"})
        if not self.fake:                                          # first change: learn the real values
            try:
                with urllib.request.urlopen(self.upstream + "/api/settings", timeout=10) as up:
                    self.fake = json.loads(up.read())
            except Exception as err:
                return self._json(502, {"error": f"cannot read the real settings: {err}"})
        unknown = set(change) - set(self.fake.get("settings", {}))
        if unknown:
            return self._json(400, {"error": "unknown setting(s): " + ", ".join(sorted(unknown))})
        restarted = bool(set(change) & RESTART_FIELDS)
        self.fake["settings"].update(change)
        self._json(200, {"ok": True, "settings": self.fake["settings"], "restarted": restarted,
                         "notes": ["dev server: nothing was changed on the headset"]})


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--upstream", default="http://127.0.0.1:8101",
                    help="a running daemon whose picture and /status are proxied")
    ap.add_argument("--whep", default="",
                    help="MediaMTX WHEP endpoint (default: same host as --upstream, port 8889)")
    args = ap.parse_args()

    Dev.upstream = args.upstream.rstrip("/")
    host = urllib.parse.urlparse(Dev.upstream).hostname or "127.0.0.1"
    Dev.whep = args.whep or f"http://{host}:8889/vr/whep"
    server = DevServer((args.bind, args.port), Dev)
    print(f"dev page  http://127.0.0.1:{args.port}/")
    print(f"upstream  {Dev.upstream}   (POST /api/settings stays local)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
