// /reports: the reports the tool sends at the end of each run, unless told not
// to (tools/autoflash/report_data.py makes them), kept for the project's own
// figures: how many devices it has converted, and how reliably, by device.
// A readout sends one too for each device as its readout ends ("kind":
// "readout", held to the schema's "readout"), kept apart (readout_reports):
// how readouts end, and how many devices the computers making them converted.
//
// POST {"reports": [<report>, ...]} (application/json): the run's report, with
// any the tool could not send before. Each is held to report-schema.json, and
// carries its own proof of work, made for its id before the report existed,
// so the tool does the work while the run goes on. The answer is always
// {"accepted", "duplicate", "rejected": [{"id", "why"}], "later"}: the tool
// forgets the first three, and sends the rest again another time.
//
// Kept, and how floods are bounded:
// - Nothing of the sender in what is kept: no address (the rate limiter holds
//   it a minute; Cloudflare's log of the request, seven days, wrangler.jsonc),
//   no time of day; a report's day, as a UTC date. For the
//   figures' model names alone, a keyed hash of the address and the model it
//   names (namedBy), which ties the sender to nothing else, kept until the
//   model is vouched for (nightly.js) or VOUCH_KEEP_DAYS pass.
// - The device as a keyed hash (DEVICE_HASH_KEY_PERMANENT, a Worker secret) of the hash the
//   tool sends of its MAC, so the stored value leads back to no device
//   without the key, which never leaves Cloudflare.
// - At most REPORTS_DAILY a day from everyone, SENDER_REPORTS_DAILY from one
//   sender (sender.js: an IPv6 address by its /48), counted under a keyed
//   hash (SENDER_KEY) of its address, never the address, and DEVICE_DAILY a
//   day for one device; past the first two, the rest wait for another day
//   ("later"), so no one computer can flood the figures.
// - Nothing is taken (503) while INGEST is not "on": index.js checks it for
//   every path, before this.
// - One no run of the tool's would write (review.js) is held for review:
//   answered as accepted, kept quarantined, counted nowhere.
// - A request costs a fixed few database queries however many reports it
//   carries (D1 allows 1,000 a request on Workers Paid, 50 on the free plan): the duplicates, the
//   devices' counts for the day, the day's count, and one batch to write.
import { answer } from "./answer.js";
import { release, reserve, reserveUpTo } from "./counters.js";
import { hex, proven, reportSubject, work } from "./proof-of-work.js";
import schema from "./report-schema.json" with { type: "json" };
import { validate } from "./report-schema.js";
import { heldFor, holdsHeldWords } from "./review.js";
import { countedNetwork } from "./networks.js";
import { senderAddress } from "./sender.js";

export const MAX_BODY = 1024 * 1024;
export const MAX_REPORTS = 99;
export const REPORTS_DAILY = 1000;
export const DEVICE_DAILY = 20;
export const SENDER_REPORTS_DAILY = 100;
export const REPORT_PROOF_BITS = 24;
// A report is sent when its run ends, or kept and sent within 30 days.
export const REPORT_PROOF_AGE = 31 * 86400;
// How many of /wanted's questions a day are counted, at most.
export const WANTED_COUNTED = 2000;
const MODEL = new RegExp(schema.properties.model.pattern);

// A report's ID, as the schema has it (report_upload.REPORT_ID_BYTES).
export const ID = new RegExp(schema.properties.id.pattern);

// Who named `model`, as model_senders keeps it: a keyed hash (SENDER_KEY) of
// the model and the sender's address, one a model, so nothing ties one
// sender's names together; and of the model and the network it came from
// (Cloudflare's ASN), null where none is given or it is a host's or a VPN's
// (networks.js). A model's name is shown in the figures only once several
// have given it, from several networks, over several days (nightly.js).
export async function namedBy(env, request, model) {
  const asn = await countedNetwork(env, request);
  return {
    sender: (await keyed(env.SENDER_KEY, `model|${model}|${senderAddress(request)}`)).slice(0, 32),
    network: asn !== null ? (await keyed(env.SENDER_KEY, `model|${model}|network|${asn}`)).slice(0, 32) : null,
  };
}
const NAMED = "INSERT OR IGNORE INTO model_senders (model, sender, network, day) VALUES (?, ?, ?, ?)";
const naming = async (env, request, model, day) => {
  const { sender, network } = await namedBy(env, request, model);
  return env.DB.prepare(NAMED).bind(model, sender, network, day);
};
const reportsOn = (day) => `reports:${day}`;

// The device as stored: HMAC-SHA256 of the tool's hash under DEVICE_HASH_KEY_PERMANENT.
const keys = new Map();
async function keyed(secret, hash) {
  if (!keys.has(secret)) {
    keys.set(secret, await crypto.subtle.importKey("raw", new TextEncoder().encode(secret),
      { name: "HMAC", hash: "SHA-256" }, false, ["sign"]));
  }
  return hex(new Uint8Array(await crypto.subtle.sign("HMAC", keys.get(secret), new TextEncoder().encode(hash))));
}

// Whether `report` is a readout's: held to the schema's "readout", and kept apart.
export const isReadout = (report) => report?.kind === "readout";

// Why `report` is turned away, or null.
async function problem(report, now, bits) {
  if (new TextEncoder().encode(JSON.stringify(report)).length > schema.maxBytes) {
    return `larger than ${schema.maxBytes} bytes`;
  }
  const [first] = validate(isReadout(report) ? schema.readout : schema, report);
  if (first) {
    return first;
  }
  if (!(await proven(report.proof, reportSubject(report.id), now.getTime() / 1000, bits, REPORT_PROOF_AGE))) {
    return "its proof of work does not check out";
  }
  return null;
}

const flag = (value) => (value === null ? null : Number(value));

// Each column of `reports` and, in order, the report's value for it.
const COLUMNS = (report, hash, day, held) => {
  const stopped = report.stopped ?? {};
  return {
    id: report.id, day, tag: report.tag, tool: report.tool, kind: report.kind, result: report.result,
    step: stopped.step ?? null, error: stopped.error ?? null, text: stopped.text ?? null, family: report.family,
    device: report.device, model: report.model, stock_version: report.stock_version, device_hash: hash, firmware: report.firmware,
    source: report.source, backup: report.backup, identity: report.identity, os_family: report.os.family,
    os_version: report.os.version, os_arch: report.os.arch, python_version: report.python.version,
    python_source: report.python.source, seconds: report.seconds, resumed: Number(report.resumed),
    signal: report.signal, wifi_typed: flag(report.wifi_typed), setup_network: flag(report.setup_network),
    power_cycled: flag(report.power_cycled), checks: report.checks, meter: report.meter,
    crash_site: stopped.crash_site ?? null, chip_revision: report.chip.revision,
    chip_flash_id: report.chip.flash_id, chip_flash_mb: report.chip.flash_mb, connection: report.computer.connection, vpn: flag(report.computer.vpn),
    networks: report.computer.networks, security_note: Number(report.computer.security_note),
    firewall: report.computer.firewall, admin: Number(report.computer.admin), ewelink_region: report.ewelink_region,
    ctrl_c_carried_on: report.ctrl_c_carried_on, launcher: report.launcher, install: report.install,
    begun_on: report.begun_on, back_after_power_cycle: report.back_after_power_cycle,
    firmware_name: report.firmware_name, firmware_name_expected: flag(report.firmware_name_expected),
    quarantined: Number(held !== null),
  };
};
const NAMES = Object.keys(COLUMNS({ os: {}, python: {}, chip: {}, computer: {} }, null, null, null));
const INSERT = `INSERT OR IGNORE INTO reports (${NAMES.join(", ")}) VALUES (${NAMES.map(() => "?").join(", ")})`;
// The report's stages, one row each, from its `stages` as JSON.
const STAGES = `INSERT OR IGNORE INTO report_stages (id, stage, seconds, busy, tries, wait, setup_network, power_cycles)
  SELECT ?1, key, json_extract(value, '$.seconds'), json_extract(value, '$.busy'), json_extract(value, '$.tries'),
    json_extract(value, '$.wait'), json_extract(value, '$.setup_network'), json_extract(value, '$.power_cycles')
  FROM json_each(?2)`;
const READOUT = `INSERT OR IGNORE INTO readout_reports (id, day, tag, tool, result, error, family, device, model,
  stock_model, stock_version, whole, device_hash, os_family, os_version, os_arch, python_version, python_source, seconds,
  converted_here, quarantined)
  VALUES (${Array(21).fill("?").join(", ")})`;
const HOLD = "INSERT OR IGNORE INTO held_reports (id, day, why) VALUES (?, ?, ?)";

// The device's record, brought up to date by one report: only where that
// report was written just before (changes(), in the same transaction), so a
// report sent twice at the same moment counts once. Its place, a converted
// device's number on the computer that first converted it: the first given
// is kept.
const DEVICE = `INSERT INTO devices (device_hash, model, device, first_day, last_day, first_result, converted_day, runs,
    runs_to_convert, conversions, back_to_sonoff, firmware, stock_version, place, dev)
  SELECT ?1, ?2, ?3, ?4, ?4, ?5, CASE WHEN ?6 THEN ?4 END, 1, CASE WHEN ?6 THEN 1 END, ?6, ?7,
    CASE WHEN ?6 OR ?7 THEN ?8 END, ?9, CASE WHEN ?6 THEN ?11 END, ?10
  WHERE changes() = 1
  ON CONFLICT (device_hash) DO UPDATE SET
    model = coalesce(excluded.model, devices.model),
    device = coalesce(excluded.device, devices.device),
    last_day = max(devices.last_day, excluded.last_day),
    runs = devices.runs + 1,
    runs_to_convert = coalesce(devices.runs_to_convert, CASE WHEN excluded.conversions = 1 THEN devices.runs + 1 END),
    converted_day = coalesce(devices.converted_day, excluded.converted_day),
    conversions = devices.conversions + excluded.conversions,
    back_to_sonoff = devices.back_to_sonoff + excluded.back_to_sonoff,
    firmware = coalesce(excluded.firmware, devices.firmware),
    stock_version = coalesce(devices.stock_version, excluded.stock_version),
    place = coalesce(devices.place, excluded.place),
    dev = max(devices.dev, excluded.dev)`;

// The statements that write one report, with its device's record, or for
// one held (`held`: why), only it, quarantined, and why it is held.
export function writes(db, report, hash, day, held = null) {
  if (isReadout(report)) {
    // A readout's report makes no device's record: those count runs.
    const statement = db.prepare(READOUT).bind(
      report.id, day, report.tag, report.tool, report.result, report.error, report.family, report.device,
      report.model, report.stock_model, report.stock_version, flag(report.whole), hash, report.os.family, report.os.version,
      report.os.arch, report.python.version, report.python.source, report.seconds, report.converted_here,
      Number(held !== null));
    return held === null ? [statement] : [statement, db.prepare(HOLD).bind(report.id, day, held)];
  }
  const statements = [db.prepare(INSERT).bind(...Object.values(COLUMNS(report, hash, day, held)))];
  // A run the person stopped makes no device's record: those count the runs
  // that ran to an end (how many to convert one, how its first ended).
  if (held === null && hash !== null && report.result !== "cancelled") {
    // A conversion is to another firmware than Sonoff's; back to Sonoff's is counted apart.
    const converted = report.result === "converted" && report.firmware !== "sonoff";
    const backToSonoff = report.result === "converted" && report.firmware === "sonoff";
    // Just after the report's own INSERT: DEVICE reads its changes().
    statements.push(db.prepare(DEVICE).bind(hash, report.model, report.device, day, report.result, Number(converted),
      Number(backToSonoff), report.firmware, report.stock_version, Number(report.tag === "dev"), report.nth_device));
  }
  statements.push(db.prepare(STAGES).bind(report.id, JSON.stringify(report.stages)));
  return held === null ? statements : [...statements, db.prepare(HOLD).bind(report.id, day, held)];
}

export async function reports(request, env, now = new Date()) {
  if (request.method !== "POST") {
    return answer(405, { error: "reports are sent here by the tool, with POST" }, { Allow: "POST" });
  }
  if (!env.DEVICE_HASH_KEY_PERMANENT || !env.SENDER_KEY) {
    return answer(503, { error: "reports are not being taken just now: try again later" }, { "Retry-After": "3600" });
  }
  if (request.headers.get("Content-Type") !== "application/json") {
    return answer(415, { error: "reports are application/json" });
  }
  const declared = Number(request.headers.get("Content-Length"));
  if (!Number.isInteger(declared) || declared <= 0) {
    return answer(411, { error: "reports say their length" });
  }
  if (declared > MAX_BODY) {
    return answer(413, { error: `reports are at most ${MAX_BODY} bytes at once` });
  }
  // By the sender as every limit counts them (sender.js), so one IPv6
  // connection cannot step round it address by address.
  const { success } = await env.REPORT_LIMIT.limit({ key: senderAddress(request) });
  if (!success) {
    return answer(429, { error: "too many at once: try again in a minute" });
  }
  const body = await request.arrayBuffer();
  if (body.byteLength !== declared) {
    return answer(400, { error: "those are not whole reports" });
  }
  let sent = null;
  try {
    sent = JSON.parse(new TextDecoder().decode(body));
  } catch {
    // below
  }
  const list = sent?.reports;
  if (sent === null || typeof sent !== "object" || Array.isArray(sent) || Object.keys(sent).join() !== "reports"
      || !Array.isArray(list) || list.length < 1 || list.length > MAX_REPORTS) {
    return answer(400, { error: `that is not reports this tool sends: {"reports": [1 to ${MAX_REPORTS}]}` });
  }
  const day = now.toISOString().slice(0, 10);
  const outcome = { accepted: [], duplicate: [], rejected: [], later: [] };
  const bits = work.bits ?? REPORT_PROOF_BITS;
  const valid = [];
  for (const report of list) {
    const id = typeof report?.id === "string" && ID.test(report.id) ? report.id : null;
    const why = await problem(report, now, bits);
    if (why) {
      outcome.rejected.push({ id, why });
    } else if (valid.some((other) => other.id === id)) {
      outcome.duplicate.push(id);
    } else {
      valid.push(report);
    }
  }
  if (!valid.length) {
    return answer(200, outcome);
  }
  const marks = valid.map(() => "?").join(", ");
  const ids = valid.map((report) => report.id);
  const { results: held } = await env.DB.prepare(
    `SELECT id FROM reports WHERE id IN (${marks}) UNION ALL SELECT id FROM readout_reports WHERE id IN (${marks})`)
    .bind(...ids, ...ids).all();
  const known = new Set(held.map((row) => row.id));
  outcome.duplicate.push(...valid.filter((report) => known.has(report.id)).map((report) => report.id));
  const fresh = valid.filter((report) => !known.has(report.id));
  const hashes = await Promise.all(fresh.map((report) =>
    (report.device_hash ? keyed(env.DEVICE_HASH_KEY_PERMANENT, report.device_hash) : null)));
  // Each device's reports today, a run's or a readout's, and those this
  // request adds, held to DEVICE_DAILY.
  const named = [...new Set(hashes.filter(Boolean))];
  const today = new Map();
  if (named.length) {
    const some = named.map(() => "?").join(", ");
    const { results } = await env.DB.prepare(
      `SELECT device_hash, COUNT(*) AS n FROM (
         SELECT device_hash FROM reports WHERE day = ? AND device_hash IN (${some})
         UNION ALL SELECT device_hash FROM readout_reports WHERE day = ? AND device_hash IN (${some}))
       GROUP BY device_hash`).bind(day, ...named, day, ...named).all();
    for (const row of results) {
      today.set(row.device_hash, row.n);
    }
  }
  const taking = [];
  fresh.forEach((report, index) => {
    const hash = hashes[index];
    if (hash !== null && (today.get(hash) ?? 0) >= DEVICE_DAILY) {
      outcome.rejected.push({ id: report.id, why: `more than ${DEVICE_DAILY} reports of one device in a day` });
      return;
    }
    if (hash !== null) {
      today.set(hash, (today.get(hash) ?? 0) + 1);
    }
    taking.push([report, hash]);
  });
  // The sender's day first, then everyone's: what everyone's has no room
  // for is given back to the sender's.
  const sender = `report-sender:${(await keyed(env.SENDER_KEY, `reports|${senderAddress(request)}`)).slice(0, 32)}:${day}`;
  const theirs = taking.length ? await reserveUpTo(env.DB, sender, day, taking.length, SENDER_REPORTS_DAILY) : 0;
  const granted = theirs ? await reserveUpTo(env.DB, reportsOn(day), day, theirs, REPORTS_DAILY) : 0;
  if (granted < theirs) {
    await release(env.DB, sender, 0, theirs - granted);
  }
  outcome.later.push(...taking.slice(granted).map(([report]) => report.id));
  const kept = taking.slice(0, granted).map(([report, hash]) => [report, hash, heldFor(report)]);
  // The models the reports counted name, each noted once as this sender's.
  const models = [...new Set(kept.filter(([report, , held]) => held === null && report.tag === "user" && report.model)
    .map(([report]) => report.model))];
  const namings = await Promise.all(models.map((model) => naming(env, request, model, day)));
  if (kept.length) {
    try {
      await env.DB.batch([...kept.flatMap(([report, hash, held]) => writes(env.DB, report, hash, day, held)), ...namings]);
    } catch (error) {
      await release(env.DB, reportsOn(day), 0, kept.length);
      await release(env.DB, sender, 0, kept.length);
      throw error;
    }
    outcome.accepted.push(...kept.map(([report]) => report.id));
  }
  return answer(200, outcome);
}

// One of /wanted's questions, counted for the figures by its product model
// and day (wanted_days): the devices people would read out for the tool to
// learn - each question, and each device once a day where the tool names it
// (its hash, as a report's, keyed as one is), whose own record says when it
// was first and last asked of and how often (wanted_devices). Only a model's
// shape is counted, none with a word no model has (review.js), and
// WANTED_COUNTED a day, so a flood of made-up names cannot grow the tables
// without end; and its sender noted (namedBy), so a name is shown only once
// several have asked of it.
export async function countWanted(env, request, product, day, deviceHash = null) {
  const model = String(product).trim().toUpperCase();
  if (!MODEL.test(model) || holdsHeldWords(model)
      || !(await reserve(env.DB, `wanted:${day}`, day, { most: WANTED_COUNTED }))) {
    return;
  }
  const statements = [env.DB.prepare(`INSERT INTO wanted_days (day, model, asked, devices) VALUES (?, ?, 1, 0)
    ON CONFLICT (day, model) DO UPDATE SET asked = asked + 1`).bind(day, model)];
  if (deviceHash && env.DEVICE_HASH_KEY_PERMANENT) {
    const device = await keyed(env.DEVICE_HASH_KEY_PERMANENT, deviceHash);
    // Counted once a day, before its record says it was asked of today.
    statements.push(env.DB.prepare(`UPDATE wanted_days SET devices = devices + 1 WHERE day = ?1 AND model = ?2
      AND NOT EXISTS (SELECT 1 FROM wanted_devices WHERE device_hash = ?3 AND last_day = ?1)`).bind(day, model, device),
    env.DB.prepare(`INSERT INTO wanted_devices (device_hash, model, first_day, last_day, asks) VALUES (?3, ?2, ?1, ?1, 1)
      ON CONFLICT (device_hash) DO UPDATE SET model = excluded.model, last_day = excluded.last_day,
        asks = wanted_devices.asks + 1`).bind(day, model, device));
  }
  if (env.SENDER_KEY) {
    statements.push(await naming(env, request, model, day));
  }
  await env.DB.batch(statements);
}
