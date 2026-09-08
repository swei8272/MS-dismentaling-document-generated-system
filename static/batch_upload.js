(() => {
  "use strict";

  const root = document.getElementById("batch-uploader");
  if (!root) return;

  const stateApi = window.DgmBatchUploadState;
  if (!stateApi) {
    throw new Error("批次上传状态模块未加载");
  }

  const form = document.getElementById("batch-upload-form");
  const input = document.getElementById("batch-files");
  const startButton = document.getElementById("start-upload");
  const retryButton = document.getElementById("retry-upload");
  const dashboard = document.getElementById("upload-dashboard");
  const progress = document.getElementById("upload-progress");
  const message = document.getElementById("upload-message");
  const list = document.getElementById("local-file-list");
  const localPagination = document.getElementById("local-pagination");
  const failurePanel = document.getElementById("upload-failure-panel");
  const failureCount = document.getElementById("upload-failure-count");
  const failureList = document.getElementById("upload-failure-list");
  const failurePagination = document.getElementById("upload-failure-pagination");
  const pendingSyncSummary = document.getElementById("pending-failure-sync");
  const pendingSyncCount = document.getElementById("pending-failure-count");
  const syncFailuresButton = document.getElementById("sync-pending-failures");
  const imageList = window.DgmBatchImages.mount();
  const batchStatus = document.getElementById("batch-status");
  const statusLabels = batchStatus ? JSON.parse(batchStatus.dataset.statusLabels) : {};

  const settings = {
    batchId: root.dataset.batchId,
    uploadUrl: root.dataset.uploadUrl,
    statusUrl: root.dataset.statusUrl,
    failureUrl: root.dataset.failureUrl,
    groupMaxFiles: Number(root.dataset.groupMaxFiles),
    groupMaxBytes: Number(root.dataset.groupMaxBytes),
    fileMaxBytes: Number(root.dataset.fileMaxBytes),
    maxAutoRetries: Number(root.dataset.maxAutoRetries),
    pollMs: Number(root.dataset.pollMs),
    failurePageSize: Number(root.dataset.failurePageSize),
  };
  const pageSize = 50;
  const outboxKey = "dgm:batch-upload-failures:" + settings.batchId;
  const items = [];
  let localPage = 1;
  let failurePage = 1;
  let failurePageCount = Number(root.dataset.failurePageCount) || 1;
  let failureRevision = root.dataset.failureRevision || "";
  let running = false;
  let failureFetchInFlight = false;
  let failureReloadQueued = false;
  let failurePageDirty = true;
  let desiredFailurePage = 1;
  let desiredFailureRevision = failureRevision;
  let failureSyncInFlight = false;
  let failureSyncQueued = false;
  let healthyRecoveryBudget = 0;
  let failureRecoveryTimer = null;
  let failureFetchFailureCount = 0;
  let failureNextRetryAt = 0;
  let storageAvailable = true;
  let failureOutbox = [];
  const requestTimeoutMs = 15000;

  const labels = {
    waiting: "等待上传",
    uploading: "上传中",
    confirming: "服务器确认中",
    retrying: "自动重试中",
    confirmed: "服务端已确认",
    failed: "失败/未确认",
  };

  function createId() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return String(Date.now()) + "-" + Math.random().toString(16).slice(2);
  }

  function formatBytes(bytes) {
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KiB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MiB";
  }

  function delay(milliseconds) {
    return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
  }

  async function fetchWithTimeout(url, options) {
    if (typeof window.AbortController !== "function") {
      return fetch(url, options);
    }
    const controller = new window.AbortController();
    const timeout = window.setTimeout(() => controller.abort(), requestTimeoutMs);
    try {
      return await fetch(url, { ...(options || {}), signal: controller.signal });
    } catch (error) {
      if (error && error.name === "AbortError") {
        const timeoutError = new Error("请求超时，结果尚未确认");
        timeoutError.retryable = true;
        throw timeoutError;
      }
      if (error && typeof error.retryable === "undefined") {
        error.retryable = true;
      }
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function loadFailureOutbox() {
    try {
      const saved = window.localStorage.getItem(outboxKey);
      return stateApi.normalizeOutbox(saved ? JSON.parse(saved) : []);
    } catch (_error) {
      storageAvailable = false;
      return [];
    }
  }

  function saveFailureOutbox() {
    try {
      if (failureOutbox.length) {
        window.localStorage.setItem(outboxKey, stateApi.serializeOutbox(failureOutbox));
      } else {
        window.localStorage.removeItem(outboxKey);
      }
      storageAvailable = true;
    } catch (_error) {
      storageAvailable = false;
    }
    updatePendingSyncSummary();
  }

  function updatePendingSyncSummary() {
    pendingSyncCount.textContent = String(failureOutbox.length);
    pendingSyncSummary.hidden = failureOutbox.length === 0 && storageAvailable;
    syncFailuresButton.disabled = failureSyncInFlight || failureOutbox.length === 0;
    if (!storageAvailable) {
      pendingSyncSummary.hidden = false;
      pendingSyncSummary.querySelector("[data-sync-message]").textContent =
        "浏览器无法保存待同步元数据；请保持本页打开并重新尝试。";
    } else if (failureOutbox.length) {
      pendingSyncSummary.querySelector("[data-sync-message]").textContent =
        "条失败记录仍待同步到服务端；这里只保存文件名、大小和标识，不保存图片或 File 对象。";
    }
  }

  function restoreOutboxItems() {
    for (const entry of failureOutbox) {
      if (items.some((item) => item.id === entry.clientId)) continue;
      items.push({
        id: entry.clientId,
        file: null,
        name: entry.name,
        size: entry.size,
        state: "failed",
        detail: "仅恢复了失败元数据；本地文件未保留，请重新选择文件。",
        retryable: true,
        failureId: null,
        syncState: "pending",
      });
    }
    dashboard.hidden = items.length === 0;
  }

  function removeOutboxClient(clientId) {
    const next = failureOutbox.filter((entry) => entry.clientId !== clientId);
    if (next.length !== failureOutbox.length) {
      failureOutbox = next;
      saveFailureOutbox();
    }
  }

  function addSelection(files) {
    const rejected = [];
    for (const file of files) {
      const item = {
        id: createId(),
        file,
        name: file.name,
        size: file.size,
        state: "waiting",
        detail: "等待分组",
        retryable: true,
        failureId: null,
        syncState: null,
      };
      if (file.size > settings.fileMaxBytes) {
        item.state = "failed";
        item.retryable = false;
        item.detail = "单个文件不能超过 " + formatBytes(settings.fileMaxBytes);
        rejected.push(item);
      }
      items.push(item);
    }
    localPage = Math.max(Math.ceil(items.length / pageSize), 1);
    dashboard.hidden = items.length === 0;
    render();
    if (rejected.length) queueClientFailures(rejected, "client_rejected");
  }

  function queueClientFailures(rejected, kind) {
    const additions = rejected.map((item) => ({
      clientId: item.id,
      name: item.name,
      size: item.size,
      kind,
      createdAt: new Date().toISOString(),
    }));
    failureOutbox = stateApi.upsertOutbox(failureOutbox, additions);
    healthyRecoveryBudget = 1;
    for (const item of rejected) {
      item.syncState = "pending";
      if (!item.detail.includes("待同步")) {
        item.detail += "；失败记录待同步到服务端";
      }
    }
    saveFailureOutbox();
    render();
    return flushFailureOutbox();
  }

  async function postFailureOutbox() {
    if (!failureOutbox.length) return;
    if (window.navigator.onLine === false) {
      const offlineError = new Error("网络仍未连接");
      offlineError.retryable = true;
      throw offlineError;
    }
    while (failureOutbox.length) {
      const syncBatch = failureOutbox.slice(0, 100);
      const response = await fetchWithTimeout(settings.failureUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          items: syncBatch.map((entry) => ({
            client_id: entry.clientId,
            name: entry.name,
            size_bytes: entry.size,
            kind: entry.kind,
          })),
        }),
      });
      if (!response.ok) {
        const requestError = new Error("失败记录同步返回 HTTP " + response.status);
        requestError.retryable =
          response.status === 408 ||
          response.status === 429 ||
          response.status >= 500;
        throw requestError;
      }
      const payload = await response.json();
      const partition = stateApi.partitionSyncResults(
        syncBatch,
        payload.results || []
      );
      if (partition.remaining.length) {
        const incompleteError = new Error("服务端未确认全部失败记录");
        incompleteError.retryable = true;
        throw incompleteError;
      }
      const acknowledgedIds = new Set(
        partition.acknowledgements.map((entry) => entry.clientId)
      );
      failureOutbox = failureOutbox.filter(
        (entry) => !acknowledgedIds.has(entry.clientId)
      );
      for (const acknowledgement of partition.acknowledgements) {
        const item = items.find(
          (candidate) => candidate.id === acknowledgement.clientId
        );
        if (!item) continue;
        if (acknowledgement.syncStatus === "already_confirmed") {
          item.state = "confirmed";
          item.retryable = false;
          item.failureId = null;
          item.syncState = null;
          item.detail =
            acknowledgement.reason ||
            "服务端此前已经确认，迟到的失败记录没有重新创建。";
        } else if (item.state !== "confirmed") {
          item.failureId = acknowledgement.failureId;
          item.retryable = true;
          item.syncState = "saved";
          item.detail = item.file
            ? "失败记录已保存到服务端，可手动重试。"
            : "失败记录已保存到服务端；本地文件未保留，请在失败记录中重新选择。";
        }
      }
      saveFailureOutbox();
      render();
    }
    await loadFailurePage(1);
  }

  async function flushFailureOutbox() {
    if (!failureOutbox.length) return;
    if (failureSyncInFlight) {
      failureSyncQueued = true;
      return;
    }
    failureSyncInFlight = true;
    failureSyncQueued = false;
    updatePendingSyncSummary();
    try {
      await stateApi.retryWithBackoff(postFailureOutbox, {
        maxRetries: settings.maxAutoRetries,
        baseDelayMs: 500,
        wait: delay,
      });
      if (!failureOutbox.length) {
        message.textContent = "待同步的失败记录已由服务端确认。";
      }
    } catch (error) {
      message.textContent =
        "仍有失败记录待同步；网络恢复后会有限重试，也可点击“再次同步”。" +
        (error && error.message ? "（" + error.message + "）" : "");
      if (
        failureOutbox.length &&
        healthyRecoveryBudget > 0 &&
        failureRecoveryTimer === null
      ) {
        healthyRecoveryBudget -= 1;
        failureRecoveryTimer = window.setTimeout(() => {
          failureRecoveryTimer = null;
          flushFailureOutbox();
        }, 4000);
      }
    } finally {
      failureSyncInFlight = false;
      updatePendingSyncSummary();
      render();
      if (failureSyncQueued && failureOutbox.length) {
        failureSyncQueued = false;
        window.setTimeout(flushFailureOutbox, 0);
      }
    }
  }

  function makeGroups(selected) {
    return stateApi.makeGroups(
      selected,
      settings.groupMaxFiles,
      settings.groupMaxBytes
    );
  }

  function requestGroup(group) {
    return new Promise((resolve, reject) => {
      const data = new FormData();
      for (const item of group) {
        data.append("files", item.file, item.name);
        data.append("client_ids", item.id);
        data.append("failure_ids", item.failureId || "");
        data.append("file_sizes", String(item.size));
        item.state = "uploading";
        item.detail = "正在传输";
      }
      render();

      const xhr = new XMLHttpRequest();
      xhr.open("POST", settings.uploadUrl);
      xhr.setRequestHeader("Accept", "application/json");
      xhr.setRequestHeader("X-Requested-With", "BatchUploader");
      xhr.timeout = 120000;
      xhr.upload.addEventListener("progress", (event) => {
        if (!event.lengthComputable) return;
        const transferComplete = event.loaded >= event.total;
        const percent = transferComplete
          ? 100
          : Math.min(Math.floor((event.loaded / event.total) * 100), 99);
        progress.value = percent;
        for (const item of group) item.detail = "已传输 " + percent + "%";
        if (transferComplete) {
          for (const item of group) {
            item.state = "confirming";
            item.detail = "字节已传完，服务器确认中";
          }
          message.textContent = "字节传输完成，正在等待服务器逐文件确认……";
        }
        render();
      });
      xhr.addEventListener("load", () => {
        let payload = null;
        try {
          payload = JSON.parse(xhr.responseText);
        } catch (_error) {
          // A non-JSON gateway response is treated as a request failure.
        }
        if (xhr.status >= 200 && xhr.status < 300 && payload) {
          resolve(payload);
          return;
        }
        reject({
          retryable:
            xhr.status === 0 ||
            xhr.status === 408 ||
            xhr.status === 429 ||
            xhr.status >= 500,
          message:
            payload && payload.error
              ? payload.error
              : "服务器返回 HTTP " + (xhr.status || "错误"),
        });
      });
      xhr.addEventListener("error", () =>
        reject({ retryable: true, message: "网络连接中断，结果尚未确认" })
      );
      xhr.addEventListener("timeout", () =>
        reject({ retryable: true, message: "上传超时，结果尚未确认" })
      );
      xhr.send(data);
    });
  }

  function applyResponse(group, payload) {
    const resultById = new Map(
      (payload.results || []).map((result) => [result.client_id, result])
    );
    const retryable = [];
    for (const item of group) {
      const result = resultById.get(item.id);
      if (!result) {
        item.state = "failed";
        item.retryable = true;
        item.syncState = "pending";
        item.detail = "服务器未返回该文件的确认结果";
        retryable.push(item);
      } else if (["added", "reused", "already_in_batch"].includes(result.status)) {
        item.state = "confirmed";
        item.retryable = false;
        item.failureId = null;
        item.syncState = null;
        item.detail = result.message;
        removeOutboxClient(item.id);
      } else {
        item.state = "failed";
        item.retryable = Boolean(result.retryable);
        item.failureId = result.failure_id || item.failureId;
        item.syncState = item.failureId ? "saved" : "pending";
        item.detail = result.message || "保存失败";
        if (item.retryable) retryable.push(item);
      }
    }
    render();
    return retryable;
  }

  async function uploadGroup(group, groupNumber, groupTotal) {
    let pending = group;
    for (
      let attempt = 0;
      attempt <= settings.maxAutoRetries && pending.length;
      attempt += 1
    ) {
      if (attempt > 0) {
        for (const item of pending) {
          item.state = "retrying";
          item.detail = "第 " + attempt + " 次自动重试";
        }
        message.textContent =
          "第 " + groupNumber + "/" + groupTotal + " 组暂时失败，退避后重试……";
        render();
        await delay(500 * Math.pow(2, attempt - 1));
      } else {
        message.textContent =
          "正在上传第 " +
          groupNumber +
          "/" +
          groupTotal +
          " 组（" +
          pending.length +
          " 张）";
      }
      try {
        const payload = await requestGroup(pending);
        pending = applyResponse(pending, payload);
      } catch (error) {
        for (const item of pending) {
          item.state = "failed";
          item.retryable = Boolean(error.retryable);
          item.syncState = "pending";
          item.detail = error.message || "上传失败，结果尚未确认";
        }
        render();
        if (!error.retryable) {
          await queueClientFailures(pending, "client_rejected");
          progress.value = 0;
          return;
        }
      }
    }
    const unknown = pending.filter((item) => !item.failureId);
    if (unknown.length) await queueClientFailures(unknown, "transport_unknown");
    progress.value = 0;
  }

  async function runQueue(selected) {
    if (running || selected.length === 0) return;
    running = true;
    startButton.disabled = true;
    retryButton.disabled = true;
    setFailureControlsDisabled(true);
    const groups = makeGroups(selected);
    try {
      for (let index = 0; index < groups.length; index += 1) {
        await uploadGroup(groups[index], index + 1, groups.length);
      }
      message.textContent =
        "本轮上传已结束；“服务端已确认”仅表示图片已保存，不表示 OCR 已完成。";
      await pollStatus({ force: true });
      await loadFailurePage(1);
    } finally {
      running = false;
      startButton.disabled = false;
      setFailureControlsDisabled(false);
      render();
      // An in-flight read may predate the final upload: require a follow-up read.
      await imageList.refresh(undefined, { force: true });
      if (failureReloadQueued || failurePageDirty) {
        loadFailurePage(desiredFailurePage, desiredFailureRevision);
      }
    }
  }

  function setFailureControlsDisabled(disabled) {
    for (const row of failureList.querySelectorAll(".failure-row")) {
      const picker = row.querySelector('input[type="file"]');
      const retry = row.querySelector("button");
      if (!picker || !retry) continue;
      picker.disabled = disabled;
      retry.disabled =
        disabled || !(picker.files && picker.files.length);
    }
  }

  function updateCounters() {
    const counts = {
      total: items.length,
      confirmed: 0,
      uploading: 0,
      waiting: 0,
      failed: 0,
      retrying: 0,
    };
    for (const item of items) {
      if (item.state === "confirmed") counts.confirmed += 1;
      if (item.state === "uploading" || item.state === "confirming") {
        counts.uploading += 1;
      }
      if (item.state === "waiting") counts.waiting += 1;
      if (item.state === "failed") counts.failed += 1;
      if (item.state === "retrying") counts.retrying += 1;
    }
    for (const [key, value] of Object.entries(counts)) {
      const target = dashboard.querySelector('[data-upload-count="' + key + '"]');
      if (target) target.textContent = String(value);
    }
    retryButton.disabled =
      running ||
      !items.some(
        (item) => item.state === "failed" && item.retryable && item.file
      );
  }

  function renderList() {
    list.replaceChildren();
    const pages = Math.max(Math.ceil(items.length / pageSize), 1);
    localPage = Math.min(Math.max(localPage, 1), pages);
    const start = (localPage - 1) * pageSize;
    for (const item of items.slice(start, start + pageSize)) {
      const row = document.createElement("div");
      row.className = "local-file-row " + item.state;
      const name = document.createElement("strong");
      name.textContent = item.name;
      const size = document.createElement("span");
      size.textContent = formatBytes(item.size);
      const state = document.createElement("span");
      state.textContent = labels[item.state] || item.state;
      const syncState = document.createElement("span");
      syncState.className = "failure-sync-state " + (item.syncState || "");
      if (item.syncState === "pending") {
        syncState.textContent = "失败记录待同步";
      } else if (item.syncState === "saved") {
        syncState.textContent = "失败记录已保存";
      } else {
        syncState.textContent = "—";
      }
      const detail = document.createElement("span");
      detail.textContent = item.detail;
      row.append(name, size, state, syncState, detail);
      list.append(row);
    }
    localPagination.replaceChildren();
    if (pages <= 1) return;
    const previous = document.createElement("button");
    previous.type = "button";
    previous.className = "button secondary";
    previous.textContent = "上一段";
    previous.disabled = localPage === 1;
    previous.addEventListener("click", () => {
      localPage -= 1;
      render();
    });
    const label = document.createElement("span");
    label.textContent = "第 " + localPage + " / " + pages + " 段";
    const next = document.createElement("button");
    next.type = "button";
    next.className = "button secondary";
    next.textContent = "下一段";
    next.disabled = localPage === pages;
    next.addEventListener("click", () => {
      localPage += 1;
      render();
    });
    localPagination.append(previous, label, next);
  }

  function render() {
    updateCounters();
    renderList();
    updatePendingSyncSummary();
  }

  function retryFailureWithFile(failure, file) {
    if (running) return;
    let item = stateApi.findRetryItem(items, failure);
    if (!item) {
      item = {
        id: failure.client_id,
        file,
        name: file.name,
        size: file.size,
        state: "waiting",
        detail: "已明确关联失败记录 #" + failure.id + "，等待上传替换文件。",
        retryable: true,
        failureId: Number(failure.id),
        syncState: "saved",
      };
      items.push(item);
    } else {
      item.id = failure.client_id;
      item.file = file;
      item.name = file.name;
      item.size = file.size;
      item.state = "waiting";
      item.detail = "已重新选择替换文件，等待上传。";
      item.retryable = true;
      item.failureId = Number(failure.id);
      item.syncState = "saved";
    }
    if (file.size > settings.fileMaxBytes) {
      item.state = "failed";
      item.retryable = false;
      item.detail =
        "替换文件仍超过 " + formatBytes(settings.fileMaxBytes) + "，原失败记录未解决。";
    }
    dashboard.hidden = false;
    localPage = Math.max(Math.ceil(items.length / pageSize), 1);
    render();
    if (item.state === "waiting") runQueue([item]);
  }

  function renderFailureRows(payload) {
    failureList.replaceChildren();
    failurePage = payload.page;
    failurePageCount = payload.page_count;
    failureCount.textContent = String(payload.total);
    failurePanel.hidden = payload.total === 0;
    for (const failure of payload.items) {
      const row = document.createElement("div");
      row.className = "failure-row";
      row.dataset.failureId = failure.id;
      row.dataset.clientId = failure.client_id;

      const description = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = failure.original_name;
      const metadata = document.createElement("small");
      metadata.textContent =
        "记录 #" +
        failure.id +
        (failure.size_bytes === null
          ? ""
          : " · 原大小 " + formatBytes(Number(failure.size_bytes)));
      const reason = document.createElement("span");
      reason.textContent = failure.reason;
      description.append(name, metadata, reason);

      const controls = document.createElement("div");
      controls.className = "failure-retry-controls";
      const picker = document.createElement("input");
      picker.type = "file";
      picker.accept = input.accept;
      picker.disabled = running;
      picker.setAttribute(
        "aria-label",
        "为失败记录 " + failure.id + " 选择替换文件"
      );
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "button secondary";
      retry.textContent = "上传此替换文件";
      retry.disabled = true;
      picker.addEventListener("change", () => {
        retry.disabled = running || !(picker.files && picker.files.length);
      });
      retry.addEventListener("click", () => {
        const file = picker.files && picker.files[0];
        if (file) {
          picker.value = "";
          retryFailureWithFile(failure, file);
        }
      });
      controls.append(picker, retry);
      row.append(description, controls);
      failureList.append(row);
    }
    renderFailurePagination();
  }

  function renderFailurePagination() {
    failurePagination.replaceChildren();
    if (failurePageCount <= 1) return;
    const previous = document.createElement("button");
    previous.type = "button";
    previous.className = "button secondary";
    previous.textContent = "上一页";
    previous.disabled = failurePage === 1;
    previous.addEventListener("click", () => loadFailurePage(failurePage - 1));
    const label = document.createElement("span");
    label.textContent = "第 " + failurePage + " / " + failurePageCount + " 页";
    const next = document.createElement("button");
    next.type = "button";
    next.className = "button secondary";
    next.textContent = "下一页";
    next.disabled = failurePage === failurePageCount;
    next.addEventListener("click", () => loadFailurePage(failurePage + 1));
    failurePagination.append(previous, label, next);
  }

  async function loadFailurePage(requestedPage, requestedRevision) {
    desiredFailurePage = Math.max(Number(requestedPage) || 1, 1);
    if (typeof requestedRevision === "string") {
      desiredFailureRevision = requestedRevision;
    }
    const automaticRequest = typeof requestedRevision === "string";
    if (automaticRequest && Date.now() < failureNextRetryAt) {
      failureReloadQueued = true;
      return false;
    }
    const selectedReplacement = Array.from(
      failureList.querySelectorAll('input[type="file"]')
    ).some((picker) => picker.files && picker.files.length);
    if (failureFetchInFlight || running || selectedReplacement) {
      failureReloadQueued = true;
      return false;
    }
    const pageToLoad = desiredFailurePage;
    const revisionToApply = desiredFailureRevision;
    failureReloadQueued = false;
    failureFetchInFlight = true;
    let loaded = false;
    try {
      const url =
        settings.failureUrl +
        "?page=" +
        pageToLoad +
        "&per_page=" +
        settings.failurePageSize;
      const response = await fetchWithTimeout(url, {
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        failurePageDirty = true;
        failureFetchFailureCount = Math.min(failureFetchFailureCount + 1, 6);
        failureNextRetryAt =
          Date.now() + Math.min(30000, 1000 * Math.pow(2, failureFetchFailureCount - 1));
        return false;
      }
      const payload = await response.json();
      const selectedDuringRequest = Array.from(
        failureList.querySelectorAll('input[type="file"]')
      ).some((picker) => picker.files && picker.files.length);
      if (selectedDuringRequest) {
        failurePageDirty = true;
        failureReloadQueued = true;
        return false;
      }
      renderFailureRows(payload);
      failurePageDirty = false;
      failureFetchFailureCount = 0;
      failureNextRetryAt = 0;
      failureRevision = revisionToApply;
      loaded = true;
    } catch (_error) {
      failurePageDirty = true;
      failureFetchFailureCount = Math.min(failureFetchFailureCount + 1, 6);
      failureNextRetryAt =
        Date.now() + Math.min(30000, 1000 * Math.pow(2, failureFetchFailureCount - 1));
    } finally {
      failureFetchInFlight = false;
      const reloadStillNeeded =
        failurePageDirty ||
        desiredFailurePage !== pageToLoad ||
        desiredFailureRevision !== failureRevision;
      if (failureReloadQueued && reloadStillNeeded && !running) {
        failureReloadQueued = false;
        const retryDelay = Math.max(failureNextRetryAt - Date.now(), 0);
        window.setTimeout(() => {
          loadFailurePage(desiredFailurePage, desiredFailureRevision);
        }, retryDelay);
      } else if (!reloadStillNeeded) {
        failureReloadQueued = false;
      }
    }
    return loaded;
  }

  async function readAndApplyStatus() {
    const response = await fetchWithTimeout(settings.statusUrl, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error("批次状态暂时不可用");
    const payload = await response.json();
    for (const key of [
      "total",
      "queued",
      "processing",
      "completed",
      "pending",
      "failed",
    ]) {
      const target = document.querySelector('[data-stat="' + key + '"]');
      if (target) target.textContent = String(payload.batch[key] || 0);
    }
    stateApi.applyBatchStatus(batchStatus, payload.batch.status, statusLabels);
    imageList.refresh();
    failureCount.textContent = String(payload.upload_failure_count || 0);
    failurePanel.hidden = !payload.upload_failure_count;
    if (
      payload.upload_failure_revision !== failureRevision ||
      failurePageDirty
    ) {
      await loadFailurePage(failurePage, payload.upload_failure_revision);
    }
  }

  const refreshStatus = stateApi.createCoalescedRefresh(readAndApplyStatus);

  function pollStatus({ force = false } = {}) {
    if (!force && document.hidden) return Promise.resolve(false);
    return refreshStatus({ force })
      .then(() => true)
      .catch(() => false); // Polling is advisory; later polls can recover.
  }

  input.addEventListener("change", () =>
    addSelection(Array.from(input.files || []))
  );
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    runQueue(items.filter((item) => item.state === "waiting" && item.file));
  });
  retryButton.addEventListener("click", () => {
    const retryable = items.filter(
      (item) => item.state === "failed" && item.retryable && item.file
    );
    for (const item of retryable) {
      item.state = "waiting";
      item.detail = "等待手动重试";
    }
    runQueue(retryable);
  });
  function requestFailureSync() {
    if (failureRecoveryTimer !== null) {
      window.clearTimeout(failureRecoveryTimer);
      failureRecoveryTimer = null;
    }
    healthyRecoveryBudget = 1;
    flushFailureOutbox();
  }

  syncFailuresButton.addEventListener("click", requestFailureSync);
  window.addEventListener("online", requestFailureSync);
  window.setInterval(pollStatus, settings.pollMs);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) pollStatus();
  });

  failureOutbox = loadFailureOutbox();
  healthyRecoveryBudget = failureOutbox.length ? 1 : 0;
  restoreOutboxItems();
  render();
  loadFailurePage(1);
  pollStatus();
  if (failureOutbox.length) window.setTimeout(flushFailureOutbox, 0);
  root.dataset.uploaderReady = "true";
})();
