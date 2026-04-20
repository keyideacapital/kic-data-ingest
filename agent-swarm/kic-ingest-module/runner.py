#!/usr/bin/env python3
"""
kic-ingest-module/runner.py — Agent-swarm wrapper around the kic-data-ingest skill.

Designed as a drop-in module for the keyideacapital/agent-swarm orchestrator.
The swarm cron invokes this runner; the runner checks the configured watch_dir
for unseen files, runs the engine + writer, and reports via the swarm's
notification interface.

Layout expected:
    agent-swarm/
    ├── modules/
    │   └── kic-ingest-module/
    │       ├── runner.py          (this file)
    │       ├── config.yaml
    │       └── state.json         (created at first run; tracks processed files)
    └── skills/
        └── kic-data-ingest/       (the .skill bundle, symlinked or copied in)
            ├── SKILL.md
            ├── scripts/
            └── references/

The runner exits 0 on success (including "nothing to do"). Any non-zero exit
surfaces as a swarm alert.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = MODULE_DIR / "config.yaml"
DEFAULT_STATE_PATH = MODULE_DIR / "state.json"
DEFAULT_SKILL_DIR = MODULE_DIR.parent.parent / "skills" / "kic-data-ingest"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Config:
    watch_dir: Path
    processed_archive_dir: Path
    output_dir: Path
    skill_dir: Path
    writer_mode: str                # dry-run | plan | apply
    airtable_api_key: str | None
    airtable_base_id: str | None
    notify_webhook: str | None      # Slack / generic POST webhook
    file_types: list[str]           # [".csv", ".xlsx", ".xlsm"]
    max_files_per_run: int
    require_stable_seconds: int     # don't process files modified <N seconds ago (still uploading)

    @classmethod
    def load(cls, path: Path) -> "Config":
        """Dead-simple YAML subset loader: `key: value` one per line, comments with `#`.
        The swarm runtime ships PyYAML, but we keep a zero-dep fallback so this
        module runs standalone during dev."""
        raw: dict[str, Any] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if not line or ":" not in line:
                    continue
                k, _, v = line.partition(":")
                raw[k.strip()] = v.strip().strip('"').strip("'")

        def _p(key: str, default: str | None = None) -> str | None:
            # ENV overrides config file; both are overridable by the same name.
            return os.environ.get(key.upper(), raw.get(key, default))

        return cls(
            watch_dir=Path(_p("watch_dir", str(MODULE_DIR / "inbox"))),
            processed_archive_dir=Path(_p("processed_archive_dir", str(MODULE_DIR / "archive"))),
            output_dir=Path(_p("output_dir", str(MODULE_DIR / "out"))),
            skill_dir=Path(_p("skill_dir", str(DEFAULT_SKILL_DIR))),
            writer_mode=_p("writer_mode", "apply") or "apply",
            airtable_api_key=_p("airtable_api_key", None),
            airtable_base_id=_p("airtable_base_id", None),
            notify_webhook=_p("notify_webhook", None),
            file_types=[x.strip() for x in (_p("file_types", ".csv,.xlsx,.xlsm") or "").split(",") if x.strip()],
            max_files_per_run=int(_p("max_files_per_run", "10") or 10),
            require_stable_seconds=int(_p("require_stable_seconds", "30") or 30),
        )


# ---------------------------------------------------------------------------
# State (processed-files ledger)
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"runs": [], "processed": {}}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        # Corrupted — start fresh but preserve the broken file for post-mortem.
        backup = path.with_suffix(f".bad.{int(time.time())}.json")
        path.rename(backup)
        return {"runs": [], "processed": {}}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write — avoid a half-flushed ledger if the swarm kills us mid-save.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    tmp.replace(path)


def file_fingerprint(path: Path) -> str:
    """sha256 of file contents + size. Re-uploading the same file with the
    same content is a no-op; a different version of the same filename is a
    new fingerprint and gets reprocessed."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return f"{h.hexdigest()[:32]}-{path.stat().st_size}"


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    path: Path
    fingerprint: str
    size: int
    mtime: float


def discover_files(cfg: Config, state: dict[str, Any]) -> list[Candidate]:
    """List files in watch_dir that (a) match the configured extensions,
    (b) haven't been seen before by fingerprint, and (c) have been stable
    on disk for at least `require_stable_seconds`."""
    if not cfg.watch_dir.exists():
        return []
    now = time.time()
    processed = state.get("processed", {})
    out: list[Candidate] = []
    for p in sorted(cfg.watch_dir.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() not in cfg.file_types:
            continue
        stat = p.stat()
        # Stability check — skip files still being uploaded.
        if now - stat.st_mtime < cfg.require_stable_seconds:
            continue
        fp = file_fingerprint(p)
        if fp in processed:
            continue
        out.append(Candidate(path=p, fingerprint=fp, size=stat.st_size, mtime=stat.st_mtime))
    # Cap per run so a 1,000-file dump doesn't monopolise the swarm.
    return out[: cfg.max_files_per_run]


# ---------------------------------------------------------------------------
# Engine + writer invocation
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    file: str
    fingerprint: str
    ok: bool
    engine_report: dict[str, Any] | None
    writer_stdout: str
    duration_sec: float
    error: str | None = None


def run_engine(cfg: Config, candidate: Candidate) -> tuple[bool, dict[str, Any] | None, str]:
    """Invoke the engine as a subprocess. Returns (ok, report_json, stdout)."""
    engine_script = cfg.skill_dir / "scripts" / "engine.py"
    if not engine_script.exists():
        return False, None, f"engine missing at {engine_script}"

    cmd = [sys.executable, str(engine_script), str(candidate.path),
           "--out", str(cfg.output_dir)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired as exc:
        return False, None, f"engine timed out: {exc}"
    if proc.returncode != 0:
        return False, None, f"engine exit {proc.returncode}: {proc.stderr}"

    # Locate the report for this specific file.
    stem = candidate.path.stem
    report_path = cfg.output_dir / f"{stem}.report.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else None
    return True, report, proc.stdout


def run_writer(cfg: Config) -> tuple[bool, str, str]:
    """Invoke the writer over the full output dir. Returns (ok, stdout, err)."""
    writer_script = cfg.skill_dir / "scripts" / "write_to_crm.py"
    if not writer_script.exists():
        return False, "", f"writer missing at {writer_script}"
    env = {**os.environ}
    if cfg.airtable_api_key: env["AIRTABLE_API_KEY"] = cfg.airtable_api_key
    if cfg.airtable_base_id: env["AIRTABLE_BASE_ID"] = cfg.airtable_base_id
    cmd = [sys.executable, str(writer_script), str(cfg.output_dir), "--mode", cfg.writer_mode]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=env)
    except subprocess.TimeoutExpired as exc:
        return False, "", f"writer timed out: {exc}"
    if proc.returncode != 0:
        return False, proc.stdout, f"writer exit {proc.returncode}: {proc.stderr}"
    return True, proc.stdout, ""


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def notify(cfg: Config, message: str, *, level: str = "info") -> None:
    """POST to notify_webhook if configured. Slack-compatible payload shape.
    Falls back to stdout so swarm log capture still gets the summary."""
    print(f"[{level.upper()}] {message}", file=sys.stdout, flush=True)
    if not cfg.notify_webhook:
        return
    try:
        from urllib import request as urlreq
        body = json.dumps({"text": f"[kic-ingest] {message}", "level": level}).encode()
        req = urlreq.Request(cfg.notify_webhook, data=body,
                             headers={"Content-Type": "application/json"})
        urlreq.urlopen(req, timeout=10).read()
    except Exception as exc:
        print(f"[WARN] notify webhook failed: {exc}", file=sys.stderr)


def summarise(results: list[RunResult]) -> str:
    if not results:
        return "No new files."
    lines = []
    total_orgs_high = total_orgs_med = total_orgs_low = 0
    total_ct_high = total_ct_med = total_ct_low = 0
    for r in results:
        if not r.ok:
            lines.append(f"  ❌ {r.file}: {r.error}")
            continue
        rep = r.engine_report or {}
        o = rep.get("orgs_by_tier", {}) or {}
        c = rep.get("contacts_by_tier", {}) or {}
        total_orgs_high += o.get("high", 0)
        total_orgs_med  += o.get("medium", 0)
        total_orgs_low  += o.get("low", 0)
        total_ct_high   += c.get("high", 0)
        total_ct_med    += c.get("medium", 0)
        total_ct_low    += c.get("low", 0)
        lines.append(
            f"  ✓ {r.file}: orgs={rep.get('orgs_total', 0)} "
            f"(H{o.get('high',0)}/M{o.get('medium',0)}/L{o.get('low',0)})  "
            f"contacts={rep.get('contacts_total', 0)} "
            f"(H{c.get('high',0)}/M{c.get('medium',0)}/L{c.get('low',0)})  "
            f"{r.duration_sec:.1f}s"
        )
    header = (
        f"Processed {len(results)} file(s). "
        f"Totals: orgs H{total_orgs_high}/M{total_orgs_med}/L{total_orgs_low}, "
        f"contacts H{total_ct_high}/M{total_ct_med}/L{total_ct_low}."
    )
    return header + "\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------

def archive_file(cfg: Config, candidate: Candidate) -> None:
    """Move processed file into archive_dir, namespaced by date for sanity."""
    date_dir = cfg.processed_archive_dir / datetime.now(timezone.utc).strftime("%Y-%m")
    date_dir.mkdir(parents=True, exist_ok=True)
    # Name collision avoidance: append a short fingerprint suffix.
    target = date_dir / f"{candidate.path.stem}__{candidate.fingerprint[:8]}{candidate.path.suffix}"
    candidate.path.rename(target)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(cfg_path: Path, state_path: Path, *, dry: bool = False) -> int:
    cfg = Config.load(cfg_path)
    state = load_state(state_path)

    # Ensure directories exist.
    cfg.watch_dir.mkdir(parents=True, exist_ok=True)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    cfg.processed_archive_dir.mkdir(parents=True, exist_ok=True)

    # Preflight: verify skill bundle is findable.
    if not (cfg.skill_dir / "scripts" / "engine.py").exists():
        msg = f"skill bundle not found at {cfg.skill_dir}"
        notify(cfg, msg, level="error")
        return 2

    # Preflight for writer: if mode=apply or plan, both Airtable creds are required.
    if cfg.writer_mode in ("apply", "plan") and not (cfg.airtable_api_key and cfg.airtable_base_id):
        msg = "writer_mode requires AIRTABLE_API_KEY + AIRTABLE_BASE_ID"
        notify(cfg, msg, level="error")
        return 2

    candidates = discover_files(cfg, state)
    if not candidates:
        # Quiet no-op — don't spam the notification channel when cron fires and
        # the inbox is empty. Return 0 so swarm logs it as green.
        print("No new files to process.")
        return 0

    notify(cfg, f"Found {len(candidates)} new file(s); starting ingest...")
    results: list[RunResult] = []

    for cand in candidates:
        t0 = time.time()
        if dry:
            results.append(RunResult(
                file=cand.path.name, fingerprint=cand.fingerprint,
                ok=True, engine_report={"orgs_total": 0, "contacts_total": 0,
                                         "orgs_by_tier": {}, "contacts_by_tier": {}},
                writer_stdout="(dry)", duration_sec=0.0,
            ))
            continue
        ok, report, engine_out = run_engine(cfg, cand)
        duration = time.time() - t0
        if not ok:
            results.append(RunResult(cand.path.name, cand.fingerprint, False, None,
                                     "", duration, error=engine_out))
            continue
        results.append(RunResult(cand.path.name, cand.fingerprint, True,
                                 report, engine_out, duration))

    # Only run writer if at least one engine run succeeded and we're not dry.
    if not dry and any(r.ok for r in results):
        notify(cfg, f"Running writer in mode={cfg.writer_mode}...")
        w_ok, w_out, w_err = run_writer(cfg)
        if not w_ok:
            notify(cfg, f"Writer failed: {w_err}", level="error")
        else:
            # The writer prints routing + counts; extract the last 10 lines as summary.
            tail = "\n".join(w_out.strip().splitlines()[-12:])
            notify(cfg, f"Writer complete:\n{tail}")

    # Archive successful files + record in state.
    for r in results:
        if not r.ok:
            continue
        cand = next((c for c in candidates if c.fingerprint == r.fingerprint), None)
        if cand:
            if not dry:
                archive_file(cfg, cand)
            state["processed"][r.fingerprint] = {
                "filename": r.file,
                "processed_at": datetime.now(timezone.utc).isoformat(),
                "orgs_total":     (r.engine_report or {}).get("orgs_total", 0),
                "contacts_total": (r.engine_report or {}).get("contacts_total", 0),
            }

    # Append run record to state history (cap at 500 runs so this file doesn't grow forever).
    state.setdefault("runs", []).append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "files": [r.file for r in results],
        "success_count": sum(1 for r in results if r.ok),
        "failure_count": sum(1 for r in results if not r.ok),
    })
    state["runs"] = state["runs"][-500:]
    if not dry:
        save_state(state_path, state)

    summary = summarise(results)
    level = "error" if any(not r.ok for r in results) else "info"
    notify(cfg, summary, level=level)
    return 0 if all(r.ok for r in results) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="KIC ingest module for agent-swarm")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    ap.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    ap.add_argument("--dry", action="store_true", help="Dry run — discover files but don't execute")
    args = ap.parse_args(argv)
    return run(Path(args.config), Path(args.state), dry=args.dry)


if __name__ == "__main__":
    sys.exit(main())
