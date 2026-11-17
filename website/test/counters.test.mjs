// api/counters.js: a limit is never passed, by count or by bytes.
import assert from "node:assert/strict";
import { test } from "node:test";
import { read, release, reserve, reserveUpTo } from "../api/counters.js";
import { d1 } from "../api/local-d1.mjs";

test("counts up to its limit, and no further", async () => {
  const db = d1();
  const taken = [];
  for (let n = 0; n < 5; n += 1) {
    taken.push(await reserve(db, "reports:2026-11-09", "2026-11-09", { most: 3 }));
  }
  assert.deepEqual(taken, [true, true, true, false, false]);
  assert.deepEqual(await read(db, "reports:2026-11-09"), { count: 3, bytes: 0 });
});

test("bytes have their own limit, a first one too", async () => {
  const db = d1();
  assert.equal(await reserve(db, "b", null, { bytes: 11, mostBytes: 10 }), false, "a first one too big");
  assert.equal(await reserve(db, "b", null, { bytes: 6, mostBytes: 10 }), true);
  assert.equal(await reserve(db, "b", null, { bytes: 5, mostBytes: 10 }), false);
  assert.equal(await reserve(db, "b", null, { bytes: 4, mostBytes: 10 }), true);
  assert.deepEqual(await read(db, "b"), { count: 2, bytes: 10 });
});

test("many at once still never pass it", async () => {
  const db = d1();
  const taken = await Promise.all(Array.from({ length: 20 }, () => reserve(db, "x", null, { most: 7 })));
  assert.equal(taken.filter(Boolean).length, 7);
});

test("one given back can be taken again; nothing goes below zero", async () => {
  const db = d1();
  await reserve(db, "r", null, { bytes: 5, most: 1 });
  assert.equal(await reserve(db, "r", null, { most: 1 }), false);
  await release(db, "r", 5);
  assert.deepEqual(await read(db, "r"), { count: 0, bytes: 0 });
  await release(db, "r", 5);
  assert.deepEqual(await read(db, "r"), { count: 0, bytes: 0 });
  assert.equal(await reserve(db, "r", null, { most: 1 }), true);
});

test("each name is counted alone; one never counted reads zero", async () => {
  const db = d1();
  await reserve(db, "a", "2026-11-09");
  assert.deepEqual(await read(db, "b"), { count: 0, bytes: 0 });
  assert.equal(db.db.prepare("SELECT day FROM counters WHERE name = 'a'").get().day, "2026-11-09");
});

test("several at once are taken whole or not at all", async () => {
  const db = d1();
  assert.equal(await reserve(db, "n", null, { by: 3, most: 5 }), true);
  assert.equal(await reserve(db, "n", null, { by: 3, most: 5 }), false, "not part of them");
  assert.deepEqual(await read(db, "n"), { count: 3, bytes: 0 });
  for (const by of [0, -1, 1.5, 6]) {
    assert.equal(await reserve(db, "m", null, { by, most: 5 }), false, String(by));
  }
});

test("as many as still fit are taken, and no more", async () => {
  const db = d1();
  assert.equal(await reserveUpTo(db, "u", "2026-11-09", 4, 10), 4);
  assert.equal(await reserveUpTo(db, "u", "2026-11-09", 9, 10), 6, "the room left");
  assert.equal(await reserveUpTo(db, "u", "2026-11-09", 1, 10), 0);
  assert.deepEqual(await read(db, "u"), { count: 10, bytes: 0 });
});

test("several given back at once", async () => {
  const db = d1();
  await reserve(db, "g", null, { by: 5 });
  await release(db, "g", 0, 3);
  assert.deepEqual(await read(db, "g"), { count: 2, bytes: 0 });
});
