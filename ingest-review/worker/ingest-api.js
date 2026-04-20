// ============================================================================
// KIC Ingest Review — /api/ingest/* endpoints
// ============================================================================
//
// Drop this into the existing worker-index.js router. All endpoints expect
// env.AIRTABLE_API_KEY and env.AIRTABLE_BASE_ID to be configured (these are
// the same secrets the existing KIC worker uses).
//
// Endpoints:
//   GET  /api/ingest/queue         — list queued items (paginated, filterable)
//   GET  /api/ingest/stats         — counts by kind/status/source/tier
//   POST /api/ingest/promote       — { external_id, edits? } → move to Orgs/Contacts
//   POST /api/ingest/reject        — { external_id, reason? } → mark rejected
//   POST /api/ingest/edit          — { external_id, fields } → patch queue row
//   POST /api/ingest/bulk-promote  — { external_ids[], edits_by_id? }
//
// Authorization: reuses the worker's existing auth middleware — no special
// handling here. The handler function receives (request, env, ctx, { user }).
//
// Airtable tables used:
//   - Ingest Review Queue  (medium-tier records awaiting review)
//   - Ingest Manual Bucket (low-tier records)
//   - Organisations        (promotion target for orgs)
//   - Contacts             (promotion target for contacts)
//   - Ingest Audit Log     (every promote/reject/edit written here)
// ============================================================================

const T = {
  REVIEW:   "Ingest Review Queue",
  MANUAL:   "Ingest Manual Bucket",
  ORGS:     "Organisations",
  CONTACTS: "Contacts",
  AUDIT:    "Ingest Audit Log",
};

const AIRTABLE_BASE = "https://api.airtable.com/v0";

// ----------------------------------------------------------------------------
// Airtable helper
// ----------------------------------------------------------------------------

async function airtable(env, method, table, pathOrParams = "", body = null) {
  const url = typeof pathOrParams === "string" && pathOrParams.startsWith("?")
    ? `${AIRTABLE_BASE}/${env.AIRTABLE_BASE_ID}/${encodeURIComponent(table)}${pathOrParams}`
    : typeof pathOrParams === "string" && pathOrParams.startsWith("/")
      ? `${AIRTABLE_BASE}/${env.AIRTABLE_BASE_ID}/${encodeURIComponent(table)}${pathOrParams}`
      : `${AIRTABLE_BASE}/${env.AIRTABLE_BASE_ID}/${encodeURIComponent(table)}`;

  const headers = {
    "Authorization": `Bearer ${env.AIRTABLE_API_KEY}`,
  };
  const init = { method, headers };
  if (body !== null) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const resp = await fetch(url, init);
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`Airtable ${method} ${url} → ${resp.status}: ${text}`);
  }
  return resp.json();
}

// Find a queue row by External ID. Checks Review Queue first, then Manual Bucket.
async function findQueueRow(env, externalId) {
  const esc = externalId.replace(/'/g, "\\'");
  const formula = `{External ID}='${esc}'`;
  for (const table of [T.REVIEW, T.MANUAL]) {
    const qs = "?" + new URLSearchParams({
      filterByFormula: formula,
      maxRecords: "1",
    });
    const data = await airtable(env, "GET", table, qs);
    if (data.records && data.records.length > 0) {
      return { table, record: data.records[0] };
    }
  }
  return null;
}

// Check if an External ID already exists in a target table (Organisations/Contacts).
async function existingTargetId(env, table, externalId) {
  const esc = externalId.replace(/'/g, "\\'");
  const qs = "?" + new URLSearchParams({
    filterByFormula: `{External ID}='${esc}'`,
    maxRecords: "1",
  });
  const data = await airtable(env, "GET", table, qs);
  return data.records && data.records.length > 0 ? data.records[0].id : null;
}

// Resolve an organisation's name to a record ID (for contact → org linking).
async function findOrgIdByNormalisedName(env, nameNormalised) {
  if (!nameNormalised) return null;
  const esc = String(nameNormalised).toLowerCase().replace(/'/g, "\\'");
  const qs = "?" + new URLSearchParams({
    filterByFormula: `LOWER({Name (normalised)})='${esc}'`,
    maxRecords: "1",
  });
  const data = await airtable(env, "GET", T.ORGS, qs);
  return data.records && data.records.length > 0 ? data.records[0].id : null;
}

// ----------------------------------------------------------------------------
// Canonical → Airtable field projection
// ----------------------------------------------------------------------------

const ORG_MAP = {
  org_name: "Organisation Name",
  org_name_normalised: "Name (normalised)",
  org_type: "Type",
  website: "Website",
  website_domain: "Website Domain",
  linkedin_company_url: "LinkedIn URL",
  hq_city: "HQ City",
  hq_country: "HQ Country",
  offices: "Offices",
  phone: "Phone",
  general_email: "General Email",
  address: "Address",
  sector: "Sector",
  stage_focus: "Stage Focus",
  aum: "AUM",
  licence_type: "Licence Type",
  activity_type: "Activity Type",
  notes: "Notes",
  source_ref: "Source Ref",
  source_region: "Source Region",
  ingestion_confidence: "Ingestion Confidence",
  ingestion_tier: "Ingestion Tier",
  external_id: "External ID",
};

const CONTACT_MAP = {
  first_name: "First Name",
  middle_name: "Middle Name",
  last_name: "Last Name",
  full_name: "Full Name",
  full_name_normalised: "Name (normalised)",
  title: "Title",
  job_title: "Job Title",
  org_name: "Organisation Name",
  org_id: "Organisation",  // link field — value must be an array of record IDs
  email: "Email",
  email_alt: "Email (Alt)",
  phone_business: "Phone (Business)",
  phone_mobile: "Phone (Mobile)",
  phone_other: "Phone (Other)",
  linkedin_profile_url: "LinkedIn URL",
  address: "Address",
  city: "City",
  country: "Country",
  notes: "Notes",
  source_ref: "Source Ref",
  source_region: "Source Region",
  ingestion_confidence: "Ingestion Confidence",
  ingestion_tier: "Ingestion Tier",
  external_id: "External ID",
};

function project(payload, map) {
  const out = {};
  for (const [k, atField] of Object.entries(map)) {
    const v = payload[k];
    if (v !== undefined && v !== null && v !== "") out[atField] = v;
  }
  return out;
}

// ----------------------------------------------------------------------------
// Audit log
// ----------------------------------------------------------------------------

async function writeAudit(env, { action, externalId, kind, user, reason, beforeJson, afterJson }) {
  const fields = {
    "Action": action,          // promoted | rejected | edited
    "External ID": externalId,
    "Kind": kind,              // Organisation | Contact
    "Actor": user?.email || user?.id || "unknown",
    "Timestamp": new Date().toISOString(),
  };
  if (reason)     fields["Reason"]    = reason;
  if (beforeJson) fields["Before"]    = typeof beforeJson === "string" ? beforeJson : JSON.stringify(beforeJson);
  if (afterJson)  fields["After"]     = typeof afterJson  === "string" ? afterJson  : JSON.stringify(afterJson);

  try {
    await airtable(env, "POST", T.AUDIT, "", { records: [{ fields }], typecast: true });
  } catch (err) {
    // Audit failure must not block the primary action — log and continue.
    console.error("audit write failed:", err.message);
  }
}

// ----------------------------------------------------------------------------
// GET /api/ingest/queue
// ----------------------------------------------------------------------------

async function handleQueueList(request, env) {
  const url = new URL(request.url);
  const kind        = url.searchParams.get("kind");        // Organisation | Contact | all
  const status      = url.searchParams.get("status");      // pending_review | manual | rejected | promoted | all
  const sourceRef   = url.searchParams.get("source_ref");  // partial match on Source Ref
  const confMin     = parseFloat(url.searchParams.get("conf_min") || "0");
  const confMax     = parseFloat(url.searchParams.get("conf_max") || "1");
  const flag        = url.searchParams.get("flag");        // e.g. "unresolved_org"
  const pageSize    = Math.min(parseInt(url.searchParams.get("page_size") || "50", 10), 100);
  const offset      = url.searchParams.get("offset") || null;

  // Build Airtable formula.
  const clauses = [];
  if (kind && kind !== "all") clauses.push(`{Kind}='${kind.replace(/'/g, "\\'")}'`);
  if (status && status !== "all") clauses.push(`{Status}='${status.replace(/'/g, "\\'")}'`);
  if (flag) clauses.push(`{Flag}='${flag.replace(/'/g, "\\'")}'`);
  if (sourceRef) clauses.push(`FIND('${sourceRef.replace(/'/g, "\\'")}', {Source Ref})>0`);
  if (confMin > 0) clauses.push(`{Ingestion Confidence}>=${confMin}`);
  if (confMax < 1) clauses.push(`{Ingestion Confidence}<=${confMax}`);
  const formula = clauses.length ? `AND(${clauses.join(",")})` : "";

  // Decide which table — review queue by default; manual bucket if status=manual.
  const table = (status === "manual") ? T.MANUAL : T.REVIEW;

  const params = new URLSearchParams({
    pageSize: String(pageSize),
    "sort[0][field]": "Ingestion Confidence",
    "sort[0][direction]": "desc",
  });
  if (formula) params.set("filterByFormula", formula);
  if (offset)  params.set("offset", offset);

  const data = await airtable(env, "GET", table, "?" + params.toString());

  const items = (data.records || []).map(rec => {
    const f = rec.fields || {};
    let payload = {};
    try { payload = f["Payload JSON"] ? JSON.parse(f["Payload JSON"]) : {}; } catch { /* ignore */ }
    return {
      airtable_id: rec.id,
      external_id: f["External ID"],
      kind: f["Kind"],
      status: f["Status"],
      display_name: f["Display Name"],
      email: f["Email"],
      organisation_name: f["Organisation Name"],
      website: f["Website"],
      source_ref: f["Source Ref"],
      ingestion_confidence: f["Ingestion Confidence"],
      ingestion_tier: f["Ingestion Tier"],
      flag: f["Flag"],
      payload,
    };
  });

  return json({
    items,
    offset: data.offset || null,
    table,
  });
}

// ----------------------------------------------------------------------------
// GET /api/ingest/stats
// ----------------------------------------------------------------------------

async function handleStats(_request, env) {
  // Fetch both queues in parallel. Page through each with a pagination loop
  // capped at 20 pages (2,000 records/queue) to stay within worker CPU budget.
  async function countAll(table) {
    let offset = null;
    const agg = { total: 0, by_kind: {}, by_status: {}, by_flag: {}, by_tier: {} };
    for (let i = 0; i < 20; i++) {
      const params = new URLSearchParams({
        pageSize: "100",
        "fields[]": "Kind",
      });
      params.append("fields[]", "Status");
      params.append("fields[]", "Flag");
      params.append("fields[]", "Ingestion Tier");
      if (offset) params.set("offset", offset);
      const data = await airtable(env, "GET", table, "?" + params.toString());
      for (const rec of data.records || []) {
        const f = rec.fields || {};
        agg.total++;
        agg.by_kind[f["Kind"]]          = (agg.by_kind[f["Kind"]]          || 0) + 1;
        agg.by_status[f["Status"]]      = (agg.by_status[f["Status"]]      || 0) + 1;
        if (f["Flag"])     agg.by_flag[f["Flag"]]       = (agg.by_flag[f["Flag"]]       || 0) + 1;
        if (f["Ingestion Tier"]) agg.by_tier[f["Ingestion Tier"]] = (agg.by_tier[f["Ingestion Tier"]] || 0) + 1;
      }
      offset = data.offset;
      if (!offset) break;
    }
    return agg;
  }

  const [review, manual] = await Promise.all([countAll(T.REVIEW), countAll(T.MANUAL)]);
  return json({ review, manual });
}

// ----------------------------------------------------------------------------
// POST /api/ingest/promote
// ----------------------------------------------------------------------------

async function handlePromote(request, env, ctx, auth) {
  const body = await request.json();
  const externalId = body.external_id;
  const userEdits  = body.edits || {};
  if (!externalId) return json({ error: "external_id required" }, 400);

  // 1. Fetch the queue row.
  const found = await findQueueRow(env, externalId);
  if (!found) return json({ error: "not found" }, 404);
  const { table: queueTable, record: queueRec } = found;
  const queueFields = queueRec.fields || {};
  let payload = {};
  try { payload = JSON.parse(queueFields["Payload JSON"] || "{}"); } catch {}

  // 2. Apply user edits on top of payload (edits win).
  const beforePayload = { ...payload };
  const merged = { ...payload, ...userEdits };

  const kind = queueFields["Kind"];  // "Organisation" | "Contact"
  if (!kind) return json({ error: "queue row missing Kind" }, 500);

  // 3. Project to target table fields.
  let targetTable, fieldMap;
  if (kind === "Organisation") {
    targetTable = T.ORGS;
    fieldMap = ORG_MAP;
  } else if (kind === "Contact") {
    targetTable = T.CONTACTS;
    fieldMap = CONTACT_MAP;
    // Resolve org FK if not already set.
    if (!merged.org_id && merged.org_name) {
      const orgNormalised = (merged.org_name_normalised || merged.org_name).toLowerCase().trim();
      const orgRecId = await findOrgIdByNormalisedName(env, orgNormalised);
      if (orgRecId) {
        merged.org_id = [orgRecId];
      }
      // Note: if unresolved, we still promote — the Contact will exist without a linked org,
      // which the reviewer can fix later. We surface this via the response.
    }
  } else {
    return json({ error: `unknown Kind: ${kind}` }, 400);
  }

  const projected = project(merged, fieldMap);
  projected[fieldMap.external_id] = externalId;

  // 4. Upsert into target table (create or update by External ID).
  let resultAction, targetRecordId;
  const existingId = await existingTargetId(env, targetTable, externalId);
  if (existingId) {
    await airtable(env, "PATCH", targetTable, "", {
      records: [{ id: existingId, fields: projected }],
      typecast: true,
    });
    resultAction = "updated";
    targetRecordId = existingId;
  } else {
    const created = await airtable(env, "POST", targetTable, "", {
      records: [{ fields: projected }],
      typecast: true,
    });
    resultAction = "created";
    targetRecordId = created.records[0].id;
  }

  // 5. Mark queue row as promoted (don't delete — keep audit trail in queue).
  await airtable(env, "PATCH", queueTable, "", {
    records: [{
      id: queueRec.id,
      fields: {
        "Status": "promoted",
        "Promoted At": new Date().toISOString(),
        "Promoted To Record ID": targetRecordId,
      },
    }],
    typecast: true,
  });

  // 6. Audit.
  ctx.waitUntil(writeAudit(env, {
    action: "promoted",
    externalId,
    kind,
    user: auth?.user,
    beforeJson: beforePayload,
    afterJson: merged,
  }));

  return json({
    ok: true,
    action: resultAction,
    external_id: externalId,
    target_table: targetTable,
    target_record_id: targetRecordId,
    org_resolved: kind === "Contact" ? Boolean(merged.org_id) : undefined,
  });
}

// ----------------------------------------------------------------------------
// POST /api/ingest/reject
// ----------------------------------------------------------------------------

async function handleReject(request, env, ctx, auth) {
  const body = await request.json();
  const externalId = body.external_id;
  const reason     = body.reason || "";
  if (!externalId) return json({ error: "external_id required" }, 400);

  const found = await findQueueRow(env, externalId);
  if (!found) return json({ error: "not found" }, 404);

  await airtable(env, "PATCH", found.table, "", {
    records: [{
      id: found.record.id,
      fields: {
        "Status": "rejected",
        "Rejected At": new Date().toISOString(),
        "Reject Reason": reason,
      },
    }],
    typecast: true,
  });

  ctx.waitUntil(writeAudit(env, {
    action: "rejected",
    externalId,
    kind: found.record.fields["Kind"],
    user: auth?.user,
    reason,
  }));

  return json({ ok: true, external_id: externalId });
}

// ----------------------------------------------------------------------------
// POST /api/ingest/edit
// ----------------------------------------------------------------------------
// Update Payload JSON fields in the queue row without promoting yet. Use when a
// reviewer wants to fix a few values before committing to promotion.

async function handleEdit(request, env, ctx, auth) {
  const body = await request.json();
  const externalId = body.external_id;
  const edits      = body.fields || {};
  if (!externalId) return json({ error: "external_id required" }, 400);

  const found = await findQueueRow(env, externalId);
  if (!found) return json({ error: "not found" }, 404);

  let payload = {};
  try { payload = JSON.parse(found.record.fields["Payload JSON"] || "{}"); } catch {}
  const beforePayload = { ...payload };
  const afterPayload  = { ...payload, ...edits };

  // Keep the display-surfaced fields in sync with the payload.
  const kind = found.record.fields["Kind"];
  const surfaceUpdates = {
    "Payload JSON": JSON.stringify(afterPayload),
  };
  if (kind === "Organisation") {
    if (edits.org_name !== undefined) surfaceUpdates["Display Name"] = edits.org_name || "";
    if (edits.website  !== undefined) surfaceUpdates["Website"]      = edits.website  || "";
  } else if (kind === "Contact") {
    if (edits.full_name !== undefined || edits.email !== undefined) {
      surfaceUpdates["Display Name"] = afterPayload.full_name || afterPayload.email || "";
    }
    if (edits.email     !== undefined) surfaceUpdates["Email"]             = edits.email     || "";
    if (edits.org_name  !== undefined) surfaceUpdates["Organisation Name"] = edits.org_name  || "";
  }

  await airtable(env, "PATCH", found.table, "", {
    records: [{ id: found.record.id, fields: surfaceUpdates }],
    typecast: true,
  });

  ctx.waitUntil(writeAudit(env, {
    action: "edited",
    externalId,
    kind,
    user: auth?.user,
    beforeJson: beforePayload,
    afterJson: afterPayload,
  }));

  return json({ ok: true, external_id: externalId, payload: afterPayload });
}

// ----------------------------------------------------------------------------
// POST /api/ingest/bulk-promote
// ----------------------------------------------------------------------------

async function handleBulkPromote(request, env, ctx, auth) {
  const body = await request.json();
  const ids = Array.isArray(body.external_ids) ? body.external_ids : [];
  const editsById = body.edits_by_id || {};
  if (ids.length === 0) return json({ error: "external_ids[] required" }, 400);
  if (ids.length > 100)  return json({ error: "max 100 per bulk request" }, 400);

  // Promote sequentially to keep the order deterministic in the audit log and
  // to avoid hammering Airtable's 5 req/sec limit. Each promote is ~4 Airtable
  // calls, so we pace at ~1/sec for headroom.
  const results = [];
  for (const externalId of ids) {
    try {
      const pseudoReq = new Request(request.url, {
        method: "POST",
        headers: request.headers,
        body: JSON.stringify({ external_id: externalId, edits: editsById[externalId] || {} }),
      });
      const resp = await handlePromote(pseudoReq, env, ctx, auth);
      const data = await resp.json();
      results.push({ external_id: externalId, ...data });
    } catch (err) {
      results.push({ external_id: externalId, ok: false, error: err.message });
    }
    // Soft pacing — 4 Airtable calls per promote at 5/sec ceiling = ~800ms between starts.
    await new Promise(r => setTimeout(r, 250));
  }
  const okCount = results.filter(r => r.ok).length;
  return json({ ok: true, total: ids.length, succeeded: okCount, failed: ids.length - okCount, results });
}

// ----------------------------------------------------------------------------
// Response helper
// ----------------------------------------------------------------------------

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

// ----------------------------------------------------------------------------
// Router — wire into the existing worker-index.js
// ----------------------------------------------------------------------------

// In the existing worker-index.js, add these cases to the main fetch handler:
//
//   const url = new URL(request.url);
//   const path = url.pathname;
//
//   // After your existing /api/* routes, add:
//   if (path === "/api/ingest/queue"         && request.method === "GET")  return handleQueueList(request, env);
//   if (path === "/api/ingest/stats"         && request.method === "GET")  return handleStats(request, env);
//   if (path === "/api/ingest/promote"       && request.method === "POST") return handlePromote(request, env, ctx, auth);
//   if (path === "/api/ingest/reject"        && request.method === "POST") return handleReject(request, env, ctx, auth);
//   if (path === "/api/ingest/edit"          && request.method === "POST") return handleEdit(request, env, ctx, auth);
//   if (path === "/api/ingest/bulk-promote"  && request.method === "POST") return handleBulkPromote(request, env, ctx, auth);
//
// Exporting the handlers so they can also be imported as a module if the main
// worker uses ES module routing.

export {
  handleQueueList,
  handleStats,
  handlePromote,
  handleReject,
  handleEdit,
  handleBulkPromote,
};
