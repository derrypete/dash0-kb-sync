"""GitHub organization discovery and shallow-clone / API documentation extractor for dash0hq."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from dash0_kb_sync.config import (
    GITHUB_BUCKET,
    PRIORITY_GITHUB_REPOS,
    ExtractedPage,
    SyncSettings,
    build_extracted_page,
    utc_now_display,
    utc_now_iso,
)
from dash0_kb_sync.db import StateDB
from dash0_kb_sync.discovery import create_http_client, fetch_with_retry

logger = logging.getLogger(__name__)

_EXCLUDED_REPO_NAMES: set[str] = {
    ".github",
    "sales-enablement",
    "take-home-assignments",
    "claude-marketplace",
}

_EXCLUDED_DIR_PARTS: set[str] = {
    ".git",
    ".github",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "target",
    "__pycache__",
    "testdata",
    "test-resources",
    "tests",
    "test",
    "e2e",
    "fixtures",
    "snapshots",
}

_EXAMPLE_REPO_KEYWORDS = ("example", "demo", "tutorial", "course", "skills", "sampling")

_CONFIG_EXT_TO_LANG: dict[str, str] = {
    ".yaml": "yaml",
    ".yml": "yaml",
    ".tf": "hcl",
    ".tfvars": "hcl",
    ".hcl": "hcl",
    ".json": "json",
    ".toml": "toml",
    ".sh": "bash",
    ".bash": "bash",
    ".promql": "promql",
    ".sql": "sql",
    ".dockerfile": "dockerfile",
}

MAX_FILE_BYTES = 200_000


@dataclass
class GitHubRepoInfo:
    """Metadata for an active public Dash0 GitHub repository."""

    name: str
    default_branch: str
    description: str
    html_url: str
    clone_url: str
    pushed_at: str | None = None
    stars: int = 0


def _repo_sort_key(repo: GitHubRepoInfo) -> tuple[int, int, str]:
    if repo.name in PRIORITY_GITHUB_REPOS:
        return (0, PRIORITY_GITHUB_REPOS.index(repo.name), repo.name)
    return (1, -repo.stars, repo.name.lower())


def extract_latest_changelog_releases(changelog_md: str, max_releases: int = 5) -> str:
    """Keep the preamble and latest `max_releases` release sections (`## ...`) of a CHANGELOG.md."""
    lines = changelog_md.replace("\r\n", "\n").splitlines()
    h2_indices = [idx for idx, ln in enumerate(lines) if re.match(r"^##\s+\S", ln)]
    if len(h2_indices) <= max_releases:
        return changelog_md.strip()

    cutoff_line = h2_indices[max_releases]
    kept = "\n".join(lines[:cutoff_line]).rstrip()
    omitted = len(h2_indices) - max_releases
    return f"{kept}\n\n*({omitted} older release sections omitted for brevity — see full CHANGELOG on GitHub.)*"


async def discover_github_repos(
    settings: SyncSettings,
    db: StateDB,
) -> list[GitHubRepoInfo]:
    """Query GitHub REST API for public, non-archived, non-fork `dash0hq` repositories.

    Falls back to cached SQLite repos + priority repos if GitHub API rate limits apply.
    """
    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"

    repos: list[GitHubRepoInfo] = []
    api_failed = False

    async with create_http_client(timeout_s=20.0) as client:
        page = 1
        while page <= 5:
            url = (
                f"https://api.github.com/orgs/{settings.github_org}/repos"
                f"?per_page=100&type=public&page={page}"
            )
            try:
                resp = await fetch_with_retry(client, url, max_attempts=2, extra_headers=headers)
                if resp.status_code in (403, 429):
                    logger.warning(
                        "GitHub REST API rate-limited (HTTP %d); falling back to git discovery.",
                        resp.status_code,
                    )
                    api_failed = True
                    break
                if resp.status_code >= 400:
                    logger.warning("GitHub API returned HTTP %d on %s", resp.status_code, url)
                    api_failed = True
                    break

                payload = resp.json()
                if not isinstance(payload, list) or not payload:
                    break

                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "")
                    if not name or name in _EXCLUDED_REPO_NAMES:
                        continue
                    if item.get("archived") or item.get("disabled"):
                        continue
                    if item.get("fork") and name not in PRIORITY_GITHUB_REPOS:
                        continue

                    repos.append(
                        GitHubRepoInfo(
                            name=name,
                            default_branch=str(item.get("default_branch") or "main"),
                            description=str(item.get("description") or "").strip(),
                            html_url=str(
                                item.get("html_url")
                                or f"https://github.com/{settings.github_org}/{name}"
                            ),
                            clone_url=str(
                                item.get("clone_url")
                                or f"https://github.com/{settings.github_org}/{name}.git"
                            ),
                            pushed_at=item.get("pushed_at"),
                            stars=int(item.get("stargazers_count") or 0),
                        )
                    )

                if len(payload) < 100:
                    break
                page += 1
            except Exception as exc:
                logger.warning("GitHub API error on %s: %s", url, exc)
                api_failed = True
                break

    if api_failed and not repos:
        known = {r["repo_name"]: r for r in db.get_known_github_repos()}
        seed_names = list(dict.fromkeys(list(PRIORITY_GITHUB_REPOS) + list(known.keys())))
        for name in seed_names:
            meta = known.get(name, {})
            branch = meta.get("default_branch", "main")
            repos.append(
                GitHubRepoInfo(
                    name=name,
                    default_branch=branch,
                    description=meta.get("description", ""),
                    html_url=f"https://github.com/{settings.github_org}/{name}",
                    clone_url=f"https://github.com/{settings.github_org}/{name}.git",
                )
            )

    repos.sort(key=_repo_sort_key)
    return repos


async def get_remote_head_sha(clone_url: str, default_branch: str) -> tuple[str | None, str]:
    """Use `git ls-remote` to inspect a repo's latest commit SHA without burning REST API quota."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "ls-remote",
        clone_url,
        f"refs/heads/{default_branch}",
        "HEAD",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20.0)
    except TimeoutError:
        proc.kill()
        return None, default_branch

    if proc.returncode != 0 or not stdout:
        return None, default_branch

    text = stdout.decode("utf-8", errors="ignore").strip()
    branch_sha: str | None = None
    head_sha: str | None = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            sha, ref = parts[0], parts[1]
            if ref == f"refs/heads/{default_branch}":
                branch_sha = sha
            elif ref == "HEAD":
                head_sha = sha
    return (branch_sha or head_sha), default_branch


def _should_include_repo_file(rel_path: Path, repo_name: str) -> bool:
    """Determine if a repository file matches the technical documentation & config criteria."""
    parts = rel_path.parts
    lower_parts = [p.lower() for p in parts]

    # Skip excluded directories anywhere in path
    for part in lower_parts[:-1]:
        if part in _EXCLUDED_DIR_PARTS or part.startswith("."):
            return False

    filename = rel_path.name
    lower_name = filename.lower()
    if lower_name.startswith("."):
        return False

    # 1. Root README.md or CHANGELOG.md
    if len(parts) == 1 and lower_name in ("readme.md", "changelog.md"):
        return True

    # 2. Helm / Chart values.yaml and chart README.md
    if any(p in ("helm", "helm-chart", "chart", "charts") for p in lower_parts[:-1]):
        if lower_name in ("values.yaml", "values.yml", "readme.md"):
            return True

    # 3. Any `docs/**/*.md` or `**/docs/**/*.md`
    if "docs" in lower_parts[:-1] and lower_name.endswith((".md", ".mdx")):
        return True

    # 4. `examples/**/*` (Markdown guides and `.yaml`, `.tf`, `.hcl`, `.otel.yaml`, `.json`, `.sh` configs)
    if "examples" in lower_parts[:-1] or "example" in lower_parts[:-1]:
        if lower_name.endswith((".md", ".mdx", ".otel.yaml", ".tfvars.example")):
            return True
        if rel_path.suffix.lower() in _CONFIG_EXT_TO_LANG:
            return True

    # 5. Agent skills or dedicated example/tutorial repos: capture `skills/**/*.md` and root/sub guides/configs
    is_example_repo = any(k in repo_name.lower() for k in _EXAMPLE_REPO_KEYWORDS)
    if "skills" in lower_parts[:-1] and lower_name.endswith(".md"):
        return True
    if is_example_repo:
        if lower_name.endswith((".md", ".mdx", ".otel.yaml")):
            return True
        if len(parts) <= 3 and rel_path.suffix.lower() in (".yaml", ".yml", ".tf", ".hcl"):
            if lower_name not in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock"):
                return True

    return False


def _format_repo_file_markdown(
    *,
    repo: GitHubRepoInfo,
    commit_sha: str,
    rel_posix: str,
    raw_text: str,
) -> tuple[str, str, str]:
    """Return `(title, blob_url, formatted_markdown_body)` for a single repository file."""
    blob_url = f"https://github.com/dash0hq/{repo.name}/blob/{repo.default_branch}/{rel_posix}"
    title = f"GitHub: dash0hq/{repo.name} — {rel_posix}"
    short_sha = commit_sha[:10] if commit_sha else "HEAD"

    header_lines = [
        f"# `{repo.name}/{rel_posix}`",
        "",
        f"- **Repository:** [dash0hq/{repo.name}]({repo.html_url})",
    ]
    if repo.description:
        header_lines.append(f"- **Repository Description:** {repo.description}")
    header_lines.extend(
        [
            f"- **GitHub File URL:** [{rel_posix}]({blob_url})",
            f"- **Branch / Commit:** `{repo.default_branch}` (`{short_sha}`)",
            "",
            "---",
            "",
        ]
    )

    lower_name = rel_posix.lower()
    if lower_name.endswith("changelog.md"):
        content_body = extract_latest_changelog_releases(raw_text, max_releases=5)
    elif lower_name.endswith((".md", ".mdx")):
        content_body = raw_text.strip()
    else:
        ext = Path(rel_posix).suffix.lower()
        if lower_name.endswith(".tfvars.example"):
            lang = "hcl"
        elif lower_name.endswith(".otel.yaml"):
            lang = "yaml"
        else:
            lang = _CONFIG_EXT_TO_LANG.get(ext, "text")
        fence = "````" if "```" in raw_text else "```"
        content_body = f"{fence}{lang}\n{raw_text.strip()}\n{fence}"

    return title, blob_url, "\n".join(header_lines) + content_body


def _extract_files_from_cloned_repo(
    clone_dir: Path,
    repo: GitHubRepoInfo,
    commit_sha: str,
) -> list[ExtractedPage]:
    """Walk a shallow-cloned repository and extract structured Markdown pages."""
    now_iso = utc_now_iso()
    now_disp = utc_now_display()
    extracted: list[ExtractedPage] = []

    candidates: list[Path] = []
    for file_path in clone_dir.rglob("*"):
        if not file_path.is_file() or file_path.is_symlink():
            continue
        rel = file_path.relative_to(clone_dir)
        if not _should_include_repo_file(rel, repo.name):
            continue
        try:
            size = file_path.stat().st_size
        except OSError:
            continue
        if size == 0 or size > MAX_FILE_BYTES:
            continue
        candidates.append(rel)

    # Sort deterministically: README.md first, CHANGELOG.md second, values.yaml third, then alphabetical
    def _file_priority(rel: Path) -> tuple[int, str]:
        p = rel.as_posix().lower()
        if p == "readme.md":
            return (0, p)
        if p == "changelog.md":
            return (1, p)
        if p.endswith("values.yaml") or p.endswith("values.yml"):
            return (2, p)
        if "/docs/" in f"/{p}":
            return (3, p)
        return (4, p)

    candidates.sort(key=_file_priority)

    for rel in candidates:
        full_path = clone_dir / rel
        try:
            raw_bytes = full_path.read_bytes()
            if b"\x00" in raw_bytes[:4096]:
                continue  # Skip binary files
            raw_text = raw_bytes.decode("utf-8", errors="replace")
        except Exception as exc:
            logger.debug("Skipping unreadable file %s/%s: %s", repo.name, rel, exc)
            continue

        rel_posix = rel.as_posix()
        title, blob_url, body_md = _format_repo_file_markdown(
            repo=repo,
            commit_sha=commit_sha,
            rel_posix=rel_posix,
            raw_text=raw_text,
        )
        extracted.append(
            build_extracted_page(
                url=blob_url,
                bucket=GITHUB_BUCKET,
                title=title,
                lastmod=repo.pushed_at,
                body_markdown=body_md,
                verified_iso=now_iso,
                verified_display=now_disp,
            )
        )

    return extracted


async def _clone_and_extract_repo(
    repo: GitHubRepoInfo,
    expected_sha: str | None,
) -> tuple[str | None, list[ExtractedPage]]:
    """Shallow-clone `repo` into a temporary directory and extract documentation & config files."""
    tmp_root = Path(tempfile.mkdtemp(prefix=f"dash0_gh_{repo.name}_"))
    try:
        cmd = [
            "git",
            "clone",
            "--depth",
            "1",
            "--single-branch",
            "--branch",
            repo.default_branch,
            repo.clone_url,
            str(tmp_root / "repo"),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        except TimeoutError:
            proc.kill()
            logger.warning("Timed out cloning %s", repo.clone_url)
            return None, []

        clone_dir = tmp_root / "repo"
        if proc.returncode != 0 or not clone_dir.exists():
            # Retry without --branch in case default_branch changed
            proc2 = await asyncio.create_subprocess_exec(
                "git",
                "clone",
                "--depth",
                "1",
                repo.clone_url,
                str(clone_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc2.communicate(), timeout=60.0)
            if proc2.returncode != 0 or not clone_dir.exists():
                logger.warning(
                    "git clone failed for %s: %s",
                    repo.name,
                    stderr.decode("utf-8", errors="ignore").strip(),
                )
                return None, []

        sha = expected_sha
        if not sha:
            rev_proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(clone_dir),
                "rev-parse",
                "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await rev_proc.communicate()
            sha = out.decode("utf-8", errors="ignore").strip() or "HEAD"

        pages = await asyncio.to_thread(_extract_files_from_cloned_repo, clone_dir, repo, sha)
        return sha, pages
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


async def sync_github_repositories(
    settings: SyncSettings,
    db: StateDB,
    *,
    force_full: bool = False,
    max_repos: int | None = None,
    on_repo_done: Callable[[str, int, str], None] | None = None,
) -> dict[str, int]:
    """Discover and incrementally sync Dash0 GitHub repositories into SQLite."""
    repos = await discover_github_repos(settings, db)
    if max_repos is not None:
        repos = repos[:max_repos]

    existing_gh_pages = db.get_pages_by_bucket(GITHUB_BUCKET, include_removed=False)
    existing_urls_by_repo: dict[str, set[str]] = {}
    for p in existing_gh_pages:
        # URL pattern: https://github.com/dash0hq/{repo}/blob/...
        m = re.match(r"^https://github\.com/[^/]+/([^/]+)/blob/", p.url)
        if m:
            existing_urls_by_repo.setdefault(m.group(1), set()).add(p.url)

    sem = asyncio.Semaphore(settings.github_concurrency)
    stats = {"repos_checked": 0, "repos_cloned": 0, "files_added": 0, "files_modified": 0, "files_unchanged": 0}

    async def _process_one(repo: GitHubRepoInfo) -> None:
        async with sem:
            remote_sha, _ = await get_remote_head_sha(repo.clone_url, repo.default_branch)
            stored_sha = db.get_github_repo_sha(repo.name)
            has_cached_files = bool(existing_urls_by_repo.get(repo.name))

            stats["repos_checked"] += 1
            if (
                not force_full
                and remote_sha
                and stored_sha == remote_sha
                and has_cached_files
            ):
                db.upsert_github_repo(
                    repo_name=repo.name,
                    default_branch=repo.default_branch,
                    commit_sha=remote_sha,
                    description=repo.description,
                    changed=False,
                )
                cached_count = len(existing_urls_by_repo.get(repo.name, set()))
                if on_repo_done:
                    on_repo_done(repo.name, cached_count, "cached")
                return

            sha, pages = await _clone_and_extract_repo(repo, remote_sha)
            if sha is None:
                if on_repo_done:
                    on_repo_done(repo.name, 0, "error")
                return

            stats["repos_cloned"] += 1
            new_urls = {p.url for p in pages}
            for page in pages:
                state = db.upsert_page(page)
                if state == "added":
                    stats["files_added"] += 1
                elif state == "modified":
                    stats["files_modified"] += 1
                else:
                    stats["files_unchanged"] += 1

            # Mark deleted files in this repo as removed
            for old_url in existing_urls_by_repo.get(repo.name, set()) - new_urls:
                db.mark_url_removed(old_url)

            db.upsert_github_repo(
                repo_name=repo.name,
                default_branch=repo.default_branch,
                commit_sha=sha,
                description=repo.description,
                changed=True,
            )
            if on_repo_done:
                on_repo_done(repo.name, len(pages), "synced")

    await asyncio.gather(*(_process_one(r) for r in repos))
    return stats
