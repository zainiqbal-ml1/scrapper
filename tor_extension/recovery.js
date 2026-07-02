/* global browser, CanliiStore, CanliiLib */

const CanliiRecovery = (() => {
  const SETTINGS_KEY = "canliiExtSettings";
  const NATIVE_HOST = "com.canlii.tor";
  const MAX_RECOVERY = 12;
  const CANLII_ORIGINS = ["https://www.canlii.org", "https://canlii.org"];

  let startDownloadsFn = null;
  let pingTabFn = null;
  let waitTabFn = null;
  let saveProgressFn = null;

  function init({ startDownloads, pingTab, waitTab, saveProgress }) {
    startDownloadsFn = startDownloads;
    pingTabFn = pingTab;
    waitTabFn = waitTab;
    saveProgressFn = saveProgress;
  }

  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  async function getSettings() {
    const data = await browser.storage.local.get(SETTINGS_KEY);
    const stored = data[SETTINGS_KEY] || {};
    return { autoRecover: stored.autoRecover !== false };
  }

  function listingUrlForJob(job) {
    const url = job.listingUrl || "";
    const ctx = CanliiLib.parseDbContext(url);
    if (!ctx) return url || "https://www.canlii.org/";
    if (ctx.year) return CanliiLib.listingUrl(ctx.juris, ctx.db, ctx.year);
    if (job.year) return CanliiLib.listingUrl(ctx.juris, ctx.db, job.year);
    return CanliiLib.listingUrl(ctx.juris, ctx.db, String(new Date().getFullYear()));
  }

  async function requestNewIdentity() {
    try {
      const res = await browser.runtime.sendNativeMessage(NATIVE_HOST, {
        action: "new_identity",
      });
      return res && res.ok ? res : null;
    } catch (e) {
      console.warn("[canlii-ext] native New Identity failed:", e);
      return null;
    }
  }

  async function clearCanliiSession() {
    try {
      await browser.browsingData.remove(
        { origins: CANLII_ORIGINS },
        {
          cookies: true,
          cache: true,
          localStorage: true,
          indexedDB: true,
          serviceWorkers: true,
        }
      );
    } catch (e) {
      console.warn("[canlii-ext] could not clear browsing data:", e);
    }
  }

  async function closeCanliiTabs() {
    const tabs = await browser.tabs.query({ url: "*://www.canlii.org/*" });
    await Promise.all(
      tabs.map((t) => (t.id ? browser.tabs.remove(t.id).catch(() => {}) : null))
    );
  }

  async function openFreshWindow(listingUrl) {
    const win = await browser.windows.create({ url: listingUrl, focused: true });
    const tabId = win.tabs && win.tabs[0] && win.tabs[0].id;
    if (!tabId) throw new Error("Could not open a fresh CanLII window.");
    await waitTabFn(tabId);
    return { windowId: win.id, tabId };
  }

  async function closeOtherWindows(keepWindowId) {
    const wins = await browser.windows.getAll({ windowTypes: ["normal"] });
    for (const w of wins) {
      if (w.id !== keepWindowId) {
        await browser.windows.remove(w.id).catch(() => {});
      }
    }
  }

  async function waitForSession(tabId, listingUrl, timeoutMs = 600000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (await pingTabFn(tabId)) {
        try {
          const res = await browser.tabs.sendMessage(tabId, {
            type: "check-session",
            listingUrl,
          });
          if (res && res.ok) return true;
        } catch (e) {
          /* not ready */
        }
      }
      await sleep(4000);
    }
    return false;
  }

  async function resumeAfterRecovery(job) {
    if (!startDownloadsFn || !job) return false;
    const needUrl = listingUrlForJob(job);
    let tabId = null;

    const tabs = await browser.tabs.query({ url: "*://www.canlii.org/*" });
    if (tabs.length && tabs[0].id) {
      tabId = tabs[0].id;
      await browser.tabs.update(tabId, { url: needUrl, active: true });
    } else {
      const win = await browser.windows.create({ url: needUrl, focused: true });
      tabId = win.tabs && win.tabs[0] && win.tabs[0].id;
    }
    if (!tabId) return false;

    await waitTabFn(tabId);
    const ready = await waitForSession(tabId, needUrl);
    if (!ready) {
      await CanliiStore.saveJob({
        ...job,
        status: "needs_reload",
        pendingNewIdentityResume: false,
        listingUrl: needUrl,
      });
      if (saveProgressFn) {
        await saveProgressFn({
          status: "needs_reload",
          listingUrl: needUrl,
          current: job.completed || 0,
          total: job.total || 0,
          skipped: job.skipped || 0,
          alreadyDone: job.alreadyDone || 0,
          error:
            "New Identity done — solve captcha on the CanLII page, then click Resume.",
          mode: "pdf-tabs",
        });
      }
      return false;
    }

    await CanliiStore.saveJob({
      ...job,
      status: "running",
      pendingNewIdentityResume: false,
      listingUrl: needUrl,
    });

    await startDownloadsFn(
      {
        resume: true,
        listingUrl: needUrl,
        allYears: job.allYears,
        subfolder: job.subfolder,
        batchSize: job.batchSize,
        batchPauseMs: job.batchPauseMs,
        skipDone: true,
        autoRecovery: true,
      },
      tabId
    );
    return true;
  }

  let resumeInFlight = false;

  async function continuePendingNewIdentity() {
    if (resumeInFlight) return;
    const job = await CanliiStore.getJob();
    if (!job || !job.pendingNewIdentityResume) return;

    resumeInFlight = true;
    try {
      if (saveProgressFn) {
        await saveProgressFn({
          status: "recovering",
          listingUrl: job.listingUrl,
          current: job.completed || 0,
          total: job.total || 0,
          skipped: job.skipped || 0,
          alreadyDone: job.alreadyDone || 0,
          error: "New Identity complete — reopening CanLII and resuming…",
          mode: "pdf-tabs",
        });
      }
      await sleep(6000);
      await resumeAfterRecovery(job);
    } catch (e) {
      console.warn("[canlii-ext] post New Identity resume failed:", e);
      await CanliiStore.saveJob({
        ...job,
        status: "needs_reload",
        pendingNewIdentityResume: false,
      });
    } finally {
      resumeInFlight = false;
    }
  }

  async function fallbackRecover({ job, listingUrl, progress, message, needUrl }) {
    await closeCanliiTabs();
    await clearCanliiSession();
    await sleep(2500);

    const { windowId, tabId } = await openFreshWindow(needUrl);
    await closeOtherWindows(windowId);

    const ready = await waitForSession(tabId, needUrl);
    if (!ready) {
      await CanliiStore.saveJob({
        ...job,
        status: "needs_reload",
        pendingNewIdentityResume: false,
        listingUrl: needUrl,
      });
      if (saveProgressFn) {
        await saveProgressFn({
          status: "needs_reload",
          listingUrl: needUrl,
          current: progress.completed,
          total: progress.total,
          skipped: progress.skipped,
          alreadyDone: progress.alreadyDone,
          error:
            "Recovery waiting for captcha — solve it on CanLII, then click Resume.",
          source: progress.source,
          mode: "pdf-tabs",
        });
      }
      return false;
    }

    await startDownloadsFn(
      {
        resume: true,
        listingUrl: needUrl,
        allYears: job.allYears,
        subfolder: job.subfolder,
        batchSize: job.batchSize,
        batchPauseMs: job.batchPauseMs,
        skipDone: true,
        autoRecovery: true,
      },
      tabId
    );
    return true;
  }

  async function tryRecover({ job, listingUrl, progress, message }) {
    if (!startDownloadsFn) return false;

    const settings = await getSettings();
    if (!settings.autoRecover) return false;

    const attempts = (job.recoveryAttempts || 0) + 1;
    if (attempts > MAX_RECOVERY) return false;

    const needUrl = listingUrlForJob(job);
    const note =
      message ||
      "Blocked — requesting Tor New Identity, then auto-resuming.";

    await CanliiStore.saveJob({
      ...job,
      status: "recovering",
      recoveryAttempts: attempts,
      listingUrl: needUrl,
      pendingNewIdentityResume: true,
      completed: progress.completed,
      skipped: progress.skipped,
      alreadyDone: progress.alreadyDone,
      total: progress.total,
    });

    if (saveProgressFn) {
      await saveProgressFn({
        status: "recovering",
        listingUrl: needUrl,
        current: progress.completed,
        total: progress.total,
        skipped: progress.skipped,
        alreadyDone: progress.alreadyDone,
        error: `${note} (recovery ${attempts}/${MAX_RECOVERY})`,
        source: progress.source,
        mode: "pdf-tabs",
      });
    }

    const native = await requestNewIdentity();
    if (native && native.method === "tor-menu") {
      return true;
    }

    await CanliiStore.saveJob({
      ...job,
      status: "recovering",
      recoveryAttempts: attempts,
      listingUrl: needUrl,
      pendingNewIdentityResume: false,
    });

    const detail =
      native && native.detail
        ? String(native.detail)
        : "Install native host: run ./install_tor_extension.sh";
    if (saveProgressFn) {
      await saveProgressFn({
        status: "recovering",
        listingUrl: needUrl,
        current: progress.completed,
        total: progress.total,
        error: `New Identity menu unavailable (${detail}) — using fallback recovery…`,
        source: progress.source,
        mode: "pdf-tabs",
      });
    }

    return fallbackRecover({ job, listingUrl, progress, message, needUrl });
  }

  return {
    init,
    tryRecover,
    continuePendingNewIdentity,
    listingUrlForJob,
    getSettings,
    MAX_RECOVERY,
  };
})();
