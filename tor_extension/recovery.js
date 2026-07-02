/* global browser, CanliiStore, CanliiLib */

const CanliiRecovery = (() => {
  const SETTINGS_KEY = "canliiExtSettings";
  const MAX_RECOVERY = 12;

  const USER_NEW_IDENTITY_MSG =
    "Blocked — click Tor menu → New Identity. " +
    "Your CanLII listing page will reopen and downloads resume automatically. " +
    "Solve captcha if shown.";

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

  async function openListingPage(needUrl) {
    const wins = await browser.windows.getAll({ windowTypes: ["normal"] });
    let tabId = null;

    if (wins.length && wins[0].id) {
      const tabs = await browser.tabs.query({ windowId: wins[0].id });
      if (tabs.length && tabs[0].id) {
        tabId = tabs[0].id;
        await browser.tabs.update(tabId, { url: needUrl, active: true });
      } else {
        const tab = await browser.tabs.create({ windowId: wins[0].id, url: needUrl, active: true });
        tabId = tab.id;
      }
    } else {
      const win = await browser.windows.create({ url: needUrl, focused: true });
      tabId = win.tabs && win.tabs[0] && win.tabs[0].id;
    }

    if (!tabId) return null;
    await waitTabFn(tabId);
    return tabId;
  }

  async function resumeAfterNewIdentity(job) {
    if (!startDownloadsFn || !job) return false;
    const needUrl = listingUrlForJob(job);

    if (saveProgressFn) {
      await saveProgressFn({
        status: "recovering",
        listingUrl: needUrl,
        current: job.completed || 0,
        total: job.total || 0,
        skipped: job.skipped || 0,
        alreadyDone: job.alreadyDone || 0,
        error: "New Identity done — reopening your CanLII page…",
        mode: "pdf-tabs",
      });
    }

    await sleep(3000);
    const tabId = await openListingPage(needUrl);
    if (!tabId) return false;

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
            "Page reopened — solve captcha on CanLII, then click Resume.",
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
    if (job.status !== "needs_new_identity" && job.status !== "recovering") return;

    resumeInFlight = true;
    try {
      await resumeAfterNewIdentity(job);
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

  async function tryRecover({ job, listingUrl, progress, message }) {
    if (!startDownloadsFn) return false;

    const settings = await getSettings();
    const attempts = (job.recoveryAttempts || 0) + 1;
    if (attempts > MAX_RECOVERY) return false;

    const needUrl = listingUrlForJob(job);
    const auto = settings.autoRecover !== false;

    await CanliiStore.saveJob({
      ...job,
      status: auto ? "needs_new_identity" : "needs_reload",
      recoveryAttempts: attempts,
      listingUrl: needUrl,
      pendingNewIdentityResume: auto,
      completed: progress.completed,
      skipped: progress.skipped,
      alreadyDone: progress.alreadyDone,
      total: progress.total,
    });

    const userMsg = auto
      ? message || USER_NEW_IDENTITY_MSG
      : message ||
        "Blocked — click Tor → New Identity, reopen this listing, then click Resume.";

    if (saveProgressFn) {
      await saveProgressFn({
        status: auto ? "needs_new_identity" : "needs_reload",
        listingUrl: needUrl,
        current: progress.completed,
        total: progress.total,
        skipped: progress.skipped,
        alreadyDone: progress.alreadyDone,
        error: userMsg,
        source: progress.source,
        mode: "pdf-tabs",
      });
    }

    return true;
  }

  return {
    init,
    tryRecover,
    continuePendingNewIdentity,
    listingUrlForJob,
    getSettings,
    USER_NEW_IDENTITY_MSG,
    MAX_RECOVERY,
  };
})();
