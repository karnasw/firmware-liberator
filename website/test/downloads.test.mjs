// api/downloads.js: the tool's downloads, as GitHub counts them, kept by day.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { d1 } from "../api/local-d1.mjs";
import { DOWNLOADS, downloadDays, readDownloads, refreshDownloads, RELEASE_PAGES, REPOSITORY } from "../api/downloads.js";

const NOW = new Date("2026-11-14T03:17:00Z");
const release = (tag, counts, more = {}) => ({ tag_name: tag, draft: false, ...more,
  assets: Object.entries(counts).map(([name, download_count]) => ({ name, download_count })) });
// A fetch answering GitHub's releases a page at a time, `pages` in turn: a
// list, an HTTP status, a Response, or an Error thrown.
const github = (...pages) => {
  const asked = [];
  const fetcher = async (url, init) => {
    asked.push({ url, init });
    const page = Number(new URL(url).searchParams.get("page"));
    const answer = pages[page - 1] ?? [];
    if (answer instanceof Error) {
      throw answer;
    }
    return answer instanceof Response ? answer
      : typeof answer === "number" ? new Response("", { status: answer }) : Response.json(answer);
  };
  return Object.assign(fetcher, { asked });
};
const kept = (bindings) => bindings.DB.db.prepare("SELECT day, downloads FROM download_days ORDER BY day")
  .all().map((row) => ({ ...row }));

test("the tool's archives are counted as one total, over its own releases alone", async () => {
  const fetcher = github([
    release("v2.3.0", { "autoflash.tar.gz": 23, "autoflash.zip": 86, "autoflash-version": 192, "liberate.sh": 43 }),
    release("emulator-source-qemu-64aed50", { "autoflash.zip": 1000 }),
    release("v2.2.0", { "autoflash.tar.gz": 4, "autoflash.zip": 1 }),
    release("v9.9.9", { "autoflash.zip": 500 }, { draft: true }),
  ]);
  assert.deepEqual(await readDownloads(fetcher), { downloads: 114 });
  assert.equal(fetcher.asked.length, 1, "a page short of full is the last");
  const [{ url, init }] = fetcher.asked;
  assert.ok(url.startsWith(`https://api.github.com/repos/${REPOSITORY}/releases?`), url);
  assert.ok(init.headers["User-Agent"], "GitHub refuses a request with none");
});

test("every page of releases is read, up to RELEASE_PAGES", async () => {
  const full = Array.from({ length: 100 }, (_, at) => release(`v1.0.${at}`, { "autoflash.zip": 1 }));
  assert.deepEqual(await readDownloads(github(full, [release("v0.9.0", { "autoflash.tar.gz": 2 })])),
    { downloads: 102 });
  assert.deepEqual(await readDownloads(github(...Array(RELEASE_PAGES).fill(full))),
    { problem: `more than ${RELEASE_PAGES} pages of releases`, rateLimited: false });
});

const missed = (bindings) => bindings.DB.db.prepare("SELECT day, problem, rate_limited FROM download_misses ORDER BY day")
  .all().map((row) => ({ ...row }));

test("GitHub's rate limit is named as such, primary or secondary; any other refusal by its status", async () => {
  const refused = (status, headers = {}, body = "") => readDownloads(github(new Response(body, { status, headers })));
  assert.deepEqual(await refused(403, { "x-ratelimit-remaining": "0", "x-ratelimit-limit": "60" },
    '{"message":"API rate limit exceeded for 192.0.2.50."}'),
  { problem: "GitHub's rate limit reached (HTTP 403, 60 requests an hour)", rateLimited: true });
  assert.deepEqual(await refused(429, { "retry-after": "60" }),
    { problem: "GitHub's rate limit reached (HTTP 429)", rateLimited: true }, "the secondary: a wait asked for");
  assert.deepEqual(await refused(403, {}, '{"message":"You have exceeded a secondary rate limit."}'),
    { problem: "GitHub's rate limit reached (HTTP 403)", rateLimited: true }, "or said in its message");
  assert.deepEqual(await refused(403, { "x-ratelimit-remaining": "41" }, '{"message":"Forbidden"}'),
    { problem: "HTTP 403", rateLimited: false }, "a 403 with requests left is no rate limit");
  assert.deepEqual(await refused(404, { "x-ratelimit-remaining": "0" }), { problem: "HTTP 404", rateLimited: false });
  assert.deepEqual(await refused(503), { problem: "HTTP 503", rateLimited: false });
});

test("a night GitHub is not read keeps no count, but why, for the day it would have kept", async () => {
  const bindings = { DB: d1() };
  const later = new Date("2026-11-15T03:17:00Z");
  const limited = new Response("", { status: 403, headers: { "x-ratelimit-remaining": "0", "x-ratelimit-limit": "60" } });
  assert.equal(await refreshDownloads(bindings, NOW, github(limited)),
    "GitHub's rate limit reached (HTTP 403, 60 requests an hour)");
  assert.equal(await refreshDownloads(bindings, later, github({ message: "Not Found" })), "not a list of releases");
  assert.deepEqual(kept(bindings), []);
  assert.deepEqual(missed(bindings), [
    { day: "2026-11-13", problem: "GitHub's rate limit reached (HTTP 403, 60 requests an hour)", rate_limited: 1 },
    { day: "2026-11-14", problem: "not a list of releases", rate_limited: 0 }]);
  assert.equal(await refreshDownloads(bindings, later, github(new Error("timed out"))), "timed out");
  assert.deepEqual(missed(bindings).at(-1), { day: "2026-11-14", problem: "timed out", rate_limited: 0 },
    "the night's latest why");
  assert.equal(await refreshDownloads(bindings, later, github([release("v1.0.0", { "autoflash.zip": 5 })])), "taken");
  assert.deepEqual(missed(bindings).map((row) => row.day), ["2026-11-13"], "read after all: not missed");
});

test("each night's counts are kept for the day just ended, read again the same night replacing them", async () => {
  const bindings = { DB: d1() };
  assert.equal(await refreshDownloads(bindings, NOW, github([release("v1.0.0", { "autoflash.zip": 5 })])), "taken");
  assert.equal(await refreshDownloads(bindings, NOW, github([release("v1.0.0", { "autoflash.zip": 6 })])), "taken");
  assert.deepEqual(kept(bindings), [{ day: "2026-11-13", downloads: 6 }]);
});

test("each day read gives its downloads beside the devices converted by then", async () => {
  const bindings = { DB: d1() };
  const read = bindings.DB.db.prepare("INSERT INTO download_days (day, downloads) VALUES (?, ?)");
  for (const [day, downloads] of [["2026-11-01", 3], ["2026-11-03", 8], ["2026-11-04", 13]]) {
    read.run(day, downloads);
  }
  const device = bindings.DB.db.prepare(`INSERT INTO devices (device_hash, first_day, last_day, first_result,
    converted_day, runs, conversions, back_to_sonoff, dev) VALUES (?, '2026-10-01', '2026-11-04', 'converted', ?, 1, ?, 0, ?)`);
  device.run("a", "2026-10-20", 1, 0);   // before the first day read
  device.run("b", "2026-11-02", 1, 0);   // between two days read
  device.run("c", "2026-11-02", 1, 1);   // the maintainer's own
  device.run("d", null, 0, 0);           // never converted
  device.run("e", "2026-11-05", 1, 0);   // after the last
  const all = async (sql, ...values) => (await bindings.DB.prepare(sql).bind(...values).all()).results;
  assert.deepEqual(await downloadDays(all, "", "dev = 0"), [
    { day: "2026-11-01", downloads: 3, converted: 1 },
    { day: "2026-11-03", downloads: 8, converted: 2 },
    { day: "2026-11-04", downloads: 13, converted: 2 },
  ]);
  assert.deepEqual((await downloadDays(all, "2026-11-04", "dev = 0")).map((row) => row.day), ["2026-11-04"]);
  assert.deepEqual(await downloadDays(async (sql) => (sql.includes("download_days") ? [] : assert.fail(sql)), "", "1"), [],
    "no day read, no devices asked for");
});

test("the files counted are the archives the release workflow uploads, and the one-liners fetch", () => {
  const workflow = readFileSync(new URL("../../.github/workflows/release.yml", import.meta.url), "utf8");
  for (const file of DOWNLOADS) {
    assert.match(workflow, new RegExp(`-o ${file.replace(/\./g, "\\.")} `), file);
  }
  const fetched = ["liberate.sh", "liberate.ps1"].map((name) =>
    readFileSync(new URL(`../../${name}`, import.meta.url), "utf8")).join("\n");
  assert.deepEqual(DOWNLOADS.filter((file) => !fetched.includes(`/${file}`)), []);
});
