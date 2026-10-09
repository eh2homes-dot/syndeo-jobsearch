"""Serves tests/fixtures/pages on 127.0.0.1 so the page reader and the headless
browser can be tested against real HTTP without leaving the machine."""
from __future__ import annotations

import contextlib
import functools
import http.server
import pathlib
import threading

PAGES = pathlib.Path(__file__).resolve().parent / "fixtures" / "pages"


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_a):  # keep test output clean
        pass

    def do_GET(self):
        # /careers/<name> serves <name>.html, so fixtures have careers-like addresses
        if self.path.startswith("/careers/") and not self.path.startswith("/careers/jobs/"):
            name = self.path.split("?")[0].rsplit("/", 1)[-1]
            if (PAGES / f"{name}.html").exists():
                self.path = f"/{name}.html"
        return super().do_GET()


@contextlib.contextmanager
def serve():
    handler = functools.partial(_Quiet, directory=str(PAGES))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
