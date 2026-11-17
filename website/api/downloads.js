// The tool's downloads as GitHub counts them, read each night (index.js
// scheduled) and kept by day (download_days), so the admin view and /stats can
// chart them beside the devices converted. GitHub gives only each release
// file's count so far, never by day: a day's row is what it said in the night
// after it, as the day's figures are made then (nightly.js), and a night it
// could not be read gives none, the chart going on from the last.
// - DOWNLOADS are the tool itself, summed over its releases (tags v<version>)
//   into one total: liberate.sh and liberate.ps1 fetch one for each new
//   version, as does someone downloading it from a release's page. Which
//   system a person is on is told by their reports, not by the file they
//   fetched: someone on a Mac may download the zip. The README's badge counts
//   every file of every release, the version file the one-liners read on
//   every run and the emulators' sources among them, so it says more.
// - Read with no token, as GitHub answers anyone 60 requests an hour from an
//   address: a Worker shares its addresses, so a night may be refused. Each
//   night refused is kept with why (download_misses), its rate limit named as
//   one, and DOWNLOADS_STALE_NIGHTS in a row are emailed to the maintainer
//   with the reasons (alerts.js). A rate limit that keeps coming means the
//   Worker needs a token to read with, and TOKEN_FIX, which the alert then
//   gives, is the one recommended: no token is wanted before then, as each
//   would be one more thing to look after; any other refusal no token mends.
import { ready } from "./database.js";

// github_settings.REPOSITORY's copy (tests/repo/test_site.py holds them alike).
export const REPOSITORY = "adamsthws/firmware-liberator";
// The release workflow's archives of the tool.
export const DOWNLOADS = ["autoflash.tar.gz", "autoflash.zip"];
const PER_PAGE = 100;
// Pages of releases read at most a night: a thousand releases.
export const RELEASE_PAGES = 10;
const TOOL_TAG = /^v\d+\.\d+\.\d+$/;
// Nights in a row GitHub may go unread before the maintainer is told, and told
// again each as many after: one refused now and then is no matter, as the next
// night's count has every download since.
export const DOWNLOADS_STALE_NIGHTS = 3;
// The fix the alert recommends, should GitHub keep refusing: not built until
// it is needed. A fine-grained token can be made never to expire on a
// personal account (an organisation's may cap it at 366 days), but never by
// a script, so it is pasted in once; a GitHub App would need no pasting, at
// the cost of signing in the Worker and a sign-up flow in apply.
export const TOKEN_FIX = "The recommended fix: a fine-grained GitHub personal access token that never expires, "
  + "with read-only access to public repositories and no other permission, pasted in once through "
  + "tools/repo/cloudflare_settings.py apply as a secret of the API's Worker. Not built yet: apply needs to ask for "
  + "it, and readDownloads in website/api/downloads.js to send it.";

// Why GitHub refused, {problem, rateLimited}: its rate limit, the primary
// (none of the hour's requests left) or the secondary (a wait asked for, or
// said in its message), named as such, with how many an hour it allows.
async function refusal(response) {
  const message = await response.text().then((text) => text.slice(0, 2000), () => "");
  const rateLimited = [403, 429].includes(response.status) && (response.headers.get("x-ratelimit-remaining") === "0"
    || response.headers.has("retry-after") || /rate limit/i.test(message));
  if (!rateLimited) {
    return { problem: `HTTP ${response.status}`, rateLimited: false };
  }
  const limit = Number(response.headers.get("x-ratelimit-limit"));
  return { problem: `GitHub's rate limit reached (HTTP ${response.status}${limit ? `, ${limit} requests an hour` : ""})`,
    rateLimited: true };
}

// {downloads} over the tool's releases, or {problem, rateLimited}.
export async function readDownloads(fetcher = fetch) {
  let downloads = 0;
  try {
    for (let page = 1; page <= RELEASE_PAGES; page += 1) {
      const response = await fetcher(`https://api.github.com/repos/${REPOSITORY}/releases?per_page=${PER_PAGE}&page=${page}`, {
        // GitHub refuses a request with no User-Agent.
        headers: { Accept: "application/vnd.github+json", "User-Agent": "firmware-liberator-api" },
        signal: AbortSignal.timeout(20_000),
      });
      if (!response.ok) {
        return await refusal(response);
      }
      const releases = await response.json();
      if (!Array.isArray(releases)) {
        return { problem: "not a list of releases", rateLimited: false };
      }
      for (const release of releases) {
        if (!TOOL_TAG.test(release?.tag_name ?? "") || release.draft) {
          continue;
        }
        for (const asset of release.assets ?? []) {
          if (DOWNLOADS.includes(asset?.name) && Number.isInteger(asset.download_count)) {
            downloads += asset.download_count;
          }
        }
      }
      if (releases.length < PER_PAGE) {
        return { downloads };
      }
    }
    return { problem: `more than ${RELEASE_PAGES} pages of releases`, rateLimited: false };
  } catch (error) {
    return { problem: String(error?.message ?? error).slice(0, 200), rateLimited: false };
  }
}

// Tonight's count kept for the day just ended, or why not kept for it
// (download_misses; a count read again the same night takes its place):
// "taken", or why not. The nightly cron's, before the figures are made
// (index.js scheduled).
export async function refreshDownloads(env, now = new Date(), fetcher = fetch) {
  if (!(await ready(env.DB))) {
    return "the database does not have every migration yet";
  }
  const got = await readDownloads(fetcher);
  const day = new Date(Date.parse(`${now.toISOString().slice(0, 10)}T00:00:00Z`) - 86_400_000).toISOString().slice(0, 10);
  if (got.downloads === undefined) {
    await env.DB.prepare("INSERT OR REPLACE INTO download_misses (day, problem, rate_limited) VALUES (?, ?, ?)")
      .bind(day, got.problem, got.rateLimited ? 1 : 0).run();
    return got.problem;
  }
  await env.DB.batch([
    env.DB.prepare("INSERT OR REPLACE INTO download_days (day, downloads) VALUES (?, ?)").bind(day, got.downloads),
    env.DB.prepare("DELETE FROM download_misses WHERE day = ?").bind(day),
  ]);
  return "taken";
}

// Each day read, oldest first, from `since` (a day): its downloads, and the
// devices converted by then, of the devices `where` keeps.
// `all` runs a query (snapshot.js's, the admin view's).
export async function downloadDays(all, since, where) {
  const days = await all("SELECT day, downloads FROM download_days WHERE day >= ? ORDER BY day", since);
  if (!days.length) {
    return [];
  }
  const converted = await all(`SELECT converted_day AS day, count(*) AS n FROM devices
    WHERE converted_day IS NOT NULL AND converted_day <= ? AND ${where} GROUP BY converted_day ORDER BY converted_day`,
  days.at(-1).day);
  let at = 0;
  let sum = 0;
  return days.map((day) => {
    for (; at < converted.length && converted[at].day <= day.day; at += 1) {
      sum += converted[at].n;
    }
    return { day: day.day, downloads: day.downloads, converted: sum };
  });
}
