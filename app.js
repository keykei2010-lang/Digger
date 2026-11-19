"use strict";

const byId = (id) => document.getElementById(id);
const state = { source: null, mode: "recover", job: null, busy: false, engines: {}, files: [], selected: new Set() };

async function request(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || `Request failed (${response.status}).`);
  }
  return response;
}

function show(view) {
  for (const name of ["scan", "results"]) {
    byId(`${name}-view`).hidden = view !== name;
    const item = byId(`${name}-nav`);
    item.classList.toggle("active", view === name);
    if (view === name) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  }
}

function formatSize(value) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${(value / 1024 ** 3).toFixed(1)} GB`;
}

function updateControls() {
  const engine = state.mode === "recover" ? "photorec" : "testdisk";
  byId("start").disabled = !state.source || state.busy || !state.engines[engine];
  byId("demo").disabled = state.busy || !state.engines[engine];
  byId("dropzone").disabled = state.busy;
}

function choose(file) {
  if (!file) return;
  if (file.size === 0) {
    byId("scan-error").textContent = "Choose a non-empty disk image.";
    return;
  }
  byId("scan-error").textContent = "";
  state.source = file;
  byId("file-title").textContent = file.name;
  byId("file-detail").textContent = `${formatSize(file.size)} · Click to choose another image`;
  updateControls();
}

function displayStatus(status, detail = "") {
  const labels = { uploading: "Preparing the image", running: "Scan running", stopping: "Stopping scan",
    complete: "Scan complete", failed: "Scan failed", stopped: "Scan stopped" };
  byId("job-status").textContent = labels[status];
  byId("job-detail").textContent = detail;
  byId("status-dot").className = status === "uploading" ? "running" : status;
  byId("stop").hidden = status !== "running";
}

function fileUrl(action, entry) {
  const path = entry.id.split("/").map(encodeURIComponent).join("/");
  return `/api/jobs/${state.job}/${action}/${path}`;
}

function showFile(entry) {
  const preview = byId("preview");
  preview.replaceChildren();
  if (entry.preview) {
    const image = document.createElement("img");
    image.src = fileUrl("preview", entry);
    image.alt = `Preview of ${entry.name}`;
    image.onerror = () => image.replaceWith("Preview unavailable.");
    preview.append(image);
  }
  const title = document.createElement("p");
  title.textContent = `${entry.name} · ${formatSize(entry.size)}`;
  const link = document.createElement("a");
  link.href = fileUrl("download", entry);
  link.textContent = "Download file";
  preview.append(title, link);
}

function visibleFiles() {
  const search = byId("search").value.trim().toLowerCase();
  const type = byId("type-filter").value;
  return state.files.filter((entry) => (!type || entry.type === type) && entry.name.toLowerCase().includes(search));
}

function updateSelection(entries = visibleFiles()) {
  const visibleSelected = entries.filter((entry) => state.selected.has(entry.id)).length;
  const all = byId("select-all");
  all.checked = entries.length > 0 && visibleSelected === entries.length;
  all.indeterminate = visibleSelected > 0 && !all.checked;
  all.disabled = entries.length === 0;
  byId("export").disabled = state.selected.size === 0;
  byId("result-count").textContent = `${entries.length} of ${state.files.length} files shown · ${state.selected.size} selected`;
}

function renderFiles() {
  const files = visibleFiles();
  const rows = document.createDocumentFragment();
  for (const entry of files) {
    const row = document.createElement("tr");
    const check = document.createElement("input");
    check.type = "checkbox";
    check.checked = state.selected.has(entry.id);
    check.setAttribute("aria-label", `Select ${entry.name}`);
    check.onchange = () => {
      if (check.checked) state.selected.add(entry.id);
      else state.selected.delete(entry.id);
      updateSelection(files);
    };
    row.insertCell().append(check);
    const label = document.createElement("button");
    label.type = "button";
    label.className = "file-button";
    label.textContent = entry.name;
    label.onclick = () => showFile(entry);
    row.insertCell().append(label);
    row.insertCell().textContent = entry.type.toUpperCase() || "File";
    row.insertCell().textContent = formatSize(entry.size);
    rows.append(row);
  }
  byId("file-rows").replaceChildren(rows);
  byId("empty-files").hidden = files.length !== 0;
  byId("empty-files").textContent = state.files.length ? "No files match the current filters." : "No files recovered.";
  updateSelection(files);
}

function displayFiles(files) {
  state.files = files;
  const filter = byId("type-filter");
  filter.replaceChildren(new Option("All file types", ""));
  const types = new Set(files.map((entry) => entry.type).filter(Boolean));
  for (const type of [...types].sort()) filter.add(new Option(type.toUpperCase(), type));
  byId("results-panel").hidden = false;
  renderFiles();
}

async function watchJob(mode) {
  let failures = 0;
  while (true) {
    try {
      const response = await request(`/api/jobs/${state.job}`);
      const job = await response.json();
      failures = 0;
      const active = ["running", "stopping"].includes(job.status);
      const detail = active ? "The recovery engine is reading the disk image."
        : mode === "inspect" ? "See the partition listing under Engine output."
        : `${job.files.length} recovered file${job.files.length === 1 ? "" : "s"} available.`;
      displayStatus(job.status, detail);
      if (!active) {
        byId("result-error").textContent = job.error || "";
        byId("engine-output").hidden = !job.details;
        byId("engine-output").open = mode === "inspect" || job.status === "failed";
        byId("engine-log").textContent = job.details.replace(/\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>]|[\x00-\x08\x0b-\x1f]/g, "");
        if (mode === "recover") displayFiles(job.files);
        return;
      }
    } catch (error) {
      failures++;
      if (failures >= 3) throw error;
    }
    await new Promise((resolve) => setTimeout(resolve, 650));
  }
}

async function start(sample) {
  if (state.busy || (!sample && !state.source)) return;
  state.busy = true;
  state.job = null;
  state.selected.clear();
  byId("search").value = "";
  byId("preview").textContent = "Select a file name to preview it.";
  byId("result-error").textContent = "";
  byId("results-panel").hidden = true;
  byId("engine-output").hidden = true;
  byId("result-source").textContent = sample ? "Sample disk image" : state.source.name;
  const mode = state.mode;
  show("results");
  displayStatus("uploading", "Preparing a workspace copy for the scan.");
  updateControls();
  try {
    const response = await request("/api/jobs", {
      method: "POST",
      headers: {
        "X-Recovery-Session": state.token,
        "X-Recovery-Mode": mode,
        "X-Recovery-Sample": sample ? "1" : "0",
        "X-Recovery-Name": sample ? "Sample" : encodeURIComponent(state.source.name)
      },
      body: sample ? null : state.source
    });
    state.job = (await response.json()).id;
    await watchJob(mode);
  } catch (error) {
    displayStatus("failed");
    byId("result-error").textContent = error.message;
  } finally {
    state.busy = false;
    updateControls();
  }
}

async function exportFiles() {
  if (!state.job || !state.selected.size) return;
  const button = byId("export");
  button.disabled = true;
  byId("result-error").textContent = "";
  try {
    const response = await request(`/api/jobs/${state.job}/export`, {
      method: "POST",
      headers: { "X-Recovery-Session": state.token, "Content-Type": "application/json" },
      body: JSON.stringify([...state.selected])
    });
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = "digger-recovered.zip";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) {
    byId("result-error").textContent = error.message;
  } finally {
    updateSelection();
  }
}

byId("scan-nav").onclick = byId("new-scan").onclick = () => show("scan");
byId("results-nav").onclick = () => show("results");
byId("modes").onclick = (event) => {
  const button = event.target.closest("[data-mode]");
  if (!button || state.busy) return;
  state.mode = button.dataset.mode;
  for (const option of document.querySelectorAll("[data-mode]")) {
    option.classList.toggle("selected", option === button);
    option.setAttribute("aria-pressed", String(option === button));
  }
  updateControls();
};
const dropzone = byId("dropzone");
dropzone.onclick = () => byId("image-file").click();
byId("image-file").onchange = (event) => choose(event.target.files[0]);
dropzone.ondragover = (event) => {
  event.preventDefault();
  if (!state.busy) dropzone.classList.add("dragging");
};
dropzone.ondragleave = () => dropzone.classList.remove("dragging");
dropzone.ondrop = (event) => {
  event.preventDefault();
  dropzone.classList.remove("dragging");
  if (!state.busy) choose(event.dataTransfer.files[0]);
};
byId("demo").onclick = () => start(true);
byId("start").onclick = () => start(false);
byId("search").oninput = byId("type-filter").onchange = renderFiles;
byId("select-all").onchange = (event) => {
  for (const file of visibleFiles()) {
    if (event.target.checked) state.selected.add(file.id);
    else state.selected.delete(file.id);
  }
  renderFiles();
};
byId("export").onclick = exportFiles;
byId("stop").onclick = async () => {
  try {
    await request(`/api/jobs/${state.job}/stop`, { method: "POST", headers: { "X-Recovery-Session": state.token } });
  } catch (error) {
    byId("result-error").textContent = error.message;
  }
};
request("/api/session").then((response) => response.json()).then(({token, engines}) => {
  state.token = token;
  state.engines = engines;
  for (const [name, ready] of Object.entries(engines)) {
    const engine = byId(`${name}-status`);
    engine.classList.toggle("ready", ready);
    engine.parentElement.title = ready ? "Engine available" : "Engine unavailable";
  }
  if (!Object.values(engines).some(Boolean)) byId("scan-error").textContent = "Recovery engines unavailable. Build TestDisk and PhotoRec first.";
  updateControls();
}).catch((error) => {
  byId("scan-error").textContent = error.message;
});
