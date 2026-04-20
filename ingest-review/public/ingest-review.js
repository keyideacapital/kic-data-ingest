/* ============================================================================
   KIC Ingest Review — frontend
   Vanilla JS, no build step. Talks to /api/ingest/* on the existing KIC worker.
   ============================================================================ */

"use strict";

// ----- Field schemas for rendering -----------------------------------------
// Ordered for reviewer priority. Only these fields are rendered/edited in the
// detail pane; the raw canonical payload retains any extra fields untouched.

const ORG_FIELDS = [
  { key: "org_name",             label: "Organisation name",   type: "text" },
  { key: "org_type",             label: "Type",                type: "text" },
  { key: "website",              label: "Website",             type: "url" },
  { key: "website_domain",       label: "Website domain",      type: "text" },
  { key: "linkedin_company_url", label: "LinkedIn URL",        type: "url" },
  { key: "hq_city",              label: "HQ city",             type: "text" },
  { key: "hq_country",           label: "HQ country",          type: "text" },
  { key: "offices",              label: "Offices",             type: "text" },
  { key: "phone",                label: "Phone",               type: "tel" },
  { key: "general_email",        label: "General email",       type: "email" },
  { key: "address",              label: "Address",             type: "textarea" },
  { key: "sector",               label: "Sector",              type: "text" },
  { key: "stage_focus",          label: "Stage focus",         type: "text" },
  { key: "aum",                  label: "AUM",                 type: "text" },
  { key: "licence_type",         label: "Licence type",        type: "text" },
  { key: "activity_type",        label: "Activity type",       type: "text" },
  { key: "notes",                label: "Notes",               type: "textarea" },
];

const CONTACT_FIELDS = [
  { key: "full_name",            label: "Full name",           type: "text" },
  { key: "first_name",           label: "First name",          type: "text" },
  { key: "last_name",            label: "Last name",           type: "text" },
  { key: "job_title",            label: "Job title",           type: "text" },
  { key: "org_name",             label: "Organisation",        type: "text" },
  { key: "email",                label: "Email",               type: "email" },
  { key: "email_alt",            label: "Email (alt)",         type: "email" },
  { key: "phone_business",       label: "Phone (business)",    type: "tel" },
  { key: "phone_mobile",         label: "Phone (mobile)",      type: "tel" },
  { key: "linkedin_profile_url", label: "LinkedIn profile",    type: "url" },
  { key: "city",                 label: "City",                type: "text" },
  { key: "country",              label: "Country",             type: "text" },
  { key: "notes",                label: "Notes",               type: "textarea" },
];

// ----- Application state ---------------------------------------------------

const state = {
  items: [],
  offset: null,
  loading: false,
  selectedIndex: -1,
  selectedIds: new Set(),   // external_ids of checkbox-selected rows
  currentEdits: {},         // edits to the currently-viewed item, keyed by field
  filters: {
    status: "pending_review",
    kind: "all",
    flag: "",
    source_ref: "",
    conf_min: 0,
    conf_max: 1,
    search: "",
  },
};

// ----- DOM shortcuts -------------------------------------------------------

const $  = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// ----- Helpers -------------------------------------------------------------

function toast(msg, kind = "success") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = `toast show ${kind}`;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => { el.hidden = true; }, 220);
  }, 2600);
}

async function api(path, opts = {}) {
  const resp = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.error || `HTTP ${resp.status}`);
  }
  return data;
}

function debounce(fn, wait = 250) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), wait);
  };
}

// ----- Filter controls -----------------------------------------------------

function bindFilters() {
  $$(".segmented").forEach(grp => {
    grp.addEventListener("click", (e) => {
      const btn = e.target.closest(".seg");
      if (!btn) return;
      const filterName = grp.dataset.filter;
      grp.querySelectorAll(".seg").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      state.filters[filterName] = btn.dataset.value;
    });
  });

  $("#filter-flag").addEventListener("change", (e) => { state.filters.flag = e.target.value; });
  $("#filter-source").addEventListener("input", debounce((e) => {
    state.filters.source_ref = e.target.value.trim();
  }, 300));

  const confMin = $("#conf-min"), confMax = $("#conf-max");
  const confMinVal = $("#conf-min-val"), confMaxVal = $("#conf-max-val");
  const updateConf = () => {
    state.filters.conf_min = Number(confMin.value) / 100;
    state.filters.conf_max = Number(confMax.value) / 100;
    confMinVal.textContent = state.filters.conf_min.toFixed(2);
    confMaxVal.textContent = state.filters.conf_max.toFixed(2);
    if (state.filters.conf_min > state.filters.conf_max) {
      // Swap visually rather than fight the user.
      const tmp = state.filters.conf_min;
      state.filters.conf_min = state.filters.conf_max;
      state.filters.conf_max = tmp;
    }
  };
  confMin.addEventListener("input", updateConf);
  confMax.addEventListener("input", updateConf);

  $("#filter-apply").addEventListener("click", () => resetAndLoad());
  $("#filter-clear").addEventListener("click", () => {
    state.filters = {
      status: "pending_review", kind: "all", flag: "", source_ref: "",
      conf_min: 0, conf_max: 1, search: "",
    };
    // Reset UI
    $$(".segmented").forEach(grp => {
      grp.querySelectorAll(".seg").forEach((b, i) => b.classList.toggle("active", i === 0));
    });
    $("#filter-flag").value = "";
    $("#filter-source").value = "";
    confMin.value = 0; confMax.value = 100;
    confMinVal.textContent = "0.00"; confMaxVal.textContent = "1.00";
    resetAndLoad();
  });

  $("#search").addEventListener("input", debounce((e) => {
    state.filters.search = e.target.value.trim();
    resetAndLoad();
  }, 350));

  $("#refresh").addEventListener("click", () => resetAndLoad());
  $("#load-more").addEventListener("click", () => loadPage());
}

// ----- Data loading --------------------------------------------------------

function buildQueryParams() {
  const f = state.filters;
  const params = new URLSearchParams();
  params.set("status", f.status);
  if (f.kind !== "all") params.set("kind", f.kind);
  if (f.flag)           params.set("flag", f.flag);
  if (f.source_ref)     params.set("source_ref", f.source_ref);
  if (f.conf_min > 0)   params.set("conf_min", String(f.conf_min));
  if (f.conf_max < 1)   params.set("conf_max", String(f.conf_max));
  if (f.search)         params.set("source_ref", (params.get("source_ref") || "") + f.search);
  params.set("page_size", "50");
  if (state.offset) params.set("offset", state.offset);
  return params;
}

async function loadPage() {
  if (state.loading) return;
  state.loading = true;
  $("#load-more").textContent = "Loading...";
  try {
    const params = buildQueryParams();
    const data = await api("/api/ingest/queue?" + params.toString());
    state.items.push(...data.items);
    state.offset = data.offset;
    renderList();
    $("#load-more").hidden = !data.offset;
    $("#list-empty").hidden = state.items.length > 0;
  } catch (err) {
    toast(`Load failed: ${err.message}`, "error");
  } finally {
    state.loading = false;
    $("#load-more").textContent = "Load more";
  }
}

async function resetAndLoad() {
  state.items = [];
  state.offset = null;
  state.selectedIndex = -1;
  state.selectedIds.clear();
  state.currentEdits = {};
  updateBulkBar();
  renderList();
  hideDetail();
  await loadPage();
  await loadStats();
}

async function loadStats() {
  try {
    const s = await api("/api/ingest/stats");
    const review  = s.review?.total  || 0;
    const manual  = s.manual?.total  || 0;
    const promoted = (s.review?.by_status?.promoted || 0) + (s.manual?.by_status?.promoted || 0);
    $$(".stat-chip[data-kind='review']")[0].textContent   = `Review ${review}`;
    $$(".stat-chip[data-kind='manual']")[0].textContent   = `Manual ${manual}`;
    $$(".stat-chip[data-kind='promoted']")[0].textContent = `Promoted ${promoted}`;
  } catch (_) {
    // stats failures are non-fatal
  }
}

// ----- List rendering ------------------------------------------------------

function renderList() {
  const ul = $("#list");
  ul.innerHTML = "";
  state.items.forEach((item, idx) => {
    const li = document.createElement("li");
    li.className = "list-item" + (idx === state.selectedIndex ? " selected" : "");
    li.dataset.index = String(idx);
    li.dataset.externalId = item.external_id;

    const checked = state.selectedIds.has(item.external_id);
    li.innerHTML = `
      <input type="checkbox" class="list-check" ${checked ? "checked" : ""} aria-label="Select" />
      <div class="list-item-body">
        <div class="list-item-title">
          ${escapeHtml(item.display_name || "(unnamed)")}
        </div>
        <div class="list-item-sub">
          ${escapeHtml(subLine(item))}
        </div>
      </div>
      <div class="list-item-meta">
        <span class="tier-pill ${item.ingestion_tier || 'low'}">${escapeHtml(item.ingestion_tier || 'low')}</span>
        <span class="list-item-conf">${(item.ingestion_confidence || 0).toFixed(2)}</span>
      </div>
    `;

    li.addEventListener("click", (e) => {
      if (e.target.classList.contains("list-check")) return;  // checkbox handled below
      selectIndex(idx);
    });
    li.querySelector(".list-check").addEventListener("change", (e) => {
      if (e.target.checked) state.selectedIds.add(item.external_id);
      else state.selectedIds.delete(item.external_id);
      updateBulkBar();
    });
    ul.appendChild(li);
  });
}

function subLine(item) {
  const bits = [];
  if (item.kind === "Contact" && item.email) bits.push(item.email);
  if (item.organisation_name) bits.push(`@ ${item.organisation_name}`);
  if (item.kind === "Organisation" && item.website) bits.push(item.website);
  const srcFile = (item.source_ref || "").split("#")[0].split("!")[0];
  if (srcFile) bits.push(srcFile);
  if (item.flag) bits.push(`⚠ ${item.flag}`);
  return bits.join(" · ");
}

function escapeHtml(s) {
  return String(s || "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function updateBulkBar() {
  const n = state.selectedIds.size;
  $("#selection-count").textContent = `${n} selected`;
  $("#bulk-actions").hidden = n === 0;
  $("#bulk-select-all").checked = n > 0 && n === state.items.length;
}

// ----- Detail pane ---------------------------------------------------------

function selectIndex(idx) {
  if (idx < 0 || idx >= state.items.length) return;
  state.selectedIndex = idx;
  state.currentEdits = {};
  renderList();
  renderDetail(state.items[idx]);
}

function hideDetail() {
  $("#detail-empty").hidden = false;
  $("#detail-card").hidden = true;
}

function renderDetail(item) {
  $("#detail-empty").hidden = true;
  const card = $("#detail-card");
  card.hidden = false;

  const tier = item.ingestion_tier || "low";
  $("#detail-tier").className = `tier-pill ${tier}`;
  $("#detail-tier").textContent = tier;
  $("#detail-kind").textContent = item.kind;

  const flagEl = $("#detail-flag");
  if (item.flag) {
    flagEl.textContent = `⚠ ${item.flag}`;
    flagEl.hidden = false;
  } else {
    flagEl.hidden = true;
  }

  $("#detail-name").textContent = item.display_name || "(unnamed)";
  $("#detail-meta").textContent =
    `confidence ${(item.ingestion_confidence || 0).toFixed(3)} · ${item.external_id}`;
  $("#detail-source-ref").textContent = item.source_ref || "(no source ref)";

  // Render editable fields
  const schema = item.kind === "Organisation" ? ORG_FIELDS : CONTACT_FIELDS;
  const form = $("#detail-form");
  form.innerHTML = "";
  schema.forEach(fieldDef => {
    const wrap = document.createElement("div");
    wrap.className = "field";
    wrap.dataset.fieldKey = fieldDef.key;

    const label = document.createElement("label");
    label.className = "field-label";
    label.textContent = fieldDef.label;
    wrap.appendChild(label);

    const currentValue = item.payload[fieldDef.key] || "";
    let input;
    if (fieldDef.type === "textarea") {
      input = document.createElement("textarea");
      input.className = "field-textarea";
    } else {
      input = document.createElement("input");
      input.type = fieldDef.type;
      input.className = "field-input";
    }
    input.value = currentValue;
    input.dataset.originalValue = currentValue;
    input.dataset.fieldKey = fieldDef.key;
    input.addEventListener("input", (e) => {
      const original = e.target.dataset.originalValue || "";
      const now = e.target.value;
      if (now !== original) {
        state.currentEdits[fieldDef.key] = now;
        wrap.classList.add("edited");
      } else {
        delete state.currentEdits[fieldDef.key];
        wrap.classList.remove("edited");
      }
    });
    wrap.appendChild(input);
    form.appendChild(wrap);
  });
}

// ----- Actions -------------------------------------------------------------

function currentItem() {
  return state.items[state.selectedIndex];
}

async function doPromote() {
  const item = currentItem();
  if (!item) return;
  try {
    const resp = await api("/api/ingest/promote", {
      method: "POST",
      body: JSON.stringify({ external_id: item.external_id, edits: state.currentEdits }),
    });
    let msg = `Promoted (${resp.action})`;
    if (item.kind === "Contact" && resp.org_resolved === false) {
      msg += " — org not resolved; contact is unlinked";
      toast(msg, "error");
    } else {
      toast(msg, "success");
    }
    // Remove from the local list and advance.
    state.items.splice(state.selectedIndex, 1);
    state.selectedIds.delete(item.external_id);
    updateBulkBar();
    if (state.selectedIndex >= state.items.length) state.selectedIndex = state.items.length - 1;
    renderList();
    if (state.selectedIndex >= 0) renderDetail(state.items[state.selectedIndex]);
    else hideDetail();
    loadStats();
  } catch (err) {
    toast(`Promote failed: ${err.message}`, "error");
  }
}

async function doReject() {
  const item = currentItem();
  if (!item) return;
  const reason = prompt("Reject reason (optional):", "");
  if (reason === null) return;  // cancelled
  try {
    await api("/api/ingest/reject", {
      method: "POST",
      body: JSON.stringify({ external_id: item.external_id, reason }),
    });
    toast("Rejected", "success");
    state.items.splice(state.selectedIndex, 1);
    state.selectedIds.delete(item.external_id);
    updateBulkBar();
    if (state.selectedIndex >= state.items.length) state.selectedIndex = state.items.length - 1;
    renderList();
    if (state.selectedIndex >= 0) renderDetail(state.items[state.selectedIndex]);
    else hideDetail();
    loadStats();
  } catch (err) {
    toast(`Reject failed: ${err.message}`, "error");
  }
}

async function doSaveEdits() {
  const item = currentItem();
  if (!item) return;
  if (Object.keys(state.currentEdits).length === 0) {
    toast("No changes to save");
    return;
  }
  try {
    const resp = await api("/api/ingest/edit", {
      method: "POST",
      body: JSON.stringify({ external_id: item.external_id, fields: state.currentEdits }),
    });
    toast("Edits saved", "success");
    // Merge saved edits back into local item so the UI reflects the new baseline.
    Object.assign(item.payload, state.currentEdits);
    if (resp.payload) item.payload = resp.payload;
    // Refresh derived display fields
    if (item.kind === "Organisation" && state.currentEdits.org_name !== undefined) {
      item.display_name = state.currentEdits.org_name;
    }
    state.currentEdits = {};
    renderList();
    renderDetail(item);
  } catch (err) {
    toast(`Save failed: ${err.message}`, "error");
  }
}

async function doBulkPromote() {
  if (state.selectedIds.size === 0) return;
  if (!confirm(`Promote ${state.selectedIds.size} records?`)) return;
  try {
    const resp = await api("/api/ingest/bulk-promote", {
      method: "POST",
      body: JSON.stringify({ external_ids: Array.from(state.selectedIds) }),
    });
    toast(`Bulk promote: ${resp.succeeded} ok, ${resp.failed} failed`, resp.failed ? "error" : "success");
    // Drop promoted items from the local list.
    const promotedIds = new Set(resp.results.filter(r => r.ok).map(r => r.external_id));
    state.items = state.items.filter(it => !promotedIds.has(it.external_id));
    state.selectedIds.clear();
    state.selectedIndex = -1;
    updateBulkBar();
    renderList();
    hideDetail();
    loadStats();
  } catch (err) {
    toast(`Bulk promote failed: ${err.message}`, "error");
  }
}

async function doBulkReject() {
  if (state.selectedIds.size === 0) return;
  const reason = prompt(`Reject ${state.selectedIds.size} records. Reason (optional):`, "");
  if (reason === null) return;
  // Sequential (no bulk endpoint) — keep it simple.
  let ok = 0, fail = 0;
  for (const externalId of Array.from(state.selectedIds)) {
    try {
      await api("/api/ingest/reject", {
        method: "POST",
        body: JSON.stringify({ external_id: externalId, reason }),
      });
      ok++;
    } catch (_) { fail++; }
  }
  toast(`Bulk reject: ${ok} ok, ${fail} failed`, fail ? "error" : "success");
  const handled = state.selectedIds;
  state.items = state.items.filter(it => !handled.has(it.external_id));
  state.selectedIds.clear();
  state.selectedIndex = -1;
  updateBulkBar();
  renderList();
  hideDetail();
  loadStats();
}

// ----- Bulk select-all -----------------------------------------------------

function bindBulkSelectAll() {
  $("#bulk-select-all").addEventListener("change", (e) => {
    if (e.target.checked) {
      state.items.forEach(it => state.selectedIds.add(it.external_id));
    } else {
      state.selectedIds.clear();
    }
    renderList();
    updateBulkBar();
  });
  $("#bulk-promote").addEventListener("click", doBulkPromote);
  $("#bulk-reject").addEventListener("click", doBulkReject);
}

// ----- Action buttons ------------------------------------------------------

function bindActions() {
  $("#action-promote").addEventListener("click", doPromote);
  $("#action-reject").addEventListener("click", doReject);
  $("#action-save-edits").addEventListener("click", doSaveEdits);
}

// ----- Keyboard ------------------------------------------------------------

function bindKeyboard() {
  document.addEventListener("keydown", (e) => {
    // Ignore when typing in a form field.
    const tag = e.target.tagName;
    if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) {
      // Allow cmd/ctrl+enter in detail to promote
      if ((e.key === "Enter") && (e.metaKey || e.ctrlKey) && currentItem()) {
        e.preventDefault();
        doPromote();
      }
      return;
    }
    switch (e.key) {
      case "j": selectIndex(Math.min(state.selectedIndex + 1, state.items.length - 1)); break;
      case "k": selectIndex(Math.max(state.selectedIndex - 1, 0)); break;
      case "p": if (currentItem()) doPromote(); break;
      case "r": if (currentItem()) doReject(); break;
      case "e": {
        if (currentItem()) {
          const firstInput = $("#detail-form .field-input, #detail-form .field-textarea");
          if (firstInput) firstInput.focus();
        }
        break;
      }
      case "/": e.preventDefault(); $("#search").focus(); break;
      case "?": toast("j/k nav · p promote · r reject · e edit · / search · cmd+enter promote"); break;
    }
  });
}

// ----- Boot ----------------------------------------------------------------

function boot() {
  bindFilters();
  bindBulkSelectAll();
  bindActions();
  bindKeyboard();
  resetAndLoad();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
