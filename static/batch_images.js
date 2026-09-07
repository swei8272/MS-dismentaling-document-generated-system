(function (scope, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else scope.DgmBatchImages = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // Serialize reads, coalesce polling, and discard a response for an old page.
  function createLoader({ page = 1, request, render, onError }) {
    let desiredPage = page;
    let version = 0;
    let pending = null;
    let queued = false;
    function refresh(nextPage, { force = false } = {}) {
      if (nextPage !== undefined && nextPage !== desiredPage) {
        desiredPage = Math.max(Number(nextPage) || 1, 1);
        version += 1;
      }
      if (pending) {
        if (nextPage !== undefined || force) queued = true;
        // A forced read confirms a state-changing action. Do not briefly
        // render the same-page response that started before that action.
        if (force) version += 1;
        return pending;
      }
      // Defer the first request so pending is set even for a synchronous mock.
      pending = Promise.resolve().then(async () => {
        do {
          queued = false;
          const requestedVersion = version;
          try {
            const payload = await request(desiredPage);
            if (requestedVersion === version) {
              render(payload);
              desiredPage = payload.page;
            }
          } catch (_error) {
            if (requestedVersion === version) onError();
          }
        } while (queued);
      }).finally(() => { pending = null; });
      return pending;
    }
    return { refresh };
  }

  function mount() {
    const root = document.getElementById("batch-images");
    if (!root) return { refresh: async () => {} };
    const content = document.getElementById("batch-images-content");
    const error = document.getElementById("batch-images-error");
    const loader = createLoader({
      page: Number(root.dataset.page) || 1,
      request: async (page) => {
        const controller = new AbortController();
        const timer = window.setTimeout(() => controller.abort(), 15000);
        try {
          const response = await fetch(root.dataset.imagesUrl + "?view=table&page=" + page, {
            cache: "no-store",
            headers: { Accept: "application/json" }, signal: controller.signal,
          });
          if (!response.ok) throw new Error("图片列表暂时不可用");
          const payload = await response.json();
          if (!Number.isSafeInteger(payload.page) || typeof payload.html !== "string") {
            throw new Error("图片列表响应无效");
          }
          return payload;
        } finally {
          window.clearTimeout(timer);
        }
      },
      render: (payload) => {
        // HTML comes from the same autoescaped Jinja partial as the initial page.
        // Replace only this panel; selected File objects and upload controls live outside it.
        if (content.innerHTML !== payload.html) content.innerHTML = payload.html;
        root.dataset.page = String(payload.page);
        error.hidden = true;
      },
      onError: () => { error.hidden = false; },
    });
    content.addEventListener("click", (event) => {
      const link = event.target.closest("a");
      if (!link || !content.contains(link) || event.button !== 0 ||
          event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      const target = new URL(link.href);
      event.preventDefault();
      loader.refresh(Number(target.searchParams.get("page")) || 1);
    });
    document.getElementById("refresh-batch-images").addEventListener("click", () => {
      loader.refresh(undefined, { force: true });
    });
    return loader;
  }
  return { createLoader, mount };
});
