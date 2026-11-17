// The maintainer's email from the nightly job (nightly.js), when something
// needs looking at: yesterday's limit for everyone reached - of readouts, by
// count or by bytes, of reports, or of addresses given - or the store of
// readouts past STORE_WARN of its limit, the night it passes it. Counts only:
// never a report, a readout, an address or anything of a sender. Sent with the
// Worker's EMAIL binding (Cloudflare Email Service) to ALERTS_RECIPIENT_EMAIL, a secret set
// by tools/repo/cloudflare_settings.py apply from the maintainer's Email
// Routing destination; with either missing, nothing is sent. Yesterday's
// emails that failed to send are a line too (mail.js), but for those to an
// address on the suppression list, which are expected now and then. And a
// list of hosts' and VPNs' networks gone untaken a week, or taken far longer
// than the one kept (networks.js). And the tool's downloads gone unread from
// GitHub for DOWNLOADS_STALE_NIGHTS nights in a row, with why each night
// (downloads.js) - and the token recommended only where its rate limit was why.
import { read } from "./counters.js";
import { DOWNLOADS_STALE_NIGHTS, TOKEN_FIX } from "./downloads.js";
import { SUPPRESSED, failedOn, sendMail } from "./mail.js";
import { LIST_JUMP, LIST_STALE_DAYS } from "./networks.js";

export const STORE_WARN = 0.75;

const DAY = 86_400_000;
const dayBefore = (day) => new Date(Date.parse(`${day}T00:00:00Z`) - DAY).toISOString().slice(0, 10);
const megabytes = (bytes) => `${Math.round(bytes / 1024 / 1024)} MB`;

// What to say, as lines: [] when all is well. `limits` as index.js gives
// them (its scheduled()): DAILY_COUNT, DAILY_BYTES, TOTAL_BYTES, NOTIFY_DAILY,
// and REPORTS_DAILY.
export async function alertLines(db, today, limits) {
  const yesterday = dayBefore(today);
  const lines = [];
  const readouts = await read(db, `readouts:${yesterday}`);
  if (readouts.count >= limits.DAILY_COUNT || readouts.bytes >= limits.DAILY_BYTES) {
    lines.push(`The day's limit of readouts was reached on ${yesterday}: ${readouts.count} of ${limits.DAILY_COUNT}, `
      + `${megabytes(readouts.bytes)} of ${megabytes(limits.DAILY_BYTES)}.`);
  }
  const reports = await read(db, `reports:${yesterday}`);
  if (reports.count >= limits.REPORTS_DAILY) {
    lines.push(`The day's limit of reports was reached on ${yesterday}: ${reports.count} of ${limits.REPORTS_DAILY}.`);
  }
  const notify = await read(db, `notify:${yesterday}`);
  if (notify.count >= limits.NOTIFY_DAILY) {
    lines.push(`The day's limit of addresses given was reached on ${yesterday}: `
      + `${notify.count} of ${limits.NOTIFY_DAILY}.`);
  }
  const failed = Object.entries(await failedOn(db, yesterday)).filter(([code]) => code !== SUPPRESSED);
  if (failed.length) {
    lines.push(`Emails that could not be sent on ${yesterday}: `
      + `${failed.map(([code, n]) => `${n} ${code}`).join(", ")} (Cloudflare's Email Sending activity log has each).`);
  }
  const { results } = await db.prepare("SELECT day, bytes FROM store_days WHERE day IN (?, ?)")
    .bind(today, yesterday).all();
  const held = Object.fromEntries(results.map((row) => [row.day, row.bytes]));
  const warn = STORE_WARN * limits.TOTAL_BYTES;
  if ((held[today] ?? 0) >= warn && !((held[yesterday] ?? 0) >= warn)) {
    lines.push(`The store of readouts holds ${megabytes(held[today])} of ${megabytes(limits.TOTAL_BYTES)}, `
      + `past ${Math.round(STORE_WARN * 100)}% (.github/readouts.md says how to make room).`);
  }
  // A list of hosts' and VPNs' networks (networks.js) not taken for
  // LIST_STALE_DAYS, and each LIST_STALE_DAYS after: told weekly, not nightly.
  const { results: lists } = await db.prepare("SELECT name, taken, problem FROM network_lists WHERE problem IS NOT NULL")
    .all();
  for (const list of lists) {
    const days = Math.round((Date.parse(`${today}T00:00:00Z`) - Date.parse(`${list.taken}T00:00:00Z`)) / DAY);
    if (days >= LIST_STALE_DAYS && days % LIST_STALE_DAYS === 0) {
      lines.push(`The ${list.name} list of networks that count toward no agreement (website/api/networks.js) has `
        + `not been taken since ${list.taken}: ${list.problem}.`);
    }
  }
  const { results: grown } = await db.prepare(`SELECT name, previous, json_array_length(asns) AS now, added
    FROM network_lists WHERE taken = ?1 AND problem IS NULL AND previous > 0
    AND json_array_length(asns) > ?2 * previous`).bind(today, 1 + LIST_JUMP).all();
  for (const list of grown) {
    lines.push(`The ${list.name} list of networks that count toward no agreement (website/api/networks.js) grew `
      + `from ${list.previous} to ${list.now} today. Among those added: ${JSON.parse(list.added).map((asn) => `AS${asn}`)
        .join(", ")}. A home internet provider among them would cost its customers their vote.`);
  }
  // The tool's downloads not read for DOWNLOADS_STALE_NIGHTS nights, and each
  // DOWNLOADS_STALE_NIGHTS after: a night read keeps yesterday (downloads.js),
  // so the nights missed are counted from the last day kept - or, never read,
  // as if read the night before the first the job ran (store_days, written
  // each night before this).
  const { results: [downloads] } = await db.prepare(`SELECT (SELECT max(day) FROM download_days) AS last,
    (SELECT min(day) FROM store_days) AS first`).all();
  const since = downloads.last ?? (downloads.first && dayBefore(dayBefore(downloads.first)));
  const missed = since ? Math.round((Date.parse(`${yesterday}T00:00:00Z`) - Date.parse(`${since}T00:00:00Z`)) / DAY) : 0;
  if (missed >= DOWNLOADS_STALE_NIGHTS && missed % DOWNLOADS_STALE_NIGHTS === 0) {
    const { results: reasons } = await db.prepare(`SELECT problem, max(rate_limited) AS limited, count(*) AS n
      FROM download_misses WHERE day > ? AND day <= ? GROUP BY problem ORDER BY n DESC, problem`).bind(since, yesterday).all();
    const nights = (n) => `${n} ${n === 1 ? "night" : "nights"}`;
    const told = reasons.reduce((sum, row) => sum + row.n, 0);
    const why = [...reasons.map((row) => `${row.problem} (${nights(row.n)})`),
      // A night the job stopped before keeping why: its log may say.
      ...(told < missed ? [`no reason kept (${nights(missed - told)}; the Worker's logs may say)`] : [])].join("; ");
    lines.push((`GitHub has not given the tool's downloads for ${missed} nights in a row`
      + `${downloads.last ? `, the last kept for ${downloads.last}` : ", none ever"} (website/api/downloads.js). Why: ${why}. `
      + (reasons.some((row) => row.limited)
        ? `GitHub's rate limit for a request with no token is shared by every Worker on the same addresses. ${TOKEN_FIX}`
        : reasons.length ? "None was GitHub's rate limit, so a token would not help: look at what it said." : "")).trim());
  }
  return lines;
}

// Sends the lines, if any: what was done, for the nightly job's answer.
export async function alert(env, today, limits) {
  const lines = await alertLines(env.DB, today, limits);
  if (!lines.length) {
    return { sent: false, lines };
  }
  if (!env.EMAIL || !env.ALERTS_RECIPIENT_EMAIL) {
    return { sent: false, lines, why: "no EMAIL binding or ALERTS_RECIPIENT_EMAIL secret" };
  }
  const mailed = await sendMail(env, {
    to: env.ALERTS_RECIPIENT_EMAIL,
    from: limits.from,
    subject: `Firmware Liberator's API: ${lines.length === 1 ? "one thing" : `${lines.length} things`} to look at`,
    text: [...lines, "", "The admin view has the detail. This was sent by the API's nightly job."].join("\n"),
  }, today);
  if (!mailed.sent) {
    return { sent: false, lines, why: `the email could not be sent: ${mailed.error}` };
  }
  return { sent: true, lines };
}
