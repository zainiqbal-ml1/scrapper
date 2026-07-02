/* global browser */

const CanliiLib = (() => {
  const ORIGIN = "https://www.canlii.org";

  function parseListingUrl(url) {
    const ctx = parseDbContext(url);
    if (!ctx || !ctx.year) return null;
    return { juris: ctx.juris, db: ctx.db, year: ctx.year };
  }

  /** Database page: /on/onlrb/ or year listing /on/onlrb/nav/date/2026 */
  function parseDbContext(url) {
    const s = String(url || "");
    let m = s.match(/canlii\.org\/(?:en\/)?([^/]+)\/([^/]+)\/nav\/date\/(\d{4})\/?/i);
    if (m) return { juris: m[1], db: m[2], year: m[3] };
    m = s.match(/canlii\.org\/(?:en\/)?([^/]+)\/([^/]+)\/?(?:$|[?#])/i);
    if (!m) return null;
    const db = m[2];
    if (db === "nav" || db === "doc" || db === "laws" || db === "info") return null;
    return { juris: m[1], db, year: null };
  }

  function listingUrl(juris, db, year) {
    return `${ORIGIN}/${juris}/${db}/nav/date/${year}/`;
  }

  function normalizePath(raw) {
    if (!raw) return "";
    let path = String(raw).trim();
    if (path.startsWith("http")) {
      try {
        path = new URL(path).pathname;
      } catch (e) {
        return "";
      }
    }
    if (!path.startsWith("/")) path = "/" + path;
    return path;
  }

  /**
   * CanLII doc URLs use a duplicated slug:
   *   /on/onlrb/doc/2026/2026canlii63962/2026canlii63962.html
   * Items JSON often returns the short form (single slug).
   */
  function docBasePath(raw) {
    let p = normalizePath(raw);
    if (!p) return "";
    if (p.endsWith(".html")) p = p.slice(0, -5);
    if (p.endsWith(".pdf")) p = p.slice(0, -4);

    const parts = p.split("/").filter(Boolean);
    const docIdx = parts.indexOf("doc");
    if (docIdx < 0 || docIdx + 2 >= parts.length) return p;

    const slug = parts[docIdx + 2];
    const prefix = "/" + parts.slice(0, docIdx + 2).join("/") + "/";

    if (parts[docIdx + 3] === slug) {
      return prefix + slug + "/" + slug;
    }
    if (parts.length === docIdx + 3) {
      return prefix + slug + "/" + slug;
    }
    return "/" + parts.join("/");
  }

  /** Paths to try: short (/on/...) and /en/on/... (both exist on CanLII). */
  function docPathVariants(base) {
    const paths = [];
    const b = docBasePath(base);
    if (!b) return paths;
    paths.push(b);
    if (!b.startsWith("/en/")) {
      paths.push("/en" + b);
    } else {
      paths.push(b.replace(/^\/en\/([a-z]{2,3})\//i, "/$1/"));
    }
    return [...new Set(paths)];
  }

  function absUrl(path) {
    const p = path.startsWith("/") ? path : "/" + path;
    return ORIGIN + p;
  }

  function sanitizeFilename(name, maxLen = 160) {
    let s = String(name || "document")
      .replace(/[<>:"/\\|?*\x00-\x1f]/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .replace(/\.+$/, "");
    if (!s) s = "document";
    if (s.length > maxLen) s = s.slice(0, maxLen).trim();
    return s + ".pdf";
  }

  function itemFilename(item) {
    const citation = (item.citation || "").replace(/\s*\(CanLII\)\s*/gi, "").trim();
    const style = (item.styleOfCause || "").trim();
    const base = style ? `${citation} - ${style}` : citation || "document";
    return sanitizeFilename(base);
  }

  function itemsToTasks(items) {
    const tasks = [];
    for (const it of items) {
      const base = docBasePath(it.url);
      if (!base) continue;
      const htmlPath = base + ".html";
      const pdfPath = base + ".pdf";
      tasks.push({
        pdfUrl: absUrl(pdfPath),
        htmlUrl: absUrl(htmlPath),
        pdfPath,
        htmlPath,
        pdfPaths: docPathVariants(it.url).map((b) => b + ".pdf"),
        htmlPaths: docPathVariants(it.url).map((b) => b + ".html"),
        filename: itemFilename(it),
      });
    }
    return tasks;
  }

  /** Same shape as parallel_scraper.py year JSON. */
  function itemsToRecords(items, yearFolder) {
    const folder = String(yearFolder || "canlii").replace(/\/$/, "");
    const records = [];
    for (const it of items) {
      const base = docBasePath(it.url);
      if (!base) continue;
      const style = (it.styleOfCause || "").trim();
      const citation = (it.citation || "").replace(/\s*\(CanLII\)\s*/gi, "").trim();
      const pdfUrl = absUrl(base + ".pdf");
      const htmlUrl = absUrl(base + ".html");
      const filename = itemFilename(it);
      records.push({
        title: style || citation,
        citation,
        date: it.judgmentDate || "",
        pdf_url: pdfUrl,
        html_url: htmlUrl,
        file: `${folder}/${filename}`,
      });
    }
    return records;
  }

  function jsonPathForYear(jsonBase, year) {
    const base = String(jsonBase || "canlii").replace(/\/\d{4}\/?$/, "").replace(/\/$/, "");
    return `${base}/${year}.json`;
  }

  return {
    parseListingUrl,
    parseDbContext,
    listingUrl,
    itemsToTasks,
    itemsToRecords,
    jsonPathForYear,
    normalizePath,
    docBasePath,
    docPathVariants,
    absUrl,
  };
})();
