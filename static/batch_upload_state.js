(function (scope, factory) {
  "use strict";

  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    scope.DgmBatchUploadState = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function normalizeOutbox(raw) {
    if (!Array.isArray(raw)) return [];
    const seen = new Set();
    const normalized = [];
    for (const entry of raw) {
      if (!entry || typeof entry !== "object") continue;
      const clientId = String(entry.clientId || "").trim();
      const size = Number(entry.size);
      if (
        !clientId ||
        clientId.length > 128 ||
        seen.has(clientId) ||
        !Number.isSafeInteger(size) ||
        size < 0
      ) {
        continue;
      }
      seen.add(clientId);
      normalized.push({
        clientId,
        name: String(entry.name || "").slice(0, 255) || "未命名图片",
        size,
        kind: entry.kind === "transport_unknown" ? "transport_unknown" : "client_rejected",
        createdAt:
          typeof entry.createdAt === "string" && entry.createdAt
            ? entry.createdAt
            : new Date().toISOString(),
      });
    }
    return normalized;
  }

  function upsertOutbox(current, additions) {
    const merged = new Map(
      normalizeOutbox(current).map(function (entry) {
        return [entry.clientId, entry];
      })
    );
    for (const entry of normalizeOutbox(additions)) {
      merged.set(entry.clientId, entry);
    }
    return Array.from(merged.values());
  }

  function partitionSyncResults(current, results) {
    const acknowledgements = new Map();
    for (const result of Array.isArray(results) ? results : []) {
      if (!result || typeof result !== "object") continue;
      const clientId = String(result.client_id || "");
      if (
        clientId &&
        (result.sync_status === "saved" || result.sync_status === "already_confirmed")
      ) {
        acknowledgements.set(clientId, {
          clientId,
          failureId:
            Number.isSafeInteger(Number(result.failure_id)) && Number(result.failure_id) > 0
              ? Number(result.failure_id)
              : null,
          syncStatus: result.sync_status,
          reason: String(result.reason || ""),
        });
      }
    }
    return {
      remaining: normalizeOutbox(current).filter(function (entry) {
        return !acknowledgements.has(entry.clientId);
      }),
      acknowledgements: Array.from(acknowledgements.values()),
    };
  }

  function serializeOutbox(entries) {
    return JSON.stringify(normalizeOutbox(entries));
  }

  function makeGroups(selected, maxFiles, maxBytes) {
    const groups = [];
    let group = [];
    let bytes = 0;
    for (const item of selected) {
      const wouldOverflow =
        group.length >= maxFiles || bytes + Number(item.size || 0) > maxBytes;
      if (group.length && wouldOverflow) {
        groups.push(group);
        group = [];
        bytes = 0;
      }
      group.push(item);
      bytes += Number(item.size || 0);
    }
    if (group.length) groups.push(group);
    return groups;
  }

  function findRetryItem(items, failure) {
    if (!Array.isArray(items) || !failure) return undefined;
    const failureId = Number(failure.id);
    const clientId = String(failure.client_id || "");
    return items.find(function (candidate) {
      return (
        candidate &&
        candidate.state !== "confirmed" &&
        (Number(candidate.failureId) === failureId ||
          String(candidate.id || "") === clientId)
      );
    });
  }

  async function retryWithBackoff(operation, options) {
    const maxRetries = Number(options.maxRetries);
    const baseDelayMs = Number(options.baseDelayMs);
    const wait = options.wait;
    let lastError;
    for (let attempt = 0; attempt <= maxRetries; attempt += 1) {
      try {
        return await operation(attempt);
      } catch (error) {
        lastError = error;
        if ((error && error.retryable === false) || attempt >= maxRetries) throw error;
        await wait(baseDelayMs * Math.pow(2, attempt));
      }
    }
    throw lastError;
  }

  return Object.freeze({
    findRetryItem,
    makeGroups,
    normalizeOutbox,
    partitionSyncResults,
    retryWithBackoff,
    serializeOutbox,
    upsertOutbox,
  });
});
