"""SQLite state persistence layer (`./data/dash0_state.db`) for incremental syncs and changelogs."""

from __future__ import annotations

import difflib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from dash0_kb_sync.config import ExtractedPage, strip_metadata_header, utc_now_iso


@dataclass
class PageRecord:
    """Stored row in the `pages` table."""

    url: str
    bucket: str
    title: str
    lastmod: str | None
    content_hash: str
    content_markdown: str
    first_seen_at: str
    last_updated_at: str
    last_checked_at: str
    is_removed: bool = False
    removed_at: str | None = None


@dataclass
class ChangeEvent:
    """Recorded addition, modification, or removal event for changelog compilation."""

    id: int
    event_type: Literal["added", "modified", "removed"]
    url: str
    bucket: str
    title: str
    occurred_at: str
    summary: str
    diff_summary: str


@dataclass
class DriveFileRecord:
    """Stored Google Drive file mapping in SQLite."""

    doc_key: str
    file_id: str
    web_view_link: str
    sha256: str
    char_count: int
    word_count: int
    page_count: int
    last_synced_at: str


def summarize_markdown_content(markdown_body: str, max_lines: int = 3, max_chars: int = 320) -> str:
    """Generate a concise 2-3 line plain-text summary from extracted Markdown body."""
    body = strip_metadata_header(markdown_body)
    # Strip GitHub file citation preamble (`- **Repository:** ... \n---\n`) if present
    if "- **Repository:**" in body[:500] and "\n---\n" in body[:900]:
        body = body.split("\n---\n", 1)[1]

    lines: list[str] = []
    in_code_block = False
    for raw in body.splitlines():
        s = raw.strip()
        if s.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block or not s:
            continue
        if s.startswith(("#", "|", "---", "===", "![")):
            continue
        # Clean markdown formatting links [text](url) -> text
        cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
        cleaned = re.sub(r"^>\s*", "", cleaned).strip()
        if len(cleaned) < 20:
            continue
        lines.append(cleaned)
        if sum(len(x) for x in lines) >= max_chars or len(lines) >= max_lines:
            break

    if not lines:
        # Fallback to headings if page only has code/tables/headings
        headings = [
            re.sub(r"^#+\s*", "", ln.strip())
            for ln in body.splitlines()
            if ln.strip().startswith("#")
        ]
        if headings:
            return "Topics covered: " + " · ".join(headings[:5])[:max_chars]
        return "Technical reference documentation and configuration examples."

    joined = " ".join(lines)
    if len(joined) > max_chars:
        joined = joined[: max_chars - 1].rsplit(" ", 1)[0] + "…"
    return joined


def _tag_markdown_lines(body: str) -> list[str]:
    """Prefix true Markdown headings vs code lines so `# comment` in YAML/Bash isn't treated as H1."""
    out: list[str] = []
    in_code = False
    for raw in body.splitlines():
        s = raw.rstrip()
        if s.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if not s.strip():
            continue
        if in_code:
            out.append(f"[CODE] {s.strip()}")
        else:
            out.append(s)
    return out


def compute_diff_summary(old_markdown: str, new_markdown: str, max_changes: int = 12) -> str:
    """Produce a concise, human-readable unified diff summary of headings and paragraphs."""
    old_body = strip_metadata_header(old_markdown)
    new_body = strip_metadata_header(new_markdown)

    old_lines = _tag_markdown_lines(old_body)
    new_lines = _tag_markdown_lines(new_body)

    diff = list(difflib.unified_diff(old_lines, new_lines, n=0, lineterm=""))
    added_headings: list[str] = []
    removed_headings: list[str] = []
    added_snippets: list[str] = []
    removed_snippets: list[str] = []

    for line in diff:
        if line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith("+"):
            content = line[1:].strip()
            if not content:
                continue
            if content.startswith("#"):
                added_headings.append(content)
            elif content.startswith("[CODE] "):
                code_ln = content[7:].strip()
                if len(code_ln) >= 18:
                    added_snippets.append(f"`{code_ln[:120]}`")
            elif len(content) >= 18:
                added_snippets.append(content[:140] + ("…" if len(content) > 140 else ""))
        elif line.startswith("-"):
            content = line[1:].strip()
            if not content:
                continue
            if content.startswith("#"):
                removed_headings.append(content)
            elif content.startswith("[CODE] "):
                code_ln = content[7:].strip()
                if len(code_ln) >= 18:
                    removed_snippets.append(f"`{code_ln[:120]}`")
            elif len(content) >= 18:
                removed_snippets.append(content[:140] + ("…" if len(content) > 140 else ""))

    bullets: list[str] = []
    for h in added_headings[:4]:
        bullets.append(f"+ Heading: `{h}`")
    for h in removed_headings[:3]:
        bullets.append(f"- Heading: `{h}`")
    for s in added_snippets[: max(2, max_changes - len(bullets))]:
        bullets.append(f"+ Added/Updated: {s}")
    if len(bullets) < 4:
        for s in removed_snippets[:2]:
            bullets.append(f"- Removed: {s}")

    if not bullets:
        return "Minor formatting, metadata, or code snippet adjustments."
    return "\n".join(bullets[:max_changes])


class StateDB:
    """SQLite state manager for Dash0 pages, GitHub SHAs, change history, and Drive IDs."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> StateDB:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.executescript(
            """
            PRAGMA journal_mode = WAL;
            PRAGMA synchronous = NORMAL;

            CREATE TABLE IF NOT EXISTS pages (
                url TEXT PRIMARY KEY,
                bucket TEXT NOT NULL,
                title TEXT NOT NULL,
                lastmod TEXT,
                content_hash TEXT NOT NULL,
                content_markdown TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_updated_at TEXT NOT NULL,
                last_checked_at TEXT NOT NULL,
                is_removed INTEGER NOT NULL DEFAULT 0,
                removed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_pages_bucket ON pages(bucket, is_removed);
            CREATE INDEX IF NOT EXISTS idx_pages_updated ON pages(last_updated_at);

            CREATE TABLE IF NOT EXISTS change_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                url TEXT NOT NULL,
                bucket TEXT NOT NULL,
                title TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                diff_summary TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_change_events_time ON change_events(occurred_at);

            CREATE TABLE IF NOT EXISTS github_repos (
                repo_name TEXT PRIMARY KEY,
                default_branch TEXT NOT NULL,
                last_commit_sha TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                last_checked_at TEXT NOT NULL,
                last_updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS drive_files (
                doc_key TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                web_view_link TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                char_count INTEGER NOT NULL DEFAULT 0,
                word_count INTEGER NOT NULL DEFAULT 0,
                page_count INTEGER NOT NULL DEFAULT 0,
                last_synced_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sync_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT INTO sync_metadata (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM sync_metadata WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row else None

    def get_page(self, url: str) -> PageRecord | None:
        row = self._conn.execute("SELECT * FROM pages WHERE url = ?", (url,)).fetchone()
        if not row:
            return None
        return self._row_to_page(row)

    def get_pages_by_bucket(self, bucket: str, include_removed: bool = False) -> list[PageRecord]:
        if include_removed:
            rows = self._conn.execute(
                "SELECT * FROM pages WHERE bucket = ? ORDER BY url ASC", (bucket,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM pages WHERE bucket = ? AND is_removed = 0 ORDER BY url ASC",
                (bucket,),
            ).fetchall()
        return [self._row_to_page(r) for r in rows]

    def get_all_active_pages(self) -> list[PageRecord]:
        rows = self._conn.execute(
            "SELECT * FROM pages WHERE is_removed = 0 ORDER BY bucket ASC, url ASC"
        ).fetchall()
        return [self._row_to_page(r) for r in rows]

    def should_rescrape_url(
        self,
        *,
        url: str,
        sitemap_lastmod: str | None,
        reverify_after_days: int | None,
        force_full: bool,
        now_dt: datetime | None = None,
    ) -> tuple[bool, str]:
        """Determine whether a discovered URL needs Playwright scraping."""
        if force_full:
            return True, "force-full"
        existing = self.get_page(url)
        if existing is None:
            return True, "new-url"
        if existing.is_removed:
            return True, "reappeared-url"
        if sitemap_lastmod and sitemap_lastmod != (existing.lastmod or ""):
            return True, "lastmod-changed"
        if reverify_after_days is not None:
            ref_now = now_dt or datetime.now(UTC)
            try:
                checked_dt = datetime.fromisoformat(existing.last_checked_at.replace("Z", "+00:00"))
                if ref_now - checked_dt > timedelta(days=reverify_after_days):
                    return True, f"stale>{reverify_after_days}d"
            except ValueError:
                return True, "invalid-checked-timestamp"
        return False, "up-to-date"

    def upsert_page(self, page: ExtractedPage) -> Literal["added", "modified", "unchanged"]:
        """Insert or update an extracted page, recording change events when content changes."""
        existing = self.get_page(page.url)
        now = page.verified_at or utc_now_iso()

        if existing is None:
            summary = summarize_markdown_content(page.body_markdown)
            self._conn.execute(
                """
                INSERT INTO pages (
                    url, bucket, title, lastmod, content_hash, content_markdown,
                    first_seen_at, last_updated_at, last_checked_at, is_removed, removed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
                """,
                (
                    page.url,
                    page.bucket,
                    page.title,
                    page.lastmod,
                    page.content_hash,
                    page.content_markdown,
                    now,
                    now,
                    now,
                ),
            )
            self._conn.execute(
                """
                INSERT INTO change_events (event_type, url, bucket, title, occurred_at, summary, diff_summary)
                VALUES ('added', ?, ?, ?, ?, ?, '')
                """,
                (page.url, page.bucket, page.title, now, summary),
            )
            self._conn.commit()
            return "added"

        if existing.content_hash != page.content_hash or existing.is_removed:
            diff_summary = compute_diff_summary(existing.content_markdown, page.content_markdown)
            summary = summarize_markdown_content(page.body_markdown)
            event_type = "added" if existing.is_removed else "modified"
            self._conn.execute(
                """
                UPDATE pages
                SET bucket = ?,
                    title = ?,
                    lastmod = COALESCE(?, lastmod),
                    content_hash = ?,
                    content_markdown = ?,
                    last_updated_at = ?,
                    last_checked_at = ?,
                    is_removed = 0,
                    removed_at = NULL
                WHERE url = ?
                """,
                (
                    page.bucket,
                    page.title,
                    page.lastmod,
                    page.content_hash,
                    page.content_markdown,
                    now,
                    now,
                    page.url,
                ),
            )
            self._conn.execute(
                """
                INSERT INTO change_events (event_type, url, bucket, title, occurred_at, summary, diff_summary)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    page.url,
                    page.bucket,
                    page.title,
                    now,
                    summary,
                    "" if event_type == "added" else diff_summary,
                ),
            )
            self._conn.commit()
            return "added" if existing.is_removed else "modified"

        # Content hash identical: refresh last_checked_at and lastmod without marking modified
        self._conn.execute(
            """
            UPDATE pages
            SET bucket = ?,
                title = ?,
                lastmod = COALESCE(?, lastmod),
                content_markdown = ?,
                last_checked_at = ?,
                is_removed = 0,
                removed_at = NULL
            WHERE url = ?
            """,
            (
                page.bucket,
                page.title,
                page.lastmod,
                page.content_markdown,
                now,
                page.url,
            ),
        )
        self._conn.commit()
        return "unchanged"

    def mark_url_removed(self, url: str, removed_at: str | None = None) -> bool:
        """Mark an existing URL as removed/deprecated (e.g. disappeared from sitemap & 404)."""
        existing = self.get_page(url)
        if existing is None or existing.is_removed:
            return False
        ts = removed_at or utc_now_iso()
        self._conn.execute(
            "UPDATE pages SET is_removed = 1, removed_at = ?, last_checked_at = ? WHERE url = ?",
            (ts, ts, url),
        )
        self._conn.execute(
            """
            INSERT INTO change_events (event_type, url, bucket, title, occurred_at, summary, diff_summary)
            VALUES ('removed', ?, ?, ?, ?, 'URL removed from sitemap and returned HTTP 404/410.', '')
            """,
            (url, existing.bucket, existing.title, ts),
        )
        self._conn.commit()
        return True

    def get_change_events_since(self, since_iso: str) -> list[ChangeEvent]:
        rows = self._conn.execute(
            """
            SELECT * FROM change_events
            WHERE occurred_at >= ?
            ORDER BY occurred_at DESC, id DESC
            """,
            (since_iso,),
        ).fetchall()
        return [
            ChangeEvent(
                id=int(r["id"]),
                event_type=r["event_type"],
                url=str(r["url"]),
                bucket=str(r["bucket"]),
                title=str(r["title"]),
                occurred_at=str(r["occurred_at"]),
                summary=str(r["summary"]),
                diff_summary=str(r["diff_summary"]),
            )
            for r in rows
        ]

    def get_bucket_counts(self) -> dict[str, dict[str, int | str | None]]:
        """Aggregate active page counts, char counts, and timestamps per bucket."""
        rows = self._conn.execute(
            """
            SELECT
                bucket,
                COUNT(*) AS page_count,
                COALESCE(SUM(LENGTH(content_markdown)), 0) AS char_count,
                MAX(last_updated_at) AS last_updated_at,
                MAX(last_checked_at) AS last_checked_at
            FROM pages
            WHERE is_removed = 0
            GROUP BY bucket
            """
        ).fetchall()
        result: dict[str, dict[str, int | str | None]] = {}
        for r in rows:
            result[str(r["bucket"])] = {
                "page_count": int(r["page_count"]),
                "char_count": int(r["char_count"]),
                "last_updated_at": r["last_updated_at"],
                "last_checked_at": r["last_checked_at"],
            }
        return result

    # --- GitHub Repository SHA Tracking ---

    def get_github_repo_sha(self, repo_name: str) -> str | None:
        row = self._conn.execute(
            "SELECT last_commit_sha FROM github_repos WHERE repo_name = ?", (repo_name,)
        ).fetchone()
        return str(row["last_commit_sha"]) if row else None

    def get_known_github_repos(self) -> list[dict[str, str]]:
        rows = self._conn.execute("SELECT * FROM github_repos ORDER BY repo_name ASC").fetchall()
        return [dict(r) for r in rows]

    def upsert_github_repo(
        self,
        *,
        repo_name: str,
        default_branch: str,
        commit_sha: str,
        description: str,
        checked_at: str | None = None,
        changed: bool = True,
    ) -> None:
        now = checked_at or utc_now_iso()
        existing_sha = self.get_github_repo_sha(repo_name)
        if existing_sha is None:
            self._conn.execute(
                """
                INSERT INTO github_repos (
                    repo_name, default_branch, last_commit_sha, description, last_checked_at, last_updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (repo_name, default_branch, commit_sha, description, now, now),
            )
        else:
            self._conn.execute(
                """
                UPDATE github_repos
                SET default_branch = ?,
                    last_commit_sha = ?,
                    description = ?,
                    last_checked_at = ?,
                    last_updated_at = CASE WHEN ? THEN ? ELSE last_updated_at END
                WHERE repo_name = ?
                """,
                (repo_name, default_branch, commit_sha, now, 1 if changed else 0, now, repo_name),
            )
        self._conn.commit()

    # --- Google Drive File Mapping ---

    def upsert_drive_file(self, rec: DriveFileRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO drive_files (
                doc_key, file_id, web_view_link, sha256, char_count, word_count, page_count, last_synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(doc_key) DO UPDATE SET
                file_id = excluded.file_id,
                web_view_link = excluded.web_view_link,
                sha256 = excluded.sha256,
                char_count = excluded.char_count,
                word_count = excluded.word_count,
                page_count = excluded.page_count,
                last_synced_at = excluded.last_synced_at
            """,
            (
                rec.doc_key,
                rec.file_id,
                rec.web_view_link,
                rec.sha256,
                rec.char_count,
                rec.word_count,
                rec.page_count,
                rec.last_synced_at,
            ),
        )
        self._conn.commit()

    def get_drive_files(self) -> dict[str, DriveFileRecord]:
        rows = self._conn.execute("SELECT * FROM drive_files ORDER BY doc_key ASC").fetchall()
        return {
            str(r["doc_key"]): DriveFileRecord(
                doc_key=str(r["doc_key"]),
                file_id=str(r["file_id"]),
                web_view_link=str(r["web_view_link"]),
                sha256=str(r["sha256"]),
                char_count=int(r["char_count"]),
                word_count=int(r["word_count"]),
                page_count=int(r["page_count"]),
                last_synced_at=str(r["last_synced_at"]),
            )
            for r in rows
        }

    @staticmethod
    def _row_to_page(row: sqlite3.Row) -> PageRecord:
        return PageRecord(
            url=str(row["url"]),
            bucket=str(row["bucket"]),
            title=str(row["title"]),
            lastmod=str(row["lastmod"]) if row["lastmod"] is not None else None,
            content_hash=str(row["content_hash"]),
            content_markdown=str(row["content_markdown"]),
            first_seen_at=str(row["first_seen_at"]),
            last_updated_at=str(row["last_updated_at"]),
            last_checked_at=str(row["last_checked_at"]),
            is_removed=bool(row["is_removed"]),
            removed_at=str(row["removed_at"]) if row["removed_at"] is not None else None,
        )
