#!/usr/bin/env python3
"""
kic-enrich-module/runner.py — Agent-swarm wrapper around the enrich.py script.

Runs on its own cron (typically after kic-ingest-module finishes). Watches the
ingest module's output_dir for URL-only organisations and runs enrichment in
bounded batches so cost stays predictable.

Typical cron:
    ingest runs every 5 min; enrich runs every 30 min with a 50-record cap.
    That caps enrichment to ~2,400 records/day at ~$0.01 each = $24/day worst case.

Layout expected:
    agent-swarm/
    ├── modules/
    │   ├── kic-ingest-module/   (files-in → canonical records)
    │   └── kic-enrich-module/   (this module — URL-only records → enriched records)
    └── skills/
        └── kic-data-ingest/
            └── scripts/enrich.py
"""
from __future__ import annotations

import argparse
import json
import os
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


@dataclass
class Config:
    out_dir: Path             # same as ingest module's output_dir
    skill_dir: Path           # where kic-data-ingest lives
    max_records_per_run: int
    cost_cap_usd: float | None
    anthropic_api_key: str | None
    notify_webhook: str | None

    @classmethod
    def load(cls, path: Path) -> "Config":
        raw: dict[str, Any] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if not line or ":" not in line:
                    continue
                k, _, v = line.partition(":")
                raw[k.strip()] = v.strip().strip('"').strip("'")

        def _p(k: str, default: str | None = None) -> str | None:
            return os.environ.get(k.upper(), raw.get(k, default))

        cost_raw = _p("cost_cap_usd", "")
        return cls(
            out_dir=Path(_p("out_dir", "/var/lib/kic-swarm/ingest-out") or ""),
            skill_dir=Path(_p("skill_dir", "/opt/agent-swarm/skills/kic-data-ingest") or ""),
            max_records_per_run=int(_p("max_records_per_run", "50") or 50),
            cost_cap_usd=float(cost_raw) if cost_raw else None,
            anthropic_api_key=_p("anthropic_api_key", None),
            notify_webhook=_p("notify_webhook", None),
        )


def notify(cfg: Config, message: str, *, level: str = "info") -> None:
    print(f"[{level.upper()}] {message}", flush=True)
    if not cfg.notify_webhook:
        return
    try:
        from urllib import request as urlreq
        body = json.dumps({"text": f"[kic-enrich] {message}", "level": level}).encode()
        req = urlreq.Request(cfg.notify_webhook, data=body,
                             headers={"Content-Type": "application/json"})
        urlreq.urlopen(req, timeout=10).read()
    except Exception as exc:
        print(f"[WARN] notify webhook failed: {exc}", file=sys.stderr)


def count_candidates(out_dir: Path) -> int:
    """Fast pre-check: how many URL-only records are waiting? Helps decide
    whether to skip a run entirely."""
    from urllib.parse import urlparse as _up
    count = 0
    for p in out_dir.glob("*.organisations.jsonl"):
        if ".enriched." in p.name:
            continue
        for line in p.read_text().splitlines():
            if not line.strip(): continue
            rec = json.loads(line)
            name = rec.get("org_name", "")
            website = rec.get("website", "")
            if website and ("." in name and name.count(" ") == 0):
                count += 1
    return count


def run(cfg: Config) -> int:
    if not cfg.out_dir.exists():
        notify(cfg, f"out_dir does not exist: {cfg.out_dir}", level="error")
        return 2
    script = cfg.skill_dir / "scripts" / "enrich.py"
    if not script.exists():
        notify(cfg, f"enrich.py not found at {script}", level="error")
        return 2
    if not cfg.anthropic_api_key:
        notify(cfg, "ANTHROPIC_API_KEY not configured", level="error")
        return 2

    candidates = count_candidates(cfg.out_dir)
    if candidates == 0:
        # Quiet no-op — expected when no new URL-dump files have landed.
        print("No URL-only records to enrich.")
        return 0

    notify(cfg, f"Starting enrichment: {candidates} URL-only candidates, "
                f"max {cfg.max_records_per_run} this run")

    cmd = [sys.executable, str(script), str(cfg.out_dir),
           "--max-records", str(cfg.max_records_per_run)]
    if cfg.cost_cap_usd is not None:
        cmd += ["--cost-cap", str(cfg.cost_cap_usd)]
    env = {**os.environ, "ANTHROPIC_API_KEY": cfg.anthropic_api_key}

    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600, env=env)
    except subprocess.TimeoutExpired:
        notify(cfg, "enrich.py timed out (>1h)", level="error")
        return 1
    duration = time.time() - t0

    if proc.returncode != 0:
        notify(cfg, f"enrich.py exited {proc.returncode}: {proc.stderr[:500]}", level="error")
        return proc.returncode

    # Parse the report for summary
    report_path = cfg.out_dir / "enrichment_report.json"
    summary = ""
    if report_path.exists():
        report = json.loads(report_path.read_text())
        summary = (
            f"attempted {report['attempted_this_run']}, "
            f"enriched {report['enriched_successfully']}, "
            f"new contacts {report['new_contacts_created']}, "
            f"cost ~${report['estimated_cost_usd']:.2f}, "
            f"{duration:.1f}s"
        )
    notify(cfg, f"Enrichment run complete: {summary}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="KIC enrichment module for agent-swarm")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args(argv)
    cfg = Config.load(Path(args.config))
    if args.dry:
        candidates = count_candidates(cfg.out_dir)
        print(f"Dry: {candidates} URL-only candidates waiting in {cfg.out_dir}")
        return 0
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main())
