// The sign-up page, src/pages/notify.astro, and its script, public/assets/
// notify.mjs: it asks the API's /v1/notify as the tool does, with work the
// Worker takes, and the site builds it.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import worker, * as api from "../api/index.js";
import { d1 } from "../api/local-d1.mjs";
import { proven } from "../api/proof-of-work.js";
import { EXTRA_PAGES } from "../src/site.mjs";
import { FORMAT, LIGHT_PROOF_AGE, LIGHT_PROOF_BITS, NOTIFY_URL, TRIES_AT_ONCE, signUp, solve, work }
  from "../public/assets/notify.mjs";

const source = readFileSync(new URL("../src/pages/notify.astro", import.meta.url), "utf8");
const openapi = JSON.parse(readFileSync(new URL("../api/openapi.json", import.meta.url), "utf8"));

test("the page is built, its script is the module, and it says it needs JavaScript", () => {
  assert.ok(EXTRA_PAGES.includes("notify"));
  // A file of the site's own, as its policy takes no inline script.
  assert.deepEqual(source.match(/<script\b[^>]*>/g), ['<script is:inline type="module" src="/assets/notify.mjs">']);
  assert.match(source, /<noscript>/);
});

test("it asks /v1/notify, as the API describes it, with the Worker's own format and work", () => {
  assert.equal(NOTIFY_URL, `${openapi.servers[0].url}/v1/notify`);
  assert.ok(openapi.paths["/v1/notify"].post && openapi.paths["/v1/notify"].options);
  assert.equal(LIGHT_PROOF_BITS, api.LIGHT_PROOF_BITS);
  assert.equal(LIGHT_PROOF_AGE, api.LIGHT_PROOF_AGE);
  assert.equal(FORMAT, api.FORMAT);
});

test("its work is what the Worker checks", async () => {
  const time = 1794000000;
  for (let n = 0; n < 20; n += 1) {
    const subject = createHash("sha256").update(`body ${n}`).digest("hex");
    const proof = solve(subject, 12, time);
    assert.ok(await proven(proof, subject, time, 12, 60), `${subject}: ${proof}`);
    const [, nonce] = proof.split(":");
    if (Number(nonce) > 0) {   // every nonce before it falls short, as proven() reads it
      assert.equal(await proven(`${time}:${Number(nonce) - 1}`, subject, time, 12, 60), false);
    }
  }
  assert.throws(() => solve("short", 12, time), RangeError);
});

test("solve() stops after the tries it is given", () => {
  const subject = createHash("sha256").update("body").digest("hex");
  const proof = solve(subject, 12, 1794000000);
  const nonce = Number(proof.split(":")[1]);
  assert.equal(solve(subject, 12, 1794000000, { tries: nonce }), null);
  assert.equal(solve(subject, 12, 1794000000, { from: nonce, tries: 1 }), proof);
});

test("the work lets the page draw between tries, and takes a new time before the Worker would refuse it", async () => {
  const subject = createHash("sha256").update("body").digest("hex");
  let clock = 1794000000 * 1000;
  let pauses = 0;
  // Each pause a slow phone's: the first time grows too old long before 2^20 tries.
  const proof = await work(subject, 20, { now: () => clock, pause: async () => { pauses += 1; clock += 5000; } });
  const [time] = proof.split(":").map(Number);
  assert.ok(pauses > 0);
  assert.ok(time > 1794000000, "begun again later");
  assert.ok(clock / 1000 - time <= LIGHT_PROOF_AGE / 2 + 5, "young enough for the Worker");
  assert.ok(await proven(proof, subject, clock / 1000, 20, LIGHT_PROOF_AGE));
  assert.ok(TRIES_AT_ONCE < 2 ** 20 / 10, "pauses many times in a sign-up's usual work");
});

test("the page shows that it is working", () => {
  const script = readFileSync(new URL("../public/assets/notify.mjs", import.meta.url), "utf8");
  assert.match(script, /classList\.add\("working"\)/);
  assert.match(script, /Working…/);
  assert.match(source, /\.working #said::before/);
});

function mailer() {
  const sent = [];
  return { sent, send: async (message) => { sent.push(message); return { messageId: "m" }; } };
}

// signUp() against the Worker itself, as the browser sends it: from the
// site's origin, with the length the browser adds.
function sendTo(bindings) {
  return (url, init) => worker.fetch(new Request(url, { ...init, headers: { ...init.headers,
    Origin: api.SITE_ORIGIN, "Content-Length": String(new TextEncoder().encode(init.body).length),
    "CF-Connecting-IP": "192.0.2.9" } }), bindings);
}

test("an address it sends is taken, alone, and confirmed by email as the tool's is", async () => {
  const bindings = { DB: d1(), INGEST: "on", SENDER_KEY: "a test key", EMAIL: mailer() };
  const answer = await signUp(" me@example.com ", { send: sendTo(bindings) });
  assert.deepEqual(answer, { taken: true,
    said: "Check your inbox: follow the link in the email to confirm your address." });
  const [message] = bindings.EMAIL.sent;
  assert.equal(message.to, "me@example.com");
  assert.match(message.text, /as more\ndevices are supported\.\n/, "no device named");
});

test("what goes wrong is said plainly", async () => {
  const bindings = { DB: d1(), INGEST: "on", SENDER_KEY: "a test key", EMAIL: mailer() };
  assert.deepEqual(await signUp("not an address", { send: sendTo(bindings) }),
    { taken: false, said: "That does not look like an email address." });
  assert.equal((await signUp("me@example.com", { send: sendTo({ ...bindings, EMAIL: undefined }) })).said,
    "Not taken: not taking addresses yet: try again another time.");
  assert.equal((await signUp("me@example.com", { send: async () => { throw new TypeError("offline"); } })).said,
    "That could not be sent: check your connection and try again.");
});
