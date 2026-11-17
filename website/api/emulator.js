// The maintainer's emulator runs (emulator_runs and the rest, migrations/):
// what both the admin view (../admin/emulator.js) and the public figures
// (snapshot.js) read of them, and the nightly job's fold of them by day.
// None of it is a person's run.

// The verdicts of a run the suite could not judge - the fault never landed,
// or landed at the wrong time, the device or the suite itself failed, or the
// computer lost the internet: neither the tool's pass nor its failure. Each
// suite's verdicts are in docs/testing.md; any other not PASS is a failure.
export const INCONCLUSIVE = ["MISTIMED", "NOT-FIRED", "NO-INTERNET", "NO-TRAFFIC", "NO-STEP", "NOT-READY",
  "HARNESS-ERROR", "HARNESS", "ERROR", "OLD-RUN-FAILED"];

export const outcomeOf = (verdict) => (verdict === "PASS" ? "pass" : INCONCLUSIVE.includes(verdict)
  ? "inconclusive" : "fail");

const DAY = 86_400_000;
export const daysBack = (now, days) => new Date(now.getTime() - days * DAY).toISOString().slice(0, 10);

// Tool versions newest first: "3.10.0" after "3.9.0", as numbers, not text.
export function newerFirst(a, b) {
  const [x, y] = [a, b].map((version) => String(version).split(".").map(Number));
  for (let at = 0; at < Math.max(x.length, y.length); at++) {
    if ((x[at] ?? 0) !== (y[at] ?? 0)) {
      return (y[at] ?? 0) - (x[at] ?? 0);
    }
  }
  return 0;
}

// Each device and suite, version by version of the tool, ever (only from
// checkouts with no changes beside their commit where `clean`): its runs, its
// first tries judged and passed, those the suite could not judge, its newest
// run and the firmware that installed, and how many of its suite's scenarios
// (`total`, that version's newest batch's) passed on that version
// (`covered`). Each version stands alone: a scenario that passed on one
// counts for no other, as a change since may have broken it. By family,
// device and suite, the newest version first.
export async function deviceTesting(all, clean = false) {
  const dirty = (table) => (clean ? `AND ${table}.dirty = 0` : "");
  const rows = await all(`SELECT r.family, r.device, r.suite, r.tool, count(*) AS runs,
      sum(r.rerun = 0 AND r.outcome != 'inconclusive') AS judged, sum(r.rerun = 0 AND r.outcome = 'pass') AS passed,
      sum(r.outcome = 'inconclusive') AS inconclusive,
      count(DISTINCT CASE WHEN r.outcome = 'pass' THEN r.scenario END) AS covered, max(r.finished) AS last,
      (SELECT l.firmware FROM emulator_runs AS l WHERE l.family = r.family AND l.device = r.device
        AND l.suite = r.suite AND l.tool = r.tool ${dirty("l")} ORDER BY l.finished DESC LIMIT 1) AS firmware,
      (SELECT b.total FROM emulator_batches AS b WHERE b.family = r.family AND b.device = r.device
        AND b.suite = r.suite AND b.tool = r.tool ${dirty("b")} ORDER BY b.started DESC LIMIT 1) AS total
    FROM emulator_runs AS r WHERE 1 = 1 ${dirty("r")}
    GROUP BY r.family, r.device, r.suite, r.tool`);
  return rows.map((row) => ({ ...row, covered: Math.min(row.covered, row.total ?? row.covered) }))
    .sort((a, b) => a.family.localeCompare(b.family) || a.device.localeCompare(b.device)
      || a.suite.localeCompare(b.suite) || newerFirst(a.tool, b.tool));
}

// The nightly fold (nightly.js): every day a run came in for since the last
// fold, before today, made again in emulator_days; then the mark moved to
// the start of today, so a run of today received before tonight is folded
// with its day tomorrow.
export function emulatorFolding(db, today) {
  return [
    db.prepare(`INSERT OR REPLACE INTO emulator_days (day, family, device, suite, runs, passed, failed, inconclusive,
        bricked, first_judged, first_passed, seconds)
      SELECT substr(finished, 1, 10) AS day, family, device, suite, count(*), sum(outcome = 'pass'),
        sum(outcome = 'fail'), sum(outcome = 'inconclusive'), sum(verdict = 'BRICK'),
        sum(rerun = 0 AND outcome != 'inconclusive'), sum(rerun = 0 AND outcome = 'pass'), sum(coalesce(seconds, 0))
      FROM emulator_runs WHERE finished < ?1 AND substr(finished, 1, 10) IN (SELECT DISTINCT substr(finished, 1, 10)
        FROM emulator_runs WHERE received >= coalesce((SELECT received FROM emulator_fold WHERE id = 1), ''))
      GROUP BY day, family, device, suite`).bind(today),
    db.prepare(`INSERT INTO emulator_fold (id, received) VALUES (1, ?1)
      ON CONFLICT (id) DO UPDATE SET received = excluded.received`).bind(`${today}T00:00:00Z`),
  ];
}
