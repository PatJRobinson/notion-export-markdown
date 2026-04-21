#! /usr/bin/env nix-shell
#! nix-shell -i python3 -p python3 python3Packages.requests

"""
Generic-but-opinionated Notion -> Markdown exporter

Usage:
  export NOTION_TOKEN="secret_xxx"

  ./notion_export \
    --out output.md \
    "research questions" \
    "contributions" \
    "core claims" \
    "evidence" \
    "risks" \
    "mitigations"

Optional:
  --include-body     include page body text
  --verbose          include noisy metadata fields
  --include-untitled include untitled pages
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests


NOTION_VERSION = "2026-03-11"
BASE_URL = "https://api.notion.com/v1"


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def progress(message: str) -> None:
    print(f"[progress] {message}", file=sys.stderr, flush=True)

def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    return text


def md_escape(text: str) -> str:
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


def extract_rich_text_array(rich_text: List[Dict[str, Any]]) -> str:
    return "".join(part.get("plain_text", "") for part in rich_text)


def page_title_from_properties(properties: Dict[str, Any]) -> str:
    for _, prop in properties.items():
        if prop.get("type") == "title":
            return extract_rich_text_array(prop.get("title", [])) or "Untitled"
    return "Untitled"


def is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        stripped = value.strip()
        return stripped == "" or stripped == "_No page body content._"
    if isinstance(value, list):
        return len(value) == 0
    return False


def clean_text(text: str) -> str:
    """
    Light cleanup for Notion-exported prose that often contains broken bullet formatting.
    """
    text = text.replace("\r\n", "\n")

    # Fix bullet glyph lines into Markdown bullets
    text = re.sub(r"[ \t]*•[ \t]*\n[ \t]*", "- ", text)
    text = re.sub(r"[ \t]*•[ \t]+", "- ", text)

    # Add spacing after labels if jammed together
    text = re.sub(r"(?i)(State clearly:)(\S)", r"\1\n\2", text)
    text = re.sub(r"(?i)(Not:)(\S)", r"\1\n\2", text)
    text = re.sub(r"(?i)(For example:)(\S)", r"\1\n\2", text)
    text = re.sub(r"(?i)(Risk:)(\S)", r"\1\n\2", text)
    text = re.sub(r"(?i)(Examiner push:)(\S)", r"\1\n\2", text)
    text = re.sub(r"(?i)(Examiner attack:)(\S)", r"\1\n\2", text)

    # Fix obvious label jams
    text = text.replace("Clearly state:findings", "Clearly state:\nfindings")
    text = text.replace("Not:demonstrating", "Not:\ndemonstrating")
    text = text.replace("State clearly:the goal", "State clearly:\nthe goal is")
    text = text.replace("State clearly:The thesis", "State clearly:\nThe thesis")
    text = text.replace("Description: Actively identify:", "Actively identify:")
    text = text.replace("Description: Prioritise coding and analysis of:", "Prioritise coding and analysis of:")
    text = text.replace("Description: Structure interviews around:", "Structure interviews around:")
    text = text.replace("Description: Consistently frame findings as:", "Consistently frame findings as:")
    text = text.replace("Description: Define and demonstrate triangulation as a primary methodological strategy:",
                        "Define and demonstrate triangulation as a primary methodological strategy:")
    text = text.replace("Description: RQ3 is explicitely not tied to security.\n\nBut now:",
                        "RQ3 is explicitely not tied to security.\n\nBut now:")
    text = text.replace("Description: Explicitly justify ROS2 as:",
                        "Explicitly justify ROS2 as:")
    text = text.replace("Description: Explicitly position ROS2 as:",
                        "Explicitly position ROS2 as:")
    text = text.replace("Description: For each major empirical theme:",
                        "For each major empirical theme:")
    text = text.replace("Description: For the practice layer, do not require assumptions to be explicitly stated by participants. Infer them from:",
                        "For the practice layer, do not require assumptions to be explicitly stated by participants. Infer them from:")
    text = text.replace("Description: Use the same extraction questions for every artefact you analyse:",
                        "Use the same extraction questions for every artefact you analyse:")
    text = text.replace("Description: Adopt consistent phrasing:",
                        "Adopt consistent phrasing:")
    text = text.replace("Description: Frame findings as:Given assumptions A and B, when components interact under condition C, behaviour D can emerge.",
                        "Frame findings as:\nGiven assumptions A and B, when components interact under condition C, behaviour D can emerge.")
    text = text.replace("Avoid claims of:A causes D", "Avoid claims of:\nA causes D")
    text = text.replace("Instead, present:", "Instead, present:\n")

    # Normalize weird numbering blocks
    text = re.sub(r"\n([0-9]+)\.\s*\n", r"\n\1. ", text)

    # Collapse excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def render_value(value: Any) -> str:
    if value is None:
        return ""

    if isinstance(value, list):
        flat_parts = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, list):
                flat_parts.extend(str(x) for x in item if str(x).strip())
            else:
                flat_parts.append(str(item))
        if not flat_parts:
            return ""
        return "\n".join(f"- {md_escape(clean_text(x))}" for x in flat_parts if str(x).strip())

    if isinstance(value, dict):
        return md_escape(clean_text(str(value)))

    return md_escape(clean_text(str(value)))


def parse_numeric_id(value: Any) -> Optional[int]:
    """
    Supports IDs shaped like:
      {'prefix': None, 'number': 5}
    or just a number/string.
    """
    if value is None:
        return None

    if isinstance(value, dict):
        number = value.get("number")
        if isinstance(number, int):
            return number
        if isinstance(number, str) and number.isdigit():
            return int(number)

    if isinstance(value, int):
        return value

    if isinstance(value, str):
        match = re.search(r"\b(\d+)\b", value)
        if match:
            return int(match.group(1))

    return None


# -----------------------------------------------------------------------------
# Data structures
# -----------------------------------------------------------------------------

@dataclass
class Node:
    page_id: str
    source_name: str
    title: str
    url: str
    properties: Dict[str, Any] = field(default_factory=dict)
    body_markdown: Optional[str] = None


# -----------------------------------------------------------------------------
# Notion client
# -----------------------------------------------------------------------------

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

    def query_data_source_all(self, data_source_id: str) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        page_num = 0

        while True:
            page_num += 1
            progress(f"Querying data source {data_source_id}: page {page_num}")
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

    def retrieve_page_property_all(self, page_id: str, property_id: str) -> List[Dict[str, Any]]:
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


# -----------------------------------------------------------------------------
# Discovery
# -----------------------------------------------------------------------------

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

    for i, name in enumerate(names, start=1):
        progress(f"Finding data source {i}/{len(names)}: {name}")
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
                "title": chosen_title,
            }
        )

    return found


# -----------------------------------------------------------------------------
# Property rendering
# -----------------------------------------------------------------------------

def humanize_property_value(
    client: NotionClient,
    page: Dict[str, Any],
    prop_name: str,
    prop_value: Dict[str, Any],
    relation_title_lookup: Dict[str, str],
) -> Any:
    ptype = prop_value.get("type")

    if ptype == "title":
        title_items = prop_value.get("title", [])
        property_id = prop_value.get("id")
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
        return extract_rich_text_array(prop_value.get("rich_text", []))

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

        if line:
            lines.append(line)

    return clean_text("\n".join(lines).strip())


# -----------------------------------------------------------------------------
# Collection
# -----------------------------------------------------------------------------

def collect_nodes(
    client: NotionClient,
    data_sources: List[Dict[str, Any]],
    include_body: bool,
) -> List[Node]:
    nodes: List[Node] = []

    raw_pages: List[Dict[str, Any]] = []
    page_title_lookup: Dict[str, str] = {}
    progress("Collecting rows from data sources")

    for ds in data_sources:
        progress(f"Loading rows from: {ds['title']}")
        rows = client.query_data_source_all(ds["id"])
        progress(f"Loaded {len(rows)} rows from: {ds['title']}")
        for row in rows:
            raw_pages.append({"source_name": ds["title"], "page": row})
            page_title_lookup[row["id"]] = page_title_from_properties(row.get("properties", {}))

    progress(f"Rendering {len(raw_pages)} pages")
    for idx, item in enumerate(raw_pages, start=1):
        page = item["page"]
        source_name = safe_text(item["source_name"], "Untitled Source")
        title = page_title_lookup.get(page["id"], "Untitled")

        progress(f"[{idx}/{len(raw_pages)}] Processing {source_name} :: {title}")

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
            progress(f"[{idx}/{len(raw_pages)}] Fetching page body for {title}")
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


# -----------------------------------------------------------------------------
# Formatting config
# -----------------------------------------------------------------------------

SECTION_CONFIG: Dict[str, Dict[str, Any]] = {
    "Research Questions": {
        "order": ["question", "Contributions", "Risks"],
        "hide": {"Name", "ID", "Page body", "Methods"},
        "labels": {
            "question": "Question",
            "Contributions": "Linked contributions",
            "Risks": "Key risks",
        },
        "anchor_prefix": "RQ",
    },
    "Contributions": {
        "order": ["Statement", "Grounding", "Justification", "Boundaries", "Research Questions"],
        "hide": {"Name", "ID", "Page body", "Methods"},
        "labels": {
            "Statement": "Claim",
            "Research Questions": "Linked research questions",
        },
        "anchor_prefix": "C",
    },
    "Core Claims": {
        "order": ["Statement", "Evidence", "Contributions"],
        "hide": {"Name", "ID", "Page body"},
        "labels": {
            "Statement": "Claim",
            "Contributions": "Linked contributions",
        },
        "anchor_prefix": "CC",
    },
    "Evidence": {
        "order": ["Description", "Core Claims"],
        "hide": {"Name", "ID", "Page body"},
        "labels": {
            "Description": "Evidence",
            "Core Claims": "Linked core claims",
        },
        "anchor_prefix": "E",
    },
    "Risks": {
        "order": ["Type", "Research Questions", "Description", "Mitigation"],
        "hide": {"Name", "ID", "Page body"},
        "labels": {
            "Research Questions": "For",
            "Description": "Risk",
            "Mitigation": "Mitigations",
        },
        "anchor_prefix": "R",
    },
    "Mitigations": {
        "order": ["Description", "Risks"],
        "hide": {"Name", "ID", "Page body"},
        "labels": {
            "Description": "Mitigation",
            "Risks": "Addresses",
        },
        "anchor_prefix": "M",
    },
}


SOURCE_ALIASES = {
    "research questions": "Research Questions",
    "contributions": "Contributions",
    "core claims": "Core Claims",
    "evidence": "Evidence",
    "risks": "Risks",
    "mitigations": "Mitigations",
}


def normalize_source_name(name: str) -> str:
    return SOURCE_ALIASES.get(name.strip().lower(), name.strip())


def ordered_properties(source_name: str, properties: Dict[str, Any], verbose: bool) -> List[Tuple[str, Any]]:
    config = SECTION_CONFIG.get(source_name, {})
    order = config.get("order", [])
    hidden = set(config.get("hide", set()))

    if verbose:
        hidden = set()

    ordered: List[Tuple[str, Any]] = []
    seen = set()

    for key in order:
        if key in properties and key not in hidden:
            ordered.append((key, properties[key]))
            seen.add(key)

    for key, value in properties.items():
        if key not in seen and key not in hidden:
            ordered.append((key, value))

    return ordered


def label_for_property(source_name: str, prop_name: str) -> str:
    config = SECTION_CONFIG.get(source_name, {})
    labels = config.get("labels", {})
    return labels.get(prop_name, prop_name)


def node_numeric_sort_key(node: Node) -> Tuple[int, str]:
    raw_id = node.properties.get("ID")
    num = parse_numeric_id(raw_id)
    if num is not None:
        return (num, node.title.lower())
    return (10**9, node.title.lower())


# -----------------------------------------------------------------------------
# Markdown rendering
# -----------------------------------------------------------------------------

def render_property_block(source_name: str, prop_name: str, value: Any) -> List[str]:
    if is_empty_value(value):
        return []

    label = label_for_property(source_name, prop_name)
    rendered = render_value(value)
    if not rendered.strip():
        return []

    lines: List[str] = []

    if "\n- " in rendered or rendered.startswith("- "):
        lines.append(f"**{label}**")
        lines.append(rendered)
        lines.append("")
    else:
        lines.append(f"**{label}**")
        lines.append(rendered)
        lines.append("")

    return lines


def should_skip_node(node: Node, include_untitled: bool) -> bool:
    if not include_untitled and node.title.strip().lower() == "untitled":
        return True
    return False


def render_node(node: Node, verbose: bool, include_body: bool) -> str:
    lines: List[str] = [f"### {node.title}", ""]

    if verbose and node.url:
        lines.append(f"- Notion URL: {node.url}")
        lines.append("")

    for prop_name, value in ordered_properties(node.source_name, node.properties, verbose=verbose):
        if prop_name == "Name" and safe_text(value).strip() == node.title.strip() and not verbose:
            continue
        if prop_name == "ID" and not verbose:
            continue
        if prop_name == "Methods" and isinstance(value, list) and all(re.fullmatch(r"[0-9a-f-]{30,}", str(v)) for v in value):
            if not verbose:
                continue

        lines.extend(render_property_block(node.source_name, prop_name, value))

    if include_body and node.body_markdown is not None and node.body_markdown.strip():
        lines.append("**Page body**")
        lines.append(node.body_markdown)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def summarize_counts(nodes: List[Node], source_order: List[str], include_untitled: bool) -> List[str]:
    counts = []
    for source_name in source_order:
        count = sum(
            1
            for node in nodes
            if node.source_name == source_name and not should_skip_node(node, include_untitled)
        )
        if count > 0:
            counts.append(f"- {count} {source_name.lower()}")
    return counts


def render_standard_section(
    section_name: str,
    nodes: List[Node],
    verbose: bool,
    include_body: bool,
    include_untitled: bool,
) -> str:
    section_nodes = [
        node for node in nodes
        if node.source_name == section_name and not should_skip_node(node, include_untitled)
    ]
    section_nodes.sort(key=node_numeric_sort_key)

    if not section_nodes:
        return ""

    lines: List[str] = [f"## {section_name}", ""]

    for node in section_nodes:
        lines.append(render_node(node, verbose=verbose, include_body=include_body))
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def render_risks_grouped_by_rq(
    all_nodes: List[Node],
    verbose: bool,
    include_body: bool,
    include_untitled: bool,
) -> str:
    risk_nodes = [
        node for node in all_nodes
        if node.source_name == "Risks" and not should_skip_node(node, include_untitled)
    ]
    if not risk_nodes:
        return ""

    rq_nodes = [
        node for node in all_nodes
        if node.source_name == "Research Questions" and not should_skip_node(node, include_untitled)
    ]
    rq_nodes.sort(key=node_numeric_sort_key)

    grouped: Dict[str, List[Node]] = {}
    ungrouped: List[Node] = []

    for risk in risk_nodes:
        rq_links = risk.properties.get("Research Questions", [])
        if isinstance(rq_links, list) and rq_links:
            for rq_title in rq_links:
                grouped.setdefault(str(rq_title), []).append(risk)
        else:
            ungrouped.append(risk)

    if not grouped and not ungrouped:
        return ""

    lines: List[str] = ["## Risks", ""]

    # Render in RQ order first
    rendered_titles = set()
    for rq in rq_nodes:
        related = grouped.get(rq.title, [])
        if not related:
            continue
        related.sort(key=node_numeric_sort_key)
        rendered_titles.add(rq.title)

        lines.append(f"### {rq.title}")
        lines.append("")
        for risk in related:
            lines.append(f"#### {risk.title}")
            lines.append("")
            for prop_name, value in ordered_properties(risk.source_name, risk.properties, verbose=verbose):
                if prop_name == "Name" and safe_text(value).strip() == risk.title.strip() and not verbose:
                    continue
                if prop_name == "ID" and not verbose:
                    continue
                lines.extend(render_property_block(risk.source_name, prop_name, value))

            if include_body and risk.body_markdown is not None and risk.body_markdown.strip():
                lines.append("**Page body**")
                lines.append(risk.body_markdown)
                lines.append("")

        lines.append("")

    # Render grouped risks whose RQ title wasn't in RQ section
    for rq_title, related in sorted(grouped.items(), key=lambda x: x[0].lower()):
        if rq_title in rendered_titles:
            continue
        related.sort(key=node_numeric_sort_key)

        lines.append(f"### {rq_title}")
        lines.append("")
        for risk in related:
            lines.append(f"#### {risk.title}")
            lines.append("")
            for prop_name, value in ordered_properties(risk.source_name, risk.properties, verbose=verbose):
                if prop_name == "Name" and safe_text(value).strip() == risk.title.strip() and not verbose:
                    continue
                if prop_name == "ID" and not verbose:
                    continue
                lines.extend(render_property_block(risk.source_name, prop_name, value))
        lines.append("")

    # Render ungrouped risks at the end
    if ungrouped:
        ungrouped.sort(key=node_numeric_sort_key)
        lines.append("### Other risks")
        lines.append("")
        for risk in ungrouped:
            lines.append(f"#### {risk.title}")
            lines.append("")
            for prop_name, value in ordered_properties(risk.source_name, risk.properties, verbose=verbose):
                if prop_name == "Name" and safe_text(value).strip() == risk.title.strip() and not verbose:
                    continue
                if prop_name == "ID" and not verbose:
                    continue
                lines.extend(render_property_block(risk.source_name, prop_name, value))
            lines.append("")

    return "\n".join(lines).strip() + "\n"


def render_markdown(
    nodes: List[Node],
    source_order: List[str],
    include_body: bool,
    verbose: bool,
    include_untitled: bool,
) -> str:
    normalized_source_order = [normalize_source_name(name) for name in source_order]

    lines: List[str] = ["# Notion Export", ""]

    summary_lines = summarize_counts(nodes, normalized_source_order, include_untitled)
    if summary_lines:
        lines.extend(summary_lines)
        lines.append("")

    for source_name in normalized_source_order:
        if source_name == "Risks":
            section = render_risks_grouped_by_rq(
                all_nodes=nodes,
                verbose=verbose,
                include_body=include_body,
                include_untitled=include_untitled,
            )
        else:
            section = render_standard_section(
                section_name=source_name,
                nodes=nodes,
                verbose=verbose,
                include_body=include_body,
                include_untitled=include_untitled,
            )

        if section.strip():
            lines.append(section.rstrip())
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("data_source_names", nargs="+", help="Titles of Notion data sources to export")
    parser.add_argument("--out", default="output.md", help="Output markdown filename")
    parser.add_argument("--include-body", action="store_true", help="Include page body blocks")
    parser.add_argument("--verbose", action="store_true", help="Include noisy metadata fields")
    parser.add_argument("--include-untitled", action="store_true", help="Include untitled pages")
    args = parser.parse_args()

    token = os.environ.get("NOTION_TOKEN")
    if not token:
        print("Missing NOTION_TOKEN environment variable.", file=sys.stderr)
        sys.exit(1)

    client = NotionClient(token)

    try:
        data_sources = discover_data_sources(client, args.data_source_names)

        # Normalize names for prettier output
        for ds in data_sources:
            ds["title"] = normalize_source_name(ds["title"])

        nodes = collect_nodes(client, data_sources, include_body=args.include_body)
        for node in nodes:
            node.source_name = normalize_source_name(node.source_name)

        source_order = [ds["title"] for ds in data_sources]
        markdown = render_markdown(
            nodes=nodes,
            source_order=source_order,
            include_body=args.include_body,
            verbose=args.verbose,
            include_untitled=args.include_untitled,
        )

        with open(args.out, "w", encoding="utf-8") as f:
            f.write(markdown)

        print(f"Wrote {args.out} with {len(nodes)} pages.")

    except Exception as e:
        print(f"Export failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
