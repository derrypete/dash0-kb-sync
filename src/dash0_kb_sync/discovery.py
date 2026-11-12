"""Sitemap XML parser, root index link-discovery pass, and URL bucket router."""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urljoin, urlparse, urlunparse
from xml.etree import ElementTree as ET

import httpx
from bs4 import BeautifulSoup

from dash0_kb_sync.config import (
    WEB_AND_API_BUCKETS,
    BucketSpec,
    DiscoveredURL,
    SyncSettings,
)

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; arm64 Mac OS X 15_0) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 "
        "dash0-kb-sync/0.1"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_SKIP_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".css",
    ".js",
    ".woff",
    ".woff2",
    ".mp4",
    ".webm",
)

_RAW_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)


def create_http_client(timeout_s: float = 25.0) -> httpx.AsyncClient:
    """Create an HTTP/2-enabled AsyncClient with sensible timeouts and headers."""
    return httpx.AsyncClient(
        http2=True,
        follow_redirects=True,
        timeout=httpx.Timeout(timeout_s, connect=10.0),
        headers=DEFAULT_HEADERS,
    )


async def fetch_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_attempts: int = 4,
    base_backoff_s: float = 1.0,
    extra_headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Fetch a URL via HTTP/2 with exponential backoff on transient failures."""
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = await client.get(url, headers=extra_headers)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
                retry_after = resp.headers.get("Retry-After")
                wait_s = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else base_backoff_s * (2 ** (attempt - 1))
                )
                logger.warning(
                    "HTTP %s for %s (attempt %d/%d), retrying in %.1fs",
                    resp.status_code,
                    url,
                    attempt,
                    max_attempts,
                    wait_s,
                )
                await asyncio.sleep(min(wait_s, 30.0))
                continue
            return resp
        except (httpx.RequestError, httpx.TimeoutException) as exc:
            last_exc = exc
            if attempt < max_attempts:
                wait_s = base_backoff_s * (2 ** (attempt - 1))
                logger.warning(
                    "Request error on %s (%s, attempt %d/%d), retrying in %.1fs",
                    url,
                    exc,
                    attempt,
                    max_attempts,
                    wait_s,
                )
                await asyncio.sleep(wait_s)
            else:
                raise
    assert last_exc is not None
    raise last_exc


def canonicalize_dash0_url(raw_url: str, base_url: str = "https://www.dash0.com") -> str | None:
    """Normalize a Dash0 URL, stripping fragments/queries and non-page assets."""
    raw = raw_url.strip()
    if not raw or raw.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None

    resolved = urljoin(base_url, raw)
    parsed = urlparse(resolved)
    if parsed.scheme not in ("http", "https"):
        return None

    host = parsed.netloc.lower()
    if host == "dash0.com":
        host = "www.dash0.com"
    if host != "www.dash0.com":
        return None

    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    lower_path = path.lower()
    if lower_path.startswith("/_next/") or lower_path.startswith("/api/"):
        return None
    if any(lower_path.endswith(ext) for ext in _SKIP_EXTENSIONS):
        return None

    return urlunparse(("https", host, path, "", "", ""))


def match_bucket_for_url(
    canonical_url: str,
    buckets: tuple[BucketSpec, ...] = WEB_AND_API_BUCKETS,
) -> str | None:
    """Classify a canonical `https://www.dash0.com/...` URL into one of the 8 web/API buckets."""
    for b in buckets:
        if not b.root_url:
            continue
        root = b.root_url.rstrip("/")
        if canonical_url == root or canonical_url.startswith(root + "/"):
            return b.name
    return None


def _strip_xml_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


async def _parse_sitemap_recursive(
    client: httpx.AsyncClient,
    sitemap_url: str,
    sem: asyncio.Semaphore,
    visited: set[str],
) -> list[tuple[str, str | None]]:
    """Fetch and parse a sitemap or sitemapindex XML recursively."""
    if sitemap_url in visited:
        return []
    visited.add(sitemap_url)

    async with sem:
        resp = await fetch_with_retry(client, sitemap_url)
        if resp.status_code >= 400:
            logger.warning("Failed to fetch sitemap %s (HTTP %d)", sitemap_url, resp.status_code)
            return []
        xml_bytes = resp.content

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        logger.error("Failed to parse XML from %s: %s", sitemap_url, exc)
        return []

    root_tag = _strip_xml_ns(root.tag).lower()
    if root_tag == "sitemapindex":
        child_urls: list[str] = []
        for child in root:
            if _strip_xml_ns(child.tag).lower() != "sitemap":
                continue
            for sub in child:
                if _strip_xml_ns(sub.tag).lower() == "loc" and sub.text:
                    child_urls.append(sub.text.strip())
        tasks = [
            _parse_sitemap_recursive(client, c_url, sem, visited) for c_url in child_urls
        ]
        nested = await asyncio.gather(*tasks)
        merged: list[tuple[str, str | None]] = []
        for sublist in nested:
            merged.extend(sublist)
        return merged

    entries: list[tuple[str, str | None]] = []
    if root_tag == "urlset":
        for url_el in root:
            if _strip_xml_ns(url_el.tag).lower() != "url":
                continue
            loc: str | None = None
            lastmod: str | None = None
            for sub in url_el:
                sub_tag = _strip_xml_ns(sub.tag).lower()
                if sub_tag == "loc" and sub.text:
                    loc = sub.text.strip()
                elif sub_tag == "lastmod" and sub.text:
                    lastmod = sub.text.strip()
            if loc:
                entries.append((loc, lastmod))
    return entries


def extract_links_from_html(html: str, page_url: str) -> set[str]:
    """Extract canonical Dash0 URLs from both DOM `<a href>` and RSC `<template>` payloads."""
    found: set[str] = set()
    soup = BeautifulSoup(html, "lxml")
    for a_tag in soup.find_all("a", href=True):
        canon = canonicalize_dash0_url(str(a_tag["href"]), base_url=page_url)
        if canon:
            found.add(canon)

    # Also scan raw HTML so links streamed inside Next.js <template> tags are caught
    for match in _RAW_HREF_RE.finditer(html):
        canon = canonicalize_dash0_url(match.group(1), base_url=page_url)
        if canon:
            found.add(canon)

    return found


async def _discover_links_on_index(
    client: httpx.AsyncClient,
    bucket: BucketSpec,
    sem: asyncio.Semaphore,
) -> list[str]:
    """Perform a lightweight link-discovery pass on a bucket's root index URL."""
    if not bucket.root_url:
        return []
    async with sem:
        try:
            resp = await fetch_with_retry(client, bucket.root_url)
            if resp.status_code >= 400:
                logger.warning(
                    "Index link-discovery returned HTTP %d on %s",
                    resp.status_code,
                    bucket.root_url,
                )
                return []
            links = extract_links_from_html(resp.text, bucket.root_url)
        except Exception as exc:
            logger.warning("Index link-discovery error on %s: %s", bucket.root_url, exc)
            return []

    matched = [u for u in sorted(links) if match_bucket_for_url(u) == bucket.name]
    return matched


async def discover_website_urls(
    settings: SyncSettings,
    target_buckets: set[str] | None = None,
) -> dict[str, list[DiscoveredURL]]:
    """Discover and classify all Dash0 URLs across the 8 website/API buckets.

    1. Recursively fetches `https://www.dash0.com/sitemap.xml` (handling sitemapindex).
    2. Performs a lightweight link-discovery pass on each bucket's root index URL to catch
       subpages not yet listed in `sitemap.xml` (such as `/docs/otelbin/*`).
    """
    active_specs = [
        b
        for b in WEB_AND_API_BUCKETS
        if target_buckets is None or b.name in target_buckets
    ]
    by_bucket: dict[str, dict[str, DiscoveredURL]] = {b.name: {} for b in active_specs}

    sem = asyncio.Semaphore(settings.http_concurrency)
    async with create_http_client() as client:
        raw_sitemap_entries = await _parse_sitemap_recursive(
            client, settings.sitemap_url, sem, visited=set()
        )
        for raw_loc, lastmod in raw_sitemap_entries:
            canon = canonicalize_dash0_url(raw_loc, base_url=settings.base_domain)
            if not canon:
                continue
            b_name = match_bucket_for_url(canon)
            if b_name and b_name in by_bucket:
                existing = by_bucket[b_name].get(canon)
                if existing is None or (lastmod and not existing.lastmod):
                    by_bucket[b_name][canon] = DiscoveredURL(
                        url=canon,
                        bucket=b_name,
                        lastmod=lastmod,
                        source="sitemap",
                    )

        # Ensure every bucket's own root URL is included and run root index link discovery
        index_tasks = [_discover_links_on_index(client, b, sem) for b in active_specs]
        index_results = await asyncio.gather(*index_tasks)

        for b, extra_urls in zip(active_specs, index_results, strict=True):
            if b.root_url:
                root_canon = canonicalize_dash0_url(b.root_url)
                if root_canon and root_canon not in by_bucket[b.name]:
                    by_bucket[b.name][root_canon] = DiscoveredURL(
                        url=root_canon,
                        bucket=b.name,
                        lastmod=None,
                        source="index_links",
                    )
            added_from_index = 0
            for u in extra_urls:
                if u not in by_bucket[b.name]:
                    by_bucket[b.name][u] = DiscoveredURL(
                        url=u,
                        bucket=b.name,
                        lastmod=None,
                        source="index_links",
                    )
                    added_from_index += 1
            if added_from_index:
                logger.info(
                    "Index link-discovery found %d extra URLs for %s",
                    added_from_index,
                    b.name,
                )

    return {
        b_name: sorted(url_map.values(), key=lambda d: d.url)
        for b_name, url_map in by_bucket.items()
    }


async def verify_removed_urls(
    urls: list[str],
    concurrency: int = 6,
) -> set[str]:
    """Check URLs missing from sitemap and return those actually returning HTTP 404/410."""
    if not urls:
        return set()

    gone: set[str] = set()
    sem = asyncio.Semaphore(concurrency)

    async def _probe(client: httpx.AsyncClient, url: str) -> None:
        async with sem:
            try:
                resp = await fetch_with_retry(client, url, max_attempts=2)
                if resp.status_code in (404, 410):
                    gone.add(url)
            except Exception as exc:
                logger.debug("Probe error checking removed URL %s: %s", url, exc)

    async with create_http_client(timeout_s=15.0) as client:
        await asyncio.gather(*(_probe(client, u) for u in urls))

    return gone
