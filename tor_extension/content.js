/* global browser, CanliiLib */

const PROGRESS_KEY = "canliiPdfProgress";
let bridgeReady = false;
let cancelled = false;

function injectBridge() {
  if (bridgeReady || document.getElementById("canlii-ext-bridge")) {
    bridgeReady = true;
    return Promise.resolve();
  }
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.id = "canlii-ext-bridge";
    s.src = browser.runtime.getURL("page_bridge.js");
    s.onload = () => {
      bridgeReady = true;
      s.remove();
      resolve();
    };
    s.onerror = () => reject(new Error("Could not inject page bridge."));
    (document.head || document.documentElement).appendChild(s);
  });
}

function pageCall(type, payload, timeoutMs = 120000) {
  return new Promise((resolve, reject) => {
    const id = Math.random().toString(36).slice(2);
    const timer = setTimeout(() => {
      window.removeEventListener("message", onMsg);
      reject(new Error("Timed out talking to CanLII page."));
    }, timeoutMs);
    function onMsg(ev) {
      if (
        ev.source !== window ||
        !ev.data ||
        ev.data.source !== "canlii-ext-page" ||
        ev.data.id !== id
      ) {
        return;
      }
      clearTimeout(timer);
      window.removeEventListener("message", onMsg);
      resolve(ev.data);
    }
    window.addEventListener("message", onMsg);
    window.postMessage({ source: "canlii-ext-content", id, type, payload }, "*");
  });
}

function scrapeYearsFromDom(juris, db) {
  const re = new RegExp(`/${juris}/${db}/nav/date/(\\d{4})`, "i");
  const years = new Set();
  for (const a of document.querySelectorAll("a[href]")) {
    const href = a.getAttribute("href") || "";
    let path = href;
    try {
      if (href.startsWith("http")) path = new URL(href).pathname;
    } catch (e) {
      continue;
    }
    const m = path.match(re);
    if (m) years.add(m[1]);
  }
  return [...years].sort((a, b) => Number(b) - Number(a));
}

async function discoverYears(juris, db) {
  const fromDom = scrapeYearsFromDom(juris, db);
  if (fromDom.length) return fromDom;
  await injectBridge();
  const res = await pageCall("fetch-years", { juris, db });
  if (res.ok && res.years && res.years.length) return res.years;
  throw new Error("Could not find years for this database.");
}

function scrapeItemsFromDom() {
  const seen = new Set();
  const items = [];
  for (const a of document.querySelectorAll('a[href*="/doc/"]')) {
    let href = a.getAttribute("href") || "";
    if (!href.endsWith(".html")) continue;
    if (!href.startsWith("/")) {
      try {
        href = new URL(href, location.origin).pathname;
      } catch (e) {
        continue;
      }
    }
    if (seen.has(href)) continue;
    seen.add(href);
    const title = (a.textContent || "").replace(/\s+/g, " ").trim();
    items.push({
      url: href,
      citation: title,
      styleOfCause: "",
    });
  }
  return items;
}

async function fetchItems(listingUrl) {
  const parsed = CanliiLib.parseListingUrl(listingUrl);
  if (!parsed) throw new Error("Not a CanLII nav/date page.");

  await injectBridge();
  const res = await pageCall("fetch-items", parsed);

  if (res.ok && res.text) {
    try {
      const items = JSON.parse(res.text);
      if (Array.isArray(items) && items.length) {
        return { parsed, items, source: "api" };
      }
    } catch (e) {
      /* fall through */
    }
  }

  const domItems = scrapeItemsFromDom();
  if (domItems.length) {
    return { parsed, items: domItems, source: "dom" };
  }

  if (res.status === 403) {
    throw new Error(
      "Items API HTTP 403 — reload the listing page after solving captcha, then retry."
    );
  }
  if (res.error === "captcha") {
    throw new Error("Captcha on page — solve it in this Tor window first.");
  }
  throw new Error(
    res.error || `Items API HTTP ${res.status || "?"} — open the listing page first.`
  );
}

async function prepareTasks({ listingUrl, subfolder }) {
  const { parsed, items, source } = await fetchItems(listingUrl);
  const yearFolder = subfolder || `canlii/${parsed.db}/${parsed.year}`;
  const jsonBase = yearFolder.replace(/\/\d{4}\/?$/, "").replace(/\/$/, "") || `canlii/${parsed.db}`;
  const records = CanliiLib.itemsToRecords(items, yearFolder);
  const tasks = CanliiLib.itemsToTasks(items).map((t) => ({
    ...t,
    year: parsed.year,
    saveAs: `${yearFolder}/${t.filename}`,
  }));
  if (!tasks.length) throw new Error("No PDF links found.");

  return {
    tasks,
    source,
    parsed,
    totalListed: tasks.length,
    total: tasks.length,
    yearRecords: { [parsed.year]: records },
    jsonBase,
  };
}

async function prepareAllYearsTasks({ listingUrl, subfolder }) {
  const ctx = CanliiLib.parseDbContext(listingUrl || location.href);
  if (!ctx) throw new Error("Not a CanLII database page.");

  const years = await discoverYears(ctx.juris, ctx.db);
  const base = (subfolder || `canlii/${ctx.db}`).replace(/\/\d{4}\/?$/, "").replace(/\/$/, "");
  const yearRecords = {};
  let allTasks = [];

  for (const year of years) {
    const yearUrl = CanliiLib.listingUrl(ctx.juris, ctx.db, year);
    try {
      const prep = await prepareTasks({
        listingUrl: yearUrl,
        subfolder: `${base}/${year}`,
      });
      Object.assign(yearRecords, prep.yearRecords);
      allTasks = allTasks.concat(prep.tasks);
    } catch (e) {
      const msg = String(e.message || e);
      if (!/no pdf|no items|403|captcha/i.test(msg)) {
        console.warn(`[canlii-ext] ${year}:`, msg);
      }
    }
  }

  if (!allTasks.length) {
    throw new Error(`No documents found across ${years.length} year(s).`);
  }

  return {
    tasks: allTasks,
    years,
    yearRecords,
    jsonBase: base,
    source: "api",
    parsed: ctx,
    totalListed: allTasks.length,
    total: allTasks.length,
  };
}

function startDownloadMessage(extra) {
  return browser.runtime.sendMessage({
    type: "start-downloads",
    listingUrl: location.href,
    batchSize: 10,
    batchPauseMs: 3000,
    ...extra,
  });
}

function formatStatus(prog) {
  const yr = prog.year ? ` [${prog.year}]` : "";
  const batch = prog.batch ? ` batch ${prog.batch}` : "";
  const had = prog.alreadyDone ? `, ${prog.alreadyDone} already had` : "";
  const skip = prog.skipped ? `, ${prog.skipped} skipped` : "";
  return `${prog.current} / ${prog.total}${had}${skip}${yr}${batch} — ${prog.last || ""}`;
}

function pollProgress(statusEl) {
  const poll = setInterval(async () => {
    const p = await browser.runtime.sendMessage({ type: "get-progress" });
    const prog = p.progress;
    if (!prog) return;
    const btnY = document.getElementById("canlii-pdf-bulk-btn");
    const btnA = document.getElementById("canlii-pdf-all-years-btn");
    const btnR = document.getElementById("canlii-pdf-resume-btn");
    if (prog.status === "running") {
      statusEl.textContent = formatStatus(prog);
    } else if (prog.status === "recovering") {
      statusEl.textContent = prog.error || "Opening fresh Tor window…";
    } else if (prog.status === "done") {
      const had = prog.alreadyDone ? `, ${prog.alreadyDone} already had` : "";
      const skip = prog.skipped ? ` (${prog.skipped} skipped)` : "";
      statusEl.textContent = `Done — ${prog.current} saved${had}${skip}`;
      clearInterval(poll);
      if (btnY) btnY.disabled = false;
      if (btnA) btnA.disabled = false;
      if (btnR) btnR.style.display = "none";
      updateResumeButton();
    } else if (prog.status === "error") {
      statusEl.textContent = prog.error || "Error";
      clearInterval(poll);
      if (btnY) btnY.disabled = false;
      if (btnA) btnA.disabled = false;
      updateResumeButton();
    } else if (prog.status === "cancelled") {
      statusEl.textContent = "Paused — click Resume to continue";
      clearInterval(poll);
      if (btnY) btnY.disabled = false;
      if (btnA) btnA.disabled = false;
      updateResumeButton();
    } else if (prog.status === "needs_new_identity") {
      statusEl.textContent =
        prog.error ||
        "Click Tor menu → New Identity. This page will reopen and resume automatically.";
      clearInterval(poll);
      if (btnY) btnY.disabled = false;
      if (btnA) btnA.disabled = false;
      if (btnR) btnR.disabled = false;
      updateResumeButton();
    } else if (prog.status === "needs_reload") {
      statusEl.textContent =
        prog.error ||
        "Reload this page, solve captcha, then click Resume.";
      clearInterval(poll);
      if (btnY) btnY.disabled = false;
      if (btnA) btnA.disabled = false;
      if (btnR) btnR.disabled = false;
      updateResumeButton();
    } else if (prog.status === "recovering") {
      statusEl.textContent = prog.error || "Opening fresh Tor window…";
      if (btnY) btnY.disabled = true;
      if (btnA) btnA.disabled = true;
      if (btnR) btnR.disabled = true;
    }
  }, 1000);
  return poll;
}

browser.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "ping") {
    sendResponse({ ok: true });
    return true;
  }
  if (msg.type === "check-session") {
    const url = msg.listingUrl || location.href;
    const ctx = CanliiLib.parseDbContext(url);
    if (!ctx) {
      sendResponse({ ok: false, error: "Not a CanLII page." });
      return true;
    }
    injectBridge()
      .then(async () => {
        try {
          const year =
            ctx.year ||
            scrapeYearsFromDom(ctx.juris, ctx.db)[0] ||
            String(new Date().getFullYear());
          const res = await pageCall(
            "fetch-items",
            { juris: ctx.juris, db: ctx.db, year },
            90000
          );
          if (res.ok && res.text) {
            const items = JSON.parse(res.text);
            sendResponse({ ok: Array.isArray(items) && items.length > 0 });
            return;
          }
          if (res.error === "captcha" || res.status === 403) {
            sendResponse({ ok: false, captcha: true });
            return;
          }
          sendResponse({ ok: false });
        } catch (e) {
          sendResponse({ ok: false, error: String(e.message || e) });
        }
      })
      .catch((e) => sendResponse({ ok: false, error: String(e.message || e) }));
    return true;
  }
  if (msg.type === "parse-page") {
    const ctx = CanliiLib.parseDbContext(msg.url || location.href);
    if (!ctx) {
      sendResponse({ ok: false, error: "Not a CanLII database page." });
      return true;
    }
    if (msg.allYears) {
      discoverYears(ctx.juris, ctx.db)
        .then((years) => {
          sendResponse({
            ok: true,
            parsed: { juris: ctx.juris, db: ctx.db },
            years,
            count: years.length,
            yearCount: years.length,
          });
        })
        .catch((e) => sendResponse({ ok: false, error: String(e.message || e) }));
      return true;
    }
    if (ctx.year) {
      fetchItems(CanliiLib.listingUrl(ctx.juris, ctx.db, ctx.year))
        .then(({ parsed, items, source }) =>
          sendResponse({ ok: true, parsed, count: items.length, source })
        )
        .catch((e) => sendResponse({ ok: false, error: String(e.message || e) }));
    } else {
      discoverYears(ctx.juris, ctx.db)
        .then((years) =>
          sendResponse({
            ok: true,
            parsed: { juris: ctx.juris, db: ctx.db },
            years,
            count: years.length,
          })
        )
        .catch((e) => sendResponse({ ok: false, error: String(e.message || e) }));
    }
    return true;
  }
  if (msg.type === "fetch-pdf-b64") {
    injectBridge()
      .then(() =>
        pageCall(
          "fetch-pdf",
          { pdfPath: msg.pdfPath, pdfPaths: msg.pdfPaths },
          180000
        )
      )
      .then((res) => {
        if (!res.ok) {
          sendResponse({
            ok: false,
            status: res.status,
            error: res.error || `HTTP ${res.status || "?"}`,
          });
          return;
        }
        sendResponse({ ok: true, base64: res.base64 });
      })
      .catch((e) => sendResponse({ ok: false, error: String(e.message || e) }));
    return true;
  }
  if (msg.type === "prepare-tasks") {
    prepareTasks(msg)
      .then((r) => sendResponse({ ok: true, ...r }))
      .catch((e) => sendResponse({ ok: false, error: String(e.message || e) }));
    return true;
  }
  if (msg.type === "prepare-all-years") {
    prepareAllYearsTasks(msg)
      .then((r) => sendResponse({ ok: true, ...r }))
      .catch((e) => sendResponse({ ok: false, error: String(e.message || e) }));
    return true;
  }
  if (msg.type === "cancel") {
    cancelled = true;
    sendResponse({ ok: true });
    return true;
  }
  return false;
});

(function initUi() {
  const ctx = CanliiLib.parseDbContext(location.href);
  if (!ctx) return;
  if (document.getElementById("canlii-pdf-bulk-bar")) return;

  injectBridge().catch(() => {});

  const bar = document.createElement("div");
  bar.id = "canlii-pdf-bulk-bar";
  document.body.appendChild(bar);

  const btnYear = document.createElement("button");
  btnYear.id = "canlii-pdf-bulk-btn";
  btnYear.type = "button";
  btnYear.textContent = ctx.year
    ? `Download all PDFs (${ctx.year})`
    : "Download this year";
  btnYear.title = "All PDFs on the current year listing";

  const btnAll = document.createElement("button");
  btnAll.id = "canlii-pdf-all-years-btn";
  btnAll.type = "button";
  btnAll.textContent = "Download ALL years";
  btnAll.title = "Every year for this database";

  const btnResume = document.createElement("button");
  btnResume.id = "canlii-pdf-resume-btn";
  btnResume.type = "button";
  btnResume.textContent = "Resume download";
  btnResume.title = "Continue a paused or interrupted download";
  btnResume.style.display = "none";

  const actions = document.createElement("div");
  actions.id = "canlii-pdf-bulk-actions";

  const status = document.createElement("div");
  status.id = "canlii-pdf-bulk-status";

  actions.appendChild(btnYear);
  actions.appendChild(btnAll);
  actions.appendChild(btnResume);
  bar.appendChild(actions);
  bar.appendChild(status);

  async function updateResumeButton() {
    const res = await browser.runtime.sendMessage({ type: "get-job" });
    const job = res && res.job;
    if (job && CanliiStore.isResumable(job) && CanliiStore.jobMatchesPage(job, location.href, ctx)) {
      const left = job.pending != null ? job.pending : "?";
      const done = job.completed || 0;
      btnResume.style.display = "block";
      btnResume.textContent =
        job.status === "needs_new_identity"
          ? "Resume after New Identity"
          : job.status === "needs_reload"
            ? "Resume after reload"
            : `Resume (${done} done, ${left} left)`;
    } else {
      btnResume.style.display = "none";
    }
  }

  async function runDownload(allYears, resume) {
    btnYear.disabled = true;
    btnAll.disabled = true;
    btnResume.disabled = true;
    cancelled = false;
    status.textContent = resume
      ? "Resuming…"
      : allYears
        ? "Finding all years…"
        : "Reading document list…";

    const m = location.pathname.match(/\/([^/]+)\/([^/]+)/);
    let subfolder = allYears
      ? (m ? `canlii/${m[2]}` : "canlii")
      : (m && ctx.year ? `canlii/${m[2]}/${ctx.year}` : "canlii");

    let listingUrl = location.href;
    if (resume) {
      const jobRes = await browser.runtime.sendMessage({ type: "get-job" });
      const job = jobRes && jobRes.job;
      if (job) {
        subfolder = job.subfolder || subfolder;
        allYears = !!job.allYears;
        listingUrl = job.listingUrl || listingUrl;
      }
    }

    try {
      const res = await startDownloadMessage({
        allYears,
        subfolder,
        skipDone: true,
        resume: !!resume,
        listingUrl,
      });
      if (!res || !res.ok) {
        throw new Error((res && res.error) || "Download failed");
      }
      status.textContent = resume
        ? "Resuming download…"
        : allYears
          ? "Downloading all years…"
          : "Downloading…";
      pollProgress(status);
    } catch (e) {
      status.textContent = String(e.message || e);
      btnYear.disabled = false;
      btnAll.disabled = false;
      btnResume.disabled = false;
      updateResumeButton();
    }
  }

  btnYear.addEventListener("click", () => {
    if (!ctx.year) {
      const y = scrapeYearsFromDom(ctx.juris, ctx.db)[0];
      if (!y) {
        status.textContent = "Open a year listing first, or use ALL years.";
        return;
      }
      location.href = CanliiLib.listingUrl(ctx.juris, ctx.db, y);
      return;
    }
    runDownload(false);
  });

  btnAll.addEventListener("click", () => runDownload(true));
  btnResume.addEventListener("click", async () => {
    const jobRes = await browser.runtime.sendMessage({ type: "get-job" });
    const job = jobRes && jobRes.job;
    runDownload(job && job.allYears, true);
  });

  updateResumeButton();
  browser.runtime.sendMessage({ type: "get-progress" }).then((p) => {
    const prog = p && p.progress;
    if (!prog) return;
    if (prog.status === "running" || prog.status === "recovering") {
      btnYear.disabled = true;
      btnAll.disabled = true;
      status.textContent =
        prog.status === "recovering"
          ? prog.error || "Opening fresh Tor window…"
          : formatStatus(prog);
      pollProgress(status);
    } else if (prog.status === "needs_new_identity") {
      status.textContent =
        prog.error ||
        "Click Tor menu → New Identity. Page will reopen and resume automatically.";
      updateResumeButton();
    } else if (prog.status === "needs_reload") {
      status.textContent =
        prog.error ||
        "Reload this page, solve captcha, then click Resume.";
      updateResumeButton();
    }
  });
})();
