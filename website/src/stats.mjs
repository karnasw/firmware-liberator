// The public figures the /stats page is built from: the snapshot the API's
// nightly job writes (website/api/snapshot.js), fetched once a build, or a
// file of them (STATS_FILE: made with website/scripts/sample-stats.mjs, to
// build the page without the live ones). The page shows them however old, with
// their date; llms.txt carries the headline only while they are FRESH_DAYS old
// or less, so a figure an AI tool repeats is never a stale one without its date.
import { readFileSync } from "node:fs";
import schema from "../api/report-schema.json" with { type: "json" };

export const STATS_URL = "https://stats.firmwareliberator.com/v1/stats.json";
export const FRESH_DAYS = 3;

const DAY = 86_400_000;
const names = schema.deviceNames;
let loaded = null;

// {figures, fresh, why}: the figures, or null with why there are none.
export function loadStats(now = new Date()) {
  loaded ??= read(now);
  return loaded;
}

async function read(now) {
  let figures;
  try {
    if (process.env.STATS_FILE) {
      figures = JSON.parse(readFileSync(process.env.STATS_FILE, "utf8"));
    } else {
      const response = await fetch(STATS_URL, { signal: AbortSignal.timeout(20_000) });
      if (!response.ok) {
        return { figures: null, fresh: false, why: `the figures answered ${response.status}` };
      }
      figures = await response.json();
    }
  } catch (error) {
    return { figures: null, fresh: false, why: `the figures could not be read: ${error.message}` };
  }
  if (figures?.format !== 1 || !/^\d{4}-\d{2}-\d{2}$/.test(figures.through ?? "")) {
    return { figures: null, fresh: false, why: "the figures are not of a format this page knows" };
  }
  return { figures, fresh: isFresh(figures, now), why: null };
}

export const isFresh = (figures, now = new Date()) =>
  now.getTime() - Date.parse(`${figures.through}T00:00:00Z`) <= (FRESH_DAYS + 1) * DAY;

// The headline figures as llms.txt's "## Figures" section, or "" when there is
// none fresh to give.
export function llmsFigures({ figures, fresh }) {
  if (!figures || !fresh) {
    return "";
  }
  const { devices, runs } = figures;
  const converted = figures.kinds.filter((row) => names[row.device]);
  const lines = [
    devices.converted !== null && `- ${devices.converted.toLocaleString("en")} devices converted, of ${devices.seen.toLocaleString("en")} seen.`,
    devices.firstRun !== null && `- ${devices.firstRun}% of devices converted on their first run; ${devices.eventually}% eventually.`,
    runs.success !== null && `- ${runs.success}% of conversion runs succeeded (${runs.runs.toLocaleString("en")} runs).`,
    // The devices the tool converts, by kind: one it does not yet is in "wanted", not here.
    converted.length && `- Most converted: ${converted.slice(0, 3).map((row) => names[row.device]).join(", ")}.`,
  ].filter(Boolean);
  if (!lines.length) {
    return "";
  }
  return [
    "## Figures",
    "",
    `As of ${figures.through} (UTC); rebuilt daily from the latest figures. Current figures:`,
    `[stats](https://firmwareliberator.com/stats); data: [stats.json](${STATS_URL}) (${figures.licence}).`,
    "",
    ...lines,
    "",
  ].join("\n");
}
