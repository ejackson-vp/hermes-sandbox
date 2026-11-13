"""Temporary request-capture proxy for diagnosing provider rejections.

Runs as a sidecar so the kubelet keeps it alive, and sits between the agent and
the real provider. Every request body is written to the capture directory before
being forwarded, so the exact payload Hermes sends is recoverable even when the
provider answers 400.

Delete this file and its container once the offending field is identified; it
exists to answer one question, not to become part of the deployment.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("CAPTURE_UPSTREAM", "https://model.graphn.ai/v1").rstrip("/")
CAPTURE_DIR = os.environ.get("CAPTURE_DIR", "/var/lib/signal-cli/capture")
COUNTER = {"n": 0}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length)

        COUNTER["n"] += 1
        stem = f"{CAPTURE_DIR}/req-{COUNTER['n']:03d}"
        try:
            os.makedirs(CAPTURE_DIR, exist_ok=True)
            with open(f"{stem}.json", "wb") as handle:
                handle.write(body)
            with open(f"{stem}-headers.json", "w") as handle:
                json.dump({k: v for k, v in self.headers.items() if k.lower() != "authorization"}, handle)
            with open(f"{CAPTURE_DIR}/latest", "w") as handle:
                handle.write(f"{stem}.json")
        except OSError as exc:  # never break the turn over a diagnostic
            print(f"capture failed: {exc}", file=sys.stderr, flush=True)

        import httpx

        headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length")}
        try:
            with httpx.Client(timeout=180) as client:
                with client.stream("POST", f"{UPSTREAM}{self.path}", content=body, headers=headers) as upstream:
                    self.send_response(upstream.status_code)
                    for key, value in upstream.headers.items():
                        if key.lower() in ("content-length", "transfer-encoding", "connection"):
                            continue
                        self.send_header(key, value)
                    self.send_header("transfer-encoding", "chunked")
                    self.end_headers()
                    for chunk in upstream.iter_raw():
                        if chunk:
                            self.wfile.write(f"{len(chunk):X}\r\n".encode())
                            self.wfile.write(chunk)
                            self.wfile.write(b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
        except Exception as exc:  # noqa: BLE001
            message = json.dumps({"error": {"message": f"proxy upstream failure: {exc}", "type": "proxy_error"}})
            payload = message.encode()
            self.send_response(502)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def log_message(self, *_args) -> None:
        return


if __name__ == "__main__":
    port = int(os.environ.get("CAPTURE_PORT", "9099"))
    print(f"capture proxy on 127.0.0.1:{port} -> {UPSTREAM}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
