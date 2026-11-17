// api/nightly.js and api/snapshot.js: what is kept of the reports for good,
// and the public figures made from it.
import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { LATEST_MIGRATION } from "../api/database.js";
import { d1 } from "../api/local-d1.mjs";
import * as night from "../api/nightly.js";
import { writes } from "../api/reports.js";
import { SMALLEST } from "../api/snapshot.js";

// Nothing here reaches the internet: the nightly cron's fetch of the lists of
// networks (networks.js) fails, as it may, and is told so.
globalThis.fetch = async () => {
  throw new Error("no network in the tests");
};

const FIXTURE = JSON.parse(readFileSync(new URL("./fixtures/reports.json", import.meta.url), "utf8")).valid[0];
const NIGHT = new Date("2026-11-10T03:17:00Z");

// A report as /reports writes it, on `day`.
async function kept(db, day, changes = {}, hash = changes.device_hash ?? null) {
  const report = { ...structuredClone(FIXTURE), ...changes, id: randomBytes(8).toString("hex") };
  await db.batch(writes(db, report, hash, day));
  return report.id;
}

function bucket() {
  const objects = new Map();
  return {
    objects,
    head: async (key) => (objects.has(key) ? { key } : null),
    put: async (key, body, options) => objects.set(key, { body, options }),
  };
}

const env = () => ({ DB: d1(), STATS: bucket() });
const rows = (bindings, sql, ...values) => bindings.DB.db.prepare(sql).all(...values).map((row) => ({ ...row }));
const figures = (bindings) => JSON.parse(bindings.STATS.objects.get(night.SNAPSHOT_KEY).body);
// Models enough senders have given, over enough days, to be shown (vouching).
const vouched = (bindings, ...models) => models.forEach((model) =>
  bindings.DB.db.prepare("INSERT INTO vouched_models (model, day) VALUES (?, '2026-11-01')").run(model));

test("each day before today is counted once, and today's are left for tomorrow", async () => {
  const bindings = env();
  await kept(bindings.DB, "2026-11-09");
  await kept(bindings.DB, "2026-11-09", { result: "failed", stopped: { step: "D4", error: "upload-cut", text: "x" } });
  await kept(bindings.DB, "2026-11-10");
  await night.nightly(bindings, NIGHT);
  await night.nightly(bindings, NIGHT);
  assert.deepEqual(rows(bindings, "SELECT day, result, step, error, n FROM daily_totals ORDER BY result"), [
    { day: "2026-11-09", result: "converted", step: "", error: "", n: 1 },
    { day: "2026-11-09", result: "failed", step: "D4", error: "upload-cut", n: 1 },
  ]);
  assert.deepEqual(rows(bindings, "SELECT day FROM folded"), [{ day: "2026-11-09" }]);
  assert.deepEqual(rows(bindings, "SELECT month, n FROM monthly_totals ORDER BY n"),
    [{ month: "2026-11", n: 1 }, { month: "2026-11", n: 1 }]);
});

test("a day unfolded after a report of it is quarantined is counted again without it", async () => {
  const bindings = env();
  const bad = await kept(bindings.DB, "2026-09-03");
  await kept(bindings.DB, "2026-09-03");
  await night.nightly(bindings, NIGHT);
  assert.equal(rows(bindings, "SELECT n FROM monthly_totals WHERE month = '2026-09'")[0].n, 2);
  bindings.DB.db.prepare("UPDATE reports SET quarantined = 1 WHERE id = ?").run(bad);
  bindings.DB.db.prepare("DELETE FROM folded WHERE day = '2026-09-03'").run();
  const ran = await night.nightly(bindings, NIGHT);
  assert.ok(ran.months.includes("2026-09"), "a month long past made again too");
  assert.equal(rows(bindings, "SELECT n FROM daily_totals WHERE day = '2026-09-03'")[0].n, 1);
  assert.equal(rows(bindings, "SELECT n FROM monthly_totals WHERE month = '2026-09'")[0].n, 1);
});

test("run and step times are a month's median and 90th percentile", async () => {
  const bindings = env();
  for (let n = 1; n <= 10; n += 1) {
    await kept(bindings.DB, "2026-11-03", { seconds: n * 100, stages: { A1: { seconds: n, busy: 0, tries: 1, wait: 0, setup_network: null, power_cycles: 0 } } });
  }
  await kept(bindings.DB, "2026-11-03", { seconds: 99999, result: "failed",
    stopped: { step: "D4", error: "other", text: "" } });   // not a conversion: not a run's time
  await night.nightly(bindings, NIGHT);
  assert.deepEqual(rows(bindings, "SELECT n, p50, p90 FROM monthly_times"), [{ n: 10, p50: 500, p90: 900 }]);
  assert.deepEqual(rows(bindings, "SELECT tool, step, n, p50, p90 FROM monthly_steps WHERE step = 'A1'"),
    [{ tool: "2.4.0", step: "A1", n: 11, p50: 6, p90: 10 }], "1 to 10, and the failed run's 12: every run's steps count");
});

test("every report is kept, however old; a limit's day counts go after a week, the lasting ones never", async () => {
  const bindings = env();
  await kept(bindings.DB, "2020-11-09");
  await kept(bindings.DB, "2025-11-11");
  const insert = bindings.DB.db.prepare(`INSERT INTO readout_reports (id, day, tag, tool, result, os_family,
    os_version, os_arch, python_version, python_source, seconds, converted_here) VALUES (?, ?, 'user', '3.0.0',
    'accepted', 'linux', 'Ubuntu 24.04', 'x86-64', '3.12.3', 'venv', 60, 0)`);
  insert.run("a".repeat(16), "2020-01-01");
  const counter = bindings.DB.db.prepare("INSERT INTO counters (name, day, count, bytes) VALUES (?, ?, 1, 0)");
  counter.run("reports:2026-11-02", "2026-11-02");
  counter.run("reports:2026-11-04", "2026-11-04");
  counter.run("readouts:all", null);
  await night.nightly(bindings, NIGHT);
  assert.deepEqual(rows(bindings, "SELECT day FROM reports ORDER BY day"), [{ day: "2020-11-09" }, { day: "2025-11-11" }]);
  assert.equal(rows(bindings, "SELECT DISTINCT id FROM report_stages").length, 2, "and their stages");
  assert.deepEqual(rows(bindings, "SELECT id FROM readout_reports"), [{ id: "a".repeat(16) }], "a readout's too");
  assert.equal(rows(bindings, "SELECT n FROM daily_totals WHERE day = '2020-11-09'")[0].n, 1, "counted all the same");
  assert.deepEqual(rows(bindings, "SELECT name FROM counters ORDER BY name"),
    [{ name: "readouts:all" }, { name: "reports:2026-11-04" }]);
});
test("the figures leave out the maintainer's testing, and anything under five", async () => {
  const bindings = env();
  const hashes = Array.from({ length: 6 }, (_, n) => `device-${n}`);
  for (const hash of hashes) {
    await kept(bindings.DB, "2026-11-05", { device_hash: hash });
  }
  await kept(bindings.DB, "2026-11-05", { device_hash: "dev-device", tag: "dev" });
  for (let n = 0; n < 4; n += 1) {
    await kept(bindings.DB, "2026-11-06", { model: "RARE1", device_hash: `rare-${n}`, device: null });
  }
  vouched(bindings, "S60TPG", "RARE1");
  await night.nightly(bindings, NIGHT);
  const stats = figures(bindings);
  assert.equal(stats.devices.seen, 6, "the dev device left out, and RARE1's, of no device kind the tool knows");
  assert.equal(stats.runs.runs, 10);
  assert.deepEqual(stats.kinds.map((row) => [row.device, row.devices, row.models]),
    [["sonoff-s60", 6, [{ model: "S60TPG", devices: 6 }]]], "RARE1's four of no kind");
  assert.equal(stats.kinds[0].firstRun, 100);
  assert.ok(!JSON.stringify(stats).includes("RARE1"));
  assert.ok(!JSON.stringify(stats).includes("device-"), "no device's id");
});

test("a run that met a device the tool does not convert yet is no failure, but counted apart", async () => {
  const bindings = env();
  for (let n = 0; n < 5; n += 1) {
    await kept(bindings.DB, "2026-11-05", { device_hash: `ok-${n}` });
    await kept(bindings.DB, "2026-11-05", { model: "NEWONE", device: null, family: null, device_hash: `new-${n}`,
      result: "failed", stopped: { step: "A2", error: "unsupported-device", text: "" } });
  }
  await night.nightly(bindings, NIGHT);
  const { runs, failures, devices } = figures(bindings);
  assert.deepEqual([runs.runs, runs.converted, runs.failed, runs.unsupported, runs.success], [10, 5, null, 5, 100]);
  assert.deepEqual(failures, [], "not where runs stop");
  assert.deepEqual([devices.seen, devices.firstRun], [5, 100], "the devices the tool converts");
});

test("a run declined, or with nothing to do, is not a failure", async () => {
  const bindings = env();
  const failed = (error) => ({ result: "failed", stopped: { step: "A3", error, text: "" } });
  for (let n = 0; n < 5; n += 1) {
    await kept(bindings.DB, "2026-11-05");
    await kept(bindings.DB, "2026-11-05", failed("declined"));
    await kept(bindings.DB, "2026-11-05", failed("nothing-to-do"));
  }
  for (let n = 0; n < 5; n += 1) {
    await kept(bindings.DB, "2026-11-05", failed("device-lost"));
  }
  await night.nightly(bindings, NIGHT);
  const { runs, failures } = figures(bindings);
  assert.deepEqual([runs.runs, runs.converted, runs.failed, runs.declined, runs.success], [20, 5, 5, 10, 50]);
  assert.deepEqual(failures, [{ step: "A3", error: "device-lost", runs: 5 }]);
});

test("a run the person stopped is no failure: counted apart, with the stages people stop at", async () => {
  const bindings = env();
  const cancelled = (step, error) => ({ result: "cancelled", stopped: { step, error, text: "" } });
  for (let n = 0; n < 5; n += 1) {
    await kept(bindings.DB, "2026-11-05");
    await kept(bindings.DB, "2026-11-05", cancelled("D1", n % 2 ? "quit" : "ctrl-c"));
    await kept(bindings.DB, "2026-11-05", cancelled("C3", "window-closed"));
  }
  await kept(bindings.DB, "2026-11-05", cancelled("A3", "quit"));   // fewer than five: not shown
  await night.nightly(bindings, NIGHT);
  const { runs, failures, cancellations } = figures(bindings);
  assert.deepEqual([runs.runs, runs.converted, runs.failed, runs.cancelled, runs.success], [16, 5, null, 11, 100]);
  assert.deepEqual(failures, []);
  assert.deepEqual(cancellations, [{ step: "C3", runs: 5 }, { step: "D1", runs: 5 }], "by stage, however stopped");
});

test("computers by how many devices each converted, from each device's number on its computer", async () => {
  const bindings = env();
  // Seven computers converted one device or more, six two or more, five three: 1, 1, 5 exactly.
  const numbers = [...Array(7).fill(1), ...Array(6).fill(2), ...Array(5).fill(3)];
  for (const [n, place] of numbers.entries()) {
    await kept(bindings.DB, "2026-11-05", { device_hash: `device-${n}`, nth_device: place });
  }
  await kept(bindings.DB, "2026-11-05", { device_hash: "tested", tag: "dev", nth_device: 1 });   // the maintainer's
  await night.nightly(bindings, NIGHT);
  const { perComputer } = figures(bindings);
  assert.deepEqual(perComputer, { computers: 7, rows: [{ devices: 3, computers: 5 }] }, "under five left out");
});

test("a computer whose earlier conversions sent no report counts at every number below its own", async () => {
  // --no-report for its first nineteen: its twentieth still says it converted twenty.
  const bindings = env();
  for (let n = 0; n < 5; n += 1) {
    await kept(bindings.DB, "2026-11-05", { device_hash: `heavy-${n}`, nth_device: 20 });
  }
  await night.nightly(bindings, NIGHT);
  assert.deepEqual(figures(bindings).perComputer, { computers: 5, rows: [{ devices: 20, computers: 5 }] });
});

test("with reports missing, the computers counted are never more than there are", async () => {
  // Five converted one device each, five twenty with only the twentieth reported: no
  // report ties a device to its computer, so the first devices may be the heavy ones'.
  const bindings = env();
  for (let n = 0; n < 5; n += 1) {
    await kept(bindings.DB, "2026-11-05", { device_hash: `heavy-${n}`, nth_device: 20 });
    await kept(bindings.DB, "2026-11-05", { device_hash: `one-${n}`, nth_device: 1 });
  }
  await night.nightly(bindings, NIGHT);
  const { perComputer } = figures(bindings);
  assert.ok(perComputer.computers >= 5 && perComputer.computers <= 10, perComputer.computers);
  assert.deepEqual(perComputer.rows, [{ devices: 20, computers: 5 }]);
});

test("the devices wanted: each device once, asked about for a readout or met by a run, however often", async () => {
  const bindings = env();
  const ask = bindings.DB.db.prepare(`INSERT INTO wanted_devices (device_hash, model, first_day, last_day, asks)
    VALUES (?, ?, '2026-11-01', '2026-11-02', ?)`);
  for (let n = 0; n < 5; n += 1) {
    ask.run(`nspanel-${n}`, "NSPANEL", 3);   // asked three times each: five devices
  }
  ask.run("few-1", "FEW", 9);   // however often, one device
  for (let n = 0; n < 5; n += 1) {
    await kept(bindings.DB, "2026-11-05", { model: "BASICR4", device: null, result: "failed", device_hash: `basicr4-${n}`,
      stopped: { step: "A2", error: "unsupported-device", text: "", crash_site: null } });
  }
  await kept(bindings.DB, "2026-11-05", { model: "NSPANEL", device: null, result: "failed", device_hash: "nspanel-0",
    stopped: { step: "A2", error: "unsupported-device", text: "", crash_site: null } });   // asked about and met: once
  vouched(bindings, "NSPANEL", "FEW", "BASICR4");
  await night.nightly(bindings, NIGHT);
  assert.deepEqual(figures(bindings).wanted, [
    { model: "BASICR4", devices: 5, asked: null, ran: 5 },
    { model: "NSPANEL", devices: 5, asked: 5, ran: null },
  ]);
});

test("each model's devices month by month, as the admin view counts them, nothing under five", async () => {
  const bindings = env();
  for (let n = 0; n < 6; n += 1) {
    await kept(bindings.DB, "2026-10-05", { model: "S60TPG", device_hash: `s60-${n}` });
  }
  await kept(bindings.DB, "2026-11-05", { model: "S60TPG", device_hash: "s60-0" });   // one in November: too few
  for (let n = 0; n < 5; n += 1) {
    await kept(bindings.DB, "2026-10-05", { model: "UNVOUCHED", device_hash: `u-${n}` });
  }
  vouched(bindings, "S60TPG");
  await night.nightly(bindings, NIGHT);
  assert.deepEqual(figures(bindings).modelMonths,
    [{ month: "2026-10", model: "S60TPG", asked: null, readOut: null, converted: 6 }]);
});

test("a model's name is shown once three senders from two networks have given it, on three days, and who gave it then forgotten", async () => {
  assert.deepEqual([night.VOUCH_SENDERS, night.VOUCH_DAYS, night.VOUCH_NETWORKS, night.VOUCH_KEEP_DAYS], [3, 3, 2, 365]);
  const bindings = env();
  const gave = bindings.DB.db.prepare("INSERT INTO model_senders (model, sender, network, day) VALUES (?, ?, ?, ?)");
  for (const [sender, network, day] of [["a", "n1", "2026-11-01"], ["b", "n2", "2026-11-03"], ["c", "n2", "2026-11-05"]]) {
    gave.run("TRUSTED1", sender, network, day);
  }
  for (const day of ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04"]) {
    gave.run("ONESENDER", "a", "n1", day);   // one sender, however many days
  }
  for (const [sender, network] of [["a", "n1"], ["b", "n2"], ["c", "n3"], ["d", "n4"]]) {
    gave.run("ONEEVENING", sender, network, "2026-11-08");   // many senders, one day
  }
  for (const [sender, day] of [["a", "2026-11-01"], ["b", "2026-11-03"], ["c", "2026-11-05"]]) {
    gave.run("ONENETWORK", sender, "n9", day);   // one rented server's network
  }
  gave.run("LONGAGO", "z", "n1", "2025-11-09");
  for (const model of ["TRUSTED1", "ONESENDER", "ONEEVENING", "ONENETWORK"]) {
    for (let n = 0; n < 5; n += 1) {
      await kept(bindings.DB, "2026-11-05", { model, device_hash: `${model}-${n}` });
    }
  }
  await night.nightly(bindings, NIGHT);
  assert.deepEqual(rows(bindings, "SELECT model, day FROM vouched_models"), [{ model: "TRUSTED1", day: "2026-11-10" }]);
  assert.deepEqual(figures(bindings).kinds.flatMap((row) => row.models.map((each) => each.model)), ["TRUSTED1"]);
  assert.ok(!JSON.stringify(figures(bindings)).includes("ONESENDER"));
  assert.ok(!JSON.stringify(figures(bindings)).includes("ONEEVENING"));
  assert.ok(!JSON.stringify(figures(bindings)).includes("ONENETWORK"));
  assert.deepEqual(rows(bindings, "SELECT DISTINCT model FROM model_senders ORDER BY model"),
    [{ model: "ONEEVENING" }, { model: "ONENETWORK" }, { model: "ONESENDER" }],
    "TRUSTED1's vouched for, LONGAGO's past a year: forgotten");
});

test("the figures say what they are, how fresh, and for which schema", async () => {
  const bindings = env();
  await night.nightly(bindings, NIGHT);
  const { options } = bindings.STATS.objects.get(night.SNAPSHOT_KEY);
  const stats = figures(bindings);
  assert.deepEqual([stats.format, stats.generated, stats.through, stats.schema, stats.licence, stats.smallest],
    [1, "2026-11-10T03:17Z", "2026-11-09", LATEST_MIGRATION, "CC BY 4.0", SMALLEST]);
  assert.equal(options.httpMetadata.contentType, "application/json");
  assert.equal(stats.devices.seen, null, "none yet: under five");
});

test("one copy a month is kept, the first written", async () => {
  const bindings = env();
  await night.nightly(bindings, NIGHT);
  const first = bindings.STATS.objects.get("v1/archive/2026-11.json").body;
  await kept(bindings.DB, "2026-11-10");
  await night.nightly(bindings, new Date("2026-11-11T03:17:00Z"));
  assert.equal(bindings.STATS.objects.get("v1/archive/2026-11.json").body, first);
  assert.notEqual(bindings.STATS.objects.get(night.SNAPSHOT_KEY).body, first);
  await night.nightly(bindings, new Date("2026-12-01T03:17:00Z"));
  assert.ok(bindings.STATS.objects.has("v1/archive/2026-12.json"));
});

test("nothing is done before the database has every migration", async () => {
  const bindings = { ...env(), DB: d1({ migrated: false }) };
  assert.deepEqual(await night.nightly(bindings, NIGHT),
    { ran: false, why: "the database does not have every migration yet" });
  assert.equal(bindings.STATS.objects.size, 0);
});

test("a night costs a fixed few queries, well within even the free plan's 50", async () => {
  const bindings = env();
  for (let day = 1; day <= 9; day += 1) {
    await kept(bindings.DB, `2026-11-0${day}`);
  }
  let queries = 0;
  const db = bindings.DB;
  const counted = { ...db, prepare: (sql) => {
    const wrap = (inner) => ({ ...inner, inner, bind: (...values) => wrap(inner.bind(...values)),
      first: (...args) => { queries += 1; return inner.first(...args); },
      all: () => { queries += 1; return inner.all(); }, run: () => { queries += 1; return inner.run(); } });
    return wrap(db.prepare(sql));
  }, batch: (statements) => { queries += 1; return db.batch(statements.map((statement) => statement.inner)); } };
  await night.nightly({ ...bindings, DB: counted }, NIGHT);
  assert.ok(queries <= 25, `${queries} queries`);
});

test("the nightly cron runs the nightly job; the hourly one the reminders alone", async () => {
  const config = readFileSync(new URL("../api/wrangler.jsonc", import.meta.url), "utf8");
  assert.match(config, /"crons": \["17 3 \* \* \*", "47 \* \* \* \*"\]/);
  assert.match(config, /"binding": "STATS", "bucket_name": "firmware-liberator-stats"/);
  const { default: worker } = await import("../api/index.js");
  const waited = [];
  await worker.scheduled({ cron: "17 3 * * *", scheduledTime: NIGHT.getTime() }, env(),
    { waitUntil: (promise) => waited.push(promise) });
  assert.equal(waited.length, 1);
  assert.equal((await waited[0]).ran, true);
  const none = "GitHub: no network in the tests; jsDelivr: no network in the tests";
  assert.deepEqual((await waited[0]).networks, { datacenter: none, vpn: none },
    "the lists of networks fetched first");
  assert.equal((await waited[0]).downloads, "no network in the tests", "and the downloads read, before the figures");
  await worker.scheduled({ cron: "47 * * * *", scheduledTime: NIGHT.getTime() }, env(),
    { waitUntil: (promise) => waited.push(promise) });
  assert.deepEqual(await waited[1], { reminded: 0 }, "no nightly job hourly");
});

test("each day's emails are kept by outcome after the counts behind them go", async () => {
  const bindings = env();
  const count = bindings.DB.db.prepare("INSERT INTO counters (name, day, count, bytes) VALUES (?, ?, ?, 0)");
  count.run("mail-sent:2026-11-09", "2026-11-09", 12);
  count.run("mail-failed:E_RECIPIENT_SUPPRESSED:2026-11-09", "2026-11-09", 2);
  count.run("mail-failed:E_RATE_LIMITED:2026-11-02", "2026-11-02", 1);
  count.run("mail-sent:2026-11-10", "2026-11-10", 3);   // today's: tomorrow
  await night.nightly(bindings, NIGHT);
  assert.deepEqual(rows(bindings, "SELECT day, outcome, code, n FROM mail_days ORDER BY day, outcome, code"), [
    { day: "2026-11-02", outcome: "failed", code: "E_RATE_LIMITED", n: 1 },
    { day: "2026-11-09", outcome: "failed", code: "E_RECIPIENT_SUPPRESSED", n: 2 },
    { day: "2026-11-09", outcome: "sent", code: "", n: 12 },
  ]);
  assert.deepEqual(rows(bindings, "SELECT name FROM counters WHERE name GLOB 'mail-*' ORDER BY name").map((row) => row.name),
    ["mail-failed:E_RECIPIENT_SUPPRESSED:2026-11-09", "mail-sent:2026-11-09", "mail-sent:2026-11-10"],
    "the count over a week old gone, its copy kept");
});

test("the month before January is December of the year before", () => {
  assert.equal(night.monthBefore("2027-01"), "2026-12");
  assert.equal(night.monthBefore("2026-11"), "2026-10");
});

test("a report held for review is in no total", async () => {
  const bindings = env();
  const db = bindings.DB;
  const report = { ...structuredClone(FIXTURE), id: randomBytes(8).toString("hex") };
  await db.batch(writes(db, report, null, "2026-11-09", "words no report would hold"));
  await night.nightly(bindings, NIGHT);
  assert.equal(rows(bindings, "SELECT * FROM daily_totals").length, 0);
});

test("each night notes how much the store of readouts holds", async () => {
  const bindings = env();
  await night.nightly(bindings, NIGHT);
  bindings.DB.db.prepare("INSERT INTO counters (name, day, count, bytes) VALUES (?, NULL, 3, 3000)")
    .run(night.READOUTS_EVER);
  await night.nightly(bindings, new Date("2026-11-11T03:17:00Z"));
  await night.nightly(bindings, new Date("2026-11-11T03:18:00Z"));
  assert.deepEqual(rows(bindings, "SELECT day, readouts, bytes FROM store_days ORDER BY day"), [
    { day: "2026-11-10", readouts: 0, bytes: 0 },
    { day: "2026-11-11", readouts: 3, bytes: 3000 },
  ]);
  const index = readFileSync(new URL("../api/index.js", import.meta.url), "utf8");
  assert.match(index, new RegExp(`const READOUTS_EVER = "${night.READOUTS_EVER}";`), "named alike in index.js");
});

test("each night clears readouts never taken as right and addresses never confirmed (pending.js)", async () => {
  const bindings = { ...env(), READOUTS: { ...bucket(), delete: async () => {} } };
  bindings.DB.db.prepare(`INSERT INTO readouts (id, key, stage, sender, fingerprint, bytes, object, received, review)
    VALUES ('old', 'K', 'whole', 's', 'f', 1, 'readouts/x/old.tar', '2026-05-01T00:00:00Z', 'pending')`).run();
  bindings.DB.db.prepare(`INSERT INTO confirmations (token, who, email, devices, readouts, day, sent)
    VALUES ('t', 'w', 'e', '[]', '[]', '2026-10-01', '2026-10-01T12:00:00Z')`).run();
  const done = await night.nightly(bindings, NIGHT);
  assert.deepEqual(done.pruned, { readouts: 1, signups: 1 });
  assert.deepEqual(rows(bindings, "SELECT id FROM readouts"), []);
});

// `n` reports of `model` on `day`, each of a device of its own.
async function many(db, day, n, model = "S60TPG", changes = {}) {
  for (let i = 0; i < n; i += 1) {
    await kept(db, day, { model, device_hash: `${model}-${day}-${i}`, ...changes });
  }
}

test("a day far above a model's usual is held for review, and in no figure", async () => {
  const bindings = env();
  await many(bindings.DB, "2026-10-20", 2);
  await night.nightly(bindings, new Date("2026-10-21T03:17:00Z"));   // its usual, counted
  await many(bindings.DB, "2026-11-09", 30);
  const ran = await night.nightly(bindings, NIGHT);
  assert.deepEqual(ran.held, [{ day: "2026-11-09", named: "sonoff-s60", n: 30 }]);
  assert.equal(rows(bindings, "SELECT count(*) AS n FROM held_reports WHERE why = ?", night.SPIKE_WHY)[0].n, 30);
  assert.equal(rows(bindings, "SELECT count(*) AS n FROM reports WHERE quarantined = 1")[0].n, 30);
  assert.equal(rows(bindings, "SELECT * FROM daily_totals WHERE day = '2026-11-09'").length, 0);
  const stats = figures(bindings);
  assert.equal(stats.devices.seen, null, "the two of 2026-10-20 alone: under five");
  assert.deepEqual(stats.kinds, [], "the held devices are in no list");
});

test("a flood of a device's reports is held whatever models they name, or none", async () => {
  const bindings = env();
  for (let n = 0; n < 30; n += 1) {
    await kept(bindings.DB, "2026-11-09", { model: n % 2 ? null : `MADEUP${n}`, device_hash: `flood-${n}` });
  }
  const ran = await night.nightly(bindings, NIGHT);
  assert.deepEqual(ran.held, [{ day: "2026-11-09", named: "sonoff-s60", n: 30 }]);
  assert.equal(rows(bindings, "SELECT count(*) AS n FROM reports WHERE quarantined = 1")[0].n, 30);
});

test("the devices the tool converts are counted by kind, whether their model was named or not", async () => {
  const bindings = env();
  for (let n = 0; n < 9; n += 1) {
    // Six named S60TPG (vouched), one S60TPF (too few to name), two none.
    const model = n < 6 ? "S60TPG" : n === 6 ? "S60TPF" : null;
    await kept(bindings.DB, "2026-11-05", { model, device_hash: `s60-${n}`, stock_version: n < 5 ? "1.2.0" : "1.3.0" });
  }
  vouched(bindings, "S60TPG", "S60TPF");
  await night.nightly(bindings, NIGHT);
  const stats = figures(bindings);
  assert.deepEqual(stats.kinds.map((row) => [row.device, row.devices, row.converted, row.models]),
    [["sonoff-s60", 9, 9, [{ model: "S60TPG", devices: 6 }]]]);
  assert.deepEqual(stats.stock.map((row) => [row.device, row.stock_version, row.runs]),
    [["sonoff-s60", "1.2.0", 5]], "1.3.0's four under five");
  assert.ok(!JSON.stringify(stats).includes("S60TPF"), "a model with too few devices is not named");
});

test("a busy model at its usual pace is not held, and five times it is", async () => {
  const at = (day) => new Date(Date.parse("2026-10-12T00:00:00Z") + day * 86_400_000).toISOString().slice(0, 10);
  const bindings = env();
  for (let day = 0; day < night.SPIKE_DAYS; day += 1) {
    await many(bindings.DB, at(day), 10);   // its usual: 10 a day
  }
  await many(bindings.DB, "2026-11-09", 30);
  assert.deepEqual((await night.nightly(bindings, NIGHT)).held, [], "30 against 10 a day, all folded tonight");
  await many(bindings.DB, "2026-11-10", 60);
  assert.deepEqual((await night.nightly(bindings, new Date("2026-11-11T03:17:00Z"))).held,
    [{ day: "2026-11-10", named: "sonoff-s60", n: 60 }]);
});

test("a new model's first day is held only past the smallest a spike can be", async () => {
  const bindings = env();
  await many(bindings.DB, "2026-11-09", night.SPIKE_SMALLEST - 1, "NEWONE", { device: null });
  assert.deepEqual((await night.nightly(bindings, NIGHT)).held, []);
  const other = env();
  await many(other.DB, "2026-11-09", night.SPIKE_SMALLEST, "NEWTWO", { device: null });
  assert.equal((await night.nightly(other, NIGHT)).held.length, 1);
});

test("the maintainer's own testing never makes a spike, and a day counted is never looked at again", async () => {
  const bindings = env();
  await many(bindings.DB, "2026-11-09", 40, "S60TPG", { tag: "dev" });
  assert.deepEqual((await night.nightly(bindings, NIGHT)).held, []);
  await many(bindings.DB, "2026-11-08", 40);
  bindings.DB.db.prepare("INSERT INTO folded (day) VALUES ('2026-11-08')").run();
  assert.deepEqual((await night.nightly(bindings, NIGHT)).held, [], "2026-11-08 was counted already");
});

test("no more than a fixed few spikes are held a night", async () => {
  const bindings = env();
  for (let n = 0; n < night.SPIKE_MOST + 3; n += 1) {
    await many(bindings.DB, "2026-11-09", night.SPIKE_SMALLEST, `FLOOD${n}`, { device: null });
  }
  assert.equal((await night.nightly(bindings, NIGHT)).held.length, night.SPIKE_MOST);
});

test("the figures give the tool's downloads each day read, beside the devices converted then, from five", async () => {
  const bindings = env();
  const read = bindings.DB.db.prepare("INSERT INTO download_days (day, downloads) VALUES (?, ?)");
  read.run("2026-11-08", 3);
  read.run("2026-11-09", 9);
  const device = bindings.DB.db.prepare(`INSERT INTO devices (device_hash, device, first_day, last_day, first_result,
    converted_day, runs, conversions, back_to_sonoff, dev) VALUES (?, 'sonoff-s60', ?, ?, 'converted', ?, 1, 1, 0, 0)`);
  for (const [n, day] of [[1, "2026-11-08"], [SMALLEST, "2026-11-09"]]) {
    for (let at = 0; at < n; at += 1) {
      device.run(`${day}-${at}`, day, day, day);
    }
  }
  await night.nightly(bindings, NIGHT);
  assert.deepEqual(figures(bindings).downloads, {
    total: 9, day: "2026-11-09",
    days: [{ day: "2026-11-08", downloads: 3, converted: null }, { day: "2026-11-09", downloads: 9, converted: SMALLEST + 1 }],
  });
});

test("with no day read, the figures say no downloads", async () => {
  const bindings = env();
  await night.nightly(bindings, NIGHT);
  assert.deepEqual(figures(bindings).downloads, { total: null, day: null, days: [] });
});
