"""Generator for `00_Dash0_Whats_New_Changelog` summarizing sync health, diffs, and deprecations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dash0_kb_sync.config import (
    BUCKETS,
    CHANGELOG_BUCKET,
    GITHUB_BUCKET,
    ExtractedPage,
    build_extracted_page,
    utc_now_display,
    utc_now_iso,
)
from dash0_kb_sync.db import ChangeEvent, PageRecord, StateDB, summarize_markdown_content


def _parse_iso_safe(raw: str | None) -> datetime | None:
    if not raw:
        return None
    cleaned = raw.strip()
    try:
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None


def _render_window_section(
    *,
    heading: str,
    window_days: int,
    events: list[ChangeEvent],
    all_active_pages: list[PageRecord],
    now_dt: datetime,
) -> str:
    """Render a 7-day or 30-day change section (Added, Modified, Removed)."""
    cutoff = now_dt - timedelta(days=window_days)

    # Deduplicate events by URL per event_type (keep latest event in window)
    added_by_url: dict[str, ChangeEvent] = {}
    modified_by_url: dict[str, ChangeEvent] = {}
    removed_by_url: dict[str, ChangeEvent] = {}

    for ev in events:
        if ev.bucket == CHANGELOG_BUCKET:
            continue
        if ev.event_type == "added" and ev.url not in added_by_url:
            added_by_url[ev.url] = ev
        elif ev.event_type == "modified" and ev.url not in modified_by_url:
            modified_by_url[ev.url] = ev
        elif ev.event_type == "removed" and ev.url not in removed_by_url:
            removed_by_url[ev.url] = ev

    lines: list[str] = [f"## {heading}", ""]

    # --- 1. Newly Added Pages / Repos ---
    added_list = list(added_by_url.values())
    lines.append(f"### Newly Added Pages & Repo Files ({len(added_list)})")
    lines.append("")

    if not added_list:
        lines.append("*No newly added pages or repository files recorded in this window.*")
        lines.append("")
    elif len(added_list) <= 60:
        for ev in added_list:
            date_str = ev.occurred_at[:10]
            lines.append(f"- **[{ev.title}]({ev.url})** (`{ev.bucket}` · Added `{date_str}`)")
            if ev.summary:
                lines.append(f"  - {ev.summary}")
        lines.append("")
    else:
        # Large initial bootstrap run: highlight pages whose upstream `<lastmod>` falls in window
        recent_upstream: list[PageRecord] = []
        for p in all_active_pages:
            if p.bucket == CHANGELOG_BUCKET:
                continue
            lmod_dt = _parse_iso_safe(p.lastmod)
            if lmod_dt and lmod_dt >= cutoff:
                recent_upstream.append(p)
        recent_upstream.sort(key=lambda p: p.lastmod or "", reverse=True)

        lines.append(
            f"> **Initial Baseline Sync Note:** `{len(added_list)}` total pages/files were indexed "
            f"into the knowledge base during this window. Below are the **{min(len(recent_upstream), 45)}** "
            f"entries with upstream updates (`<lastmod>` / GitHub push) in the last {window_days} days:"
        )
        lines.append("")
        shown = recent_upstream[:45] if recent_upstream else all_active_pages[:30]
        for p in shown:
            lmod_label = (p.lastmod or p.first_seen_at)[:10]
            summary = summarize_markdown_content(p.content_markdown)
            lines.append(
                f"- **[{p.title}]({p.url})** (`{p.bucket}` · Upstream Updated `{lmod_label}`)"
            )
            lines.append(f"  - {summary}")
        if len(recent_upstream) > 45:
            lines.append(
                f"- *…plus {len(recent_upstream) - 45} additional recently updated entries across buckets.*"
            )
        lines.append("")

    # --- 2. Modified Pages / Repos ---
    modified_list = list(modified_by_url.values())
    lines.append(f"### Modified Pages & Repo Files ({len(modified_list)})")
    lines.append("")

    if not modified_list:
        lines.append("*No modified pages or repository files detected in this window.*")
        lines.append("")
    else:
        for ev in modified_list[:60]:
            date_str = ev.occurred_at[:16].replace("T", " ") + " UTC"
            lines.append(f"- **[{ev.title}]({ev.url})** (`{ev.bucket}` · Modified `{date_str}`)")
            if ev.diff_summary:
                for diff_line in ev.diff_summary.splitlines()[:8]:
                    lines.append(f"  - {diff_line}")
            elif ev.summary:
                lines.append(f"  - {ev.summary}")
        if len(modified_list) > 60:
            lines.append(f"- *…plus {len(modified_list) - 60} additional modified entries.*")
        lines.append("")

    # --- 3. Removed / Deprecated URLs ---
    removed_list = list(removed_by_url.values())
    lines.append(f"### Removed / Deprecated URLs ({len(removed_list)})")
    lines.append("")

    if not removed_list:
        lines.append("*No removed or deprecated (404) URLs detected in this window.*")
        lines.append("")
    else:
        for ev in removed_list[:50]:
            date_str = ev.occurred_at[:10]
            lines.append(
                f"- ~~{ev.title}~~ — `{ev.url}` (`{ev.bucket}` · Removed `{date_str}`)"
            )
        lines.append("")

    return "\n".join(lines).strip()


def generate_changelog_page(
    db: StateDB,
    *,
    sync_mode: str = "incremental",
) -> ExtractedPage:
    """Build and persist `00_Dash0_Whats_New_Changelog` in SQLite and return it."""
    now_dt = datetime.now(UTC)
    now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    now_disp = utc_now_display()

    bucket_counts = db.get_bucket_counts()
    known_repos = db.get_known_github_repos()
    all_active_pages = db.get_all_active_pages()

    total_web_pages = sum(
        int(bucket_counts.get(b.name, {}).get("page_count", 0) or 0)
        for b in BUCKETS
        if b.name not in (CHANGELOG_BUCKET, GITHUB_BUCKET)
    )
    total_gh_files = int(bucket_counts.get(GITHUB_BUCKET, {}).get("page_count", 0) or 0)

    lines: list[str] = [
        "# Dash0 Knowledge Base — What's New & Sync Health Changelog",
        "",
        "Use this document in **NotebookLM** or **Gemini Gems** to answer questions such as:",
        '- *"What changed in Dash0 this week?"*',
        '- *"Which new integrations, API endpoints, or blog posts were added recently?"*',
        '- *"Have any Dash0 documentation URLs been deprecated or removed?"*',
        "",
        "## 1. Sync Health Summary",
        "",
        f"- **Last Sync Timestamp:** `{now_disp}`",
        f"- **Sync Mode:** `{sync_mode}`",
        f"- **Total Website & API Reference Entries Indexed:** `{total_web_pages:,}`",
        f"- **Total GitHub Repositories Tracked:** `{len(known_repos):,}` (`{total_gh_files:,}` extracted files)",
        "",
        "| Bucket | Indexed Entries | Total Characters | Last Content Update | Last Verified |",
        "| :--- | ---: | ---: | :--- | :--- |",
    ]

    for b in BUCKETS:
        if b.name == CHANGELOG_BUCKET:
            continue
        stats = bucket_counts.get(b.name, {})
        p_count = int(stats.get("page_count", 0) or 0)
        c_count = int(stats.get("char_count", 0) or 0)
        upd = str(stats.get("last_updated_at") or "—")[:16].replace("T", " ")
        chk = str(stats.get("last_checked_at") or "—")[:16].replace("T", " ")
        lines.append(f"| `{b.name}` | {p_count:,} | {c_count:,} | {upd} | {chk} |")

    lines.append("")

    # 7-Day and 30-Day Change Windows
    since_7d = (now_dt - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    since_30d = (now_dt - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    events_7d = db.get_change_events_since(since_7d)
    events_30d = db.get_change_events_since(since_30d)

    lines.append(
        _render_window_section(
            heading="2. Changes in the Last 7 Days",
            window_days=7,
            events=events_7d,
            all_active_pages=all_active_pages,
            now_dt=now_dt,
        )
    )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        _render_window_section(
            heading="3. Changes in the Last 30 Days",
            window_days=30,
            events=events_30d,
            all_active_pages=all_active_pages,
            now_dt=now_dt,
        )
    )

    body_md = "\n".join(lines).strip()
    changelog_page = build_extracted_page(
        url="https://www.dash0.com/#whats-new-changelog",
        bucket=CHANGELOG_BUCKET,
        title="Dash0 What's New & Sync Health Changelog",
        lastmod=now_iso,
        body_markdown=body_md,
        verified_iso=now_iso,
        verified_display=now_disp,
    )
    db.upsert_page(changelog_page)
    return changelog_page
