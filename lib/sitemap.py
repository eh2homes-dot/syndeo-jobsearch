"""Page URLs a site lists in its sitemaps (robots.txt Sitemap: lines,
/sitemap.xml, /sitemap_index.xml, and nested sitemaps a level or two down)."""
from __future__ import annotations

import re
from urllib.parse import urlparse
from xml.etree import ElementTree

from lib.http import get

MAX_SITEMAPS = 12


def _root(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def page_urls(site: str, keep=None) -> list[str]:
    """Every page URL in the site's sitemaps for which keep(url) is true."""
    root = _root(site)
    maps: list[str] = []
    r = get(root + "/robots.txt", timeout=15, retries=1)
    if r is not None:
        maps += re.findall(r"(?im)^\s*sitemap:\s*(\S+)", r.text)
    maps += [root + "/sitemap.xml", root + "/sitemap_index.xml"]
    seen: set[str] = set()
    found: list[str] = []
    while maps and len(seen) < MAX_SITEMAPS:
        m = maps.pop(0)
        if m in seen:
            continue
        seen.add(m)
        r = get(m, timeout=20, retries=1)
        if r is None or "<" not in r.text[:200]:
            continue
        try:
            tree = ElementTree.fromstring(r.content)
        except ElementTree.ParseError:
            continue
        for loc in tree.iter():
            if not loc.tag.endswith("loc") or not loc.text:
                continue
            u = loc.text.strip()
            if u.endswith(".xml") or "sitemap" in u.lower():
                maps.append(u)
            elif keep is None or keep(u):
                found.append(u.rstrip("/"))
    return list(dict.fromkeys(found))
