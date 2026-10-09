"""A headless browser for careers pages that only show their jobs after scripts run.

Used as the last step of reading a page, never the first: a plain download is
tried before this. What it adds over a plain download:

  * the page as a visitor sees it, after its scripts have loaded the job list,
    including lists that sit inside an embedded frame;
  * the addresses the page itself called while loading, which is how the job
    system behind a careers page is recognised (the page loads its jobs from it);
  * clicking "Load more" / "Next" until the list stops growing.

Playwright is optional. If it isn't installed the searches still run; pages that
need it are reported as needing a job-board link instead.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger("jobsearch.browser")


class BrowserUnavailable(Exception):
    """The browser couldn't be used for this page. Never the page's fault as far
    as we can tell: no browser installed, the run's budget is spent, or the page
    timed out. Callers treat it as "couldn't check", not as "no jobs"."""


@dataclass
class Rendered:
    url: str
    final_url: str = ""
    frames: list = field(default_factory=list)      # [(frame_url, html)], main frame first
    text: str = ""                                   # what a visitor can read on the page (all frames)
    status: int = 0                                  # HTTP status of the page itself (0 if unknown)
    requests: list = field(default_factory=list)    # every address the page called
    clicks: int = 0                                  # "load more" / "next" clicks made
    stopped: str = ""                                # why paging stopped, when it was cut short


# Finds the "show me more jobs" controls on a page and clicks the one numbered
# `skip` (0 = the best guess). "Load more" style buttons rank ahead of "Next",
# because a page can also have a "Next" on a photo carousel. Returns the label
# of what was clicked, or "" when there is no such control.
_CLICK_MORE = r"""
(skip) => {
  const more = /^(load|show|view|see)\s+(more|all)(\s+(jobs|positions|openings|roles|results|opportunities|listings))?$|^more\s+(jobs|positions|openings|roles|results|listings)$/i;
  const next = /^next(\s+page)?$|^(›|»|→|>|>>)$/i;
  const label = /^(next|next page|go to next page|load more|show more)(\s+(jobs|results))?$/i;
  const found = [[], []];
  for (const el of document.querySelectorAll('button, a, [role=button], input[type=button], input[type=submit]')) {
    const t = (el.innerText || el.value || '').trim().replace(/\s+/g, ' ');
    const l = (el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
    const rank = more.test(t) || /^(load|show) more/i.test(l) ? 0 : (next.test(t) || label.test(l) ? 1 : -1);
    if (rank < 0) continue;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
    if (/\bdisabled\b/.test(String(el.className || '')) || el.closest('.disabled')) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none') continue;
    found[rank].push([el, t || l || 'more']);
  }
  const all = found[0].concat(found[1]);
  if (skip >= all.length) return '';
  all[skip][0].scrollIntoView({block: 'center'});
  all[skip][0].click();
  return all[skip][1];
}
"""
MAX_CONTROLS = 3   # how many different "more" controls to try before deciding the list has ended


class Browser:
    """One headless Chromium for the whole run, started on first use."""

    def __init__(self, max_pages: int = 150, max_seconds: float = 20 * 60):
        self.max_pages = max_pages          # budget: pages rendered per run
        self.max_seconds = max_seconds      # budget: total time spent rendering per run
        self.pages_rendered = 0
        self.seconds = 0.0
        self._pw = None
        self._browser = None
        self._failed = ""

    # -- lifecycle ---------------------------------------------------------
    def _start(self) -> None:
        if self._browser is not None:
            return
        if self._failed:
            raise BrowserUnavailable(self._failed)
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 - not installed, or no browser binary
            self._failed = f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
            log.warning("headless browser unavailable (%s); pages that need it are skipped", self._failed)
            raise BrowserUnavailable(self._failed) from exc

    def close(self) -> None:
        for obj, method in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, method)()
            except Exception:  # noqa: BLE001
                pass
        self._browser = self._pw = None

    @property
    def exhausted(self) -> str:
        if self.pages_rendered >= self.max_pages:
            return f"the run's limit of {self.max_pages} browser-rendered pages was reached"
        if self.seconds >= self.max_seconds:
            return f"the run's {int(self.max_seconds // 60)}-minute browser budget was used up"
        return ""

    # -- rendering ---------------------------------------------------------
    def render(self, url: str, collect: Optional[Callable[[list], int]] = None,
               max_clicks: int = 20) -> Rendered:
        """Open `url`, wait for it to settle, and return what it shows.

        `collect(frames)` is called after the page loads and again after each
        "load more" / "next" click. It returns how many jobs it has seen so far;
        clicking stops as soon as a click adds nothing.
        """
        if self.exhausted:
            raise BrowserUnavailable(self.exhausted)
        self._start()
        started = time.time()
        self.pages_rendered += 1
        out = Rendered(url=url)
        context = None
        try:
            context = self._browser.new_context(viewport={"width": 1366, "height": 900}, locale="en-US")
            context.route("**/*", lambda route: route.abort()
                          if route.request.resource_type in ("image", "media", "font")
                          else route.continue_())
            page = context.new_page()
            page.on("request", lambda req: out.requests.append(req.url))
            response = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            out.status = response.status if response is not None else 0
            self._settle(page, 10_000)
            for _ in range(3):                  # nudge lists that load as you scroll
                try:
                    page.mouse.wheel(0, 20_000)
                except Exception:  # noqa: BLE001
                    break
                page.wait_for_timeout(400)
            self._settle(page, 4_000)

            out.frames, out.text = self._snapshot(page)
            out.final_url = page.url
            if collect is None:
                return out
            count = collect(out.frames)
            for _ in range(max_clicks):
                grew = False
                for control in range(MAX_CONTROLS):      # the first "Next" found may belong to a carousel
                    if not self._click_more(page, control):
                        break
                    self._settle(page, 4_000)
                    frames, text = self._snapshot(page)
                    seen = collect(frames)
                    if seen > count:
                        count, out.frames, out.text, out.final_url = seen, frames, text, page.url
                        out.clicks += 1
                        grew = True
                        break
                if not grew:
                    break                       # nothing on the page adds jobs any more: end of the list
            else:
                out.stopped = f"stopped after {max_clicks} pages of results"
            return out
        except BrowserUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - a page that won't load is a normal outcome
            reason = str(exc).splitlines()[0][:140] if str(exc) else ""
            if context is None or re.search(r"(browser|context|target)\b.{0,40}\bclosed", reason, re.I):
                self.close()                    # the browser itself died: start a fresh one next time
            raise BrowserUnavailable(f"the page didn't load in the browser ({type(exc).__name__}: {reason})") from exc
        finally:
            self.seconds += time.time() - started
            try:
                if context is not None:
                    context.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _settle(page, timeout_ms: int) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:  # noqa: BLE001 - pages with live trackers never go idle
            pass
        page.wait_for_timeout(700)

    @staticmethod
    def _snapshot(page) -> tuple:
        frames, texts = [], []
        for frame in page.frames:
            try:
                if frame.url.startswith("http"):
                    frames.append((frame.url, frame.content()))
                    texts.append(frame.evaluate("() => document.body ? document.body.innerText : ''") or "")
            except Exception:  # noqa: BLE001 - a frame that vanished mid-read
                continue
        return frames, "\n".join(texts)

    @staticmethod
    def _click_more(page, skip: int = 0) -> str:
        for frame in page.frames:
            try:
                label = frame.evaluate(_CLICK_MORE, skip)
            except Exception:  # noqa: BLE001
                continue
            if label:
                return label
        return ""


_shared: Optional[Browser] = None


def shared() -> Browser:
    """The run's one browser."""
    global _shared
    if _shared is None:
        _shared = Browser()
    return _shared


def close_shared() -> None:
    global _shared
    if _shared is not None:
        _shared.close()
        _shared = None
