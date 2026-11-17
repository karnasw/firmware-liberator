// admin/: the maintainer's view - only through Access, changing nothing but
// a held report, and nothing a report says read as markup.
import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { verified } from "../admin/access.js";
import worker, * as admin from "../admin/index.js";
import { d1 } from "../api/local-d1.mjs";
import { writes } from "../api/reports.js";

const TEAM = "liberator";
const AUD = "a".repeat(64);
const NOW = Date.parse("2026-11-10T09:00:00Z");
const FIXTURE = JSON.parse(readFileSync(new URL("./fixtures/reports.json", import.meta.url), "utf8")).valid[0];

const { privateKey, publicKey } = await crypto.subtle.generateKey(
  { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
  true, ["sign", "verify"]);
const jwk = { ...(await crypto.subtle.exportKey("jwk", publicKey)), kid: "k1", alg: "RS256" };
const keys = async (url) => {
  assert.equal(url, `https://${TEAM}.cloudflareaccess.com/cdn-cgi/access/certs`);
  return new Response(JSON.stringify({ keys: [jwk] }));
};
const b64 = (value) => Buffer.from(typeof value === "string" ? value : JSON.stringify(value)).toString("base64url");

async function token(claims = {}, header = {}) {
  const head = b64({ alg: "RS256", kid: "k1", ...header });
  const body = b64({ aud: [AUD], iss: `https://${TEAM}.cloudflareaccess.com`, exp: NOW / 1000 + 600, email: "owner@example.com", ...claims });
  const signature = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", privateKey, new TextEncoder().encode(`${head}.${body}`));
  return `${head}.${body}.${Buffer.from(signature).toString("base64url")}`;
}

const asked = (jwt, init = {}) => new Request("https://admin.firmwareliberator.com/", {
  ...init, headers: { ...(jwt ? { "Cf-Access-Jwt-Assertion": jwt } : {}), ...(init.headers ?? {}) } });
const env = (more = {}) => ({ DB: d1(), ACCESS_TEAM: TEAM, ACCESS_AUD: AUD, ...more });

test("a token Access signed for this view, in date, lets its holder in", async () => {
  assert.equal((await verified(asked(await token()), env(), NOW, keys)).email, "owner@example.com");
});

test("any other token is refused", async () => {
  const forged = (await token()).replace(/\.[^.]+$/, `.${Buffer.from("x".repeat(256)).toString("base64url")}`);
  for (const [why, jwt, bindings] of [
    ["none", null, env()],
    ["another application's", await token({ aud: ["b".repeat(64)] }), env()],
    ["another team's", await token({ iss: "https://elsewhere.cloudflareaccess.com" }), env()],
    ["expired", await token({ exp: NOW / 1000 - 1 }), env()],
    ["not yet valid", await token({ nbf: NOW / 1000 + 3600 }), env()],
    ["not signed", (await token({}, { alg: "none" })).replace(/\.[^.]+$/, "."), env()],
    ["signed by no key of Access's", await token({}, { kid: "k9" }), env()],
    ["forged", forged, env()],
    ["not a token", "not.a.token", env()],
    ["with no audience set", await token(), env({ ACCESS_AUD: "" })],
  ]) {
    assert.equal(await verified(asked(jwt), bindings, NOW, keys), null, why);
  }
});

test("without Access's token, nothing: no page, no change", async () => {
  for (const request of [asked(null), asked(null, { method: "POST" })]) {
    const response = await worker.fetch(request, env());
    assert.equal(response.status, 403);
  }
});

// A database with a report held for review, and one counted.
async function held(text = "buy cheap watches") {
  const bindings = env();
  const db = bindings.DB;
  const ok = { ...structuredClone(FIXTURE), id: randomBytes(8).toString("hex") };
  const junk = { ...structuredClone(FIXTURE), id: randomBytes(8).toString("hex"), result: "failed",
    stopped: { step: "D4", error: "upload-cut", text } };
  await db.batch(writes(db, ok, "device-1", "2026-11-09"));
  await db.batch([...writes(db, junk, "device-2", "2026-11-09", "words that read as none of the tool's messages"),
    db.prepare("INSERT INTO devices (device_hash, first_day, last_day, first_result, runs, conversions, back_to_sonoff, dev) VALUES ('device-2', '2026-11-09', '2026-11-09', 'failed', 1, 0, 0, 0)")]);
  db.db.prepare("INSERT INTO folded (day) VALUES ('2026-11-09')").run();
  return { bindings, db, junk };
}
const rows = (db, sql) => db.db.prepare(sql).all().map((row) => ({ ...row }));

test("the view shows what the database holds, as text, never markup, and is kept out of everything", async () => {
  const { bindings } = await held('<script>alert("x")</script>');
  const html = await admin.page(bindings, { email: "owner@example.com" }, new Date(NOW), keys);
  assert.ok(!html.includes("<script>alert"), "a report's words are escaped");
  assert.ok(html.includes("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;"));
  assert.match(html, /Held for review/);
  const response = await admin.handle(asked(await token()), bindings, NOW, keys);
  assert.equal(response.status, 200);
  assert.match(await response.text(), /<h1>Admin<\/h1>/);
});

test("a held report and a recent one say the device kind as well as the model", async () => {
  const { bindings } = await held();
  const html = await admin.page(bindings, { email: "owner@example.com" }, new Date(NOW), keys);
  const section = (id) => html.split(`id="${id}">`)[1].split("</section>")[0];
  assert.match(section("held"), /<th>Device<\/th><th>Model<\/th>/);
  assert.match(section("held"), /<td>Sonoff S60<\/td><td>S60TPG<\/td>/);
  assert.match(html.split("The latest reports")[1], /<td>Sonoff S60<\/td><td>S60TPG<\/td>/);
});

test("the view lists the keys a later whole readout disagrees with, unsettled", async () => {
  const { bindings, db } = await held();
  const add = db.db.prepare(`INSERT INTO readouts (id, key, stage, sender, fingerprint, bytes, object, received, review,
    checked) VALUES (?, 'NEWTHING|SN-NEW-01|1.0.0', 'whole', ?, ?, 100, 'readouts/x.tar', ?, ?, ?)`);
  for (const n of [1, 2, 3]) {
    add.run(`r${n}`, `s${n}`, "f".repeat(64), "2026-10-01T10:00:00Z", "quorum", 0);
  }
  add.run("r4", "s4", "f".repeat(64), "2026-11-01T10:00:00Z", "quorum", 1);   // a check alike
  const disputed = async () => (await admin.page(bindings, { email: "owner@example.com" }, new Date(NOW), keys))
    .split('id="disputed">')[1].split("</section>")[0];
  assert.match(await disputed(), /<p class="none">None\.<\/p>/, "a check alike is no dispute");
  add.run("r5", "s5", "e".repeat(64), "2026-11-08T10:00:00Z", "pending", 1);
  assert.match(await disputed(), /NEWTHING\|SN-NEW-01\|1\.0\.0<\/td><td>whole<\/td><td class="number">4<\/td><td class="number">1<\/td><td>2026-11-08<\/td>/);
});

test("the view lists the offers readouts agreed on", async () => {
  const { bindings, db } = await held();
  db.db.prepare("INSERT INTO agreed_offers (key, offer, offered, day) VALUES ('NEWTHING|SN-NEW-01|1.0.0', 'x', '1.2.0', '2026-11-09')")
    .run();
  const html = await admin.page(bindings, { email: "owner@example.com" }, new Date(NOW), keys);
  assert.match(html.split('id="offers">')[1], /^[^]*?<td>NEWTHING\|SN-NEW-01\|1\.0\.0<\/td><td>1\.2\.0<\/td><td>2026-11-09<\/td>/);
});

test("the view shows each list of networks that count as none: its size, when taken, and the last try", async () => {
  const { bindings, db } = await held();
  assert.match(await admin.page(bindings, { email: "owner@example.com" }, new Date(NOW), keys), /Not fetched yet\./);
  db.db.prepare(`INSERT INTO network_lists (name, asns, taken, problem, previous)
    VALUES ('vpn', '[64500,64501]', '2026-11-02', 'HTTP 404', 1)`).run();
  const html = await admin.page(bindings, { email: "owner@example.com" }, new Date(NOW), keys);
  assert.match(html, /<td>vpn<\/td><td class="number">2<\/td><td class="number">1<\/td><td>2026-11-02<\/td><td>HTTP 404<\/td>/);
});

test("the view shows the tool's downloads, one total beside the devices converted, each night GitHub was read", async () => {
  const { bindings, db } = await held();
  const view = async () => admin.page(bindings, { email: "owner@example.com" }, new Date(NOW), keys);
  assert.match(await view(), /GitHub not read yet/);
  assert.match((await view()).split('id="downloads">')[1].split("</section>")[0], /Not enough days yet\./);
  const read = db.db.prepare("INSERT INTO download_days (day, downloads) VALUES (?, ?)");
  read.run("2026-11-08", 1210);
  read.run("2026-11-09", 1312);
  const html = await view();
  assert.ok(html.includes('<div class="tile-label">Downloads</div><div class="tile-value">1,312</div>'
    + '<div class="tile-note">to 2026-11-09</div>'));
  db.db.prepare("INSERT INTO download_misses (day, problem, rate_limited) VALUES ('2026-11-08', 'HTTP 500', 0)").run();
  assert.ok((await view()).includes('<div class="tile-note">to 2026-11-09</div>'), "a night refused before the last read is past");
  db.db.prepare("INSERT INTO download_misses (day, problem, rate_limited) VALUES ('2026-11-10', 'GitHub''s rate limit reached (HTTP 403)', 1)").run();
  assert.ok((await view()).includes('<div class="tile-note">to 2026-11-09 · refused for 2026-11-10: '
    + 'GitHub&#39;s rate limit reached (HTTP 403)</div>'));
  const chart = html.split('id="downloads">')[1].split("</section>")[0];
  assert.match(chart, /<title>2026-11-09: 1,312 downloads<\/title>/);
  assert.match(chart, /<title>2026-11-09: \d+ devices converted<\/title>/);
});

test("the view counts your users' runs and devices in all and by system, your own and held reports left out", async () => {
  const bindings = env();
  const db = bindings.DB;
  const run = async (hash, os, result = "converted", tag = "user") => db.batch(writes(db,
    { ...structuredClone(FIXTURE), id: randomBytes(8).toString("hex"), tag, result, os: { ...FIXTURE.os, family: os },
      stopped: result === "converted" ? null : { step: "D4", error: "upload-cut", text: "" } }, hash, "2026-11-09"));
  const view = async () => admin.page(bindings, { email: "owner@example.com" }, new Date(NOW), keys);
  assert.match((await view()).split('id="downloads">')[1].split("</section>")[0], /No runs yet\./);
  await run("device-1", "windows", "failed");
  await run("device-6", "macos", "cancelled");   // stopped by the person: a run, no device seen
  await run("device-1", "windows");
  await run("device-2", "macos", "failed");
  await run("device-2", "linux");   // the same device, from a second system
  await run("device-3", "linux");
  await run("device-4", "linux", "converted", "dev");
  db.db.prepare("UPDATE devices SET dev = 1 WHERE device_hash = 'device-4'").run();
  await run("device-4", "linux");   // the maintainer's device, though tagged as a user's
  await run(null, "windows", "failed");   // a run that met no device
  const held = { ...structuredClone(FIXTURE), id: randomBytes(8).toString("hex"), os: { ...FIXTURE.os, family: "macos" } };
  await db.batch(writes(db, held, "device-5", "2026-11-09"));
  db.db.prepare("UPDATE reports SET quarantined = 1 WHERE id = ?").run(held.id);
  const html = await view();
  const row = (name, runs, seen, converted) => `<tr><td>${name}</td><td class="number">${runs}</td>`
    + `<td class="number">${seen}</td><td class="number">${converted}</td></tr>`;
  const section = html.split('id="downloads">')[1].split("</section>")[0];
  assert.ok(section.includes(row("All systems", 7, 3, 3) + row("Windows", 3, 1, 1) + row("Linux", 2, 2, 2)
    + row("macOS", 2, 1, 0)), section);
  assert.ok(html.includes('<div class="tile-label">Devices seen, by system</div><div class="tile-value">3</div>'
    + '<div class="tile-note">Windows 1 · Linux 2 · macOS 1</div>'), "macOS's ties Linux's runs, so after it");
});

test("a held report released is counted again: its day folded afresh tonight", async () => {
  const { db, junk } = await held();
  await db.batch(admin.release(db, junk.id));
  assert.deepEqual(rows(db, `SELECT quarantined FROM reports WHERE id = '${junk.id}'`), [{ quarantined: 0 }]);
  assert.deepEqual(rows(db, "SELECT * FROM held_reports"), []);
  assert.deepEqual(rows(db, "SELECT * FROM folded"), [], "counted again tonight");
});

test("a held report deleted goes, with its device's record if nothing else of it is left", async () => {
  const { db, junk } = await held();
  await db.batch(admin.remove(db, junk.id));
  assert.deepEqual(rows(db, "SELECT count(*) AS n FROM reports"), [{ n: 1 }]);
  assert.deepEqual(rows(db, "SELECT DISTINCT id FROM report_stages"), rows(db, "SELECT id FROM reports"),
    "its stages go with it");
  assert.deepEqual(rows(db, "SELECT device_hash FROM devices ORDER BY device_hash"), [{ device_hash: "device-1" }]);
  assert.deepEqual(rows(db, "SELECT * FROM folded"), [{ day: "2026-11-09" }], "nothing to count again");
});

test("only a report held can be released or deleted from here", async () => {
  const { db } = await held();
  const [{ id }] = rows(db, "SELECT id FROM reports WHERE quarantined = 0");
  await db.batch(admin.remove(db, id));
  await db.batch(admin.release(db, id));
  assert.deepEqual(rows(db, `SELECT count(*) AS n FROM reports WHERE id = '${id}'`), [{ n: 1 }]);
});

test("a change comes only from a form on this view itself", async () => {
  const { bindings, db, junk } = await held();
  const post = async (headers) => admin.handle(new Request("https://admin.firmwareliberator.com/held", {
    method: "POST", body: new URLSearchParams({ id: junk.id, action: "delete" }),
    headers: { "Cf-Access-Jwt-Assertion": await token(), ...headers } }), bindings, NOW, keys);
  for (const headers of [{}, { Origin: "https://evil.example" }, { Origin: admin.ORIGIN, "Sec-Fetch-Site": "cross-site" }]) {
    assert.equal((await post(headers)).status, 403, JSON.stringify(headers));
  }
  assert.deepEqual(rows(db, "SELECT count(*) AS n FROM held_reports"), [{ n: 1 }], "nothing changed");
  const done = await post({ Origin: admin.ORIGIN, "Sec-Fetch-Site": "same-origin" });
  assert.deepEqual([done.status, done.headers.get("Location")], [303, "/#held"]);
  assert.deepEqual(rows(db, "SELECT count(*) AS n FROM held_reports"), [{ n: 0 }]);
});

test("the email when readouts agree is switched off and on from the view, and only from it", async () => {
  const bindings = env();
  const set = async (value, headers = { Origin: admin.ORIGIN, "Sec-Fetch-Site": "same-origin" }, name = "email_on_agreement") =>
    admin.handle(new Request("https://admin.firmwareliberator.com/settings", { method: "POST",
      body: new URLSearchParams({ name, value }), headers: { "Cf-Access-Jwt-Assertion": await token(), ...headers } }),
    bindings, NOW, keys);
  const page = async () => admin.page(bindings, { email: "owner@example.com" }, new Date(NOW), keys);
  assert.match(await page(), /Email me when readouts come to agree: <strong>on<\/strong> <button>Turn off/, "on unless set");
  assert.equal((await set("off", { Origin: "https://evil.example" })).status, 403);
  assert.equal((await set("maybe")).status, 400);
  assert.equal((await set("off", undefined, "something_else")).status, 400);
  assert.deepEqual(rows(bindings.DB, "SELECT * FROM settings"), []);
  const done = await set("off");
  assert.deepEqual([done.status, done.headers.get("Location")], [303, "/#switches"]);
  assert.deepEqual(rows(bindings.DB, "SELECT name, value FROM settings"), [{ name: "email_on_agreement", value: "off" }]);
  assert.match(await page(), /<strong>off<\/strong> <button>Turn on/);
  await set("on");
  assert.deepEqual(rows(bindings.DB, "SELECT value FROM settings"), [{ value: "on" }]);
});

test("readouts alike from too few networks are listed, and approved from the view alone", async () => {
  const bindings = env();
  const add = bindings.DB.db.prepare(`INSERT INTO readouts (id, key, stage, sender, fingerprint, bytes, object, received)
    VALUES (?, 'NEWTHING|SN-NEW-01|1.0.0', 'whole', ?, ?, 100, 'readouts/x.tar', '2026-11-09T10:00:00Z')`);
  for (const n of [1, 2, 3]) {
    add.run(`r${n}`, `s${n}`, "f".repeat(64));
    bindings.DB.db.prepare("INSERT INTO readout_networks (id, network) VALUES (?, ?)").run(`r${n}`, n < 3 ? "a" : "b");
  }
  add.run("r4", "s4", "e".repeat(64));   // another fingerprint: not one of them
  bindings.DB.db.prepare("INSERT INTO counters (name, day, count, bytes) VALUES ('readouts:pending', NULL, 4, 400)").run();
  const html = await admin.page(bindings, { email: "owner@example.com" }, new Date(NOW), keys);
  assert.match(html, /Nearly agreed/);
  assert.match(html, /NEWTHING\|SN-NEW-01\|1\.0\.0<\/td><td>whole<\/td><td><\/td><td class="number">3<\/td><td class="number">2<\/td>/,
    "a whole readout's offer is not what it is agreed on");
  const approve = async (fields, headers = { Origin: admin.ORIGIN, "Sec-Fetch-Site": "same-origin" }) =>
    admin.handle(new Request("https://admin.firmwareliberator.com/approve", { method: "POST",
      body: new URLSearchParams(fields), headers: { "Cf-Access-Jwt-Assertion": await token(), ...headers } }),
    bindings, NOW, keys);
  const group = { key: "NEWTHING|SN-NEW-01|1.0.0", stage: "whole", fingerprint: "f".repeat(64) };
  assert.equal((await approve(group, { Origin: "https://evil.example" })).status, 403);
  assert.equal((await approve({ ...group, stage: "half" })).status, 400);
  assert.equal((await approve({ ...group, fingerprint: "e".repeat(64) })).status, 303);
  assert.deepEqual(rows(bindings.DB, "SELECT review FROM readouts WHERE id = 'r4'"), [{ review: "pending" }],
    "one alone is not nearly agreed: nothing approved");
  const done = await approve(group);
  assert.deepEqual([done.status, done.headers.get("Location")], [303, "/#nearly"]);
  assert.deepEqual(rows(bindings.DB, "SELECT id, review FROM readouts ORDER BY id"),
    [{ id: "r1", review: "approved" }, { id: "r2", review: "approved" }, { id: "r3", review: "approved" },
      { id: "r4", review: "pending" }]);
  assert.deepEqual(rows(bindings.DB, "SELECT count, bytes FROM counters WHERE name = 'readouts:pending'"),
    [{ count: 1, bytes: 100 }], "their room given back");
});

test("a form not of a held report and an action is turned away", async () => {
  const { bindings } = await held();
  for (const body of [{ id: "nope", action: "delete" }, { id: "f".repeat(16), action: "drop" }]) {
    const response = await admin.handle(new Request("https://admin.firmwareliberator.com/held", {
      method: "POST", body: new URLSearchParams(body),
      headers: { "Cf-Access-Jwt-Assertion": await token(), Origin: admin.ORIGIN } }), bindings, NOW, keys);
    assert.equal(response.status, 400, JSON.stringify(body));
  }
});

test("the store's pace, and the day it would be full at it", () => {
  const day = (n) => new Date(NOW - (30 - n) * 86_400_000).toISOString().slice(0, 10);
  const store = Array.from({ length: 31 }, (_, n) => ({ day: day(n), bytes: n * 10 * 1024 * 1024 }));
  const pace = admin.pace(store, new Date(NOW));
  assert.equal(Math.round(pace.perDay / 1024 / 1024), 10);
  assert.match(pace.full, /^20\d\d-\d\d-\d\d$/);
  assert.equal(admin.pace(store.slice(0, 1), new Date(NOW)), null);
  assert.equal(admin.pace(store.map((row) => ({ ...row, bytes: 5 })), new Date(NOW)).full, null, "not filling");
});

test("the month's use: from Cloudflare's analytics, said plainly when it cannot be", async () => {
  assert.match(await admin.usage(env(), new Date(NOW)), /ANALYTICS_TOKEN/);
  const answer = (body) => async () => new Response(JSON.stringify(body));
  const bindings = env({ ANALYTICS_TOKEN: "t", ACCOUNT_ID: "a1" });
  assert.match(await admin.usage(bindings, new Date(NOW), answer({ errors: [{ message: "unknown field" }] })), /unknown field/);
  const used = await admin.usage(bindings, new Date(NOW), answer({ data: { viewer: { accounts: [{
    workers: [{ sum: { requests: 7 } }], d1: [{ sum: { rowsRead: 9, rowsWritten: 3 } }],
    r2: [{ max: { payloadSize: 5, metadataSize: 1 }, dimensions: { bucketName: "b" } }] }] } } }));
  assert.deepEqual(used, { "Workers requests this month": 7, "D1 rows read this month": 9, "D1 rows written this month": 3,
    "R2 storage, bytes": 6 });
});

test("what the plan includes is api_health.py's, and the view binds the API's database", () => {
  const health = readFileSync(new URL("../../tools/repo/api_health.py", import.meta.url), "utf8");
  for (const [what, value] of Object.entries(admin.INCLUDED)) {
    // As Python may write it: 10_000_000, or 10 * 1000 ** 3.
    const forms = [value.toLocaleString("en").replaceAll(",", "_")];
    if (value % 1000 ** 3 === 0) forms.push(`${value / 1000 ** 3} \\* 1000 \\*\\* 3`);
    assert.match(health, new RegExp(`"${what}": (${forms.join("|")}),`), what);
  }
  const id = (path) => /"database_id": "([^"]+)"/.exec(readFileSync(new URL(path, import.meta.url), "utf8"))[1];
  assert.equal(id("../admin/wrangler.jsonc"), id("../api/wrangler.jsonc"));
});

test("every answer is uncached, unframed, unindexed and runs no script", async () => {
  const response = await worker.fetch(asked(null), env());
  const csp = response.headers.get("Content-Security-Policy");
  assert.match(csp, /default-src 'none'/);
  assert.doesNotMatch(csp, /script-src/);
  assert.match(csp, /frame-ancestors 'none'/);
  assert.equal(response.headers.get("Cache-Control"), "no-store");
  assert.equal(response.headers.get("X-Robots-Tag"), "noindex, nofollow");
});

test("each model's devices month by month: asked to be read out, read out, converted", async () => {
  const bindings = env();
  const db = bindings.DB.db;
  const wanted = db.prepare("INSERT INTO wanted_devices (device_hash, model, first_day, last_day, asks) VALUES (?, ?, ?, ?, 1)");
  wanted.run("a", "NEWTHING", "2026-09-02", "2026-11-01");
  wanted.run("b", "NEWTHING", "2026-11-03", "2026-11-03");
  wanted.run("c", "NEWTHING", "2024-01-01", "2024-01-01");   // before the charts begin
  const readOut = db.prepare(`INSERT INTO readout_reports (id, day, tag, tool, result, model, device_hash, os_family,
    os_version, os_arch, python_version, python_source, seconds, converted_here, quarantined)
    VALUES (?, ?, ?, '3.0.0', 'accepted', 'NEWTHING', ?, 'linux', 'x', 'x86-64', '3.12.3', 'venv', 60, 0, ?)`);
  readOut.run("1", "2026-11-04", "user", "a", 0);
  readOut.run("2", "2026-11-05", "user", "a", 0);   // the same device again that month: one
  readOut.run("3", "2026-11-05", "dev", "d", 0);   // the maintainer's own testing
  readOut.run("4", "2026-11-05", "user", "e", 1);   // quarantined
  const run = (id, hash, day, changes = {}) => bindings.DB.batch(writes(bindings.DB,
    { ...structuredClone(FIXTURE), id, model: "S60TPG", ...changes }, hash, day));
  await run("r1", "f", "2026-10-01");
  await run("r2", "g", "2026-10-02");
  await run("r3", "g", "2026-10-20", { firmware: "esphome", source: "tasmota" });   // converted again: one that month
  await run("r4", "g", "2026-11-02", { firmware: "esphome", source: "tasmota" });   // and again the next
  await run("r5", "h", "2026-10-03", { firmware: "sonoff", source: "tasmota" });   // back to Sonoff's: no conversion
  await run("r6", "i", "2026-10-04", { tag: "dev" });   // the maintainer's own
  const { months, models } = await admin.gather(bindings, new Date(NOW)).then((data) => data.popularity);
  assert.deepEqual([months.length, months[0], months.at(-1)], [admin.POPULAR_MONTHS, "2024-12", "2026-11"]);
  assert.deepEqual(models.map(({ model, series }) => [model, series]), [
    ["NEWTHING", { "asked to be read out": { "2026-09": 1, "2026-11": 1 }, "read out": { "2026-11": 1 } }],
    ["S60TPG", { converted: { "2026-10": 2, "2026-11": 1 } }]]);
  const html = await admin.page(bindings, { email: "owner@example.com" }, new Date(NOW), keys);
  assert.match(html, /<figcaption>NEWTHING<\/figcaption>/);
  assert.match(html, /<title>S60TPG, 2026-10: 2 converted<\/title>/);
});

test("the stages that needed the device's own network, and why, or a power cycle, out of the runs through them", async () => {
  const bindings = env();
  const stages = (d4) => ({ A1: { seconds: 12, busy: 0, tries: 1, wait: 0, setup_network: null, power_cycles: 0 },
    D4: { seconds: 61, busy: 0, tries: 1, wait: 0, power_cycles: 0, setup_network: null, ...d4 } });
  const run = (id, d4, changes = {}) => bindings.DB.batch(writes(bindings.DB,
    { ...structuredClone(FIXTURE), id, stages: stages(d4), ...changes }, `device-${id}`, "2026-11-01"));
  await run("r1", { setup_network: "not given", power_cycles: 2 });
  await run("r2", { setup_network: "not joined" });
  await run("r3", {});
  await run("r4", { setup_network: "not given" }, { tag: "dev" });   // the maintainer's own: left out
  const { needed } = await admin.gather(bindings, new Date(NOW));
  const why = (reason) => `why${admin.REASONS.indexOf(reason)}`;
  assert.deepEqual(needed.map((row) => [row.stage, row.runs, row.setup, row[why("not given")], row[why("not joined")],
    row.cycled, row.cycles]), [["D4", 3, 2, 1, 1, 1, 2]], "A1 needed neither");
  const html = await admin.page(bindings, { email: "owner@example.com" }, new Date(NOW), keys);
  assert.match(html, /Setup network and power cycles, by stage/);
  assert.match(html, /<th class="number">- settings lost<\/th>/);
});
