"""Stub HKEX server for black-box CLI tests.

Serves canned discovery responses and PDF payloads on localhost so the CLI
under test can be exercised end-to-end without touching the real internet.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

FIXTURES = Path(__file__).parent / "fixtures"


class StubState:
    """Mutable per-test routing table the handler reads."""

    def __init__(self) -> None:
        self.json_response_path: Path | None = None
        self.json_status: int = 200
        self.html_response_path: Path | None = None
        self.pdf_response_path: Path | None = None
        self.pdf_status: int = 200
        self.pdf_fail_first_n: int = 0  # transient 503s before success
        self.pdf_failure_count: int = 0
        self.request_log: list[tuple[str, str]] = []


class _Handler(BaseHTTPRequestHandler):
    state: StubState  # set by start_stub_server

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return  # silence default stderr noise

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        self.state.request_log.append((self.command, self.path))

        if parsed.path.endswith("/titlesearchservlet.do"):
            self._serve_json()
        elif parsed.path.endswith("/titlesearch.xhtml"):
            self._serve_html()
        elif parsed.path.endswith(".pdf"):
            self._serve_pdf()
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_json(self) -> None:
        if self.state.json_status != 200:
            self.send_response(self.state.json_status)
            self.end_headers()
            return
        path = self.state.json_response_path
        if path is None:
            self.send_response(500)
            self.end_headers()
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self) -> None:
        path = self.state.html_response_path
        if path is None:
            self.send_response(404)
            self.end_headers()
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_pdf(self) -> None:
        if self.state.pdf_failure_count < self.state.pdf_fail_first_n:
            self.state.pdf_failure_count += 1
            self.send_response(503)
            self.end_headers()
            return
        path = self.state.pdf_response_path
        if path is None:
            self.send_response(404)
            self.end_headers()
            return
        body = path.read_bytes()
        self.send_response(self.state.pdf_status)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_stub_server() -> tuple[ThreadingHTTPServer, StubState, str]:
    state = StubState()
    handler = type("BoundHandler", (_Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base_url = f"http://{host}:{port}"
    return server, state, base_url
