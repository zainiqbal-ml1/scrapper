(function () {
  if (window.__canliiPdfBridge) return;
  window.__canliiPdfBridge = true;

  function isChallenge(text) {
    const low = String(text || "").slice(0, 8000).toLowerCase();
    return (
      low.includes("captcha-delivery") ||
      low.includes("proceed with our captcha") ||
      low.includes("calls upon users accessing") ||
      low.includes("please enable js")
    );
  }

  function bufToBase64(buf) {
    const bytes = new Uint8Array(buf);
    let bin = "";
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return btoa(bin);
  }

  async function fetchPdfPath(pdfPath, referer) {
    const headers = { Accept: "application/pdf,*/*" };
    if (referer) headers.Referer = referer;
    const r = await fetch(pdfPath, {
      credentials: "same-origin",
      headers,
    });
    if (!r.ok) {
      return { ok: false, status: r.status };
    }
    const buf = await r.arrayBuffer();
    const head = new Uint8Array(buf.slice(0, 4));
    if (String.fromCharCode(...head) !== "%PDF") {
      return { ok: false, error: "not-pdf" };
    }
    return { ok: true, base64: bufToBase64(buf), path: pdfPath };
  }

  window.addEventListener("message", async (ev) => {
    if (ev.source !== window || !ev.data || ev.data.source !== "canlii-ext-content") return;
    const { id, type, payload } = ev.data;

    function reply(data) {
      window.postMessage({ source: "canlii-ext-page", id, ...data }, "*");
    }

    if (type === "fetch-items") {
      const { juris, db, year } = payload;
      const path = `/${juris}/${db}/nav/date/${year}/items`;
      try {
        const r = await fetch(path, {
          credentials: "same-origin",
          headers: { Accept: "application/json, text/plain, */*" },
        });
        const text = await r.text();
        if (!r.ok) {
          reply({ ok: false, status: r.status, text: text.slice(0, 500) });
          return;
        }
        if (isChallenge(text)) {
          reply({ ok: false, error: "captcha" });
          return;
        }
        reply({ ok: true, text });
      } catch (e) {
        reply({ ok: false, error: String(e) });
      }
      return;
    }

    if (type === "fetch-years") {
      const { juris, db } = payload;
      const paths = [`/${juris}/${db}/`, `/en/${juris}/${db}/`];
      try {
        const years = new Set();
        for (const path of paths) {
          const r = await fetch(path, { credentials: "same-origin" });
          if (!r.ok) continue;
          const text = await r.text();
          const re = new RegExp(`/${juris}/${db}/nav/date/(\\d{4})`, "gi");
          for (const m of text.matchAll(re)) {
            years.add(m[1]);
          }
          if (years.size) break;
        }
        const list = [...years].sort((a, b) => Number(b) - Number(a));
        reply({ ok: true, years: list });
      } catch (e) {
        reply({ ok: false, error: String(e) });
      }
      return;
    }

    if (type === "fetch-pdf") {
      const paths = payload.pdfPaths || [payload.pdfPath];
      const referer = payload.referer || window.location.href;
      try {
        for (const pdfPath of paths) {
          if (!pdfPath) continue;
          let p = pdfPath;
          if (p.startsWith("http")) {
            try {
              p = new URL(p).pathname;
            } catch (e) {
              continue;
            }
          }
          if (!p.startsWith("/")) p = "/" + p;
          const res = await fetchPdfPath(p, referer);
          if (res.ok) {
            reply(res);
            return;
          }
        }
        reply({ ok: false, status: 404, error: "not-found" });
      } catch (e) {
        reply({ ok: false, error: String(e) });
      }
    }
  });
})();
