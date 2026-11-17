// The counts the API's limits are held to, in the database's counters table
// (migrations/0001_initial.sql): one row a name, each changed in a single
// statement, so two requests at once can never both pass a limit.

// `by` more of `name` (one, by default), adding `bytes`, if that keeps it
// within `most` and `mostBytes`: whether it did. All or none: never part.
// `day` (YYYY-MM-DD) marks a day's count, which the nightly job deletes
// later; null keeps it for good.
export async function reserve(db, name, day, { by = 1, bytes = 0, most = Number.MAX_SAFE_INTEGER,
  mostBytes = Number.MAX_SAFE_INTEGER } = {}) {
  if (!(Number.isInteger(by) && by >= 1) || by > most || bytes > mostBytes) {
    return false;
  }
  const row = await db.prepare(
    `INSERT INTO counters (name, day, count, bytes) VALUES (?1, ?2, ?3, ?4)
     ON CONFLICT (name) DO UPDATE SET count = count + excluded.count, bytes = bytes + excluded.bytes
     WHERE count + excluded.count <= ?5 AND bytes + excluded.bytes <= ?6
     RETURNING count`).bind(name, day, by, bytes, most, mostBytes).first();
  return row !== null;
}

// As many of `wanted` as still fit within `most`, reserved: how many. At
// most two tries, so a request costs a fixed few queries however busy it is.
export async function reserveUpTo(db, name, day, wanted, most) {
  if (await reserve(db, name, day, { by: wanted, most })) {
    return wanted;
  }
  const room = most - (await read(db, name)).count;
  if (room >= 1 && room < wanted && (await reserve(db, name, day, { by: room, most }))) {
    return room;
  }
  return 0;
}

// Gives back `by` reserved (one, by default), and their bytes: for those
// reserved and then not kept.
export async function release(db, name, bytes = 0, by = 1) {
  await db.prepare(
    "UPDATE counters SET count = max(count - ?3, 0), bytes = max(bytes - ?2, 0) WHERE name = ?1",
  ).bind(name, bytes, by).run();
}

// {count, bytes} of `name`: zeroes for one never counted.
export async function read(db, name) {
  const row = await db.prepare("SELECT count, bytes FROM counters WHERE name = ?").bind(name).first();
  return { count: row?.count ?? 0, bytes: row?.bytes ?? 0 };
}
