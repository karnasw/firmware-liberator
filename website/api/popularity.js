// How popular each model is, month by month: for the admin view and the
// public figures alike (snapshot.js), so the two never count it differently.
// Each month, a model's devices:
// - asked: whose readout was first asked about (wanted_devices.first_day);
// - readOut: read out (the readout reports, a device once a month);
// - converted: converted to another firmware than Sonoff's (the run reports,
//   a device once a month - a device converted again counts again).
// The maintainer's own testing (tag "dev", and the devices it has seen) and
// quarantined reports are left out, and so is every report of a device with
// one held for review.

export const SERIES = { asked: "asked to be read out", readOut: "read out", converted: "converted" };

// Of a device, none of whose sightings is the maintainer's or held for review.
// A report naming no device stands for one of its own.
const COUNTED = `coalesce(device_hash, '') NOT IN (SELECT device_hash FROM devices WHERE dev = 1)
  AND coalesce(device_hash, '') NOT IN (SELECT reports.device_hash FROM held_reports
    JOIN reports ON reports.id = held_reports.id WHERE reports.device_hash IS NOT NULL)`;

// [{model, month, asked, readOut, converted}] from `from` ("YYYY-MM-DD") on,
// of the models `models` (SQL on `model`) allows, by model then month.
export async function popularity(all, from, models = "1") {
  return all(`SELECT model, month, sum(asked) AS asked, sum(read_out) AS readOut, sum(converted) AS converted FROM (
      SELECT model, substr(first_day, 1, 7) AS month, count(*) AS asked, 0 AS read_out, 0 AS converted
        FROM wanted_devices WHERE first_day >= ?1 AND ${COUNTED} GROUP BY model, month
      UNION ALL
      SELECT model, substr(day, 1, 7), 0, count(DISTINCT coalesce(device_hash, id)), 0 FROM readout_reports
        WHERE day >= ?1 AND tag = 'user' AND quarantined = 0 AND model IS NOT NULL AND ${COUNTED}
        GROUP BY model, substr(day, 1, 7)
      UNION ALL
      SELECT model, substr(day, 1, 7), 0, 0, count(DISTINCT coalesce(device_hash, id)) FROM reports
        WHERE day >= ?1 AND tag = 'user' AND quarantined = 0 AND model IS NOT NULL AND result = 'converted'
          AND firmware IS NOT NULL AND firmware != 'sonoff' AND ${COUNTED}
        GROUP BY model, substr(day, 1, 7))
    WHERE ${models} GROUP BY model, month ORDER BY model, month`, from);
}

// The `count` months to `now`'s, oldest first: ["2026-11", ...].
export function monthsTo(now, count) {
  return Array.from({ length: count }, (_, back) => new Date(Date.UTC(now.getUTCFullYear(),
    now.getUTCMonth() - (count - 1 - back), 1)).toISOString().slice(0, 7));
}
