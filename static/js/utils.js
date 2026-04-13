/* ========== Upload: Drop Zone + Auto-Submit ========== */

const dropZone = document.getElementById("dropZone");
const fileInput = document.getElementById("fileInput");
const uploadCounter = document.getElementById("uploadCounter");
const fileDisplay = document.querySelector(".file_display");
const uploadUrl = dropZone.dataset.url;
const MAX_UPLOAD_BYTES = parseInt(dropZone.dataset.maxBytes, 10) || Infinity;

let isUploading = false;

// Format bytes into a human-readable string
function formatBytes(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

// Warn before closing/navigating away during an active upload
window.addEventListener("beforeunload", (e) => {
    if (isUploading) {
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

    // Escape HTML to prevent XSS from filenames
    function esc(str) {
        const d = document.createElement("div");
        d.textContent = str;
        return d.innerHTML;
    }

    // Update the header counter
    function updateCounter() {
        let text = `Uploading ${totalFiles} file${totalFiles > 1 ? "s" : ""} \u2014 ${completedCount}/${totalFiles} complete`;
        if (failedCount > 0) {
            text += ` (${failedCount} failed)`;
        }
        uploadCounter.textContent = text;
    }

    updateCounter();

    // Mark a row as failed with an error message
    function markRowError(row, message) {
        row.classList.remove("uploading");
        row.classList.add("upload-error");
        row.querySelector(".file-info .date").textContent = message || "Upload failed";
        const indicator = row.querySelector(".upload-indicator");
        if (indicator) indicator.remove();
        failedCount++;
        updateCounter();
    }

    // Track which files pass client-side validation
    let validCount = 0;

    // Insert in-progress rows at the top of the file list
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

        // Prepend to top of file list
        fileDisplay.prepend(row);
        rows[file.name] = row;

        // Client-side file size check
        if (file.size > MAX_UPLOAD_BYTES) {
            const limitStr = formatBytes(MAX_UPLOAD_BYTES);
            const sizeStr = formatBytes(file.size);
            markRowError(row, `File is ${sizeStr} \u2014 exceeds ${limitStr} limit`);
        } else {
            formData.append("uploaded_files", file);
            validCount++;
        }
    }

    // Scroll the section body to the top so the user sees the new rows
    fileDisplay.closest(".section-body").scrollTop = 0;

    // If all files were rejected client-side, stop here
    if (validCount === 0) {
        uploadCounter.textContent = `${failedCount} file${failedCount > 1 ? "s" : ""} rejected (too large)`;
        setTimeout(() => { uploadCounter.textContent = ""; }, 4000);
        isUploading = false;
        return;
    }

    const res = await fetch(uploadUrl, {
        method: "POST",
        body: formData,
    });

    if (!res.ok) {
        // Mark only the rows that were actually sent (not already rejected)
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
                // All files processed — show final summary
                let summary = `${completedCount} file${completedCount !== 1 ? "s" : ""} uploaded`;
                if (failedCount > 0) {
                    summary += `, ${failedCount} failed`;
                }
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
                    // Per-file error from the backend
                    markRowError(row, event.message || "Processing failed");
                } else if (event.status === "done") {
                    // Transform the uploading row into a completed file card
                    completedCount++;
                    updateCounter();
                    transformToCompleted(row, event);
                } else {
                    // Update progress indicator
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
    // event has: file (original name), name (processed name), location (file ID),
    //            status, original_extension
    const fileId = event.location;
    const displayName = event.name;
    const ext = (event.original_extension || "").replace(".", "");

    // Escape HTML to prevent XSS from filenames
    function esc(str) {
        const d = document.createElement("div");
        d.textContent = str;
        return d.innerHTML;
    }

    // Format a "just now" timestamp
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

    // Wire up the delete button on this new card
    attachDeleteHandler(row.querySelector(".btn-delete"));

    // Apply current filter
    applyCurrentFilter();

    // Remove the animation class after it finishes
    row.addEventListener("animationend", () => {
        row.classList.remove("just-completed");
    }, { once: true });
}

/* ========== Delete Handlers ========== */

function attachDeleteHandler(btn) {
    btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm("Delete this document?")) return;

        const res = await fetch(`/delete/${btn.dataset.id}`, {
            method: "DELETE",
        });

        if (res.ok) {
            const card = btn.closest(".file_card");
            card.remove();
            rebuildCheckboxes();
            updateSelection();
            refreshFilterBar();
        }
    });
}

// Attach delete handlers to all existing cards on page load
document.querySelectorAll(".btn-delete").forEach(attachDeleteHandler);

/* ========== Checkboxes: Shift-Click Multi-Select ========== */

const bulkActions = document.getElementById("bulkActions");
const selectedCountEl = document.getElementById("selectedCount");
let checkboxes = Array.from(document.querySelectorAll(".file-checkbox"));
let lastCheckedIndex = null;

function rebuildCheckboxes() {
    checkboxes = Array.from(document.querySelectorAll(".file-checkbox"));
    lastCheckedIndex = null;

    // Re-attach click handlers
    checkboxes.forEach((cb, index) => {
        // Remove old listeners by cloning
        const newCb = cb.cloneNode(true);
        cb.parentNode.replaceChild(newCb, cb);
        checkboxes[index] = newCb;

        newCb.addEventListener("click", (e) => {
            if (e.shiftKey && lastCheckedIndex !== null) {
                const start = Math.min(lastCheckedIndex, index);
                const end = Math.max(lastCheckedIndex, index);
                const checked = newCb.checked;
                for (let i = start; i <= end; i++) {
                    checkboxes[i].checked = checked;
                }
            }
            lastCheckedIndex = index;
            updateSelection();
        });
    });
}

function updateSelection() {
    const selected = checkboxes.filter((cb) => cb.checked);

    // Toggle highlight on cards
    checkboxes.forEach((cb) => {
        cb.closest(".file_card").classList.toggle("selected", cb.checked);
    });

    // Show/hide bulk actions bar
    if (selected.length > 0) {
        bulkActions.classList.add("visible");
        selectedCountEl.textContent = selected.length;
    } else {
        bulkActions.classList.remove("visible");
    }

    // Update select-all checkbox state
    syncSelectAllCheckbox();
}

// Initial setup for existing checkboxes
rebuildCheckboxes();

/* ========== Select All ========== */

const selectAllCb = document.getElementById("selectAllCb");

selectAllCb.addEventListener("change", () => {
    const isChecked = selectAllCb.checked;

    // Only affect visible (non-filtered) cards
    checkboxes.forEach((cb) => {
        const card = cb.closest(".file_card");
        if (!card.classList.contains("filtered-out")) {
            cb.checked = isChecked;
        }
    });

    updateSelection();
});

function syncSelectAllCheckbox() {
    const visibleCheckboxes = checkboxes.filter(
        (cb) => !cb.closest(".file_card").classList.contains("filtered-out")
    );

    if (visibleCheckboxes.length === 0) {
        selectAllCb.checked = false;
        selectAllCb.indeterminate = false;
        return;
    }

    const checkedCount = visibleCheckboxes.filter((cb) => cb.checked).length;

    if (checkedCount === 0) {
        selectAllCb.checked = false;
        selectAllCb.indeterminate = false;
    } else if (checkedCount === visibleCheckboxes.length) {
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
    const confirmMsg = `Delete ${count} document${count > 1 ? "s" : ""}?`;
    if (!confirm(confirmMsg)) return;

    // Show loading state on the button
    bulkDeleteBtn.disabled = true;
    bulkDeleteBtn.innerHTML = '<span class="btn-spinner"></span>Deleting...';

    // Show the deleting overlay
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
        // Remove deleted cards from the DOM
        const result = await res.json();
        const deletedIds = new Set(result.deleted || []);
        selected.forEach((cb) => {
            if (deletedIds.has(cb.dataset.id)) {
                cb.closest(".file_card").remove();
            }
        });
        rebuildCheckboxes();
        updateSelection();
        refreshFilterBar();
    } else {
        alert("Failed to delete selected documents.");
    }

    // Reset button state
    bulkDeleteBtn.disabled = false;
    bulkDeleteBtn.textContent = "Delete Selected";
    deleteOverlay.classList.remove("visible");
});

/* ========== Extension Filter Bar ========== */

let activeFilter = "all";

function refreshFilterBar() {
    // Gather current extensions from DOM cards
    const cards = Array.from(fileDisplay.querySelectorAll(".file_card:not(.uploading):not(.upload-error)"));
    const extSet = new Set();
    cards.forEach((card) => {
        const ext = card.dataset.extension;
        if (ext) extSet.add(ext);
    });

    const filterBar = document.getElementById("filterBar");
    const exts = Array.from(extSet).sort();

    // Rebuild pills
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

    // Reattach click handlers
    attachFilterHandlers();
}

function attachFilterHandlers() {
    document.querySelectorAll(".filter-pill").forEach((pill) => {
        pill.addEventListener("click", () => {
            activeFilter = pill.dataset.ext;
            document.querySelectorAll(".filter-pill").forEach((p) => p.classList.remove("active"));
            pill.classList.add("active");
            applyCurrentFilter();
        });
    });
}

function applyCurrentFilter() {
    const cards = fileDisplay.querySelectorAll(".file_card");
    cards.forEach((card) => {
        if (activeFilter === "all") {
            card.classList.remove("filtered-out");
        } else {
            const ext = card.dataset.extension || "";
            if (ext === activeFilter) {
                card.classList.remove("filtered-out");
            } else {
                card.classList.add("filtered-out");
            }
        }
    });

    // Uncheck anything that's now hidden
    checkboxes.forEach((cb) => {
        const card = cb.closest(".file_card");
        if (card.classList.contains("filtered-out")) {
            cb.checked = false;
        }
    });

    updateSelection();
}

// Initial attachment
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
            case "az": {
                const nameA = (a.dataset.filename || "").toLowerCase();
                const nameB = (b.dataset.filename || "").toLowerCase();
                return nameA.localeCompare(nameB);
            }
            case "za": {
                const nameA = (a.dataset.filename || "").toLowerCase();
                const nameB = (b.dataset.filename || "").toLowerCase();
                return nameB.localeCompare(nameA);
            }
            case "newest": {
                const dateA = a.dataset.date || "";
                const dateB = b.dataset.date || "";
                return dateB.localeCompare(dateA);
            }
            case "oldest": {
                const dateA = a.dataset.date || "";
                const dateB = b.dataset.date || "";
                return dateA.localeCompare(dateB);
            }
            case "ext": {
                const extA = (a.dataset.extension || "").toLowerCase();
                const extB = (b.dataset.extension || "").toLowerCase();
                const cmp = extA.localeCompare(extB);
                if (cmp !== 0) return cmp;
                // Secondary sort by name within same extension
                return (a.dataset.filename || "").toLowerCase().localeCompare((b.dataset.filename || "").toLowerCase());
            }
            default:
                return 0;
        }
    });

    // Re-append in sorted order (uploading cards stay at top)
    cards.forEach((card) => fileDisplay.appendChild(card));
    rebuildCheckboxes();
}

/* ========== Settings Panel ========== */

const settingsBtn = document.getElementById("settingsBtn");
const settingsPanel = document.getElementById("settingsPanel");
const settingsOverlay = document.getElementById("settingsOverlay");
const settingsClose = document.getElementById("settingsClose");
const settingsSaveBtn = document.getElementById("settingsSaveBtn");

function openSettings() {
    settingsOverlay.classList.add("visible");
    // Force reflow before adding open class for transition
    settingsPanel.offsetHeight;
    settingsPanel.classList.add("open");

    // Fetch current workspace settings from the API
    const workspaceId = settingsPanel.dataset.workspace;
    if (!workspaceId) return;

    const promptEl = document.getElementById("settingPrompt");
    const topNEl = document.getElementById("settingTopN");
    const tempEl = document.getElementById("settingTemp");
    const similarityEl = document.getElementById("settingSimilarity");
    const topNValueEl = document.getElementById("topNValue");
    const tempValueEl = document.getElementById("tempValue");

    // Show a loading state
    promptEl.disabled = true;
    topNEl.disabled = true;
    tempEl.disabled = true;
    similarityEl.disabled = true;
    topNValueEl.disabled = true;
    tempValueEl.disabled = true;
    promptEl.placeholder = "Loading settings...";

    fetch(`/api/v1/workspaces/${workspaceId}/settings`)
        .then((res) => {
            if (!res.ok) throw new Error(`Failed to fetch settings (${res.status})`);
            return res.json();
        })
        .then((data) => {
            console.log("Fetched workspace settings:", data);
            promptEl.value = data.prompt || "";
            topNEl.value = data.top_n ?? 4;
            topNValueEl.value = topNEl.value;
            tempEl.value = data.temperature ?? 0.7;
            tempValueEl.value = tempEl.value;
            // Snap fetched similarity to nearest option (0, 0.25, 0.5, 0.75)
            const raw = data.similarity_threshold ?? 0.25;
            const options = [0, 0.25, 0.5, 0.75];
            const closest = options.reduce((prev, curr) =>
                Math.abs(curr - raw) < Math.abs(prev - raw) ? curr : prev
            );
            similarityEl.value = String(closest);
        })
        .catch((err) => {
            console.error("Error loading workspace settings:", err);
            promptEl.placeholder = "Failed to load settings";
        })
        .finally(() => {
            promptEl.disabled = false;
            topNEl.disabled = false;
            tempEl.disabled = false;
            similarityEl.disabled = false;
            topNValueEl.disabled = false;
            tempValueEl.disabled = false;
            promptEl.placeholder = "Enter a system prompt for this workspace...";
        });
}

function closeSettings() {
    settingsPanel.classList.remove("open");
    // Wait for slide-out transition to finish before hiding overlay
    setTimeout(() => {
        settingsOverlay.classList.remove("visible");
    }, 300);
}

settingsBtn.addEventListener("click", openSettings);
settingsClose.addEventListener("click", closeSettings);
settingsOverlay.addEventListener("click", closeSettings);

// Close on Escape key
document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && settingsPanel.classList.contains("open")) {
        closeSettings();
    }
});

// Range slider <-> number input bidirectional sync
const topNRange = document.getElementById("settingTopN");
const topNInput = document.getElementById("topNValue");
topNRange.addEventListener("input", () => { topNInput.value = topNRange.value; });
topNInput.addEventListener("input", () => {
    let v = Math.min(Math.max(parseInt(topNInput.value) || 1, 1), 10);
    topNRange.value = v;
});
topNInput.addEventListener("change", () => {
    let v = Math.min(Math.max(parseInt(topNInput.value) || 1, 1), 10);
    topNInput.value = v;
    topNRange.value = v;
});

const tempRange = document.getElementById("settingTemp");
const tempInput = document.getElementById("tempValue");
tempRange.addEventListener("input", () => { tempInput.value = tempRange.value; });
tempInput.addEventListener("input", () => {
    let v = Math.min(Math.max(parseFloat(tempInput.value) || 0, 0), 1);
    tempRange.value = v;
});
tempInput.addEventListener("change", () => {
    let v = Math.min(Math.max(parseFloat(tempInput.value) || 0, 0), 1);
    v = Math.round(v * 10) / 10; // snap to 0.1 step
    tempInput.value = v;
    tempRange.value = v;
});

// Save button — posts settings to backend
settingsSaveBtn.addEventListener("click", async () => {
    const workspaceId = settingsPanel.dataset.workspace;
    if (!workspaceId) return;

    settingsSaveBtn.disabled = true;
    settingsSaveBtn.textContent = "Saving...";

    const payload = {
        prompt: document.getElementById("settingPrompt").value,
        top_n: parseInt(document.getElementById("settingTopN").value) || 4,
        temperature: parseFloat(document.getElementById("settingTemp").value) || 0.7,
        similarity_threshold: parseFloat(document.getElementById("settingSimilarity").value),
    };

    try {
        const res = await fetch(`/api/v1/workspaces/${workspaceId}/settings`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });

        if (!res.ok) throw new Error(`Save failed (${res.status})`);

        settingsSaveBtn.textContent = "Saved!";
        settingsSaveBtn.style.backgroundColor = "#22c55e";
        setTimeout(() => {
            settingsSaveBtn.textContent = "Save Settings";
            settingsSaveBtn.style.backgroundColor = "";
            settingsSaveBtn.disabled = false;
            closeSettings();
        }, 600);
    } catch (err) {
        console.error("Error saving workspace settings:", err);
        settingsSaveBtn.textContent = "Save Failed";
        settingsSaveBtn.style.backgroundColor = "#ef4444";
        setTimeout(() => {
            settingsSaveBtn.textContent = "Save Settings";
            settingsSaveBtn.style.backgroundColor = "";
            settingsSaveBtn.disabled = false;
        }, 2000);
    }
});
