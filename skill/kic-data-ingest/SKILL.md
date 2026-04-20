---
name: kic-data-ingest
description: Ingest messy CSV/XLSX contact-and-company spreadsheets into the KIC CRM (Airtable). Use this skill whenever the user has ONE OR MORE .csv/.xlsx/.xlsm files containing contacts, companies, investors, funds, deal flow, CRM exports, MAS/HKMA/ASIC regulator lists, LinkedIn exports, or any tabular data intended for the KIC CRM — even if they phrase it casually (e.g. "import this list", "add these to the CRM", "clean up this spreadsheet and put it in Airtable", "these investors need to go in the system", "parse this VC list"). The skill produces confidence-tiered canonical records (high → auto-apply, medium → review queue, low → manual bucket) with full source-row traceability. Always use this skill when the destination is the KIC CRM and the source is a spreadsheet, regardless of shape, encoding, or quality. Also use it for follow-up tasks like re-running after the reviewer has processed the queue, or updating already-ingested records from a refreshed source file.
---

# KIC Data Ingest

Convert any tabular file (CSV or XLSX, any encoding, any shape) into confidence-tiered canonical records ready for the KIC CRM in Airtable, with full source-row traceability.

## When to reach for this skill

- User provides one or more `.csv`/`.xlsx`/`.xlsm` files and mentions the KIC CRM, Airtable, Organisations, Contacts, or any equivalent ingestion intent
- User exports a contact list (LinkedIn, Outlook, HubSpot, Salesforce), a regulator list (MAS, HKMA, ASIC, FCA), an investor list (VCs, family offices, LPs), or deal flow spreadsheets and wants them in the CRM
- User has previously ingested data and wants to re-run after reviewer cleanup, or ingest a refreshed version of the same source
- User describes a file by its messiness ("this has multiple tables", "it's broken", "header is missing", "mixed columns") — this skill handles multi-region detection, encoding detection, and field fuzzy-matching out of the box

## What it does

Pipeline: **load → profile → extract → resolve → score → emit → write**

1. **Encoding detection** — tries utf-8, cp1252, latin-1 in order; no user config needed
2. **Multi-region detection** — a single sheet can contain multiple tabular regions plus URL-dump regions plus noise; each region gets its own column mapping
3. **Fuzzy column mapping** — ~135 synonyms across 25+ canonical fields, `rapidfuzz` token-sort ratio with a 82 threshold
4. **Canonical schema** — records conform to the KIC Airtable shape: `Organisations`, `Contacts`, `Interactions`, `Documents`
5. **Per-record confidence scoring** — weighted signal strength × column-mapping quality, producing three tiers:
   - `high` ≥ 0.85 → auto-apply to Organisations/Contacts
   - `medium` 0.60–0.85 → review queue
   - `low` < 0.60 → manual bucket
6. **Intra- and inter-file dedup** — match on website domain first (strongest signal), then normalised name; merges keep highest-confidence record and union all source_refs into a `|`-joined provenance trail
7. **Airtable writer** — tier-aware, idempotent via deterministic `external_id = sha256(match_key)`, batch-writes at 4 req/sec

## Quickstart

```bash
# Full pipeline: ingest, enrich URL-only records, then write to Airtable
python scripts/engine.py INPUT1.csv INPUT2.xlsx --out out/
python scripts/enrich.py out/ --max-records 50       # optional enrichment pass
python scripts/write_to_crm.py out/ --mode apply
```

Environment variables required:
```
AIRTABLE_API_KEY=pat...                # for writer
AIRTABLE_BASE_ID=app...
ANTHROPIC_API_KEY=sk-ant-...           # for enrich
# Optional table-name overrides:
KIC_TABLE_ORGS="Organisations"
KIC_TABLE_CONTACTS="Contacts"
KIC_TABLE_REVIEW="Ingest Review Queue"
KIC_TABLE_MANUAL="Ingest Manual Bucket"
KIC_TABLE_DOCUMENTS="Documents"
```

## Typical workflow Claude Code should follow

When invoked, walk through these steps in order. Do not skip the dry-run.

1. **Inventory**. List the input files the user has provided. Confirm back to the user: "I see N files: [list]. I'll ingest all of them into the KIC CRM."

2. **Engine run**. Run `engine.py` against all input files at once:
   ```bash
   python scripts/engine.py [files...] --out out/
   ```
   Surface the per-file extraction counts and tier histograms from stdout. If any file produces zero records, investigate the `*.report.json` to see what went wrong (unmapped headers are the usual culprit).

3. **Dry-run the writer**. Before any network call:
   ```bash
   python scripts/write_to_crm.py out/ --mode dry-run
   ```
   This surfaces the routing plan: how many records will auto-apply, how many will hit the review queue, how many land in manual bucket. Share these numbers with the user.

4. **Enrichment pass (recommended for files with URL-only records)**. If the engine report shows URL-dump regions or a large number of low-tier records with websites, run the enrichment agent:
   ```bash
   python scripts/enrich.py out/ --max-records 50 --cost-cap 1.00
   ```
   This fetches each URL-only org's homepage, extracts real org fields + named contacts via Claude, and promotes records to the appropriate tier based on source-site type (official sites can reach high; directories/aggregators cap at medium). The writer then picks up the `*.enriched.jsonl` files automatically.

   Dry-run first if you want to see counts before burning tokens:
   ```bash
   python scripts/enrich.py out/ --dry-run
   ```

5. **Confirm scale with user**. If the auto-apply count is above, say, 500 records, or if the user hasn't run this skill before on this Airtable base, **pause and confirm** before the apply step. This is the last moment before records land in live Organisations/Contacts.

6. **Plan mode** (optional but recommended for first-time or large runs):
   ```bash
   python scripts/write_to_crm.py out/ --mode plan
   ```
   This fetches existing Airtable records, computes the create/update diff, and prints what would change. No writes happen. Useful for verifying idempotency on reruns.

7. **Apply**:
   ```bash
   python scripts/write_to_crm.py out/ --mode apply
   ```
   At ~4 Airtable req/sec this takes roughly 2–5 minutes per 1,000 records (depending on how many need create vs update).

8. **Hand off**. Tell the user:
   - How many records auto-applied
   - How many landed in the review queue (point them to `/ingest-review` in the Deal Management system)
   - How many need manual handling
   - Where the provenance lives (every record's `Source Ref` field)

## File layout

```
kic-data-ingest/
├── SKILL.md                          (this file)
├── scripts/
│   ├── engine.py                     Pipeline: extract + score + emit JSONL
│   └── write_to_crm.py               Airtable writer: dry-run/plan/apply
└── references/
    ├── canonical_schema.json         Target Airtable shape
    └── field_synonyms.json           Column-name synonym map
```

## Extending

### Adding a new column synonym

The synonym map at `references/field_synonyms.json` is a plain lookup: canonical field → list of observed raw headers. To handle a new source file whose columns aren't recognised, open the engine's report (`out/<stem>.report.json`), find the `unmapped_headers` array, and add entries to the relevant canonical key. The match is fuzzy, so near-variants are already caught — only add genuinely new patterns.

### Adding a new known-superset sheet rule

If an xlsx file has a master sheet that duplicates per-sector sheets (like the MAS FID export), add a regex to `KNOWN_SUPERSETS_XLSX` in `engine.py`. The existing entry for MAS FID is a pattern to copy.

### Adjusting tier thresholds

Constants at the top of `engine.py`: `TIER_HIGH_MIN = 0.85`, `TIER_MEDIUM_MIN = 0.60`. The scoring weights are in `_score_org` and `_score_contact`. Any change should be validated against the `out/*.report.json` tier histograms before being considered an improvement.

### Adding a new source format

Extend `load_any()` in `engine.py` with a new loader that yields `(region_name, rows_as_strings, encoding)`. The rest of the pipeline is format-agnostic.

## Provenance and audit trail

Every record the engine emits carries:
- `source_ref` — `filename#sheet!row=N` pointing to the exact source cell
- `source_region` — named region within the file (`main#region-1`, `Banking#region-1`, etc.)
- `ingestion_confidence` — the raw 0.0–1.0 score
- `ingestion_tier` — `high`/`medium`/`low`

When cross-file dedup merges records, `source_ref` is concatenated with `|` to preserve the full chain: `linkedin.csv!row=47|singapore.xlsx#FID!row=3921`. Never strip this — reviewers rely on it to trace a record back to its origin and to spot-check ingestion quality.

## Known limitations (v1)

- When one org has multiple regulatory licences (UBS AG has 55 MAS registrations), the merge keeps a single `licence_type`. A v2 would promote this to an `OrgLicences` child table. The `source_ref` chain lets us re-expand when that table exists.
- Contacts whose org lives in the review queue (not yet in Organisations) can't auto-link at apply time and get demoted to the review queue themselves. The reviewer promotes the org first, then the contact's next pass resolves the FK. To mitigate, `/ingest-review` supports bulk promoting an org and all its pending contacts together.
- The writer is intra-base idempotent via `external_id`, but it doesn't yet cross-check the Manual Bucket when deciding whether a medium-tier record is new. If a reviewer manually promotes a Manual Bucket row to Organisations, a subsequent engine run will still upsert to Organisations correctly (because external_id matches), but the Manual Bucket row remains as a dead pointer. Clean up in `/ingest-review`.

## Integration with agent-swarm

This skill is also drop-in compatible with the `keyideacapital/agent-swarm` cron runner. See `../agent-swarm/kic-ingest-module/` for the swarm wrapper that watches a configurable `watch_dir` and runs the engine+writer whenever a new file lands.
