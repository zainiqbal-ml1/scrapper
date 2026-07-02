/* global browser */

const CanliiStore = (() => {
  const DONE_KEY = "canliiPdfDone";
  const SKIPPED_KEY = "canliiPdfSkipped";
  const DONE_PATHS_KEY = "canliiPdfDonePaths";
  const JOB_KEY = "canliiPdfJob";

  function normPath(p) {
    return String(p || "")
      .replace(/\\/g, "/")
      .replace(/ \(\d+\)(\.pdf)$/i, "$1");
  }

  async function getDoneKeys() {
    const data = await browser.storage.local.get([
      DONE_KEY,
      SKIPPED_KEY,
      DONE_PATHS_KEY,
    ]);
    return {
      urls: new Set([...(data[DONE_KEY] || []), ...(data[SKIPPED_KEY] || [])]),
      paths: new Set((data[DONE_PATHS_KEY] || []).map(normPath)),
    };
  }

  async function getDoneUrls() {
    const { urls } = await getDoneKeys();
    return urls;
  }

  async function markDone(pdfUrl, saveAs) {
    if (pdfUrl) {
      const data = await browser.storage.local.get(DONE_KEY);
      const done = data[DONE_KEY] || [];
      if (!done.includes(pdfUrl)) {
        done.push(pdfUrl);
        await browser.storage.local.set({ [DONE_KEY]: done });
      }
    }
    if (saveAs) {
      const p = normPath(saveAs);
      const data = await browser.storage.local.get(DONE_PATHS_KEY);
      const paths = data[DONE_PATHS_KEY] || [];
      if (!paths.includes(p)) {
        paths.push(p);
        await browser.storage.local.set({ [DONE_PATHS_KEY]: paths });
      }
    }
  }

  async function markSkipped(pdfUrl) {
    if (!pdfUrl) return;
    const data = await browser.storage.local.get(SKIPPED_KEY);
    const skipped = data[SKIPPED_KEY] || [];
    if (skipped.includes(pdfUrl)) return;
    skipped.push(pdfUrl);
    await browser.storage.local.set({ [SKIPPED_KEY]: skipped });
  }

  async function unmarkSkipped(pdfUrl) {
    if (!pdfUrl) return;
    const data = await browser.storage.local.get(SKIPPED_KEY);
    const skipped = (data[SKIPPED_KEY] || []).filter((u) => u !== pdfUrl);
    await browser.storage.local.set({ [SKIPPED_KEY]: skipped });
  }

  async function unmarkSkippedMany(urls) {
    if (!urls || !urls.length) return;
    const data = await browser.storage.local.get(SKIPPED_KEY);
    const drop = new Set(urls);
    const skipped = (data[SKIPPED_KEY] || []).filter((u) => !drop.has(u));
    await browser.storage.local.set({ [SKIPPED_KEY]: skipped });
  }

  async function filterByStorage(tasks, skipDone) {
    if (!skipDone) {
      return { tasks, alreadyDone: 0 };
    }
    const { urls, paths } = await getDoneKeys();
    const before = tasks.length;
    const filtered = tasks.filter((t) => {
      if (urls.has(t.pdfUrl)) return false;
      if (t.saveAs && paths.has(normPath(t.saveAs))) return false;
      return true;
    });
    return { tasks: filtered, alreadyDone: before - filtered.length };
  }

  async function saveJob(job) {
    await browser.storage.local.set({
      [JOB_KEY]: { ...job, updatedAt: Date.now() },
    });
  }

  async function getJob() {
    const data = await browser.storage.local.get(JOB_KEY);
    return data[JOB_KEY] || null;
  }

  async function clearJob() {
    await browser.storage.local.remove(JOB_KEY);
  }

  function isResumable(job) {
    return (
      job &&
      [
        "paused",
        "cancelled",
        "error",
        "needs_reload",
        "needs_new_identity",
        "recovering",
      ].includes(job.status)
    );
  }

  function jobMatchesPage(job, url, ctx) {
    if (!isResumable(job) || !ctx) return false;
    if (job.db && ctx.db && job.db === ctx.db) {
      if (job.allYears) return true;
      if (job.year && ctx.year) return job.year === ctx.year;
      if (!ctx.year) return true;
    }
    const href = String(url || "");
    if (job.juris && job.db) {
      return new RegExp(`/${job.juris}/${job.db}/`, "i").test(href);
    }
    return false;
  }

  return {
    DONE_KEY,
    SKIPPED_KEY,
    JOB_KEY,
    DONE_PATHS_KEY,
    getDoneUrls,
    getDoneKeys,
    markDone,
    markSkipped,
    unmarkSkipped,
    unmarkSkippedMany,
    filterByStorage,
    saveJob,
    getJob,
    clearJob,
    isResumable,
    jobMatchesPage,
  };
})();
