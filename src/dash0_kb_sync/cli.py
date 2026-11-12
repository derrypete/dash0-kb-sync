"""Typer + Rich CLI for `dash0-kb-sync` (`auth`, `run`, and `status`)."""

from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from dash0_kb_sync.changelog import generate_changelog_page
from dash0_kb_sync.compiler import CompiledBundle, compile_all_buckets, count_words
from dash0_kb_sync.config import (
    API_REFERENCE_BUCKET,
    BUCKET_MAP,
    BUCKETS,
    CHANGELOG_BUCKET,
    GITHUB_BUCKET,
    DiscoveredURL,
    SyncSettings,
    resolve_bucket_name,
    utc_now_iso,
)
from dash0_kb_sync.db import StateDB
from dash0_kb_sync.discovery import (
    canonicalize_dash0_url,
    discover_website_urls,
    match_bucket_for_url,
    verify_removed_urls,
)
from dash0_kb_sync.extractor import BrowserExtractor, ScrapeOutcome
from dash0_kb_sync.gdrive_sync import DriveAuthError, GoogleDriveSyncEngine
from dash0_kb_sync.github_fetcher import sync_github_repositories
from dash0_kb_sync.openapi_parser import extract_api_reference

app = typer.Typer(
    name="dash0-kb-sync",
    help=(
        "Production-grade Dash0 documentation, OpenAPI, and GitHub knowledge-base "
        "extractor with in-place Google Docs sync for NotebookLM & Gemini Gems."
    ),
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
logger = logging.getLogger("dash0_kb_sync")


def _configure_logging(settings: SyncSettings, verbose: bool = False) -> None:
    settings.ensure_directories()
    root_logger = logging.getLogger("dash0_kb_sync")
    root_logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    root_logger.handlers.clear()

    file_handler = RotatingFileHandler(
        settings.log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)


@app.command("auth")
def auth_command() -> None:
    """Run one-time browser OAuth 2.0 login and verify the `Dash0_Knowledge_Base` Drive folder."""
    settings = SyncSettings()
    _configure_logging(settings)

    console.print(
        Panel.fit(
            "[bold cyan]Dash0 Knowledge Base — Google Drive OAuth 2.0 Setup[/bold cyan]\n"
            f"Credentials path: [yellow]{settings.credentials_path}[/yellow]\n"
            f"Token path:       [yellow]{settings.token_path}[/yellow]\n"
            f"Target scope:     [green]{settings.drive_scopes[0]}[/green]",
            title="Authentication",
        )
    )

    with StateDB(settings.db_path) as db:
        try:
            engine = GoogleDriveSyncEngine(settings, db, interactive=True)
            folder_id, folder_url = engine.ensure_drive_folder()
        except DriveAuthError as exc:
            console.print(
                Panel(
                    f"[bold red]Authentication Setup Required[/bold red]\n\n{exc}\n\n"
                    "[bold]Quick Setup Steps:[/bold]\n"
                    "1. Open [link=https://console.cloud.google.com/apis/library/drive.googleapis.com]Google Cloud Console -> Google Drive API[/link] and click [bold]Enable[/bold].\n"
                    "2. Go to [bold]APIs & Services -> Credentials -> Create Credentials -> OAuth client ID[/bold].\n"
                    "3. Choose Application type: [bold]Desktop app[/bold] and download the JSON.\n"
                    f"4. Save that file as [cyan]{settings.credentials_path}[/cyan] and re-run:\n"
                    "   [bold green]uv run python -m dash0_kb_sync auth[/bold green]",
                    title="Missing OAuth Credentials",
                    border_style="red",
                )
            )
            raise typer.Exit(code=1) from None

    console.print(
        Panel.fit(
            f"[bold green]Google Drive authentication succeeded![/bold green]\n"
            f"- Folder Name: [bold]{settings.drive_folder_name}[/bold]\n"
            f"- Folder ID:   [cyan]{folder_id}[/cyan]\n"
            f"- Folder URL:  [link={folder_url}]{folder_url}[/link]\n"
            f"- Manifest:    [dim]{settings.drive_manifest_path}[/dim]",
            title="Ready for Sync",
            border_style="green",
        )
    )


async def _run_pipeline_async(
    settings: SyncSettings,
    *,
    force_full: bool,
    selected_buckets: set[str] | None,
    explicit_urls: list[str] | None,
    skip_drive: bool,
    drive_only: bool,
    max_pages_per_bucket: int | None,
    max_repos: int | None,
) -> int:
    sync_mode = "drive-only" if drive_only else ("force-full" if force_full else "incremental")
    logger.info(
        "Starting dash0-kb-sync pipeline (mode=%s, buckets=%s, skip_drive=%s, drive_only=%s)",
        sync_mode,
        sorted(selected_buckets) if selected_buckets else "ALL",
        skip_drive,
        drive_only,
    )

    with StateDB(settings.db_path) as db:
        # Initial changelog snapshot at start of run
        generate_changelog_page(db, sync_mode=sync_mode)

        target_web_buckets: set[str] = set()
        if not drive_only:
            for b in BUCKETS:
                if b.kind in ("web", "openapi"):
                    if selected_buckets is None or b.name in selected_buckets:
                        target_web_buckets.add(b.name)

        run_github = (not drive_only) and (selected_buckets is None or GITHUB_BUCKET in selected_buckets)

        discovered_by_bucket: dict[str, list[DiscoveredURL]] = {}
        if target_web_buckets:
            with console.status("[bold cyan]Discovering URLs from sitemap.xml & root indexes...[/bold cyan]"):
                discovered_by_bucket = await discover_website_urls(
                    settings, target_buckets=target_web_buckets
                )
            total_discovered = sum(len(v) for v in discovered_by_bucket.values())
            console.print(
                f"[green]✓[/green] Discovered [bold]{total_discovered:,}[/bold] canonical URLs "
                f"across [bold]{len(target_web_buckets)}[/bold] website/API buckets."
            )
            logger.info("Discovered %d URLs across %d buckets", total_discovered, len(target_web_buckets))

            # Check for URLs that disappeared from sitemap.xml & return 404 (only on unbounded bucket runs)
            if max_pages_per_bucket is None:
                missing_candidates: list[str] = []
                for b_name in target_web_buckets:
                    if b_name == API_REFERENCE_BUCKET:
                        continue
                    live_set = {d.url for d in discovered_by_bucket.get(b_name, [])}
                    for stored in db.get_pages_by_bucket(b_name, include_removed=False):
                        if stored.url not in live_set:
                            missing_candidates.append(stored.url)
                if missing_candidates:
                    gone_404 = await verify_removed_urls(missing_candidates)
                    for dead_url in gone_404:
                        db.mark_url_removed(dead_url)
                    if gone_404:
                        console.print(
                            f"[yellow]![/yellow] Marked [bold]{len(gone_404)}[/bold] deprecated/404 URLs as removed."
                        )

        # Queue HTML URLs that require Playwright scraping
        scrape_queue: list[DiscoveredURL] = []
        queued_urls_seen: set[str] = set()
        skipped_up_to_date = 0
        for b_name, urls in discovered_by_bucket.items():
            if b_name == API_REFERENCE_BUCKET:
                continue
            spec = BUCKET_MAP[b_name]
            bucket_slice = urls[:max_pages_per_bucket] if max_pages_per_bucket else urls
            for item in bucket_slice:
                should_scrape, _ = db.should_rescrape_url(
                    url=item.url,
                    sitemap_lastmod=item.lastmod,
                    reverify_after_days=spec.reverify_after_days,
                    force_full=force_full,
                )
                if should_scrape:
                    scrape_queue.append(item)
                    queued_urls_seen.add(item.url)
                else:
                    skipped_up_to_date += 1

        if explicit_urls and not drive_only:
            for raw_u in explicit_urls:
                canon_u = canonicalize_dash0_url(raw_u)
                if not canon_u or canon_u in queued_urls_seen:
                    continue
                matched_b = match_bucket_for_url(canon_u)
                if matched_b and matched_b != API_REFERENCE_BUCKET:
                    scrape_queue.append(
                        DiscoveredURL(url=canon_u, bucket=matched_b, lastmod=None, source="index_links")
                    )
                    queued_urls_seen.add(canon_u)
                    if selected_buckets is not None:
                        selected_buckets.add(matched_b)

        need_browser = bool(scrape_queue) or (API_REFERENCE_BUCKET in target_web_buckets)
        pages_added = 0
        pages_modified = 0
        pages_unchanged = skipped_up_to_date
        scrape_errors = 0
        total_tabs_expanded = 0

        if need_browser:
            async with BrowserExtractor(settings) as extractor:
                # 1. Special OpenAPI Compiler for `03_Dash0_API_Reference`
                if API_REFERENCE_BUCKET in target_web_buckets:
                    with console.status(
                        "[bold cyan]Locating & compiling Dash0 OpenAPI 3.1 specification...[/bold cyan]"
                    ):
                        fallback_api_urls = discovered_by_bucket.get(API_REFERENCE_BUCKET, [])
                        if max_pages_per_bucket:
                            fallback_api_urls = fallback_api_urls[:max_pages_per_bucket]
                        api_pages = await extract_api_reference(
                            settings, extractor, fallback_api_urls
                        )
                        live_api_urls = {p.url for p in api_pages}
                        for p in api_pages:
                            st = db.upsert_page(p)
                            if st == "added":
                                pages_added += 1
                            elif st == "modified":
                                pages_modified += 1
                            else:
                                pages_unchanged += 1
                        # Mark obsolete OpenAPI tag/schema sections removed
                        for old_p in db.get_pages_by_bucket(API_REFERENCE_BUCKET, include_removed=False):
                            if old_p.url not in live_api_urls:
                                db.mark_url_removed(old_p.url)

                    console.print(
                        f"[green]✓[/green] Compiled [bold]{len(api_pages)}[/bold] deterministic "
                        f"OpenAPI reference sections for [cyan]{API_REFERENCE_BUCKET}[/cyan]."
                    )

                # 2. Playwright Headless Browser Extraction with Interactive Tab Expansion
                if scrape_queue:
                    with Progress(
                        SpinnerColumn(),
                        TextColumn("[bold blue]{task.description}"),
                        BarColumn(),
                        MofNCompleteColumn(),
                        TimeElapsedColumn(),
                        console=console,
                    ) as progress:
                        task_id = progress.add_task(
                            f"Scraping pages ({sync_mode})", total=len(scrape_queue)
                        )

                        def _on_scrape(outcome: ScrapeOutcome) -> None:
                            nonlocal pages_added, pages_modified, pages_unchanged, scrape_errors, total_tabs_expanded
                            progress.advance(task_id, 1)
                            if outcome.is_not_found:
                                db.mark_url_removed(outcome.url)
                                return
                            if outcome.error or outcome.page is None:
                                scrape_errors += 1
                                logger.error("Failed scraping %s: %s", outcome.url, outcome.error)
                                return
                            total_tabs_expanded += outcome.expanded_tabs_total
                            st = db.upsert_page(outcome.page)
                            if st == "added":
                                pages_added += 1
                            elif st == "modified":
                                pages_modified += 1
                            else:
                                pages_unchanged += 1

                        await extractor.scrape_batch(scrape_queue, on_progress=_on_scrape)

                    console.print(
                        f"[green]✓[/green] Playwright extracted [bold]{len(scrape_queue) - scrape_errors}[/bold] pages "
                        f"(expanded [bold]{total_tabs_expanded}[/bold] interactive tabs, "
                        f"skipped [bold]{skipped_up_to_date}[/bold] unchanged cached pages)."
                    )

        # 3. GitHub Organization Sync (`09_Dash0_GitHub_Repos_and_Examples`)
        if run_github:
            with Progress(
                SpinnerColumn(),
                TextColumn("[bold magenta]{task.description}"),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                gh_task = progress.add_task("Syncing dash0hq GitHub repositories...", total=None)

                def _on_repo(repo_name: str, file_count: int, state: str) -> None:
                    progress.update(
                        gh_task,
                        description=f"GitHub: {repo_name} ({file_count} files, {state})",
                    )

                gh_stats = await sync_github_repositories(
                    settings,
                    db,
                    force_full=force_full,
                    max_repos=max_repos,
                    on_repo_done=_on_repo,
                )
            console.print(
                f"[green]✓[/green] Checked [bold]{gh_stats['repos_checked']}[/bold] GitHub repos "
                f"(cloned [bold]{gh_stats['repos_cloned']}[/bold], "
                f"+{gh_stats['files_added']} added, ~{gh_stats['files_modified']} modified)."
            )

        # 4. Refresh `00_Dash0_Whats_New_Changelog` with latest run's additions/diffs
        generate_changelog_page(db, sync_mode=sync_mode)

        # 5. Compile Markdown Bundles with Master TOC & Safety Splitting
        compile_targets: set[str] | None = None
        if selected_buckets is not None:
            compile_targets = set(selected_buckets) | {CHANGELOG_BUCKET}

        bundles: list[CompiledBundle] = compile_all_buckets(
            db, settings, target_buckets=compile_targets
        )
        console.print(
            f"[green]✓[/green] Compiled [bold]{len(bundles)}[/bold] Markdown bundles into "
            f"[cyan]{settings.output_dir}[/cyan]."
        )

        # 6. Native Google Docs In-Place Sync
        if not skip_drive:
            try:
                engine = GoogleDriveSyncEngine(settings, db, interactive=False)
            except DriveAuthError as exc:
                console.print(f"[bold red]Google Drive Sync Error:[/bold red] {exc}")
                logger.error("Google Drive auth error: %s", exc)
                return 1

            with Progress(
                SpinnerColumn(),
                TextColumn("[bold green]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                d_task = progress.add_task("Syncing native Google Docs in-place", total=len(bundles))

                def _on_drive_start(bundle: CompiledBundle) -> None:
                    size_kb = max(1, bundle.char_count // 1024)
                    progress.update(
                        d_task,
                        description=f"Syncing Google Docs: {bundle.doc_key} ({size_kb:,} KB)",
                    )

                def _on_drive(act) -> None:
                    progress.advance(d_task, 1)
                    logger.info(
                        "Drive sync %s: %s (%s)", act.action, act.doc_key, act.file_id
                    )

                drive_actions = engine.sync_all(
                    bundles,
                    on_start=_on_drive_start,
                    on_progress=_on_drive,
                )

            created_n = sum(1 for a in drive_actions if a.action == "created")
            updated_n = sum(1 for a in drive_actions if a.action == "updated")
            unchanged_n = sum(1 for a in drive_actions if a.action == "unchanged")
            console.print(
                f"[green]✓[/green] Google Drive in-place sync complete: "
                f"[bold]{created_n}[/bold] created, [bold]{updated_n}[/bold] updated, "
                f"[bold]{unchanged_n}[/bold] unchanged."
            )
        else:
            console.print("[dim]Skipped Google Drive upload (--skip-drive).[/dim]")

        now_iso = utc_now_iso()
        db.set_meta("last_sync_at", now_iso)
        db.set_meta("last_sync_mode", sync_mode)

        # Display run summary table
        summary_table = Table(title="Compiled Knowledge Base Bundles")
        summary_table.add_column("Document Key", style="cyan")
        summary_table.add_column("Pages", justify="right")
        summary_table.add_column("Characters", justify="right")
        summary_table.add_column("Words", justify="right")
        summary_table.add_column("Local File", style="dim")

        for b in bundles:
            summary_table.add_row(
                b.doc_key,
                f"{b.page_count:,}",
                f"{b.char_count:,}",
                f"{b.word_count:,}",
                b.file_path.name,
            )
        console.print(summary_table)

    logger.info(
        "Completed sync run (added=%d, modified=%d, unchanged=%d, errors=%d)",
        pages_added,
        pages_modified,
        pages_unchanged,
        scrape_errors,
    )
    return 0


@app.command("run")
def run_command(
    force_full: Annotated[
        bool,
        typer.Option(
            "--force-full/--incremental",
            help="Force full re-scrape of all URLs & repos (default: smart --incremental).",
        ),
    ] = False,
    bucket: Annotated[
        list[str] | None,
        typer.Option(
            "--bucket",
            "-b",
            help="Sync only specific bucket(s) by name, number (00..09), or alias (repeatable).",
        ),
    ] = None,
    url: Annotated[
        list[str] | None,
        typer.Option(
            "--url",
            "-u",
            help="Explicit Dash0 URL(s) to include in Playwright extraction (repeatable).",
        ),
    ] = None,
    skip_drive: Annotated[
        bool,
        typer.Option(
            "--skip-drive",
            help="Compile Markdown bundles locally without uploading to Google Drive.",
        ),
    ] = False,
    drive_only: Annotated[
        bool,
        typer.Option(
            "--drive-only",
            help="Compile cached SQLite pages and sync to Google Drive without re-polling Dash0 or GitHub.",
        ),
    ] = False,
    max_pages: Annotated[
        int | None,
        typer.Option(
            "--max-pages",
            help="Optional cap on URLs scraped per website bucket (useful for smoke tests).",
        ),
    ] = None,
    max_repos: Annotated[
        int | None,
        typer.Option(
            "--max-repos",
            help="Optional cap on GitHub repositories synced (useful for smoke tests).",
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable debug logging in ./logs/sync.log."),
    ] = False,
) -> None:
    """Run the Dash0 knowledge base discovery, extraction, compilation, and Google Drive sync pipeline."""
    if skip_drive and drive_only:
        console.print("[bold red]Error:[/bold red] Cannot combine --skip-drive and --drive-only.")
        raise typer.Exit(code=2)

    settings = SyncSettings()
    _configure_logging(settings, verbose=verbose)

    resolved_buckets: set[str] | None = None
    if bucket:
        resolved_buckets = set()
        for raw_b in bucket:
            try:
                resolved_buckets.add(resolve_bucket_name(raw_b))
            except ValueError as exc:
                console.print(f"[bold red]Error:[/bold red] {exc}")
                raise typer.Exit(code=2) from None

    exit_code = asyncio.run(
        _run_pipeline_async(
            settings,
            force_full=force_full,
            selected_buckets=resolved_buckets,
            explicit_urls=url,
            skip_drive=skip_drive,
            drive_only=drive_only,
            max_pages_per_bucket=max_pages,
            max_repos=max_repos,
        )
    )
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@app.command("status")
def status_command() -> None:
    """Print status table of all 10 buckets, page/char counts, Google Doc URLs, and last sync timestamps."""
    settings = SyncSettings()
    settings.ensure_directories()

    with StateDB(settings.db_path) as db:
        bucket_counts = db.get_bucket_counts()
        drive_files = db.get_drive_files()
        last_sync_at = db.get_meta("last_sync_at") or "Never"
        last_sync_mode = db.get_meta("last_sync_mode") or "—"
        folder_url = db.get_meta("drive_folder_url") or "Not configured (`run auth`)"

    table = Table(
        title=escape(f"Dash0 Knowledge Base Status (Last Run: {last_sync_at} [{last_sync_mode}])"),
        show_lines=False,
    )
    table.add_column("Bucket", style="bold cyan")
    table.add_column("Pages", justify="right", no_wrap=True)
    table.add_column("Chars", justify="right", no_wrap=True)
    table.add_column("Words", justify="right", no_wrap=True)
    table.add_column("Parts", justify="center", no_wrap=True)
    table.add_column("Google Doc URL(s)", overflow="fold")
    table.add_column("Last Sync", style="dim", no_wrap=True)

    for b in BUCKETS:
        db_stat = bucket_counts.get(b.name, {})
        page_count = int(db_stat.get("page_count", 0) or 0)

        # Inspect local compiled markdown files if present
        md_files = sorted(
            f
            for f in settings.output_dir.glob(f"{b.name}*.md")
            if f.name == f"{b.name}.md" or f.name.startswith(f"{b.name}_Part_")
        )
        if md_files:
            texts = [f.read_text(encoding="utf-8") for f in md_files]
            char_count = sum(len(t) for t in texts)
            word_count = sum(count_words(t) for t in texts)
            parts_str = str(len(md_files))
        else:
            char_count = int(db_stat.get("char_count", 0) or 0)
            word_count = 0
            parts_str = "—"

        # Match Drive records (`{bucket}` or `{bucket}_Part_*`)
        matching_drive = [
            rec
            for key, rec in sorted(drive_files.items())
            if key == b.name or key.startswith(f"{b.name}_Part_")
        ]
        if matching_drive:
            urls_str = "\n".join(rec.web_view_link for rec in matching_drive)
            sync_ts = max(rec.last_synced_at for rec in matching_drive)[:16].replace("T", " ") + "Z"
        else:
            urls_str = "[dim]Not synced to Drive[/dim]"
            raw_upd = str(db_stat.get("last_checked_at") or "—")
            sync_ts = raw_upd[:16].replace("T", " ") + ("Z" if raw_upd != "—" else "")

        table.add_row(
            b.name,
            f"{page_count:,}",
            f"{char_count:,}",
            f"{word_count:,}" if word_count else "—",
            parts_str,
            urls_str,
            sync_ts,
        )

    console.print(table)
    console.print(f"[bold]Google Drive Folder:[/bold] {folder_url}")
    console.print(f"[bold]SQLite State DB:[/bold]     {settings.db_path}")
    console.print(f"[bold]Markdown Output:[/bold]     {settings.output_dir}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
