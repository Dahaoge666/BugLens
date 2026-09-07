"""Serve the BugLens SPA and proxy /v1 requests to the local backend."""

from __future__ import annotations

import argparse
import http.client
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


class SpaProxyHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    backend_host = "127.0.0.1"
    backend_port = 8000

    def do_GET(self) -> None:
        if self.path.startswith("/v1/") or self.path == "/v1":
            self.proxy()
        else:
            super().do_GET()

    def do_POST(self) -> None:
        self.proxy()

    def do_PUT(self) -> None:
        self.proxy()

    def do_DELETE(self) -> None:
        self.proxy()

    def send_head(self):  # type: ignore[no-untyped-def]
        requested = Path(urlsplit(self.path).path.lstrip("/"))
        target = Path(self.directory) / requested
        if not target.exists() and "." not in requested.name:
            self.path = "/index.html"
        return super().send_head()

    def proxy(self) -> None:
        content_length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(content_length) if content_length else None
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP and key.lower() != "host"
        }
        connection = http.client.HTTPConnection(
            self.backend_host, self.backend_port, timeout=3600
        )
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in HOP_BY_HOP:
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            while chunk := response.read(64 * 1024):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException) as exc:
            payload = json.dumps({"error": "backend_unavailable"}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.log_error("backend proxy failed: %s", exc)
        finally:
            connection.close()
            self.close_connection = True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--backend-host", default="127.0.0.1")
    parser.add_argument("--backend-port", type=int, default=8000)
    args = parser.parse_args()

    def handler(*values, **kwargs):  # type: ignore[no-untyped-def]
        return SpaProxyHandler(*values, directory=args.directory, **kwargs)

    SpaProxyHandler.backend_host = args.backend_host
    SpaProxyHandler.backend_port = args.backend_port
    server = ThreadingHTTPServer((args.host, args.port), handler)
    server.daemon_threads = True
    print(f"Serving {args.directory} at http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
