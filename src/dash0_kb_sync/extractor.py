"""Playwright Chromium extraction engine with interactive tab expansion and DOM-to-Markdown conversion."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from markdownify import MarkdownConverter
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from dash0_kb_sync.config import (
    DiscoveredURL,
    ExtractedPage,
    SyncSettings,
    build_extracted_page,
)

logger = logging.getLogger(__name__)

# In-browser JavaScript that expands all ARIA/Radix/HeadlessUI tabs, custom button-group tabs,
# <details> elements, and [aria-expanded="false"] accordions inside main/article.
EXPAND_INTERACTIVE_JS = r"""
async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const root = document.querySelector("article") || document.querySelector("main") || document.body;
  let expandedTabGroups = 0;
  let expandedTabsTotal = 0;
  let expandedAccordions = 0;

  const isIgnoredControl = (el) => {
    if (!el) return true;
    const cls = (el.className || "").toString();
    const txt = (el.textContent || "").trim();
    if (
      cls.includes("inline-code-copy") ||
      cls.includes("release-history_") ||
      el.getAttribute("aria-label") === "Copy" ||
      txt === "Copy" ||
      txt === "Copied" ||
      txt === "⌘K" ||
      el.closest("nav") ||
      el.closest("aside")
    ) {
      return true;
    }
    return false;
  };

  // 1. Expand standard ARIA / Radix / HeadlessUI [role="tablist"] components
  const tablists = Array.from(root.querySelectorAll('[role="tablist"]')).filter(
    (tl) => !tl.closest("nav") && !tl.closest("aside") && !tl.closest(".expanded-tab")
  );

  for (const tablist of tablists) {
    if (!tablist.isConnected) continue;
    const tabs = Array.from(tablist.querySelectorAll('[role="tab"]')).filter(
      (t) => !isIgnoredControl(t)
    );
    if (tabs.length === 0) continue;

    // Determine the enclosing tab widget container
    let container = tablist.parentElement;
    while (
      container &&
      container !== root &&
      container.querySelectorAll('[role="tabpanel"]').length === 0 &&
      container.parentElement &&
      container.parentElement !== root
    ) {
      if (container.parentElement.querySelectorAll('[role="tablist"]').length > 1) {
        break;
      }
      container = container.parentElement;
    }
    if (!container) container = tablist.parentElement || tablist;

    const captured = [];
    for (const tab of tabs) {
      const label = (tab.innerText || tab.textContent || "Tab").trim().replace(/\s+/g, " ");
      try {
        tab.scrollIntoView({ block: "nearest" });
        tab.click();
      } catch (e) {}
      await sleep(150);

      let panel = null;
      const controlsId = tab.getAttribute("aria-controls");
      if (controlsId) {
        panel = document.getElementById(controlsId);
      }
      if (!panel) {
        const candidates = Array.from(container.querySelectorAll('[role="tabpanel"]'));
        panel =
          candidates.find(
            (p) =>
              !p.hasAttribute("hidden") &&
              p.getAttribute("data-state") !== "inactive" &&
              getComputedStyle(p).display !== "none"
          ) || candidates[0];
      }
      if (panel) {
        const html = panel.innerHTML.trim();
        if (html) {
          captured.push({ label: label || "Option", html });
        }
      }
    }

    if (captured.length > 0) {
      const replacement = document.createElement("div");
      replacement.className = "expanded-tabs-container";
      for (const item of captured) {
        const wrap = document.createElement("div");
        wrap.className = "expanded-tab";
        const h4 = document.createElement("h4");
        h4.textContent = `[Tab: ${item.label}]`;
        const body = document.createElement("div");
        body.innerHTML = item.html;
        wrap.appendChild(h4);
        wrap.appendChild(body);
        replacement.appendChild(wrap);
      }
      container.replaceWith(replacement);
      expandedTabGroups += 1;
      expandedTabsTotal += captured.length;
    }
  }

  // 2. Expand custom button-group code/content tabs (without role="tablist")
  const allContainers = Array.from(root.querySelectorAll("div, section")).filter(
    (el) =>
      el.isConnected &&
      !el.closest("nav") &&
      !el.closest("aside") &&
      !el.closest(".expanded-tabs-container") &&
      el.children.length >= 2 &&
      el.children.length <= 12
  );

  for (const bar of allContainers) {
    if (!bar.isConnected) continue;
    const childButtons = Array.from(bar.children).filter(
      (c) => c.tagName === "BUTTON" && !isIgnoredControl(c)
    );
    if (childButtons.length < 2 || childButtons.length !== bar.children.length) continue;

    const labels = childButtons.map((b) => (b.innerText || "").trim());
    if (labels.some((l) => !l || l.length > 45)) continue;

    const widget = bar.parentElement;
    if (!widget || widget === root || widget.children.length < 2 || widget.children.length > 4) {
      continue;
    }
    const contentSibling = Array.from(widget.children).find((c) => c !== bar);
    if (!contentSibling) continue;

    const captured = [];
    const seenHtml = new Set();
    for (const btn of childButtons) {
      const label = (btn.innerText || "").trim().replace(/\s+/g, " ");
      try {
        btn.click();
      } catch (e) {}
      await sleep(150);
      const html = contentSibling.innerHTML.trim();
      if (html) {
        captured.push({ label, html });
        seenHtml.add(html);
      }
    }

    // Only replace if clicking buttons actually switched or rendered content panels
    if (captured.length >= 2 && seenHtml.size >= 2) {
      const replacement = document.createElement("div");
      replacement.className = "expanded-tabs-container";
      for (const item of captured) {
        const wrap = document.createElement("div");
        wrap.className = "expanded-tab";
        const h4 = document.createElement("h4");
        h4.textContent = `[Tab: ${item.label}]`;
        const body = document.createElement("div");
        body.innerHTML = item.html;
        wrap.appendChild(h4);
        wrap.appendChild(body);
        replacement.appendChild(wrap);
      }
      widget.replaceWith(replacement);
      expandedTabGroups += 1;
      expandedTabsTotal += captured.length;
    }
  }

  // 3. Expand all <details> and [aria-expanded="false"] accordions inside main/article
  const detailsEls = Array.from(root.querySelectorAll("details"));
  for (const det of detailsEls) {
    if (!det.open) {
      det.open = true;
      expandedAccordions += 1;
    }
  }

  const collapsedTriggers = Array.from(
    root.querySelectorAll('button[aria-expanded="false"], [role="button"][aria-expanded="false"]')
  ).filter((el) => !isIgnoredControl(el) && !el.closest('[role="dialog"]'));

  for (const trig of collapsedTriggers) {
    try {
      trig.click();
      expandedAccordions += 1;
      await sleep(120);
    } catch (e) {}
  }

  return { expandedTabGroups, expandedTabsTotal, expandedAccordions };
}
"""

_LANGUAGE_ALIASES: dict[str, str] = {
    "sh": "bash",
    "shell": "bash",
    "zsh": "bash",
    "console": "bash",
    "terminal": "bash",
    "yml": "yaml",
    "tf": "hcl",
    "terraform": "hcl",
    "py": "python",
    "js": "javascript",
    "ts": "typescript",
    "tsx": "tsx",
    "jsx": "jsx",
    "rb": "ruby",
    "cs": "csharp",
    "c#": "csharp",
    "golang": "go",
    "prom": "promql",
    "prometheus": "promql",
    "plaintext": "text",
    "plain": "text",
    "txt": "text",
}

_KNOWN_LANGUAGES: set[str] = {
    "bash",
    "yaml",
    "json",
    "jsonc",
    "promql",
    "hcl",
    "python",
    "go",
    "java",
    "javascript",
    "typescript",
    "tsx",
    "jsx",
    "rust",
    "ruby",
    "csharp",
    "php",
    "sql",
    "dockerfile",
    "toml",
    "ini",
    "xml",
    "html",
    "css",
    "graphql",
    "protobuf",
    "proto",
    "http",
    "diff",
    "markdown",
    "md",
    "c",
    "cpp",
    "kotlin",
    "swift",
    "elixir",
    "erlang",
    "otel",
    "logql",
    "text",
}


def normalize_code_language(raw_lang: str | None, code_text: str = "") -> str:
    """Normalize a code language badge or infer language deterministically from code content."""
    if raw_lang:
        cleaned = raw_lang.strip().lower()
        cleaned = re.sub(r"^language-", "", cleaned)
        cleaned = re.sub(r"^lang-", "", cleaned)
        cleaned = cleaned.split()[0] if cleaned else ""
        cleaned = _LANGUAGE_ALIASES.get(cleaned, cleaned)
        if cleaned in _KNOWN_LANGUAGES or re.fullmatch(r"[a-z0-9_+-]{1,18}", cleaned):
            return cleaned

    sample = code_text.strip()
    if not sample:
        return ""
    if sample.startswith(("{", "[")):
        try:
            json.loads(sample)
            return "json"
        except Exception:
            if re.search(r'"[^"]+"\s*:', sample):
                return "json"
    if re.search(r'^(resource|variable|provider|module|terraform|data)\s+"', sample, re.M):
        return "hcl"
    if re.search(
        r"^(apiVersion:|kind:|receivers:|processors:|exporters:|service:|scrape_configs:)",
        sample,
        re.M,
    ):
        return "yaml"
    if re.search(
        r"^\s*(\$\s+)?(curl|helm|kubectl|docker|npm|npx|pnpm|uv|pip|brew|git|export| dash0)\b",
        sample,
        re.M,
    ):
        return "bash"
    if re.search(r"\b(rate|irate|increase|histogram_quantile|sum\s+by|avg\s+by)\s*\(", sample):
        return "promql"
    return ""


class Dash0MarkdownConverter(MarkdownConverter):
    """Custom MarkdownConverter preserving fenced code languages and clean table cells."""

    def __init__(self, **options) -> None:
        options.setdefault("heading_style", "ATX")
        options.setdefault("bullets", "-")
        options.setdefault("escape_underscores", False)
        options.setdefault("escape_asterisks", False)
        super().__init__(**options)

    def convert_pre(self, el: Tag, text: str, parent_tags: set[str]) -> str:
        # Remove line-number gutters rendered by Prism / Shiki
        for gutter in el.find_all(
            lambda t: isinstance(t, Tag)
            and (
                t.get("id") == "line-numbers"
                or "line-numbers" in " ".join(t.get("class") or [])
                or "select-none" in (t.get("class") or [])
            )
        ):
            gutter.decompose()

        # Handle Prism (`div[class*="token-line"]`) and Shiki (`span.line`) block-per-line DOM
        token_lines = el.find_all(
            lambda t: isinstance(t, Tag)
            and any("token-line" in cls or cls == "line" for cls in (t.get("class") or []))
        )
        if token_lines:
            raw_code = "\n".join(line_el.get_text() for line_el in token_lines)
        else:
            code_el = el.find("code")
            raw_code = code_el.get_text() if code_el else el.get_text()

        raw_code = raw_code.replace("\r\n", "\n").strip("\n")
        code_el = el.find("code")

        lang = el.get("data-code-lang") or ""
        if not lang:
            for node in (code_el, el):
                if not isinstance(node, Tag):
                    continue
                for attr in ("data-language", "data-lang", "lang"):
                    if node.get(attr):
                        lang = str(node.get(attr))
                        break
                classes = node.get("class") or []
                for cls in classes:
                    if cls.startswith(("language-", "lang-")):
                        lang = cls
                        break
                if lang:
                    break

        norm_lang = normalize_code_language(str(lang) if lang else None, raw_code)
        fence = "````" if "```" in raw_code else "```"
        return f"\n\n{fence}{norm_lang}\n{raw_code}\n{fence}\n\n"

    def convert_td(self, el: Tag, text: str, parent_tags: set[str]) -> str:
        clean = re.sub(r"\s*\n+\s*", " ", text).strip().replace("|", "\\|")
        colspan = int(el.get("colspan", 1) or 1)
        return " " + clean + " |" * colspan

    def convert_th(self, el: Tag, text: str, parent_tags: set[str]) -> str:
        clean = re.sub(r"\s*\n+\s*", " ", text).strip().replace("|", "\\|")
        colspan = int(el.get("colspan", 1) or 1)
        return " " + clean + " |" * colspan


def _unwrap_nextjs_image_url(raw_url: str, page_url: str) -> str:
    """Convert `/_next/image?url=%2Fimages%2F...` into direct absolute `https://www.dash0.com/...`."""
    resolved = urljoin(page_url, raw_url.strip())
    parsed = urlparse(resolved)
    if parsed.path == "/_next/image":
        qs = parse_qs(parsed.query)
        inner = qs.get("url", [None])[0]
        if inner:
            return urljoin(page_url, unquote(inner))
    return resolved


def _select_content_root(soup: BeautifulSoup) -> Tag:
    """Isolate `<article>` or `<main>` content column while stripping navigation chrome."""
    main_el = soup.find("main")
    if main_el:
        article_el = main_el.find("article")
        if isinstance(article_el, Tag):
            return article_el
        # On `/hub/integrations/*`, main has `div.flex > div.flex.flex-col` + sticky right TOC
        flex_wrapper = main_el.find(
            lambda t: isinstance(t, Tag)
            and t.name == "div"
            and "flex" in (t.get("class") or [])
            and t.find("h1") is not None
        )
        if isinstance(flex_wrapper, Tag):
            # Remove sticky right-hand TOC siblings inside flex_wrapper
            for child in list(flex_wrapper.children):
                if isinstance(child, Tag) and "sticky" in (child.get("class") or []):
                    child.decompose()
            return flex_wrapper
        return main_el

    article_el = soup.find("article")
    if isinstance(article_el, Tag):
        return article_el

    body_el = soup.find("body")
    if isinstance(body_el, Tag):
        for chrome_tag in body_el.find_all(["header", "footer", "nav", "aside"], recursive=False):
            chrome_tag.decompose()
        return body_el

    return soup


def _normalize_dash0_dom(root: Tag, page_url: str, soup: BeautifulSoup) -> None:
    """Strip UI chrome and normalize code blocks, callouts, details, and relative links."""
    # 1. Remove scripts, styles, SVGs, iframes, cookie banners, navs, asides, popovers
    for tag in root.find_all(
        ["script", "style", "noscript", "template", "svg", "iframe", "canvas", "form"]
    ):
        tag.decompose()

    for tag in root.find_all(["nav", "aside"]):
        tag.decompose()

    # Remove article-level mobile footers (`article > footer`), preserving `article > header`
    for footer in root.find_all("footer"):
        footer.decompose()

    for tag in list(root.find_all(True)):
        if not tag.parent:
            continue
        role = (tag.get("role") or "").lower()
        cls_list = tag.get("class") or []
        cls_str = " ".join(cls_list)
        el_id = str(tag.get("id") or "")

        if role in ("navigation", "dialog", "banner", "contentinfo"):
            tag.decompose()
            continue
        if tag.has_attr("popover") or "release-history_" in cls_str:
            tag.decompose()
            continue
        if "CybotCookiebot" in el_id or "cookie" in el_id.lower():
            tag.decompose()
            continue
        if "sr-only" in cls_list:
            tag.decompose()
            continue
        if "sticky" in cls_list and tag.find("h1") is None and len(tag.get_text(strip=True)) < 1200:
            # Right-hand sticky TOC / share bar
            tag.decompose()
            continue

    # 2. Remove inline-code copy buttons and standalone Copy / UI buttons
    for btn in list(root.find_all("button")):
        btn_txt = btn.get_text(strip=True)
        btn_cls = " ".join(btn.get("class") or [])
        if (
            "inline-code-copy" in btn_cls
            or btn.get("aria-label") == "Copy"
            or btn_txt in ("", "Copy", "Copied", "⌘K", "View all blogs")
        ):
            btn.decompose()

    # 3. Normalize Dash0 `<div id="code-snippet">` and `<pre>` blocks with header language badges
    for pre in list(root.find_all("pre")):
        if not pre.parent:
            continue
        detected_lang = ""
        wrapper = pre.parent
        if isinstance(wrapper, Tag) and wrapper.name == "div":
            # Look for preceding header bar containing language span (e.g. `<span>yaml</span>`)
            prev_sib = pre.find_previous_sibling("div")
            if isinstance(prev_sib, Tag) and prev_sib.find("pre") is None:
                badge_text = prev_sib.get_text(" ", strip=True)
                # Strip residual "Copy" word if present
                badge_text = re.sub(r"\bCopy\b", "", badge_text, flags=re.I).strip()
                if badge_text and len(badge_text) <= 24:
                    detected_lang = badge_text
                    prev_sib.decompose()

        norm_lang = normalize_code_language(detected_lang, pre.get_text())
        if norm_lang:
            pre["data-code-lang"] = norm_lang

    # 4. Normalize `<div class="callout-wrapper">` & `[role="alert"]` into clean blockquotes
    for callout in list(root.find_all(class_=lambda c: c and "callout-wrapper" in c)):
        if not callout.parent:
            continue
        inner_html_cls = " ".join(
            " ".join(d.get("class") or []) for d in callout.find_all("div")
        )
        kind = "Note"
        if "amber" in inner_html_cls or "yellow" in inner_html_cls or "orange" in inner_html_cls:
            kind = "Warning"
        elif "red" in inner_html_cls:
            kind = "Caution"
        elif "green" in inner_html_cls:
            kind = "Tip"

        bq = soup.new_tag("blockquote")
        lead = soup.new_tag("p")
        strong = soup.new_tag("strong")
        strong.string = f"{kind}: "
        lead.append(strong)
        for child in list(callout.children):
            bq.append(child.extract())
        bq.insert(0, lead)
        callout.replace_with(bq)

    # 5. Format `<details><summary>...</summary>...</details>` clearly
    for details in list(root.find_all("details")):
        summary = details.find("summary")
        if isinstance(summary, Tag):
            title_txt = summary.get_text(" ", strip=True)
            h4 = soup.new_tag("h4")
            h4.string = f"[Details: {title_txt}]"
            summary.replace_with(h4)

    # 6. Convert all relative `href` and `src` attributes to absolute Dash0 URLs
    for a_tag in list(root.find_all("a", href=True)):
        href = str(a_tag["href"]).strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:")):
            continue
        abs_href = _unwrap_nextjs_image_url(href, page_url)
        a_tag["href"] = abs_href
        # If anchor wraps only an image with empty text, give it alt text or image filename
        if not a_tag.get_text(strip=True):
            img = a_tag.find("img")
            if isinstance(img, Tag) and img.get("alt"):
                a_tag.string = f"[Image: {img['alt']}]"

    for img_tag in list(root.find_all("img", src=True)):
        src = str(img_tag["src"]).strip()
        if src.startswith("data:"):
            img_tag.decompose()
            continue
        img_tag["src"] = _unwrap_nextjs_image_url(src, page_url)


def clean_html_to_markdown(html: str, page_url: str) -> tuple[str, str]:
    """Extract clean page title and Markdown body from rendered Dash0 HTML."""
    soup = BeautifulSoup(html, "lxml")

    # Determine page title: prefer content H1, fallback to <title>
    raw_doc_title = ""
    if soup.title and soup.title.string:
        raw_doc_title = re.sub(r"\s*[·|\-–]\s*Dash0\s*$", "", soup.title.string.strip())

    root = _select_content_root(soup)
    h1 = root.find("h1")
    h1_title = h1.get_text(" ", strip=True) if isinstance(h1, Tag) else ""
    page_title = h1_title or raw_doc_title or page_url

    _normalize_dash0_dom(root, page_url, soup)

    converter = Dash0MarkdownConverter()
    md = converter.convert(str(root))

    # Post-process Markdown whitespace and stray artifacts
    lines = [ln.rstrip() for ln in md.replace("\r\n", "\n").split("\n")]
    cleaned_md = "\n".join(lines).strip()
    cleaned_md = re.sub(r"\n{3,}", "\n\n", cleaned_md)

    return page_title, cleaned_md


@dataclass
class ScrapeOutcome:
    """Result of scraping a single URL with Playwright."""

    url: str
    page: ExtractedPage | None = None
    is_not_found: bool = False
    expanded_tab_groups: int = 0
    expanded_tabs_total: int = 0
    expanded_accordions: int = 0
    error: str | None = None


class BrowserExtractor:
    """Polite async Playwright Chromium pool for rendering and extracting Dash0 pages."""

    def __init__(self, settings: SyncSettings) -> None:
        self.settings = settings
        self._playwright = None
        self._browser: Browser | None = None
        self._contexts: list[BrowserContext] = []

    async def __aenter__(self) -> BrowserExtractor:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        for ctx in self._contexts:
            try:
                await ctx.close()
            except Exception:
                pass
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def _new_context(self) -> BrowserContext:
        assert self._browser is not None
        ctx = await self._browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; arm64 Mac OS X 15_0) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 "
                "dash0-kb-sync/0.1"
            ),
        )
        # Block heavy media/font assets to speed up rendering while keeping all JS/HTML/JSON intact
        await ctx.route(
            "**/*.{woff,woff2,ttf,otf,mp4,webm}",
            lambda route: route.abort(),
        )
        self._contexts.append(ctx)
        return ctx

    async def _navigate_and_settle(self, page: Page, url: str) -> int:
        """Navigate to URL, preferring networkidle with graceful fallback to domcontentloaded."""
        try:
            resp = await page.goto(
                url,
                wait_until="networkidle",
                timeout=min(self.settings.page_timeout_ms, 18_000),
            )
            status = resp.status if resp else 200
        except PlaywrightTimeoutError:
            resp = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.settings.page_timeout_ms,
            )
            status = resp.status if resp else 200
            try:
                await page.wait_for_selector("article, main h1, main h2", timeout=6_000)
            except PlaywrightTimeoutError:
                pass
            await page.wait_for_timeout(500)

        # Short settle delay for streamed React Server Components
        await page.wait_for_timeout(200)
        return status

    async def scrape_one(
        self,
        context: BrowserContext,
        item: DiscoveredURL,
        max_retries: int = 2,
    ) -> ScrapeOutcome:
        """Render one page, expand all interactive tabs/accordions, and extract clean Markdown."""
        last_err: str | None = None
        for attempt in range(1, max_retries + 1):
            page = await context.new_page()
            try:
                status = await self._navigate_and_settle(page, item.url)
                if status in (404, 410):
                    return ScrapeOutcome(url=item.url, is_not_found=True)
                if status >= 500 and attempt < max_retries:
                    last_err = f"HTTP {status}"
                    await asyncio.sleep(1.5 * attempt)
                    continue

                stats = await page.evaluate(EXPAND_INTERACTIVE_JS)
                html = await page.content()

                title, body_md = clean_html_to_markdown(html, item.url)
                if not body_md.strip() and attempt < max_retries:
                    await page.wait_for_timeout(1000)
                    html = await page.content()
                    title, body_md = clean_html_to_markdown(html, item.url)

                extracted = build_extracted_page(
                    url=item.url,
                    bucket=item.bucket,
                    title=title,
                    lastmod=item.lastmod,
                    body_markdown=body_md or "*No textual content extracted.*",
                )

                if self.settings.rate_limit_delay_ms > 0:
                    await asyncio.sleep(self.settings.rate_limit_delay_ms / 1000.0)

                return ScrapeOutcome(
                    url=item.url,
                    page=extracted,
                    expanded_tab_groups=int(stats.get("expandedTabGroups", 0)),
                    expanded_tabs_total=int(stats.get("expandedTabsTotal", 0)),
                    expanded_accordions=int(stats.get("expandedAccordions", 0)),
                )
            except Exception as exc:
                last_err = str(exc)
                logger.warning(
                    "Scrape attempt %d/%d failed for %s: %s",
                    attempt,
                    max_retries,
                    item.url,
                    exc,
                )
                if attempt < max_retries:
                    await asyncio.sleep(1.5 * attempt)
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

        return ScrapeOutcome(url=item.url, error=last_err or "Unknown scrape error")

    async def scrape_batch(
        self,
        items: list[DiscoveredURL],
        on_progress: Callable[[ScrapeOutcome], None] | None = None,
    ) -> list[ScrapeOutcome]:
        """Scrape a batch of URLs using a polite worker pool of Playwright browser contexts."""
        if not items:
            return []

        worker_count = min(self.settings.browser_concurrency, len(items))
        contexts = [await self._new_context() for _ in range(worker_count)]
        queue: asyncio.Queue[tuple[int, DiscoveredURL]] = asyncio.Queue()
        for idx, it in enumerate(items):
            queue.put_nowait((idx, it))

        results: list[ScrapeOutcome | None] = [None] * len(items)

        async def _worker(ctx: BrowserContext) -> None:
            while not queue.empty():
                try:
                    idx, target = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                outcome = await self.scrape_one(ctx, target)
                results[idx] = outcome
                if on_progress:
                    on_progress(outcome)
                queue.task_done()

        await asyncio.gather(*(_worker(ctx) for ctx in contexts))
        return [r for r in results if r is not None]

    async def discover_openapi_spec_url(
        self,
        api_ref_url: str = "https://www.dash0.com/docs/api-reference",
    ) -> str | None:
        """Load `/docs/api-reference` in Playwright to locate or intercept the OpenAPI spec URL."""
        ctx = await self._new_context()
        page = await ctx.new_page()
        intercepted: list[str] = []

        def _on_request(req) -> None:
            u = req.url.lower()
            if ("openapi" in u or "swagger" in u) and (
                u.endswith(".json") or u.endswith(".yaml") or u.endswith(".yml")
            ):
                intercepted.append(req.url)

        page.on("request", _on_request)
        try:
            await self._navigate_and_settle(page, api_ref_url)
            href = await page.evaluate(
                """() => {
                    const anchors = Array.from(document.querySelectorAll('a[href]'));
                    for (const a of anchors) {
                        const h = a.getAttribute('href') || '';
                        const txt = (a.innerText || '').toLowerCase();
                        if (
                            h.includes('openapi') ||
                            h.endsWith('.json') ||
                            h.endsWith('.yaml') ||
                            h.endsWith('.yml') ||
                            txt.includes('download openapi') ||
                            txt.includes('openapi json')
                        ) {
                            return a.href;
                        }
                    }
                    return null;
                }"""
            )
            if href:
                return str(href)
            if intercepted:
                return intercepted[0]
            return None
        except Exception as exc:
            logger.warning("Playwright OpenAPI discovery failed on %s: %s", api_ref_url, exc)
            return None
        finally:
            await page.close()
