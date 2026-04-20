// ============================================================================
// KIC Ingest Enrichment — single-record on-demand enrichment (review UI hook)
// ============================================================================
//
// Append this to worker-index.js alongside the existing /api/ingest/* handlers
// to add a "Enrich now" button workflow to the review queue UI.
//
// When a reviewer is looking at a URL-only record and clicks "Enrich", the UI
// POSTs to /api/ingest/enrich-one with the external_id. The worker:
//   1. Reads the queue row's Payload JSON to get the website URL
//   2. Classifies the URL (official / directory / unknown)
//   3. Fetches the homepage (bounded size, respects content-type)
//   4. Calls Claude with the same prompts as enrich.py
//   5. Patches the queue row's Payload JSON with the extracted fields and
//      recomputes the ingestion_tier per the same rules as enrich.py
//   6. Returns the enriched payload so the UI can refresh the detail pane
//
// This path is intentionally single-record and synchronous (a few seconds) —
// reviewer clicks, sees spinner, gets updated fields. Bulk background
// enrichment belongs in the agent-swarm module, not here.
// ============================================================================

const ENRICH_FETCH_TIMEOUT_MS = 12000;
const ENRICH_MAX_BYTES = 500_000;
const ENRICH_USER_AGENT = "KIC-Ingest-Enrichment/1.0 (+https://keyideacapital.com)";
const ENRICH_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001";

const DIRECTORY_DOMAINS = new Set([
  "crunchbase.com", "pitchbook.com", "tracxn.com", "cbinsights.com",
  "dealroom.co", "angellist.com", "wellfound.com", "techcrunch.com",
  "bloomberg.com", "reuters.com", "forbes.com", "fortune.com",
  "wikipedia.org", "linkedin.com", "zoominfo.com", "hunter.io",
  "rocketreach.co", "f6s.com", "producthunt.com",
]);

function classifyUrl(url) {
  try {
    const u = url.match(/^https?:\/\//i) ? url : "http://" + url;
    let host = new URL(u).hostname.toLowerCase().replace(/^www\./, "");
    const parts = host.split(".");
    const candidates = new Set([host]);
    if (parts.length >= 2) candidates.add(parts.slice(-2).join("."));
    if (parts.length >= 3) candidates.add(parts.slice(-3).join("."));
    for (const c of candidates) {
      if (DIRECTORY_DOMAINS.has(c)) return "directory";
    }
    return "official";
  } catch {
    return "unknown";
  }
}

function tierForConf(c) {
  if (c >= 0.85) return "high";
  if (c >= 0.60) return "medium";
  return "low";
}

// --- HTML -> text reduction ------------------------------------------------

function extractTextSnapshot(html, maxChars = 6000) {
  const stripped = String(html)
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, " ")
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, " ")
    .replace(/<!--[\s\S]*?-->/g, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return stripped.length > maxChars ? stripped.slice(0, maxChars) + "…(truncated)" : stripped;
}

async function fetchPageText(url) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ENRICH_FETCH_TIMEOUT_MS);
  try {
    const resp = await fetch(url, {
      headers: { "User-Agent": ENRICH_USER_AGENT, "Accept": "text/html,*/*;q=0.5" },
      signal: controller.signal,
      redirect: "follow",
    });
    const ctype = resp.headers.get("content-type") || "";
    if (!ctype.includes("text/html")) {
      return { ok: false, status: resp.status, error: `non-html: ${ctype}`, finalUrl: resp.url };
    }
    // Stream-read with byte cap
    const reader = resp.body.getReader();
    let received = 0;
    const chunks = [];
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      chunks.push(value);
      received += value.length;
      if (received >= ENRICH_MAX_BYTES) { reader.cancel(); break; }
    }
    const buf = new Uint8Array(received);
    let off = 0;
    for (const c of chunks) { buf.set(c.subarray(0, Math.min(c.length, received - off)), off); off += c.length; }
    const text = new TextDecoder("utf-8", { fatal: false }).decode(buf);
    return { ok: resp.ok, status: resp.status, html: text, finalUrl: resp.url };
  } catch (err) {
    return { ok: false, status: 0, error: err.name === "AbortError" ? "timeout" : err.message };
  } finally {
    clearTimeout(timer);
  }
}

// --- Anthropic call --------------------------------------------------------

async function callAnthropic(env, system, user, maxTokens = 700) {
  const resp = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "x-api-key": env.ANTHROPIC_API_KEY,
      "anthropic-version": "2023-06-01",
      "content-type": "application/json",
    },
    body: JSON.stringify({
      model: ENRICH_ANTHROPIC_MODEL,
      max_tokens: maxTokens,
      system,
      messages: [{ role: "user", content: user }],
    }),
  });
  if (!resp.ok) {
    throw new Error(`anthropic ${resp.status}: ${(await resp.text()).slice(0, 300)}`);
  }
  const data = await resp.json();
  const text = (data.content || [])
    .filter(b => b.type === "text")
    .map(b => b.text)
    .join("");
  return { text, usage: data.usage || {} };
}

function parseJsonFromText(text) {
  if (!text) return null;
  const fenced = text.match(/```(?:json)?\s*(\{[\s\S]*?\})\s*```/);
  const candidate = fenced ? fenced[1] : (text.slice(text.indexOf("{")));
  if (!candidate || !candidate.startsWith("{")) return null;
  // Walk to find balanced braces
  let depth = 0;
  for (let i = 0; i < candidate.length; i++) {
    if (candidate[i] === "{") depth++;
    else if (candidate[i] === "}") {
      depth--;
      if (depth === 0) {
        try { return JSON.parse(candidate.slice(0, i + 1)); }
        catch { return null; }
      }
    }
  }
  return null;
}

// --- Prompt templates (must stay in sync with scripts/enrich.py) -----------

const ORG_SYSTEM = "You extract structured information about a company from its web page. You return ONLY a JSON object. Do not include any prose outside the JSON. If a field is not stated on the page, set it to null — do not guess.";

function orgPrompt(url, sourceClass, body) {
  return `Below is the text of a company's web page. Extract what you can.

URL: ${url}
SOURCE CLASSIFICATION: ${sourceClass}   (official = company's own site; directory = aggregator/third-party)

Return a JSON object with these keys:
- org_name: string | null
- org_type: string | null — e.g. "VC", "PE", "Bank", "Family Office", "Corporate", "Advisor", "Fund Manager"
- hq_city: string | null
- hq_country: string | null
- sector: string | null
- stage_focus: string | null
- aum: string | null
- phone: string | null
- general_email: string | null
- linkedin_company_url: string | null
- notes: string | null — 1 short sentence of distinguishing context
- extraction_confidence: number between 0 and 1

Page text:
"""
${body}
"""

Return ONLY the JSON.`;
}

// --- Endpoint handler ------------------------------------------------------

async function handleEnrichOne(request, env, ctx, auth) {
  const body = await request.json();
  const externalId = body.external_id;
  if (!externalId) return json({ error: "external_id required" }, 400);

  // Find the queue row — reuses helpers from ingest-api.js (findQueueRow).
  const found = await findQueueRow(env, externalId);
  if (!found) return json({ error: "not found" }, 404);
  const queueFields = found.record.fields || {};
  let payload = {};
  try { payload = JSON.parse(queueFields["Payload JSON"] || "{}"); } catch {}

  const url = (payload.website || "").trim();
  if (!url) return json({ error: "record has no website URL to enrich from" }, 400);

  const sourceClass = classifyUrl(url);

  // Fetch + extract
  const fetched = await fetchPageText(url);
  if (!fetched.ok || !fetched.html) {
    return json({
      ok: false, reason: "fetch_failed",
      details: fetched.error || `status ${fetched.status}`,
    }, 502);
  }
  const bodyText = extractTextSnapshot(fetched.html);
  if (bodyText.length < 50) {
    return json({ ok: false, reason: "fetch_failed", details: "page body too small" }, 502);
  }

  const { text: llmText, usage } = await callAnthropic(env, ORG_SYSTEM,
    orgPrompt(fetched.finalUrl, sourceClass, bodyText));
  const orgData = parseJsonFromText(llmText);
  if (!orgData) {
    return json({ ok: false, reason: "no_extraction", details: "LLM did not return valid JSON" }, 502);
  }

  // Merge into payload using the same rules as enrich.py.merge_into_org
  const extractConf = Number(orgData.extraction_confidence) || 0.6;
  const fieldsToMerge = [
    "org_name", "org_type", "hq_city", "hq_country", "sector",
    "stage_focus", "aum", "phone", "general_email", "linkedin_company_url", "notes",
  ];
  const merged = { ...payload };
  for (const k of fieldsToMerge) {
    const v = orgData[k];
    if (v && !merged[k]) merged[k] = v;
  }
  // Replace URL-derived name
  const nameLooksLikeUrl = (merged.org_name || "").includes(".") &&
    !(merged.org_name || "").includes(" ");
  if (nameLooksLikeUrl && orgData.org_name) merged.org_name = orgData.org_name;

  // Tier reassignment
  let newConf;
  if (sourceClass === "directory") {
    newConf = Math.min(0.80, 0.55 + 0.25 * extractConf);
  } else if (sourceClass === "official") {
    newConf = 0.60 + 0.35 * extractConf;
  } else {
    newConf = 0.55 + 0.30 * extractConf;
  }
  newConf = Math.max(newConf, Number(payload.ingestion_confidence) || 0);
  merged.ingestion_confidence = Math.round(newConf * 1000) / 1000;
  merged.ingestion_tier = tierForConf(newConf);
  merged.enrichment_source_class = sourceClass;
  merged.enriched_at = new Date().toISOString();
  merged.source_ref = (merged.source_ref || "") + `|enriched:${sourceClass}:${url}`;

  // Patch the queue row
  const surfaceUpdates = {
    "Payload JSON": JSON.stringify(merged),
    "Display Name": merged.org_name || queueFields["Display Name"] || "",
    "Ingestion Confidence": merged.ingestion_confidence,
    "Ingestion Tier": merged.ingestion_tier,
  };
  await airtable(env, "PATCH", found.table, "", {
    records: [{ id: found.record.id, fields: surfaceUpdates }],
    typecast: true,
  });

  // Audit
  ctx.waitUntil(writeAudit(env, {
    action: "enriched",
    externalId,
    kind: queueFields["Kind"],
    user: auth?.user,
    reason: `source=${sourceClass}, extract_conf=${extractConf}, tokens=${(usage.input_tokens||0)+(usage.output_tokens||0)}`,
    beforeJson: payload,
    afterJson: merged,
  }));

  return json({
    ok: true,
    external_id: externalId,
    source_class: sourceClass,
    payload: merged,
    new_tier: merged.ingestion_tier,
    new_confidence: merged.ingestion_confidence,
    llm_tokens: (usage.input_tokens || 0) + (usage.output_tokens || 0),
  });
}

// ----------------------------------------------------------------------------
// Router integration: add to worker-index.js:
//
//   if (path === "/api/ingest/enrich-one" && request.method === "POST") {
//       return handleEnrichOne(request, env, ctx, auth);
//   }
// ----------------------------------------------------------------------------

export { handleEnrichOne };
