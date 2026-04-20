# CLAUDE.md — project ingestion pattern

This file tells Claude Code how this project integrates with the **kic-data-ingest** skill. Any KIC project (the Deal Management System, DeckShare, AI Assistant Hub, Linen & Laundry, DrinkyCrow, Thred, or any future project) can drop this file into its root to get spreadsheet-to-CRM ingestion available as a standard workflow.

---

## Skill reference

This project uses the `kic-data-ingest` skill. It lives at one of:
- `~/.claude/skills/kic-data-ingest/` (user-level install)
- `./skills/kic-data-ingest/` (project-level vendored copy)
- `/opt/agent-swarm/skills/kic-data-ingest/` (production swarm)

The skill bundle contains:
```
kic-data-ingest/
├── SKILL.md
├── scripts/
│   ├── engine.py          # CSV/XLSX → canonical JSONL
│   ├── enrich.py          # URL-only records → enriched records (needs ANTHROPIC_API_KEY)
│   └── write_to_crm.py    # JSONL → Airtable (needs AIRTABLE_API_KEY + AIRTABLE_BASE_ID)
└── references/
    ├── canonical_schema.json
    └── field_synonyms.json
```

## When to invoke it in this project

Claude Code should reach for this skill whenever the user:
- Drops one or more `.csv`/`.xlsx`/`.xlsm` files into the repo and wants them in the CRM
- Says "ingest this", "add these to Airtable", "clean up this spreadsheet", "parse this investor list", "import these contacts", or similar phrasing — **even if the word "skill" is never mentioned**
- Asks to refresh an existing dataset from a newer export
- Asks about the ingest review queue, manual bucket, or the `/ingest-review` UI

Do NOT reach for it when:
- The user is asking about record shape in Airtable (that's a schema question — point them at `references/canonical_schema.json`)
- The user is manually editing an Airtable record in the app (that's out of this skill's scope)

## Standard workflow in this project

```bash
# 1. Run the ingest (always starts here)
python skills/kic-data-ingest/scripts/engine.py INPUT_FILES... --out .ingest-out/

# 2. If the engine report shows URL-only records, enrich them
python skills/kic-data-ingest/scripts/enrich.py .ingest-out/ --max-records 50 --cost-cap 1.00

# 3. Dry-run the writer, show routing counts to the user
python skills/kic-data-ingest/scripts/write_to_crm.py .ingest-out/ --mode dry-run

# 4. If the user confirms, apply
python skills/kic-data-ingest/scripts/write_to_crm.py .ingest-out/ --mode apply
```

## Environment

This project expects the following env vars for CRM operations. They should be set in whatever secrets mechanism this project uses (Wrangler secrets for Cloudflare Workers, `.env` for local dev, the swarm's injection for production).

```
AIRTABLE_API_KEY=pat...
AIRTABLE_BASE_ID=app...
ANTHROPIC_API_KEY=sk-ant-...           # only needed for enrichment

# Optional per-project Airtable table-name overrides:
KIC_TABLE_ORGS="Organisations"
KIC_TABLE_CONTACTS="Contacts"
KIC_TABLE_REVIEW="Ingest Review Queue"
KIC_TABLE_MANUAL="Ingest Manual Bucket"
KIC_TABLE_DOCUMENTS="Documents"
```

Default table names match the KIC CRM base. If this project uses a different base with different table names, override via env.

## Conventions for this project

- **Output directory**: always `.ingest-out/` at the repo root. Gitignored. Do not commit.
- **Input staging**: the user can drop source files anywhere — if they land in `./inbox/`, that's fine; if they're elsewhere, pass absolute paths to `engine.py`. Do NOT move source files without permission.
- **Source-ref preservation**: every record carries a `source_ref` trail. Never strip or rewrite it.
- **Cost awareness**: before running `enrich.py`, estimate candidate count from the engine report's `orgs_by_tier.low` for URL-dump regions. Tell the user the expected cost (~$0.01/record) before burning tokens.
- **Safety rail**: the writer has three modes (`dry-run` / `plan` / `apply`). ALWAYS run `dry-run` or `plan` first and show the user the routing counts before you run `apply`. A single `apply` can create thousands of records — this is the moment where destructive mistakes happen.

## Idempotency

The pipeline is safe to re-run on the same inputs. Every record carries an `external_id = sha256(match_key)` so Airtable upserts hit the same row rather than creating duplicates. If the user re-uploads a corrected version of the same source file, re-running will update affected records in place.

The one scenario this doesn't cover cleanly: if a reviewer manually promoted a record from the Manual Bucket to Organisations by hand, and a later engine run re-ingests that same record, the engine's output will correctly find the Organisations row (by external_id) and update it — but the Manual Bucket row will remain as a stale pointer. Clean this up in `/ingest-review` if it becomes a problem.

## Review & promotion

Medium-tier records land in the review queue at `system.keyideacapital.com/ingest-review`. This is shared across all KIC projects — reviewers handle queue items from any source from one interface. If a project adds custom fields to Organisations/Contacts, add them to the `ORG_FIELD_MAP` / `CONTACT_FIELD_MAP` in `write_to_crm.py` so the writer knows how to project them.

## Extending for this project

If this project's source files introduce new column-name patterns, update `skills/kic-data-ingest/references/field_synonyms.json`. Run `engine.py --out` on a representative file, check the `report.json.unmapped_headers` array, and add synonyms until the list empties out. Commit the updated synonym map back to the skill repo so other projects benefit.

If this project's source files have a specific XLSX layout (e.g. master + subset sheets like the MAS FID export), add an entry to `KNOWN_SUPERSETS_XLSX` in `engine.py` rather than post-processing externally.

## Agent-swarm integration

If this project's data files land on disk programmatically (webhook, scheduled export, email attachment processor), route them into `/var/lib/kic-swarm/ingest-inbox/` and the swarm's `kic-ingest-module` will pick them up within 5 minutes. No per-project runner needed.

## Deliverables checklist

When Claude Code finishes an ingest run in this project, it should hand back:
1. Count of records created / updated in Organisations and Contacts
2. Count of records routed to the review queue (with a link to `/ingest-review`)
3. Count in the manual bucket (with a note about what they need)
4. Location of the engine JSONL output (for audit)
5. The `enrichment_report.json` if enrichment ran
6. An estimate of total Airtable requests consumed (for rate-limit awareness)
