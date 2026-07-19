document.addEventListener("change", (event) => {
  if (event.target.matches(".filters select")) {
    event.target.form?.requestSubmit();
  }
  if (event.target.matches("[data-price-input]")) {
    syncMarginFromPrice(event.target);
  }
  if (event.target.matches("[data-margin-input]")) {
    syncPriceFromMargin(event.target);
  }
  if (event.target.matches(".coverage-check input")) {
    event.target.closest(".coverage-check")?.classList.toggle("selected", event.target.checked);
  }
  if (event.target.matches("[data-toggle-visible-selection]")) {
    const table = event.target.closest("table");
    (table || document).querySelectorAll(".row-select").forEach((box) => { box.checked = event.target.checked; });
    updateVisibleSelectionToggle(table);
  }
  if (event.target.matches(".row-select")) {
    updateVisibleSelectionToggle(event.target.closest("table"));
  }
});

document.addEventListener("input", (event) => {
  if (event.target.matches("[data-price-input]")) {
    event.target.dataset.lastEdited = "price";
  }
  if (event.target.matches("[data-margin-input]")) {
    event.target.dataset.lastEdited = "margin";
  }
  if (event.target.matches("[data-clear-data-input]")) {
    const dialog = event.target.closest("dialog");
    const confirm = dialog?.querySelector("[data-confirm-clear-data]");
    if (confirm) confirm.disabled = event.target.value.trim() !== "CLEAR DATA";
  }
});

document.addEventListener("focusout", (event) => {
  if (event.target.matches("[data-price-input]") && event.target.dataset.lastEdited === "price") {
    syncMarginFromPrice(event.target);
    delete event.target.dataset.lastEdited;
  }
  if (event.target.matches("[data-margin-input]") && event.target.dataset.lastEdited === "margin") {
    syncPriceFromMargin(event.target);
    delete event.target.dataset.lastEdited;
  }
});

document.addEventListener("click", (event) => {
  if (event.target.matches("[data-toggle-visible-selection]")) {
    const table = event.target.closest("[data-comparison-table]");
    (table || document).querySelectorAll("tbody .row-select").forEach((box) => { box.checked = event.target.checked; });
    updateVisibleSelectionToggle(table);
    event.stopPropagation();
  }
  if (event.target.matches("[data-open-clear-data]")) {
    document.querySelector("[data-clear-data-dialog]")?.showModal();
  }
  if (event.target.matches("[data-close-clear-data]")) {
    event.target.closest("dialog")?.close();
  }
  if (event.target.matches("[data-select-visible]")) {
    document.querySelectorAll(".row-select").forEach((box) => { box.checked = true; });
  }
  if (event.target.matches("[data-clear-selection]")) {
    document.querySelectorAll(".row-select").forEach((box) => { box.checked = false; });
  }
  if (event.target.matches("[data-save-selected-prices]")) {
    saveSelectedUpdatedPrices(event.target);
  }
});

document.addEventListener("submit", (event) => {
  if (event.target.matches("[data-upload-form]")) {
    event.preventDefault();
    const file = event.target.querySelector("input[type=file]").files[0];
    const status = event.target.querySelector("[data-upload-status]");
    if (!file) return;
    status.textContent = "Uploading...";
    file.arrayBuffer().then((buffer) => fetch(`/imports/upload?filename=${encodeURIComponent(file.name)}`, {
      method: "POST",
      body: buffer,
      headers: { "content-type": "application/octet-stream", "x-filename": file.name }
    })).then((response) => {
      if (response.redirected) {
        window.location.href = response.url;
      } else if (response.ok) {
        window.location.reload();
      } else {
        status.textContent = "Upload failed.";
      }
    }).catch(() => { status.textContent = "Upload failed."; });
  }
  if (event.target.matches("#comparison-export")) {
    const ids = Array.from(document.querySelectorAll(".row-select:checked")).map((box) => box.value);
    document.querySelector("#selected-ids").value = ids.join(",");
  }
});

function formatEta(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "Calculating";
  const value = Math.max(0, Number(seconds));
  const minutes = Math.floor(value / 60);
  const remaining = Math.floor(value % 60);
  return minutes ? `${minutes}m ${remaining}s` : `${remaining}s`;
}

function formatTimestamp(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short"
  });
}

function renderProgress(job) {
  const progress = job.progress || {};
  const root = document.querySelector("[data-job-status]");
  if (!root) return;
  const displayStatus = job.status === "retrying_visible" ? job.status : (progress.status || job.status || "");
  root.querySelector("[data-status]").textContent = displayStatus;
  const competitor = root.querySelector("[data-competitor]");
  if (competitor) competitor.textContent = progress.competitor || "";
  root.querySelector("[data-completed]").textContent = progress.completed ?? 0;
  root.querySelector("[data-total]").textContent = progress.total ?? job.planned_count ?? 0;
  root.querySelector("[data-remaining]").textContent = progress.remaining ?? "";
  root.querySelector("[data-eta]").textContent = formatEta(progress.eta_seconds);
  const message = document.querySelector("[data-message]");
  if (message) message.textContent = job.message || progress.message || "";
  const last = document.querySelector("[data-last-part]");
  if (last) last.textContent = progress.last_attempted_part ? `Last: ${progress.last_attempted_part}` : "";
  const body = document.querySelector("[data-progress-rows]");
  if (body) {
    body.innerHTML = (progress.rows || []).map((row) => `<tr><td>${row.run_order || ""}</td><td>${row.competitor || ""}</td><td>${row.manufacturer || ""}</td><td>${row.oem_part_number || ""}</td><td>${row.selling_price || ""}</td><td>${row.result_type || ""}</td><td>${formatTimestamp(row.checked_at)}</td></tr>`).join("");
  }
  const competitorProgress = document.querySelector("[data-competitor-progress]");
  if (competitorProgress) {
    const entries = Object.entries(job.progress_by_competitor || {});
    competitorProgress.innerHTML = entries.map(([key, item]) => `
      <div class="progress-card">
        <span>${key}</span>
        <b>${item.completed ?? 0} / ${item.total ?? 0}</b>
        <small>${item.run_status || item.status || "waiting"}${item.last_attempted_part ? ` · Last: ${item.last_attempted_part}` : ""}</small>
      </div>
    `).join("");
  }
}

function moneyNumber(value) {
  if (!value) return NaN;
  return Number.parseFloat(String(value).replace(/[$,% ,]/g, ""));
}

function formatMoney(value) {
  if (!Number.isFinite(value)) return "";
  return value.toFixed(2);
}

function syncMarginFromPrice(input) {
  const productId = input.dataset.productId;
  const margin = document.querySelector(`[data-margin-input][data-product-id="${productId}"]`);
  if (!margin) return;
  const price = moneyNumber(input.value);
  const cost = moneyNumber(margin.dataset.cost);
  if (!Number.isFinite(price) || price <= 0 || !Number.isFinite(cost)) return;
  margin.value = (((price - cost) / price) * 100).toFixed(2);
}

function syncPriceFromMargin(input) {
  const productId = input.dataset.productId;
  const price = document.querySelector(`[data-price-input][data-product-id="${productId}"]`);
  if (!price) return;
  const margin = moneyNumber(input.value);
  const cost = moneyNumber(input.dataset.cost);
  if (!Number.isFinite(margin) || margin >= 100 || !Number.isFinite(cost)) return;
  price.value = formatMoney(cost / (1 - margin / 100));
}

function saveSelectedUpdatedPrices(button) {
  const checkedIds = new Set(Array.from(document.querySelectorAll(".row-select:checked")).map((box) => box.value));
  const rows = Array.from(document.querySelectorAll("[data-price-input]")).filter((input) => checkedIds.has(input.dataset.productId)).map((input) => {
    const productId = input.dataset.productId;
    const form = document.querySelector(`#comparison-review-${CSS.escape(productId)}`);
    const status = document.querySelector(`select[form="comparison-review-${CSS.escape(productId)}"]`);
    const ruleCodes = Array.from(document.querySelectorAll(`input[name="rule_code"][form="comparison-review-${CSS.escape(productId)}"]`)).map((item) => item.value);
    return {
      product_id: productId,
      suggested_new_price: input.value,
      review_status: status?.value || "Approved",
      rule_codes: ruleCodes,
      has_form: Boolean(form)
    };
  }).filter((row) => row.has_form && row.suggested_new_price && row.suggested_new_price.trim());
  if (!rows.length) {
    window.alert("Select at least one row with an Updated Price to save.");
    return;
  }
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = "Saving...";
  fetch("/comparison/bulk-save", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ rows })
  }).then((response) => response.json()).then((result) => {
    const query = button.dataset.returnQuery || "";
    const joiner = query ? "&" : "";
    window.location.href = `/comparison?${query}${joiner}message=${encodeURIComponent(`Saved ${result.saved || 0} selected updated prices.`)}`;
  }).catch(() => {
    button.disabled = false;
    button.textContent = originalText;
    window.alert("Selected prices could not be saved.");
  });
}

function updateVisibleSelectionToggle(table) {
  const root = table || document.querySelector("[data-comparison-table]");
  if (!root) return;
  const toggle = root.querySelector("[data-toggle-visible-selection]");
  const boxes = Array.from(root.querySelectorAll(".row-select"));
  if (!toggle || !boxes.length) return;
  const checked = boxes.filter((box) => box.checked).length;
  toggle.checked = checked === boxes.length;
  toggle.indeterminate = checked > 0 && checked < boxes.length;
}

function sortableValue(cell) {
  if (!cell) return "";
  return cell.dataset.sortValue || cell.innerText || "";
}

function applyTableSorting() {
  document.querySelectorAll(".table-wrap table").forEach((table) => {
    if (table.dataset.sortReady === "1") return;
    table.dataset.sortReady = "1";
    const headers = table.querySelectorAll("thead th");
    headers.forEach((header) => {
      if (header.querySelector("input, button, select")) return;
      if (!header.textContent.trim()) return;
      header.classList.add("sortable-heading");
      header.tabIndex = 0;
      const sortRows = () => {
        const tbody = table.tBodies[0];
        if (!tbody) return;
        const rows = Array.from(tbody.rows);
        const nextDirection = header.dataset.direction === "asc" ? "desc" : "asc";
        headers.forEach((item) => {
          item.dataset.direction = "";
          item.classList.remove("sort-asc", "sort-desc");
        });
        header.dataset.direction = nextDirection;
        header.classList.add(nextDirection === "asc" ? "sort-asc" : "sort-desc");
        rows.sort((left, right) => {
          const leftValue = sortableValue(left.cells[header.cellIndex]).trim();
          const rightValue = sortableValue(right.cells[header.cellIndex]).trim();
          const leftNumber = moneyNumber(leftValue);
          const rightNumber = moneyNumber(rightValue);
          if (Number.isFinite(leftNumber) || Number.isFinite(rightNumber)) {
            const leftRank = Number.isFinite(leftNumber) ? leftNumber : Number.POSITIVE_INFINITY;
            const rightRank = Number.isFinite(rightNumber) ? rightNumber : Number.POSITIVE_INFINITY;
            return nextDirection === "asc" ? leftRank - rightRank : rightRank - leftRank;
          }
          return nextDirection === "asc"
            ? leftValue.localeCompare(rightValue, undefined, { numeric: true, sensitivity: "base" })
            : rightValue.localeCompare(leftValue, undefined, { numeric: true, sensitivity: "base" });
        });
        rows.forEach((row) => tbody.appendChild(row));
      };
      header.addEventListener("click", sortRows);
      header.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          sortRows();
        }
      });
    });
  });
}

function initializePriceMarginPairs() {
  // The server renders matching values. Recalculate only after the user changes a field.
}

function pollJob() {
  const root = document.querySelector("[data-job-status]");
  if (!root) return;
  const jobId = root.dataset.jobId;
  fetch(`/collections/jobs/${encodeURIComponent(jobId)}/status`)
    .then((response) => response.json())
    .then((job) => {
      renderProgress(job);
      const status = job.status === "retrying_visible" ? job.status : ((job.progress && job.progress.status) || job.status || "");
      if (!["completed", "failed", "completed_with_warnings", "stopped_blocked", "stopped_challenge"].includes(status)) {
        window.setTimeout(pollJob, 2000);
      }
    })
    .catch(() => window.setTimeout(pollJob, 5000));
}

pollJob();
applyTableSorting();
initializePriceMarginPairs();
