// What the API keeps only for a while (index.js), and the nightly job's
// clearing of it (prunePending, called from nightly.js):
// - Readouts not taken as right - pending, with no three agreeing and no
//   approval, or rejected - are deleted PENDING_DAYS after they came in, with
//   the marks of the offered files they held, giving back their bytes and
//   places, so made-up readouts cannot fill the store or a key's places. But
//   for one disputing readouts taken as right (checked, index.js CHECK_DAYS):
//   kept until the maintainer settles it, so a dispute never goes unseen. A
//   key holds MAX_PLACES at most, so these cannot fill the store either.
// - Addresses given to /v1/notify and never confirmed are deleted
//   SIGNUP_DAYS after they were given.
import { release } from "./counters.js";

// Half a year: a rare device's owners may come months apart, and a readout
// deleted before the next arrives never meets one to agree with.
export const PENDING_DAYS = 180;
export const SIGNUP_DAYS = 7;
// The count of bytes held by readouts not yet taken as right (counters.js).
export const PENDING = "readouts:pending";
// The name index.js counts every readout's bytes under (READOUTS_EVER).
export const READOUTS_EVER = "readouts:all";
// Readouts deleted a night at most: four or five queries each, well within a
// request's allowance; any more go the next night.
export const PRUNE_EACH_NIGHT = 40;

const DAY = 24 * 60 * 60 * 1000;
const dayBefore = (today, days) => new Date(Date.parse(`${today}T00:00:00Z`) - days * DAY).toISOString();

// Clears what has been kept too long, as of `today` (YYYY-MM-DD): how many
// readouts and sign-ups it deleted.
export async function prunePending(env, today) {
  const old = (await env.DB.prepare(
    `SELECT id, object, bytes, review FROM readouts WHERE review IN ('pending', 'rejected') AND received < ?1
     AND NOT (review = 'pending' AND checked = 1) ORDER BY received LIMIT ?2`,
  ).bind(dayBefore(today, PENDING_DAYS), PRUNE_EACH_NIGHT).all()).results;
  for (const row of old) {
    const marks = (await env.DB.prepare("SELECT sha FROM readout_firmware WHERE readout = ?").bind(row.id).all())
      .results;
    for (const mark of marks) {
      await env.READOUTS.delete(`readouts/firmware/${mark.sha}`);
    }
    await env.READOUTS.delete(row.object);
    await env.DB.batch([
      env.DB.prepare("DELETE FROM readout_firmware WHERE readout = ?").bind(row.id),
      env.DB.prepare("DELETE FROM readout_networks WHERE id = ?").bind(row.id),
      env.DB.prepare("DELETE FROM readouts WHERE id = ?").bind(row.id),
    ]);
    await release(env.DB, READOUTS_EVER, row.bytes);
    if (row.review === "pending") {
      await release(env.DB, PENDING, row.bytes);
    }
  }
  const signups = await env.DB.prepare("DELETE FROM confirmations WHERE day < ?1").bind(
    dayBefore(today, SIGNUP_DAYS).slice(0, 10)).run();
  return { readouts: old.length, signups: signups.meta?.changes ?? 0 };
}
