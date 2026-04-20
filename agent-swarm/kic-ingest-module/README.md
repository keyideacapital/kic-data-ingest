# kic-ingest-module (agent-swarm)

Agent-swarm module that watches a directory for new CSV/XLSX files and runs the `kic-data-ingest` skill against each new file. Designed to be dropped into `keyideacapital/agent-swarm` under `modules/kic-ingest-module/`.

## Install

```bash
# From the agent-swarm repo root
mkdir -p modules/kic-ingest-module
cp kic-ingest-module/* modules/kic-ingest-module/

# Install the skill bundle it depends on
mkdir -p skills/kic-data-ingest
cp -r ../kic-data-ingest/* skills/kic-data-ingest/

# Runtime deps (if not already in the swarm's base environment)
pip install rapidfuzz openpyxl
```

## Configure

Edit `modules/kic-ingest-module/config.yaml` to set `watch_dir`, `archive_dir`, and `output_dir`. These must be on persistent disk the swarm has write access to.

Airtable credentials come from the swarm's secrets. Set in whichever secret-injection mechanism the swarm uses:
```
AIRTABLE_API_KEY=pat...
AIRTABLE_BASE_ID=app...
```

Optional Slack webhook for run summaries:
```
NOTIFY_WEBHOOK=https://hooks.slack.com/services/...
```

## Wire into the swarm cron

In the swarm's cron config (format will depend on the swarm's scheduler — systemd-timer / cron / node-schedule, whatever the orchestrator uses):

```
# Every 5 minutes, check the inbox and ingest anything new.
*/5 * * * * cd /opt/agent-swarm && python modules/kic-ingest-module/runner.py
```

Or via the swarm's native module registration (if `agent-swarm` uses a registry pattern):

```yaml
modules:
  - name: kic-ingest
    path: modules/kic-ingest-module/runner.py
    schedule: "*/5 * * * *"
    timeout: 1800   # 30 min hard cap
```

## Behaviour

On each tick:

1. **Scans `watch_dir`** for CSV/XLSX files, filtering out:
   - Files already processed (via sha256+size fingerprint ledger in `state.json`)
   - Files modified <30s ago (still uploading)
   - More than `max_files_per_run` per tick (defaults to 10)

2. **Runs the engine** against each new file into `output_dir`.

3. **Runs the writer** once over the combined output dir. Idempotent, so this is safe even if one file produced records that collide with an earlier run's.

4. **Archives** processed files to `processed_archive_dir/YYYY-MM/` with a fingerprint suffix so repeat uploads of the same filename don't collide.

5. **Notifies** via webhook (if configured) with a per-file breakdown:

   ```
   [kic-ingest] Processed 2 file(s). Totals: orgs H140/M23/L0, contacts H98/M12/L0.
     ✓ deal_flow_jan.xlsx: orgs=45 (H40/M5/L0) contacts=52 (H48/M4/L0) 12.3s
     ✓ lp_contacts_refresh.csv: orgs=118 (H100/M18/L0) contacts=58 (H50/M8/L0) 4.1s
   ```

## Manual operations

```bash
# Dry-run: discover files but don't process them
python runner.py --dry

# Run once, outside the cron
python runner.py

# Reset the processed-files ledger (re-ingest everything in watch_dir)
rm state.json
```

## How the module plays with the swarm's meta-loop

The swarm's self-improving meta-loop can consume the `state.json` history to notice patterns — e.g. "this file type from this sender always produces low-tier records, someone should add field synonyms". The state file is structured JSON (`runs: [...]`, `processed: {fingerprint: {...}}`) specifically so meta-loop agents can read it.

When a file consistently falls into the Manual Bucket with unmapped headers, the meta-loop can:
1. Read the `unmapped_headers` from the relevant `*.report.json`
2. Suggest new synonyms to add to `references/field_synonyms.json`
3. File a PR against the skill repo

Leaving a note for future-self: this is the hook point for making the ingest self-improving. Not implemented in v1.
