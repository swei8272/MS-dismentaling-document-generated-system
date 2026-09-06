(() => {
  "use strict";

  const root = document.getElementById("batch-uploader");
  if (!root) return;

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

  const settings = {
    uploadUrl: root.dataset.uploadUrl,
    statusUrl: root.dataset.statusUrl,
    failureUrl: root.dataset.failureUrl,
    groupMaxFiles: Number(root.dataset.groupMaxFiles),
    groupMaxBytes: Number(root.dataset.groupMaxBytes),
    fileMaxBytes: Number(root.dataset.fileMaxBytes),
    maxAutoRetries: Number(root.dataset.maxAutoRetries),
    pollMs: Number(root.dataset.pollMs),
  };
  const pageSize = 50;
  const items = [];
  let localPage = 1;
  let running = false;
  let pollInFlight = false;
  let knownFailures = Array.from(failureList.querySelectorAll(".failure-row")).map((row) => ({
    id: Number(row.dataset.failureId),
    clientId: row.dataset.clientId,
    name: row.querySelector("strong").textContent,
    size: row.dataset.size === "" ? null : Number(row.dataset.size),
    claimed: false,
  }));

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
    return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function formatBytes(bytes) {
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
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
      };
      const priorFailure = knownFailures.find(
        (failure) =>
          !failure.claimed && failure.name === file.name &&
          (failure.size === null || failure.size === file.size)
      );
      if (priorFailure) {
        priorFailure.claimed = true;
        item.id = priorFailure.clientId || item.id;
        item.failureId = priorFailure.id;
        item.detail = "已关联刷新前的失败记录，上传成功后将自动解决。";
      }
      if (file.size > settings.fileMaxBytes) {
        item.state = "failed";
        item.retryable = false;
        item.detail = `单个文件不能超过 ${formatBytes(settings.fileMaxBytes)}`;
        rejected.push(item);
      }
      items.push(item);
    }
    localPage = Math.max(Math.ceil(items.length / pageSize), 1);
    dashboard.hidden = items.length === 0;
    render();
    if (rejected.length) persistClientFailures(rejected, "client_rejected");
  }

  async function persistClientFailures(rejected, kind) {
    try {
      const response = await fetch(settings.failureUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          items: rejected.map((item) => ({
            client_id: item.id,
            name: item.name,
            size_bytes: item.size,
            kind,
          })),
        }),
      });
      if (!response.ok) return;
      const payload = await response.json();
      const byId = new Map(payload.results.map((result) => [result.client_id, result]));
      for (const item of rejected) {
        const result = byId.get(item.id);
        if (result) {
          item.failureId = result.failure_id;
          if (!knownFailures.some((failure) => failure.id === result.failure_id)) {
            knownFailures.push({
              id: result.failure_id,
              clientId: item.id,
              name: item.name,
              size: item.size,
              claimed: true,
            });
          }
        }
      }
      render();
      pollStatus();
    } catch (_error) {
      // The local error remains visible; a later upload/status poll can recover.
    }
  }

  function makeGroups(selected) {
    const groups = [];
    let group = [];
    let bytes = 0;
    for (const item of selected) {
      const wouldOverflow =
        group.length >= settings.groupMaxFiles || bytes + item.size > settings.groupMaxBytes;
      if (group.length && wouldOverflow) {
        groups.push(group);
        group = [];
        bytes = 0;
      }
      group.push(item);
      bytes += item.size;
    }
    if (group.length) groups.push(group);
    return groups;
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
        const percent = Math.min(Math.round((event.loaded / event.total) * 100), 100);
        progress.value = percent;
        for (const item of group) item.detail = `已传输 ${percent}%`;
        if (percent === 100) {
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
          retryable: xhr.status === 0 || xhr.status === 408 || xhr.status === 429 || xhr.status >= 500,
          message: payload && payload.error ? payload.error : `服务器返回 HTTP ${xhr.status || "错误"}`,
        });
      });
      xhr.addEventListener("error", () => reject({ retryable: true, message: "网络连接中断，结果尚未确认" }));
      xhr.addEventListener("timeout", () => reject({ retryable: true, message: "上传超时，结果尚未确认" }));
      xhr.send(data);
    });
  }

  function applyResponse(group, payload) {
    const resultById = new Map((payload.results || []).map((result) => [result.client_id, result]));
    const retryable = [];
    for (const item of group) {
      const result = resultById.get(item.id);
      if (!result) {
        item.state = "failed";
        item.retryable = true;
        item.detail = "服务器未返回该文件的确认结果";
        retryable.push(item);
      } else if (["added", "reused", "already_in_batch"].includes(result.status)) {
        const resolvedFailureId = item.failureId;
        item.state = "confirmed";
        item.retryable = false;
        item.failureId = null;
        item.detail = result.message;
        if (resolvedFailureId) {
          knownFailures = knownFailures.filter((failure) => failure.id !== resolvedFailureId);
        }
      } else {
        item.state = "failed";
        item.retryable = Boolean(result.retryable);
        item.failureId = result.failure_id || item.failureId;
        item.detail = result.message || "保存失败";
        if (item.retryable) retryable.push(item);
      }
    }
    render();
    return retryable;
  }

  function delay(milliseconds) {
    return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
  }

  async function uploadGroup(group, groupNumber, groupTotal) {
    let pending = group;
    for (let attempt = 0; attempt <= settings.maxAutoRetries && pending.length; attempt += 1) {
      if (attempt > 0) {
        for (const item of pending) {
          item.state = "retrying";
          item.detail = `第 ${attempt} 次自动重试`;
        }
        message.textContent = `第 ${groupNumber}/${groupTotal} 组暂时失败，退避后重试……`;
        render();
        await delay(500 * 2 ** (attempt - 1));
      } else {
        message.textContent = `正在上传第 ${groupNumber}/${groupTotal} 组（${pending.length} 张）`;
      }
      try {
        const payload = await requestGroup(pending);
        pending = applyResponse(pending, payload);
      } catch (error) {
        for (const item of pending) {
          item.state = "failed";
          item.retryable = Boolean(error.retryable);
          item.detail = error.message || "上传失败，结果尚未确认";
        }
        render();
        if (!error.retryable) {
          await persistClientFailures(pending, "client_rejected");
          return;
        }
      }
    }
    const unknown = pending.filter((item) => !item.failureId);
    if (unknown.length) await persistClientFailures(unknown, "transport_unknown");
    progress.value = 0;
  }

  async function runQueue(selected) {
    if (running || selected.length === 0) return;
    running = true;
    startButton.disabled = true;
    retryButton.disabled = true;
    const groups = makeGroups(selected);
    try {
      for (let index = 0; index < groups.length; index += 1) {
        await uploadGroup(groups[index], index + 1, groups.length);
      }
      message.textContent = "本轮上传已结束；“服务端已确认”仅表示图片已保存，不表示 OCR 已完成。";
      await pollStatus();
    } finally {
      running = false;
      startButton.disabled = false;
      render();
    }
  }

  function updateCounters() {
    const counts = { total: items.length, confirmed: 0, uploading: 0, waiting: 0, failed: 0, retrying: 0 };
    for (const item of items) {
      if (item.state === "confirmed") counts.confirmed += 1;
      if (item.state === "uploading" || item.state === "confirming") counts.uploading += 1;
      if (item.state === "waiting") counts.waiting += 1;
      if (item.state === "failed") counts.failed += 1;
      if (item.state === "retrying") counts.retrying += 1;
    }
    for (const [key, value] of Object.entries(counts)) {
      const target = dashboard.querySelector(`[data-upload-count="${key}"]`);
      if (target) target.textContent = String(value);
    }
    retryButton.disabled = running || !items.some((item) => item.state === "failed" && item.retryable && item.file);
  }

  function renderList() {
    list.replaceChildren();
    const pages = Math.max(Math.ceil(items.length / pageSize), 1);
    localPage = Math.min(Math.max(localPage, 1), pages);
    const start = (localPage - 1) * pageSize;
    for (const item of items.slice(start, start + pageSize)) {
      const row = document.createElement("div");
      row.className = `local-file-row ${item.state}`;
      const name = document.createElement("strong");
      name.textContent = item.name;
      const size = document.createElement("span");
      size.textContent = formatBytes(item.size);
      const state = document.createElement("span");
      state.textContent = labels[item.state] || item.state;
      const detail = document.createElement("span");
      detail.textContent = item.detail;
      row.append(name, size, state, detail);
      list.append(row);
    }
    localPagination.replaceChildren();
    if (pages <= 1) return;
    const previous = document.createElement("button");
    previous.type = "button";
    previous.className = "button secondary";
    previous.textContent = "上一段";
    previous.disabled = localPage === 1;
    previous.addEventListener("click", () => { localPage -= 1; render(); });
    const label = document.createElement("span");
    label.textContent = `第 ${localPage} / ${pages} 段`;
    const next = document.createElement("button");
    next.type = "button";
    next.className = "button secondary";
    next.textContent = "下一段";
    next.disabled = localPage === pages;
    next.addEventListener("click", () => { localPage += 1; render(); });
    localPagination.append(previous, label, next);
  }

  function render() {
    updateCounters();
    renderList();
  }

  async function pollStatus() {
    if (pollInFlight || document.hidden) return;
    pollInFlight = true;
    try {
      const response = await fetch(settings.statusUrl, { headers: { Accept: "application/json" } });
      if (!response.ok) return;
      const payload = await response.json();
      for (const key of ["total", "queued", "processing", "completed", "pending", "failed"]) {
        const target = document.querySelector(`[data-stat="${key}"]`);
        if (target) target.textContent = String(payload.batch[key] || 0);
      }
      failureCount.textContent = String(payload.upload_failure_count || 0);
      failurePanel.hidden = !payload.upload_failure_count;
      failureList.replaceChildren();
      knownFailures = (payload.upload_failures || []).map((failure) => ({
        id: Number(failure.id),
        clientId: failure.client_id,
        name: failure.original_name,
        size: failure.size_bytes === null ? null : Number(failure.size_bytes),
        claimed: items.some((item) => item.failureId === Number(failure.id)),
      }));
      for (const failure of payload.upload_failures || []) {
        const row = document.createElement("div");
        row.className = "failure-row";
        row.dataset.failureId = failure.id;
        row.dataset.clientId = failure.client_id;
        row.dataset.size = failure.size_bytes === null ? "" : failure.size_bytes;
        const name = document.createElement("strong");
        name.textContent = failure.original_name;
        const reason = document.createElement("span");
        reason.textContent = failure.reason;
        row.append(name, reason);
        failureList.append(row);
      }
    } catch (_error) {
      // Polling is advisory and never starts overlapping requests.
    } finally {
      pollInFlight = false;
    }
  }

  input.addEventListener("change", () => addSelection(Array.from(input.files || [])));
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    runQueue(items.filter((item) => item.state === "waiting"));
  });
  retryButton.addEventListener("click", () => {
    const retryable = items.filter((item) => item.state === "failed" && item.retryable && item.file);
    for (const item of retryable) {
      item.state = "waiting";
      item.detail = "等待手动重试";
    }
    runQueue(retryable);
  });
  window.setInterval(pollStatus, settings.pollMs);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pollStatus(); });
})();
