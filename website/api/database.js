// The API's D1 database (DB in wrangler.jsonc). Made and brought up to date
// by tools/repo/cloudflare_settings.py apply, with the maintainer's token,
// never by CI, as D1 permissions reach every database in the account: it
// runs each file in migrations/ it has not run yet, in the order of their
// names, and records each in d1_migrations, as wrangler would.
// - A migration, once committed, is never changed: a change is a new file,
//   with the next number (0002_..., ...).
// - Each can be run again harmlessly (CREATE ... IF NOT EXISTS): running one
//   over the API is not atomic, so one cut off part way is run again.
// - Each only adds (a table, a column, an index): the Worker deployed before
//   a migration is applied, or after, must still work.

// The newest migration this code needs (test/database.test.mjs holds it to
// the newest file in migrations/).
export const LATEST_MIGRATION = "0001_initial.sql";

// Whether the database has every migration this code needs, asked at most
// once a minute in a Worker: false until `apply` has run the newest, so a
// Worker deployed first answers 503 rather than failing on a missing table.
const checked = new WeakMap();

export async function ready(db, now = Date.now()) {
  const last = checked.get(db);
  if (last && last.ready && now - last.at < 60_000) {
    return true;
  }
  let found = null;
  try {
    found = await db.prepare("SELECT name FROM d1_migrations WHERE name = ?").bind(LATEST_MIGRATION).first("name");
  } catch {
    found = null;   // no d1_migrations: never migrated
  }
  checked.set(db, { at: now, ready: found === LATEST_MIGRATION });
  return found === LATEST_MIGRATION;
}
