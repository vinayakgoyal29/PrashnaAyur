/**
 * doctor.js
 * =========
 * Doctor dashboard: login, SSE queue subscription, and detail view rendering.
 */

// ── DOM refs ──────────────────────────────────────────────────────────────────
const loginScreen    = document.getElementById("login-screen");
const dashScreen     = document.getElementById("dashboard-screen");
const doctorIdInput  = document.getElementById("doctor-id");
const doctorPinInput = document.getElementById("doctor-pin");
const loginError     = document.getElementById("login-error");
const btnLogin       = document.getElementById("btn-login");
const btnLogout      = document.getElementById("btn-logout");
const doctorLabel    = document.getElementById("doctor-label");
const sseDot         = document.getElementById("sse-dot");
const sseLabel       = document.getElementById("sse-label");
const queueList      = document.getElementById("queue-list");
const btnRefresh     = document.getElementById("btn-refresh");
const emptyState     = document.getElementById("empty-state");
const detailContent  = document.getElementById("detail-content");
const urgentBanner   = document.getElementById("urgent-banner");
const btnFhirExport  = document.getElementById("btn-fhir-export");

// Detail fields
const dName         = document.getElementById("d-name");
const dAge          = document.getElementById("d-age");
const dAbha         = document.getElementById("d-abha");
const dStatus       = document.getElementById("d-status");
const socratesTable = document.getElementById("socrates-table").querySelector("tbody");
const dConditions   = document.getElementById("d-conditions");
const dMedications  = document.getElementById("d-medications");
const dTranscript   = document.getElementById("d-transcript");

// ── State ─────────────────────────────────────────────────────────────────────
let records  = [];   // full list from /api/queue
let activeId = null; // currently selected record's bundle id
let sseConn  = null; // EventSource
let currentFhirRecord = null; // for FHIR export

// ─────────────────────────────────────────────────────────────────────────────
// LOGIN
// ─────────────────────────────────────────────────────────────────────────────
btnLogin.addEventListener("click", doLogin);
doctorPinInput.addEventListener("keydown", e => { if (e.key === "Enter") doLogin(); });

async function doLogin() {
  loginError.classList.remove("visible");
  const doctor_id = doctorIdInput.value.trim();
  const pin       = doctorPinInput.value.trim();

  if (!doctor_id || !pin) {
    loginError.textContent = "Please enter your Doctor ID and PIN.";
    loginError.classList.add("visible");
    return;
  }

  try {
    const resp = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ doctor_id, pin }),
      credentials: "include",
    });

    if (!resp.ok) {
      loginError.textContent = "Invalid credentials. Please try again.";
      loginError.classList.add("visible");
      return;
    }

    const data = await resp.json();
    doctorLabel.textContent = `Logged in as: ${data.doctor_id}`;
    loginScreen.classList.remove("active");
    dashScreen.classList.add("active");

    await loadInitialQueue();
    connectSSE();

  } catch (err) {
    loginError.textContent = "Network error. Please try again.";
    loginError.classList.add("visible");
    console.error("[login]", err);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// LOGOUT
// ─────────────────────────────────────────────────────────────────────────────
btnLogout.addEventListener("click", async () => {
  sseConn && sseConn.close();
  await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
  dashScreen.classList.remove("active");
  loginScreen.classList.add("active");
  records = [];
  activeId = null;
  queueList.innerHTML = "";
  clearDetail();
});

// ─────────────────────────────────────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────────────────────
// QUEUE LOADING & REFRESH (Directive #3 Section 6)
// ─────────────────────────────────────────────────────────────────────────────
async function fetchAndRenderQueue() {
  try {
    const resp = await fetch("/api/queue", { credentials: "include" });
    console.log(`[queue] GET /api/queue status: ${resp.status}`);
    if (resp.status === 401) {
      console.warn("[queue] Unauthorized (401). Session token missing or expired.");
      if (loginScreen && dashScreen) {
        dashScreen.classList.remove("active");
        loginScreen.classList.add("active");
        const loginErr = document.getElementById("login-error");
        if (loginErr) {
          loginErr.textContent = "Session expired. Please log in again.";
          loginErr.classList.add("visible");
        }
      }
      return;
    }
    if (!resp.ok) {
      const errText = await resp.text().catch(() => "");
      console.error(`[queue] Error response body:`, errText);
      throw new Error(`Queue fetch failed (${resp.status}): ${errText}`);
    }
    records = await resp.json();
    renderQueue();
    console.log(`[queue] Successfully loaded ${records.length} patient records.`);
  } catch (err) {
    console.error("[queue] Refresh failed:", err);
    if (queueList) {
      const existingNotice = document.getElementById("queue-fetch-error-notice");
      if (existingNotice) existingNotice.remove();

      const errNotice = document.createElement("div");
      errNotice.id = "queue-fetch-error-notice";
      errNotice.className = "qi-urgency urgency-emergency";
      errNotice.style.margin = "0.5rem";
      errNotice.style.padding = "0.75rem";
      errNotice.style.display = "flex";
      errNotice.style.justifyContent = "space-between";
      errNotice.style.alignItems = "center";
      errNotice.innerHTML = `
        <span>Failed to load queue.</span>
        <button id="queue-retry-action" style="background: white; color: #dc2626; border: 1px solid #dc2626; border-radius: 4px; padding: 2px 8px; cursor: pointer; font-weight: 600; font-size: 0.75rem;">Retry</button>
      `;
      queueList.prepend(errNotice);

      const retryBtn = document.getElementById("queue-retry-action");
      if (retryBtn) {
        retryBtn.addEventListener("click", (ev) => {
          ev.preventDefault();
          errNotice.remove();
          fetchAndRenderQueue();
        });
      }
      setTimeout(() => {
        if (errNotice.parentNode) errNotice.remove();
      }, 6000);
    }
  }
}

const loadInitialQueue = fetchAndRenderQueue;

// ─────────────────────────────────────────────────────────────────────────────
// SERVER-SENT EVENTS
// ─────────────────────────────────────────────────────────────────────────────
function connectSSE() {
  if (sseConn) sseConn.close();

  sseConn = new EventSource("/api/queue/stream", { withCredentials: true });

  sseConn.onopen = () => {
    sseDot.classList.add("connected");
    sseLabel.textContent = "Live";
  };

  sseConn.onerror = () => {
    sseDot.classList.remove("connected");
    sseLabel.textContent = "Reconnecting";
    // Browser handles EventSource reconnection automatically
  };

  function handleIncomingRecord(record) {
    // Deduplicate in records array (Directive #3 Section 3)
    const existingIdx = records.findIndex(r => r.encounter_id === record.encounter_id);
    if (existingIdx !== -1) {
      records[existingIdx] = record;
    } else {
      records.unshift(record); // newest first
    }
    prependQueueItem(record);
    console.log("[sse] Received and prepended patient encounter to queue:", record.encounter_id, record.token);
  }

  sseConn.onmessage = (e) => {
    try {
      const record = JSON.parse(e.data);
      handleIncomingRecord(record);
    } catch (err) {
      console.error("[sse] parse error:", err);
    }
  };

  sseConn.addEventListener("new_patient", (e) => {
    try {
      const record = JSON.parse(e.data);
      handleIncomingRecord(record);
    } catch (err) {
      console.error("[sse:new_patient] parse error:", err);
    }
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// QUEUE RENDERING (Directive #3 Section 3: Deduplicated DOM rendering)
// ─────────────────────────────────────────────────────────────────────────────
function renderQueue() {
  queueList.innerHTML = "";
  const seen = new Set();
  records.forEach(r => {
    if (!seen.has(r.encounter_id)) {
      seen.add(r.encounter_id);
      queueList.appendChild(buildQueueItem(r));
    }
  });
}

function prependQueueItem(record) {
  // Deduplicate in DOM by encounter_id (Directive #3 Section 3)
  const existing = queueList.querySelector(`[data-encounter-id="${record.encounter_id}"]`) || queueList.querySelector(`[data-id="${record.encounter_id}"]`);
  const newItem = buildQueueItem(record);
  if (existing) {
    existing.replaceWith(newItem);
  } else {
    queueList.prepend(newItem);
  }
  try {
    newItem.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (_) {}
}

function buildQueueItem(record) {
  const urgency = record.urgency || "ROUTINE";
  const name    = record.name || "Unknown";
  const abhaId  = record.abha_id || "-";
  const docs    = record.uploaded_doc_paths || [];
  const encounterId = record.encounter_id;
  const token   = record.token;

  const el = document.createElement("div");
  el.className = `queue-item`;
  el.dataset.id = encounterId;
  el.setAttribute("data-encounter-id", encounterId);

  let badgeColor = "#2e7d32"; // Green for ROUTINE
  let badgeText = "ROUTINE";
  if (urgency === "EMERGENCY") {
    badgeColor = "#c62828";
    badgeText = "EMERGENCY";
  } else if (urgency === "MODERATE") {
    badgeColor = "#f57c00";
    badgeText = "MODERATE";
  }

  let docIndicator = "";
  if (docs.length > 0) {
    docIndicator = `
      <div style="font-size:0.75rem; color:#666; margin-top:0.25rem; display:flex; align-items:center; gap:0.25rem;">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"></path>
        </svg>
        ${docs.length} file(s)
      </div>
    `;
  }

  const tokenBadge = token ? `<span style="background:#e8f5e9; color:#1b5e20; border:1px solid #a5d6a7; padding:0.1rem 0.35rem; border-radius:4px; font-size:0.75rem; font-weight:700; margin-right:0.35rem;">${esc(token)}</span>` : "";

  el.innerHTML = `
    <div style="display:flex; justify-content:space-between; align-items:flex-start;">
      <div class="qi-name">${tokenBadge}${esc(name)}</div>
      <div style="background:${badgeColor}; color:white; padding:0.15rem 0.4rem; border-radius:4px; font-size:0.7rem; font-weight:bold;">${badgeText}</div>
    </div>
    <div class="qi-sub" style="margin-top:0.25rem;">ABHA: ${esc(abhaId)}</div>
    ${docIndicator}
  `;

  el.addEventListener("click", () => selectRecord(encounterId));
  return el;
}

// ─────────────────────────────────────────────────────────────────────────────
// DETAIL VIEW
// ─────────────────────────────────────────────────────────────────────────────
const docModal = document.getElementById("doc-modal");
const docModalContent = document.getElementById("doc-modal-content");
const btnCloseModal = document.getElementById("btn-close-modal");
if (btnCloseModal) btnCloseModal.addEventListener("click", () => { docModal.style.display = "none"; });

const dDocuments = document.getElementById("d-documents");
const prescriptionSuite = document.getElementById("prescription-suite");
const btnAddRxRow = document.getElementById("btn-add-rx-row");
const rxRows = document.getElementById("rx-rows");
const rxPathya = document.getElementById("rx-pathya");

function showDrawerLoadError() {
  if (detailContent) {
    detailContent.innerHTML = `<div class="qi-urgency urgency-emergency" style="padding:1rem; margin:1rem;">Failed to load patient encounter details. Please try again.</div>`;
    detailContent.style.display = "flex";
  }
  if (emptyState) emptyState.style.display = "none";
}

function renderDocumentThumbnails(documents) {
  if (!dDocuments) return;
  dDocuments.innerHTML = "";
  if (!documents || documents.length === 0) {
    dDocuments.innerHTML = `<span style="color:#aaa">No documents attached.</span>`;
    return;
  }
  documents.forEach(doc => {
    const card = document.createElement("div");
    card.style.cssText = "display:flex; flex-direction:column; align-items:center; background:#fff; border:1px solid #d2e3fc; border-radius:8px; padding:0.5rem; gap:0.4rem; width:130px; box-shadow:0 1px 3px rgba(0,0,0,0.06);";

    const thumb = document.createElement("div");
    thumb.style.cssText = "width:100%; height:80px; border:1px solid #eee; border-radius:4px; overflow:hidden; cursor:pointer; background:#f8fafc; display:flex; align-items:center; justify-content:center; position:relative;";
    
    const fileUrl = doc.url || (doc.stored_filename ? `/api/records/file/${doc.stored_filename}` : (typeof doc === 'string' ? `/api/records/file/${doc}` : ''));
    const fileName = doc.original_filename || doc.stored_filename || (typeof doc === 'string' ? doc : 'Document');
    const ext = fileName.split('.').pop().toLowerCase();

    thumb.title = `Click to preview: ${fileName}`;

    if (ext === 'pdf') {
      thumb.innerHTML = `
        <div style="display:flex; flex-direction:column; align-items:center; gap:0.25rem;">
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#d32f2f" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
            <polyline points="14 2 14 8 20 8"></polyline>
            <line x1="16" y1="13" x2="8" y2="13"></line>
            <line x1="16" y1="17" x2="8" y2="17"></line>
            <polyline points="10 9 9 9 8 9"></polyline>
          </svg>
          <span style="font-size:0.68rem; font-weight:700; color:#d32f2f;">PDF</span>
        </div>
      `;
    } else {
      const img = document.createElement("img");
      img.src = fileUrl;
      img.alt = fileName;
      img.style.cssText = "width:100%; height:100%; object-fit:cover;";
      thumb.appendChild(img);
    }

    const openPreview = () => {
      if (!docModalContent) return;
      docModalContent.innerHTML = "";
      if (ext === 'pdf') {
        const iframe = document.createElement("iframe");
        iframe.src = fileUrl;
        iframe.style.cssText = "width:100%; height:100%; border:none;";
        docModalContent.appendChild(iframe);
      } else {
        const img = document.createElement("img");
        img.src = fileUrl;
        img.alt = fileName;
        img.style.cssText = "max-width:100%; max-height:80vh; object-fit:contain;";
        docModalContent.appendChild(img);
      }
      if (docModal) docModal.style.display = "flex";
    };

    thumb.addEventListener("click", openPreview);

    const nameLabel = document.createElement("div");
    nameLabel.style.cssText = "font-size:0.75rem; font-weight:600; color:#333; text-align:center; width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;";
    nameLabel.textContent = fileName;
    nameLabel.title = fileName;

    const actionsRow = document.createElement("div");
    actionsRow.style.cssText = "display:flex; gap:0.35rem; width:100%; justify-content:center;";

    const previewBtn = document.createElement("button");
    previewBtn.className = "btn btn-outline btn-sm";
    previewBtn.style.cssText = "padding:0.15rem 0.4rem; font-size:0.7rem; flex:1;";
    previewBtn.textContent = "View";
    previewBtn.addEventListener("click", openPreview);

    const downloadLink = document.createElement("a");
    downloadLink.href = fileUrl;
    downloadLink.target = "_blank";
    downloadLink.download = fileName;
    downloadLink.className = "btn btn-outline btn-sm";
    downloadLink.style.cssText = "padding:0.15rem 0.4rem; font-size:0.7rem; flex:1; text-align:center; text-decoration:none; display:inline-block;";
    downloadLink.textContent = "Get";
    downloadLink.title = "Download or open in new tab";

    actionsRow.appendChild(previewBtn);
    actionsRow.appendChild(downloadLink);

    card.appendChild(thumb);
    card.appendChild(nameLabel);
    card.appendChild(actionsRow);
    dDocuments.appendChild(card);
  });
}

async function openPatientDrawer(encounterId) {
  activeId = encounterId;

  // Highlight selected item in queue
  document.querySelectorAll(".queue-item").forEach(el => {
    el.classList.toggle("active", el.dataset.id === encounterId);
  });

  try {
    const response = await fetch(`/api/encounters/${encounterId}`, { credentials: "include" });
    if (!response.ok) {
      showDrawerLoadError();
      return;
    }
    const record = await response.json();
    currentFhirRecord = record;

    const isUrgent = record.urgency === "EMERGENCY";

    // Metrics
    dName.textContent    = record.name || "-";
    dAge.textContent     = record.age || "-";
    dAbha.textContent    = record.abha_id || "-";
    dStatus.textContent  = record.urgency || "ROUTINE";
    dStatus.className    = `metric-value${isUrgent ? " urgent" : ""}`;

    // Urgent banner
    urgentBanner.style.display = isUrgent ? "block" : "none";

    // SOCRATES table (Directive #4 Section 4.2)
    socratesTable.innerHTML = renderSocratesTable(record.structured_intake);

    // ABHA history
    const abhaData = record.abha_data || {};
    const conditions = (abhaData.chronic_conditions || []).map(c => {
      if (typeof c === "string") return c;
      if (c && typeof c === "object") {
        const cond = c.condition || c.name || "";
        const date = c.diagnosed_date || c.date || "";
        const status = c.status || "";
        if (cond && date && status) return `${cond} — ${date} (${status})`;
        if (cond && date) return `${cond} (${date})`;
        if (cond) return cond;
      }
      return String(c);
    }).filter(Boolean);

    const medications = (abhaData.past_medications || []).map(m => {
      if (typeof m === "string") return m;
      if (m && typeof m === "object") {
        const drug = m.drug || m.name || "";
        const dosage = m.dosage || "";
        const freq = m.frequency || "";
        if (drug && dosage && freq) return `${drug} ${dosage} — ${freq}`;
        if (drug && dosage) return `${drug} (${dosage})`;
        if (drug) return drug;
      }
      return String(m);
    }).filter(Boolean);

    renderList(dConditions, conditions, "No conditions on record.");
    renderList(dMedications, medications, "No medications on record.");

    // Transcript (Directive #4 Section 4.2)
    dTranscript.innerHTML = renderTranscript(record);

    // Documents (Directive #4 Section 4.2)
    renderDocumentThumbnails(record.documents || []);

    // Prescription State
    if (record.status === "DISCHARGED") {
      btnFinalizeRx.style.display = "none";
      btnAddRxRow.style.display = "none";
      rxConfirmation.style.display = "none";
      document.querySelectorAll(".rx-row input, #rx-pathya").forEach(el => el.disabled = true);
    } else {
      btnFinalizeRx.style.display = "inline-block";
      btnAddRxRow.style.display = "inline-block";
      rxConfirmation.style.display = "none";
      document.querySelectorAll(".rx-row input, #rx-pathya").forEach(el => {
        el.disabled = false;
        el.value = "";
      });
      const rowsList = document.querySelectorAll(".rx-row");
      for (let i = 1; i < rowsList.length; i++) rowsList[i].remove();
    }

    emptyState.style.display  = "none";
    detailContent.style.display = "flex";

  } catch (err) {
    console.error("[drawer] Failed to fetch encounter details:", err);
    showDrawerLoadError();
  }
}

const btnFinalizeRx = document.getElementById("btn-finalize-rx");
const rxConfirmation = document.getElementById("rx-confirmation");
const rxConfirmText = document.getElementById("rx-confirm-text");

btnAddRxRow.addEventListener("click", () => {
  const row = document.createElement("div");
  row.className = "rx-row";
  row.style.cssText = "display:flex; gap:0.5rem; margin-bottom:0.5rem;";
  row.innerHTML = `
    <input type="text" placeholder="Medicine Name" class="rx-med" style="flex:1">
    <input type="text" placeholder="Dose" class="rx-dose" style="flex:1">
    <input type="text" placeholder="Vehicle/Anupana" class="rx-vehicle" style="flex:1">
    <input type="text" placeholder="Frequency" class="rx-freq" style="flex:1">
  `;
  rxRows.appendChild(row);
});

btnFinalizeRx.addEventListener("click", async () => {
  if (!activeId) {
    alert("Please select a patient from the queue before prescribing.");
    return;
  }
  const rows = [];
  document.querySelectorAll(".rx-row").forEach(r => {
    const medEl = r.querySelector(".rx-med");
    const med = medEl ? medEl.value.trim() : "";
    if (med) {
      rows.push({
        medicine_name: med,
        dose: (r.querySelector(".rx-dose") ? r.querySelector(".rx-dose").value.trim() : "") || "1 dose",
        vehicle: (r.querySelector(".rx-vehicle") ? r.querySelector(".rx-vehicle").value.trim() : "") || "Warm water",
        frequency: (r.querySelector(".rx-freq") ? r.querySelector(".rx-freq").value.trim() : "") || "Twice daily"
      });
    }
  });

  const payload = {
    encounter_id: activeId,
    ayush_rx: rows,
    pathya_apathya: rxPathya ? rxPathya.value.trim() : ""
  };

  try {
    console.log("[rx] Finalizing prescription for encounter:", activeId, "Payload:", JSON.stringify(payload, null, 2));
    const resp = await fetch("/api/prescription/finalize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      credentials: "include"
    });
    console.log(`[rx] POST /api/prescription/finalize status: ${resp.status}`);
    const respText = await resp.text();
    console.log(`[rx] POST /api/prescription/finalize response body:`, respText);

    let respJson = null;
    try {
      respJson = JSON.parse(respText);
    } catch (_) {}

    if (!resp.ok) {
      const detail = respJson ? (respJson.detail || JSON.stringify(respJson)) : respText;
      console.error(`[rx] Prescription finalize failed (${resp.status}):`, detail);
      throw new Error(`(${resp.status}) ${detail}`);
    }

    const data = respJson || {};
    console.log("[rx] Prescription finalized successfully:", data);
    
    // Update local state
    const record = records.find(r => r.encounter_id === activeId);
    if (record) record.status = "DISCHARGED";

    // Update queue DOM item badge if present
    const qItem = queueList.querySelector(`[data-id="${activeId}"]`) || queueList.querySelector(`[data-encounter-id="${activeId}"]`);
    if (qItem) {
      const badge = qItem.querySelector(".qi-name + div");
      if (badge) {
        badge.textContent = "DISCHARGED";
        badge.style.background = "#78909c";
      }
    }

    // Show success
    btnFinalizeRx.style.display = "none";
    btnAddRxRow.style.display = "none";
    document.querySelectorAll(".rx-row input, #rx-pathya").forEach(el => el.disabled = true);
    
    rxConfirmText.innerHTML = `<strong>Prescription Finalized & Linked</strong><br>Prescription ID: <code>${data.prescription_id}</code><br>Care Context: <code>${data.care_context_id}</code><br>Linked to ABHA: <code>${data.abha_id}</code>`;
    rxConfirmation.style.display = "block";
    
  } catch (err) {
    alert(`Error finalizing prescription: ${err.message}`);
    console.error("[rx] Finalize prescription exception:", err);
  }
});

function selectRecord(encounterId) {
  openPatientDrawer(encounterId);
}

function clearDetail() {
  emptyState.style.display  = "block";
  detailContent.style.display = "none";
  currentFhirRecord = null;
}

function renderList(container, items, emptyMsg) {
  container.innerHTML = "";
  if (!items.length) {
    const li = document.createElement("li");
    li.textContent = emptyMsg;
    li.style.color = "#aaa";
    container.appendChild(li);
  } else {
    items.forEach(text => {
      const li = document.createElement("li");
      li.textContent = text;
      container.appendChild(li);
    });
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// FHIR EXPORT
// ─────────────────────────────────────────────────────────────────────────────
btnFhirExport.addEventListener("click", () => {
  if (!currentFhirRecord) return;
  const json = JSON.stringify(currentFhirRecord, null, 2);
  const blob = new Blob([json], { type: "application/json" });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement("a");
  a.href     = url;
  a.download = `prashnaayur_fhir_${currentFhirRecord.id?.slice(0, 8) ?? "bundle"}.json`;
  a.click();
  URL.revokeObjectURL(url);
});

// ─────────────────────────────────────────────────────────────────────────────
// UTILS
// ─────────────────────────────────────────────────────────────────────────────
function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ─────────────────────────────────────────────────────────────────────────────
// SOCRATES & TRANSCRIPT RENDERERS (Directive #3 Sections 4.1 & 5)
// ─────────────────────────────────────────────────────────────────────────────
function renderSocratesTable(structured) {
  const s = structured || {};
  const rows = [
    ["Site", s.site],
    ["Onset", s.onset],
    ["Character", s.character],
    ["Radiation", s.radiation],
    ["Associations", s.associations],
    ["Time Course", s.time_course],
    ["Exacerbating / Relieving", s.exacerbating_relieving_factors],
    ["Severity", s.severity],
    ["Prakriti Notes", s.prakriti_notes],
  ];
  return rows.map(([label, value]) =>
    `<tr><td>${esc(label)}</td><td>${esc(value ?? 'Not assessed')}</td></tr>`
  ).join('');
}

function renderTranscript(record) {
  const transcript = (record && record.transcript) ? record.transcript : [];
  if (!Array.isArray(transcript) || transcript.length === 0) {
    return '<p class="text-slate-500" style="color:#888; font-style:italic; padding:0.5rem 0;">No transcript available.</p>';
  }
  return transcript.map(t => {
    const role = (t.role === "patient" || t.role === "user") ? "patient" : "assistant";
    const label = role === "patient" ? "Patient" : "Assistant";
    return `<div class="tx-line ${role}"><strong>${label}:</strong> ${esc(t.text)}</div>`;
  }).join('');
}

// ─────────────────────────────────────────────────────────────────────────────
// INITIALIZATION & RESILIENCE (Directive #3 Section 6)
// ─────────────────────────────────────────────────────────────────────────────
async function checkActiveSession() {
  try {
    const resp = await fetch("/api/queue", { credentials: "include" });
    if (resp.ok) {
      records = await resp.json();
      loginScreen.classList.remove("active");
      dashScreen.classList.add("active");
      renderQueue();
      connectSSE();
      console.log(`[init] Active doctor session restored. Loaded ${records.length} patients.`);
    }
  } catch (err) {
    console.log("[init] No active session found or network error:", err);
  }
}

function initDashboard() {
  try {
    const rBtns = [
      document.getElementById("btn-refresh"),
      document.getElementById("refresh-queue-btn")
    ];
    rBtns.forEach(btn => {
      if (btn) {
        btn.addEventListener("click", (e) => {
          e.preventDefault();
          fetchAndRenderQueue();
        });
      }
    });
  } catch (e) {
    console.error("Refresh button init failed:", e);
  }

  try {
    checkActiveSession();
  } catch (e) {
    console.error("Active session check failed:", e);
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initDashboard);
} else {
  initDashboard();
}
