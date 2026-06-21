# Notion Research Tooling

Small command-line tools for exporting, importing, and updating the PhD research
Notion workspace.

The programs are executable scripts with `nix-shell` shebangs. Run them directly
from this directory; they install/use their Python and `requests` dependency via
Nix.

## Setup

All tools authenticate with the Notion API token from the environment:

```bash
export NOTION_TOKEN="secret_xxx"
```

The Notion integration must have access to the relevant data sources. Most
commands discover data sources by title, using names such as:

```text
research questions
contributions
core claims
evidence
risks
mitigations
```

Progress logs are written to stderr with prefixes such as `[progress]`,
`[warn]`, and `[error]`.

## `./notion_export`

Exports Notion data sources to Markdown.

Typical full export:

```bash
./notion_export \
  --out output.md \
  --include-body \
  "research questions" \
  "contributions" \
  "core claims" \
  "evidence" \
  "risks" \
  "mitigations"
```

Basic usage:

```bash
./notion_export [options] data_source_names...
```

Options:

```text
--out FILE            Output Markdown file. Defaults to output.md.
--include-body        Include page body blocks.
--verbose             Include noisy metadata fields.
--include-untitled    Include untitled pages.
```

Notes:

- Data source lookup is by title.
- Relation values are rendered as related page titles where possible.
- Risks are rendered grouped under linked research questions.
- The exporter is read-only.

## `./notion_importer`

Imports Evidence entries from Markdown into the Evidence data source and resolves
Core Claim relations by title.

Dry run:

```bash
./notion_importer \
  --in evidence_to_import.md \
  --evidence-db "evidence" \
  --core-claims-db "core claims" \
  --dry-run
```

Create missing evidence rows, skipping existing titles:

```bash
./notion_importer --in evidence_to_import.md
```

Update existing evidence rows with matching titles:

```bash
./notion_importer --in evidence_to_import.md --update
```

Options:

```text
--in FILE                 Markdown file containing evidence entries. Required.
--evidence-db NAME        Evidence data source title. Defaults to evidence.
--core-claims-db NAME     Core Claims data source title. Defaults to core claims.
--dry-run                 Parse and resolve only; do not write.
--update                  Update existing Evidence rows with matching title.
--limit N                 Import only the first N entries.
--verbose                 Print full property payload summaries.
```

Expected Markdown shape:

```markdown
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
```

Recognised labels are mapped onto Notion properties where possible, including
`Evidence`/`Description`, `Linked core claims`/`Core Claims`, `Status`,
`Notes / extraction`, `URL`, `Source Kind`, `Domain`, `Use in thesis`,
`Apparatus Role`, `Overall source quality`, `Type`, and `Scope`.

Importer behavior:

- Matching is title-based.
- Existing Evidence rows are skipped unless `--update` is passed.
- Core Claim relations are resolved by Core Claim page title.
- Unknown fields and unresolved relations are reported as warnings.

## `./notion_updater`

Inspects and mutates the research database. It was added for the June 2026
research-question migration and is written to be idempotent: repeated plan/apply
runs should not create duplicate pages or duplicate notes.

Help:

```bash
./notion_updater --help
```

Plan the RQ migration without writing:

```bash
./notion_updater plan-rq-migration
```

Apply the RQ migration:

```bash
./notion_updater apply-rq-migration
```

Inspection commands:

```bash
./notion_updater export-schema
./notion_updater list-databases
./notion_updater list-pages --database "research questions"
./notion_updater find-page --title "Working understandings of behaviour"
```

Database override options:

```text
--research-questions-db NAME_OR_ID    Defaults to research questions.
--contributions-db NAME_OR_ID         Defaults to contributions.
--core-claims-db NAME_OR_ID           Defaults to core claims.
--risks-db NAME_OR_ID                 Defaults to risks.
```

Migration behavior:

- Reads the current schemas before planning writes.
- Finds pages by title, with aliases for renamed RQs/contributions.
- Creates missing active RQs/contributions if needed.
- Updates existing pages where found.
- Adds relation values only where the schema has relation properties.
- Updates select/status-like properties only where such properties exist.
- Appends marked page-body notes when exact schema fields are unavailable.
- Does not delete or archive pages.
- Uses this marker for script-added notes:

```text
[notion_updater migration 2026-06]
```

At the end of plan/apply it prints:

- pages created
- pages updated
- relations touched
- notes added
- properties missing/skipped
- warnings/manual follow-ups

Recommended live-update workflow:

```bash
./notion_export --out before.md --include-body \
  "research questions" "contributions" "core claims" "evidence" "risks" "mitigations"

./notion_updater plan-rq-migration
./notion_updater apply-rq-migration

./notion_export --out after.md --include-body \
  "research questions" "contributions" "core claims" "evidence" "risks" "mitigations"

./notion_updater plan-rq-migration
./notion_updater plan-rq-migration
```

The final two plan runs should report no page mutations needed, apart from
stable schema/manual follow-up warnings.

## Safety Notes

- These scripts operate on the live Notion workspace.
- Prefer `--dry-run` or `plan-rq-migration` before writes.
- Keep Markdown exports before large migrations.
- The tools use title-based matching; avoid creating pages with duplicate titles
  in the same data source.
- Schema gaps are reported rather than treated as fatal where possible.
