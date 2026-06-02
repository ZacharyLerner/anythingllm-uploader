/* ========== Upload: Drop Zone + Auto-Submit ========== */

const dropZone = document.getElementById("dropZone");
const fileInput = document.getElementById("fileInput");
const uploadCounter = document.getElementById("uploadCounter");
const fileDisplay = document.querySelector(".file_display");
const uploadUrl = dropZone.dataset.url;
const MAX_UPLOAD_BYTES = parseInt(dropZone.dataset.maxBytes, 10) || Infinity;

let isUploading = false;
let isScraping = false;

// Format bytes into a human-readable string
function formatBytes(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

// Warn before closing/navigating away during an active upload or scrape
window.addEventListener("beforeunload", (e) => {
    if (isUploading || isScraping) {
        e.preventDefault();
    }
});

// Click the zone to open file picker
dropZone.addEventListener("click", () => fileInput.click());

// Drag visual feedback
dropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropZone.classList.add("dragover");
});
dropZone.addEventListener("dragleave", () => {
    dropZone.classList.remove("dragover");
});

// Drop files -> upload immediately
dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("dragover");
    if (e.dataTransfer.files.length) {
        uploadFiles(e.dataTransfer.files);
    }
});

// File picker selection -> upload immediately
fileInput.addEventListener("change", () => {
    if (fileInput.files.length) {
        uploadFiles(fileInput.files);
        fileInput.value = "";
    }
});

async function uploadFiles(files) {
    isUploading = true;
    const formData = new FormData();
    const totalFiles = files.length;
    let completedCount = 0;
    let failedCount = 0;

    const rows = {};
    const steps = { uploaded: 25, converted: 50, embedded: 75, done: 100 };
    const statusLabels = {
        uploaded: "Uploading...",
        converted: "Converting...",
        embedded: "Embedding...",
        done: "Complete",
    };

    function esc(str) {
        const d = document.createElement("div");
        d.textContent = str;
        return d.innerHTML;
    }

    function updateCounter() {
        let text = `Uploading ${totalFiles} file${totalFiles > 1 ? "s" : ""} \u2014 ${completedCount}/${totalFiles} complete`;
        if (failedCount > 0) text += ` (${failedCount} failed)`;
        uploadCounter.textContent = text;
    }

    updateCounter();

    function markRowError(row, message) {
        row.classList.remove("uploading");
        row.classList.add("upload-error");
        row.querySelector(".file-info .date").textContent = message || "Upload failed";
        const indicator = row.querySelector(".upload-indicator");
        if (indicator) indicator.remove();
        failedCount++;
        updateCounter();
    }

    let validCount = 0;

    for (const file of files) {
        const row = document.createElement("div");
        row.className = "file_card uploading";
        row.innerHTML = `
            <div class="file-info">
                <div class="filename">${esc(file.name)}</div>
                <div class="date">Waiting...</div>
            </div>
            <div class="upload-indicator">
                <div class="progress-bar-track">
                    <div class="progress-bar-fill"></div>
                </div>
                <span class="progress-label">0%</span>
            </div>
        `;
        fileDisplay.prepend(row);
        rows[file.name] = row;

        if (file.size > MAX_UPLOAD_BYTES) {
            const limitStr = formatBytes(MAX_UPLOAD_BYTES);
            const sizeStr = formatBytes(file.size);
            markRowError(row, `File is ${sizeStr} \u2014 exceeds ${limitStr} limit`);
        } else {
            formData.append("uploaded_files", file);
            validCount++;
        }
    }

    fileDisplay.closest(".section-body").scrollTop = 0;

    if (validCount === 0) {
        uploadCounter.textContent = `${failedCount} file${failedCount > 1 ? "s" : ""} rejected (too large)`;
        setTimeout(() => { uploadCounter.textContent = ""; }, 4000);
        isUploading = false;
        return;
    }

    const res = await fetch(uploadUrl, { method: "POST", body: formData });

    if (!res.ok) {
        for (const [name, row] of Object.entries(rows)) {
            if (!row.classList.contains("upload-error")) {
                markRowError(row, "Upload failed");
            }
        }
        uploadCounter.textContent = "Upload failed";
        setTimeout(() => { uploadCounter.textContent = ""; }, 3000);
        isUploading = false;
        return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();

        for (const line of lines) {
            if (!line.startsWith("data: ")) continue;
            const payload = line.slice(6).trim();
            if (!payload) continue;

            if (payload === "[DONE]") {
                let summary = `${completedCount} file${completedCount !== 1 ? "s" : ""} uploaded`;
                if (failedCount > 0) summary += `, ${failedCount} failed`;
                uploadCounter.textContent = summary;
                setTimeout(() => { uploadCounter.textContent = ""; }, 3000);
                rebuildCheckboxes();
                refreshFilterBar();
                isUploading = false;
                return;
            }

            try {
                const event = JSON.parse(payload);
                const row = rows[event.file];
                if (!row) continue;

                if (event.status === "error") {
                    markRowError(row, event.message || "Processing failed");
                } else if (event.status === "done") {
                    completedCount++;
                    updateCounter();
                    transformToCompleted(row, event);
                } else {
                    const fill = row.querySelector(".progress-bar-fill");
                    const label = row.querySelector(".progress-label");
                    const dateEl = row.querySelector(".file-info .date");
                    const pct = steps[event.status] || 0;
                    fill.style.width = pct + "%";
                    label.textContent = pct + "%";
                    dateEl.textContent = statusLabels[event.status] || event.status;
                }
            } catch (e) {
                console.warn("Skipping unparseable payload:", payload);
            }
        }
    }
}

function transformToCompleted(row, event) {
    const fileId = event.location;
    const displayName = event.name;
    const ext = (event.original_extension || "").replace(".", "");

    function esc(str) {
        const d = document.createElement("div");
        d.textContent = str;
        return d.innerHTML;
    }

    const now = new Date();
    const dateStr = now.toLocaleString("en-US", {
        month: "short", day: "numeric", year: "numeric",
        hour: "numeric", minute: "2-digit",
    });

    row.className = "file_card just-completed";
    row.setAttribute("data-extension", event.original_extension || "");
    row.setAttribute("data-filename", displayName);
    row.setAttribute("data-date", now.toISOString());
    row.innerHTML = `
        <input type="checkbox" class="file-checkbox" data-id="${esc(fileId)}">
        <span class="ext-badge ext-${esc(ext)}">${esc(ext.toUpperCase() || "?")}</span>
        <div class="file-info">
            <div class="filename">${esc(displayName)}</div>
            <div class="date">${dateStr}</div>
        </div>
        <button class="btn-delete" data-id="${esc(fileId)}">Delete</button>
    `;

    attachDeleteHandler(row.querySelector(".btn-delete"));
    applyCurrentFilter();

    row.addEventListener("animationend", () => {
        row.classList.remove("just-completed");
    }, { once: true });
}

/* ========== Delete Handlers ========== */

function attachDeleteHandler(btn) {
    btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm("Delete this document?")) return;

        const res = await fetch(`/delete/${btn.dataset.id}`, { method: "DELETE" });

        if (res.ok) {
            const card = btn.closest(".file_card");
            card.remove();
            rebuildCheckboxes();
            updateSelection();
            refreshFilterBar();
        }
    });
}

document.querySelectorAll(".file_display .btn-delete").forEach(attachDeleteHandler);

/* ========== Checkboxes: Shift-Click Multi-Select ========== */

const bulkActions = document.getElementById("bulkActions");
const selectedCountEl = document.getElementById("selectedCount");
let checkboxes = Array.from(fileDisplay.querySelectorAll(".file-checkbox"));
let lastCheckedIndex = null;

function rebuildCheckboxes() {
    checkboxes = Array.from(fileDisplay.querySelectorAll(".file-checkbox"));
    lastCheckedIndex = null;

    checkboxes.forEach((cb, index) => {
        const newCb = cb.cloneNode(true);
        cb.parentNode.replaceChild(newCb, cb);
        checkboxes[index] = newCb;

        newCb.addEventListener("click", (e) => {
            if (e.shiftKey && lastCheckedIndex !== null) {
                const start = Math.min(lastCheckedIndex, index);
                const end = Math.max(lastCheckedIndex, index);
                const checked = newCb.checked;
                for (let i = start; i <= end; i++) checkboxes[i].checked = checked;
            }
            lastCheckedIndex = index;
            updateSelection();
        });
    });
}

function updateSelection() {
    const selected = checkboxes.filter((cb) => cb.checked);
    checkboxes.forEach((cb) => {
        cb.closest(".file_card").classList.toggle("selected", cb.checked);
    });
    if (selected.length > 0) {
        bulkActions.classList.add("visible");
        selectedCountEl.textContent = selected.length;
    } else {
        bulkActions.classList.remove("visible");
    }
    syncSelectAllCheckbox();
}

rebuildCheckboxes();

/* ========== Select All ========== */

const selectAllCb = document.getElementById("selectAllCb");

selectAllCb.addEventListener("change", () => {
    const isChecked = selectAllCb.checked;
    checkboxes.forEach((cb) => {
        const card = cb.closest(".file_card");
        if (!card.classList.contains("filtered-out")) cb.checked = isChecked;
    });
    updateSelection();
});

function syncSelectAllCheckbox() {
    const visible = checkboxes.filter((cb) => !cb.closest(".file_card").classList.contains("filtered-out"));
    if (visible.length === 0) {
        selectAllCb.checked = false;
        selectAllCb.indeterminate = false;
        return;
    }
    const checkedCount = visible.filter((cb) => cb.checked).length;
    if (checkedCount === 0) {
        selectAllCb.checked = false;
        selectAllCb.indeterminate = false;
    } else if (checkedCount === visible.length) {
        selectAllCb.checked = true;
        selectAllCb.indeterminate = false;
    } else {
        selectAllCb.checked = false;
        selectAllCb.indeterminate = true;
    }
}

/* ========== Bulk Delete ========== */

const deleteOverlay = document.getElementById("deleteOverlay");
const deleteOverlayTitle = document.getElementById("deleteOverlayTitle");
const deleteOverlayMsg = document.getElementById("deleteOverlayMsg");
const bulkDeleteBtn = document.getElementById("bulkDeleteBtn");

bulkDeleteBtn.addEventListener("click", async () => {
    const selected = checkboxes.filter((cb) => cb.checked);
    const fileIds = selected.map((cb) => cb.dataset.id);
    if (!fileIds.length) return;

    const count = fileIds.length;
    if (!confirm(`Delete ${count} document${count > 1 ? "s" : ""}?`)) return;

    bulkDeleteBtn.disabled = true;
    bulkDeleteBtn.innerHTML = '<span class="btn-spinner"></span>Deleting...';
    deleteOverlayTitle.textContent = `Deleting ${count} file${count > 1 ? "s" : ""}...`;
    deleteOverlayMsg.textContent = count >= 5
        ? "This may take a moment for larger selections."
        : "Removing from workspace...";
    deleteOverlay.classList.add("visible");

    const res = await fetch("/delete-bulk", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file_ids: fileIds }),
    });

    if (res.ok) {
        const result = await res.json();
        const deletedIds = new Set(result.deleted || []);
        selected.forEach((cb) => {
            if (deletedIds.has(cb.dataset.id)) cb.closest(".file_card").remove();
        });
        rebuildCheckboxes();
        updateSelection();
        refreshFilterBar();
    } else {
        alert("Failed to delete selected documents.");
    }

    bulkDeleteBtn.disabled = false;
    bulkDeleteBtn.textContent = "Delete Selected";
    deleteOverlay.classList.remove("visible");
});

/* ==========================================================================
   WEB SCRAPING — Form (Phase 1: Discover, Phase 2: Save Job)
   ========================================================================== */

const scrapeArea = document.getElementById("scrapeArea");
const scrapeBtn = document.getElementById("scrapeBtn");
const scrapeUrlInput = document.getElementById("scrapeUrl");
const scrapeJobNameInput = document.getElementById("scrapeJobName");
const scrapeFormExtras = document.getElementById("scrapeFormExtras");
const scrapeAdvancedToggle = document.getElementById("scrapeAdvancedToggle");
const scrapeAdvancedPanel = document.getElementById("scrapeAdvanced");
const scrapeMaxPagesInput = document.getElementById("scrapeMaxPages");
const scrapeStayOnDomainCb = document.getElementById("scrapeStayOnDomain");

const scrapeDiscovery = document.getElementById("scrapeDiscovery");
const scrapeDiscoveryCount = document.getElementById("scrapeDiscoveryCount");
const scrapeUrlList = document.getElementById("scrapeUrlList");
const scrapeSelectAllCb = document.getElementById("scrapeSelectAllCb");
const scrapeProcessBtn = document.getElementById("scrapeProcessBtn");
const scrapeCancelBtn = document.getElementById("scrapeCancelBtn");
const scrapeSaveSchedule = document.getElementById("scrapeSaveSchedule");

const discoverUrl = scrapeArea.dataset.discoverUrl;
const jobsUrl = scrapeArea.dataset.jobsUrl;

let discoveredUrls = [];
let selectedScope = null;
let selectedDepth = 1;

// Escape HTML to prevent XSS
function escHtml(str) {
    const d = document.createElement("div");
    d.textContent = str;
    return d.innerHTML;
}

/* ---------- Form extras visibility ---------- */

function updateFormVisibility() {
    const hasUrl = scrapeUrlInput.value.trim().length > 0;
    const hasName = scrapeJobNameInput.value.trim().length > 0;
    scrapeFormExtras.classList.toggle("visible", hasUrl && hasName);
}

/* ---------- Advanced Options Toggle ---------- */

scrapeAdvancedToggle.addEventListener("click", () => {
    scrapeAdvancedToggle.classList.toggle("open");
    scrapeAdvancedPanel.classList.toggle("open");
});

/* ---------- Scope Card Selection ---------- */

const scopeCards = document.getElementById("scopeCards");
const scrapeDepthPanel = document.getElementById("scrapeDepthPanel");

scopeCards.addEventListener("click", (e) => {
    const card = e.target.closest(".scrape-scope-card");
    if (!card) return;
    scopeCards.querySelectorAll(".scrape-scope-card").forEach((c) => c.classList.remove("active"));
    card.classList.add("active");
    selectedScope = card.dataset.scope;
    scrapeDepthPanel.classList.toggle("visible", selectedScope === "links");
    updatePreview();
    updateSubmitBtn();
});

/* ---------- Depth Button Selection ---------- */

const scrapeDepthBtns = document.getElementById("scrapeDepthBtns");

scrapeDepthBtns.addEventListener("click", (e) => {
    const btn = e.target.closest(".scrape-depth-btn");
    if (!btn) return;
    scrapeDepthBtns.querySelectorAll(".scrape-depth-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    selectedDepth = parseInt(btn.dataset.depth, 10);
    updatePreview();
});

/* ---------- Live Preview ---------- */

const scrapePreviewDot = document.getElementById("scrapePreviewDot");
const scrapePreviewText = document.getElementById("scrapePreviewText");

function updatePreview() {
    const url = scrapeUrlInput.value.trim();
    const jobName = scrapeJobNameInput.value.trim();
    const maxVal = parseInt(scrapeMaxPagesInput.value, 10);
    const maxLabel = maxVal === 0 ? "no limit" : maxVal;
    const stayOnDomain = scrapeStayOnDomainCb.checked;
    const nameSuffix = jobName ? ` (${jobName})` : "";
    const domainSuffix = stayOnDomain ? ", staying on this domain" : "";

    if (!url && !selectedScope) {
        scrapePreviewDot.className = "scrape-preview-dot";
        scrapePreviewText.textContent = "Enter a URL and choose a scope to get started.";
        return;
    }
    if (!selectedScope) {
        scrapePreviewDot.className = "scrape-preview-dot";
        scrapePreviewText.textContent = "Choose a scope above.";
        return;
    }
    if (!url) {
        scrapePreviewDot.className = "scrape-preview-dot";
        scrapePreviewText.textContent = "Enter a URL above.";
        return;
    }

    scrapePreviewDot.className = "scrape-preview-dot active";

    if (selectedScope === "single") {
        scrapePreviewText.textContent = `Will add 1 page${nameSuffix}: ${url}`;
    } else if (selectedScope === "links") {
        const n = selectedDepth;
        scrapePreviewText.textContent =
            `Will follow links ${n} level${n !== 1 ? "s" : ""} deep from this page (up to ${maxLabel} pages)${nameSuffix}${domainSuffix}.`;
    } else if (selectedScope === "prefix") {
        let pathname = "/";
        try {
            let p = new URL(url).pathname || "/";
            if (!p.endsWith("/")) p = p.replace(/\/[^/]*$/, "/") || "/";
            pathname = p;
        } catch (_) {}
        scrapePreviewText.textContent =
            `Will add all pages under ${pathname} (up to ${maxLabel} pages)${nameSuffix}${domainSuffix}.`;
    }
}

function updateSubmitBtn() {
    const hasUrl = scrapeUrlInput.value.trim().length > 0;
    scrapeBtn.disabled = !(hasUrl && selectedScope);
}

scrapeUrlInput.addEventListener("input", () => { updateFormVisibility(); updatePreview(); updateSubmitBtn(); });
scrapeJobNameInput.addEventListener("input", () => { updateFormVisibility(); updatePreview(); });
scrapeMaxPagesInput.addEventListener("input", updatePreview);
scrapeStayOnDomainCb.addEventListener("change", updatePreview);

/* ---------- Form Reset ---------- */

function resetScrapeForm() {
    scrapeUrlInput.value = "";
    scrapeJobNameInput.value = "";
    selectedScope = null;
    selectedDepth = 1;
    scrapeFormExtras.classList.remove("visible");
    scopeCards.querySelectorAll(".scrape-scope-card").forEach((c) => c.classList.remove("active"));
    scrapeDepthPanel.classList.remove("visible");
    scrapeDepthBtns.querySelectorAll(".scrape-depth-btn").forEach((b, i) => {
        b.classList.toggle("active", i === 0);
    });
    scrapeBtn.textContent = "Discover pages";
    scrapeBtn.disabled = true;
    scrapeSaveSchedule.value = "";
    updatePreview();
}

/* ---------- Phase 1: Discover Pages ---------- */

scrapeBtn.addEventListener("click", async () => {
    const baseUrl = scrapeUrlInput.value.trim();

    scrapeDiscovery.classList.remove("visible");
    scrapeUrlList.innerHTML = "";
    discoveredUrls = [];

    scrapeBtn.disabled = true;
    scrapeBtn.innerHTML = '<span class="btn-spinner"></span> Searching&hellip;';

    const mode = selectedScope === "prefix" ? "prefix" : "depth";
    const max_depth = selectedScope === "links" ? selectedDepth : (selectedScope === "single" ? 0 : 1);
    const maxPagesVal = parseInt(scrapeMaxPagesInput.value, 10);

    const payload = {
        base_url: baseUrl,
        mode,
        max_depth,
        max_pages: maxPagesVal === 0 ? 10000 : maxPagesVal,
        allow_offsite: !scrapeStayOnDomainCb.checked,
    };

    try {
        const res = await fetch(discoverUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });

        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || `Discovery failed (${res.status})`);
        }

        const data = await res.json();
        discoveredUrls = data.urls || [];
        const blockedUrls = data.blocked || [];

        if (discoveredUrls.length === 0 && blockedUrls.length === 0) {
            scrapeDiscoveryCount.textContent = "No pages found";
            scrapeUrlList.innerHTML = '<div class="scrape-url-empty">No downloadable pages were found at this URL.</div>';
            scrapeProcessBtn.style.display = "none";
            scrapeDiscovery.classList.add("visible");
        } else {
            renderDiscoveredUrls(blockedUrls);
            scrapeDiscovery.classList.add("visible");
        }
    } catch (err) {
        console.error("Scrape discovery error:", err);
        alert("Failed to find pages: " + err.message);
    } finally {
        scrapeBtn.disabled = false;
        scrapeBtn.textContent = "Discover pages";
    }
});

function renderDiscoveredUrls(blockedUrls = []) {
    scrapeUrlList.innerHTML = "";
    scrapeSelectAllCb.checked = true;
    scrapeProcessBtn.style.display = discoveredUrls.length > 0 ? "" : "none";

    discoveredUrls.forEach((url, i) => {
        const item = document.createElement("div");
        item.className = "scrape-url-item";
        item.innerHTML = `
            <input type="checkbox" class="scrape-url-cb" data-index="${i}" checked>
            <span class="url-text" title="${escHtml(url)}">${escHtml(url)}</span>
        `;
        scrapeUrlList.appendChild(item);
    });

    if (blockedUrls.length > 0) {
        const divider = document.createElement("div");
        divider.className = "scrape-url-blocked-header";
        divider.innerHTML = `
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>
            ${blockedUrls.length} page${blockedUrls.length !== 1 ? "s" : ""} blocked by website host
        `;
        scrapeUrlList.appendChild(divider);
        blockedUrls.forEach((url) => {
            const item = document.createElement("div");
            item.className = "scrape-url-item scrape-url-item--blocked";
            item.innerHTML = `<span class="url-text" title="${escHtml(url)}">${escHtml(url)}</span>`;
            scrapeUrlList.appendChild(item);
        });
    }

    updateDiscoveryCount();

    scrapeUrlList.querySelectorAll(".scrape-url-cb").forEach((cb) => {
        cb.addEventListener("change", updateDiscoveryCount);
    });
}

function updateDiscoveryCount() {
    const cbs = scrapeUrlList.querySelectorAll(".scrape-url-cb");
    const checkedCount = Array.from(cbs).filter((cb) => cb.checked).length;
    const total = discoveredUrls.length;
    scrapeDiscoveryCount.textContent = `Found ${total} page${total !== 1 ? "s" : ""}`;
    scrapeProcessBtn.textContent = `Save job & scrape now (${checkedCount})`;
    scrapeProcessBtn.disabled = checkedCount === 0;

    if (checkedCount === 0) {
        scrapeSelectAllCb.checked = false;
        scrapeSelectAllCb.indeterminate = false;
    } else if (checkedCount === cbs.length) {
        scrapeSelectAllCb.checked = true;
        scrapeSelectAllCb.indeterminate = false;
    } else {
        scrapeSelectAllCb.checked = false;
        scrapeSelectAllCb.indeterminate = true;
    }
}

scrapeSelectAllCb.addEventListener("change", () => {
    const checked = scrapeSelectAllCb.checked;
    scrapeUrlList.querySelectorAll(".scrape-url-cb").forEach((cb) => { cb.checked = checked; });
    updateDiscoveryCount();
});

scrapeCancelBtn.addEventListener("click", () => {
    scrapeDiscovery.classList.remove("visible");
    discoveredUrls = [];
    scrapeUrlList.innerHTML = "";
});

/* ---------- Phase 2: Save Job + Immediately Run via SSE ---------- */

scrapeProcessBtn.addEventListener("click", async () => {
    const cbs = scrapeUrlList.querySelectorAll(".scrape-url-cb:checked");
    // We only use selectedUrls for the initial count display; the backend re-discovers
    const selectedUrlCount = cbs.length;
    if (selectedUrlCount === 0) return;

    const jobName = scrapeJobNameInput.value.trim() || "Untitled Job";
    const schedule = scrapeSaveSchedule.value || null;
    const mode = selectedScope === "prefix" ? "prefix" : (selectedScope === "single" ? "single" : "depth");
    const maxPagesVal = parseInt(scrapeMaxPagesInput.value, 10);

    isScraping = true;
    scrapeProcessBtn.disabled = true;
    scrapeProcessBtn.textContent = "Saving\u2026";
    scrapeBtn.disabled = true;

    // 1. Create the job
    let job;
    try {
        const res = await fetch(jobsUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                name: jobName,
                base_url: scrapeUrlInput.value.trim(),
                mode: mode,
                max_depth: selectedScope === "links" ? selectedDepth : (selectedScope === "single" ? 0 : 1),
                max_pages: maxPagesVal === 0 ? 10000 : maxPagesVal,
                allow_offsite: !scrapeStayOnDomainCb.checked,
                schedule_interval: schedule,
            }),
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || "Failed to save job");
        }
        job = await res.json();
    } catch (err) {
        alert("Failed to create scrape job: " + err.message);
        isScraping = false;
        scrapeProcessBtn.disabled = false;
        scrapeProcessBtn.textContent = "Save job & scrape now";
        scrapeBtn.disabled = false;
        return;
    }

    // 2. Insert card immediately into the UI (in running state)
    const workspaceId = document.getElementById("scrapeJobsPanel").dataset.workspaceId;
    insertJobCard(job, workspaceId, true);

    // Remove empty state notice if present
    const emptyNotice = document.getElementById("scrapeJobsEmpty");
    if (emptyNotice) emptyNotice.remove();

    // 3. Collapse the discovery panel and reset form
    scrapeDiscovery.classList.remove("visible");
    resetScrapeForm();
    scrapeProcessBtn.disabled = false;
    scrapeProcessBtn.textContent = "Save job & scrape now";

    // 4. Run the job via SSE — streaming into the new card
    isScraping = false;
    await runJobById(job.id, workspaceId);
});

/* ==========================================================================
   SCRAPE JOB CARDS
   ========================================================================== */

const scrapeJobsPanel = document.getElementById("scrapeJobsPanel");
const scrapeJobsList = document.getElementById("scrapeJobsList");

function formatJobDate(isoStr) {
    if (!isoStr) return "Never";
    const d = new Date(isoStr);
    return d.toLocaleString("en-US", {
        month: "short", day: "numeric", year: "numeric",
        hour: "numeric", minute: "2-digit",
    });
}

function insertJobCard(job, workspaceId, startRunning = false) {
    // Remove the empty-state message if present
    const empty = document.getElementById("scrapeJobsEmpty");
    if (empty) empty.remove();

    const card = document.createElement("div");
    card.className = "scrape-job-card" + (startRunning ? " job-running" : "");
    card.dataset.jobId = job.id;
    card.dataset.runUrl = `/${workspaceId}/scrape/jobs/${job.id}/run`;
    card.dataset.patchUrl = `/${workspaceId}/scrape/jobs/${job.id}`;
    card.dataset.deleteUrl = `/${workspaceId}/scrape/jobs/${job.id}`;

    const scheduleOpts = [
        { value: "", label: "Manual" },
        { value: "hourly", label: "Hourly" },
        { value: "daily", label: "Daily" },
        { value: "weekly", label: "Weekly" },
    ];
    const scheduleOptions = scheduleOpts.map(o =>
        `<option value="${o.value}"${(job.schedule_interval || "") === o.value ? " selected" : ""}>${o.label}</option>`
    ).join("");

    const nextRun = job.schedule_interval && job.next_scrape_at
        ? formatJobDate(job.next_scrape_at)
        : "Manual only";

    card.innerHTML = `
        <div class="scrape-job-card-header">
            <div class="scrape-job-card-left">
                <div class="scrape-job-running-indicator" title="Scrape in progress">
                    <span class="job-running-spinner"></span>
                </div>
                <div class="scrape-job-info">
                    <div class="scrape-job-name">${escHtml(job.name)}</div>
                    <div class="scrape-job-url" title="${escHtml(job.base_url)}">${escHtml(job.base_url)}</div>
                </div>
            </div>
            <div class="scrape-job-card-right">
                <span class="scrape-job-scope-badge">${escHtml(job.mode)}</span>
                <span class="scrape-job-page-count" data-job-id="${escHtml(job.id)}">${job.page_count || 0} page${(job.page_count || 0) !== 1 ? "s" : ""}</span>
            </div>
        </div>

        <div class="scrape-job-card-meta">
            <div class="scrape-job-meta-item">
                <span class="scrape-job-meta-label">Last scraped</span>
                <span class="scrape-job-meta-value job-last-scraped">${formatJobDate(job.last_scraped_at)}</span>
            </div>
            <div class="scrape-job-meta-item">
                <span class="scrape-job-meta-label">Next run</span>
                <span class="scrape-job-meta-value job-next-run">${escHtml(nextRun)}</span>
            </div>
            <div class="scrape-job-meta-item scrape-job-schedule-wrap">
                <label class="scrape-job-meta-label" for="schedule-${escHtml(job.id)}">Schedule</label>
                <select class="scrape-job-schedule-select" id="schedule-${escHtml(job.id)}" data-job-id="${escHtml(job.id)}">
                    ${scheduleOptions}
                </select>
            </div>
        </div>

        <div class="scrape-job-progress" id="job-progress-${escHtml(job.id)}">
            <div class="scrape-job-progress-bar-track">
                <div class="scrape-job-progress-bar-fill"></div>
            </div>
            <div class="scrape-job-progress-status"></div>
        </div>

        <div class="scrape-job-card-actions">
            <button class="btn-job-run" data-job-id="${escHtml(job.id)}">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                Scrape Now
            </button>
            <button class="btn-job-delete btn-delete" data-job-id="${escHtml(job.id)}">Delete Job</button>
        </div>
    `;

    // Prepend to top
    scrapeJobsList.prepend(card);

    // Mark pages panel as not yet loaded
    const newPagesPanel = card.querySelector(".scrape-job-pages-panel");
    if (newPagesPanel) newPagesPanel.dataset.loaded = "false";

    // Attach handlers
    attachJobCardHandlers(card);

    return card;
}

function updateJobCard(card, job) {
    // Update meta values
    const lastScrapedEl = card.querySelector(".job-last-scraped");
    const nextRunEl = card.querySelector(".job-next-run");
    const pageCountEl = card.querySelector(".scrape-job-page-count");

    if (lastScrapedEl) lastScrapedEl.textContent = formatJobDate(job.last_scraped_at);
    if (nextRunEl) {
        nextRunEl.textContent = (job.schedule_interval && job.next_scrape_at)
            ? formatJobDate(job.next_scrape_at)
            : "Manual only";
    }
    if (pageCountEl) {
        pageCountEl.textContent = `${job.page_count || 0} page${(job.page_count || 0) !== 1 ? "s" : ""}`;
    }

    card.classList.toggle("job-running", !!job.is_running);
}

function attachJobCardHandlers(card) {
    const jobId = card.dataset.jobId;

    // Schedule change
    const scheduleSelect = card.querySelector(".scrape-job-schedule-select");
    if (scheduleSelect) {
        scheduleSelect.addEventListener("change", async () => {
            const newInterval = scheduleSelect.value || null;
            try {
                const res = await fetch(card.dataset.patchUrl, {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ schedule_interval: newInterval }),
                });
                if (res.ok) {
                    const updated = await res.json();
                    updateJobCard(card, updated);
                }
            } catch (e) {
                console.error("Failed to update schedule:", e);
            }
        });
    }

    // Scrape Now button
    const runBtn = card.querySelector(".btn-job-run");
    if (runBtn) {
        runBtn.addEventListener("click", () => {
            const workspaceId = scrapeJobsPanel.dataset.workspaceId;
            runJobById(jobId, workspaceId);
        });
    }

    // View Pages toggle
    const pagesToggleBtn = card.querySelector(".btn-job-pages-toggle");
    const pagesPanel = card.querySelector(".scrape-job-pages-panel");
    if (pagesToggleBtn && pagesPanel) {
        pagesToggleBtn.addEventListener("click", () => toggleJobPages(card, pagesToggleBtn, pagesPanel));
    }

    // Delete Job button
    const deleteBtn = card.querySelector(".btn-job-delete");
    if (deleteBtn) {
        deleteBtn.addEventListener("click", async () => {
            if (!confirm("Delete this scrape job and all its downloaded pages?")) return;

            deleteBtn.disabled = true;
            deleteBtn.textContent = "Deleting...";

            deleteOverlayTitle.textContent = "Deleting scrape job...";
            deleteOverlayMsg.textContent = "Removing all pages from workspace...";
            deleteOverlay.classList.add("visible");

            try {
                const res = await fetch(card.dataset.deleteUrl, { method: "DELETE" });
                if (res.ok) {
                    card.remove();
                    // Show empty state if no jobs remain
                    if (scrapeJobsList.querySelectorAll(".scrape-job-card").length === 0) {
                        const empty = document.createElement("div");
                        empty.className = "scrape-jobs-empty";
                        empty.id = "scrapeJobsEmpty";
                        empty.textContent = "No scrape jobs yet. Add a website above to get started.";
                        scrapeJobsList.appendChild(empty);
                    }
                } else {
                    alert("Failed to delete job.");
                    deleteBtn.disabled = false;
                    deleteBtn.textContent = "Delete Job";
                }
            } catch (e) {
                alert("Failed to delete job: " + e.message);
                deleteBtn.disabled = false;
                deleteBtn.textContent = "Delete Job";
            } finally {
                deleteOverlay.classList.remove("visible");
            }
        });
    }
}

// Attach handlers to all job cards that already exist in the DOM
document.querySelectorAll(".scrape-job-card").forEach(attachJobCardHandlers);

/* ---------- Job Pages Panel ---------- */

async function toggleJobPages(card, btn, panel) {
    const isOpen = panel.classList.contains("visible");

    if (isOpen) {
        panel.classList.remove("visible");
        btn.classList.remove("open");
        return;
    }

    // Mark as open immediately so the UI responds
    panel.classList.add("visible");
    btn.classList.add("open");

    // If already loaded (and not stale), don't re-fetch
    if (panel.dataset.loaded === "true") return;

    const listEl = panel.querySelector(".scrape-job-pages-list");
    listEl.innerHTML = '<div class="job-pages-loading">Loading pages\u2026</div>';

    try {
        const res = await fetch(panel.dataset.pagesUrl);
        if (!res.ok) throw new Error(`Failed to load pages (${res.status})`);
        const pages = await res.json();
        panel.dataset.loaded = "true";
        renderJobPages(listEl, pages, card);
    } catch (e) {
        listEl.innerHTML = `<div class="job-pages-empty">Failed to load pages: ${escHtml(e.message)}</div>`;
    }
}

function renderJobPages(listEl, pages, card) {
    if (!pages || pages.length === 0) {
        listEl.innerHTML = '<div class="job-pages-empty">No pages scraped yet. Run a scrape to populate this job.</div>';
        return;
    }

    listEl.innerHTML = "";

    // Search bar
    const searchWrap = document.createElement("div");
    searchWrap.className = "job-pages-search-wrap";
    searchWrap.innerHTML = `<input type="search" class="job-pages-search" placeholder="Search pages\u2026">`;
    listEl.appendChild(searchWrap);

    const searchInput = searchWrap.querySelector(".job-pages-search");

    const itemsContainer = document.createElement("div");
    itemsContainer.className = "job-pages-items";
    listEl.appendChild(itemsContainer);

    function renderItems(filter) {
        const q = (filter || "").toLowerCase();
        const filtered = q ? pages.filter(p =>
            (p.source_url || "").toLowerCase().includes(q) ||
            (p.filename || "").toLowerCase().includes(q)
        ) : pages;

        itemsContainer.innerHTML = "";
        if (filtered.length === 0) {
            itemsContainer.innerHTML = '<div class="job-pages-empty">No matching pages.</div>';
            return;
        }

        filtered.forEach(page => {
            const row = document.createElement("div");
            row.className = "job-page-row";
            row.dataset.fileId = page.id;

            const checkedDate = page.last_checked_at
                ? new Date(page.last_checked_at).toLocaleString("en-US", {
                    month: "short", day: "numeric", year: "numeric",
                    hour: "numeric", minute: "2-digit",
                  })
                : (page.uploaded_at
                    ? new Date(page.uploaded_at).toLocaleString("en-US", {
                        month: "short", day: "numeric", year: "numeric",
                        hour: "numeric", minute: "2-digit",
                      })
                    : "Unknown");

            const displayUrl = page.source_url || page.filename;

            row.innerHTML = `
                <div class="job-page-info">
                    <div class="job-page-url" title="${escHtml(displayUrl)}">
                        ${page.source_url
                            ? `<a href="${escHtml(page.source_url)}" target="_blank" rel="noopener noreferrer">${escHtml(displayUrl)}</a>`
                            : escHtml(displayUrl)
                        }
                    </div>
                    <div class="job-page-meta">Last checked: ${escHtml(checkedDate)}</div>
                </div>
                <button class="btn-job-page-delete" data-file-id="${escHtml(page.id)}" title="Remove this page">
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
                </button>
            `;

            // Per-page delete
            row.querySelector(".btn-job-page-delete").addEventListener("click", async (e) => {
                e.stopPropagation();
                if (!confirm("Remove this page from the workspace?")) return;

                const btn = e.currentTarget;
                btn.disabled = true;

                const res = await fetch(`/delete/${page.id}`, { method: "DELETE" });
                if (res.ok) {
                    // Remove from local pages array so re-renders stay accurate
                    const idx = pages.findIndex(p => p.id === page.id);
                    if (idx !== -1) pages.splice(idx, 1);

                    row.remove();

                    // Update the page count badge on the card header
                    const pageCountEl = card.querySelector(".scrape-job-page-count");
                    if (pageCountEl) {
                        const current = parseInt(pageCountEl.textContent) || 0;
                        const next = Math.max(0, current - 1);
                        pageCountEl.textContent = `${next} page${next !== 1 ? "s" : ""}`;
                    }

                    // Update the toggle button count badge
                    const toggleCount = card.querySelector(".job-pages-toggle-count");
                    if (toggleCount) {
                        const current = parseInt(toggleCount.textContent.replace(/\D/g, "")) || 0;
                        const next = Math.max(0, current - 1);
                        toggleCount.textContent = `(${next})`;
                    }

                    if (pages.length === 0) {
                        itemsContainer.innerHTML = '<div class="job-pages-empty">No pages scraped yet. Run a scrape to populate this job.</div>';
                    }
                } else {
                    alert("Failed to delete page.");
                    btn.disabled = false;
                }
            });

            itemsContainer.appendChild(row);
        });
    }

    searchInput.addEventListener("input", () => renderItems(searchInput.value));
    renderItems("");
}

/* ---------- Run a job via SSE ---------- */

async function runJobById(jobId, workspaceId) {
    const card = scrapeJobsList.querySelector(`[data-job-id="${CSS.escape(jobId)}"]`);
    if (!card) return;

    const runBtn = card.querySelector(".btn-job-run");
    const progressArea = card.querySelector(".scrape-job-progress");
    const progressFill = card.querySelector(".scrape-job-progress-bar-fill");
    const progressStatus = card.querySelector(".scrape-job-progress-status");

    // Mark as running
    card.classList.add("job-running");
    if (runBtn) { runBtn.disabled = true; }
    progressArea.classList.add("visible");
    progressFill.style.width = "0%";
    progressStatus.textContent = "Discovering pages\u2026";

    const runUrl = `/${workspaceId}/scrape/jobs/${jobId}/run`;

    try {
        const res = await fetch(runUrl, { method: "POST" });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || `Run failed (${res.status})`);
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        let totalUrls = 0;
        let processedUrls = 0;

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split("\n");
            buffer = lines.pop();

            for (const line of lines) {
                if (!line.startsWith("data: ")) continue;
                const payload = line.slice(6).trim();
                if (!payload) continue;

                if (payload === "[DONE]") {
                    progressFill.style.width = "100%";
                    progressStatus.textContent = "Complete";
                    setTimeout(() => {
                        progressArea.classList.remove("visible");
                        progressFill.style.width = "0%";
                        progressStatus.textContent = "";
                    }, 2500);

                    card.classList.remove("job-running");
                    if (runBtn) runBtn.disabled = false;
                    return;
                }

                try {
                    const event = JSON.parse(payload);

                    if (event.status === "discovering") {
                        progressStatus.textContent = "Discovering pages\u2026";
                    } else if (event.status === "discovered") {
                        totalUrls = event.count || 0;
                        progressStatus.textContent = `Found ${totalUrls} page${totalUrls !== 1 ? "s" : ""}. Processing\u2026`;
                    } else if (event.status === "done") {
                        // Final summary from server
                        const pageCount = event.page_count || 0;
                        const pageCountEl = card.querySelector(".scrape-job-page-count");
                        if (pageCountEl) pageCountEl.textContent = `${pageCount} page${pageCount !== 1 ? "s" : ""}`;

                        // Update the toggle button count badge
                        const toggleCount = card.querySelector(".job-pages-toggle-count");
                        if (toggleCount) toggleCount.textContent = `(${pageCount})`;
                        else {
                            const toggleBtn = card.querySelector(".btn-job-pages-toggle");
                            if (toggleBtn && pageCount > 0) {
                                // Add the badge if it didn't exist
                                const span = document.createElement("span");
                                span.className = "job-pages-toggle-count";
                                span.textContent = `(${pageCount})`;
                                toggleBtn.appendChild(span);
                            }
                        }

                        // Mark the pages panel as stale so it re-fetches on next open
                        const pagesPanel = card.querySelector(".scrape-job-pages-panel");
                        if (pagesPanel) {
                            pagesPanel.dataset.loaded = "false";
                            // If the panel is currently open, refresh it now
                            if (pagesPanel.classList.contains("visible")) {
                                const listEl = pagesPanel.querySelector(".scrape-job-pages-list");
                                listEl.innerHTML = '<div class="job-pages-loading">Refreshing\u2026</div>';
                                fetch(pagesPanel.dataset.pagesUrl)
                                    .then(r => r.json())
                                    .then(pages => {
                                        pagesPanel.dataset.loaded = "true";
                                        renderJobPages(listEl, pages, card);
                                    })
                                    .catch(() => { listEl.innerHTML = '<div class="job-pages-empty">Failed to reload pages.</div>'; });
                            }
                        }

                        // Refresh last scraped time via a quick GET
                        refreshJobCardMeta(card, jobId, workspaceId);
                    } else if (["new", "changed", "unchanged", "removed"].includes(event.status)) {
                        processedUrls++;
                        const pct = totalUrls > 0 ? Math.round((processedUrls / totalUrls) * 95) : 50;
                        progressFill.style.width = pct + "%";

                        const label = event.status === "new" ? "Adding" :
                                      event.status === "changed" ? "Updating" :
                                      event.status === "removed" ? "Removing" : "Checking";
                        progressStatus.textContent = `${label}: ${event.url}`;
                    } else if (event.status === "error" && event.url) {
                        // Per-URL error — continue
                        processedUrls++;
                    } else if (event.status === "error") {
                        progressStatus.textContent = "Error: " + (event.message || "Unknown error");
                    }
                } catch (e) {
                    console.warn("Skipping unparseable job event:", payload);
                }
            }
        }
    } catch (err) {
        console.error("Job run error:", err);
        progressStatus.textContent = "Run failed: " + err.message;
        setTimeout(() => {
            progressArea.classList.remove("visible");
        }, 3000);
    } finally {
        card.classList.remove("job-running");
        if (runBtn) runBtn.disabled = false;
    }
}

async function refreshJobCardMeta(card, jobId, workspaceId) {
    try {
        const res = await fetch(`/${workspaceId}/scrape/jobs`);
        if (!res.ok) return;
        const jobs = await res.json();
        const job = jobs.find(j => j.id === jobId);
        if (job) updateJobCard(card, job);
    } catch (e) {
        // Non-critical — ignore
    }
}

/* ---------- Background job polling ---------- */
// If any job card is in running state when the page loads (background scheduler run),
// poll every 5 seconds to refresh status and hide the spinner when done.

function startPollingIfNeeded() {
    const workspaceId = scrapeJobsPanel.dataset.workspaceId;
    const runningCards = Array.from(scrapeJobsList.querySelectorAll(".scrape-job-card.job-running"));
    if (runningCards.length === 0) return;

    const interval = setInterval(async () => {
        try {
            const res = await fetch(`/${workspaceId}/scrape/jobs`);
            if (!res.ok) return;
            const jobs = await res.json();
            const jobMap = Object.fromEntries(jobs.map(j => [j.id, j]));

            let stillRunning = false;
            scrapeJobsList.querySelectorAll(".scrape-job-card").forEach(card => {
                const job = jobMap[card.dataset.jobId];
                if (job) {
                    updateJobCard(card, job);
                    if (job.is_running) stillRunning = true;
                }
            });

            if (!stillRunning) clearInterval(interval);
        } catch (e) {
            // ignore
        }
    }, 5000);
}

startPollingIfNeeded();

/* ========== Upload Search Bar ========== */

let uploadSearchTerm = "";
const uploadSearchBar = document.getElementById("uploadSearchBar");
if (uploadSearchBar) {
    uploadSearchBar.addEventListener("input", () => {
        uploadSearchTerm = uploadSearchBar.value.trim().toLowerCase();
        applyCurrentFilter();
    });
}

/* ========== Extension Filter Bar ========== */

let activeFilter = "all";

function refreshFilterBar() {
    const cards = Array.from(fileDisplay.querySelectorAll(".file_card:not(.uploading):not(.upload-error)"));
    const extSet = new Set();
    cards.forEach((card) => {
        const ext = card.dataset.extension;
        if (ext) extSet.add(ext);
    });

    const filterBar = document.getElementById("filterBar");
    const exts = Array.from(extSet).sort();

    filterBar.innerHTML = "";

    const allPill = document.createElement("button");
    allPill.className = "filter-pill" + (activeFilter === "all" ? " active" : "");
    allPill.dataset.ext = "all";
    allPill.textContent = "All";
    filterBar.appendChild(allPill);

    exts.forEach((ext) => {
        const pill = document.createElement("button");
        pill.className = "filter-pill" + (activeFilter === ext ? " active" : "");
        pill.dataset.ext = ext;
        pill.textContent = ext.replace(".", "").toUpperCase();
        filterBar.appendChild(pill);
    });

    attachFilterHandlers();
}

function attachFilterHandlers() {
    document.querySelectorAll("#filterBar .filter-pill").forEach((pill) => {
        pill.addEventListener("click", () => {
            activeFilter = pill.dataset.ext;
            document.querySelectorAll("#filterBar .filter-pill").forEach((p) => p.classList.remove("active"));
            pill.classList.add("active");
            applyCurrentFilter();
        });
    });
}

function applyCurrentFilter() {
    const cards = fileDisplay.querySelectorAll(".file_card");
    cards.forEach((card) => {
        const ext = card.dataset.extension || "";
        const filename = (card.dataset.filename || "").toLowerCase();
        const matchesExt = activeFilter === "all" || ext === activeFilter;
        const matchesSearch = !uploadSearchTerm || filename.includes(uploadSearchTerm);

        if (matchesExt && matchesSearch) {
            card.classList.remove("filtered-out");
        } else {
            card.classList.add("filtered-out");
        }
    });

    checkboxes.forEach((cb) => {
        const card = cb.closest(".file_card");
        if (card.classList.contains("filtered-out")) cb.checked = false;
    });

    updateSelection();
}

attachFilterHandlers();

/* ========== Sort ========== */

const sortSelect = document.getElementById("sortSelect");

sortSelect.addEventListener("change", () => {
    applySort(sortSelect.value);
});

function applySort(mode) {
    const cards = Array.from(fileDisplay.querySelectorAll(".file_card:not(.uploading):not(.upload-error)"));

    cards.sort((a, b) => {
        switch (mode) {
            case "az": return (a.dataset.filename || "").toLowerCase().localeCompare((b.dataset.filename || "").toLowerCase());
            case "za": return (b.dataset.filename || "").toLowerCase().localeCompare((a.dataset.filename || "").toLowerCase());
            case "newest": return (b.dataset.date || "").localeCompare(a.dataset.date || "");
            case "oldest": return (a.dataset.date || "").localeCompare(b.dataset.date || "");
            case "ext": {
                const cmp = (a.dataset.extension || "").toLowerCase().localeCompare((b.dataset.extension || "").toLowerCase());
                if (cmp !== 0) return cmp;
                return (a.dataset.filename || "").toLowerCase().localeCompare((b.dataset.filename || "").toLowerCase());
            }
            default: return 0;
        }
    });

    cards.forEach((card) => fileDisplay.appendChild(card));
    rebuildCheckboxes();
}

/* ========== Section Collapse ========== */

function setupCollapseBtn(btnId, panelEl) {
    const btn = document.getElementById(btnId);
    if (!btn || !panelEl) return;
    const label = btn.querySelector(".btn-collapse-label");
    btn.addEventListener("click", () => {
        const collapsed = panelEl.classList.toggle("collapsed");
        btn.setAttribute("aria-expanded", String(!collapsed));
        btn.title = collapsed ? "Expand" : "Collapse";
        if (label) label.textContent = collapsed ? "Expand" : "Collapse";
    });
}

setupCollapseBtn("uploadCollapseBtn", document.querySelector(".workflow-upload"));
setupCollapseBtn("scrapeCollapseBtn", document.querySelector(".workflow-scrape"));
setupCollapseBtn("jobsCollapseBtn", document.querySelector(".workflow-jobs"));
