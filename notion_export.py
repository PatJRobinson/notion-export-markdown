#! /usr/bin/env nix-shell
#! nix-shell -i python3 -p python3 python3Packages.requests


"""
Generic Notion -> Markdown exporter

What it does:
- Finds data sources by title
- Retrieves each data source schema
- Queries all rows with pagination
- Exports every property dynamically
- Resolves relation IDs to page titles
- Optionally exports page body text
- Writes one output.md

Setup:
  pip install requests

Environment:
  export NOTION_TOKEN="secret_xxx"

Usage:
  ./notion_export \
    --include-body \
    --out output.md \
    "Research Questions" \
    "Contributions" \
    "Core Claims" \
    "Evidence" \
    "Risks" \
    "Mitigations"
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests


NOTION_VERSION = "2026-03-11"
BASE_URL = "https://api.notion.com/v1"


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    return text


def md_escape(text: str) -> str:
    # Keep this light so Markdown stays readable.
    return text.replace("\r\n", "\n").strip()

def notion_rich_text_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value

    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                if "plain_text" in item:
                    parts.append(item["plain_text"])
                elif "text" in item and isinstance(item["text"], dict):
                    parts.append(item["text"].get("content", ""))
        return "".join(parts)

    if isinstance(value, dict):
        if "plain_text" in value:
            return value["plain_text"]
        if "name" in value and isinstance(value["name"], str):
            return value["name"]

    return ""


def safe_text(value: Any, fallback: str = "") -> str:
    text = notion_rich_text_to_text(value)
    if text:
        return text

    if isinstance(value, str):
        return value

    return fallback

@dataclass
class Node:
    page_id: str
    source_name: str
    title: str
    url: str
    properties: Dict[str, Any] = field(default_factory=dict)
    body_markdown: Optional[str] = None


class NotionClient:
    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            }
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        url = f"{BASE_URL}{path}"
        resp = self.session.request(method, url, timeout=60, **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"{method} {path} failed: {resp.status_code} {resp.text}")
        return resp.json()

    def search_data_sources_by_title(self, query: str) -> List[Dict[str, Any]]:
        payload = {
            "query": query,
            "filter": {"property": "object", "value": "data_source"},
            "page_size": 100,
        }
        data = self._request("POST", "/search", json=payload)
        return data.get("results", [])

    def retrieve_data_source(self, data_source_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/data_sources/{data_source_id}")

    def query_data_source_all(self, data_source_id: str) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            payload: Dict[str, Any] = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor

            data = self._request("POST", f"/data_sources/{data_source_id}/query", json=payload)
            results.extend(data.get("results", []))

            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            time.sleep(0.05)

        return results

    def retrieve_page(self, page_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/pages/{page_id}")

    def retrieve_page_property_all(self, page_id: str, property_id: str) -> List[Dict[str, Any]]:
        """
        For large relation/title/rich_text/etc. values, page property values may need pagination.
        """
        results: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            path = f"/pages/{page_id}/properties/{property_id}"
            if cursor:
                path += f"?start_cursor={cursor}"

            data = self._request("GET", path)
            if data.get("object") == "list":
                results.extend(data.get("results", []))
                if not data.get("has_more"):
                    break
                cursor = data.get("next_cursor")
            else:
                # Some property retrievals come back as a single object rather than a paginated list.
                results.append(data)
                break

            time.sleep(0.05)

        return results

    def retrieve_block_children_all(self, block_id: str) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            path = f"/blocks/{block_id}/children?page_size=100"
            if cursor:
                path += f"&start_cursor={cursor}"

            data = self._request("GET", path)
            results.extend(data.get("results", []))

            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            time.sleep(0.05)

        return results


def extract_rich_text_array(rich_text: List[Dict[str, Any]]) -> str:
    return "".join(part.get("plain_text", "") for part in rich_text)


def page_title_from_properties(properties: Dict[str, Any]) -> str:
    for _, prop in properties.items():
        if prop.get("type") == "title":
            return extract_rich_text_array(prop.get("title", [])) or "Untitled"
    return "Untitled"


def humanize_property_value(
    client: NotionClient,
    page: Dict[str, Any],
    prop_name: str,
    prop_value: Dict[str, Any],
    relation_title_lookup: Dict[str, str],
) -> Any:
    """
    Returns either:
      - string
      - list[str]
      - dict / fallback string
    """
    ptype = prop_value.get("type")

    if ptype == "title":
        title_items = prop_value.get("title", [])
        property_id = prop_value.get("id")
        # Use the page property endpoint in case the title is large.
        if property_id:
            try:
                items = client.retrieve_page_property_all(page["id"], property_id)
                title_parts = [item.get("title", {}) for item in items if item.get("object") == "property_item"]
                flat = []
                for t in title_parts:
                    if isinstance(t, dict):
                        flat.append(t.get("plain_text", ""))
                if flat:
                    return "".join(flat)
            except Exception:
                pass
        return extract_rich_text_array(title_items)

    if ptype == "rich_text":
        items = prop_value.get("rich_text", [])
        # Often fine inline; if truncated, page property endpoint is safer, but this keeps it simple.
        return extract_rich_text_array(items)

    if ptype == "number":
        return prop_value.get("number")

    if ptype == "select":
        sel = prop_value.get("select")
        return sel.get("name") if sel else ""

    if ptype == "multi_select":
        return [x.get("name", "") for x in prop_value.get("multi_select", [])]

    if ptype == "status":
        st = prop_value.get("status")
        return st.get("name") if st else ""

    if ptype == "date":
        date_obj = prop_value.get("date")
        if not date_obj:
            return ""
        start = date_obj.get("start", "")
        end = date_obj.get("end")
        return f"{start} → {end}" if end else start

    if ptype == "checkbox":
        return "true" if prop_value.get("checkbox") else "false"

    if ptype == "url":
        return prop_value.get("url") or ""

    if ptype == "email":
        return prop_value.get("email") or ""

    if ptype == "phone_number":
        return prop_value.get("phone_number") or ""

    if ptype == "people":
        people = []
        for person in prop_value.get("people", []):
            name = person.get("name") or person.get("person", {}).get("email") or person.get("id", "")
            if name:
                people.append(name)
        return people

    if ptype == "files":
        files_out = []
        for f in prop_value.get("files", []):
            name = f.get("name", "file")
            file_url = ""
            if f.get("type") == "external":
                file_url = f.get("external", {}).get("url", "")
            elif f.get("type") == "file":
                file_url = f.get("file", {}).get("url", "")
            files_out.append(f"{name}: {file_url}" if file_url else name)
        return files_out

    if ptype == "created_time":
        return prop_value.get("created_time") or ""

    if ptype == "last_edited_time":
        return prop_value.get("last_edited_time") or ""

    if ptype == "created_by":
        user = prop_value.get("created_by") or {}
        return user.get("name") or user.get("id", "")

    if ptype == "last_edited_by":
        user = prop_value.get("last_edited_by") or {}
        return user.get("name") or user.get("id", "")

    if ptype == "relation":
        relation_prop_id = prop_value.get("id")
        relation_items: List[Dict[str, Any]] = []

        # Inline relation values can be truncated for large relations.
        if relation_prop_id:
            try:
                property_items = client.retrieve_page_property_all(page["id"], relation_prop_id)
                for item in property_items:
                    if item.get("type") == "relation":
                        rel = item.get("relation")
                        if rel:
                            relation_items.append(rel)
            except Exception:
                relation_items = prop_value.get("relation", [])
        else:
            relation_items = prop_value.get("relation", [])

        titles = []
        for rel in relation_items:
            rel_id = rel.get("id")
            if not rel_id:
                continue
            title = relation_title_lookup.get(rel_id, rel_id)
            titles.append(title)
        return titles

    if ptype == "formula":
        formula = prop_value.get("formula", {})
        ftype = formula.get("type")
        return formula.get(ftype) if ftype else formula

    if ptype == "rollup":
        rollup = prop_value.get("rollup", {})
        rtype = rollup.get("type")
        if rtype == "number":
            return rollup.get("number")
        if rtype == "date":
            d = rollup.get("date")
            if not d:
                return ""
            start = d.get("start", "")
            end = d.get("end")
            return f"{start} → {end}" if end else start
        if rtype == "array":
            arr = []
            for item in rollup.get("array", []):
                # Reuse formatting on synthetic property values.
                synthetic = {"type": item.get("type"), item.get("type"): item.get(item.get("type"))}
                arr.append(humanize_property_value(client, page, prop_name, synthetic, relation_title_lookup))
            return arr
        return rollup

    if ptype in {"unique_id", "verification"}:
        return prop_value.get(ptype)

    return prop_value.get(ptype, f"[Unsupported property type: {ptype}]")


def render_blocks_as_markdown(blocks: List[Dict[str, Any]], indent: int = 0) -> str:
    lines: List[str] = []

    for block in blocks:
        btype = block.get("type")
        has_children = block.get("has_children", False)
        prefix = " " * indent

        def rt(key: str) -> str:
            return extract_rich_text_array(block.get(key, {}).get("rich_text", []))

        line = None

        if btype == "paragraph":
            text = rt("paragraph")
            if text:
                line = f"{prefix}{text}"
        elif btype == "heading_1":
            text = rt("heading_1")
            if text:
                line = f"{prefix}# {text}"
        elif btype == "heading_2":
            text = rt("heading_2")
            if text:
                line = f"{prefix}## {text}"
        elif btype == "heading_3":
            text = rt("heading_3")
            if text:
                line = f"{prefix}### {text}"
        elif btype == "bulleted_list_item":
            text = rt("bulleted_list_item")
            if text:
                line = f"{prefix}- {text}"
        elif btype == "numbered_list_item":
            text = rt("numbered_list_item")
            if text:
                line = f"{prefix}1. {text}"
        elif btype == "to_do":
            obj = block.get("to_do", {})
            text = extract_rich_text_array(obj.get("rich_text", []))
            checked = obj.get("checked", False)
            line = f"{prefix}- [{'x' if checked else ' '}] {text}"
        elif btype == "quote":
            text = rt("quote")
            if text:
                line = f"{prefix}> {text}"
        elif btype == "code":
            obj = block.get("code", {})
            text = extract_rich_text_array(obj.get("rich_text", []))
            lang = obj.get("language", "")
            line = f"{prefix}```{lang}\n{text}\n{prefix}```"
        elif btype == "callout":
            text = rt("callout")
            if text:
                line = f"{prefix}> {text}"
        elif btype == "toggle":
            text = rt("toggle")
            if text:
                line = f"{prefix}- {text}"
        elif btype == "divider":
            line = f"{prefix}---"
        elif btype in {"image", "file", "pdf", "bookmark", "embed", "video"}:
            line = f"{prefix}[{btype}]"
        elif btype == "child_page":
            title = block.get("child_page", {}).get("title", "Untitled")
            line = f"{prefix}- Child page: {title}"
        else:
            line = None

        if line:
            lines.append(line)

        if has_children:
            lines.append("")  # visual separation

    return "\n".join(lines).strip()


def notion_title_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                if "plain_text" in item:
                    parts.append(item["plain_text"])
                elif "text" in item and isinstance(item["text"], dict):
                    parts.append(item["text"].get("content", ""))
        return "".join(parts)
    return ""


def choose_best_data_source(match_query: str, candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    normalized_query = match_query.strip().lower()

    def title_of(ds: Dict[str, Any]) -> str:
        return safe_text(ds.get("title")) or safe_text(ds.get("name")) or ds.get("id", "")

    exact = [ds for ds in candidates if title_of(ds).strip() == match_query.strip()]
    if exact:
        return exact[0]

    lower = [ds for ds in candidates if title_of(ds).strip().lower() == normalized_query]
    if lower:
        return lower[0]

    contains = [ds for ds in candidates if normalized_query in title_of(ds).strip().lower()]
    if contains:
        return contains[0]

    return candidates[0]

def discover_data_sources(client: NotionClient, names: List[str]) -> List[Dict[str, Any]]:
    found = []

    for name in names:
        results = client.search_data_sources_by_title(name)
        if not results:
            raise RuntimeError(
                f"Could not find data source titled '{name}'. "
                "Make sure the original source is shared with the integration."
            )

        chosen = choose_best_data_source(name, results)
        chosen_title = safe_text(chosen.get("title")) or safe_text(chosen.get("name")) or name

        found.append(
            {
                "query_name": name,
                "id": chosen["id"],
                "title": chosen_title,   # guaranteed string
            }
        )

    return found

def collect_nodes(
    client: NotionClient,
    data_sources: List[Dict[str, Any]],
    include_body: bool,
) -> List[Node]:
    nodes: List[Node] = []

    # First pass: collect raw rows and titles
    raw_pages: List[Dict[str, Any]] = []
    page_title_lookup: Dict[str, str] = {}

    for ds in data_sources:
        rows = client.query_data_source_all(ds["id"])
        for row in rows:
            raw_pages.append({"source_name": ds["title"], "page": row})
            page_title_lookup[row["id"]] = page_title_from_properties(row.get("properties", {}))

    # Second pass: render properties with relation titles resolved
    for item in raw_pages:
        page = item["page"]
        source_name = item["source_name"]
        title = page_title_lookup.get(page["id"], "Untitled")

        rendered_props: Dict[str, Any] = {}
        for prop_name, prop_value in page.get("properties", {}).items():
            rendered_props[prop_name] = humanize_property_value(
                client=client,
                page=page,
                prop_name=prop_name,
                prop_value=prop_value,
                relation_title_lookup=page_title_lookup,
            )

        body_md: Optional[str] = None
        if include_body:
            try:
                blocks = client.retrieve_block_children_all(page["id"])
                body_md = render_blocks_as_markdown(blocks)
            except Exception as e:
                body_md = f"[Could not retrieve page body: {e}]"

        nodes.append(
            Node(
                page_id=page["id"],
                source_name=source_name,
                title=title,
                url=page.get("url", ""),
                properties=rendered_props,
                body_markdown=body_md,
            )
        )

    return nodes


def render_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        # Flatten nested lists reasonably.
        flat_parts = []
        for item in value:
            if isinstance(item, list):
                flat_parts.extend(str(x) for x in item)
            else:
                flat_parts.append(str(item))
        return "\n".join(f"- {md_escape(x)}" for x in flat_parts if str(x).strip())
    if isinstance(value, dict):
        return md_escape(str(value))
    return md_escape(str(value))


def render_markdown(nodes: List[Node], source_order: List[str]) -> str:
    grouped: Dict[str, List[Node]] = {name: [] for name in source_order}
    for node in nodes:
        grouped.setdefault(node.source_name, []).append(node)

    lines: List[str] = []
    lines.append("# Notion Export")
    lines.append("")

    for source_name in source_order:
        items = grouped.get(source_name, [])
        lines.append(f"## {source_name}")
        lines.append("")

        # Stable human ordering by title.
        items.sort(key=lambda n: n.title.lower())

        for node in items:
            lines.append(f"### {node.title}")
            lines.append("")
            lines.append(f"- Notion URL: {node.url}")
            lines.append("")

            for prop_name, value in node.properties.items():
                # Skip empty values.
                rendered = render_value(value)
                if not rendered.strip():
                    continue

                if "\n- " in rendered or rendered.startswith("- "):
                    lines.append(f"**{prop_name}**")
                    lines.append(rendered)
                    lines.append("")
                else:
                    lines.append(f"**{prop_name}**: {rendered}")
                    lines.append("")

            if node.body_markdown is not None:
                if node.body_markdown.strip():
                    lines.append("**Page body**")
                    lines.append(node.body_markdown)
                    lines.append("")
                else:
                    lines.append("**Page body**:")
                    lines.append("")
                    lines.append("_No page body content._")
                    lines.append("")

    return "\n".join(lines).strip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("data_source_names", nargs="+", help="Titles of Notion data sources to export")
    parser.add_argument("--out", default="output.md", help="Output markdown filename")
    parser.add_argument(
        "--include-body",
        action="store_true",
        help="Include page body blocks in addition to properties",
    )
    args = parser.parse_args()

    token = os.environ.get("NOTION_TOKEN")
    if not token:
        print("Missing NOTION_TOKEN environment variable.", file=sys.stderr)
        sys.exit(1)

    client = NotionClient(token)

    try:
        data_sources = discover_data_sources(client, args.data_source_names)
        nodes = collect_nodes(client, data_sources, include_body=args.include_body)
        source_order = [ds["title"] for ds in data_sources]
        markdown = render_markdown(nodes, source_order)

        with open(args.out, "w", encoding="utf-8") as f:
            f.write(markdown)

        print(f"Wrote {args.out} with {len(nodes)} pages.")
    except Exception as e:
        print(f"Export failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
