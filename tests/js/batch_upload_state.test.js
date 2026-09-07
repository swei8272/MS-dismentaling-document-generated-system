"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const path = require("node:path");

const state = require(path.resolve(
  __dirname,
  "..",
  "..",
  "static",
  "batch_upload_state.js"
));

test("groups are bounded by file count and byte total", () => {
  const twentySix = Array.from({ length: 26 }, (_unused, index) => ({
    id: String(index),
    size: 1024,
  }));
  assert.deepEqual(
    state.makeGroups(twentySix, 25, 64 * 1024 * 1024).map((group) => group.length),
    [25, 1]
  );
  const byteBounded = [
    { id: "a", size: 40 },
    { id: "b", size: 30 },
    { id: "c", size: 20 },
  ];
  assert.deepEqual(
    state.makeGroups(byteBounded, 25, 64).map((group) =>
      group.reduce((total, item) => total + item.size, 0)
    ),
    [40, 50]
  );
});

test("refresh normalization keeps only metadata and never a File-like value", () => {
  const normalized = state.normalizeOutbox([
    {
      clientId: "offline-one",
      name: "断网图片.png",
      size: 123,
      kind: "transport_unknown",
      createdAt: "2026-09-07T00:00:00Z",
      file: { bytes: "must not survive" },
      dataUrl: "must not survive",
      attempts: 99,
    },
  ]);
  assert.deepEqual(normalized, [
    {
      clientId: "offline-one",
      name: "断网图片.png",
      size: 123,
      kind: "transport_unknown",
      createdAt: "2026-09-07T00:00:00Z",
    },
  ]);
  const serialized = state.serializeOutbox(normalized);
  assert.equal(serialized.includes("must not survive"), false);
  assert.equal(Object.hasOwn(JSON.parse(serialized)[0], "file"), false);
});

test("repeated offline enqueue and repeated acknowledgements are idempotent", () => {
  const first = {
    clientId: "same-client",
    name: "第一次.png",
    size: 100,
    kind: "transport_unknown",
    createdAt: "2026-09-07T00:00:00Z",
  };
  const latest = {
    ...first,
    name: "重新编码.png",
    size: 90,
  };
  const outbox = state.upsertOutbox([first], [latest, latest]);
  assert.equal(outbox.length, 1);
  assert.equal(outbox[0].name, "重新编码.png");
  assert.equal(outbox[0].size, 90);

  const partition = state.partitionSyncResults(outbox, [
    {
      client_id: "same-client",
      failure_id: 17,
      sync_status: "saved",
      reason: "saved",
    },
    {
      client_id: "same-client",
      failure_id: 17,
      sync_status: "saved",
      reason: "saved twice",
    },
  ]);
  assert.deepEqual(partition.remaining, []);
  assert.equal(partition.acknowledgements.length, 1);
  assert.equal(partition.acknowledgements[0].failureId, 17);
});

test("already-confirmed acknowledgement clears a late pending failure", () => {
  const pending = [
    {
      clientId: "success-raced-sync",
      name: "恢复上传.png",
      size: 88,
      kind: "transport_unknown",
      createdAt: "2026-09-07T00:00:00Z",
    },
  ];
  const partition = state.partitionSyncResults(pending, [
    {
      client_id: "success-raced-sync",
      failure_id: null,
      sync_status: "already_confirmed",
      reason: "already confirmed",
    },
  ]);
  assert.deepEqual(partition.remaining, []);
  assert.equal(partition.acknowledgements[0].syncStatus, "already_confirmed");
  assert.equal(partition.acknowledgements[0].failureId, null);
});

test("failure metadata sync has at most two automatic retries", async () => {
  let attempts = 0;
  const waits = [];
  await assert.rejects(
    state.retryWithBackoff(
      async () => {
        attempts += 1;
        const error = new Error("offline");
        error.retryable = true;
        throw error;
      },
      {
        maxRetries: 2,
        baseDelayMs: 10,
        wait: async (milliseconds) => waits.push(milliseconds),
      }
    ),
    /offline/
  );
  assert.equal(attempts, 3);
  assert.deepEqual(waits, [10, 20]);
});

test("bounded retry succeeds when connectivity returns", async () => {
  let attempts = 0;
  const result = await state.retryWithBackoff(
    async () => {
      attempts += 1;
      if (attempts < 3) {
        const error = new Error("temporary network error");
        error.retryable = true;
        throw error;
      }
      return "saved";
    },
    {
      maxRetries: 2,
      baseDelayMs: 1,
      wait: async () => {},
    }
  );
  assert.equal(result, "saved");
  assert.equal(attempts, 3);
});

test("explicit retry reuses a restored outbox row by exact client id", () => {
  const restored = {
    id: "offline-client",
    failureId: null,
    state: "failed",
  };
  const items = [restored];

  assert.equal(
    state.findRetryItem(items, { id: 42, client_id: "offline-client" }),
    restored
  );
  assert.equal(
    state.findRetryItem(
      [{ id: "offline-client", failureId: 42, state: "confirmed" }],
      { id: 42, client_id: "offline-client" }
    ),
    undefined
  );
});
