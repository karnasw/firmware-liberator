// api/pending.js: what the API keeps only for a while, and the nightly job's
// clearing of it.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { read, reserve } from "../api/counters.js";
import { d1 } from "../api/local-d1.mjs";
import { PENDING, PENDING_DAYS, READOUTS_EVER, SIGNUP_DAYS, prunePending } from "../api/pending.js";

function bucket() {
  const objects = new Map();
  return { objects, delete: async (key) => { objects.delete(key); } };
}

async function kept(env, id, review, received, bytes = 100, marks = []) {
  await env.DB.prepare(`INSERT INTO readouts (id, key, stage, sender, fingerprint, bytes, object, received, review)
    VALUES (?1, 'NEWTHING|SN-NEW-01|1.0.0', 'whole', ?1, 'f', ?2, ?3, ?4, ?5)`)
    .bind(id, bytes, `readouts/x/${id}.tar`, received, review).run();
  env.READOUTS.objects.set(`readouts/x/${id}.tar`, {});
  for (const sha of marks) {
    env.READOUTS.objects.set(`readouts/firmware/${sha}`, {});
    await env.DB.prepare("INSERT INTO readout_firmware (readout, sha) VALUES (?1, ?2)").bind(id, sha).run();
  }
  await reserve(env.DB, READOUTS_EVER, null, { bytes });
  if (review === "pending") {
    await reserve(env.DB, PENDING, null, { bytes });
  }
}

test("a readout never taken as right is deleted after its days, with its marks, its bytes given back", async () => {
  assert.equal(PENDING_DAYS, 180);
  const env = { DB: d1(), READOUTS: bucket() };
  await kept(env, "old-pending", "pending", "2026-05-01T10:00:00Z", 100, ["a".repeat(64)]);
  await kept(env, "old-rejected", "rejected", "2026-05-02T10:00:00Z", 50);
  await kept(env, "old-right", "quorum", "2026-05-01T10:00:00Z", 70);
  await kept(env, "new-pending", "pending", "2026-06-01T10:00:00Z", 30);
  const done = await prunePending(env, "2026-11-09");
  assert.equal(done.readouts, 2);
  const left = (await env.DB.prepare("SELECT id FROM readouts ORDER BY id").all()).results.map((row) => row.id);
  assert.deepEqual(left, ["new-pending", "old-right"], "one taken as right is kept for good");
  assert.deepEqual([...env.READOUTS.objects.keys()].sort(), ["readouts/x/new-pending.tar", "readouts/x/old-right.tar"]);
  assert.equal((await env.DB.prepare("SELECT count(*) AS n FROM readout_firmware").first()).n, 0);
  assert.equal((await read(env.DB, READOUTS_EVER)).bytes, 100);
  assert.equal((await read(env.DB, PENDING)).bytes, 30);
});

test("an address never confirmed is deleted after its days", async () => {
  assert.equal(SIGNUP_DAYS, 7);
  const env = { DB: d1(), READOUTS: bucket() };
  for (const [token, day] of [["a".repeat(32), "2026-11-01"], ["b".repeat(32), "2026-11-03"]]) {
    await env.DB.prepare("INSERT INTO confirmations (token, who, email, devices, readouts, day, sent) VALUES (?1, 'w', 'e', '[]', '[]', ?2, ?2 || 'T12:00:00Z')")
      .bind(token, day).run();
  }
  assert.equal((await prunePending(env, "2026-11-09")).signups, 1);
  assert.equal((await env.DB.prepare("SELECT day FROM confirmations").first()).day, "2026-11-03");
});

test("it names the store's count as index.js does", () => {
  const index = readFileSync(new URL("../api/index.js", import.meta.url), "utf8");
  assert.match(index, new RegExp(`^const READOUTS_EVER = "${READOUTS_EVER}";$`, "m"));
});

test("a readout disputing those taken as right is kept until it is settled", async () => {
  const env = { DB: d1(), READOUTS: bucket() };
  await kept(env, "old-dispute", "pending", "2026-05-01T10:00:00Z");
  await kept(env, "old-settled", "rejected", "2026-05-01T10:00:00Z");
  await env.DB.prepare("UPDATE readouts SET checked = 1").run();
  assert.equal((await prunePending(env, "2026-11-09")).readouts, 1);
  const left = (await env.DB.prepare("SELECT id FROM readouts").all()).results.map((row) => row.id);
  assert.deepEqual(left, ["old-dispute"], "one rejected goes as any other");
});
