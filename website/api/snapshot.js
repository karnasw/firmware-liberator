// The public figures the nightly job writes (stats.json, CC BY 4.0), which
// the website's /stats page is built from: totals only, never a report.
// - The maintainer's own testing (tag "dev", and devices it has seen),
//   quarantined reports, and devices with a report held for review are left out.
// - No figure stands for fewer than SMALLEST: a count below it is null, a
//   rate is given only over SMALLEST or more, and a row of a list (a device,
//   a model, a Sonoff version, a tool version) under it is left out, so no one
//   report or device can be picked out.
// - The devices the tool converts are counted by their kind (the reports'
//   `device`, one of the tool's own keys), so every one is counted, whether
//   eWeLink named its model or not: how its conversions go is the kind's, and
//   its model (an S60's region) is only a breakdown within it. The devices it
//   does not convert yet have no kind, and are counted by model ("wanted", and
//   each model's months).
// - Devices a computer converted (perComputer): from each converted device's
//   number on the computer that first converted it (devices.place), the
//   computers that converted at least k devices are the devices numbered k -
//   or, as a computer whose earlier conversions sent no report (--no-report)
//   is at k all the same, the most numbered k or more - so those that
//   converted exactly k are that less the same for k + 1. Never more than
//   there are; fewer where reports are missing. A computer is the tool's
//   records on it, never named: a fresh download counts again.
// - No model's name is shown before it is vouched for (vouched_models:
//   VOUCH_SENDERS senders from VOUCH_NETWORKS networks over VOUCH_DAYS days,
//   nightly.js), so no made-up name can be put on the page by one sender,
//   however many reports it makes.
// - A run's success is a conversion, over conversions, failures and crashes:
//   one the person declined, with nothing to do, or that met a device the tool
//   does not convert yet (counted as "unsupported", and in "wanted"), counts
//   apart, as does one the person stopped ("cancelled", with the stages
//   people stop at); the device figures are of devices the tool converts.
// - How far each device's testing on the emulators has got (`testing`): the
//   maintainer's own runs, never a person's, so nothing in them is anyone's
//   to keep back - but only from checkouts with no changes beside their
//   commit: version by version of the tool, newest first (`versions`), the
//   share of its suites' scenarios passed on that version, of its first
//   tries, the day it was last tested and the firmware it ran; and the same
//   of its newest version at the top. A device in testing shows here before
//   anyone has converted one.
// - The tool's downloads (`downloads`), as GitHub counted them each night it
//   was read (downloads.js), beside the devices converted by that day: the
//   downloads whole, as GitHub shows them to anyone, the devices as above.
import { LATEST_MIGRATION } from "./database.js";
import { downloadDays } from "./downloads.js";
import { deviceTesting, newerFirst } from "./emulator.js";
import { monthsTo, popularity } from "./popularity.js";

export const FORMAT = 1;
export const SMALLEST = 5;
// Months of each model's devices given (modelMonths), the latest last.
export const TREND_MONTHS = 24;
// Months of run times given, the latest first.
export const TIME_MONTHS = 12;
const NOT_FAILURES = "('declined', 'nothing-to-do', 'unsupported-device')";
// A device none of whose reports is held for review: a spike's reports made
// its record before they were held (nightly.js).
const NOT_HELD = `device_hash NOT IN (SELECT reports.device_hash FROM held_reports
  JOIN reports ON reports.id = held_reports.id WHERE reports.device_hash IS NOT NULL)`;

const VOUCHED = "model IN (SELECT model FROM vouched_models)";

const count = (n) => (Number.isInteger(n) && n >= SMALLEST ? n : null);
// A percentage to one decimal, of `whole` SMALLEST or more.
const rate = (part, whole) => (whole >= SMALLEST ? Math.round((1000 * part) / whole) / 10 : null);

// sum(n) of monthly_totals' public rows as runs: what ended how.
const OUTCOMES = `sum(n) AS runs,
  sum(CASE WHEN result = 'converted' THEN n ELSE 0 END) AS converted,
  sum(CASE WHEN result = 'failed' AND error NOT IN ${NOT_FAILURES} THEN n ELSE 0 END) AS failed,
  sum(CASE WHEN result = 'crashed' THEN n ELSE 0 END) AS crashed,
  sum(CASE WHEN result = 'failed' AND error IN ('declined', 'nothing-to-do') THEN n ELSE 0 END) AS declined,
  sum(CASE WHEN result = 'failed' AND error = 'unsupported-device' THEN n ELSE 0 END) AS unsupported,
  sum(CASE WHEN result = 'updated' THEN n ELSE 0 END) AS updated,
  sum(CASE WHEN result = 'put-back' THEN n ELSE 0 END) AS put_back,
  sum(CASE WHEN result = 'cancelled' THEN n ELSE 0 END) AS cancelled`;

const success = (row) => rate(row.converted, row.converted + row.failed + row.crashed);

function outcomes(row) {
  return {
    runs: count(row.runs), converted: count(row.converted), failed: count(row.failed), crashed: count(row.crashed),
    declined: count(row.declined), unsupported: count(row.unsupported), updated: count(row.updated),
    putBack: count(row.put_back), cancelled: count(row.cancelled), success: success(row),
  };
}

export async function snapshot(db, now = new Date()) {
  const all = async (sql, ...values) => (await db.prepare(sql).bind(...values).all()).results;
  const today = now.toISOString().slice(0, 10);
  const through = new Date(Date.parse(`${today}T00:00:00Z`) - 86_400_000).toISOString().slice(0, 10);
  const [devices] = await all(`SELECT count(*) AS seen, sum(conversions > 0) AS converted,
    sum(first_result = 'converted') AS first_run, sum(back_to_sonoff > 0) AS back FROM devices
    WHERE dev = 0 AND ${NOT_HELD} AND device IS NOT NULL`);
  const [median] = await all(`SELECT runs_to_convert AS value FROM (SELECT runs_to_convert,
      row_number() OVER (ORDER BY runs_to_convert) AS rank, count(*) OVER () AS n
    FROM devices WHERE dev = 0 AND ${NOT_HELD} AND device IS NOT NULL AND runs_to_convert IS NOT NULL)
    WHERE rank = (n + 1) / 2`);
  const [runs] = await all(`SELECT ${OUTCOMES} FROM monthly_totals WHERE tag = 'user'`);
  const months = await all(`SELECT month, ${OUTCOMES} FROM monthly_totals WHERE tag = 'user'
    GROUP BY month ORDER BY month`);
  const newDevices = new Map((await all(`SELECT substr(first_day, 1, 7) AS month, count(*) AS n FROM devices
    WHERE dev = 0 AND ${NOT_HELD} GROUP BY month`)).map((row) => [row.month, row.n]));
  // Each kind's devices, from its models' in one query: a model named within
  // it once vouched for and of SMALLEST or more.
  const byKind = new Map();
  for (const row of await all(`SELECT device, model, ${VOUCHED} AS vouched, count(*) AS devices,
      sum(conversions > 0) AS converted, sum(first_result = 'converted') AS first_run
    FROM devices WHERE dev = 0 AND ${NOT_HELD} AND device IS NOT NULL GROUP BY device, model
    ORDER BY devices DESC, model`)) {
    const kind = byKind.get(row.device) ?? { device: row.device, devices: 0, converted: 0, first_run: 0, models: [] };
    kind.devices += row.devices;
    kind.converted += row.converted;
    kind.first_run += row.first_run;
    if (row.vouched && row.devices >= SMALLEST) {
      kind.models.push({ model: row.model, devices: row.devices });
    }
    byKind.set(row.device, kind);
  }
  const kinds = [...byKind.values()].filter((row) => row.devices >= SMALLEST)
    .sort((a, b) => b.devices - a.devices || a.device.localeCompare(b.device));
  // Each model's devices month by month, as the admin view counts them.
  const modelMonths = (await popularity(all, `${monthsTo(now, TREND_MONTHS)[0]}-01`, VOUCHED))
    .map((row) => ({ month: row.month, model: row.model, asked: count(row.asked), readOut: count(row.readOut),
      converted: count(row.converted) }))
    .filter((row) => row.asked !== null || row.readOut !== null || row.converted !== null);
  const routes = await all(`SELECT source, firmware, ${OUTCOMES} FROM monthly_totals
    WHERE tag = 'user' AND firmware != '' GROUP BY source, firmware HAVING sum(n) >= ? ORDER BY runs DESC`, SMALLEST);
  const tools = await all(`SELECT tool, ${OUTCOMES} FROM monthly_totals WHERE tag = 'user'
    GROUP BY tool HAVING sum(n) >= ? ORDER BY tool`, SMALLEST);
  const failures = await all(`SELECT step, error, sum(n) AS runs FROM monthly_totals
    WHERE tag = 'user' AND result IN ('failed', 'crashed') AND error NOT IN ${NOT_FAILURES}
    GROUP BY step, error HAVING sum(n) >= ? ORDER BY runs DESC, step, error`, SMALLEST);
  // The stages people stop a run at, whichever way they stopped it.
  const cancellations = await all(`SELECT step, sum(n) AS runs FROM monthly_totals
    WHERE tag = 'user' AND result = 'cancelled'
    GROUP BY step HAVING sum(n) >= ? ORDER BY runs DESC, step`, SMALLEST);
  // The devices the tool does not convert yet, each once however often it was
  // met: asked about for a readout (wanted_devices), or met by a run
  // (unsupported-device) - one device both, once.
  const wanted = await all(`SELECT model, count(*) AS devices, sum(asked) AS asked, sum(ran) AS ran FROM (
      SELECT model, device_hash, max(asked) AS asked, max(ran) AS ran FROM (
        SELECT model, device_hash, 1 AS asked, 0 AS ran FROM wanted_devices
        UNION ALL
        SELECT model, device_hash, 0, 1 FROM reports WHERE tag = 'user' AND quarantined = 0
          AND error = 'unsupported-device' AND device_hash IS NOT NULL AND model IS NOT NULL)
      WHERE ${VOUCHED} AND device_hash NOT IN (SELECT device_hash FROM devices WHERE dev = 1) AND ${NOT_HELD}
      GROUP BY model, device_hash)
    GROUP BY model HAVING count(*) >= ? ORDER BY devices DESC, model`, SMALLEST);
  const stock = await all(`SELECT device, stock_version, sum(runs) AS runs, sum(converted) AS converted FROM monthly_stock
    WHERE tag = 'user' GROUP BY device, stock_version HAVING sum(runs) >= ? ORDER BY device, stock_version`, SMALLEST);
  const computers = await all(`SELECT os_family, python_source, sum(n) AS runs FROM monthly_environment
    WHERE tag = 'user' GROUP BY os_family, python_source HAVING sum(n) >= ? ORDER BY runs DESC`, SMALLEST);
  const times = await all(`SELECT month, device, firmware, source, n, p50, p90 FROM monthly_times
    WHERE tag = 'user' AND n >= ? AND month IN (SELECT DISTINCT month FROM monthly_times ORDER BY month DESC LIMIT ?)
    ORDER BY month DESC, n DESC`, SMALLEST, TIME_MONTHS);
  const places = new Map((await all(`SELECT place, count(*) AS n FROM devices
    WHERE place IS NOT NULL AND dev = 0 AND ${NOT_HELD} GROUP BY place`))
    .map((row) => [row.place, row.n]));
  const reached = new Map();   // k: the computers that converted at least k
  let most = 0;
  for (const place of [...places.keys()].sort((a, b) => b - a)) {
    most = Math.max(most, places.get(place));
    reached.set(place, most);
  }
  const atLeast = (k) => [...reached].filter(([place]) => place >= k).reduce((top, [, n]) => Math.max(top, n), 0);
  // Each device's versions, its suites summed in each: newest first.
  const testing = new Map();
  for (const row of await deviceTesting(all, true)) {
    const device = testing.get(row.device) ?? { device: row.device, family: row.family, suites: new Set(),
      versions: new Map() };
    const version = device.versions.get(row.tool) ?? { tool: row.tool, last: "", firmware: {}, scenarios: 0,
      passed: 0, judged: 0, firstPassed: 0 };
    if (row.total) {
      version.scenarios += row.total;
      version.passed += row.covered;
    }
    version.judged += row.judged ?? 0;
    version.firstPassed += row.passed ?? 0;
    if (row.last > version.last) {
      version.last = row.last;
      version.firmware = JSON.parse(row.firmware ?? "{}");
    }
    device.suites.add(row.suite);
    device.versions.set(row.tool, version);
    testing.set(row.device, device);
  }
  const testedOn = (row) => ({
    tool: row.tool, lastDay: row.last.slice(0, 10), firmware: row.firmware, scenarios: row.scenarios || null,
    passed: row.passed, coverage: row.scenarios ? Math.round((1000 * row.passed) / row.scenarios) / 10 : null,
    firstTry: rate(row.firstPassed, row.judged),
  });
  // The tool's downloads, each night GitHub was read, beside the devices
  // converted by then: GitHub shows anyone the downloads, so they are whole.
  const downloads = await downloadDays(all, "", `dev = 0 AND ${NOT_HELD} AND device IS NOT NULL`);
  const counted = [...reached.keys()].sort((a, b) => a - b)
    .map((place) => ({ devices: place, computers: reached.get(place) - atLeast(place + 1) }))
    .filter((row) => row.computers >= SMALLEST);
  return {
    format: FORMAT,
    generated: now.toISOString().slice(0, 16) + "Z",
    through,
    schema: LATEST_MIGRATION,
    licence: "CC BY 4.0",
    attribution: "FirmwareLiberator, https://firmwareliberator.com/stats",
    smallest: SMALLEST,
    devices: {
      seen: count(devices.seen), converted: count(devices.converted ?? 0), backToSonoff: count(devices.back ?? 0),
      firstRun: rate(devices.first_run ?? 0, devices.seen), eventually: rate(devices.converted ?? 0, devices.seen),
      medianRuns: (devices.converted ?? 0) >= SMALLEST ? (median?.value ?? null) : null,
    },
    runs: outcomes(runs),
    months: months.map((row) => ({ month: row.month, newDevices: count(newDevices.get(row.month)), ...outcomes(row) })),
    kinds: kinds.map((row) => ({
      device: row.device, devices: row.devices, converted: count(row.converted),
      firstRun: rate(row.first_run, row.devices), eventually: rate(row.converted, row.devices),
      models: row.models,
    })),
    modelMonths,
    wanted: wanted.map((row) => ({ model: row.model, devices: row.devices, asked: count(row.asked), ran: count(row.ran) })),
    routes: routes.map((row) => ({ source: row.source, firmware: row.firmware, ...outcomes(row) })),
    tools: tools.map((row) => ({ tool: row.tool, ...outcomes(row) })),
    failures: failures.map((row) => ({ step: row.step || null, error: row.error, runs: row.runs })),
    cancellations: cancellations.map((row) => ({ step: row.step || null, runs: row.runs })),
    perComputer: { computers: count(atLeast(1)), rows: counted },
    downloads: {
      total: downloads.at(-1)?.downloads ?? null, day: downloads.at(-1)?.day ?? null,
      days: downloads.map((row) => ({ day: row.day, downloads: row.downloads, converted: count(row.converted) })),
    },
    stock: stock.map((row) => ({ device: row.device, stock_version: row.stock_version, runs: row.runs, converted: count(row.converted),
      success: rate(row.converted, row.runs) })),
    computers: computers.map((row) => ({ os: row.os_family, python: row.python_source, runs: row.runs })),
    times: times.map((row) => ({ month: row.month, device: row.device || null, firmware: row.firmware || null,
      source: row.source, runs: row.n, median: row.p50, p90: row.p90 })),
    testing: [...testing.values()].map((row) => {
      const versions = [...row.versions.values()].sort((a, b) => newerFirst(a.tool, b.tool)).map(testedOn);
      return { device: row.device, family: row.family, suites: [...row.suites].sort(), ...versions[0], versions };
    }).sort((a, b) => a.device.localeCompare(b.device)),
  };
}
