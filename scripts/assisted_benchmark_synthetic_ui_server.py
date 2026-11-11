"""Loopback-only read surface for the standalone synthetic UI workload."""
from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path

from assisted_benchmark_synthetic_ui import SyntheticUiWorkload


UI_PAGE = Path(__file__).resolve().parents[1] / "tools" / "assisted-benchmark" / "synthetic-ui" / "index.html"


def create_server(workload: SyntheticUiWorkload, port: int = 0) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ProjectBuddySyntheticUI/1"

        def do_GET(self):
            if self.path == "/":
                self._send(HTTPStatus.OK, UI_PAGE.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/view":
                body = json.dumps(workload.visible_view(), separators=(",", ":")).encode("utf-8")
                self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
            else:
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")

        def do_POST(self):
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Allow", "GET")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send(self, status: HTTPStatus, body: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args):
            return

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = create_server(SyntheticUiWorkload(args.seed), args.port)
    try:
        print(f"Synthetic UI available at http://127.0.0.1:{server.server_port}/")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())