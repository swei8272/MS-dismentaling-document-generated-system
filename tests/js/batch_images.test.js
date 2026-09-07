"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { createLoader } = require("../../static/batch_images.js");
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

test("late page response never overwrites the newly selected page", async () => {
  const first = deferred();
  const calls = [], rendered = [];
  const loader = createLoader({
    request: (page) => { calls.push(page); return page === 1 ? first.promise : { page }; },
    render: (payload) => rendered.push(payload.page), onError: assert.fail,
  });
  const pending = loader.refresh();
  await Promise.resolve();
  loader.refresh(2);
  first.resolve({ page: 1 });
  await pending;
  assert.deepEqual(calls, [1, 2]);
  assert.deepEqual(rendered, [2]);
});

test("polling coalesces but upload completion forces a fresh read", async () => {
  const first = deferred();
  let calls = 0;
  const rendered = [];
  const loader = createLoader({
    request: () => ++calls === 1 ? first.promise : { page: 1, total: 51 },
    render: (payload) => rendered.push(payload.total), onError: assert.fail,
  });
  const pending = loader.refresh();
  await Promise.resolve();
  loader.refresh();
  loader.refresh();
  assert.equal(calls, 1);
  loader.refresh(undefined, { force: true });
  first.resolve({ page: 1, total: 50 });
  await pending;
  assert.equal(calls, 2);
  assert.equal(rendered.at(-1), 51);
});

test("failed update preserves displayed rows and recovers on next poll", async () => {
  let calls = 0, errors = 0, displayed = "old rows";
  const loader = createLoader({
    request: async () => { if (++calls === 1) throw Error("offline"); return { page: 1, html: "new rows" }; },
    render: (payload) => { displayed = payload.html; }, onError: () => { errors++; },
  });
  await loader.refresh();
  assert.equal(errors, 1);
  assert.equal(displayed, "old rows");
  await loader.refresh();
  assert.equal(displayed, "new rows");
});

test("automatic refresh keeps current page and accepts server page clamping", async () => {
  const calls = [];
  const loader = createLoader({page: 2,
    request: async (page) => { calls.push(page); return { page: 1 }; },
    render: () => {}, onError: assert.fail,
  });
  await loader.refresh();
  await loader.refresh();
  assert.deepEqual(calls, [2, 1]);
});
