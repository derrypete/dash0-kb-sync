"""Configuration, bucket definitions, and shared Pydantic models for dash0-kb-sync."""

from __future__ import annotations

import hashlib
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]

CHANGELOG_BUCKET = "00_Dash0_Whats_New_Changelog"
GITHUB_BUCKET = "09_Dash0_GitHub_Repos_and_Examples"
API_REFERENCE_BUCKET = "03_Dash0_API_Reference"


class BucketSpec(BaseModel):
    """Specification for one target knowledge-base bucket."""

    name: str
    kind: Literal["changelog", "web", "openapi", "github"]
    root_url: str | None = None
    aliases: tuple[str, ...] = ()
    reverify_after_days: int | None = None
    description: str = ""


BUCKETS: tuple[BucketSpec, ...] = (
    BucketSpec(
        name="00_Dash0_Whats_New_Changelog",
        kind="changelog",
        aliases=("00", "changelog", "whats-new"),
        description="Sync health summary and 7-day / 30-day changelog with unified diffs",
    ),
    BucketSpec(
        name="01_Dash0_Core_Docs",
        kind="web",
        root_url="https://www.dash0.com/docs/dash0",
        aliases=("01", "core", "core-docs", "docs"),
        reverify_after_days=3,
        description="Dash0 core product documentation",
    ),
    BucketSpec(
        name="02_Dash0_OTelBin_Docs",
        kind="web",
        root_url="https://www.dash0.com/docs/otelbin",
        aliases=("02", "otelbin", "otelbin-docs"),
        description="OTelBin collector configuration editor & validation docs",
    ),
    BucketSpec(
        name="03_Dash0_API_Reference",
        kind="openapi",
        root_url="https://www.dash0.com/docs/api-reference",
        aliases=("03", "api", "api-reference", "openapi"),
        description="Dash0 OpenAPI specification, endpoints, parameters, and schemas",
    ),
    BucketSpec(
        name="04_Dash0_Guides",
        kind="web",
        root_url="https://www.dash0.com/guides",
        aliases=("04", "guides", "guide"),
        reverify_after_days=3,
        description="Technical guides and architecture best practices",
    ),
    BucketSpec(
        name="05_Dash0_Knowledge_Base",
        kind="web",
        root_url="https://www.dash0.com/knowledge",
        aliases=("05", "knowledge", "kb"),
        description="Observability & OpenTelemetry knowledge base articles",
    ),
    BucketSpec(
        name="06_Dash0_Comparisons",
        kind="web",
        root_url="https://www.dash0.com/comparisons",
        aliases=("06", "comparisons", "comparison"),
        description="Observability platform and tooling comparisons",
    ),
    BucketSpec(
        name="07_Dash0_Blog",
        kind="web",
        root_url="https://www.dash0.com/blog",
        aliases=("07", "blog", "blogs"),
        reverify_after_days=3,
        description="Dash0 product announcements and engineering blog posts",
    ),
    BucketSpec(
        name="08_Dash0_Integrations_Hub",
        kind="web",
        root_url="https://www.dash0.com/hub/integrations",
        aliases=("08", "integrations", "hub"),
        description="Dash0 Integrations Hub setup guides, dashboards, and PromQL check rules",
    ),
    BucketSpec(
        name="09_Dash0_GitHub_Repos_and_Examples",
        kind="github",
        root_url="https://github.com/dash0hq",
        aliases=("09", "github", "repos", "examples"),
        description="Dash0 public GitHub repositories, Helm values, Terraform, and OTel examples",
    ),
)

BUCKET_MAP: dict[str, BucketSpec] = {b.name: b for b in BUCKETS}
WEB_AND_API_BUCKETS: tuple[BucketSpec, ...] = tuple(
    b for b in BUCKETS if b.kind in ("web", "openapi")
)

PRIORITY_GITHUB_REPOS: tuple[str, ...] = (
    "dash0-operator",
    "terraform-provider-dash0",
    "otelbin",
    "dash0-cli",
    "dash0-api-client-go",
    "mcp-dash0",
    "dash0-sdk-web",
    "dash0-lambda-extension",
    "signal-control-edge",
    "signal-control-edge-tutorial",
    "opentelemetry-python-distribution",
    "opentelemetry-js-distribution",
    "opentelemetry-java-distribution",
    "opentelemetry-dotnet-distribution",
    "opentelemetry-ruby-distribution",
    "dash0-semantic-conventions",
    "agent-skills",
    "dash0-agent-plugin",
    "dash0-examples",
    "dash0-k8s-hpa-example",
    "otel-cicd-action",
)


def resolve_bucket_name(query: str) -> str:
    """Resolve a bucket name, numeric prefix (e.g. '08'), or alias (e.g. 'integrations')."""
    cleaned = query.strip()
    if cleaned in BUCKET_MAP:
        return cleaned
    lowered = cleaned.lower()
    for b in BUCKETS:
        if b.name.lower() == lowered or lowered in b.aliases:
            return b.name
    valid = ", ".join(b.name for b in BUCKETS)
    raise ValueError(f"Unknown bucket '{query}'. Valid buckets: {valid}")


class SyncSettings(BaseModel):
    """Runtime configuration for the Dash0 KB Sync pipeline."""

    project_root: Path = Field(default=PROJECT_ROOT)
    sitemap_url: str = Field(default="https://www.dash0.com/sitemap.xml")
    base_domain: str = Field(default="https://www.dash0.com")
    github_org: str = Field(default="dash0hq")
    github_token: str | None = Field(
        default_factory=lambda: os.environ.get("GITHUB_TOKEN") or None
    )

    # Local storage paths
    db_path: Path = Field(default_factory=lambda: PROJECT_ROOT / "data" / "dash0_state.db")
    drive_manifest_path: Path = Field(
        default_factory=lambda: PROJECT_ROOT / "data" / "drive_manifest.json"
    )
    output_dir: Path = Field(default_factory=lambda: PROJECT_ROOT / "output" / "markdown")
    credentials_path: Path = Field(
        default_factory=lambda: PROJECT_ROOT / "config" / "credentials.json"
    )
    token_path: Path = Field(default_factory=lambda: PROJECT_ROOT / "config" / "token.json")
    log_path: Path = Field(default_factory=lambda: PROJECT_ROOT / "logs" / "sync.log")

    # Google Drive settings
    drive_folder_name: str = Field(default="Dash0_Knowledge_Base")
    drive_scopes: list[str] = Field(
        default_factory=lambda: ["https://www.googleapis.com/auth/drive.file"]
    )
    drive_http_timeout_sec: int = Field(default=300, ge=30, le=900)

    # Polite concurrency & timeout settings
    browser_concurrency: int = Field(default=4, ge=1, le=12)
    http_concurrency: int = Field(default=8, ge=1, le=20)
    github_concurrency: int = Field(default=4, ge=1, le=10)
    rate_limit_delay_ms: int = Field(default=200, ge=0)
    page_timeout_ms: int = Field(default=30_000, ge=5_000)

    # Google Docs & NotebookLM safety split thresholds
    max_chars_per_doc: int = Field(default=750_000, ge=50_000)
    max_words_per_doc: int = Field(default=120_000, ge=10_000)

    def ensure_directories(self) -> None:
        """Ensure all required runtime directories exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.drive_manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)


class DiscoveredURL(BaseModel):
    """A URL discovered from sitemap.xml or root index link pass."""

    url: str
    bucket: str
    lastmod: str | None = None
    source: Literal["sitemap", "index_links"] = "sitemap"


class ExtractedPage(BaseModel):
    """Normalized extracted page ready for SQLite persistence and compilation."""

    url: str
    bucket: str
    title: str
    lastmod: str | None = None
    body_markdown: str
    content_markdown: str
    content_hash: str
    verified_at: str


PAGE_SEPARATOR = "=" * 80
_LAST_VERIFIED_RE = re.compile(r"^- \*\*Last Verified:\*\* .*$", re.MULTILINE)


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now_display() -> str:
    """Return current UTC timestamp formatted as YYYY-MM-DD HH:MM UTC."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")


def strip_metadata_header(content_markdown: str) -> str:
    """Extract the body markdown below the 80-char === metadata header block."""
    pattern = re.compile(
        r"^={80}\n# PAGE: .*?\n={80}\n*",
        re.DOTALL,
    )
    return pattern.sub("", content_markdown, count=1).strip()


def normalize_for_hash(markdown_text: str) -> str:
    """Normalize Markdown text for deterministic SHA-256 content hashing.

    Strips volatile timestamps like `Last Verified:` so re-verifying an unchanged
    page produces the exact same content hash.
    """
    text = markdown_text.replace("\r\n", "\n").replace("\r", "\n")
    text = _LAST_VERIFIED_RE.sub("- **Last Verified:** <normalized>", text)
    lines = [line.rstrip() for line in text.split("\n")]
    normalized = "\n".join(lines).strip()
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized


def compute_content_hash(title: str, canonical_url: str, body_markdown: str) -> str:
    """Compute SHA-256 hash over normalized title, canonical URL, and body content."""
    payload = f"TITLE: {title.strip()}\nURL: {canonical_url.strip()}\n\n{normalize_for_hash(body_markdown)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_FENCED_CODE_BLOCK_RE = re.compile(r"(^`{3,}.*?^`{3,})", re.MULTILINE | re.DOTALL)
_LINKED_MD_IMG_RE = re.compile(r"\[\s*!\[([^\]]*)\]\(([^)\n]+)\)\s*\]\(([^)\n]+)\)")
_MD_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)\n]+)\)")
_HTML_LINKED_IMG_RE = re.compile(
    r"<a\b[^>]*\bhref=[\"']([^\"']+)[\"'][^>]*>\s*(<img\b[^>]*>)\s*</a>",
    re.IGNORECASE | re.DOTALL,
)
_HTML_IMG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)


def convert_images_to_links(markdown_text: str) -> str:
    """Convert embedded Markdown/HTML images into lightweight Markdown links outside code blocks.

    When Google Drive imports Markdown into a native Google Doc, `![alt](url)` causes
    Google's backend to download and embed binary images (ballooning docs to 10-15 MB and
    timing out NotebookLM ingestion). Rendering `[Image: alt](url)` preserves semantic
    context and clickable URLs while keeping every Google Doc pure lightweight text (~350 KB).
    """
    if "![" not in markdown_text and "<img" not in markdown_text.lower():
        return markdown_text

    def _clean_label(raw_alt: str) -> str:
        alt = " ".join(raw_alt.split()).replace("[", "(").replace("]", ")")
        return f"Image: {alt}" if alt else "Image"

    def _replace_linked_md_img(match: re.Match[str]) -> str:
        alt = match.group(1)
        target_raw = match.group(3).strip()
        target_url = target_raw.split()[0].strip("<>") if target_raw else ""
        if not target_url or target_url.startswith("data:"):
            return ""
        return f"[{_clean_label(alt)}]({target_url})"

    def _replace_md_img(match: re.Match[str]) -> str:
        alt = match.group(1)
        src_raw = match.group(2).strip()
        img_url = src_raw.split()[0].strip("<>") if src_raw else ""
        if not img_url or img_url.startswith("data:"):
            return ""
        return f"[{_clean_label(alt)}]({img_url})"

    def _extract_html_alt(img_tag: str) -> str:
        m_alt = re.search(r"\balt=[\"']([^\"']*)[\"']", img_tag, re.IGNORECASE)
        return m_alt.group(1) if m_alt else ""

    def _replace_html_linked_img(match: re.Match[str]) -> str:
        href = match.group(1).strip()
        img_tag = match.group(2)
        if not href or href.startswith("data:"):
            return ""
        return f"[{_clean_label(_extract_html_alt(img_tag))}]({href})"

    def _replace_html_img(match: re.Match[str]) -> str:
        tag_str = match.group(0)
        m_src = re.search(r"\bsrc=[\"']([^\"']+)[\"']", tag_str, re.IGNORECASE)
        if not m_src:
            return ""
        src = m_src.group(1).strip()
        if not src or src.startswith("data:"):
            return ""
        return f"[{_clean_label(_extract_html_alt(tag_str))}]({src})"

    segments = _FENCED_CODE_BLOCK_RE.split(markdown_text)
    for idx in range(0, len(segments), 2):
        seg = segments[idx]
        seg = _LINKED_MD_IMG_RE.sub(_replace_linked_md_img, seg)
        seg = _MD_IMG_RE.sub(_replace_md_img, seg)
        seg = _HTML_LINKED_IMG_RE.sub(_replace_html_linked_img, seg)
        seg = _HTML_IMG_RE.sub(_replace_html_img, seg)
        segments[idx] = seg
    return "".join(segments)


def format_page_entry(
    *,
    title: str,
    canonical_url: str,
    bucket: str,
    verified_display: str,
    lastmod: str | None,
    body_markdown: str,
) -> str:
    """Format a page entry with the canonical Dash0 KB metadata header."""
    clean_title = " ".join(title.split()) or canonical_url
    lastmod_display = lastmod.strip() if lastmod and lastmod.strip() else "N/A"
    clean_body = convert_images_to_links(body_markdown.replace("\r\n", "\n")).strip()
    clean_body = re.sub(r"\n{3,}", "\n\n", clean_body)
    return (
        f"{PAGE_SEPARATOR}\n"
        f"# PAGE: {clean_title}\n"
        f"- **Canonical URL:** {canonical_url}\n"
        f"- **Section:** {bucket}\n"
        f"- **Last Verified:** {verified_display}\n"
        f"- **Last Modified (Sitemap):** {lastmod_display}\n"
        f"{PAGE_SEPARATOR}\n\n"
        f"{clean_body}\n"
    )


def build_extracted_page(
    *,
    url: str,
    bucket: str,
    title: str,
    lastmod: str | None,
    body_markdown: str,
    verified_iso: str | None = None,
    verified_display: str | None = None,
) -> ExtractedPage:
    """Construct an ExtractedPage with consistent metadata header and SHA-256 hash."""
    now_iso = verified_iso or utc_now_iso()
    now_disp = verified_display or utc_now_display()
    clean_title = " ".join(title.split()) or url
    clean_body = convert_images_to_links(body_markdown.replace("\r\n", "\n")).strip()
    clean_body = re.sub(r"\n{3,}", "\n\n", clean_body)
    content_hash = compute_content_hash(clean_title, url, clean_body)
    content_md = format_page_entry(
        title=clean_title,
        canonical_url=url,
        bucket=bucket,
        verified_display=now_disp,
        lastmod=lastmod,
        body_markdown=clean_body,
    )
    return ExtractedPage(
        url=url,
        bucket=bucket,
        title=clean_title,
        lastmod=lastmod,
        body_markdown=clean_body,
        content_markdown=content_md,
        content_hash=content_hash,
        verified_at=now_iso,
    )
