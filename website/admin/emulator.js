// The maintainer's emulator runs (emulator_runs and the rest, ../api/migrations):
// taken from the suites under SCOPE, and shown on the admin view.
//
// The suites (tools/repo/emulator_stats.py) send what each scenario's run came
// to as each run ends, and what each batch set out to run as it begins, with
// Access's service token - a separate Access application covers SCOPE alone,
// whose one policy lets in that token and no one else
// (tools/repo/cloudflare_settings.py), so ACCESS_UPLOAD_AUD is its audience
// and no other. The token can send runs and batches and do nothing else: the
// view itself refuses its JWT, and SCOPE refuses the maintainer's. Should it
// leak, DAILY_RUNS and DAILY_BATCHES bound what it can add in a day.
//
// Nothing here is a person's run: those are the API's reports, and no figure
// of theirs counts these.

import { daysBack, deviceTesting, INCONCLUSIVE, outcomeOf } from "../api/emulator.js";
import { esc, FULL, columns, heat, number, table, tile } from "./render.js";

// Every path the suites use: what Access's application for them covers.
export const SCOPE = "/v1/emulator";
export const RUNS_PATH = `${SCOPE}/runs`;
export const BATCHES_PATH = `${SCOPE}/batches`;
// At most this many runs or batches a request; the suites send more as several.
export const MAX_RUNS = 500;
// The largest body taken, ample for MAX_RUNS runs.
export const MAX_BODY = 1_000_000;
// At most this many taken a day (UTC), each: a whole suite on every machine
// several times over, and a bound on what a leaked token could add.
export const DAILY_RUNS = 20_000;
export const DAILY_BATCHES = 2_000;
// A run is taken up to this long after it finished (the suites keep unsent
// ones until they can send them), and no later than a little after now.
const OLDEST_DAYS = 60;
const AHEAD_MS = 3_600_000;

// What a run that did not pass came from, as the maintainer marks it on the
// view: "tool", a bug in the tool, is what counts as a bug found; the rest are
// the test rig's.
export const EMULATOR_CAUSES = ["tool", "emulator", "cloud", "suite", "flaky"];
// What a mark may point at: a commit of the repository, or one of its issues
// or pull requests, by number or link.
const REPOSITORY = "https://github.com/adamsthws/firmware-liberator";
const CAUSE_REF = /^(?:[0-9a-f]{7,40}|#\d{1,7}|https:\/\/github\.com\/adamsthws\/firmware-liberator\/(?:issues|pull)\/\d{1,7}|https:\/\/github\.com\/adamsthws\/firmware-liberator\/commit\/[0-9a-f]{7,40})$/;

const SHORT = /^[\x20-\x7e]{1,64}$/;
// At most this many of a run's runs say why they stopped (emulator_stats.py STOPPED_MOST).
export const STOPPED_MOST = 16;
const finishedAt = (v) => typeof v === "string" && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/.test(v) && !Number.isNaN(Date.parse(v));
const word = (v) => typeof v === "string" && /^[a-z][a-z0-9-]{0,31}$/.test(v);
// A chip family or device by key: any, so a new one needs no change here.
const COMMON = {
  family: word,
  device: (v) => typeof v === "string" && /^[a-z0-9][a-z0-9-]{0,47}$/.test(v),
  suite: (v) => v === "fault" || v === "attended" || v === "check",
  cloud: (v) => v === "stub" || v === "real" || v === "none",
  tool: (v) => typeof v === "string" && /^\d{1,4}\.\d{1,4}\.\d{1,4}$/.test(v),
  revision: (v) => v === null || (typeof v === "string" && /^[0-9a-f]{7,40}$/.test(v)),
  dirty: (v) => typeof v === "boolean",
  workers: (v) => v === null || (Number.isInteger(v) && v >= 1 && v <= 256),
};
// Each field a run carries, and what it may hold: every one is there, and no
// other. Null where a suite has nothing to say (an attended run has no fault).
const RUN_FIELDS = {
  id: (v) => typeof v === "string" && /^[0-9a-f]{32}$/.test(v),
  finished: finishedAt,
  batch: (v) => typeof v === "string" && /^[0-9a-f]{16}$/.test(v),
  ...COMMON,
  scenario: (v) => typeof v === "string" && /^[\x20-\x7e]{1,200}$/.test(v),
  route: (v) => v === null || (typeof v === "string" && SHORT.test(v)),
  step: (v) => v === null || (typeof v === "string" && SHORT.test(v)),
  fault: (v) => v === null || (typeof v === "string" && SHORT.test(v)),
  // The fault's argument: an outage's length in seconds, "once", ...
  arg: (v) => v === null || (typeof v === "string" && SHORT.test(v)),
  at: (v) => v === null || (Number.isInteger(v) && v >= 0 && v <= 100),
  verdict: (v) => typeof v === "string" && /^[A-Z][A-Z0-9-]{0,39}$/.test(v),
  rerun: (v) => typeof v === "boolean",
  tries: (v) => v === null || (Number.isInteger(v) && v >= 0 && v <= 99),
  seconds: (v) => v === null || (Number.isInteger(v) && v >= 0 && v <= 172_800),
  firmware: (v) => v !== null && typeof v === "object" && !Array.isArray(v) && Object.keys(v).length <= 16
    && Object.entries(v).every(([name, version]) => /^[a-z][a-z0-9-]{0,23}$/.test(name)
      && typeof version === "string" && /^[\x20-\x7e]{1,40}$/.test(version)),
  // Why each of the scenario's runs that stopped did, in order, as the tool
  // classes it (tools/autoflash/failures.py): a pass after a second run says
  // why the first stopped.
  stopped_by: (v) => Array.isArray(v) && v.length <= STOPPED_MOST && v.every(word),
  // The suite's own error, by exception class alone, on a run it could not judge.
  error: (v) => v === null || (typeof v === "string" && /^[A-Za-z_][A-Za-z0-9_.]{0,63}$/.test(v)),
  // Whether the computer was short of memory: a failure then may be no fault of the tool's.
  starved: (v) => typeof v === "boolean",
  // The emulator image the run had, by its tag (image.sh tag).
  image: (v) => v === null || (typeof v === "string" && /^[0-9a-f]{8,64}$/.test(v)),
};
// What a batch set out to do as it began: its suite's scenarios for the
// device, and how many it runs.
const BATCH_FIELDS = {
  batch: (v) => typeof v === "string" && /^[0-9a-f]{16}$/.test(v),
  started: finishedAt,
  ...COMMON,
  total: (v) => Number.isInteger(v) && v >= 1 && v <= 100_000,
  planned: (v) => Number.isInteger(v) && v >= 0 && v <= 100_000,
  // Which list it ran (emulator_stats.py TIERS): every scenario, a routine run's, or part of it.
  tier: (v) => v === "full" || v === "quick" || v === "subset",
};

// Why `item` cannot be taken as `fields` say, or null.
function refusedBy(fields, item, when, now) {
  if (item === null || typeof item !== "object" || Array.isArray(item)) {
    return "not one";
  }
  const extra = Object.keys(item).filter((name) => !Object.hasOwn(fields, name));
  if (extra.length) {
    return `fields it never has: ${extra.slice(0, 5).join(", ")}`;
  }
  const wrong = Object.keys(fields).filter((name) => !fields[name](item[name]));
  if (wrong.length) {
    return `missing or not as it has them: ${wrong.join(", ")}`;
  }
  const at = Date.parse(item[when]);
  if (at > now + AHEAD_MS || at < now - OLDEST_DAYS * 86_400_000) {
    return `${when} ${item[when]}: not within the last ${OLDEST_DAYS} days`;
  }
  return null;
}

export const refused = (run, now) => refusedBy(RUN_FIELDS, run, "finished", now);
export const refusedBatch = (batch, now) => refusedBy(BATCH_FIELDS, batch, "started", now);

// Each kind of thing the suites send: its path's body key, fields, the
// column it is dated by, its daily bound, and its row as stored - a run with
// its outcome, as INCONCLUSIVE has it.
const STORED = { firmware: (value) => JSON.stringify(value), stopped_by: (value) => JSON.stringify(value) };
export const KINDS = {
  [RUNS_PATH]: { key: "runs", table: "emulator_runs", fields: RUN_FIELDS, when: "finished", daily: DAILY_RUNS,
    id: (run) => run.id, extra: (run) => ({ outcome: outcomeOf(run.verdict) }) },
  [BATCHES_PATH]: { key: "batches", table: "emulator_batches", fields: BATCH_FIELDS, when: "started",
    daily: DAILY_BATCHES, id: (batch) => `${batch.batch}/${batch.family}/${batch.device}/${batch.suite}`,
    extra: () => ({}) },
};

const json = (status, body, headers = {}) => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...headers } });

const stamp = (now) => new Date(now).toISOString().slice(0, 19) + "Z";

// POST a KINDS path: {"runs": [1 to MAX_RUNS]} (or "batches"), answered
// {"taken": [id...], "refused": [{"id", "why"}]}: one already taken is taken
// again, so a suite that never heard the answer sends it once more and
// nothing is counted twice. A batch's id is "batch/family/device/suite".
export async function upload(request, env, now = Date.now(), path = RUNS_PATH) {
  const kind = KINDS[path];
  if (request.headers.get("Content-Type") !== "application/json") {
    return json(415, { error: `${kind.key} are application/json` });
  }
  const body = await request.arrayBuffer();
  if (body.byteLength > MAX_BODY) {
    return json(413, { error: `at most ${MAX_BODY} bytes at once` });
  }
  let sent = null;
  try {
    sent = JSON.parse(new TextDecoder().decode(body));
  } catch {
    // below
  }
  const items = sent?.[kind.key];
  if (sent === null || typeof sent !== "object" || Object.keys(sent).join() !== kind.key || !Array.isArray(items)
      || items.length < 1 || items.length > MAX_RUNS) {
    return json(400, { error: `that is not ${kind.key}: {"${kind.key}": [1 to ${MAX_RUNS}]}` });
  }
  const today = stamp(now).slice(0, 10);
  const taken = (await env.DB.prepare(`SELECT count(*) AS n FROM ${kind.table} WHERE received >= ?`)
    .bind(today).first("n")) ?? 0;
  if (taken + items.length > kind.daily) {
    return json(429, { error: `at most ${kind.daily} ${kind.key} a day: the rest tomorrow` }, { "Retry-After": "3600" });
  }
  const accepted = [];
  const refusals = [];
  const statements = [];
  for (const item of items) {
    const why = refusedBy(kind.fields, item, kind.when, now);
    if (why) {
      const id = item !== null && typeof item === "object" ? String(kind.id(item)).slice(0, 200) : null;
      refusals.push({ id, why });
      continue;
    }
    accepted.push(kind.id(item));
    const row = { ...item, ...kind.extra(item), received: stamp(now) };
    const names = Object.keys(row);
    statements.push(env.DB.prepare(`INSERT OR IGNORE INTO ${kind.table} (${names.join(", ")})
      VALUES (${names.map((_, at) => `?${at + 1}`).join(", ")})`)
      .bind(...names.map((name) => (STORED[name] ? STORED[name](row[name]) : row[name]))));
  }
  if (statements.length) {
    await env.DB.batch(statements);
  }
  return json(200, { taken: accepted, refused: refusals });
}

// A run's cause set, or cleared ("" for none), with what it points at, by
// run id: only from a form on the view (index.js checks that).
export async function setCause(form, env) {
  const id = String(form.get("id") ?? "");
  const cause = String(form.get("cause") ?? "");
  const ref = String(form.get("ref") ?? "").trim();
  if (!/^[0-9a-f]{32}$/.test(id) || (cause !== "" && !EMULATOR_CAUSES.includes(cause))
      || (ref !== "" && !CAUSE_REF.test(ref))) {
    return false;
  }
  await env.DB.prepare("UPDATE emulator_runs SET cause = ?2, cause_ref = ?3 WHERE id = ?1 AND outcome != 'pass'")
    .bind(id, cause || null, cause && ref ? ref : null).run();
  return true;
}

// A mark's commit or issue as a link to it: "#12" or a commit's first ten.
export function refLink(ref) {
  if (!ref) {
    return "";
  }
  const [, kind, what] = ref.match(/\/(issues|pull|commit)\/(\w+)$/) ?? [];
  const commit = kind === "commit" ? what : /^[0-9a-f]+$/.test(ref) ? ref : null;
  const href = ref.startsWith("https://") ? ref : commit ? `${REPOSITORY}/commit/${commit}`
    : `${REPOSITORY}/issues/${ref.slice(1)}`;
  return `<a href="${esc(href)}" rel="noreferrer">${esc(commit ? commit.slice(0, 10) : kind ? `#${what}` : ref)}</a>`;
}

// How many days the view's recent figures go back, and its long ones.
export const EMULATOR_DAYS = 30;
export const LONG_DAYS = 90;
// How many revisions the pass-rate table shows, newest first.
export const REVISIONS = 12;

// Each recent run with whether it is a new failure: one that fails where its
// scenario's run before it, on another revision, passed - what a change broke.
// Looked at over twice EMULATOR_DAYS, so a run early in it has its before.
const withBefore = (since) => `WITH ordered AS (
    SELECT *, lag(outcome) OVER (PARTITION BY family, device, scenario ORDER BY finished) AS before,
      lag(revision) OVER (PARTITION BY family, device, scenario ORDER BY finished) AS before_revision
    FROM emulator_runs WHERE finished >= '${since}')`;
const NEW_FAILURE = "(outcome = 'fail' AND before = 'pass' AND revision IS NOT before_revision)";

// Everything the view's emulator section shows, in a fixed few queries: the
// long figures from the nightly fold (emulator_days) and today's runs, the
// rest from the runs of the last EMULATOR_DAYS.
export async function gatherEmulator(all, now) {
  // The days folded: those before the last fold's (emulator_fold); from it on,
  // the runs themselves.
  const [fold] = await all("SELECT substr(received, 1, 10) AS day FROM emulator_fold WHERE id = 1");
  const today = fold?.day ?? "";
  const since = daysBack(now, EMULATOR_DAYS);
  const before = daysBack(now, 2 * EMULATOR_DAYS);
  const week = daysBack(now, 7);
  const DAYS = `SELECT day, family, device, suite, runs, passed, failed, inconclusive, bricked, first_judged,
      first_passed, seconds FROM emulator_days WHERE day < ?1
    UNION ALL SELECT substr(finished, 1, 10), family, device, suite, count(*), sum(outcome = 'pass'),
      sum(outcome = 'fail'), sum(outcome = 'inconclusive'), sum(verdict = 'BRICK'),
      sum(rerun = 0 AND outcome != 'inconclusive'), sum(rerun = 0 AND outcome = 'pass'), sum(coalesce(seconds, 0))
    FROM emulator_runs WHERE finished >= ?1 GROUP BY 1, 2, 3, 4`;
  const [totals] = await all(`SELECT sum(runs) AS runs, sum(bricked) AS bricks, sum(first_judged) AS judged,
      sum(first_passed) AS passed,
      sum(CASE WHEN day >= ?2 THEN runs ELSE 0 END) AS week, sum(CASE WHEN day >= ?2 THEN first_judged ELSE 0 END)
        AS week_judged, sum(CASE WHEN day >= ?2 THEN first_passed ELSE 0 END) AS week_passed,
      sum(CASE WHEN day >= ?2 THEN inconclusive ELSE 0 END) AS week_inconclusive,
      sum(CASE WHEN day >= ?2 THEN seconds ELSE 0 END) AS week_seconds
    FROM (${DAYS})`, today, week);
  const [marks] = await all(`SELECT sum(cause IS NULL) AS unmarked, sum(cause = 'tool') AS bugs,
      count(DISTINCT CASE WHEN cause = 'tool' THEN coalesce(cause_ref, id) END) AS bug_refs
    FROM emulator_runs WHERE outcome != 'pass'`);
  return {
    totals: { ...totals, ...marks },
    days: await all(`SELECT day, sum(passed) AS passed, sum(failed - bricked) AS failed, sum(bricked) AS bricked,
        sum(inconclusive) AS inconclusive FROM (${DAYS}) WHERE day >= ?2 GROUP BY day ORDER BY day`,
    today, daysBack(now, LONG_DAYS)),
    revisions: await all(`${withBefore(before)}
      SELECT revision, max(tool) AS tool, max(dirty) AS dirty, count(*) AS runs,
        sum(rerun = 0 AND outcome != 'inconclusive') AS judged, sum(rerun = 0 AND outcome = 'pass') AS passed,
        sum(outcome = 'inconclusive') AS inconclusive, sum(${NEW_FAILURE}) AS new_failures,
        count(DISTINCT scenario) AS scenarios, max(finished) AS last
      FROM ordered WHERE finished >= ? GROUP BY revision ORDER BY last DESC LIMIT ${REVISIONS}`, since),
    devices: await deviceTesting(all),
    batches: await all(`SELECT started, family, device, suite, tier, planned, total, revision, dirty
      FROM emulator_batches WHERE started >= ? ORDER BY started DESC LIMIT ${REVISIONS}`, since),
    verdicts: await all(`SELECT verdict, outcome, count(*) AS n FROM emulator_runs WHERE finished >= ?
      AND outcome != 'pass' GROUP BY verdict ORDER BY n DESC`, since),
    // The test rig's own trouble, by family and day: what the suites could not judge.
    rig: await all(`SELECT family, verdict, error, count(*) AS n, max(finished) AS last FROM emulator_runs
      WHERE finished >= ? AND outcome = 'inconclusive' GROUP BY family, verdict, error ORDER BY n DESC`, since),
    // Each fault at each length: what it has found - a failure, or a pass that
    // took a run again after one stopped - so a length that finds nothing the
    // others do not can go.
    faultLengths: await all(`SELECT fault, arg, sum(outcome != 'inconclusive') AS runs, sum(outcome = 'fail') AS failed,
        sum(outcome = 'pass' AND stopped_by != '[]') AS stopped_then_passed,
        count(DISTINCT CASE WHEN outcome = 'fail' OR (outcome = 'pass' AND stopped_by != '[]') THEN scenario END)
          AS scenarios
      FROM emulator_runs WHERE suite = 'fault' AND fault IS NOT NULL AND finished >= ?
      GROUP BY fault, arg HAVING runs > 0 ORDER BY fault, CAST(arg AS INTEGER), arg`, since),
    // Why runs stopped, by step: of passes after a second run, the first's
    // reason; of failures, every run's.
    stopped: await all(`SELECT step, stop.value AS why, sum(outcome = 'pass') AS passed, sum(outcome = 'fail') AS failed,
        count(DISTINCT scenario) AS scenarios
      FROM emulator_runs, json_each(emulator_runs.stopped_by) AS stop
      WHERE suite = 'fault' AND outcome != 'inconclusive' AND finished >= ?
      GROUP BY step, why ORDER BY passed + failed DESC LIMIT 40`, since),
    // First tries by emulator image: a change to the emulator moves these, not
    // the tool.
    images: await all(`SELECT family, image, count(*) AS runs, sum(rerun = 0 AND outcome != 'inconclusive') AS judged,
        sum(rerun = 0 AND outcome = 'pass') AS passed, sum(outcome = 'inconclusive') AS inconclusive,
        max(finished) AS last
      FROM emulator_runs WHERE finished >= ? GROUP BY family, image ORDER BY last DESC LIMIT ${REVISIONS}`, since),
    stepFault: await all(`SELECT step, fault, sum(outcome != 'inconclusive') AS runs, sum(outcome = 'fail') AS failed
      FROM emulator_runs WHERE suite = 'fault' AND step IS NOT NULL AND fault IS NOT NULL AND finished >= ?
      GROUP BY step, fault HAVING runs > 0`, since),
    tries: await all(`SELECT step, sum(tries = 1) AS t1, sum(tries = 2) AS t2, sum(tries = 3) AS t3,
        sum(tries >= 4) AS t4, avg(seconds) AS seconds
      FROM emulator_runs WHERE suite = 'fault' AND outcome = 'pass' AND step IS NOT NULL AND finished >= ?
      GROUP BY step ORDER BY step`, since),
    flaky: await all(`SELECT family, device, scenario, revision, sum(outcome = 'pass') AS passed,
        sum(outcome = 'fail') AS failed, max(finished) AS last
      FROM emulator_runs WHERE finished >= ? GROUP BY family, device, scenario, revision
      HAVING passed > 0 AND failed > 0 ORDER BY last DESC LIMIT 50`, since),
    // Real-cloud runs by the hour into their batch they finished in: eWeLink
    // drops sessions more the longer a batch goes on.
    cloudHours: await all(`SELECT CAST((julianday(finished) - julianday(first)) * 24 AS INTEGER) AS hour,
        sum(outcome != 'inconclusive') AS runs, sum(outcome = 'fail') AS failed
      FROM emulator_runs JOIN (SELECT batch AS b, min(finished) AS first FROM emulator_runs
        WHERE finished >= ?1 GROUP BY batch) ON b = batch
      WHERE cloud = 'real' AND finished >= ?1 GROUP BY hour ORDER BY hour`, since),
    causes: await all(`SELECT outcome, coalesce(cause, 'not marked') AS cause, count(*) AS n FROM emulator_runs
      WHERE outcome != 'pass' GROUP BY outcome, cause ORDER BY outcome DESC, n DESC`),
    failures: await all(`${withBefore(before)}
      SELECT id, finished, family, device, suite, scenario, verdict, outcome, tries, revision, tool, cause, cause_ref,
        starved,
        ${NEW_FAILURE} AS new_failure
      FROM ordered WHERE outcome != 'pass' AND finished >= ? ORDER BY finished DESC LIMIT 60`, since),
  };
}

const share = (part, of) => (of ? `${Math.round((100 * part) / of)}%` : "–");
const hours = (seconds) => `${number(Math.round((seconds ?? 0) / 360) / 10)} h`;
const firmwareSaid = (text) => {
  try {
    return Object.entries(JSON.parse(text ?? "{}")).map(([name, version]) => `${name} ${version}`).join(", ");
  } catch {
    return "";
  }
};

// The emulator section of the view: cards, from what gatherEmulator() read.
export function emulatorSections(data) {
  const t = data.totals ?? {};
  const steps = [...new Set(data.stepFault.map((row) => row.step))].sort();
  const faults = [...new Set(data.stepFault.map((row) => row.fault))].sort();
  const cell = new Map(data.stepFault.map((row) => [`${row.step}\n${row.fault}`, row]));
  const causeForm = (row) => `<form class="inline" method="post" action="/emulator-cause">`
    + `<input type="hidden" name="id" value="${esc(row.id)}"><select name="cause" aria-label="Cause">`
    + [["", "-"], ...EMULATOR_CAUSES.map((cause) => [cause, cause])].map(([value, label]) =>
      `<option value="${value}"${(row.cause ?? "") === value ? " selected" : ""}>${label}</option>`).join("")
    + `</select> <input name="ref" size="10" aria-label="Commit or issue" placeholder="commit or #issue"`
    + ` value="${esc(row.cause_ref ?? "")}"> <button>Mark</button></form> ${refLink(row.cause_ref)}`;
  return [
    `<section class="card" id="emulator"><h2>Emulator runs</h2><p class="sub">The maintainer's own suites - fault,
      attended and the final-firmware check, every chip family: one row a scenario run, sent as it ends. No person's
      run is among them. Pass rates are of first tries the suite could judge: a batch re-running what failed, or a
      run the suite could not judge (inconclusive: ${esc(INCONCLUSIVE.join(", "))}), counts apart. Days before today
      are counted each night.</p><div class="tiles">`
      + tile("Runs", number(t.runs ?? 0), `${share(t.passed, t.judged)} of first tries passed`)
      + tile("The last 7 days", number(t.week ?? 0), `${share(t.week_passed, t.week_judged)} of first tries passed`)
      + tile("Inconclusive, 7 days", share(t.week_inconclusive, t.week), "the test rig's, not the tool's")
      + tile("Emulator time, 7 days", hours(t.week_seconds))
      + tile("Bricked", number(t.bricks ?? 0), "should stay 0")
      + tile("Bugs found", number(t.bug_refs ?? 0),
        `${number(t.bugs ?? 0)} runs marked "tool" · ${t.judged ? number(Math.round((1000 * (t.bug_refs ?? 0)) / t.judged * 10) / 10) : "–"} a thousand judged`)
      + tile("Not marked", number(t.unmarked ?? 0), "runs that did not pass")
      + `</div>`
      + columns(data.days.map((row) => ({ day: row.day, parts: { passed: row.passed, inconclusive: row.inconclusive,
        failed: row.failed, bricked: row.bricked } })), ["passed", "inconclusive", "failed", "bricked"],
      `Emulator runs a day, the last ${LONG_DAYS} days`, FULL, ["series-1", "muted", "series-2", "critical"])
      + `</section>`,
    `<section class="card" id="emulator-revisions"><h2>By revision</h2><p class="sub">The newest ${REVISIONS}
      checkouts the suites ran in the last ${EMULATOR_DAYS} days, newest first. A new failure is a scenario that
      passed on the revision before and fails on this one: what a change broke.</p>`
      + table([["Revision", (row) => (row.revision ?? "none") + (row.dirty ? " (changed)" : "")], ["Tool", "tool"],
        ["Runs", "runs", "number"], ["Scenarios", "scenarios", "number"],
        ["First tries passed", (row) => `${share(row.passed, row.judged)} of ${number(row.judged)}`],
        ["Inconclusive", "inconclusive", "number"], ["New failures", "new_failures", "number"],
        ["Last", (row) => String(row.last).slice(0, 16)]], data.revisions, "No runs yet.")
      + `</section>`,
    `<section class="card" id="emulator-devices"><h2>By device</h2><p class="sub">Every device and suite ever run,
      version by version of the tool, the newest first: how many of its suite's scenarios have passed on that
      version - each version on its own, as a change since may break one that passed - its first tries, and the
      firmware its newest run of that version installed: a firmware release no run has met shows here first.</p>`
      + table([["Family", "family"], ["Device", "device"], ["Suite", "suite"], ["Tool", "tool"],
        ["Scenarios passed", (row) => (row.total ? `${number(row.covered)} of ${number(row.total)} (${share(row.covered, row.total)})`
          : `${number(row.covered)}`)],
        ["First tries passed", (row) => `${share(row.passed, row.judged)} of ${number(row.judged)}`],
        ["Inconclusive", "inconclusive", "number"], ["Last", (row) => String(row.last).slice(0, 10)],
        ["Firmware", (row) => firmwareSaid(row.firmware)]], data.devices, "No runs yet.")
      + `</section>`,
    `<section class="card" id="emulator-batches"><h2>Batches</h2><p class="sub">The newest ${REVISIONS} batches
      of the last ${EMULATOR_DAYS} days, and which list each ran: every scenario (full), a routine run's (quick:
      the shortest and longest outage alone), or part of one. A device's scenarios passed are of its whole suite,
      so a routine run's leave the lengths it skips unpassed.</p>`
      + table([["Started", (row) => String(row.started).slice(0, 16)], ["Device", "device"], ["Suite", "suite"],
        ["List", "tier"], ["Planned", "planned", "number"], ["Of", "total", "number"],
        ["Revision", (row) => (row.revision ?? "none") + (row.dirty ? " (changed)" : "")]], data.batches, "None yet.")
      + `</section>`,
    `<section class="card" id="emulator-faults"><h2>Fault by step</h2><p class="sub">The last ${EMULATOR_DAYS} days'
      fault-suite runs: those failed of those judged, at each step and fault. The darker, the more failed.</p>`
      + heat(steps, faults, (step, fault) => {
        const row = cell.get(`${step}\n${fault}`);
        return row ? { share: row.failed / row.runs, text: `${row.failed}/${row.runs}` } : null;
      }, "Step", "None run yet.")
      + `</section>`,
    `<section class="card" id="emulator-lengths"><h2>Fault by length</h2><p class="sub">The last ${EMULATOR_DAYS}
      days' fault-suite runs judged, by fault and its length or argument: what each has found - a failure, or a pass
      that needed a run again after one stopped. A length that finds nothing the others do not is a candidate to
      drop; a routine run (fault_suite.py list quick) runs only the shortest and longest outage.</p>`
      + table([["Fault", "fault"], ["Length", (row) => row.arg ?? ""], ["Judged", "runs", "number"],
        ["Failed", "failed", "number"], ["Passed after a stop", "stopped_then_passed", "number"],
        ["Scenarios", "scenarios", "number"]], data.faultLengths, "None run yet.")
      + `</section>`,
    `<div class="grid2"><section class="card" id="emulator-stopped"><h2>Why runs stopped</h2><p class="sub">The
      last ${EMULATOR_DAYS} days' fault-suite runs, by step and the class of what a run stopped with
      (tools/autoflash/failures.py): of a pass after more than one run, why the earlier ones stopped; of a failure,
      every run's. Many at one step and class is a bug a pass hides.</p>`
      + table([["Step", "step"], ["Stopped with", "why"], ["In passes", "passed", "number"],
        ["In failures", "failed", "number"], ["Scenarios", "scenarios", "number"]], data.stopped, "None stopped.")
      + `</section>`,
    `<section class="card" id="emulator-images"><h2>By emulator image</h2><p class="sub">First tries by the
      emulator image they ran on (image.sh tag), the newest ${REVISIONS} of the last ${EMULATOR_DAYS} days: a
      change to an emulator moves these, the tool's code staying as it was.</p>`
      + table([["Family", "family"], ["Image", (row) => row.image ?? "none"], ["Runs", "runs", "number"],
        ["First tries passed", (row) => `${share(row.passed, row.judged)} of ${number(row.judged)}`],
        ["Inconclusive", "inconclusive", "number"], ["Last", (row) => String(row.last).slice(0, 10)]],
      data.images, "None yet.")
      + `</section></div>`,
    `<div class="grid2"><section class="card" id="emulator-tries"><h2>Tries to convert</h2><p class="sub">Passing
      fault-suite runs by step: how many runs of the tool the device took to convert after its fault, and how long,
      the last ${EMULATOR_DAYS} days. More than one works, but is more for a person to sit through.</p>`
      + table([["Step", "step"], ["1", "t1", "number"], ["2", "t2", "number"], ["3", "t3", "number"],
        ["4+", "t4", "number"], ["Seconds", (row) => number(Math.round(row.seconds ?? 0))]], data.tries, "None yet.")
      + `</section>`,
    `<section class="card" id="emulator-verdicts"><h2>Verdicts</h2><p class="sub">The last ${EMULATOR_DAYS} days'
      runs that did not pass, and what every such run was marked as coming from.</p>`
      + table([["Verdict", "verdict"], ["Outcome", "outcome"], ["Runs", "n", "number"]], data.verdicts, "None yet.")
      + table([["Outcome", "outcome"], ["From", "cause"], ["Runs", "n", "number"]], data.causes, "None.")
      + `</section></div>`,
    `<div class="grid2"><section class="card" id="emulator-rig"><h2>The test rig</h2><p class="sub">Runs the suites
      could not judge, the last ${EMULATOR_DAYS} days, by family: the emulators', the suites' and the network's
      trouble, kept apart from the tool's.</p>`
      + table([["Family", "family"], ["Verdict", "verdict"], ["Error", (row) => row.error ?? ""],
        ["Runs", "n", "number"], ["Last", (row) => String(row.last).slice(0, 10)]], data.rig,
      "None: every run was judged.")
      + `</section>`,
    `<section class="card" id="emulator-flaky"><h2>Flaky</h2><p class="sub">Scenarios that both passed and failed
      on one revision, the last ${EMULATOR_DAYS} days.</p>`
      + table([["Device", "device"], ["Scenario", "scenario", "text"], ["Revision", "revision"],
        ["Passed", "passed", "number"], ["Failed", "failed", "number"]], data.flaky, "None.")
      + `</section></div>`,
    `<section class="card" id="emulator-cloud"><h2>Real cloud, by hour</h2><p class="sub">Runs on eWeLink's own
      cloud by the hour into their batch, the last ${EMULATOR_DAYS} days: failures climbing late are eWeLink's
      limits, not the tool.</p>`
      + table([["Hour", "hour", "number"], ["Judged", "runs", "number"], ["Failed", "failed", "number"],
        ["", (row) => share(row.failed, row.runs)]], data.cloudHours, "No real-cloud runs.")
      + `</section>`,
    `<section class="card" id="emulator-failures"><h2>Not passed</h2><p class="sub">The newest 60 failed or
      inconclusive runs of the last ${EMULATOR_DAYS} days. Mark what each came from, with the commit or issue
      that fixed it or follows it: "tool" counts as a bug found, once for each commit or issue.</p>`
      + table([["Finished", (row) => String(row.finished).slice(0, 16)], ["Device", "device"], ["Suite", "suite"],
        ["Scenario", "scenario", "text"], ["Verdict", (row) => row.verdict + (row.new_failure ? " (new)" : "")
          + (row.starved ? " (memory-starved)" : "")],
        ["Outcome", "outcome"], ["Tries", "tries", "number"], ["Revision", "revision"], ["Cause", causeForm, "html"]],
      data.failures, "None.")
      + `</section>`,
  ];
}
