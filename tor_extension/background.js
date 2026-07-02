/* global browser, CanliiLib */

const PROGRESS_KEY = "canliiPdfProgress";
let cancelRequested = false;

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function saveProgress(progress) {
  await browser.storage.local.set({ [PROGRESS_KEY]: progress });
}

function waitForDownload(downloadId, timeoutMs = 180000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      browser.downloads.onChanged.removeListener(onChanged);
      reject(new Error("download timed out"));
    }, timeoutMs);

    function onChanged(delta) {
      if (delta.id !== downloadId) return;
      if (delta.state && delta.state.current === "complete") {
        cleanup();
        resolve();
      }
      if (delta.error) {
        cleanup();
        reject(new Error(delta.error.current || "download failed"));
      }
    }

    function cleanup() {
      clearTimeout(timer);
      browser.downloads.onChanged.removeListener(onChanged);
    }

    browser.downloads.onChanged.addListener(onChanged);
    browser.downloads.search({ id: downloadId }).then((items) => {
      const item = items[0];
      if (!item) return;
      if (item.state === "complete") {
        cleanup();
        resolve();
      } else if (item.state === "interrupted") {
        cleanup();
        reject(new Error(item.error || "interrupted"));
      }
    });
  });
}

async function savePdfBase64(base64, filename) {
  const bin = atob(base64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const blob = new Blob([bytes], { type: "application/pdf" });
  const blobUrl = URL.createObjectURL(blob);
  try {
    const downloadId = await browser.downloads.download({
      url: blobUrl,
      filename,
      conflictAction: "uniquify",
      saveAs: false,
    });
    await waitForDownload(downloadId);
  } finally {
    setTimeout(() => URL.revokeObjectURL(blobUrl), 120000);
  }
}

async function saveJsonFile(records, filename) {
  const json = JSON.stringify(records, null, 2);
  const blob = new Blob([json], { type: "application/json;charset=utf-8" });
  const blobUrl = URL.createObjectURL(blob);
  try {
    const downloadId = await browser.downloads.download({
      url: blobUrl,
      filename,
      conflictAction: "overwrite",
      saveAs: false,
    });
    await waitForDownload(downloadId);
  } finally {
    setTimeout(() => URL.revokeObjectURL(blobUrl), 120000);
  }
}

function applyOutcomesToRecords(records, outcomes) {
  return records.map((rec) => {
    const o = outcomes.get(rec.pdf_url);
    if (!o) return { ...rec };
    if (o.skipped || o.error) {
      return { ...rec, file: null, error: o.error || "skipped" };
    }
    return { ...rec };
  });
}

async function saveYearIndexes(yearRecords, outcomes, jsonBase) {
  if (!yearRecords || !Object.keys(yearRecords).length) return;
  for (const [year, records] of Object.entries(yearRecords)) {
    const updated = applyOutcomesToRecords(records, outcomes);
    const path = CanliiLib.jsonPathForYear(jsonBase, year);
    await saveJsonFile(updated, path);
  }
}

async function filterOnDisk(tasks) {
  let recent;
  try {
    recent = await browser.downloads.search({
      state: "complete",
      orderBy: ["-startTime"],
      limit: 8000,
    });
  } catch (e) {
    return { tasks, fromDisk: 0 };
  }
  const paths = new Set();
  for (const d of recent) {
    if (d.exists === false) continue;
    paths.add(String(d.filename).replace(/\\/g, "/"));
  }
  const remaining = [];
  let fromDisk = 0;
  for (const t of tasks) {
    const saveAs = String(t.saveAs || "").replace(/\\/g, "/");
    const hit =
      paths.has(saveAs) ||
      [...paths].some(
        (p) => p === saveAs || p.endsWith("/" + t.filename)
      );
    if (hit) {
      await CanliiStore.markDone(t.pdfUrl);
      fromDisk += 1;
      continue;
    }
    remaining.push(t);
  }
  return { tasks: remaining, fromDisk };
}

async function filterPendingTasks(tasks, skipDone) {
  const { tasks: afterStorage, alreadyDone: fromStorage } =
    await CanliiStore.filterByStorage(tasks, skipDone);
  if (!skipDone) {
    return { tasks: afterStorage, alreadyDone: 0 };
  }
  const { tasks: afterDisk, fromDisk } = await filterOnDisk(afterStorage);
  return {
    tasks: afterDisk,
    alreadyDone: fromStorage + fromDisk,
  };
}

function formatProgressLine(prog) {
  const parts = [`${prog.current} / ${prog.total}`];
  if (prog.alreadyDone) parts.push(`${prog.alreadyDone} already had`);
  if (prog.skipped) parts.push(`${prog.skipped} skipped`);
  return parts.join(", ");
}

function waitTabComplete(tabId, timeoutMs = 120000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      browser.tabs.onUpdated.removeListener(onUpdated);
      reject(new Error("tab load timed out"));
    }, timeoutMs);

    function onUpdated(id, info) {
      if (id !== tabId) return;
      if (info.status === "complete") {
        cleanup();
        resolve();
      }
    }

    function cleanup() {
      clearTimeout(timer);
      browser.tabs.onUpdated.removeListener(onUpdated);
    }

    browser.tabs.onUpdated.addListener(onUpdated);
    browser.tabs.get(tabId).then((tab) => {
      if (tab.status === "complete") {
        cleanup();
        resolve();
      }
    }).catch(reject);
  });
}

async function fetchPdfFromListing(listingTabId, task) {
  let res;
  try {
    res = await browser.tabs.sendMessage(listingTabId, {
      type: "fetch-pdf-b64",
      pdfPath: task.pdfPath,
      pdfPaths: task.pdfPaths || [task.pdfPath],
    });
  } catch (e) {
    throw new Error(
      `${task.filename}: keep the listing tab open — ${e.message || e}`
    );
  }
  if (!res || !res.ok) {
    const status = res && res.status;
    const err = (res && res.error) || "fetch failed";
    if (status === 404 || err === "not-found") {
      return { skipped: true, reason: "404" };
    }
    if (res && res.error === "not-pdf") {
      throw new Error(`${task.filename}: captcha/block (not a PDF)`);
    }
    throw new Error(
      `${task.filename}: HTTP ${status || err} — ${task.pdfUrl}`
    );
  }
  await savePdfBase64(res.base64, task.saveAs);
  return { skipped: false };
}

function isRetryableBlock(msg) {
  return /403|captcha|forbidden|blocked/i.test(msg);
}

function isSkippableError(msg) {
  return /HTTP 404\b|not-found|\b404\b.*skip/i.test(msg);
}

async function closeTabs(tabIds) {
  await Promise.all(tabIds.map((id) => browser.tabs.remove(id).catch(() => {})));
}

async function runTabBatches({
  tasks,
  listingTabId,
  batchSize = 10,
  batchPauseMs = 3000,
  listingUrl,
  source,
  yearRecords,
  jsonBase,
  alreadyDone = 0,
  jobMeta,
}) {
  if (!listingTabId) {
    throw new Error("Listing tab id missing — reload the nav/date page and retry.");
  }

  cancelRequested = false;
  let completed = 0;
  let skipped = 0;
  const outcomes = new Map();

  async function touchJob(status) {
    if (!jobMeta) return;
    await CanliiStore.saveJob({
      ...jobMeta,
      status: status || "running",
      completed,
      skipped,
      pending: tasks.length - completed,
      alreadyDone,
      total: tasks.length,
    });
  }

  await saveProgress({
    status: "running",
    listingUrl,
    current: 0,
    total: tasks.length,
    skipped: 0,
    alreadyDone,
    source,
    mode: "pdf-tabs",
  });
  await touchJob("running");

  for (let i = 0; i < tasks.length; i += batchSize) {
    if (cancelRequested) {
      await touchJob("paused");
      await saveProgress({
        status: "cancelled",
        listingUrl,
        current: completed,
        total: tasks.length,
        skipped,
        alreadyDone,
      });
      await saveYearIndexes(yearRecords, outcomes, jsonBase).catch(() => {});
      return { cancelled: true, total: tasks.length };
    }

    const batch = tasks.slice(i, i + batchSize);
    const slots = [];

    for (const task of batch) {
      if (cancelRequested) break;
      if (!task.pdfUrl || !task.pdfUrl.startsWith("https://")) {
        skipped += 1;
        outcomes.set(task.pdfUrl || task.filename, {
          skipped: true,
          error: "invalid url",
        });
        continue;
      }
      const opts = { url: task.pdfUrl, active: false, openerTabId: listingTabId };
      // Open PDF tab (try /en/ variant if primary 404s in tab)
      let tab;
      try {
        tab = await browser.tabs.create(opts);
      } catch (e) {
        const alt = task.pdfPaths && task.pdfPaths[1];
        if (!alt) throw e;
        tab = await browser.tabs.create({
          url: "https://www.canlii.org" + alt,
          active: false,
          openerTabId: listingTabId,
        });
      }
      slots.push({ tabId: tab.id, task });
      await sleep(400);
    }

    await Promise.all(slots.map((s) => waitTabComplete(s.tabId).catch(() => {})));

    for (const slot of slots) {
      if (cancelRequested) break;
      const { tabId, task } = slot;
      try {
        const result = await fetchPdfFromListing(listingTabId, task);
        if (result.skipped) {
          skipped += 1;
          await CanliiStore.markSkipped(task.pdfUrl);
          outcomes.set(task.pdfUrl, { skipped: true, error: "HTTP 404" });
          await touchJob("running");
          await saveProgress({
            status: "running",
            listingUrl,
            current: completed,
            total: tasks.length,
            skipped,
            alreadyDone,
            last: `skipped 404: ${task.filename}`,
            year: task.year || (task.saveAs && task.saveAs.match(/\/(\d{4})\/[^/]+$/)?.[1]),
            source,
            mode: "pdf-tabs",
            batch: Math.floor(i / batchSize) + 1,
          });
          continue;
        }
        completed += 1;
        await CanliiStore.markDone(task.pdfUrl);
        outcomes.set(task.pdfUrl, { ok: true });
        await touchJob("running");
        await saveProgress({
          status: "running",
          listingUrl,
          current: completed,
          total: tasks.length,
          skipped,
          alreadyDone,
          last: task.filename,
          year: task.year || (task.saveAs && task.saveAs.match(/\/(\d{4})\/[^/]+$/)?.[1]),
          source,
          mode: "pdf-tabs",
          batch: Math.floor(i / batchSize) + 1,
        });
      } catch (e) {
        const msg = String(e.message || e);
        if (isSkippableError(msg)) {
          skipped += 1;
          await CanliiStore.markSkipped(task.pdfUrl);
          outcomes.set(task.pdfUrl, { skipped: true, error: msg });
          await touchJob("running");
          await saveProgress({
            status: "running",
            listingUrl,
            current: completed,
            total: tasks.length,
            skipped,
            alreadyDone,
            last: `skipped: ${task.filename}`,
            source,
            mode: "pdf-tabs",
          });
          continue;
        }
        if (isRetryableBlock(msg)) {
          await sleep(10000);
          try {
            await browser.tabs.update(tabId, { url: task.pdfUrl });
            await waitTabComplete(tabId);
            const retry = await fetchPdfFromListing(listingTabId, task);
            if (retry.skipped) {
              skipped += 1;
              await CanliiStore.markSkipped(task.pdfUrl);
              outcomes.set(task.pdfUrl, { skipped: true, error: "HTTP 404" });
              continue;
            }
            completed += 1;
            await CanliiStore.markDone(task.pdfUrl);
            outcomes.set(task.pdfUrl, { ok: true });
            continue;
          } catch (e2) {
            await touchJob("error");
            await saveProgress({
              status: "error",
              listingUrl,
              current: completed,
              total: tasks.length,
              skipped,
              error: `${task.filename}: ${e2.message || e2}`,
            });
            throw e2;
          }
        }
        await touchJob("error");
        await saveProgress({
          status: "error",
          listingUrl,
          current: completed,
          total: tasks.length,
          skipped,
          alreadyDone,
          error: msg,
        });
        await saveYearIndexes(yearRecords, outcomes, jsonBase).catch(() => {});
        throw e;
      } finally {
        await browser.tabs.remove(tabId).catch(() => {});
      }
      await sleep(600);
    }

    if (i + batchSize < tasks.length && batchPauseMs > 0) {
      await saveProgress({
        status: "running",
        listingUrl,
        current: completed,
        total: tasks.length,
        last: `batch pause (${batchPauseMs}ms)`,
        source,
        mode: "pdf-tabs",
      });
      await sleep(batchPauseMs);
    }
  }

  await saveYearIndexes(yearRecords, outcomes, jsonBase).catch((e) => {
    console.warn("[canlii-ext] year JSON save failed:", e);
  });

  await CanliiStore.clearJob();
  await saveProgress({
    status: "done",
    listingUrl,
    current: completed,
    total: tasks.length,
    skipped,
    alreadyDone,
    source,
    mode: "pdf-tabs",
  });
  return { total: tasks.length, completed, skipped, alreadyDone, source };
}

async function mergeResumeJob(msg) {
  if (!msg.resume) return msg;
  const job = await CanliiStore.getJob();
  if (!job || !CanliiStore.isResumable(job)) return msg;
  return {
    ...msg,
    listingUrl: msg.listingUrl || job.listingUrl,
    allYears: msg.allYears != null ? msg.allYears : job.allYears,
    subfolder: msg.subfolder || job.subfolder,
    batchSize: msg.batchSize || job.batchSize || 10,
    batchPauseMs: msg.batchPauseMs != null ? msg.batchPauseMs : job.batchPauseMs ?? 3000,
    skipDone: true,
  };
}

async function startDownloads(msg, listingTabId) {
  if (!listingTabId) {
    throw new Error("Reload the listing page and try again.");
  }
  msg = await mergeResumeJob(msg);
  const prepType = msg.allYears ? "prepare-all-years" : "prepare-tasks";
  const prep = await browser.tabs.sendMessage(listingTabId, {
    type: prepType,
    listingUrl: msg.listingUrl,
    subfolder: msg.subfolder,
  });
  if (!prep || !prep.ok) {
    throw new Error((prep && prep.error) || "Could not read document list.");
  }

  const skipDone = msg.skipDone !== false;
  const { tasks, alreadyDone } = await filterPendingTasks(prep.tasks, skipDone);
  if (!tasks.length) {
    const n = alreadyDone || prep.totalListed || 0;
    throw new Error(
      n > 0 ? `All ${n} PDFs already downloaded — nothing left.` : "No documents found."
    );
  }

  const parsed = prep.parsed || {};
  const jobMeta = {
    listingUrl: msg.listingUrl,
    allYears: !!msg.allYears,
    subfolder: msg.subfolder,
    jsonBase: prep.jsonBase,
    batchSize: msg.batchSize || 10,
    batchPauseMs: msg.batchPauseMs ?? 3000,
    db: parsed.db,
    juris: parsed.juris,
    year: parsed.year,
    totalListed: prep.totalListed || prep.tasks.length,
    alreadyDone,
  };

  return runTabBatches({
    tasks,
    listingTabId,
    batchSize: msg.batchSize || 10,
    batchPauseMs: msg.batchPauseMs ?? 3000,
    listingUrl: msg.listingUrl,
    source: prep.source,
    years: prep.years,
    yearRecords: prep.yearRecords,
    jsonBase: prep.jsonBase,
    alreadyDone,
    jobMeta,
  });
}

browser.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "save-progress") {
    saveProgress(msg.progress).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg.type === "start-downloads") {
    const listingTabId = msg.listingTabId || (sender.tab && sender.tab.id);
    startDownloads(msg, listingTabId).catch((e) => {
      saveProgress({
        status: "error",
        listingUrl: msg.listingUrl,
        current: 0,
        total: 0,
        error: String(e.message || e),
      });
    });
    sendResponse({ ok: true, started: true });
    return true;
  }
  if (msg.type === "run-tab-batches") {
    runTabBatches(msg)
      .then((r) => sendResponse({ ok: true, ...r }))
      .catch((e) => sendResponse({ ok: false, error: String(e.message || e) }));
    return true;
  }
  if (msg.type === "get-progress") {
    browser.storage.local.get(PROGRESS_KEY).then((data) => {
      sendResponse({ ok: true, progress: data[PROGRESS_KEY] || null });
    });
    return true;
  }
  if (msg.type === "get-job") {
    CanliiStore.getJob().then((job) => sendResponse({ ok: true, job }));
    return true;
  }
  if (msg.type === "clear-job") {
    CanliiStore.clearJob().then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg.type === "cancel") {
    cancelRequested = true;
    saveProgress({ status: "cancelled" }).then(() => sendResponse({ ok: true }));
    return true;
  }
  return false;
});
