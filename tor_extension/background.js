/* global browser, CanliiLib */

const PROGRESS_KEY = "canliiPdfProgress";
const CONSECUTIVE_404_LIMIT = 3;
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
    const err = String(e.message || e);
    if (/receiving end does not exist|could not establish connection/i.test(err)) {
      return { connectionLost: true };
    }
    throw new Error(
      `${task.filename}: keep the listing tab open — ${err}`
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

async function pingContentTab(tabId) {
  try {
    const res = await browser.tabs.sendMessage(tabId, { type: "ping" });
    return !!(res && res.ok);
  } catch (e) {
    return false;
  }
}

async function waitForContentTab(tabId, timeoutMs = 45000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const tab = await browser.tabs.get(tabId);
      if (tab.status === "complete" && (await pingContentTab(tabId))) {
        return tabId;
      }
    } catch (e) {
      throw new Error("Listing tab closed — reopen the CanLII page and click Resume.");
    }
    await sleep(600);
  }
  throw new Error(
    "Could not connect — refresh the CanLII page, solve captcha if shown, then Resume."
  );
}

function listingUrlForContext(job, msg) {
  const url = msg.listingUrl || (job && job.listingUrl) || "";
  const ctx = CanliiLib.parseDbContext(url);
  if (!ctx) return url;
  if (ctx.year) {
    return CanliiLib.listingUrl(ctx.juris, ctx.db, ctx.year);
  }
  if (job && job.year) {
    return CanliiLib.listingUrl(ctx.juris, ctx.db, job.year);
  }
  const y = new Date().getFullYear();
  return CanliiLib.listingUrl(ctx.juris, ctx.db, String(y));
}

async function ensureListingTab(msg) {
  const job = msg.resume ? await CanliiStore.getJob() : null;
  const targetUrl = msg.listingUrl || (job && job.listingUrl);
  const ctx = CanliiLib.parseDbContext(targetUrl || "");
  const needUrl = listingUrlForContext(job, msg);

  if (msg.listingTabId) {
    try {
      await browser.tabs.get(msg.listingTabId);
      if (await pingContentTab(msg.listingTabId)) {
        return msg.listingTabId;
      }
      await browser.tabs.update(msg.listingTabId, { url: needUrl, active: true });
      return waitForContentTab(msg.listingTabId);
    } catch (e) {
      /* try other tabs */
    }
  }

  const tabs = await browser.tabs.query({ url: "*://www.canlii.org/*" });
  let fallback = null;
  for (const t of tabs) {
    if (!t.id) continue;
    const pageCtx = CanliiLib.parseDbContext(t.url || "");
    if (!ctx || !pageCtx) continue;
    if (pageCtx.juris === ctx.juris && pageCtx.db === ctx.db) {
      fallback = t;
      if (await pingContentTab(t.id)) return t.id;
    }
  }

  if (fallback && fallback.id) {
    await browser.tabs.update(fallback.id, { url: needUrl, active: true });
    return waitForContentTab(fallback.id);
  }

  const tab = await browser.tabs.create({ url: needUrl, active: true });
  return waitForContentTab(tab.id);
}

function friendlyConnectionError(err) {
  const msg = String((err && err.message) || err || "");
  if (/receiving end does not exist|could not establish connection/i.test(msg)) {
    return "Refresh the CanLII listing page, solve captcha if needed, then click Resume.";
  }
  return msg;
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
  let consecutive404 = 0;
  let streak404Urls = [];
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
      listingTabId,
    });
  }

  async function pauseForCircuitReload(customMsg) {
    await CanliiStore.unmarkSkippedMany(streak404Urls);
    const errMsg =
      customMsg ||
      `${CONSECUTIVE_404_LIMIT} PDFs returned 404 in a row — likely blocked. ` +
      "Reload this CanLII page (Tor: New Identity or refresh), solve captcha, then click Resume.";
    await touchJob("needs_reload");
    await saveProgress({
      status: "needs_reload",
      listingUrl,
      current: completed,
      total: tasks.length,
      skipped,
      alreadyDone,
      error: errMsg,
      source,
      mode: "pdf-tabs",
    });
    await saveYearIndexes(yearRecords, outcomes, jsonBase).catch(() => {});
    return {
      needsReload: true,
      total: tasks.length,
      completed,
      skipped,
      alreadyDone,
    };
  }

  async function handleTask404(task) {
    streak404Urls.push(task.pdfUrl);
    consecutive404 += 1;
    outcomes.set(task.pdfUrl, { skipped: true, error: "HTTP 404" });

    if (consecutive404 >= CONSECUTIVE_404_LIMIT) {
      return { pause: true };
    }

    await CanliiStore.markSkipped(task.pdfUrl);
    skipped += 1;
    return { pause: false };
  }

  function noteSuccess() {
    consecutive404 = 0;
    streak404Urls = [];
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
        if (result.connectionLost) {
          return pauseForCircuitReload(
            "Lost connection to the listing tab — refresh this CanLII page, solve captcha, then Resume."
          );
        }
        if (result.skipped) {
          const h404 = await handleTask404(task);
          if (h404.pause) {
            return pauseForCircuitReload();
          }
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
        noteSuccess();
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
          const h404 = await handleTask404(task);
          if (h404.pause) {
            return pauseForCircuitReload();
          }
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
              const h404 = await handleTask404(task);
              if (h404.pause) {
                return pauseForCircuitReload();
              }
              continue;
            }
            noteSuccess();
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
  msg = await mergeResumeJob(msg);
  let resolvedTabId;
  try {
    resolvedTabId = await ensureListingTab({ ...msg, listingTabId });
  } catch (e) {
    throw new Error(friendlyConnectionError(e));
  }

  const prepType = msg.allYears ? "prepare-all-years" : "prepare-tasks";
  let prep;
  try {
    prep = await browser.tabs.sendMessage(resolvedTabId, {
      type: prepType,
      listingUrl: msg.listingUrl,
      subfolder: msg.subfolder,
    });
  } catch (e) {
    throw new Error(friendlyConnectionError(e));
  }
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
    listingTabId: resolvedTabId,
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
        error: friendlyConnectionError(e),
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
