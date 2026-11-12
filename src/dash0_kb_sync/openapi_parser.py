"""Deterministic OpenAPI 3.0/3.1 specification downloader and Markdown compiler."""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from typing import Any

import yaml

from dash0_kb_sync.config import (
    API_REFERENCE_BUCKET,
    DiscoveredURL,
    ExtractedPage,
    SyncSettings,
    build_extracted_page,
    utc_now_display,
    utc_now_iso,
)
from dash0_kb_sync.discovery import create_http_client, fetch_with_retry
from dash0_kb_sync.extractor import BrowserExtractor

logger = logging.getLogger(__name__)

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head", "trace")

SCHEMA_ALPHA_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("A-C", "A", "C"),
    ("D-F", "D", "F"),
    ("G-L", "G", "L"),
    ("M-P", "M", "P"),
    ("Q-S", "Q", "S"),
    ("T-Z", "T", "Z"),
)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "general"


def _escape_cell(text: str) -> str:
    """Clean multi-line descriptions so Markdown table cells stay single-line and valid."""
    if not text:
        return "—"
    cleaned = text.replace("\r\n", "\n").strip()
    cleaned = re.sub(r"\s*\n+\s*", " ", cleaned)
    cleaned = cleaned.replace("|", "\\|")
    if len(cleaned) > 420:
        cleaned = cleaned[:417].rsplit(" ", 1)[0] + "..."
    return cleaned


def _resolve_ref(ref: str, spec: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Resolve an internal JSON pointer like `#/components/schemas/CheckRule`."""
    name = ref.rsplit("/", 1)[-1]
    if not ref.startswith("#/"):
        return name, {}
    node: Any = spec
    for part in ref[2:].split("/"):
        part_unescaped = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part_unescaped in node:
            node = node[part_unescaped]
        else:
            return name, {}
    return name, node if isinstance(node, dict) else {}


def _format_schema_type(schema: Any, spec: dict[str, Any], depth: int = 0) -> str:
    """Format an OpenAPI 3.0/3.1 schema expression into a concise, readable type string."""
    if not isinstance(schema, dict) or depth > 4:
        return "any"

    if "$ref" in schema:
        ref_name = str(schema["$ref"]).rsplit("/", 1)[-1]
        return f"`{ref_name}`"

    for keyword, sep in (("oneOf", " | "), ("anyOf", " | "), ("allOf", " & ")):
        if keyword in schema and isinstance(schema[keyword], list):
            parts = [_format_schema_type(sub, spec, depth + 1) for sub in schema[keyword][:8]]
            extra = f" (+{len(schema[keyword]) - 8} more)" if len(schema[keyword]) > 8 else ""
            return sep.join(parts) + extra

    raw_type = schema.get("type")
    nullable = bool(schema.get("nullable"))
    if isinstance(raw_type, list):
        non_null = [t for t in raw_type if t != "null"]
        if "null" in raw_type:
            nullable = True
        base_type = " | ".join(str(t) for t in non_null) if non_null else "null"
    elif isinstance(raw_type, str):
        base_type = raw_type
    elif "properties" in schema:
        base_type = "object"
    elif "enum" in schema:
        base_type = "enum"
    elif "const" in schema:
        return f"const `{json.dumps(schema['const'])}`"
    else:
        base_type = "any"

    if base_type == "array":
        items = schema.get("items", {})
        item_type = _format_schema_type(items, spec, depth + 1)
        result = f"array<{item_type}>"
    elif base_type == "object" and isinstance(schema.get("additionalProperties"), dict):
        val_type = _format_schema_type(schema["additionalProperties"], spec, depth + 1)
        result = f"map<string, {val_type}>"
    else:
        fmt = schema.get("format")
        result = f"{base_type} ({fmt})" if fmt else base_type

    if nullable and "null" not in result:
        result = f"{result} | null"
    return result


def _format_constraints(schema: dict[str, Any]) -> str:
    """Extract default, enum, const, pattern, and numeric/length bounds."""
    if not isinstance(schema, dict):
        return "—"
    notes: list[str] = []
    if "const" in schema:
        notes.append(f"const={json.dumps(schema['const'])}")
    if "default" in schema:
        notes.append(f"default={json.dumps(schema['default'])}")
    if "enum" in schema and isinstance(schema["enum"], list):
        vals = [json.dumps(v) for v in schema["enum"][:10]]
        suffix = f", … ({len(schema['enum'])} total)" if len(schema["enum"]) > 10 else ""
        notes.append("enum: " + ", ".join(vals) + suffix)
    for key in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"):
        if key in schema:
            notes.append(f"{key}={schema[key]}")
    if "pattern" in schema:
        notes.append(f"pattern=`{schema['pattern']}`")
    if schema.get("readOnly"):
        notes.append("readOnly")
    if schema.get("writeOnly"):
        notes.append("writeOnly")
    return _escape_cell("; ".join(notes)) if notes else "—"


def _collect_object_properties(
    schema: dict[str, Any],
    spec: dict[str, Any],
    visited_refs: set[str] | None = None,
) -> tuple[dict[str, Any], set[str]]:
    """Collect direct + `allOf` merged properties and required fields for a schema."""
    if visited_refs is None:
        visited_refs = set()

    props: dict[str, Any] = {}
    required: set[str] = set()

    if "$ref" in schema:
        ref = str(schema["$ref"])
        if ref in visited_refs:
            return props, required
        visited_refs.add(ref)
        _, target = _resolve_ref(ref, spec)
        return _collect_object_properties(target, spec, visited_refs)

    if isinstance(schema.get("allOf"), list):
        for sub in schema["allOf"]:
            if isinstance(sub, dict):
                sub_props, sub_req = _collect_object_properties(sub, spec, visited_refs)
                props.update(sub_props)
                required.update(sub_req)

    if isinstance(schema.get("properties"), dict):
        props.update(schema["properties"])
    if isinstance(schema.get("required"), list):
        required.update(str(r) for r in schema["required"])

    return props, required


def _render_properties_table(
    props: dict[str, Any],
    required_set: set[str],
    spec: dict[str, Any],
) -> str:
    """Render an OpenAPI object's properties as a clean Markdown table."""
    if not props:
        return ""
    lines = [
        "| Property | Type | Required | Constraints / Enum | Description |",
        "| :--- | :--- | :---: | :--- | :--- |",
    ]
    for prop_name, prop_schema in props.items():
        p_dict = prop_schema if isinstance(prop_schema, dict) else {}
        type_str = _escape_cell(_format_schema_type(p_dict, spec))
        req_str = "Yes" if prop_name in required_set else "No"
        # Resolve $ref solely for description/constraints fallback if wrapper has none
        desc_raw = str(p_dict.get("description") or "")
        constraints_src = p_dict
        if "$ref" in p_dict and not desc_raw:
            _, ref_target = _resolve_ref(str(p_dict["$ref"]), spec)
            desc_raw = str(ref_target.get("description") or "")
            constraints_src = ref_target
        constraints_str = _format_constraints(constraints_src)
        desc_str = _escape_cell(desc_raw)
        lines.append(f"| `{prop_name}` | {type_str} | {req_str} | {constraints_str} | {desc_str} |")
    return "\n".join(lines)


def _render_overview_markdown(spec: dict[str, Any], spec_url: str, tag_counts: dict[str, int]) -> str:
    """Compile the API Overview, Authentication, Base URLs, and Versioning section."""
    info = spec.get("info", {})
    title = info.get("title", "Dash0 API")
    version = info.get("version", "1.0.0")
    openapi_ver = spec.get("openapi", "3.1.0")
    description = (info.get("description") or "").strip()

    parts: list[str] = [
        f"# {title} — Overview, Authentication & Servers",
        "",
        f"- **OpenAPI Version:** `{openapi_ver}`",
        f"- **API Version:** `{version}`",
        f"- **OpenAPI Specification URL:** {spec_url}",
        "",
    ]

    servers = spec.get("servers") or []
    if servers:
        parts.extend(
            [
                "## Base URLs & Regional Endpoints",
                "",
                "| Base URL | Region / Description |",
                "| :--- | :--- |",
            ]
        )
        for srv in servers:
            if isinstance(srv, dict):
                parts.append(
                    f"| `{srv.get('url', '')}` | {_escape_cell(str(srv.get('description', '')))} |"
                )
        parts.append("")

    sec_schemes = (spec.get("components") or {}).get("securitySchemes") or {}
    if sec_schemes:
        parts.extend(["## Authentication & Security Schemes", ""])
        for s_name, s_def in sec_schemes.items():
            if not isinstance(s_def, dict):
                continue
            s_type = s_def.get("type", "")
            scheme = s_def.get("scheme", "")
            s_desc = (s_def.get("description") or "").strip()
            parts.append(f"### `{s_name}` (`{s_type}`{f' / `{scheme}`' if scheme else ''})")
            if s_desc:
                parts.append(s_desc)
            if scheme == "bearer":
                parts.extend(
                    [
                        "",
                        "```http",
                        "Authorization: Bearer <DASH0_AUTH_TOKEN>",
                        "Dash0-Dataset: default",
                        "```",
                    ]
                )
            parts.append("")

    if description:
        parts.extend(["## API Conventions & Guidelines", "", description, ""])

    if tag_counts:
        parts.extend(
            [
                "## Endpoint Groups (Tags)",
                "",
                "| Tag | Endpoint Count |",
                "| :--- | :---: |",
            ]
        )
        for tag_name, count in tag_counts.items():
            parts.append(f"| **{tag_name}** | {count} |")
        parts.append("")

    return "\n".join(parts).strip()


def _render_operation_markdown(
    method: str,
    path: str,
    op: dict[str, Any],
    path_params: list[Any],
    spec: dict[str, Any],
) -> str:
    """Compile a single OpenAPI operation (`METHOD /path`) into detailed Markdown."""
    method_up = method.upper()
    summary = (op.get("summary") or "").strip()
    op_id = op.get("operationId") or ""
    deprecated = bool(op.get("deprecated"))
    description = (op.get("description") or "").strip()

    heading = f"## `{method_up} {path}`"
    if summary:
        heading += f" — {summary}"

    lines: list[str] = [heading, ""]
    if deprecated:
        lines.extend(["> **Warning:** This endpoint is marked as **deprecated**.", ""])

    meta_bits: list[str] = []
    if op_id:
        meta_bits.append(f"**Operation ID:** `{op_id}`")
    sec = op.get("security", spec.get("security"))
    if sec == []:
        meta_bits.append("**Auth:** Public (no token required)")
    elif isinstance(sec, list) and sec:
        schemes = [k for item in sec if isinstance(item, dict) for k in item.keys()]
        if schemes:
            meta_bits.append("**Auth:** " + ", ".join(f"`{s}`" for s in schemes))
    if meta_bits:
        lines.append("- " + " · ".join(meta_bits))
        lines.append("")

    if description:
        lines.extend([description, ""])

    # Merge path-level and operation-level parameters (operation overrides path by name+in)
    merged_params: dict[tuple[str, str], dict[str, Any]] = {}
    for raw_p in list(path_params) + list(op.get("parameters") or []):
        if not isinstance(raw_p, dict):
            continue
        p_obj = _resolve_ref(str(raw_p["$ref"]), spec)[1] if "$ref" in raw_p else raw_p
        if not p_obj:
            continue
        key = (str(p_obj.get("name", "")), str(p_obj.get("in", "query")))
        merged_params[key] = p_obj

    if merged_params:
        lines.extend(
            [
                "### Parameters",
                "",
                "| Name | In | Type | Required | Constraints | Description |",
                "| :--- | :---: | :--- | :---: | :--- | :--- |",
            ]
        )
        for (p_name, p_in), p_obj in merged_params.items():
            p_schema = p_obj.get("schema") if isinstance(p_obj.get("schema"), dict) else {}
            p_type = _escape_cell(_format_schema_type(p_schema, spec))
            p_req = "Yes" if p_obj.get("required") or p_in == "path" else "No"
            p_const = _format_constraints(p_schema)
            p_desc = _escape_cell(str(p_obj.get("description") or ""))
            lines.append(
                f"| `{p_name}` | `{p_in}` | {p_type} | {p_req} | {p_const} | {p_desc} |"
            )
        lines.append("")

    # Request Body
    req_body = op.get("requestBody")
    if isinstance(req_body, dict):
        if "$ref" in req_body:
            _, req_body = _resolve_ref(str(req_body["$ref"]), spec)
        rb_req = "required" if req_body.get("required") else "optional"
        rb_desc = (req_body.get("description") or "").strip()
        lines.append(f"### Request Body ({rb_req})")
        lines.append("")
        if rb_desc:
            lines.extend([rb_desc, ""])

        content_map = req_body.get("content") or {}
        for ctype, media_obj in content_map.items():
            if not isinstance(media_obj, dict):
                continue
            m_schema = media_obj.get("schema") if isinstance(media_obj.get("schema"), dict) else {}
            s_type = _format_schema_type(m_schema, spec)
            lines.append(f"- **Content-Type:** `{ctype}` — Schema: {s_type}")
            props, req_fields = _collect_object_properties(m_schema, spec)
            if props:
                lines.append("")
                lines.append(_render_properties_table(props, req_fields, spec))
            lines.append("")

    # Responses
    responses = op.get("responses") or {}
    if isinstance(responses, dict) and responses:
        lines.extend(
            [
                "### Responses",
                "",
                "| Status | Content-Type | Schema | Description |",
                "| :---: | :--- | :--- | :--- |",
            ]
        )
        for status_code, resp_obj in responses.items():
            if not isinstance(resp_obj, dict):
                continue
            if "$ref" in resp_obj:
                _, resp_obj = _resolve_ref(str(resp_obj["$ref"]), spec)
            r_desc = _escape_cell(str(resp_obj.get("description") or ""))
            r_content = resp_obj.get("content") or {}
            if not r_content:
                lines.append(f"| `{status_code}` | — | — | {r_desc} |")
            else:
                for ctype, media_obj in r_content.items():
                    m_schema = (
                        media_obj.get("schema")
                        if isinstance(media_obj, dict) and isinstance(media_obj.get("schema"), dict)
                        else {}
                    )
                    s_type = _escape_cell(_format_schema_type(m_schema, spec))
                    lines.append(f"| `{status_code}` | `{ctype}` | {s_type} | {r_desc} |")
        lines.append("")

    return "\n".join(lines).strip()


def _render_schema_definition(name: str, schema: dict[str, Any], spec: dict[str, Any]) -> str:
    """Compile a single `#/components/schemas/{name}` entry into Markdown."""
    lines: list[str] = [f"## Schema: `{name}`", ""]
    lines.append(f"- **JSON Pointer:** `#/components/schemas/{name}`")
    lines.append(f"- **Type:** {_format_schema_type(schema, spec)}")
    constraints = _format_constraints(schema)
    if constraints != "—":
        lines.append(f"- **Constraints:** {constraints}")
    lines.append("")

    desc = (schema.get("description") or "").strip()
    if desc:
        lines.extend([desc, ""])

    disc = schema.get("discriminator")
    if isinstance(disc, dict) and disc.get("propertyName"):
        prop_name = disc["propertyName"]
        lines.append(f"- **Discriminator Property:** `{prop_name}`")
        mapping = disc.get("mapping")
        if isinstance(mapping, dict) and mapping:
            mapped = ", ".join(
                f"`{k}` -> `{str(v).rsplit('/', 1)[-1]}`" for k, v in list(mapping.items())[:25]
            )
            lines.append(f"- **Discriminator Mapping:** {mapped}")
        lines.append("")

    props, req_fields = _collect_object_properties(schema, spec)
    if props:
        lines.append(_render_properties_table(props, req_fields, spec))
        lines.append("")

    return "\n".join(lines).strip()


def compile_openapi_spec_to_pages(
    spec: dict[str, Any],
    spec_url: str = "https://www.dash0.com/docs/api-reference/openapi.json",
    lastmod: str | None = None,
) -> list[ExtractedPage]:
    """Deterministically compile an OpenAPI 3.0/3.1 spec into logical `03_Dash0_API_Reference` pages."""
    now_iso = utc_now_iso()
    now_disp = utc_now_display()

    # Group operations by Tag preserving OpenAPI tag order
    declared_tags = [
        str(t["name"])
        for t in (spec.get("tags") or [])
        if isinstance(t, dict) and t.get("name")
    ]
    tag_descriptions: dict[str, str] = {
        str(t["name"]): str(t.get("description") or "").strip()
        for t in (spec.get("tags") or [])
        if isinstance(t, dict) and t.get("name")
    }

    ops_by_tag: dict[str, list[tuple[str, str, dict[str, Any], list[Any]]]] = defaultdict(list)
    paths = spec.get("paths") or {}
    for path_str in sorted(paths.keys()):
        path_item = paths[path_str]
        if not isinstance(path_item, dict):
            continue
        path_params = path_item.get("parameters") if isinstance(path_item.get("parameters"), list) else []
        for method in HTTP_METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            tags = op.get("tags") or ["Untagged"]
            primary_tag = str(tags[0]) if tags else "Untagged"
            ops_by_tag[primary_tag].append((method, path_str, op, path_params))

    ordered_tags = [t for t in declared_tags if t in ops_by_tag] + sorted(
        t for t in ops_by_tag.keys() if t not in declared_tags
    )
    tag_counts = {t: len(ops_by_tag[t]) for t in ordered_tags}

    pages: list[ExtractedPage] = []

    # 1. Overview Page
    overview_md = _render_overview_markdown(spec, spec_url, tag_counts)
    pages.append(
        build_extracted_page(
            url="https://www.dash0.com/docs/api-reference",
            bucket=API_REFERENCE_BUCKET,
            title="Dash0 API Reference — Overview, Authentication & Base URLs",
            lastmod=lastmod,
            body_markdown=overview_md,
            verified_iso=now_iso,
            verified_display=now_disp,
        )
    )

    # 2. Tag Endpoint Pages
    for tag_name in ordered_tags:
        tag_slug = _slugify(tag_name)
        tag_desc = tag_descriptions.get(tag_name, "")
        section_lines: list[str] = [f"# Dash0 API — {tag_name}", ""]
        if tag_desc:
            section_lines.extend([tag_desc, ""])
        for method, path_str, op, path_params in ops_by_tag[tag_name]:
            section_lines.append(_render_operation_markdown(method, path_str, op, path_params, spec))
            section_lines.append("\n---\n")

        pages.append(
            build_extracted_page(
                url=f"https://www.dash0.com/docs/api-reference#tag-{tag_slug}",
                bucket=API_REFERENCE_BUCKET,
                title=f"Dash0 API Endpoints — {tag_name}",
                lastmod=lastmod,
                body_markdown="\n".join(section_lines).strip(),
                verified_iso=now_iso,
                verified_display=now_disp,
            )
        )

    # 3. Component Schemas grouped deterministically by alphabetical range
    schemas = (spec.get("components") or {}).get("schemas") or {}
    if isinstance(schemas, dict) and schemas:
        grouped_schemas: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        for s_name in sorted(schemas.keys(), key=lambda s: s.lower()):
            s_def = schemas[s_name]
            if not isinstance(s_def, dict):
                continue
            first_ch = s_name[0].upper() if s_name else "Z"
            assigned = "T-Z"
            for label, start_ch, end_ch in SCHEMA_ALPHA_GROUPS:
                if start_ch <= first_ch <= end_ch:
                    assigned = label
                    break
            grouped_schemas[assigned].append((s_name, s_def))

        for label, _, _ in SCHEMA_ALPHA_GROUPS:
            items = grouped_schemas.get(label)
            if not items:
                continue
            schema_lines: list[str] = [
                f"# Dash0 API Component Schemas ({label})",
                "",
                f"Contains **{len(items)}** OpenAPI component schemas (`#/components/schemas/*`).",
                "",
            ]
            for s_name, s_def in items:
                schema_lines.append(_render_schema_definition(s_name, s_def, spec))
                schema_lines.append("\n---\n")

            pages.append(
                build_extracted_page(
                    url=f"https://www.dash0.com/docs/api-reference#schemas-{label.lower()}",
                    bucket=API_REFERENCE_BUCKET,
                    title=f"Dash0 API Component Schemas ({label})",
                    lastmod=lastmod,
                    body_markdown="\n".join(schema_lines).strip(),
                    verified_iso=now_iso,
                    verified_display=now_disp,
                )
            )

    return pages


def parse_openapi_bytes(raw_bytes: bytes, source_url: str) -> dict[str, Any]:
    """Parse raw OpenAPI JSON or YAML bytes into a Python dictionary."""
    text = raw_bytes.decode("utf-8")
    if source_url.lower().endswith((".yaml", ".yml")):
        data = yaml.safe_load(text)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = yaml.safe_load(text)
    if not isinstance(data, dict) or ("openapi" not in data and "swagger" not in data):
        raise ValueError(f"Payload from {source_url} is not a valid OpenAPI document.")
    return data


async def extract_api_reference(
    settings: SyncSettings,
    extractor: BrowserExtractor,
    fallback_urls: list[DiscoveredURL],
) -> list[ExtractedPage]:
    """Discover and compile Dash0's OpenAPI specification, falling back to HTML scraping if unavailable."""
    api_index_url = "https://www.dash0.com/docs/api-reference"
    spec_url = await extractor.discover_openapi_spec_url(api_index_url)
    candidate_urls: list[str] = []
    if spec_url:
        candidate_urls.append(spec_url)
    default_json_url = "https://www.dash0.com/docs/api-reference/openapi.json"
    if default_json_url not in candidate_urls:
        candidate_urls.append(default_json_url)

    async with create_http_client(timeout_s=30.0) as client:
        for url in candidate_urls:
            try:
                resp = await fetch_with_retry(client, url)
                if resp.status_code == 200 and resp.content:
                    spec = parse_openapi_bytes(resp.content, url)
                    lastmod = resp.headers.get("last-modified")
                    compiled = compile_openapi_spec_to_pages(spec, spec_url=url, lastmod=lastmod)
                    logger.info(
                        "Compiled OpenAPI spec (%s) into %d structured reference sections",
                        url,
                        len(compiled),
                    )
                    return compiled
            except Exception as exc:
                logger.warning("Failed to download/parse OpenAPI spec from %s: %s", url, exc)

    logger.warning(
        "OpenAPI specification unavailable; falling back to Playwright HTML scraping (%d URLs)",
        len(fallback_urls),
    )
    outcomes = await extractor.scrape_batch(fallback_urls)
    return [o.page for o in outcomes if o.page is not None]
