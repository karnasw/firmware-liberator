// Every email the API sends - index.js's confirmations and reminders,
// alerts.js's alerts - through Cloudflare Email Service's EMAIL binding,
// counted by what came of it, in the database's counters, for the admin view
// and the nightly alert: mail-sent:<day>, or mail-failed:<code>:<day> with
// Email Service's error code (E_RATE_LIMIT_EXCEEDED, say). Kept COUNTER_DAYS
// (nightly.js), as every day's count is. A bounce or a complaint comes later,
// and is never seen here: tools/repo/api_health.py mail reads those, daily.
import { reserve } from "./counters.js";

// What send() throws for an address on the account's suppression list: one
// that bounced for good, or whose owner marked an email of ours as spam.
// Nothing is sent to it, and nothing is to be tried again.
export const SUPPRESSED = "E_RECIPIENT_SUPPRESSED";
const CODE = /\bE_[A-Z_]+\b/;

// Sends `message`: {sent: true}, or {sent: false, code, error} with what was
// thrown. Never throws.
export async function sendMail(env, message, day) {
  try {
    await env.EMAIL.send(message);
  } catch (error) {
    const code = CODE.exec(String(error?.code ?? ""))?.[0] ?? CODE.exec(String(error?.message ?? error))?.[0] ?? "OTHER";
    await reserve(env.DB, `mail-failed:${code}:${day}`, day);
    return { sent: false, code, error: String(error) };
  }
  await reserve(env.DB, `mail-sent:${day}`, day);
  return { sent: true };
}

// `day`'s emails that failed, by code: {code: count}.
export async function failedOn(db, day) {
  const { results } = await db.prepare("SELECT name, count FROM counters WHERE name GLOB 'mail-failed:*:' || ?1")
    .bind(day).all();
  return Object.fromEntries(results.filter((row) => row.count > 0)
    .map((row) => [row.name.slice("mail-failed:".length, -(day.length + 1)), row.count]));
}
