// admin/emulator.js and api/emulator.js: the suites' emulator runs and
// batches - taken with the upload application's service token alone, held to
// what each has, folded each night, and shown on the view.
import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { test } from "node:test";
import * as admin from "../admin/index.js";
import { BATCHES_PATH, DAILY_RUNS, EMULATOR_CAUSES, MAX_RUNS, refLink, refused, refusedBatch, RUNS_PATH, SCOPE }
  from "../admin/emulator.js";
import { deviceTesting, emulatorFolding, INCONCLUSIVE, newerFirst, outcomeOf } from "../api/emulator.js";
import { d1 } from "../api/local-d1.mjs";

const TEAM = "liberator";
const AUD = "a".repeat(64);
const UPLOAD_AUD = "c".repeat(64);
const NOW = Date.parse("2026-11-13T09:00:00Z");

const { privateKey, publicKey } = await crypto.subtle.generateKey(
  { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
  true, ["sign", "verify"]);
const jwk = { ...(await crypto.subtle.exportKey("jwk", publicKey)), kid: "k1", alg: "RS256" };
const keys = async () => new Response(JSON.stringify({ keys: [jwk] }));
const b64 = (value) => Buffer.from(JSON.stringify(value)).toString("base64url");

async function token(claims) {
  const head = b64({ alg: "RS256", kid: "k1" });
  const body = b64({ iss: `https://${TEAM}.cloudflareaccess.com`, exp: NOW / 1000 + 600, ...claims });
  const signature = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", privateKey, new TextEncoder().encode(`${head}.${body}`));
  return `${head}.${body}.${Buffer.from(signature).toString("base64url")}`;
}
// What Access passes on for the suites' service token, and for the maintainer.
const service = () => token({ aud: [UPLOAD_AUD], common_name: "abc123.access" });
const maintainer = () => token({ aud: [AUD], email: "owner@example.com" });

const env = () => ({ DB: d1(), ACCESS_TEAM: TEAM, ACCESS_AUD: AUD, ACCESS_UPLOAD_AUD: UPLOAD_AUD });

export function run(more = {}) {
  return {
    id: randomBytes(16).toString("hex"), finished: "2026-11-13T08:00:00Z", batch: "0123456789abcdef",
    suite: "fault", family: "espressif", device: "sonoff-s60", scenario: "sonoff>tasmota:D4:deviceoff-1",
    route: "sonoff>tasmota", step: "D4", fault: "deviceoff", at: 40, verdict: "PASS", rerun: false, tries: 2,
    seconds: 312, cloud: "stub", tool: "3.0.0", revision: "fd341cb23abc", dirty: false,
    firmware: { tasmota: "16.0.0" }, workers: 6, arg: "1", stopped_by: [], error: null, starved: false,
    image: "49b92eac78699485", ...more,
  };
}

export function batch(more = {}) {
  return {
    batch: "0123456789abcdef", started: "2026-11-13T07:00:00Z", family: "espressif", device: "sonoff-s60",
    suite: "fault", total: 4, planned: 4, cloud: "stub", tool: "3.0.0", revision: "fd341cb23abc", dirty: false,
    workers: 6, tier: "full", ...more,
  };
}

const send = async (jwt, body, init = {}) => admin.handle(new Request(`https://admin.firmwareliberator.com${init.path ?? RUNS_PATH}`, {
  method: "POST", body: typeof body === "string" ? body : JSON.stringify(body), ...init,
  headers: { "Content-Type": "application/json", ...(jwt ? { "Cf-Access-Jwt-Assertion": jwt } : {}), ...(init.headers ?? {}) },
}), init.bindings ?? env(), init.now ?? NOW, keys);
const rows = (bindings, table = "emulator_runs") => bindings.DB.db.prepare(`SELECT * FROM ${table}`).all();

test("the suites' service token sends runs, each taken once however often it is sent, with its outcome", async () => {
  const bindings = env();
  const one = run();
  for (let time = 0; time < 2; time++) {
    const response = await send(await service(), { runs: [one, run({ verdict: "FAIL-SCREEN", tries: 4 })] }, { bindings });
    assert.equal(response.status, 200);
    const answer = await response.json();
    assert.equal(answer.taken.length, 2);
    assert.deepEqual(answer.refused, []);
  }
  const kept = rows(bindings);
  assert.equal(kept.length, 3, "the same run sent again is not counted again");
  const first = kept.find((row) => row.id === one.id);
  assert.equal(first.firmware, '{"tasmota":"16.0.0"}');
  assert.deepEqual([first.dirty, first.rerun, first.outcome, first.received, first.cause], [0, 0, "pass", "2026-11-13T09:00:00Z", null]);
  assert.deepEqual(kept.filter((row) => row.id !== one.id).map((row) => row.outcome), ["fail", "fail"]);
});

test("a verdict the suite could not judge is inconclusive, any other but a pass a failure", () => {
  assert.equal(outcomeOf("PASS"), "pass");
  for (const verdict of ["MISTIMED", "NOT-FIRED", "NO-INTERNET", "HARNESS-ERROR", "NOT-READY"]) {
    assert.ok(INCONCLUSIVE.includes(verdict), verdict);
    assert.equal(outcomeOf(verdict), "inconclusive");
  }
  for (const verdict of ["FAIL", "BRICK", "FAIL-SCREEN", "LEFTOVER", "NEEDS-JOIN", "SOMETHING-NEW"]) {
    assert.equal(outcomeOf(verdict), "fail", verdict);
  }
});

test("a batch sends what it set out to run, once", async () => {
  const bindings = env();
  for (let time = 0; time < 2; time++) {
    const answer = await (await send(await service(), { batches: [batch(), batch({ family: "beken", device: "sonoff-s61" })] },
      { bindings, path: BATCHES_PATH })).json();
    assert.deepEqual(answer.taken, ["0123456789abcdef/espressif/sonoff-s60/fault", "0123456789abcdef/beken/sonoff-s61/fault"]);
  }
  assert.equal(rows(bindings, "emulator_batches").length, 2);
  for (const bad of [batch({ total: 0 }), batch({ planned: -1 }), batch({ suite: "other" }), batch({ host: "x" })]) {
    assert.notEqual(refusedBatch(bad, NOW), null, JSON.stringify(bad));
  }
  assert.equal((await send(await service(), { runs: [run()] }, { bindings, path: BATCHES_PATH })).status, 400,
    "runs are not batches");
});

test("nothing but the upload application's service token sends runs", async () => {
  const bindings = env();
  for (const [why, jwt] of [
    ["no token", null],
    ["the maintainer's sign-in to the view", await maintainer()],
    ["the upload application's, but a person's", await token({ aud: [UPLOAD_AUD], email: "owner@example.com", common_name: "x" })],
    ["the view's audience with a service token", await token({ aud: [AUD], common_name: "abc123.access" })],
  ]) {
    assert.equal((await send(jwt, { runs: [run()] }, { bindings })).status, 403, why);
  }
  const unset = { ...env(), ACCESS_UPLOAD_AUD: "" };
  assert.equal((await send(await service(), { runs: [run()] }, { bindings: unset })).status, 403,
    "with no upload application set, none");
  assert.equal(rows(bindings).length, 0);
});

test("everything under the suites' path is held to their token alone, the runs' path and any other", async () => {
  assert.deepEqual([RUNS_PATH, BATCHES_PATH], ["/v1/emulator/runs", "/v1/emulator/batches"]);
  for (const path of [SCOPE, `${SCOPE}/`, `${SCOPE}/later`]) {
    const ask = async (jwt) => (await admin.handle(new Request(`https://admin.firmwareliberator.com${path}`, {
      method: "POST", headers: { "Cf-Access-Jwt-Assertion": jwt } }), env(), NOW, keys)).status;
    assert.equal(await ask(await maintainer()), 403, `${path}: not the maintainer's sign-in`);
    assert.equal(await ask(await service()), 404, `${path}: nothing there yet`);
  }
});

test("the service token reaches nothing of the view", async () => {
  for (const [path, method] of [["/", "GET"], ["/held", "POST"], ["/settings", "POST"], ["/emulator-cause", "POST"]]) {
    const response = await admin.handle(new Request(`https://admin.firmwareliberator.com${path}`, {
      method, headers: { "Cf-Access-Jwt-Assertion": await service(), Origin: "https://admin.firmwareliberator.com" },
    }), env(), NOW, keys);
    assert.equal(response.status, 403, path);
  }
});

test("runs are taken only as the suites send them", async () => {
  const jwt = await service();
  assert.equal((await send(jwt, "not json")).status, 400);
  assert.equal((await send(jwt, { runs: [] })).status, 400);
  assert.equal((await send(jwt, { runs: Array.from({ length: MAX_RUNS + 1 }, () => run()) })).status, 400);
  assert.equal((await send(jwt, { runs: [run()], more: 1 })).status, 400);
  assert.equal((await send(jwt, { runs: [run()] }, { headers: { "Content-Type": "text/plain" } })).status, 415);
  const get = await admin.handle(new Request(`https://admin.firmwareliberator.com${RUNS_PATH}`, {
    headers: { "Cf-Access-Jwt-Assertion": jwt } }), env(), NOW, keys);
  assert.equal(get.status, 405);
});

test("no more than DAILY_RUNS are taken a day, so a leaked token can add only so many", async () => {
  const bindings = env();
  const insert = bindings.DB.db.prepare(`INSERT INTO emulator_runs (id, finished, received, batch, suite, family,
    device, scenario, verdict, outcome, rerun, cloud, tool, dirty, firmware)
    VALUES (?, '2026-11-13T08:00:00Z', ?, 'b', 'fault', 'espressif', 'sonoff-s60', 's', 'PASS', 'pass', 0, 'stub', '3.0.0', 0, '{}')`);
  bindings.DB.db.exec("BEGIN");
  for (let n = 0; n < DAILY_RUNS - 1; n++) {
    insert.run(`r${n}`, "2026-11-13T08:00:00Z");
  }
  insert.run("yesterday", "2026-11-12T23:00:00Z");
  bindings.DB.db.exec("COMMIT");
  const jwt = await service();
  const over = await send(jwt, { runs: [run(), run()] }, { bindings });
  assert.equal(over.status, 429);
  assert.equal(over.headers.get("Retry-After"), "3600");
  assert.equal((await send(jwt, { runs: [run()] }, { bindings })).status, 200, "one more is within it");
  const tomorrow = Date.parse("2026-11-14T00:30:00Z");
  const later = await token({ aud: [UPLOAD_AUD], common_name: "abc123.access", exp: tomorrow / 1000 + 600 });
  assert.equal((await send(later, { runs: [run()] }, { bindings, now: tomorrow })).status, 200, "and the next day, more");
});

test("a run not as a run is refused alone, with why; the rest are taken", async () => {
  const bindings = env();
  const bad = [
    run({ id: "x" }), run({ verdict: "pass" }), run({ suite: "other" }), run({ cloud: "elsewhere" }),
    run({ at: 101 }), run({ tries: -1 }), run({ dirty: 1 }), run({ rerun: "no" }), run({ firmware: ["16.0.0"] }),
    run({ revision: "HEAD" }), run({ host: "my-machine" }), run({ outcome: "pass" }),
    run({ finished: "2026-11-14T12:00:00Z" }), run({ finished: "2026-08-01T00:00:00Z" }),
    run({ scenario: "é" }), run({ family: "Espressif" }),
  ];
  const { step, ...noStep } = run();
  const answer = await (await send(await service(), { runs: [run(), ...bad, noStep] }, { bindings })).json();
  assert.equal(answer.taken.length, 1);
  assert.equal(answer.refused.length, bad.length + 1);
  assert.match(answer.refused.find((one) => one.id === noStep.id).why, /step/);
  assert.match(answer.refused.at(-2).why, /family/);
  assert.match(bad.map((one) => refused(one, NOW)).join("\n"), /fields it never has: host/);
  assert.match(bad.map((one) => refused(one, NOW)).join("\n"), /fields it never has: outcome/, "the view decides it");
  assert.equal(rows(bindings).length, 1);
});

test("a run of a family or device the view has never seen is taken, and the final-firmware check's", () => {
  assert.equal(refused(run({ family: "realtek", device: "sonoff-new-thing", suite: "attended", route: null, step: null,
    fault: null, at: null, tries: null }), NOW), null);
  assert.equal(refused(run({ suite: "check", scenario: "check:sonoff>tasmota", step: null, fault: null, at: null }), NOW), null);
});

const view = async (bindings, live = false) => admin.page(bindings, { email: "owner@example.com" }, new Date(NOW), keys, live);
const section = (html, id) => html.split(`id="${id}">`)[1].split("</section>")[0];

test("the view shows the runs: totals, by revision, by device with its coverage, fault by step, and what failed", async () => {
  const bindings = env();
  const scenario = "sonoff>tasmota:D4:deviceoff-1";
  const runs = [
    run({ scenario, revision: "1111111", finished: "2026-11-11T10:00:00Z", seconds: 3600 }),
    run({ scenario, revision: "2222222", finished: "2026-11-12T10:00:00Z", verdict: "FAIL", tries: 4 }),
    run({ scenario, revision: "2222222", finished: "2026-11-12T11:00:00Z", rerun: true }),
    run({ scenario: "sonoff>tasmota:D4:crash-60", revision: "2222222", finished: "2026-11-12T11:30:00Z", verdict: "MISTIMED" }),
    run({ family: "beken", device: "sonoff-s61", scenario: "sonoff>openbeken:apply:deviceoff-1@10", step: "apply",
      firmware: { openbeken: "1.18.320-2" }, verdict: "BRICK", revision: "2222222", finished: "2026-11-12T12:00:00Z" }),
    run({ suite: "attended", scenario: "stray-keys", route: null, step: null, fault: null, at: null, tries: null,
      verdict: "FAIL-SCREEN", cloud: "stub", finished: "2026-11-12T13:00:00Z", revision: "2222222" }),
  ];
  await send(await service(), { runs }, { bindings });
  await send(await service(), { batches: [batch()] }, { bindings, path: BATCHES_PATH });
  const html = await view(bindings);
  const top = section(html, "emulator");
  assert.match(top, /Runs<\/div><div class="tile-value">6<\/div><div class="tile-note">25% of first tries passed/,
    "first tries judged: 1 passed of 4 (the re-run and the inconclusive one apart)");
  assert.match(top, /Bricked<\/div><div class="tile-value">1</);
  assert.match(top, /Not marked<\/div><div class="tile-value">4</);
  assert.match(top, /Inconclusive, 7 days<\/div><div class="tile-value">17%/);
  assert.match(top, /class="swatch muted"><\/span>inconclusive/);
  const revisions = section(html, "emulator-revisions");
  assert.match(revisions, /<td>2222222<\/td><td>3\.0\.0<\/td><td class="number">5<\/td><td class="number">4<\/td><td>0% of 3<\/td><td class="number">1<\/td><td class="number">1<\/td>/,
    "a scenario that passed on the revision before and fails on this is a new failure");
  const devices = section(html, "emulator-devices");
  assert.match(devices, /<td>espressif<\/td><td>sonoff-s60<\/td><td>fault<\/td><td>3\.0\.0<\/td><td>1 of 4 \(25%\)<\/td>/,
    "one of the batch's four scenarios passed on 3.0.0");
  assert.match(devices, /<td>beken<\/td><td>sonoff-s61<\/td><td>fault<\/td>[^]*openbeken 1\.18\.320-2/);
  assert.match(section(html, "emulator-faults"), /<th>D4<\/th><td class="number heat"[^>]*>1\/3<\/td>/,
    "the inconclusive run is not judged");
  assert.match(section(html, "emulator-rig"), /<td>espressif<\/td><td>MISTIMED<\/td><td><\/td><td class="number">1<\/td>/);
  assert.match(section(html, "emulator-flaky"), /sonoff&gt;tasmota:D4:deviceoff-1/);
  const failures = section(html, "emulator-failures");
  assert.match(failures, /FAIL \(new\)/);
  assert.equal((failures.match(/<select name="cause"/g) ?? []).length, 4);
});

test("the view shows what each fault and length found, why runs stopped, and runs by emulator image", async () => {
  const bindings = env();
  const outage = (arg, more) => run({ scenario: `sonoff>esphome:K2:outage-${arg}`, step: "K2", fault: "outage", arg,
    ...more });
  const runs = [
    outage("5"),
    outage("120", { tries: 2, stopped_by: ["network-join"] }),
    outage("180", { tries: 2, stopped_by: ["network-join"], image: "2a93935ce9c93ff6" }),
    outage("180", { verdict: "FAIL", tries: 4, stopped_by: ["network-join", "device-lost"], starved: true }),
    run({ scenario: "outdated>esphome:U1:crash-1", step: "U1", fault: "crash", verdict: "ERROR",
      error: "FileNotFoundError" }),
  ];
  const answer = await send(await service(), { runs }, { bindings });
  assert.equal((await answer.json()).taken.length, runs.length);
  assert.deepEqual(JSON.parse(rows(bindings).find((row) => row.verdict === "FAIL").stopped_by),
    ["network-join", "device-lost"]);
  const html = await view(bindings);
  const lengths = section(html, "emulator-lengths");
  assert.match(lengths, /<td>outage<\/td><td>5<\/td><td class="number">1<\/td><td class="number">0<\/td><td class="number">0<\/td>/);
  assert.match(lengths, /<td>outage<\/td><td>120<\/td><td class="number">1<\/td><td class="number">0<\/td><td class="number">1<\/td>/,
    "a pass that needed a run again counts as found");
  assert.match(lengths, /<td>outage<\/td><td>180<\/td><td class="number">2<\/td><td class="number">1<\/td><td class="number">1<\/td>/);
  assert.ok(lengths.indexOf("<td>120</td>") < lengths.indexOf("<td>180</td>"), "by length as numbers");
  const stopped = section(html, "emulator-stopped");
  assert.match(stopped, /<td>K2<\/td><td>network-join<\/td><td class="number">2<\/td><td class="number">1<\/td>/);
  assert.match(stopped, /<td>K2<\/td><td>device-lost<\/td><td class="number">0<\/td><td class="number">1<\/td>/);
  const images = section(html, "emulator-images");
  assert.match(images, /<td>espressif<\/td><td>49b92eac78699485<\/td><td class="number">4<\/td>/);
  assert.match(images, /<td>espressif<\/td><td>2a93935ce9c93ff6<\/td><td class="number">1<\/td>/);
  assert.match(section(html, "emulator-rig"), /<td>ERROR<\/td><td>FileNotFoundError<\/td>/);
  assert.match(section(html, "emulator-failures"), /FAIL \(memory-starved\)/);
});

test("a batch says which list it ran, and the view shows it", async () => {
  const bindings = env();
  assert.match(refusedBatch(batch({ tier: "most" }), NOW), /tier/);
  await send(await service(), { batches: [batch({ tier: "quick", planned: 3 })] }, { bindings, path: BATCHES_PATH });
  assert.match(section(await view(bindings), "emulator-batches"),
    /<td>sonoff-s60<\/td><td>fault<\/td><td>quick<\/td><td class="number">3<\/td><td class="number">4<\/td>/);
});

test("a run's new fields are held to what they may be", () => {
  const now = NOW;
  for (const [field, value] of [["stopped_by", ["Network Join"]], ["stopped_by", Array(17).fill("other")],
    ["stopped_by", null], ["error", "Not an error: it has spaces"], ["starved", 1], ["image", "XYZ"],
    ["arg", "x".repeat(65)]]) {
    assert.match(refused(run({ [field]: value }), now), new RegExp(field), `${field}: ${JSON.stringify(value)}`);
  }
  assert.equal(refused(run(), now), null);
});

test("the nightly fold counts each day once, again when a run of it comes late, and the view reads it", async () => {
  const bindings = env();
  const jwt = await service();
  await send(jwt, { runs: [run({ finished: "2026-11-11T10:00:00Z" }), run({ finished: "2026-11-12T10:00:00Z", verdict: "FAIL" })] },
    { bindings, now: Date.parse("2026-11-12T12:00:00Z") });
  await bindings.DB.batch(emulatorFolding(bindings.DB, "2026-11-13"));
  const days = () => rows(bindings, "emulator_days").map(({ day, runs, passed, failed }) => [day, runs, passed, failed]).sort();
  assert.deepEqual(days(), [["2026-11-11", 1, 1, 0], ["2026-11-12", 1, 0, 1]]);
  // One of the 11th comes in late; one of today is not folded until its day is over.
  await send(jwt, { runs: [run({ finished: "2026-11-11T11:00:00Z" }), run({ finished: "2026-11-13T08:00:00Z" })] }, { bindings });
  await bindings.DB.batch(emulatorFolding(bindings.DB, "2026-11-14"));
  assert.deepEqual(days(), [["2026-11-11", 2, 2, 0], ["2026-11-12", 1, 0, 1], ["2026-11-13", 1, 1, 0]]);
  // The view: folded days from emulator_days, the rest from the runs, each once.
  bindings.DB.db.prepare("UPDATE emulator_runs SET verdict = 'X' WHERE finished < '2026-11-14'").run();
  assert.match(section(await view(bindings), "emulator"), /Runs<\/div><div class="tile-value">4</);
});

test("a failure's cause is marked from the view's own form with its commit or issue, a passing run never", async () => {
  const bindings = env();
  const failed = run({ verdict: "FAIL" });
  const again = run({ verdict: "FAIL" });
  const passed = run();
  await send(await service(), { runs: [failed, again, passed] }, { bindings });
  const mark = async (id, cause, ref = "", origin = "https://admin.firmwareliberator.com") => admin.handle(
    new Request("https://admin.firmwareliberator.com/emulator-cause", {
      method: "POST", body: new URLSearchParams({ id, cause, ref }),
      headers: { "Cf-Access-Jwt-Assertion": await maintainer(), Origin: origin } }), bindings, NOW, keys);
  assert.equal((await mark(failed.id, "tool", "", "https://elsewhere.example")).status, 403);
  assert.equal((await mark(failed.id, "nonsense")).status, 400);
  assert.equal((await mark(failed.id, "tool", "javascript:alert(1)")).status, 400);
  assert.equal((await mark(failed.id, "tool", "https://example.com/issues/1")).status, 400);
  assert.equal((await mark(failed.id, "tool", "#42")).status, 303);
  assert.equal((await mark(again.id, "tool", "https://github.com/adamsthws/firmware-liberator/issues/42")).status, 303);
  assert.equal((await mark(passed.id, "tool")).status, 303);
  const marked = Object.fromEntries(rows(bindings).map((row) => [row.id, [row.cause, row.cause_ref]]));
  assert.deepEqual(marked[passed.id], [null, null]);
  assert.deepEqual(marked[failed.id], ["tool", "#42"]);
  const html = await view(bindings);
  assert.match(section(html, "emulator"), /Bugs found<\/div><div class="tile-value">2</, "a link and a number count apart");
  assert.match(section(html, "emulator-failures"), /<a href="https:\/\/github\.com\/adamsthws\/firmware-liberator\/issues\/42" rel="noreferrer">#42<\/a>/);
  assert.equal((await mark(failed.id, "")).status, 303, "and cleared");
  assert.deepEqual(rows(bindings).filter((row) => row.id === failed.id).map((row) => [row.cause, row.cause_ref]), [[null, null]]);
  assert.ok(EMULATOR_CAUSES.includes("tool"));
});

test("a mark's commit or issue links to it", () => {
  assert.equal(refLink("#7"), '<a href="https://github.com/adamsthws/firmware-liberator/issues/7" rel="noreferrer">#7</a>');
  assert.equal(refLink("abcdef1234567"), '<a href="https://github.com/adamsthws/firmware-liberator/commit/abcdef1234567" rel="noreferrer">abcdef1234</a>');
  assert.equal(refLink("https://github.com/adamsthws/firmware-liberator/pull/9"),
    '<a href="https://github.com/adamsthws/firmware-liberator/pull/9" rel="noreferrer">#9</a>');
  assert.equal(refLink(null), "");
});

test("the view updates itself every minute only when asked to", async () => {
  const bindings = env();
  assert.doesNotMatch(await view(bindings), /http-equiv="refresh"/);
  const live = await view(bindings, true);
  assert.match(live, /<meta http-equiv="refresh" content="60">/);
  assert.match(live, /<a href="\/">stop updating<\/a>/);
  const response = await admin.handle(new Request("https://admin.firmwareliberator.com/?live=1", {
    headers: { "Cf-Access-Jwt-Assertion": await maintainer() } }), bindings, NOW, keys);
  assert.match(await response.text(), /http-equiv="refresh"/);
});

test("a device's figures: version by version, each on its own, clean checkouts alone where asked", async () => {
  const bindings = env();
  await send(await service(), { runs: [
    run({ tool: "2.9.0", scenario: "a", finished: "2026-11-01T10:00:00Z" }),
    run({ tool: "2.9.0", scenario: "z", finished: "2026-11-01T11:00:00Z" }),
    run({ scenario: "a", finished: "2026-11-12T10:00:00Z" }),
    run({ scenario: "b", finished: "2026-11-12T11:00:00Z", dirty: true }),
    run({ tool: "2.10.0", scenario: "a", finished: "2026-11-05T10:00:00Z", verdict: "FAIL" }),
  ] }, { bindings });
  await send(await service(), { batches: [batch({ total: 10 }), batch({ batch: "fedcba9876543210", tool: "2.9.0", total: 3 })] },
    { bindings, path: BATCHES_PATH });
  const all = async (sql, ...values) => (await bindings.DB.prepare(sql).bind(...values).all()).results;
  assert.deepEqual((await deviceTesting(all)).map((row) => [row.tool, row.covered, row.total, row.runs]),
    [["3.0.0", 2, 10, 2], ["2.10.0", 0, null, 1], ["2.9.0", 2, 3, 2]],
    "newest first, 2.10.0 after 3.0.0 and before 2.9.0; a scenario passed on 2.9.0 counts for no other");
  assert.deepEqual((await deviceTesting(all, true)).map((row) => [row.tool, row.covered]),
    [["3.0.0", 1], ["2.10.0", 0], ["2.9.0", 2]]);
  assert.deepEqual(["2.9.0", "3.0.0", "2.10.0", "3.0.1"].sort(newerFirst), ["3.0.1", "3.0.0", "2.10.0", "2.9.0"]);
});

test("the public figures give each device's testing, from checkouts with no changes alone", async () => {
  const { snapshot } = await import("../api/snapshot.js");
  const bindings = env();
  await send(await service(), { runs: [
    run({ scenario: "a", finished: "2026-11-12T10:00:00Z" }),
    run({ scenario: "b", finished: "2026-11-12T11:00:00Z" }),
    run({ scenario: "c", finished: "2026-11-12T12:00:00Z", dirty: true }),
    run({ scenario: "d", finished: "2026-11-12T13:00:00Z", verdict: "FAIL" }),
  ] }, { bindings });
  await send(await service(), { runs: [run({ tool: "2.9.0", scenario: "a", finished: "2026-11-01T10:00:00Z" })] },
    { bindings });
  await send(await service(), { batches: [batch({ total: 8 }), batch({ batch: "fedcba9876543210", tool: "2.9.0", total: 4 })] },
    { bindings, path: BATCHES_PATH });
  const { testing } = await snapshot(bindings.DB, new Date(NOW));
  const newest = { tool: "3.0.0", lastDay: "2026-11-12", firmware: { tasmota: "16.0.0" }, scenarios: 8, passed: 2,
    coverage: 25, firstTry: null };
  assert.deepEqual(testing, [{ device: "sonoff-s60", family: "espressif", suites: ["fault"], ...newest,
    versions: [newest, { tool: "2.9.0", lastDay: "2026-11-01", firmware: { tasmota: "16.0.0" }, scenarios: 4, passed: 1,
      coverage: 25, firstTry: null }] }],
  "the newest version at the top, then each; under five first tries judged, no share of them is given");
});
