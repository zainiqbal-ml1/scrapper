/* global browser */

const CanliiStore = (() => {
  const DONE_KEY = "canliiPdfDone";
  const SKIPPED_KEY = "canliiPdfSkipped";
  const JOB_KEY = "canliiPdfJob";

  async function getDoneUrls() {
    const data = await browser.storage.local.get([DONE_KEY, SKIPPED_KEY]);
    return new Set([...(data[DONE_KEY] || []), ...(data[SKIPPED_KEY] || [])]);
  }

  async function markDone(pdfUrl) {
    if (!pdfUrl) return;
    const data = await browser.storage.local.get(DONE_KEY);
    const done = data[DONE_KEY] || [];
    if (done.includes(pdfUrl)) return;
    done.push(pdfUrl);
    await browser.storage.local.set({ [DONE_KEY]: done });
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
    const done = await getDoneUrls();
    const before = tasks.length;
    const filtered = tasks.filter((t) => !done.has(t.pdfUrl));
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
      ["paused", "cancelled", "error", "needs_reload"].includes(job.status)
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
    getDoneUrls,
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
