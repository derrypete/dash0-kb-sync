"""Unit and browser integration tests for `dash0-kb-sync`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from playwright.async_api import async_playwright

from dash0_kb_sync.changelog import generate_changelog_page
from dash0_kb_sync.compiler import CompiledBundle, compile_bucket
from dash0_kb_sync.config import SyncSettings, build_extracted_page, convert_images_to_links
from dash0_kb_sync.db import StateDB
from dash0_kb_sync.discovery import canonicalize_dash0_url, match_bucket_for_url
from dash0_kb_sync.extractor import EXPAND_INTERACTIVE_JS, clean_html_to_markdown
from dash0_kb_sync.gdrive_sync import GOOGLE_DOC_MIME, GoogleDriveSyncEngine
from dash0_kb_sync.github_fetcher import extract_latest_changelog_releases
from dash0_kb_sync.openapi_parser import compile_openapi_spec_to_pages


def test_url_canonicalization_and_bucket_routing() -> None:
    assert (
        canonicalize_dash0_url("/hub/integrations/int_k8s/overview/?tag=AI#setup")
        == "https://www.dash0.com/hub/integrations/int_k8s/overview"
    )
    assert canonicalize_dash0_url("https://dash0.com/docs/otelbin/") == "https://www.dash0.com/docs/otelbin"
    assert canonicalize_dash0_url("/_next/image?url=/foo.png") is None

    assert match_bucket_for_url("https://www.dash0.com/docs/dash0/get-started") == "01_Dash0_Core_Docs"
    assert match_bucket_for_url("https://www.dash0.com/docs/otelbin/validating") == "02_Dash0_OTelBin_Docs"
    assert match_bucket_for_url("https://www.dash0.com/docs/api-reference") == "03_Dash0_API_Reference"
    assert match_bucket_for_url("https://www.dash0.com/guides/eks") == "04_Dash0_Guides"
    assert match_bucket_for_url("https://www.dash0.com/knowledge/otel") == "05_Dash0_Knowledge_Base"
    assert match_bucket_for_url("https://www.dash0.com/comparisons/datadog") == "06_Dash0_Comparisons"
    assert match_bucket_for_url("https://www.dash0.com/blog/profiling-ga") == "07_Dash0_Blog"
    assert match_bucket_for_url("https://www.dash0.com/hub/integrations/int_k8s/overview") == "08_Dash0_Integrations_Hub"


@pytest.mark.asyncio
async def test_playwright_interactive_tab_and_accordion_expansion() -> None:
    """Verify ARIA tabs, custom React button tabs, and <details> all expand into Markdown."""
    sample_html = """
    <!DOCTYPE html>
    <html>
    <head><title>Kubernetes Collector Setup · Dash0</title></head>
    <body>
      <header><nav>Top Nav Noise</nav></header>
      <main>
        <article>
          <h1>Kubernetes Collector Setup</h1>
          <p>Deploy using your preferred installation method:</p>

          <!-- Standard ARIA/Radix Tab Component -->
          <div class="radix-tabs">
            <div role="tablist">
              <button role="tab" aria-selected="true" onclick="showTab('helm')">Helm</button>
              <button role="tab" aria-selected="false" onclick="showTab('terraform')">Terraform</button>
              <button role="tab" aria-selected="false" onclick="showTab('kubectl')">Kubectl Manifest</button>
            </div>
            <div role="tabpanel" id="tab-panel">
              <div id="code-snippet">
                <div><span>bash</span><button>Copy</button></div>
                <pre><code>helm install dash0 dash0/dash0-operator</code></pre>
              </div>
            </div>
          </div>

          <details>
            <summary>Troubleshooting CRD Permissions</summary>
            <p>Ensure cluster-admin RBAC is granted before applying CRDs.</p>
          </details>

          <div class="callout-wrapper">
            <div class="border-amber-500">Always pin chart versions in production.</div>
          </div>
          <p>See <a href="/docs/dash0/get-started">Getting Started</a>.</p>
        </article>
      </main>
      <script>
        const snippets = {
          helm: '<div id="code-snippet"><div><span>bash</span></div><pre><code>helm install dash0 dash0/dash0-operator</code></pre></div>',
          terraform: '<div id="code-snippet"><div><span>hcl</span></div><pre><code>resource "dash0_check_rule" "cpu" { name = "High CPU" }</code></pre></div>',
          kubectl: '<div id="code-snippet"><div><span>yaml</span></div><pre><code>apiVersion: operator.dash0.com/v1alpha1\\nkind: Dash0Monitoring</code></pre></div>'
        };
        function showTab(key) {
          document.getElementById('tab-panel').innerHTML = snippets[key];
        }
      </script>
    </body>
    </html>
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(sample_html)
        stats = await page.evaluate(EXPAND_INTERACTIVE_JS)
        rendered_html = await page.content()
        await browser.close()

    assert stats["expandedTabGroups"] == 1
    assert stats["expandedTabsTotal"] == 3
    assert stats["expandedAccordions"] == 1

    title, md = clean_html_to_markdown(rendered_html, "https://www.dash0.com/docs/dash0/k8s")
    assert title == "Kubernetes Collector Setup"
    assert "Top Nav Noise" not in md
    assert "#### [Tab: Helm]" in md
    assert "#### [Tab: Terraform]" in md
    assert "#### [Tab: Kubectl Manifest]" in md
    assert "```bash\nhelm install dash0 dash0/dash0-operator\n```" in md
    assert '```hcl\nresource "dash0_check_rule" "cpu"' in md
    assert "```yaml\napiVersion: operator.dash0.com/v1alpha1" in md
    assert "#### [Details: Troubleshooting CRD Permissions]" in md
    assert "**Warning:**" in md
    assert "(https://www.dash0.com/docs/dash0/get-started)" in md


def test_openapi_compiler_deterministic_output() -> None:
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "Dash0 API", "version": "1.0.0", "description": "Dataset rules."},
        "servers": [{"url": "https://api.us-east-1.aws.dash0.com", "description": "US East"}],
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer", "description": "Bearer token"}
            },
            "schemas": {
                "CheckRule": {
                    "type": "object",
                    "required": ["name", "expression"],
                    "properties": {
                        "name": {"type": "string", "description": "Alert rule name"},
                        "expression": {"type": "string", "description": "PromQL expression"},
                        "threshold": {"type": ["number", "null"], "default": 0.9},
                    },
                }
            },
        },
        "tags": [{"name": "Manage Alerting", "description": "Check rule endpoints"}],
        "paths": {
            "/api/alerting/check-rules/{originOrId}": {
                "put": {
                    "tags": ["Manage Alerting"],
                    "summary": "Upsert check rule",
                    "operationId": "putCheckRule",
                    "parameters": [
                        {
                            "name": "originOrId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                            "description": "Origin label or UUID",
                        }
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/CheckRule"}
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Updated check rule",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/CheckRule"}
                                }
                            },
                        }
                    },
                }
            }
        },
    }

    pages = compile_openapi_spec_to_pages(spec)
    assert len(pages) == 3  # Overview + 1 Tag + 1 Schema group (A-C)
    joined = "\n\n".join(p.content_markdown for p in pages)
    assert "https://api.us-east-1.aws.dash0.com" in joined
    assert "`PUT /api/alerting/check-rules/{originOrId}`" in joined
    assert "`originOrId`" in joined
    assert "## Schema: `CheckRule`" in joined
    assert "PromQL expression" in joined
    assert r"number \| null" in joined


def test_sqlite_incremental_state_and_changelog_diffs(tmp_path: Path) -> None:
    db_path = tmp_path / "dash0_state.db"
    with StateDB(db_path) as db:
        p1 = build_extracted_page(
            url="https://www.dash0.com/docs/dash0/get-started",
            bucket="01_Dash0_Core_Docs",
            title="Get Started",
            lastmod="2026-11-01T00:00:00Z",
            body_markdown="# Get Started\n\nSend OpenTelemetry traces to Dash0 in two minutes.",
            verified_iso="2026-11-10T10:00:00Z",
            verified_display="2026-11-10 10:00 UTC",
        )
        assert db.upsert_page(p1) == "added"

        # Re-verifying identical content with a newer Last Verified timestamp must remain 'unchanged'
        p1_reverified = build_extracted_page(
            url="https://www.dash0.com/docs/dash0/get-started",
            bucket="01_Dash0_Core_Docs",
            title="Get Started",
            lastmod="2026-11-01T00:00:00Z",
            body_markdown="# Get Started\n\nSend OpenTelemetry traces to Dash0 in two minutes.",
            verified_iso="2026-11-11T10:00:00Z",
            verified_display="2026-11-11 10:00 UTC",
        )
        assert db.upsert_page(p1_reverified) == "unchanged"

        # Staleness check (> 3 days triggers re-scrape for /docs/dash0)
        future_now = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
        should, reason = db.should_rescrape_url(
            url=p1.url,
            sitemap_lastmod="2026-11-01T00:00:00Z",
            reverify_after_days=3,
            force_full=False,
            now_dt=future_now,
        )
        assert should is True
        assert reason == "stale>3d"

        # Modify content and verify changelog diff summary
        p1_mod = build_extracted_page(
            url="https://www.dash0.com/docs/dash0/get-started",
            bucket="01_Dash0_Core_Docs",
            title="Get Started",
            lastmod="2026-11-11T12:00:00Z",
            body_markdown=(
                "# Get Started\n\nSend OpenTelemetry traces to Dash0 in two minutes.\n\n"
                "## Continuous Profiling\n\nEnable eBPF profiling across all Kubernetes nodes."
            ),
        )
        assert db.upsert_page(p1_mod) == "modified"

        changelog = generate_changelog_page(db, sync_mode="incremental")
        assert "Continuous Profiling" in changelog.content_markdown
        assert "Modified Pages & Repo Files (1)" in changelog.content_markdown


def test_compiler_auto_splitting_and_toc(tmp_path: Path) -> None:
    settings = SyncSettings(
        project_root=tmp_path,
        output_dir=tmp_path / "output" / "markdown",
        max_chars_per_doc=60_000,
        max_words_per_doc=15_000,
    )
    db_path = tmp_path / "state.db"
    with StateDB(db_path) as db:
        for i in range(6):
            body = f"# Integration {i}\n\n" + ("Detailed OpenTelemetry configuration guide. " * 600)
            p = build_extracted_page(
                url=f"https://www.dash0.com/hub/integrations/int_{i}/overview",
                bucket="08_Dash0_Integrations_Hub",
                title=f"Integration {i}",
                lastmod=None,
                body_markdown=body,
            )
            db.upsert_page(p)
        records = db.get_pages_by_bucket("08_Dash0_Integrations_Hub")

    bundles = compile_bucket(
        "08_Dash0_Integrations_Hub", records, settings, generated_display="2026-11-11 10:00 UTC"
    )
    bundles_later = compile_bucket(
        "08_Dash0_Integrations_Hub", records, settings, generated_display="2026-11-12 03:00 UTC"
    )
    assert len(bundles) >= 2
    assert [b.sha256 for b in bundles] == [b.sha256 for b in bundles_later]
    assert bundles[0].doc_key == "08_Dash0_Integrations_Hub_Part_1"
    assert bundles[1].doc_key == "08_Dash0_Integrations_Hub_Part_2"
    for b in bundles:
        assert b.char_count <= settings.max_chars_per_doc
        text = b.file_path.read_text(encoding="utf-8")
        assert "## Master Table of Contents" in text


def test_gdrive_in_place_update_preserves_file_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify `files().create()` is used once and subsequent syncs call `files().update(fileId=...)`."""
    settings = SyncSettings(
        project_root=tmp_path,
        db_path=tmp_path / "data" / "dash0_state.db",
        drive_manifest_path=tmp_path / "data" / "drive_manifest.json",
        output_dir=tmp_path / "output" / "markdown",
    )
    settings.ensure_directories()

    mock_files_api = MagicMock()
    # Folder search -> empty -> create folder 'folder_123'
    mock_files_api.list.return_value.execute.side_effect = [
        {"files": []},  # folder lookup
        {"files": []},  # initial doc lookup in folder
    ]
    mock_files_api.create.return_value.execute.side_effect = [
        {"id": "folder_123", "name": "Dash0_Knowledge_Base", "webViewLink": "https://drive/folder_123"},
        {"id": "doc_fixed_id_999", "name": "02_Dash0_OTelBin_Docs", "webViewLink": "https://docs/doc_fixed_id_999"},
    ]
    mock_files_api.update.return_value.execute.return_value = {
        "id": "doc_fixed_id_999",
        "name": "02_Dash0_OTelBin_Docs",
        "webViewLink": "https://docs/doc_fixed_id_999",
    }

    mock_service = MagicMock()
    mock_service.files.return_value = mock_files_api

    monkeypatch.setattr(
        "dash0_kb_sync.gdrive_sync.load_or_authenticate_credentials",
        lambda *a, **kw: MagicMock(),
    )
    monkeypatch.setattr(
        "dash0_kb_sync.gdrive_sync.build",
        lambda *a, **kw: mock_service,
    )

    md_file = settings.output_dir / "02_Dash0_OTelBin_Docs.md"
    md_file.write_text("# OTelBin v1\n", encoding="utf-8")

    b1 = CompiledBundle(
        doc_key="02_Dash0_OTelBin_Docs",
        bucket="02_Dash0_OTelBin_Docs",
        part_index=1,
        total_parts=1,
        file_path=md_file,
        page_count=1,
        char_count=14,
        word_count=2,
        sha256="hash_v1",
    )

    with StateDB(settings.db_path) as db:
        engine = GoogleDriveSyncEngine(settings, db, interactive=False)
        folder_id, _ = engine.ensure_drive_folder()
        assert folder_id == "folder_123"

        act1 = engine.sync_bundle(b1, folder_id)
        assert act1.action == "created"
        assert act1.file_id == "doc_fixed_id_999"

        # Same hash -> unchanged, zero API calls
        act2 = engine.sync_bundle(b1, folder_id)
        assert act2.action == "unchanged"
        assert act2.file_id == "doc_fixed_id_999"
        assert mock_files_api.update.call_count == 0

        # Updated hash -> in-place `files().update(fileId='doc_fixed_id_999')`, NEVER delete!
        md_file.write_text("# OTelBin v2 updated\n", encoding="utf-8")
        b2 = CompiledBundle(
            doc_key="02_Dash0_OTelBin_Docs",
            bucket="02_Dash0_OTelBin_Docs",
            part_index=1,
            total_parts=1,
            file_path=md_file,
            page_count=1,
            char_count=21,
            word_count=3,
            sha256="hash_v2",
        )
        act3 = engine.sync_bundle(b2, folder_id)
        assert act3.action == "updated"
        assert act3.file_id == "doc_fixed_id_999"
        assert mock_files_api.update.call_count == 1
        _, update_kwargs = mock_files_api.update.call_args
        assert update_kwargs["fileId"] == "doc_fixed_id_999"
        assert mock_files_api.delete.call_count == 0


def test_changelog_truncates_to_5_releases() -> None:
    md = "# Changelog\n\n" + "\n\n".join(f"## v1.{i}.0\n- Feature {i}" for i in range(8, 0, -1))
    trimmed = extract_latest_changelog_releases(md, max_releases=5)
    assert "## v1.8.0" in trimmed
    assert "## v1.4.0" in trimmed
    assert "## v1.3.0" not in trimmed
    assert "3 older release sections omitted" in trimmed


def test_prism_token_line_and_line_numbers_code_block_preservation() -> None:
    """Verify Prism/Shiki multi-line div code blocks retain exact indentation and strip line numbers."""
    html = """
    <main>
      <h1>Kubernetes Setup</h1>
      <div id="code-snippet">
        <div><span>yaml</span><button>Copy</button></div>
        <pre class="prism-code language-yaml">
          <div id="line-numbers" class="select-none"><span>1</span><span>2</span><span>3</span></div>
          <div>
            <div><div class="token-linepl-1"><span>receivers:</span></div></div>
            <div><div class="token-linepl-1"><span>  otlp:</span></div></div>
            <div><div class="token-linepl-1"><span>    protocols: {}</span></div></div>
          </div>
        </pre>
      </div>
    </main>
    """
    _, md = clean_html_to_markdown(html, "https://www.dash0.com/hub/integrations/int_k8s/overview")
    expected = "```yaml\nreceivers:\n  otlp:\n    protocols: {}\n```"
    assert expected in md
    assert "123" not in md


def test_gdrive_timeout_recovery_and_duplicate_prevention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify socket timeouts during large Markdown conversions retry cleanly and adopt server-created docs."""
    settings = SyncSettings(
        project_root=tmp_path,
        db_path=tmp_path / "data" / "dash0_state.db",
        drive_manifest_path=tmp_path / "data" / "drive_manifest.json",
        output_dir=tmp_path / "output" / "markdown",
    )
    settings.ensure_directories()

    mock_files_api = MagicMock()
    # 1st list: initial doc lookup in folder -> empty
    # 2nd list (after create TimeoutError): Drive finished creating 'doc_adopted_777' server-side!
    mock_files_api.list.return_value.execute.side_effect = [
        {"files": []},
        {
            "files": [
                {
                    "id": "doc_adopted_777",
                    "name": "01_Dash0_Core_Docs_Part_1",
                    "webViewLink": "https://docs/doc_adopted_777",
                }
            ]
        },
    ]
    # First create() times out waiting on Google's Markdown->Docs converter
    mock_files_api.create.return_value.execute.side_effect = TimeoutError("The read operation timed out")
    # Adopted update() also hits one transient TimeoutError, then succeeds on retry!
    mock_files_api.update.return_value.execute.side_effect = [
        TimeoutError("Transient socket timeout"),
        {
            "id": "doc_adopted_777",
            "name": "01_Dash0_Core_Docs_Part_1",
            "webViewLink": "https://docs/doc_adopted_777",
        },
    ]

    mock_service = MagicMock()
    mock_service.files.return_value = mock_files_api

    monkeypatch.setattr(
        "dash0_kb_sync.gdrive_sync.load_or_authenticate_credentials",
        lambda *a, **kw: MagicMock(),
    )
    monkeypatch.setattr(
        "dash0_kb_sync.gdrive_sync.build",
        lambda *a, **kw: mock_service,
    )
    monkeypatch.setattr("dash0_kb_sync.gdrive_sync.time.sleep", lambda *_a: None)

    md_file = settings.output_dir / "01_Dash0_Core_Docs_Part_1.md"
    md_file.write_text("# Core Docs Part 1\n", encoding="utf-8")
    bundle = CompiledBundle(
        doc_key="01_Dash0_Core_Docs_Part_1",
        bucket="01_Dash0_Core_Docs",
        part_index=1,
        total_parts=4,
        file_path=md_file,
        page_count=45,
        char_count=727_000,
        word_count=92_000,
        sha256="hash_core_p1",
    )

    with StateDB(settings.db_path) as db:
        engine = GoogleDriveSyncEngine(settings, db, interactive=False)
        act = engine.sync_bundle(bundle, "folder_123")

    assert act.action == "created"
    assert act.file_id == "doc_adopted_777"
    # create() was attempted only once, then adopted via update() rather than creating a duplicate!
    assert mock_files_api.create.call_count == 1
    assert mock_files_api.update.call_count == 2


def test_images_converted_to_lightweight_links_preserving_code_blocks() -> None:
    """Verify HTML and Markdown images convert to `[Image: ...]` links without touching code blocks."""
    html = """
    <main>
      <h1>Docker Monitoring</h1>
      <img src="/_content/comparisons/chart.webp" alt="Container CPU Dashboard" />
      <a href="https://github.com/dash0hq/otelbin"><img src="https://img.shields.io/badge/ci-pass.svg" alt="CI Status" /></a>
    </main>
    """
    _, md = clean_html_to_markdown(html, "https://www.dash0.com/comparisons/docker")
    assert "![" not in md
    assert "[Image: Container CPU Dashboard](https://www.dash0.com/_content/comparisons/chart.webp)" in md
    assert "[Image: CI Status](https://github.com/dash0hq/otelbin)" in md

    raw_gh_md = (
        "[![Build](https://img.shields.io/badge.svg)](https://github.com/dash0hq/dash0-operator/actions)\n\n"
        "[![Unit Tests][ci-img]][ci]\n"
        "![GitHub License][license-img]\n\n"
        "  ```bash\n"
        "  echo hello\n"
        "  ```\n\n"
        "![](https://www.dash0.com/diagram.png)\n\n"
        '<img src="https://www.dash0.com/arch.png" alt="Architecture" width="600">\n\n'
        "```markdown\n![keep-inside-code-fence](https://example.com/demo.png)\n```\n"
    )
    cleaned = convert_images_to_links(raw_gh_md)
    assert "[Image: Build](https://github.com/dash0hq/dash0-operator/actions)" in cleaned
    assert "[Image: Unit Tests][ci]" in cleaned
    assert "[Image: GitHub License][license-img]" in cleaned
    assert "[Image](https://www.dash0.com/diagram.png)" in cleaned
    assert "[Image: Architecture](https://www.dash0.com/arch.png)" in cleaned
    assert "![keep-inside-code-fence](https://example.com/demo.png)" in cleaned


def test_compiler_structural_block_splitting_fence_safe(tmp_path: Path) -> None:
    """Verify code-heavy/table-heavy single pages split cleanly outside code fences."""
    settings = SyncSettings(
        project_root=tmp_path,
        output_dir=tmp_path / "output" / "markdown",
        max_chars_per_doc=750_000,
        max_blocks_per_doc=60,
    )
    sections: list[str] = ["# CLI Command Reference\n\nOverview."]
    for idx in range(12):
        code_blocks = "\n\n".join(
            f"```bash\n## comment inside code fence {idx}_{j}\ndash0 check-rules list\n```"
            for j in range(6)
        )
        table = "| Flag | Description |\n| --- | --- |\n| `--output` | Output format |\n"
        sections.append(f"## Command Group {idx}\n\n{table}\n{code_blocks}")

    p = build_extracted_page(
        url="https://www.dash0.com/docs/dash0/miscellaneous/tooling/dash0-cli/commands",
        bucket="01_Dash0_Core_Docs",
        title="Command Reference",
        lastmod=None,
        body_markdown="\n\n".join(sections),
    )
    db_path = tmp_path / "state.db"
    with StateDB(db_path) as db:
        db.upsert_page(p)
        records = db.get_pages_by_bucket("01_Dash0_Core_Docs")

    bundles = compile_bucket(
        "01_Dash0_Core_Docs", records, settings, generated_display="2026-11-12 10:00 UTC"
    )
    assert len(bundles) >= 2
    for b in bundles:
        text = b.file_path.read_text(encoding="utf-8")
        # Every output bundle must maintain balanced code fences
        assert text.count("```") % 2 == 0


def test_compiler_non_empty_paragraph_line_budget_for_yaml(tmp_path: Path) -> None:
    """Verify dense YAML/code bundles split before exceeding Google Docs `<p>` paragraph limits."""
    settings = SyncSettings(
        project_root=tmp_path,
        output_dir=tmp_path / "output" / "markdown",
        max_non_empty_lines_per_doc=600,
    )
    db_path = tmp_path / "state.db"
    with StateDB(db_path) as db:
        for i in range(5):
            yaml_lines = "\n".join(f"  metric_{j}: value_{j}" for j in range(180))
            body = f"# Collector Config {i}\n\n|k|v|\n|-|-|\n|a|b|\n\n```yaml\n{yaml_lines}\n```"
            p = build_extracted_page(
                url=f"https://github.com/dash0hq/dash0-examples/blob/main/ex_{i}/otel.yaml",
                bucket="09_Dash0_GitHub_Repos_and_Examples",
                title=f"GitHub: dash0hq/dash0-examples — ex_{i}/otel.yaml",
                lastmod=None,
                body_markdown=body,
            )
            db.upsert_page(p)
        records = db.get_pages_by_bucket("09_Dash0_GitHub_Repos_and_Examples")

    bundles = compile_bucket(
        "09_Dash0_GitHub_Repos_and_Examples", records, settings, generated_display="2026-11-12 15:00 UTC"
    )
    assert len(bundles) >= 2
    for b in bundles:
        text = b.file_path.read_text(encoding="utf-8")
        non_empty = sum(1 for ln in text.splitlines() if ln.strip())
        assert non_empty <= 650



