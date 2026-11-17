// api/reports.js: what /reports takes, keeps and turns away.
import assert from "node:assert/strict";
import { createHash, createHmac, randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { read } from "../api/counters.js";
import worker from "../api/index.js";
import { d1 } from "../api/local-d1.mjs";
import { work } from "../api/proof-of-work.js";
import * as api from "../api/reports.js";

const NOW = new Date("2026-11-09T15:30:00Z");
const BITS = 8;
const KEY = "a key for the tests alone";
work.bits = BITS;
const FIXTURE = JSON.parse(readFileSync(new URL("./fixtures/reports.json", import.meta.url), "utf8")).valid[0];

// The tool's work for a report's id (tools/autoflash/proof_of_work.py), made easy.
function proofFor(id, time = NOW.getTime() / 1000, bits = BITS) {
  for (let nonce = 0; ; nonce += 1) {
    const hash = createHash("sha256").update(`${Math.floor(time)}:report-${id}:${nonce}`).digest();
    if (hash.readUInt32BE(0) >>> (32 - bits) === 0) return `${Math.floor(time)}:${nonce}`;
  }
}

// Work that does not check out for `id`: the first nonce falling short. A
// fixed one would pass by chance, one time in 2 ** BITS, for a fresh id.
function wrongProofFor(id, time = NOW.getTime() / 1000) {
  for (let nonce = 0; ; nonce += 1) {
    const hash = createHash("sha256").update(`${Math.floor(time)}:report-${id}:${nonce}`).digest();
    if (hash.readUInt32BE(0) >>> (32 - BITS) !== 0) return `${Math.floor(time)}:${nonce}`;
  }
}

// A report as the tool makes it, with a fresh id and its proof.
function report(changes = {}, proofTime) {
  const id = changes.id ?? randomBytes(8).toString("hex");
  const made = { ...structuredClone(FIXTURE), ...changes, id };
  return { ...made, proof: changes.proof ?? proofFor(id, proofTime) };
}

const hashOf = (n) => createHash("sha256").update(`a device ${n}`).digest("hex");
const keyed = (hash) => createHmac("sha256", KEY).update(hash).digest("hex");

function env({ allowed = true, key = KEY } = {}) {
  return { DB: d1(), INGEST: "on", DEVICE_HASH_KEY_PERMANENT: key, SENDER_KEY: "a sender key for the tests",
    REPORT_LIMIT: { limit: async () => ({ success: allowed }) } };
}

function post(body, headers = {}, from = "192.0.2.9") {
  const bytes = Buffer.from(typeof body === "string" ? body : JSON.stringify(body));
  return new Request("https://api.firmwareliberator.com/v1/reports", {
    method: "POST", body: bytes,
    headers: { "Content-Type": "application/json", "Content-Length": String(bytes.byteLength),
      "CF-Connecting-IP": from, ...headers },
  });
}

const send = async (bindings, reports, now = NOW) => {
  const response = await api.reports(post({ reports }), bindings, now);
  return { status: response.status, ...(await response.json()) };
};
const rows = (bindings, sql, ...values) => bindings.DB.db.prepare(sql).all(...values).map((row) => ({ ...row }));

test("a report the tool makes is kept, on the day it came in, and nothing of when in the day", async () => {
  const bindings = env();
  const sent = report({ device_hash: hashOf(1) });
  assert.deepEqual(await send(bindings, [sent]),
    { status: 200, accepted: [sent.id], duplicate: [], rejected: [], later: [] });
  const [kept] = rows(bindings, "SELECT * FROM reports");
  assert.equal(kept.id, sent.id);
  assert.equal(kept.day, "2026-11-09");
  assert.equal(kept.model, "S60TPG");
  const stages = rows(bindings, `SELECT stage, seconds, busy, tries, wait, setup_network, power_cycles
    FROM report_stages WHERE id = ? ORDER BY stage`, sent.id);
  assert.deepEqual(stages, Object.entries(sent.stages).sort()
    .map(([stage, { seconds, busy, tries, wait, setup_network, power_cycles }]) =>
      ({ stage, seconds, busy, tries, wait, setup_network, power_cycles })), "a row a stage");
  assert.ok(!Object.values(kept).some((value) => String(value).includes("15:30")), "no time of day");
});

test("the device is kept as a keyed hash, never as the tool sent it", async () => {
  const bindings = env();
  await send(bindings, [report({ device_hash: hashOf(1) })]);
  const [{ device_hash: stored }] = rows(bindings, "SELECT device_hash FROM reports");
  assert.equal(stored, keyed(hashOf(1)));
  const everything = JSON.stringify(rows(bindings, "SELECT * FROM reports")) + JSON.stringify(rows(bindings, "SELECT * FROM devices"));
  assert.ok(!everything.includes(hashOf(1)));
});

test("one sent again, in the same request or a later one, is kept once", async () => {
  const bindings = env();
  const sent = report();
  const first = await send(bindings, [sent, sent]);
  assert.deepEqual([first.accepted, first.duplicate], [[sent.id], [sent.id]]);
  const again = await send(bindings, [sent]);
  assert.deepEqual([again.accepted, again.duplicate], [[], [sent.id]]);
  assert.equal(rows(bindings, "SELECT id FROM reports").length, 1);
  assert.deepEqual(await read(bindings.DB, "reports:2026-11-09"), { count: 1, bytes: 0 }, "counted once");
});

test("each report is held to the schema and its own work; the rest of the request is not", async () => {
  const bindings = env();
  const good = report();
  const cases = [
    [report({ result: "exploded" }), /result: not one of the allowed words/],
    [{ ...report(), email: "me@example.com" }, /email: not allowed/],
    [report({ id: "0123456789abcdef", proof: wrongProofFor("0123456789abcdef") }), /proof of work/],
    [report({ proof: proofFor("f".repeat(32)) }), /proof of work/, "work for another report"],
    [report({ stopped: { step: "D4", error: "other", text: "x".repeat(9000) } }), /larger than|too long/],
    ["not a report", /not object/],
    [null, /not object/],
  ];
  const answer = await send(bindings, [good, ...cases.map(([value]) => value)]);
  assert.deepEqual(answer.accepted, [good.id]);
  assert.equal(answer.rejected.length, cases.length);
  answer.rejected.forEach((rejected, index) => assert.match(rejected.why, cases[index][1], cases[index][2] ?? rejected.why));
  assert.equal(answer.rejected[0].id, cases[0][0].id, "named where it can be");
  assert.equal(answer.rejected.at(-1).id, null);
});

test("work made up to 31 days before is good, and older is not", async () => {
  const bindings = env();
  const kept = report({}, NOW.getTime() / 1000 - 30 * 86400);
  const stale = report({}, NOW.getTime() / 1000 - 32 * 86400);
  const answer = await send(bindings, [kept, stale]);
  assert.deepEqual(answer.accepted, [kept.id]);
  assert.deepEqual(answer.rejected.map((item) => item.id), [stale.id]);
});

test("the day's limit is never passed: what does not fit waits for another day", async () => {
  const bindings = env();
  bindings.DB.db.prepare("INSERT INTO counters (name, day, count, bytes) VALUES (?, ?, ?, 0)")
    .run("reports:2026-11-09", "2026-11-09", api.REPORTS_DAILY - 2);
  const sent = [report(), report(), report()];
  const answer = await send(bindings, sent);
  assert.deepEqual([answer.accepted, answer.later], [sent.slice(0, 2).map((r) => r.id), [sent[2].id]]);
  const tomorrow = await send(bindings, [sent[2]], new Date("2026-11-10T00:00:01Z"));
  assert.deepEqual(tomorrow.accepted, [sent[2].id], "sent again the next day, its work still good");
});

test("one device's reports are held to their own limit a day", async () => {
  const bindings = env();
  const hash = hashOf(2);
  const first = await send(bindings, Array.from({ length: api.DEVICE_DAILY - 1 }, () => report({ device_hash: hash })));
  assert.equal(first.accepted.length, api.DEVICE_DAILY - 1);
  const answer = await send(bindings, [report({ device_hash: hash }), report({ device_hash: hash }), report({ device_hash: hashOf(3) })]);
  assert.equal(answer.accepted.length, 2, "its last, and another device's");
  assert.match(answer.rejected[0].why, /more than 20 reports of one device in a day/);
});

test("a device's record: its first run, the runs to its first conversion, and what came after", async () => {
  const bindings = env();
  const hash = hashOf(4);
  const failed = { device_hash: hash, result: "failed", firmware: "tasmota",
    stopped: { step: "D4", error: "upload-cut", text: "the delivery stopped part way", crash_site: null } };
  await send(bindings, [report(failed), report({ ...failed, resumed: true })]);
  await send(bindings, [report({ device_hash: hash })]);   // converted, to Tasmota
  const later = new Date("2026-11-12T09:00:00Z");
  await send(bindings, [report({ device_hash: hash, firmware: "esphome", source: "tasmota" }, later.getTime() / 1000)], later);
  await send(bindings, [report({ device_hash: hash, firmware: "sonoff", source: "esphome" }, later.getTime() / 1000)], later);
  const [record] = rows(bindings, "SELECT * FROM devices");
  assert.deepEqual(record, {
    device_hash: keyed(hash), model: "S60TPG", device: "sonoff-s60", first_day: "2026-11-09", last_day: "2026-11-12",
    first_result: "failed", converted_day: "2026-11-09", runs: 5, runs_to_convert: 3, conversions: 2,
    back_to_sonoff: 1, firmware: "sonoff", stock_version: "1.2.0", place: 1, dev: 0,
  });
});

test("a device's record follows it across days, back to Sonoff's firmware too", async () => {
  const bindings = env();
  const hash = hashOf(5);
  const later = new Date("2026-11-20T09:00:00Z");
  await send(bindings, [report({ device_hash: hash })]);
  await send(bindings, [report({ device_hash: hash, firmware: "sonoff", source: "tasmota" }, later.getTime() / 1000)], later);
  await send(bindings, [report({ device_hash: hash, tag: "dev", firmware: "esphome", source: "sonoff" }, later.getTime() / 1000)],
    later);
  const [record] = rows(bindings, "SELECT first_day, last_day, first_result, runs, runs_to_convert, conversions, "
    + "back_to_sonoff, firmware, dev FROM devices");
  assert.deepEqual(record, { first_day: "2026-11-09", last_day: "2026-11-20", first_result: "converted", runs: 3,
    runs_to_convert: 1, conversions: 2, back_to_sonoff: 1, firmware: "esphome", dev: 1 });
});

test("a run the person stopped is kept, but makes no device's record", async () => {
  // Those count the runs that ran to an end: how many to convert one, how its first ended.
  const bindings = env();
  const hash = hashOf(6);
  await send(bindings, [report({ device_hash: hash, result: "cancelled", stopped: { step: "D1", error: "quit", text: "", crash_site: null } })]);
  assert.deepEqual(rows(bindings, "SELECT result, step, error, text FROM reports"),
    [{ result: "cancelled", step: "D1", error: "quit", text: "" }]);
  assert.equal(rows(bindings, "SELECT * FROM devices").length, 0);
  await send(bindings, [report({ device_hash: hash })]);
  const [record] = rows(bindings, "SELECT first_result, runs, runs_to_convert FROM devices");
  assert.deepEqual(record, { first_result: "converted", runs: 1, runs_to_convert: 1 });
});

test("a converted device keeps the number the first computer to convert it gave it", async () => {
  const bindings = env();
  const failed = { result: "failed", nth_device: null, stopped: { step: "D4", error: "upload-cut", text: "", crash_site: null } };
  await send(bindings, [report({ device_hash: hashOf(7), ...failed })]);
  await send(bindings, [report({ device_hash: hashOf(7), nth_device: 3 })]);
  await send(bindings, [report({ device_hash: hashOf(7), nth_device: 1, firmware: "esphome", source: "tasmota" })]);
  await send(bindings, [report({ device_hash: hashOf(8), nth_device: 2, firmware: "sonoff", source: "tasmota" })]);
  assert.deepEqual(rows(bindings, "SELECT device_hash, place FROM devices ORDER BY place"),
    [{ device_hash: keyed(hashOf(8)), place: null }, { device_hash: keyed(hashOf(7)), place: 3 }],
    "a conversion's alone, the first kept; going back to Sonoff's is none");
});

// Each report field is kept in a column of its own, so any of them can be
// counted with plain SQL: a field the schema gains without one would be
// taken, and thrown away. A field's column is named for it - its path, "."
// as "_", or a nested one's own name - and where it is kept elsewhere, these
// say so, and why.
const SCHEMA = JSON.parse(readFileSync(new URL("../api/report-schema.json", import.meta.url), "utf8"));
const SAMPLES = JSON.parse(readFileSync(new URL("./fixtures/reports.json", import.meta.url), "utf8"));
const KEPT = {
  run: {
    table: "reports",
    elsewhere: {
      format: "the schema's version, the same for every report kept",
      proof: "checked as the report comes in, then of no use",
      stages: "a row a stage, in report_stages",
      nth_device: "the device's place, in devices, given once",
    },
    own: ["day", "quarantined"],   // the API's, from no field
  },
  readout: {
    table: "readout_reports",
    elsewhere: {
      format: "the schema's version, the same for every report kept",
      proof: "checked as the report comes in, then of no use",
      kind: "readout_reports holds readouts' alone",
    },
    own: ["day", "quarantined"],
  },
  stage: { table: "report_stages", elsewhere: {}, own: ["id", "stage"] },
};

// Each field `rule` has, by its path: a nested one's as "os.arch".
const fields = (rule, path = []) => Object.entries(rule.properties).flatMap(([name, inner]) =>
  (inner.properties ? fields(inner, [...path, name]) : [[...path, name].join(".")]));
const valueAt = (report, field) => field.split(".").reduce((value, name) => value?.[name], report);
const columnsOf = (bindings, table) => rows(bindings, `SELECT name FROM pragma_table_info('${table}')`)
  .map((row) => row.name);

// Each field `rule` keeps in `kept.table`, and its column there: the field's
// column, or undefined where the table has none.
function columnsFor(bindings, rule, kept) {
  const columns = columnsOf(bindings, kept.table);
  return new Map(fields(rule).filter((field) => !(field in kept.elsewhere)).map((field) => [field,
    [field.replaceAll(".", "_"), field.split(".").at(-1)].find((name) => columns.includes(name))]));
}

test("each field of a report has a column of its own, and each column a field", () => {
  const bindings = env();
  const rules = { run: SCHEMA, readout: SCHEMA.readout, stage: SCHEMA.properties.stages.additionalProperties };
  for (const [what, rule] of Object.entries(rules)) {
    const kept = KEPT[what];
    const mapped = columnsFor(bindings, rule, kept);
    for (const [field, column] of mapped) {
      assert.ok(column, `a ${what} report's ${field} has no column in ${kept.table}: add one (website/api/migrations/), `
        + "and fill it in api/reports.js");
    }
    const filled = new Set([...mapped.values(), ...kept.own]);
    assert.deepEqual(columnsOf(bindings, kept.table).filter((column) => !filled.has(column)), [],
      `${kept.table}: a column no ${what} report's field fills`);
  }
});

test("each field of a report is kept, as the report gave it", async () => {
  // Every sample report the schema takes, kept: each field must have a value
  // in one of them, for this to see it is written.
  const bindings = env();
  // Each its own id, work and device.
  const fresh = ({ id, proof, ...sample }, n) => ({ ...sample, device_hash: hashOf(100 + n) });
  const sent = { run: SAMPLES.valid.map((sample, n) => report(fresh(sample, n))),
    readout: SAMPLES.readoutValid.map((sample, n) => readoutReport(fresh(sample, 50 + n))) };
  assert.equal((await send(bindings, [...sent.run, ...sent.readout])).accepted.length,
    sent.run.length + sent.readout.length);
  // As each is kept: true/false as 1/0, the device's hash keyed.
  const stored = (field, value) => (field === "device_hash" ? keyed(value)
    : typeof value === "boolean" ? Number(value) : value);
  const check = (what, rule, reports, keptRow) => {
    for (const [field, column] of columnsFor(bindings, rule, KEPT[what])) {
      const given = reports.filter((made) => valueAt(made, field) != null);
      assert.ok(given.length, `no sample ${what} report has a ${field}: give one a value (test/fixtures/reports.json)`);
      for (const made of given) {
        assert.equal(keptRow(made)[column], stored(field, valueAt(made, field)), `a ${what} report's ${field}, in ${column}`);
      }
    }
  };
  const byId = (table) => (made) => rows(bindings, `SELECT * FROM ${table} WHERE id = ?`, made.id)[0];
  check("run", SCHEMA, sent.run, byId("reports"));
  check("readout", SCHEMA.readout, sent.readout, byId("readout_reports"));
  const stages = sent.run.flatMap((made) => Object.entries(made.stages)
    .map(([stage, values]) => ({ ...values, id: made.id, stage })));
  check("stage", SCHEMA.properties.stages.additionalProperties, stages,
    (made) => rows(bindings, "SELECT * FROM report_stages WHERE id = ? AND stage = ?", made.id, made.stage)[0]);
});

test("a report with no device is kept, with no device record", async () => {
  const bindings = env();
  await send(bindings, [report({ device_hash: null })]);
  assert.equal(rows(bindings, "SELECT * FROM reports").length, 1);
  assert.equal(rows(bindings, "SELECT * FROM devices").length, 0);
});

test("a report written twice at the same moment counts once in its device's record", async () => {
  const bindings = env();
  const sent = report({ device_hash: hashOf(6) });
  const writes = [...api.writes(bindings.DB, sent, keyed(sent.device_hash), "2026-11-09"),
    ...api.writes(bindings.DB, sent, keyed(sent.device_hash), "2026-11-09")];
  await bindings.DB.batch(writes);
  assert.equal(rows(bindings, "SELECT runs FROM devices")[0].runs, 1);
});

test("a request of the most reports costs a fixed few queries", async () => {
  const bindings = env();
  let queries = 0;
  const db = bindings.DB;
  const counted = { ...db, prepare: (sql) => {
    const statement = db.prepare(sql);
    const wrap = (inner) => ({ ...inner, bind: (...values) => wrap(inner.bind(...values)),
      first: (...args) => { queries += 1; return inner.first(...args); },
      all: () => { queries += 1; return inner.all(); }, run: () => { queries += 1; return inner.run(); },
      inner });
    return wrap(statement);
  }, batch: (statements) => { queries += 1; return db.batch(statements.map((statement) => statement.inner)); } };
  const sent = Array.from({ length: api.MAX_REPORTS }, (_, n) => report({ device_hash: hashOf(100 + n) }));
  const answer = await api.reports(post({ reports: sent }), { ...bindings, DB: counted }, NOW);
  assert.equal((await answer.json()).accepted.length, api.MAX_REPORTS);
  assert.ok(queries <= 8, `${queries} queries`);
});

test("a write that fails gives back its places in the day's count", async () => {
  const bindings = env();
  bindings.DB.batch = async () => {
    throw new Error("D1 down");
  };
  await assert.rejects(send(bindings, [report(), report()]), /D1 down/);
  assert.deepEqual(await read(bindings.DB, "reports:2026-11-09"), { count: 0, bytes: 0 });
});

test("anything but reports as the tool sends them is turned away, before any is looked at", async () => {
  const many = Array.from({ length: api.MAX_REPORTS + 1 }, () => ({}));
  const cases = [
    [new Request("https://api.firmwareliberator.com/v1/reports"), 405],
    [post({ reports: [report()] }, { "Content-Type": "text/plain" }), 415],
    [post({ reports: [report()] }, { "Content-Length": "x" }), 411],
    [post({ reports: [report()] }, { "Content-Length": String(api.MAX_BODY + 1) }), 413],
    [post({ reports: [report()] }, { "Content-Length": "5" }), 400],
    [post("not json"), 400],
    [post([report()]), 400],
    [post({ reports: [] }), 400],
    [post({ reports: many }), 400],
    [post({ reports: [report()], also: 1 }), 400],
  ];
  for (const [request, status] of cases) {
    const bindings = env();
    assert.equal((await api.reports(request, bindings, NOW)).status, status, `${status}`);
    assert.equal(rows(bindings, "SELECT * FROM reports").length, 0);
  }
});

test("too many from one address wait a minute", async () => {
  assert.equal((await api.reports(post({ reports: [report()] }), env({ allowed: false }), NOW)).status, 429);
});

test("nothing is taken before the device hash's key and the senders' are set", async () => {
  for (const bindings of [{ ...env(), DEVICE_HASH_KEY_PERMANENT: undefined }, { ...env(), DEVICE_HASH_KEY_PERMANENT: "" },
    { ...env(), SENDER_KEY: undefined }]) {
    const response = await api.reports(post({ reports: [report()] }), bindings, NOW);
    assert.equal(response.status, 503);
    assert.equal(response.headers.get("Retry-After"), "3600");
    assert.equal(rows(bindings, "SELECT * FROM reports").length, 0);
  }
});

test("nothing is taken while the Worker's one switch is off", async () => {
  for (const ingest of ["off", undefined]) {
    const bindings = { ...env(), INGEST: ingest };
    const response = await worker.fetch(post({ reports: [report({}, Date.now() / 1000)] }), bindings);
    assert.equal(response.status, 503, String(ingest));
    assert.equal(rows(bindings, "SELECT * FROM reports").length, 0);
  }
});

test("the Worker sends /reports here, once its database has the reports' tables", async () => {
  const sent = report({}, Date.now() / 1000);
  const response = await worker.fetch(post({ reports: [sent] }), env());
  assert.deepEqual((await response.json()).accepted, [sent.id]);
  const config = readFileSync(new URL("../api/wrangler.jsonc", import.meta.url), "utf8");
  assert.match(config, /"INGEST": "on"/);
  assert.match(config, /"name": "REPORT_LIMIT"/);
});

test("the tool does the same work the Worker asks of a report", () => {
  const tool = readFileSync(new URL("../../tools/autoflash/report_data.py", import.meta.url), "utf8");
  assert.match(tool, /^DEVICE_PREFIX = "firmware-liberator device:"$/m);
  assert.equal(api.REPORT_PROOF_BITS, 24);
  const sender = readFileSync(new URL("../../tools/autoflash/report_upload.py", import.meta.url), "utf8");
  assert.match(sender, new RegExp(`^PROOF_BITS = ${api.REPORT_PROOF_BITS}$`, "m"));
});

test("each question /wanted is asked is counted by its model and day, within a limit", async () => {
  const bindings = env();
  for (const product of ["NSPanel", "nspanel", "BASICR4", "not a model!", "x".repeat(40), "SHIT-LAMP"]) {
    await api.countWanted(bindings, post({}), product, "2026-11-09");
  }
  assert.deepEqual(rows(bindings, "SELECT model, asked FROM wanted_days ORDER BY model"),
    [{ model: "BASICR4", asked: 1 }, { model: "NSPANEL", asked: 2 }], "no word no model has");
  bindings.DB.db.prepare("INSERT INTO counters (name, day, count, bytes) VALUES (?, ?, ?, 0)")
    .run("wanted:2026-11-10", "2026-11-10", api.WANTED_COUNTED);
  await api.countWanted(bindings, post({}), "NSPANEL", "2026-11-10");
  assert.equal(rows(bindings, "SELECT * FROM wanted_days WHERE day = '2026-11-10'").length, 0);
});

test("who named a model is kept as a keyed hash of it and their address, once a day, never the address", async () => {
  const bindings = env();
  await api.countWanted(bindings, post({}, {}, "198.51.100.23"), "NSPANEL", "2026-11-09");
  await api.countWanted(bindings, post({}, {}, "198.51.100.23"), "NSPANEL", "2026-11-09");
  await api.reports(post({ reports: [report({ model: "NSPANEL" }), report({ model: "BASICR4" })] }, {}, "198.51.100.23"),
    bindings, NOW);
  const kept = rows(bindings, "SELECT model, sender, network, day FROM model_senders ORDER BY model");
  assert.deepEqual(kept.map((row) => [row.model, row.day]), [["BASICR4", "2026-11-09"], ["NSPANEL", "2026-11-09"]],
    "one a model and day, however often given");
  assert.match(kept[0].sender, /^[0-9a-f]{32}$/);
  assert.notEqual(kept[0].sender, kept[1].sender, "nothing ties one sender's models together");
  assert.deepEqual(kept.map((row) => row.network), [null, null], "no network given");
  const onNetwork = post({ reports: [report({ model: "MINIR4" })] }, {}, "198.51.100.23");
  Object.defineProperty(onNetwork, "cf", { value: { asn: 64500 } });
  await api.reports(onNetwork, bindings, NOW);
  const [network] = rows(bindings, "SELECT network FROM model_senders WHERE model = 'MINIR4'");
  assert.match(network.network, /^[0-9a-f]{32}$/);
  assert.ok(!JSON.stringify(rows(bindings, "SELECT * FROM model_senders")).includes("64500"), "never the ASN itself");
  assert.ok(!JSON.stringify(rows(bindings, "SELECT * FROM model_senders")).includes("198.51.100"));
});

test("a model named from a big cloud's network is named from none", async () => {
  const bindings = env();
  const fromCloud = post({ reports: [report({ model: "MINIR4" })] }, {}, "198.51.100.23");
  Object.defineProperty(fromCloud, "cf", { value: { asn: 16509 } });
  await api.reports(fromCloud, bindings, NOW);
  assert.deepEqual(rows(bindings, "SELECT network FROM model_senders"), [{ network: null }]);
});

test("a report held for review, or the maintainer's own, vouches for no model", async () => {
  const bindings = env();
  await send(bindings, [report({ model: "NEWDEV", tag: "dev" }),
    report({ model: "NEWDEV", result: "failed", stopped: { step: "D4", error: "upload-cut", text: "buy cheap watches", crash_site: null } })]);
  assert.deepEqual(rows(bindings, "SELECT * FROM model_senders"), []);
});

test("a report no run would write is answered as accepted, held for review, and counted nowhere", async () => {
  const bindings = env();
  const junk = report({ device_hash: hashOf(9), result: "failed",
    stopped: { step: "D4", error: "upload-cut", text: "buy cheap watches at example dot com", crash_site: null } });
  const answer = await send(bindings, [junk]);
  assert.deepEqual(answer.accepted, [junk.id], "its sender learns nothing");
  assert.deepEqual(rows(bindings, "SELECT quarantined FROM reports"), [{ quarantined: 1 }]);
  assert.deepEqual(rows(bindings, "SELECT id, day, why FROM held_reports"),
    [{ id: junk.id, day: "2026-11-09", why: "words that read as none of the tool's messages" }]);
  assert.equal(rows(bindings, "SELECT * FROM devices").length, 0, "no device's record");
});

test("a report the tool writes is never held", async () => {
  const bindings = env();
  await send(bindings, [report({ device_hash: hashOf(10) }), report({ result: "failed",
    stopped: { step: "D4", error: "upload-cut", text: "the delivery stopped part way: the device fetched 1 of 2", crash_site: null } })]);
  assert.deepEqual(rows(bindings, "SELECT quarantined FROM reports"), [{ quarantined: 0 }, { quarantined: 0 }]);
  assert.equal(rows(bindings, "SELECT * FROM held_reports").length, 0);
});

test("one sender's reports are held to their own limit a day, the rest left for later", async () => {
  const bindings = env();
  const sendFrom = async (from, reports) => (await api.reports(post({ reports }, {}, from), bindings, NOW)).json();
  // No device, so the limit for one device does not come first.
  const loose = () => report({ device_hash: null });
  const first = await sendFrom("2001:db8:1:2::10", Array.from({ length: api.SENDER_REPORTS_DAILY - 2 }, loose));
  assert.equal(first.accepted.length, api.SENDER_REPORTS_DAILY - 2);
  const sameHome = Array.from({ length: 4 }, loose);
  const answer = await sendFrom("2001:db8:1:ffff::99", sameHome);   // the same /48
  assert.deepEqual([answer.accepted.length, answer.later], [2, sameHome.slice(2).map((r) => r.id)]);
  assert.equal((await sendFrom("192.0.2.77", [loose()])).accepted.length, 1, "another sender is not held by it");
});

test("the per-minute limit counts an IPv6 sender by its /48, as every other limit does", async () => {
  const keys = [];
  const bindings = { ...env(), REPORT_LIMIT: { limit: async ({ key }) => { keys.push(key); return { success: true }; } } };
  for (const from of ["2001:db8:1:2::10", "2001:db8:1:ffff::99", "192.0.2.9"]) {
    await api.reports(post({ reports: [report()] }, {}, from), bindings, NOW);
  }
  assert.deepEqual(keys, ["2001:0db8:0001::/48", "2001:0db8:0001::/48", "192.0.2.9"]);
});

test("a sender is counted under a keyed hash, never its address", async () => {
  const bindings = env();
  await api.reports(post({ reports: [report()] }, {}, "198.51.100.23"), bindings, NOW);
  const names = rows(bindings, "SELECT name FROM counters").map((row) => row.name);
  assert.ok(names.some((name) => /^report-sender:[0-9a-f]{32}:2026-11-09$/.test(name)), names.join());
  assert.ok(!JSON.stringify(rows(bindings, "SELECT * FROM counters")).includes("198.51.100.23"));
});

test("what everyone's day has no room for is given back to the sender's", async () => {
  const bindings = env();
  bindings.DB.db.prepare("INSERT INTO counters (name, day, count, bytes) VALUES (?, ?, ?, 0)")
    .run("reports:2026-11-09", "2026-11-09", api.REPORTS_DAILY - 1);
  await api.reports(post({ reports: [report(), report(), report()] }), bindings, NOW);
  const [sender] = rows(bindings, "SELECT count FROM counters WHERE name LIKE 'report-sender:%'");
  assert.equal(sender.count, 1, "only the one kept");
});

// A readout's report, each device's as its readout ends ("kind": "readout").
const READOUT = JSON.parse(readFileSync(new URL("./fixtures/reports.json", import.meta.url), "utf8")).readoutValid[0];
function readoutReport(changes = {}) {
  const id = changes.id ?? randomBytes(8).toString("hex");
  return { ...structuredClone(READOUT), ...changes, id, proof: changes.proof ?? proofFor(id) };
}

test("a readout's report is kept apart from the runs', its device a keyed hash, and makes no device's record", async () => {
  const bindings = env();
  const sent = readoutReport({ device_hash: hashOf(7) });
  assert.deepEqual(await send(bindings, [sent]),
    { status: 200, accepted: [sent.id], duplicate: [], rejected: [], later: [] });
  const [kept] = rows(bindings, "SELECT * FROM readout_reports");
  assert.deepEqual([kept.id, kept.day, kept.result, kept.model, kept.stock_model, kept.whole, kept.converted_here],
    [sent.id, "2026-11-09", "accepted", "BASICR4", "SN-ESP32C3-BASICR4-01", 1, 3]);
  assert.equal(kept.os_arch, sent.os.arch);
  assert.equal(kept.device_hash, keyed(hashOf(7)));
  assert.deepEqual(rows(bindings, "SELECT id FROM reports"), [], "no run's report");
  assert.deepEqual(rows(bindings, "SELECT * FROM devices"), [], "a readout counts no run of its device");
  assert.ok(!Object.values(kept).some((value) => String(value).includes("15:30")), "no time of day");
});

test("a readout's report and a run's share their ids, the day's counts and a device's limit", async () => {
  const bindings = env();
  const sent = readoutReport();
  await send(bindings, [sent]);
  const again = await send(bindings, [sent, report({ id: sent.id })]);
  assert.deepEqual([again.accepted, again.duplicate], [[], [sent.id, sent.id]]);
  assert.deepEqual(await read(bindings.DB, "reports:2026-11-09"), { count: 1, bytes: 0 });
  const hash = hashOf(8);
  const many = await send(bindings, [
    ...Array.from({ length: api.DEVICE_DAILY - 1 }, () => report({ device_hash: hash })),
    readoutReport({ device_hash: hash }), readoutReport({ device_hash: hash })]);
  assert.equal(many.accepted.length, api.DEVICE_DAILY);
  assert.match(many.rejected[0].why, /reports of one device in a day/);
});

test("a readout's report is held to the readout's schema, not a run's", async () => {
  const bindings = env();
  const runs = await send(bindings, [readoutReport({ stopped: null })]);
  assert.equal(runs.rejected.length, 1, "a run's field");
  const named = await send(bindings, [readoutReport({ name: "Kitchen" })]);
  assert.equal(named.rejected.length, 1, "nothing of the device's name in the account");
  const asRun = await send(bindings, [report({ kind: "readout" })]);
  assert.equal(asRun.rejected.length, 1, "a run's report called a readout's");
});

test("a readout's report with words no report would hold is held for review, and counted nowhere", async () => {
  const bindings = env();
  const words = JSON.parse(readFileSync(new URL("../api/held-words.json", import.meta.url), "utf8")).words;
  const word = words.find((each) => /^[a-z]{4,}$/.test(each)).toUpperCase();   // a model's shape
  const sent = readoutReport({ model: word });
  const answer = await send(bindings, [sent]);
  assert.deepEqual(answer.accepted, [sent.id]);
  assert.deepEqual(rows(bindings, "SELECT quarantined FROM readout_reports"), [{ quarantined: 1 }]);
  assert.deepEqual(rows(bindings, "SELECT id FROM held_reports"), [{ id: sent.id }]);
});
