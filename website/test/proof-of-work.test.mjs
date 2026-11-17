// api/proof-of-work.js: the work the API asks, as the tool does it.
import assert from "node:assert/strict";
import { test } from "node:test";
import { bodySubject, proven, reportSubject } from "../api/proof-of-work.js";

const NOW = 1794000000;
const BITS = 12;
const AGE = 60;
const REPORT_ID = "0123456789abcdef";

// Made by the tool (tests/autoflash/test_proof_of_work.py, VECTORS).
const vectors = async () => [
  [reportSubject(REPORT_ID), "1794000000:4905"],
  [await bodySubject(new TextEncoder().encode("readout")), "1794000000:2293"],
];

test("the tool's work checks out here", async () => {
  for (const [subject, proof] of await vectors()) {
    assert.ok(await proven(proof, subject, NOW, BITS, AGE), subject);
  }
});

test("work for one subject is good for no other", async () => {
  const [[report, proof], [body]] = await vectors();
  assert.equal(await proven(proof, body, NOW, BITS, AGE), false);
  assert.equal(await proven(proof, reportSubject("f".repeat(32)), NOW, BITS, AGE), false);
  assert.doesNotMatch(report, /^[0-9a-f]{64}$/);
});

test("work too old, or claiming to be from the future, is turned away", async () => {
  const [[subject, proof]] = await vectors();
  assert.equal(await proven(proof, subject, NOW + AGE + 1, BITS, AGE), false);
  assert.equal(await proven(proof, subject, NOW - AGE - 1, BITS, AGE), false);
  assert.ok(await proven(proof, subject, NOW + AGE, BITS, AGE));
});

test("anything not shaped as work is turned away", async () => {
  const [[subject]] = await vectors();
  for (const proof of [undefined, "", "1794000000", "1794000000:198:1", "x:198", "1794000000:-1",
    "1794000000:1e3", `${"9".repeat(13)}:1`, `1794000000:${"9".repeat(21)}`]) {
    assert.equal(await proven(proof, subject, NOW, BITS, AGE), false, String(proof));
  }
});
