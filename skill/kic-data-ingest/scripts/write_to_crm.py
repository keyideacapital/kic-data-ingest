#!/usr/bin/env python3
"""
kic_ingest/write_to_crm.py — Airtable writer for the KIC ingest engine.

Consumes JSONL produced by engine.py and writes to the KIC CRM Airtable base
with tier-appropriate routing:

  high     -> Organisations / Contacts  (upsert, auto-apply)
  medium   -> Ingest Review Queue       (status=pending_review)
  low      -> Ingest Manual Bucket      (status=manual)

Idempotency:
  Every record carries an `external_id` = sha256(match_key)[:24]. Re-running
  the writer against the same JSONL finds existing rows by external_id and
  updates them rather than creating duplicates.

Contact -> Org FK resolution:
  After orgs are upserted, the writer holds a name_normalised -> airtable_id
  lookup in memory. Contacts resolve their `org_id` from this map before write.
  Unresolved contacts land in the review queue flagged 'unresolved_org'.

Usage:
    export AIRTABLE_API_KEY=pat...
    export AIRTABLE_BASE_ID=app...
    python scripts/write_to_crm.py out/ --mode dry-run
    python scripts/write_to_crm.py out/ --mode plan
    python scripts/write_to_crm.py out/ --mode apply

Modes:
  dry-run  Read JSONL, compute all writes, print summary. No network calls.
  plan     Fetch existing Airtable records, compute create vs update diff,
           print actions. No writes.
  apply    plan + actually perform the writes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib import request as urlrequest, parse as urlparse_mod, error as urlerror

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

AIRTABLE_API_BASE = "https://api.airtable.com/v0"
BATCH_SIZE = 10                  # Airtable's hard limit for batch create/update
REQ_PER_SEC = 4                  # Conservative under the 5/sec limit
MIN_REQ_INTERVAL = 1.0 / REQ_PER_SEC

# Airtable table names — adjust via env if the KIC base uses different names.
TABLE_ORGS = os.environ.get("KIC_TABLE_ORGS", "Organisations")
TABLE_CONTACTS = os.environ.get("KIC_TABLE_CONTACTS", "Contacts")
TABLE_REVIEW_QUEUE = os.environ.get("KIC_TABLE_REVIEW", "Ingest Review Queue")
TABLE_MANUAL_BUCKET = os.environ.get("KIC_TABLE_MANUAL", "Ingest Manual Bucket")
TABLE_DOCUMENTS = os.environ.get("KIC_TABLE_DOCUMENTS", "Documents")

# Fields that exist on Organisations / Contacts in the Airtable base. Values are
# the Airtable field names — these can differ from the canonical JSONL field
# names (e.g. Airtable might use "Organisation Name" instead of "org_name").
# The map is applied at write-time; keys not in the map are passed through.
ORG_FIELD_MAP = {
    "org_name": "Organisation Name",
    "org_name_normalised": "Name (normalised)",
    "org_type": "Type",
    "website": "Website",
    "website_domain": "Website Domain",
    "linkedin_company_url": "LinkedIn URL",
    "hq_city": "HQ City",
    "hq_country": "HQ Country",
    "offices": "Offices",
    "phone": "Phone",
    "general_email": "General Email",
    "address": "Address",
    "sector": "Sector",
    "stage_focus": "Stage Focus",
    "aum": "AUM",
    "licence_type": "Licence Type",
    "activity_type": "Activity Type",
    "notes": "Notes",
    "source_ref": "Source Ref",
    "source_region": "Source Region",
    "ingestion_confidence": "Ingestion Confidence",
    "ingestion_tier": "Ingestion Tier",
    "external_id": "External ID",
}

CONTACT_FIELD_MAP = {
    "first_name": "First Name",
    "middle_name": "Middle Name",
    "last_name": "Last Name",
    "full_name": "Full Name",
    "full_name_normalised": "Name (normalised)",
    "title": "Title",
    "job_title": "Job Title",
    "org_name": "Organisation Name",  # text cache; true link below
    "org_id": "Organisation",          # Airtable link field
    "email": "Email",
    "email_alt": "Email (Alt)",
    "phone_business": "Phone (Business)",
    "phone_mobile": "Phone (Mobile)",
    "phone_other": "Phone (Other)",
    "linkedin_profile_url": "LinkedIn URL",
    "address": "Address",
    "city": "City",
    "country": "Country",
    "notes": "Notes",
    "source_ref": "Source Ref",
    "source_region": "Source Region",
    "ingestion_confidence": "Ingestion Confidence",
    "ingestion_tier": "Ingestion Tier",
    "external_id": "External ID",
}

# Fields dropped at write-time — computed client-side, not stored in Airtable.
ORG_FIELDS_DROP = set()
CONTACT_FIELDS_DROP = set()


# ---------------------------------------------------------------------------
# Rate-limited HTTP client
# ---------------------------------------------------------------------------

class AirtableClient:
    def __init__(self, api_key: str, base_id: str, *, dry: bool = False):
        self.api_key = api_key
        self.base_id = base_id
        self.dry = dry
        self._last_request_ts = 0.0
        # Counters the caller can inspect after a run.
        self.requests_made = 0
        self.records_created = 0
        self.records_updated = 0

    def _url(self, table: str, suffix: str = "") -> str:
        table_enc = urlparse_mod.quote(table, safe="")
        return f"{AIRTABLE_API_BASE}/{self.base_id}/{table_enc}{suffix}"

    def _throttle(self) -> None:
        dt = time.monotonic() - self._last_request_ts
        if dt < MIN_REQ_INTERVAL:
            time.sleep(MIN_REQ_INTERVAL - dt)
        self._last_request_ts = time.monotonic()

    def _request(self, method: str, url: str, body: dict | None = None) -> dict:
        if self.dry:
            # Return a synthetic response — caller must handle dry mode explicitly
            # for correctness; this is a safety net.
            return {"records": [], "_dry": True}
        self._throttle()
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urlrequest.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urlrequest.urlopen(req, timeout=60) as resp:
                self.requests_made += 1
                return json.loads(resp.read().decode("utf-8"))
        except urlerror.HTTPError as exc:
            # Surface the Airtable error body for diagnosis.
            body_text = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Airtable HTTP {exc.code} on {method} {url}: {body_text}") from exc

    def list_all_records(self, table: str, *, fields: list[str] | None = None) -> list[dict]:
        """Page through every record in `table`, returning Airtable raw record dicts.
        Used during --plan and --apply to build external_id -> record_id lookups."""
        if self.dry:
            return []
        records: list[dict] = []
        offset: str | None = None
        while True:
            params: dict[str, Any] = {"pageSize": 100}
            if offset:
                params["offset"] = offset
            if fields:
                # Airtable accepts repeated fields[] params; urlencode with doseq handles this.
                params["fields[]"] = fields
            url = self._url(table, "?" + urlparse_mod.urlencode(params, doseq=True))
            resp = self._request("GET", url)
            records.extend(resp.get("records", []))
            offset = resp.get("offset")
            if not offset:
                break
        return records

    def batch_create(self, table: str, records: list[dict]) -> list[dict]:
        created: list[dict] = []
        for i in range(0, len(records), BATCH_SIZE):
            chunk = records[i:i + BATCH_SIZE]
            body = {"records": [{"fields": r} for r in chunk], "typecast": True}
            resp = self._request("POST", self._url(table), body)
            created.extend(resp.get("records", []))
            self.records_created += len(resp.get("records", []))
        return created

    def batch_update(self, table: str, updates: list[tuple[str, dict]]) -> list[dict]:
        """updates = [(record_id, fields_dict), ...]"""
        updated: list[dict] = []
        for i in range(0, len(updates), BATCH_SIZE):
            chunk = updates[i:i + BATCH_SIZE]
            body = {
                "records": [{"id": rid, "fields": f} for rid, f in chunk],
                "typecast": True,
            }
            resp = self._request("PATCH", self._url(table), body)
            updated.extend(resp.get("records", []))
            self.records_updated += len(resp.get("records", []))
        return updated


# ---------------------------------------------------------------------------
# Match-key and external_id derivation
# ---------------------------------------------------------------------------

def _sha256_short(s: str, length: int = 24) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:length]


def org_match_key(org: dict) -> str:
    """Stable match key for an org. Prefers domain (strongest signal) over name."""
    dom = (org.get("website_domain") or "").strip().lower()
    if dom:
        return f"dom::{dom}"
    name_norm = (org.get("org_name_normalised") or "").strip().lower()
    if name_norm:
        return f"name::{name_norm}"
    # Fallback to source_ref when nothing else is available — unique but won't
    # match across sources, which is correct: nothing to match on.
    return f"src::{org.get('source_ref', '')}"


def contact_match_key(contact: dict) -> str:
    """Stable match key for a contact. Email > (name+org)."""
    email = (contact.get("email") or "").strip().lower()
    if email:
        return f"email::{email}"
    name = (contact.get("full_name_normalised") or "").strip().lower()
    org = (contact.get("org_name") or "").strip().lower()
    if name:
        return f"nameorg::{name}::{org}"
    return f"src::{contact.get('source_ref', '')}"


def with_external_id(record: dict, kind: str) -> dict:
    """Return a copy of `record` with `external_id` set to a deterministic hash."""
    if kind == "org":
        key = org_match_key(record)
    elif kind == "contact":
        key = contact_match_key(record)
    else:
        key = record.get("source_ref", "")
    out = dict(record)
    out["external_id"] = _sha256_short(f"{kind}:{key}")
    return out


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------

@dataclass
class Bundle:
    orgs: list[dict] = field(default_factory=list)
    contacts: list[dict] = field(default_factory=list)
    documents: list[dict] = field(default_factory=list)


def load_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def _merge_by_external_id(records: list[dict], kind: str) -> list[dict]:
    """Collapse records sharing the same external_id before write.

    When the same org/contact appears in multiple source files (e.g. a firm in
    both the SG MAS export AND the LinkedIn export), the engine emits it once
    per file because dedup only runs intra-file. Cross-file dedup happens here
    at write-time. We keep the highest-confidence record as the base and
    fill empty fields from the others; source_ref is concatenated with '|' to
    preserve provenance across all appearances.
    """
    by_eid: dict[str, dict] = {}
    for r in records:
        eid = r["external_id"]
        existing = by_eid.get(eid)
        if existing is None:
            by_eid[eid] = dict(r)
            continue
        winner, loser = (existing, r) if existing.get("ingestion_confidence", 0) >= r.get("ingestion_confidence", 0) else (r, existing)
        merged = dict(winner)
        for k, v in loser.items():
            if k in ("ingestion_confidence", "ingestion_tier", "source_ref", "source_region", "external_id"):
                continue
            if v and not merged.get(k):
                merged[k] = v
        # Union source refs across all appearances.
        w_src = winner.get("source_ref", "")
        l_src = loser.get("source_ref", "")
        if l_src and l_src not in w_src:
            merged["source_ref"] = f"{w_src}|{l_src}" if w_src else l_src
        # If tiers differ, promote to the better tier — a record that hit high
        # in file A and medium in file B is high overall.
        tier_rank = {"high": 3, "medium": 2, "low": 1}
        w_tier = winner.get("ingestion_tier", "low")
        l_tier = loser.get("ingestion_tier", "low")
        merged["ingestion_tier"] = w_tier if tier_rank.get(w_tier, 0) >= tier_rank.get(l_tier, 0) else l_tier
        merged["ingestion_confidence"] = max(winner.get("ingestion_confidence", 0),
                                             loser.get("ingestion_confidence", 0))
        merged["external_id"] = eid
        by_eid[eid] = merged
    return list(by_eid.values())


def load_bundle(out_dir: Path) -> Bundle:
    """Load every *.organisations.jsonl / *.contacts.jsonl / *.documents.jsonl
    in `out_dir`, tag each record with external_id, and collapse cross-file
    duplicates so the writer sees one row per unique entity."""
    b = Bundle()
    raw_orgs: list[dict] = []
    raw_contacts: list[dict] = []
    for p in sorted(out_dir.glob("*.organisations.jsonl")):
        for r in load_jsonl(p):
            raw_orgs.append(with_external_id(r, "org"))
    for p in sorted(out_dir.glob("*.contacts.jsonl")):
        for r in load_jsonl(p):
            raw_contacts.append(with_external_id(r, "contact"))
    for p in sorted(out_dir.glob("*.documents.jsonl")):
        b.documents.extend(load_jsonl(p))
    b.orgs = _merge_by_external_id(raw_orgs, "org")
    b.contacts = _merge_by_external_id(raw_contacts, "contact")
    # Stash raw counts for the summary printout.
    b.raw_orgs_count = len(raw_orgs)        # type: ignore[attr-defined]
    b.raw_contacts_count = len(raw_contacts)  # type: ignore[attr-defined]
    return b


# ---------------------------------------------------------------------------
# Tier routing
# ---------------------------------------------------------------------------

@dataclass
class RoutingPlan:
    orgs_auto: list[dict] = field(default_factory=list)
    orgs_review: list[dict] = field(default_factory=list)
    orgs_manual: list[dict] = field(default_factory=list)
    contacts_auto: list[dict] = field(default_factory=list)
    contacts_review: list[dict] = field(default_factory=list)
    contacts_manual: list[dict] = field(default_factory=list)

    def summarise(self) -> dict:
        return {
            "orgs_auto": len(self.orgs_auto),
            "orgs_review": len(self.orgs_review),
            "orgs_manual": len(self.orgs_manual),
            "contacts_auto": len(self.contacts_auto),
            "contacts_review": len(self.contacts_review),
            "contacts_manual": len(self.contacts_manual),
        }


def route_by_tier(bundle: Bundle) -> RoutingPlan:
    plan = RoutingPlan()
    for o in bundle.orgs:
        tier = o.get("ingestion_tier", "low")
        if tier == "high":
            plan.orgs_auto.append(o)
        elif tier == "medium":
            plan.orgs_review.append(o)
        else:
            plan.orgs_manual.append(o)
    for c in bundle.contacts:
        tier = c.get("ingestion_tier", "low")
        if tier == "high":
            plan.contacts_auto.append(c)
        elif tier == "medium":
            plan.contacts_review.append(c)
        else:
            plan.contacts_manual.append(c)
    return plan


# ---------------------------------------------------------------------------
# Field mapping for Airtable writes
# ---------------------------------------------------------------------------

def _project(record: dict, field_map: dict[str, str], drop: set[str]) -> dict:
    """Rename canonical keys to Airtable field names; drop anything unknown or
    explicitly dropped. Returns a dict ready for Airtable POST/PATCH."""
    out: dict[str, Any] = {}
    for canonical, atfield in field_map.items():
        if canonical in drop:
            continue
        if canonical in record and record[canonical] not in (None, ""):
            out[atfield] = record[canonical]
    return out


def _project_for_queue(record: dict, kind: str, status: str,
                       unresolved_reason: str | None = None) -> dict:
    """Flatten any canonical record into a review/manual queue row. Queue tables
    store the full canonical JSON in a Long Text field so reviewers can see all
    context, plus the highest-signal fields promoted for at-a-glance display."""
    out: dict[str, Any] = {
        "Kind": "Organisation" if kind == "org" else "Contact",
        "Status": status,
        "Source Ref": record.get("source_ref", ""),
        "Ingestion Confidence": record.get("ingestion_confidence", 0.0),
        "Ingestion Tier": record.get("ingestion_tier", ""),
        "External ID": record.get("external_id", ""),
        "Payload JSON": json.dumps(record, ensure_ascii=False),
    }
    # Surface the most useful human-readable fields for grid display.
    if kind == "org":
        out["Display Name"] = record.get("org_name", "")
        out["Website"] = record.get("website", "")
    else:
        out["Display Name"] = record.get("full_name", "") or record.get("email", "")
        out["Email"] = record.get("email", "")
        out["Organisation Name"] = record.get("org_name", "")
    if unresolved_reason:
        out["Flag"] = unresolved_reason
    return out


# ---------------------------------------------------------------------------
# Upsert engine
# ---------------------------------------------------------------------------

@dataclass
class UpsertResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    # external_id -> airtable record id (for FK resolution downstream)
    id_by_external: dict[str, str] = field(default_factory=dict)
    # normalised_name -> airtable record id (for contact->org FK resolution)
    id_by_name_norm: dict[str, str] = field(default_factory=dict)


def _fields_equal(existing: dict, incoming: dict) -> bool:
    """True if the Airtable record's existing fields already match the incoming
    projection (so we can skip the PATCH). Missing fields in `existing` that are
    None/empty in `incoming` count as equal."""
    for k, v in incoming.items():
        ev = existing.get(k)
        if v in ("", None) and ev in ("", None):
            continue
        if ev != v:
            return False
    return True


def upsert_by_external_id(
    client: AirtableClient,
    table: str,
    records: list[dict],
    field_map: dict[str, str],
    drop: set[str],
    *,
    dry: bool = False,
    progress_label: str = "",
) -> UpsertResult:
    """Upsert `records` into `table` keyed on External ID.

    Strategy:
      1. Page through the table once, building external_id -> (record_id, fields) map.
      2. For each incoming record, project to Airtable shape.
      3. If external_id is already present and the projected payload equals the
         stored fields, skip. Otherwise PATCH.
      4. If external_id is not present, batch CREATE.
    """
    result = UpsertResult()
    existing_by_eid: dict[str, tuple[str, dict]] = {}
    if not dry:
        needed_fields = list(set(field_map.values()))
        existing_records = client.list_all_records(table, fields=needed_fields)
        for rec in existing_records:
            flds = rec.get("fields", {})
            eid = flds.get(field_map.get("external_id", "External ID"))
            if eid:
                existing_by_eid[eid] = (rec["id"], flds)

    to_create: list[dict] = []
    to_update: list[tuple[str, dict]] = []
    incoming_projections: dict[str, dict] = {}   # external_id -> projected fields

    for rec in records:
        eid = rec["external_id"]
        projected = _project(rec, field_map, drop)
        projected[field_map["external_id"]] = eid
        incoming_projections[eid] = projected

        if eid in existing_by_eid:
            rid, existing_fields = existing_by_eid[eid]
            if _fields_equal(existing_fields, projected):
                result.unchanged += 1
            else:
                to_update.append((rid, projected))
            result.id_by_external[eid] = rid
        else:
            to_create.append(projected)

    if dry:
        result.created = len(to_create)
        result.updated = len(to_update)
        return result

    print(f"   [{progress_label}] create={len(to_create)} update={len(to_update)} "
          f"unchanged={result.unchanged}")
    if to_create:
        created = client.batch_create(table, to_create)
        result.created = len(created)
        # Capture newly-minted record ids so contact-linking can find them.
        for rec in created:
            flds = rec.get("fields", {})
            eid = flds.get(field_map.get("external_id", "External ID"))
            if eid:
                result.id_by_external[eid] = rec["id"]
    if to_update:
        client.batch_update(table, to_update)
        result.updated = len(to_update)
    return result


def upsert_queue(
    client: AirtableClient,
    table: str,
    records: list[dict],
    kind: str,
    status: str,
    *,
    dry: bool = False,
    unresolved_eids: set[str] | None = None,
    unresolved_reason: str = "unresolved_org",
    progress_label: str = "",
) -> UpsertResult:
    """Write records to a review/manual queue table. External ID still drives
    idempotency so re-runs don't pile up dupes in the queue."""
    result = UpsertResult()
    field_map = {
        "external_id": "External ID",
        "source_ref": "Source Ref",
        "ingestion_confidence": "Ingestion Confidence",
        "ingestion_tier": "Ingestion Tier",
    }

    existing_by_eid: dict[str, tuple[str, dict]] = {}
    if not dry:
        existing_records = client.list_all_records(
            table,
            fields=["External ID", "Status", "Kind", "Display Name", "Payload JSON", "Flag"],
        )
        for rec in existing_records:
            eid = rec.get("fields", {}).get("External ID")
            if eid:
                existing_by_eid[eid] = (rec["id"], rec.get("fields", {}))

    to_create: list[dict] = []
    to_update: list[tuple[str, dict]] = []
    for rec in records:
        eid = rec["external_id"]
        flag = unresolved_reason if unresolved_eids and eid in unresolved_eids else None
        payload = _project_for_queue(rec, kind, status, unresolved_reason=flag)

        if eid in existing_by_eid:
            rid, existing_fields = existing_by_eid[eid]
            if _fields_equal(existing_fields, payload):
                result.unchanged += 1
            else:
                to_update.append((rid, payload))
        else:
            to_create.append(payload)

    if dry:
        result.created = len(to_create)
        result.updated = len(to_update)
        return result

    print(f"   [{progress_label}] queue create={len(to_create)} update={len(to_update)} "
          f"unchanged={result.unchanged}")
    if to_create:
        client.batch_create(table, to_create)
        result.created = len(to_create)
    if to_update:
        client.batch_update(table, to_update)
        result.updated = len(to_update)
    return result


# ---------------------------------------------------------------------------
# Contact -> Org FK resolution
# ---------------------------------------------------------------------------

def build_name_index(
    client: AirtableClient,
    org_upsert: UpsertResult,
    *,
    dry: bool = False,
) -> dict[str, str]:
    """Build normalised_name -> airtable_record_id lookup used to resolve
    Contact.org_name -> Contact.org_id link field."""
    index: dict[str, str] = {}
    if dry:
        return index
    # Pull the Organisations table with just Name (normalised) + External ID.
    existing = client.list_all_records(
        TABLE_ORGS,
        fields=[ORG_FIELD_MAP["org_name_normalised"], ORG_FIELD_MAP["external_id"]],
    )
    for rec in existing:
        name_norm = rec.get("fields", {}).get(ORG_FIELD_MAP["org_name_normalised"])
        if name_norm:
            index[name_norm.strip().lower()] = rec["id"]
    return index


def _normalise_for_lookup(s: str) -> str:
    # Mirrors engine.normalise_org_name's final form: lowercase, stripped.
    return (s or "").strip().lower()


def resolve_contact_org_fks(
    contacts: list[dict],
    name_index: dict[str, str],
) -> tuple[list[dict], set[str]]:
    """Return (contacts_with_org_id_set, set_of_unresolved_external_ids).

    For high-tier contacts, we MUST resolve org FK to auto-apply. If we can't,
    the contact is demoted to the review queue with flag=unresolved_org.
    """
    resolved: list[dict] = []
    unresolved_eids: set[str] = set()
    for c in contacts:
        org_name_norm = _normalise_for_lookup(c.get("org_name", ""))
        # Engine's normalise_org_name strips suffixes, so try both.
        rid = name_index.get(org_name_norm)
        if not rid:
            # Try after stripping common org suffixes — mirrors engine normaliser roughly.
            stripped = " ".join(t for t in org_name_norm.split()
                                if t not in {"pte", "ltd", "limited", "inc", "llc", "corp"})
            rid = name_index.get(stripped)
        c_out = dict(c)
        if rid:
            # Airtable link fields expect a LIST of record IDs.
            c_out["org_id"] = [rid]
        else:
            unresolved_eids.add(c["external_id"])
        resolved.append(c_out)
    return resolved, unresolved_eids


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def run(out_dir: Path, mode: str, *, api_key: str | None, base_id: str | None) -> int:
    bundle = load_bundle(out_dir)
    plan = route_by_tier(bundle)

    raw_orgs = getattr(bundle, "raw_orgs_count", len(bundle.orgs))
    raw_contacts = getattr(bundle, "raw_contacts_count", len(bundle.contacts))
    print(f"Loaded {raw_orgs} org rows, {raw_contacts} contact rows from {out_dir}")
    print(f"After cross-file merge: {len(bundle.orgs)} orgs, {len(bundle.contacts)} contacts "
          f"(collapsed {raw_orgs - len(bundle.orgs)} orgs, {raw_contacts - len(bundle.contacts)} contacts)")
    print(f"Routing plan: {plan.summarise()}")

    if mode == "dry-run":
        print("\n[dry-run] No network calls. Sample payloads:")
        if plan.orgs_auto:
            sample = _project(plan.orgs_auto[0], ORG_FIELD_MAP, ORG_FIELDS_DROP)
            sample[ORG_FIELD_MAP["external_id"]] = plan.orgs_auto[0]["external_id"]
            print("  org auto-apply sample:")
            print("   ", json.dumps(sample, ensure_ascii=False, indent=2)[:600])
        if plan.contacts_review:
            print("  contact review-queue sample:")
            print("   ", json.dumps(_project_for_queue(plan.contacts_review[0], "contact", "pending_review"),
                                    ensure_ascii=False, indent=2)[:600])
        return 0

    if not api_key or not base_id:
        print("ERROR: --mode plan/apply needs AIRTABLE_API_KEY and AIRTABLE_BASE_ID env vars.",
              file=sys.stderr)
        return 2

    client = AirtableClient(api_key, base_id, dry=False)

    # -- Stage 1: upsert high-tier orgs to Organisations. --
    print("\n→ Stage 1: Organisations (high tier)")
    org_result = upsert_by_external_id(
        client, TABLE_ORGS, plan.orgs_auto, ORG_FIELD_MAP, ORG_FIELDS_DROP,
        dry=(mode == "plan"), progress_label="orgs",
    )
    print(f"   created={org_result.created} updated={org_result.updated} "
          f"unchanged={org_result.unchanged}")

    # -- Stage 2: build name -> record_id index for contact FK resolution. --
    print("\n→ Stage 2: Build Organisation name index for FK resolution")
    name_index = build_name_index(client, org_result, dry=(mode == "plan"))
    print(f"   name index size: {len(name_index)}")

    # -- Stage 3: resolve contact -> org FKs. --
    contacts_auto_linked, auto_unresolved = resolve_contact_org_fks(plan.contacts_auto, name_index)
    if auto_unresolved:
        # High-tier contacts whose org couldn't be resolved get demoted to review.
        print(f"   {len(auto_unresolved)} high-tier contacts have unresolved orgs -> review queue")
        demoted = [c for c in plan.contacts_auto if c["external_id"] in auto_unresolved]
        plan.contacts_auto = [c for c in contacts_auto_linked if c["external_id"] not in auto_unresolved]
        plan.contacts_review = plan.contacts_review + demoted
    else:
        plan.contacts_auto = contacts_auto_linked

    # -- Stage 4: upsert high-tier contacts. --
    print("\n→ Stage 3: Contacts (high tier, org-resolved)")
    contact_result = upsert_by_external_id(
        client, TABLE_CONTACTS, plan.contacts_auto, CONTACT_FIELD_MAP, CONTACT_FIELDS_DROP,
        dry=(mode == "plan"), progress_label="contacts",
    )
    print(f"   created={contact_result.created} updated={contact_result.updated} "
          f"unchanged={contact_result.unchanged}")

    # -- Stage 5: medium-tier orgs + contacts -> review queue. --
    print("\n→ Stage 4: Review queue (medium tier)")
    upsert_queue(
        client, TABLE_REVIEW_QUEUE, plan.orgs_review, "org", "pending_review",
        dry=(mode == "plan"), progress_label="orgs-review",
    )
    upsert_queue(
        client, TABLE_REVIEW_QUEUE, plan.contacts_review, "contact", "pending_review",
        dry=(mode == "plan"), progress_label="contacts-review",
        unresolved_eids=auto_unresolved,
    )

    # -- Stage 6: low-tier -> manual bucket. --
    print("\n→ Stage 5: Manual bucket (low tier)")
    upsert_queue(
        client, TABLE_MANUAL_BUCKET, plan.orgs_manual, "org", "manual",
        dry=(mode == "plan"), progress_label="orgs-manual",
    )
    upsert_queue(
        client, TABLE_MANUAL_BUCKET, plan.contacts_manual, "contact", "manual",
        dry=(mode == "plan"), progress_label="contacts-manual",
    )

    # -- Stage 7: Documents provenance --
    print("\n→ Stage 6: Documents")
    doc_field_map = {
        "document_id": "Document ID",
        "filename": "Filename",
        "file_type": "File Type",
        "source_path": "Source Path",
        "region_count": "Region Count",
        "row_count": "Row Count",
        "encoding": "Encoding",
        "ingested_at": "Ingested At",
        "source_ref": "Source Ref",
        "external_id": "External ID",
    }
    docs_with_eid = [{**d, "external_id": _sha256_short("doc:" + d.get("document_id", ""))}
                     for d in bundle.documents]
    upsert_by_external_id(
        client, TABLE_DOCUMENTS, docs_with_eid, doc_field_map, set(),
        dry=(mode == "plan"), progress_label="documents",
    )

    print(f"\n✓ {mode} complete. Total Airtable requests: {client.requests_made}")
    if mode == "apply":
        print(f"  Created {client.records_created} records, updated {client.records_updated}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="KIC Ingest Engine → Airtable writer")
    ap.add_argument("out_dir", help="Engine output directory (contains *.jsonl files)")
    ap.add_argument("--mode", choices=["dry-run", "plan", "apply"], default="dry-run",
                    help="dry-run: no network; plan: read Airtable, no writes; apply: write.")
    ap.add_argument("--api-key", default=os.environ.get("AIRTABLE_API_KEY"))
    ap.add_argument("--base-id", default=os.environ.get("AIRTABLE_BASE_ID"))
    args = ap.parse_args(argv)
    return run(Path(args.out_dir), args.mode, api_key=args.api_key, base_id=args.base_id)


if __name__ == "__main__":
    sys.exit(main())
