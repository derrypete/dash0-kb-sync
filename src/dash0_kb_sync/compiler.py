"""Markdown bundle compiler with Master Table of Contents and Google Docs / NotebookLM auto-splitting."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path

from dash0_kb_sync.config import (
    API_REFERENCE_BUCKET,
    BUCKET_MAP,
    BUCKETS,
    CHANGELOG_BUCKET,
    GITHUB_BUCKET,
    PRIORITY_GITHUB_REPOS,
    SyncSettings,
    _split_outside_code_fences,
    convert_images_to_links,
    count_code_blocks,
    count_markdown_tables,
    normalize_for_hash,
    utc_now_display,
)
from dash0_kb_sync.db import PageRecord, StateDB

logger = logging.getLogger(__name__)

_COMPILED_AT_RE = re.compile(r"^- \*\*Compiled At:\*\* .*$", re.MULTILINE)


def compute_bundle_hash(rendered_markdown: str) -> str:
    """Compute a timestamp-invariant SHA-256 hash of a compiled Markdown bundle.

    Strips volatile `Compiled At:` and `Last Verified:` lines so unchanged buckets
    never trigger unnecessary Google Docs re-uploads on subsequent runs.
    """
    scrubbed = _COMPILED_AT_RE.sub("- **Compiled At:** <normalized>", rendered_markdown)
    return hashlib.sha256(normalize_for_hash(scrubbed).encode("utf-8")).hexdigest()


@dataclass
class CompiledBundle:
    """Metadata for a compiled Markdown file written to `./output/markdown/`."""

    doc_key: str
    bucket: str
    part_index: int
    total_parts: int
    file_path: Path
    page_count: int
    char_count: int
    word_count: int
    sha256: str


def count_words(text: str) -> int:
    """Return whitespace-delimited word count."""
    return len(re.findall(r"\S+", text))


def _sort_bucket_pages(bucket: str, pages: list[PageRecord]) -> list[PageRecord]:
    """Sort pages deterministically within a bucket so split boundaries stay stable."""
    spec = BUCKET_MAP.get(bucket)
    root_url = (spec.root_url or "").rstrip("/") if spec else ""

    if bucket == API_REFERENCE_BUCKET:
        def _api_key(p: PageRecord) -> tuple[int, str]:
            if p.url == root_url:
                return (0, "")
            if "#tag-" in p.url:
                return (1, p.url)
            if "#schemas-" in p.url:
                return (2, p.url)
            return (3, p.url)

        return sorted(pages, key=_api_key)

    if bucket == GITHUB_BUCKET:
        def _gh_key(p: PageRecord) -> tuple[int, int, str, int, str]:
            m = re.match(r"^https://github\.com/[^/]+/([^/]+)/blob/[^/]+/(.+)$", p.url)
            if not m:
                return (9, 999, p.url, 9, p.url)
            repo_name, rel_path = m.group(1), m.group(2).lower()
            if repo_name in PRIORITY_GITHUB_REPOS:
                repo_group, repo_idx = 0, PRIORITY_GITHUB_REPOS.index(repo_name)
            else:
                repo_group, repo_idx = 1, 0
            if rel_path == "readme.md":
                file_rank = 0
            elif rel_path == "changelog.md":
                file_rank = 1
            elif rel_path.endswith(("values.yaml", "values.yml")):
                file_rank = 2
            elif "/docs/" in f"/{rel_path}":
                file_rank = 3
            else:
                file_rank = 4
            return (repo_group, repo_idx, repo_name.lower(), file_rank, rel_path)

        return sorted(pages, key=_gh_key)

    def _web_key(p: PageRecord) -> tuple[int, str]:
        return (0 if p.url == root_url else 1, p.url)

    return sorted(pages, key=_web_key)


def _split_markdown_sections_fence_safe(markdown_text: str) -> list[str]:
    """Split Markdown along `## ` headings strictly outside fenced code blocks."""
    sections: list[str] = []
    cur_buf: list[str] = []

    for is_code, seg in _split_outside_code_fences(markdown_text):
        if is_code:
            cur_buf.append(seg)
            continue
        parts = re.split(r"(?m)(?=^## )", seg)
        for idx, part in enumerate(parts):
            if idx > 0 and part.startswith("## ") and cur_buf:
                sections.append("".join(cur_buf).strip())
                cur_buf = [part]
            else:
                cur_buf.append(part)

    if cur_buf:
        tail = "".join(cur_buf).strip()
        if tail:
            sections.append(tail)

    if len(sections) > 1:
        return sections

    # Fallback: split along double-newlines outside code fences
    para_sections: list[str] = []
    p_buf: list[str] = []
    for is_code, seg in _split_outside_code_fences(markdown_text):
        if is_code:
            p_buf.append(seg)
            continue
        paras = seg.split("\n\n")
        for idx, para in enumerate(paras):
            if idx > 0 and p_buf:
                joined = "".join(p_buf).strip()
                if joined:
                    para_sections.append(joined)
                p_buf = [para]
            else:
                p_buf.append(para)
    if p_buf:
        joined = "".join(p_buf).strip()
        if joined:
            para_sections.append(joined)
    return para_sections or [markdown_text]


def _split_oversized_entry(
    page: PageRecord,
    max_chars: int,
    max_words: int,
    max_tables: int,
    max_blocks: int,
    max_lines: int,
) -> list[PageRecord]:
    """Safely split a single oversized PageRecord along fence-safe `## ` headings if it exceeds budgets."""
    md = page.content_markdown
    p_tables = count_markdown_tables(md)
    p_blocks = p_tables + count_code_blocks(md)
    p_lines = md.count("\n")
    if (
        len(md) <= max_chars
        and count_words(md) <= max_words
        and p_tables <= max_tables
        and p_blocks <= max_blocks
        and p_lines <= max_lines
    ):
        return [page]

    sections = _split_markdown_sections_fence_safe(md)

    chunks: list[str] = []
    cur: list[str] = []
    cur_chars = 0
    cur_words = 0
    cur_tables = 0
    cur_blocks = 0
    cur_lines = 0

    for sec in sections:
        s_chars = len(sec) + 2
        s_words = count_words(sec)
        s_tables = count_markdown_tables(sec)
        s_blocks = s_tables + count_code_blocks(sec)
        s_lines = sec.count("\n") + 2
        if cur and (
            cur_chars + s_chars > max_chars
            or cur_words + s_words > max_words
            or cur_tables + s_tables > max_tables
            or cur_blocks + s_blocks > max_blocks
            or cur_lines + s_lines > max_lines
        ):
            chunks.append("\n\n".join(cur).strip())
            cur = [sec]
            cur_chars = s_chars
            cur_words = s_words
            cur_tables = s_tables
            cur_blocks = s_blocks
            cur_lines = s_lines
        else:
            cur.append(sec)
            cur_chars += s_chars
            cur_words += s_words
            cur_tables += s_tables
            cur_blocks += s_blocks
            cur_lines += s_lines

    if cur:
        chunks.append("\n\n".join(cur).strip())

    result: list[PageRecord] = []
    for idx, text in enumerate(chunks, start=1):
        result.append(
            PageRecord(
                url=f"{page.url}#chunk-{idx}" if len(chunks) > 1 else page.url,
                bucket=page.bucket,
                title=f"{page.title} (Section {idx}/{len(chunks)})" if len(chunks) > 1 else page.title,
                lastmod=page.lastmod,
                content_hash=page.content_hash,
                content_markdown=text + "\n",
                first_seen_at=page.first_seen_at,
                last_updated_at=page.last_updated_at,
                last_checked_at=page.last_checked_at,
            )
        )
    return result


def _render_bundle_markdown(
    *,
    bucket: str,
    part_index: int,
    total_parts: int,
    pages: list[PageRecord],
    generated_display: str,
) -> str:
    """Assemble the Master Table of Contents and sequential page entries for one output document."""
    part_suffix = f" — Part {part_index} of {total_parts}" if total_parts > 1 else ""
    spec = BUCKET_MAP.get(bucket)
    desc = spec.description if spec else ""

    toc_lines: list[str] = [
        f"# {bucket}{part_suffix}",
        "",
    ]
    if desc:
        toc_lines.extend([f"> {desc}", ""])

    toc_lines.extend(
        [
            f"- **Bucket:** `{bucket}`",
            f"- **Part:** `{part_index} of {total_parts}`",
            f"- **Included Entries:** `{len(pages)}`",
            f"- **Compiled At:** `{generated_display}`",
            "",
            "## Master Table of Contents",
            "",
        ]
    )

    for idx, p in enumerate(pages, start=1):
        clean_title = p.title.replace("[", "(").replace("]", ")")
        toc_lines.append(f"{idx}. [{clean_title}]({p.url})")

    toc_lines.extend(["", "---", ""])
    header_block = "\n".join(toc_lines)
    body_block = "\n\n".join(p.content_markdown.strip() for p in pages)
    return f"{header_block}\n{body_block}\n"


def compile_bucket(
    bucket: str,
    pages: list[PageRecord],
    settings: SyncSettings,
    generated_display: str | None = None,
) -> list[CompiledBundle]:
    """Compile a single bucket into one or more deterministic Markdown files under `./output/markdown/`."""
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    gen_disp = generated_display or utc_now_display()

    ordered = _sort_bucket_pages(bucket, pages)

    # Reserve headroom for Master Table of Contents (~160 chars & ~20 words per TOC line + 1,200 base)
    base_char_reserve = 1_500
    base_word_reserve = 250
    base_line_reserve = 20
    max_bucket_chars = (
        min(settings.max_chars_per_doc, 320_000)
        if bucket == API_REFERENCE_BUCKET
        else settings.max_chars_per_doc
    )
    max_bucket_tables = (
        settings.max_tables_per_doc
        if bucket == API_REFERENCE_BUCKET
        else min(settings.max_tables_per_doc, 180)
    )
    effective_page_max_chars = max(20_000, max_bucket_chars - 25_000)
    effective_page_max_words = max(5_000, settings.max_words_per_doc - 4_000)
    effective_page_max_tables = max(30, max_bucket_tables - 20)
    effective_page_max_blocks = max(40, settings.max_blocks_per_doc - 30)
    effective_page_max_lines = max(800, settings.max_lines_per_doc - 800)

    normalized_pages: list[PageRecord] = []
    for p in ordered:
        cleaned_md = convert_images_to_links(p.content_markdown)
        page_clean = replace(p, content_markdown=cleaned_md) if cleaned_md != p.content_markdown else p
        normalized_pages.extend(
            _split_oversized_entry(
                page_clean,
                effective_page_max_chars,
                effective_page_max_words,
                effective_page_max_tables,
                effective_page_max_blocks,
                effective_page_max_lines,
            )
        )

    if not normalized_pages:
        return []

    # Partition deterministically along page boundaries respecting char, word, table, block, and line budgets
    parts: list[list[PageRecord]] = []
    current_part: list[PageRecord] = []
    cur_chars = base_char_reserve
    cur_words = base_word_reserve
    cur_tables = 0
    cur_blocks = 0
    cur_lines = base_line_reserve

    for p in normalized_pages:
        toc_line_est_chars = len(p.title) + len(p.url) + 16
        toc_line_est_words = count_words(p.title) + 3
        entry_chars = len(p.content_markdown) + 2 + toc_line_est_chars
        entry_words = count_words(p.content_markdown) + toc_line_est_words
        entry_tables = count_markdown_tables(p.content_markdown)
        entry_blocks = entry_tables + count_code_blocks(p.content_markdown)
        entry_lines = p.content_markdown.count("\n") + 3

        if current_part and (
            cur_chars + entry_chars > max_bucket_chars
            or cur_words + entry_words > settings.max_words_per_doc
            or cur_tables + entry_tables > max_bucket_tables
            or cur_blocks + entry_blocks > settings.max_blocks_per_doc
            or cur_lines + entry_lines > settings.max_lines_per_doc
        ):
            parts.append(current_part)
            current_part = [p]
            cur_chars = base_char_reserve + entry_chars
            cur_words = base_word_reserve + entry_words
            cur_tables = entry_tables
            cur_blocks = entry_blocks
            cur_lines = base_line_reserve + entry_lines
        else:
            current_part.append(p)
            cur_chars += entry_chars
            cur_words += entry_words
            cur_tables += entry_tables
            cur_blocks += entry_blocks
            cur_lines += entry_lines

    if current_part:
        parts.append(current_part)

    total_parts = len(parts)
    written_bundles: list[CompiledBundle] = []
    expected_filenames: set[str] = set()

    for idx, part_pages in enumerate(parts, start=1):
        doc_key = bucket if total_parts == 1 else f"{bucket}_Part_{idx}"
        filename = f"{doc_key}.md"
        expected_filenames.add(filename)
        file_path = settings.output_dir / filename

        rendered = _render_bundle_markdown(
            bucket=bucket,
            part_index=idx,
            total_parts=total_parts,
            pages=part_pages,
            generated_display=gen_disp,
        )
        sha256 = compute_bundle_hash(rendered)

        if bucket != CHANGELOG_BUCKET and file_path.exists():
            try:
                prev_text = file_path.read_text(encoding="utf-8")
            except OSError:
                prev_text = None
            if prev_text is not None and compute_bundle_hash(prev_text) == sha256:
                rendered = prev_text
            else:
                file_path.write_text(rendered, encoding="utf-8")
        else:
            file_path.write_text(rendered, encoding="utf-8")

        char_count = len(rendered)
        word_count = count_words(rendered)

        written_bundles.append(
            CompiledBundle(
                doc_key=doc_key,
                bucket=bucket,
                part_index=idx,
                total_parts=total_parts,
                file_path=file_path,
                page_count=len(part_pages),
                char_count=char_count,
                word_count=word_count,
                sha256=sha256,
            )
        )

    # Clean up stale local `.md` files for this bucket (e.g. if split count changed)
    for old_file in settings.output_dir.glob(f"{bucket}*.md"):
        if old_file.name not in expected_filenames:
            if old_file.name == f"{bucket}.md" or old_file.name.startswith(f"{bucket}_Part_"):
                old_file.unlink(missing_ok=True)

    return written_bundles


def compile_all_buckets(
    db: StateDB,
    settings: SyncSettings,
    target_buckets: set[str] | None = None,
) -> list[CompiledBundle]:
    """Compile all active buckets (or `target_buckets`) from SQLite into `./output/markdown/`."""
    gen_disp = utc_now_display()
    bundles: list[CompiledBundle] = []
    for b in BUCKETS:
        if target_buckets is not None and b.name not in target_buckets:
            continue
        pages = db.get_pages_by_bucket(b.name, include_removed=False)
        if not pages:
            continue
        bucket_bundles = compile_bucket(b.name, pages, settings, generated_display=gen_disp)
        bundles.extend(bucket_bundles)
    return bundles
