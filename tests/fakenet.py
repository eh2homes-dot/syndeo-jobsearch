"""A fake network for offline tests.

    with fakenet.serve(routes) as calls:
        ...

`routes` maps "METHOD url-regex" to {"json": ...} | {"text": "..."} with an
optional "status" and "url" (the final address after redirects). The first
matching route answers; anything unmatched is a 404. `calls` collects the
"METHOD url" of every request made, so a test can assert what was fetched.
"""
from __future__ import annotations

import contextlib
import json as _json
import re
import time

import requests
from urllib.parse import urlparse


class FakeResponse:
    def __init__(self, url, spec):
        self.status_code = int(spec.get("status", 200))
        self.url = spec.get("url", url)
        self.headers = spec.get("headers", {})
        self._json = spec.get("json")
        self.text = spec["text"] if "text" in spec else (_json.dumps(self._json) if "json" in spec else "")
        self.content = self.text.encode("utf-8")

    def json(self):
        if self._json is None:
            return _json.loads(self.text)      # raises ValueError on HTML, like the real thing
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error for url: {self.url}", response=self)


@contextlib.contextmanager
def serve(routes: dict, passthrough=("127.0.0.1", "localhost")):
    """Requests to `passthrough` hosts go to the real network (a local test server)."""
    compiled = [(k.split(" ", 1)[0], re.compile(k.split(" ", 1)[1]), v)
                for k, v in routes.items() if not k.startswith("_")]
    calls: list = []

    def answer(method, url, **_kw):
        calls.append(f"{method.upper()} {url}")
        for m, rx, spec in compiled:
            if m == method.upper() and rx.search(url):
                return FakeResponse(url, spec)
        return FakeResponse(url, {"status": 404, "text": "not found"})

    saved = (requests.Session.request, requests.get, requests.post, time.sleep)

    def session_request(self, method, url, **kw):
        if urlparse(url).hostname in passthrough:
            return saved[0](self, method, url, **kw)
        return answer(method, url, **kw)

    requests.Session.request = session_request
    requests.get = lambda url, **kw: answer("GET", url, **kw)
    requests.post = lambda url, **kw: answer("POST", url, **kw)
    time.sleep = lambda *_a, **_k: None
    try:
        yield calls
    finally:
        requests.Session.request, requests.get, requests.post, time.sleep = saved
