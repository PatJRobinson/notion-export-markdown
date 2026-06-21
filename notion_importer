#! /usr/bin/env nix-shell
#! nix-shell -i python3 -p python3 python3Packages.requests

"""
Markdown -> Notion Evidence importer

Purpose:
  Import evidence entries written in the same Markdown shape as the Notion
  exporter output, resolve Core Claim relations by title, and create/update
  rows in the Evidence data source.

Expected entry shape:

  ### Evidence title

  **Evidence**
  Description text...

  **Linked core claims**
  - Claim A
  - Claim B

  **Status**
  - Identified

  **Notes / extraction**
  Notes...

Usage:
  export NOTION_TOKEN="secret_xxx"

  ./notion_importer.py \
    --in evidence_to_import.md \
    --evidence-db "evidence" \
    --core-claims-db "core claims" \
    --dry-run

  ./notion_importer.py --in evidence_to_import.md --update

Defaults:
  --evidence-db "evidence"
  --core-claims-db "core claims"
  --mode skip-existing

Notes:
  - This script uses the Notion data_sources API shape used by your exporter.
  - It is intentionally conservative: by default, existing Evidence rows with the
    same title are skipped.
  - Use --update to patch existing rows.
  - Relation matching is by Core Claim page title.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


NOTION_VERSION = "2026-03-11"
BASE_URL = "https://api.notion.com/v1"


# -----------------------------------------------------------------------------
# Logging / text helpers
# -----------------------------------------------------------------------------

def progress(message: str) -> None:
    print(f"[progress] {message}", file=sys.stderr, flush=True)


def warn(message: str) -> None:
    print(f"[warn] {message}", file=sys.stderr, flush=True)


def die(message: str, code: int = 1) -> None:
    print(f"[error] {message}", file=sys.stderr, flush=True)
    sys.exit(code)


def normalize_title(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).casefold()


def strip_markdown_bullet(line: str) -> str:
    return re.sub(r"^\s*[-*]\s+", "", line).strip()


def split_listish(value: str) -> List[str]:
    """
    Parse exporter-style list fields.

    Handles:
      - bullet lists
      - newline-separated values
      - comma-separated fallback for single-line values
    """
    lines = [line.rstrip() for line in value.replace("\r\n", "\n").split("\n")]
    items: List[str] = []

    bullet_lines = [line for line in lines if re.match(r"^\s*[-*]\s+", line)]
    if bullet_lines:
        for line in bullet_lines:
            item = strip_markdown_bullet(line)
            if item:
                items.append(item)
        return items

    non_empty = [line.strip() for line in lines if line.strip()]
    if len(non_empty) > 1:
        return non_empty

    if len(non_empty) == 1 and "," in non_empty[0]:
        return [part.strip() for part in non_empty[0].split(",") if part.strip()]

    return non_empty


def first_listish(value: str) -> str:
    items = split_listish(value)
    return items[0] if items else value.strip()


def rich_text_chunks(text: str, chunk_size: int = 1900) -> List[Dict[str, Any]]:
    """
    Notion rich_text content max is 2000 chars; keep under that.
    """
    text = text.strip()
    if not text:
        return []
    chunks = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
    return [{"type": "text", "text": {"content": chunk}} for chunk in chunks]


def extract_rich_text_array(rich_text: List[Dict[str, Any]]) -> str:
    return "".join(part.get("plain_text", "") for part in rich_text)


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


def page_title_from_properties(properties: Dict[str, Any]) -> str:
    for _, prop in properties.items():
        if prop.get("type") == "title":
            return extract_rich_text_array(prop.get("title", [])) or "Untitled"
    return "Untitled"


# -----------------------------------------------------------------------------
# Markdown parsing
# -----------------------------------------------------------------------------

FIELD_LABELS: Dict[str, str] = {
    # Markdown label -> Notion property name
    "evidence": "Description",
    "description": "Description",
    "linked core claims": "Core Claims",
    "core claims": "Core Claims",
    "url": "URL",
    "status": "Status",
    "notes / extraction": "Notes / extraction",
    "notes": "Notes / extraction",
    "source kind": "Source Kind",
    "domain": "Domain",
    "scope/domain": "Domain",
    "use in thesis": "Use in thesis",
    "apparatus role": "Apparatus Role",
    "overall source quality": "Overall source quality",
    "type": "Type",
    "scope": "Scope",
}

@dataclass
class EvidenceEntry:
    title: str
    fields: Dict[str, str] = field(default_factory=dict)
    source_line: int = 0


def parse_evidence_markdown(markdown: str) -> List[EvidenceEntry]:
    """
    Parse one or more entries from Markdown.

    Starts a new entry at every H3 line:
      ### Title

    Field blocks are detected as:
      **Field Name**
      value until next **Field** or next ### heading
    """
    lines = markdown.replace("\r\n", "\n").split("\n")
    entries: List[EvidenceEntry] = []
    current: Optional[EvidenceEntry] = None
    current_field: Optional[str] = None
    current_buf: List[str] = []

    def flush_field() -> None:
        nonlocal current_field, current_buf, current
        if current is not None and current_field is not None:
            value = "\n".join(current_buf).strip()
            if value:
                normalized = FIELD_LABELS.get(current_field.strip().casefold(), current_field.strip())
                current.fields[normalized] = value
        current_field = None
        current_buf = []

    def flush_entry() -> None:
        nonlocal current
        flush_field()
        if current is not None and current.title.strip():
            entries.append(current)
        current = None

    heading_re = re.compile(r"^###\s+(.+?)\s*$")
    field_re = re.compile(r"^\*\*(.+?)\*\*\s*$")

    for idx, line in enumerate(lines, start=1):
        h = heading_re.match(line)
        if h:
            flush_entry()
            current = EvidenceEntry(title=h.group(1).strip(), source_line=idx)
            continue

        if current is None:
            continue

        f = field_re.match(line)
        if f:
            flush_field()
            current_field = f.group(1).strip()
            current_buf = []
            continue

        if current_field is not None:
            current_buf.append(line)

    flush_entry()
    return entries


# -----------------------------------------------------------------------------
# Notion client
# -----------------------------------------------------------------------------

class NotionClient:
    def __init__(self, token: str, request_delay: float = 0.05) -> None:
        self.request_delay = request_delay
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
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "1"))
            time.sleep(retry_after)
            resp = self.session.request(method, url, timeout=60, **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"{method} {path} failed: {resp.status_code} {resp.text}")
        if self.request_delay:
            time.sleep(self.request_delay)
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

        return results

    def create_page(self, data_source_id: str, properties: Dict[str, Any]) -> Dict[str, Any]:
        payload = {
            "parent": {"data_source_id": data_source_id},
            "properties": properties,
        }
        try:
            return self._request("POST", "/pages", json=payload)
        except RuntimeError as first_error:
            # Older workspaces/API shapes may still expect database_id.
            payload["parent"] = {"database_id": data_source_id}
            try:
                return self._request("POST", "/pages", json=payload)
            except RuntimeError:
                raise first_error

    def update_page(self, page_id: str, properties: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PATCH", f"/pages/{page_id}", json={"properties": properties})


# -----------------------------------------------------------------------------
# Data source discovery and schema helpers
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


def discover_data_source(client: NotionClient, name: str) -> Dict[str, Any]:
    progress(f"Finding data source: {name}")
    results = client.search_data_sources_by_title(name)
    if not results:
        raise RuntimeError(
            f"Could not find data source titled '{name}'. "
            "Make sure the source is shared with the integration."
        )

    chosen = choose_best_data_source(name, results)
    chosen_title = safe_text(chosen.get("title")) or safe_text(chosen.get("name")) or name
    full = client.retrieve_data_source(chosen["id"])
    full["title_text"] = chosen_title
    return full


def data_source_properties(data_source: Dict[str, Any]) -> Dict[str, Any]:
    props = data_source.get("properties", {})
    if not props:
        raise RuntimeError(
            f"Data source {data_source.get('id')} has no properties in API response."
        )
    return props


def title_property_name(properties: Dict[str, Any]) -> str:
    for name, spec in properties.items():
        if spec.get("type") == "title":
            return name
    # Common fallback
    if "Name" in properties:
        return "Name"
    raise RuntimeError("Could not identify title property in data source schema.")


def property_spec(properties: Dict[str, Any], prop_name: str) -> Optional[Dict[str, Any]]:
    if prop_name in properties:
        return properties[prop_name]
    # Case-insensitive fallback
    wanted = prop_name.casefold()
    for name, spec in properties.items():
        if name.casefold() == wanted:
            return spec
    return None


def actual_property_name(properties: Dict[str, Any], prop_name: str) -> Optional[str]:
    if prop_name in properties:
        return prop_name
    wanted = prop_name.casefold()
    for name in properties.keys():
        if name.casefold() == wanted:
            return name
    return None


def build_title_index(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        title = page_title_from_properties(row.get("properties", {}))
        if title:
            index[normalize_title(title)] = row
    return index


# -----------------------------------------------------------------------------
# Property payload construction
# -----------------------------------------------------------------------------

def make_property_payload(
    prop_name: str,
    raw_value: str,
    prop_spec: Dict[str, Any],
    relation_lookup: Dict[str, Dict[str, Any]],
    unmatched_relations: List[str],
) -> Optional[Dict[str, Any]]:
    ptype = prop_spec.get("type")
    raw_value = raw_value.strip()

    if not raw_value:
        return None

    if ptype == "title":
        return {"title": rich_text_chunks(raw_value)}

    if ptype == "rich_text":
        return {"rich_text": rich_text_chunks(raw_value)}

    if ptype == "url":
        url = first_listish(raw_value)
        return {"url": url or None}

    if ptype == "select":
        value = first_listish(raw_value)
        return {"select": {"name": value}} if value else None

    if ptype == "multi_select":
        items = split_listish(raw_value)
        return {"multi_select": [{"name": item} for item in items if item]}

    if ptype == "status":
        value = first_listish(raw_value)
        return {"status": {"name": value}} if value else None

    if ptype == "relation":
        relation_items = []
        for item in split_listish(raw_value):
            found = relation_lookup.get(normalize_title(item))
            if found:
                relation_items.append({"id": found["id"]})
            else:
                unmatched_relations.append(item)
        return {"relation": relation_items}

    if ptype == "checkbox":
        lowered = first_listish(raw_value).casefold()
        return {"checkbox": lowered in {"true", "yes", "y", "1", "checked"}}

    if ptype == "number":
        first = first_listish(raw_value)
        try:
            return {"number": float(first)}
        except ValueError:
            warn(f"Could not parse number for {prop_name}: {first!r}; skipping")
            return None

    # Skip formula, rollup, created_time, etc.
    warn(f"Unsupported or read-only property type for {prop_name}: {ptype}; skipping")
    return None


def build_evidence_properties(
    entry: EvidenceEntry,
    evidence_props_schema: Dict[str, Any],
    core_claim_lookup: Dict[str, Dict[str, Any]],
) -> Tuple[Dict[str, Any], List[str], List[str]]:
    payload: Dict[str, Any] = {}
    unmatched_relations: List[str] = []
    skipped_fields: List[str] = []

    title_prop = title_property_name(evidence_props_schema)
    payload[title_prop] = {"title": rich_text_chunks(entry.title)}

    for raw_prop_name, raw_value in entry.fields.items():
        prop_name = actual_property_name(evidence_props_schema, raw_prop_name)
        if not prop_name:
            skipped_fields.append(raw_prop_name)
            continue

        spec = property_spec(evidence_props_schema, prop_name)
        if not spec:
            skipped_fields.append(raw_prop_name)
            continue

        relation_lookup = core_claim_lookup if prop_name.casefold() in {
            "core claims", "linked core claims"
        } else {}

        built = make_property_payload(
            prop_name=prop_name,
            raw_value=raw_value,
            prop_spec=spec,
            relation_lookup=relation_lookup,
            unmatched_relations=unmatched_relations,
        )
        if built is not None:
            payload[prop_name] = built

    return payload, unmatched_relations, skipped_fields


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="infile", required=True, help="Markdown file containing evidence entries")
    parser.add_argument("--evidence-db", default="evidence", help="Evidence data source title")
    parser.add_argument("--core-claims-db", default="core claims", help="Core Claims data source title")
    parser.add_argument("--dry-run", action="store_true", help="Parse and resolve only; do not write")
    parser.add_argument("--update", action="store_true", help="Update existing Evidence rows with matching title")
    parser.add_argument("--limit", type=int, default=None, help="Import only first N entries")
    parser.add_argument("--verbose", action="store_true", help="Print full property payloads")
    args = parser.parse_args()

    token = os.environ.get("NOTION_TOKEN")
    if not token:
        die("Missing NOTION_TOKEN environment variable.")

    infile = os.path.abspath(args.infile)
    if not os.path.exists(infile):
        die(f"Input file not found: {infile}")

    markdown = open(infile, "r", encoding="utf-8").read()
    entries = parse_evidence_markdown(markdown)
    if args.limit is not None:
        entries = entries[: args.limit]

    if not entries:
        die("No evidence entries found. Expected headings like: ### Evidence title")

    progress(f"Parsed {len(entries)} evidence entries from {infile}")

    client = NotionClient(token)

    evidence_ds = discover_data_source(client, args.evidence_db)
    core_claims_ds = discover_data_source(client, args.core_claims_db)

    evidence_props_schema = data_source_properties(evidence_ds)
    progress(f"Evidence data source: {evidence_ds.get('title_text', args.evidence_db)} ({evidence_ds['id']})")
    progress(f"Core Claims data source: {core_claims_ds.get('title_text', args.core_claims_db)} ({core_claims_ds['id']})")

    progress("Loading existing Evidence rows for duplicate detection")
    existing_evidence = build_title_index(client.query_data_source_all(evidence_ds["id"]))

    progress("Loading Core Claims rows for relation resolution")
    core_claim_lookup = build_title_index(client.query_data_source_all(core_claims_ds["id"]))

    created = 0
    updated = 0
    skipped = 0
    had_warnings = False

    for i, entry in enumerate(entries, start=1):
        progress(f"[{i}/{len(entries)}] {entry.title}")
        existing = existing_evidence.get(normalize_title(entry.title))

        if existing and not args.update:
            skipped += 1
            progress(f"Skipping existing entry: {entry.title}")
            continue

        payload, unmatched, skipped_fields = build_evidence_properties(
            entry=entry,
            evidence_props_schema=evidence_props_schema,
            core_claim_lookup=core_claim_lookup,
        )

        if unmatched:
            had_warnings = True
            warn(f"{entry.title}: unmatched Core Claims: {', '.join(unmatched)}")

        if skipped_fields:
            had_warnings = True
            warn(f"{entry.title}: skipped unknown/unsupported fields: {', '.join(skipped_fields)}")

        if args.verbose or args.dry_run:
            print(f"\n--- {entry.title} ---")
            print(f"Action: {'update' if existing and args.update else 'create' if not existing else 'skip'}")
            print(f"Properties: {', '.join(payload.keys())}")
            if unmatched:
                print(f"Unmatched Core Claims: {', '.join(unmatched)}")
            if skipped_fields:
                print(f"Skipped fields: {', '.join(skipped_fields)}")

        if args.dry_run:
            continue

        if existing and args.update:
            client.update_page(existing["id"], payload)
            updated += 1
        else:
            page = client.create_page(evidence_ds["id"], payload)
            existing_evidence[normalize_title(entry.title)] = page
            created += 1

    if args.dry_run:
        print(f"Dry run complete: parsed={len(entries)}, existing_skipped={skipped}, warnings={had_warnings}")
    else:
        print(f"Import complete: created={created}, updated={updated}, skipped={skipped}, warnings={had_warnings}")


if __name__ == "__main__":
    main()
