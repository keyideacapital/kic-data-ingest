// ============================================================================
// Patch: add "Enrich" button to the detail pane in ingest-review.js
// ============================================================================
//
// Apply these three small changes to ingest-review.js to wire the enrichment
// button. The backing worker endpoint is in ingest-enrich-api.js.
//
// CHANGE 1 — ingest-review.html, in the .detail-actions block:
// ----------------------------------------------------------------------------
// Before:
//     <div class="detail-actions">
//       <button id="action-promote" class="btn-primary">Promote</button>
//       <button id="action-save-edits" class="btn-secondary">Save edits</button>
//       <button id="action-reject" class="btn-danger">Reject</button>
//     </div>
//
// After:
//     <div class="detail-actions">
//       <button id="action-enrich"    class="btn-secondary">Enrich</button>
//       <button id="action-promote"   class="btn-primary">Promote</button>
//       <button id="action-save-edits" class="btn-secondary">Save edits</button>
//       <button id="action-reject"    class="btn-danger">Reject</button>
//     </div>
//
//
// CHANGE 2 — ingest-review.js, in bindActions():
// ----------------------------------------------------------------------------
// Add this line at the end of bindActions():
//     $("#action-enrich").addEventListener("click", doEnrich);
//
//
// CHANGE 3 — ingest-review.js, new function (drop anywhere in the "Actions" section):
// ----------------------------------------------------------------------------

async function doEnrich() {
  const item = currentItem();
  if (!item) return;
  if (!item.payload?.website) {
    toast("Cannot enrich — record has no website URL", "error");
    return;
  }
  const btn = $("#action-enrich");
  btn.disabled = true;
  btn.textContent = "Enriching…";
  try {
    const resp = await api("/api/ingest/enrich-one", {
      method: "POST",
      body: JSON.stringify({ external_id: item.external_id }),
    });
    if (!resp.ok) {
      toast(`Enrichment failed: ${resp.details || resp.reason}`, "error");
      return;
    }
    // Replace the local item's payload and surfaced fields
    item.payload = resp.payload;
    item.ingestion_confidence = resp.new_confidence;
    item.ingestion_tier = resp.new_tier;
    if (resp.payload.org_name) item.display_name = resp.payload.org_name;
    state.currentEdits = {};
    renderList();
    renderDetail(item);
    toast(`Enriched via ${resp.source_class} source → ${resp.new_tier} tier`, "success");
  } catch (err) {
    toast(`Enrich failed: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Enrich";
  }
}

//
// CHANGE 4 (optional) — keyboard shortcut 'x' for enrich:
// ----------------------------------------------------------------------------
// In bindKeyboard()'s switch statement, after case "e":
//     case "x": if (currentItem()) doEnrich(); break;
