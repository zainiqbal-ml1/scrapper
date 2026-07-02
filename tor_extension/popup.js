/* global browser */

const statusEl = document.getElementById("status");
const pageHint = document.getElementById("page-hint");
const allYearsEl = document.getElementById("allYears");
const batchSizeEl = document.getElementById("batchSize");
const batchPauseEl = document.getElementById("batchPause");
const subfolderEl = document.getElementById("subfolder");
const skipDoneEl = document.getElementById("skipDone");
const startBtn = document.getElementById("start");
const resumeBtn = document.getElementById("resume");
const cancelBtn = document.getElementById("cancel");

let listingUrl = null;
let pageCtx = null;
let pollTimer = null;

function setStatus(text, cls) {
  statusEl.textContent = text;
  statusEl.className = cls || "";
}

async function getActiveTab() {
  const tabs = await browser.tabs.query({ active: true, currentWindow: true });
  return tabs[0];
}

async function tabMessage(type, payload) {
  const tab = await getActiveTab();
  if (!tab || !tab.id) {
    return { ok: false, error: "No active tab." };
  }
  try {
    return await browser.tabs.sendMessage(tab.id, { type, ...payload });
  } catch (e) {
    return {
      ok: false,
      error: "Reload the CanLII page, then open this popup again.",
    };
  }
}

async function updateResumeButton() {
  const res = await browser.runtime.sendMessage({ type: "get-job" });
  const job = res && res.job;
  if (!job || !["paused", "cancelled", "error", "needs_reload"].includes(job.status)) {
    resumeBtn.style.display = "none";
    return;
  }
  const tab = await getActiveTab();
  const url = tab && tab.url ? tab.url : "";
  if (!pageCtx || !job.db) {
    resumeBtn.style.display = "none";
    return;
  }
  const matches =
    job.db === pageCtx.db &&
    (job.allYears || !pageCtx.year || job.year === pageCtx.year);
  if (!matches && !new RegExp(`/${job.juris || ".."}/${job.db}/`, "i").test(url)) {
    resumeBtn.style.display = "none";
    return;
  }
  const left = job.pending != null ? job.pending : "?";
  const done = job.completed || 0;
  resumeBtn.style.display = "block";
  resumeBtn.textContent = `Resume (${done} done, ${left} left)`;
  if (job.allYears) allYearsEl.checked = true;
  if (job.subfolder) subfolderEl.value = job.subfolder;
}

async function refreshPageInfo() {
  const tab = await getActiveTab();
  const url = tab && tab.url ? tab.url : "";
  if (!/canlii\.org\/(?:en\/)?[^/]+\/[^/]+/i.test(url)) {
    listingUrl = null;
    pageCtx = null;
    pageHint.textContent = "Open e.g. …/on/onlrb/nav/date/2026";
    startBtn.disabled = true;
    resumeBtn.style.display = "none";
    return;
  }
  listingUrl = url;
  startBtn.disabled = false;

  const res = await tabMessage("parse-page", { url, allYears: allYearsEl.checked });
  if (!res.ok) {
    pageHint.textContent = res.error || "Could not read page.";
    startBtn.disabled = true;
    return;
  }

  pageCtx = res.parsed;
  const { juris, db, year } = res.parsed;
  if (allYearsEl.checked && res.years) {
    pageHint.textContent = `${juris}/${db} — ${res.years.length} years (${res.years[0]}…${res.years[res.years.length - 1]})`;
    if (!subfolderEl.value) subfolderEl.value = `canlii/${db}`;
    startBtn.textContent = `Download ALL years (${res.years.length})`;
  } else if (year) {
    pageHint.textContent = `${juris}/${db}/${year} — ${res.count} documents`;
    if (!subfolderEl.value) subfolderEl.value = `canlii/${db}/${year}`;
    startBtn.textContent = `Download ${year} (${res.count} PDFs)`;
  } else if (res.years) {
    pageHint.textContent = `${juris}/${db} — ${res.years.length} years available`;
    if (!subfolderEl.value) subfolderEl.value = `canlii/${db}`;
    startBtn.textContent = "Download ALL years";
  }
  await updateResumeButton();
}

async function pollProgress() {
  const res = await browser.runtime.sendMessage({ type: "get-progress" });
  const p = res.progress;
  if (!p) return;
  if (p.status === "running") {
    const yr = p.year ? ` year ${p.year}` : "";
    const batch = p.batch ? ` batch ${p.batch}` : "";
    const had = p.alreadyDone ? `, ${p.alreadyDone} already had` : "";
    const skip = p.skipped ? `, ${p.skipped} skipped` : "";
    setStatus(
      `Downloading ${p.current} / ${p.total}${had}${skip}${yr}${batch}\n${p.last || "…"}`,
      "ok"
    );
    startBtn.disabled = true;
    resumeBtn.disabled = true;
  } else if (p.status === "done") {
    const had = p.alreadyDone ? `, ${p.alreadyDone} already had` : "";
    const skip = p.skipped ? ` (${p.skipped} skipped)` : "";
    setStatus(`Done — ${p.current} saved${had}${skip}.`, "ok");
    startBtn.disabled = false;
    resumeBtn.disabled = false;
    clearInterval(pollTimer);
    pollTimer = null;
    updateResumeButton();
  } else if (p.status === "error") {
    setStatus(`Stopped at ${p.current}/${p.total}\n${p.error}`, "err");
    startBtn.disabled = false;
    resumeBtn.disabled = false;
    clearInterval(pollTimer);
    pollTimer = null;
    updateResumeButton();
  } else if (p.status === "cancelled") {
    setStatus("Paused — click Resume to continue.", "");
    startBtn.disabled = false;
    resumeBtn.disabled = false;
    updateResumeButton();
  } else if (p.status === "needs_reload") {
    setStatus(
      p.error ||
        "Reload the CanLII page (Tor: New Identity), solve captcha, then Resume.",
      "err"
    );
    startBtn.disabled = false;
    resumeBtn.disabled = false;
    clearInterval(pollTimer);
    pollTimer = null;
    updateResumeButton();
  }
}

async function startDownload(resume) {
  if (!listingUrl && !resume) return;
  setStatus(resume ? "Resuming…" : "Starting…", "");
  const tab = await getActiveTab();
  if (!tab || !tab.id) {
    setStatus("No active tab.", "err");
    return;
  }

  let allYears = allYearsEl.checked;
  let subfolder = subfolderEl.value.trim();
  if (resume) {
    const jobRes = await browser.runtime.sendMessage({ type: "get-job" });
    const job = jobRes && jobRes.job;
    if (job) {
      allYears = !!job.allYears;
      subfolder = job.subfolder || subfolder;
      listingUrl = job.listingUrl || listingUrl;
    }
  }

  const res = await browser.runtime.sendMessage({
    type: "start-downloads",
    listingTabId: tab.id,
    listingUrl,
    allYears,
    resume: !!resume,
    batchSize: Number(batchSizeEl.value) || 10,
    batchPauseMs: Number(batchPauseEl.value) || 3000,
    subfolder: allYears ? subfolder.replace(/\/\d{4}\/?$/, "") : subfolder,
    skipDone: skipDoneEl.checked,
  });
  if (!res.ok) {
    setStatus(res.error || "Failed to start.", "err");
    return;
  }
  setStatus(resume ? "Resuming…" : allYears ? "Downloading all years…" : "Downloading…", "ok");
  if (!pollTimer) {
    pollTimer = setInterval(pollProgress, 800);
  }
  pollProgress();
}

allYearsEl.addEventListener("change", () => refreshPageInfo());

startBtn.addEventListener("click", () => startDownload(false));
resumeBtn.addEventListener("click", () => startDownload(true));

cancelBtn.addEventListener("click", async () => {
  await browser.runtime.sendMessage({ type: "cancel" });
  setStatus("Paused — click Resume to continue.", "");
  startBtn.disabled = false;
  resumeBtn.disabled = false;
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  updateResumeButton();
});

refreshPageInfo();
pollProgress();
