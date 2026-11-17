// What the build made, as search engines and AI tools read it. Run after
// `npm run build` (`npm run check`; the website workflow does, before it
// deploys), so an upgrade of Astro or its sitemap that changes it fails here.
// Not under test/, which `npm test` runs before anything is built.
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { EXTRA_PAGES, PAGES, SITE, retired, slug } from "../src/site.mjs";

const DIST = fileURLToPath(new URL("../dist/", import.meta.url));
const read = (path) => readFileSync(DIST + path, "utf-8");
// A page's address, and the file the build makes for it ("file" format):
// each Markdown page's, and those built from none (EXTRA_PAGES).
const addresses = [...PAGES.map(slug), ...EXTRA_PAGES];
const file = (address) => `${address || "index"}.html`;
const locs = (xml) => [...xml.matchAll(/<loc>([^<]+)<\/loc>/g)].map((match) => match[1]);
const local = (url) => {
  assert.ok(url.startsWith(`${SITE}/`), url);
  return url.slice(SITE.length + 1);
};

test("the sitemap lists every page but the retired ones, and nothing else", () => {
  const listed = locs(read("sitemap-index.xml")).flatMap((sitemap) => locs(read(local(sitemap))));
  const wanted = addresses.filter((address) => !retired(address)).map((address) => `${SITE}/${address}`);
  assert.deepEqual([...listed].sort(), [...wanted].sort());
});

test("each page names its own address, is titled and described, and only a retired one is noindex", () => {
  for (const address of addresses) {
    const html = read(file(address));
    const canonical = [...html.matchAll(/<link rel="canonical" href="([^"]*)"/g)].map((match) => match[1]);
    assert.deepEqual(canonical, [`${SITE}/${address}`], address);
    assert.match(html, /<title>[^<]+<\/title>/, address);
    assert.match(html, /<meta name="description" content="[^"]+"/, address);
    assert.equal(html.includes('<meta name="robots" content="noindex">'), retired(address), address);
  }
});

test("the 404 page is kept out of search results", () => {
  assert.ok(read("404.html").includes('<meta name="robots" content="noindex">'));
});

test("robots.txt lets every crawler in and names the sitemap the build made", () => {
  const robots = read("robots.txt");
  assert.match(robots, /^User-agent: \*$/m);
  assert.doesNotMatch(robots, /^Disallow: *\/\S*$/m);
  const sitemaps = [...robots.matchAll(/^Sitemap: (\S+)$/gm)].map((match) => local(match[1]));
  assert.deepEqual(sitemaps, ["sitemap-index.xml"]);
  assert.ok(existsSync(DIST + sitemaps[0]));
});

test("llms.txt is served", () => {
  assert.match(read("llms.txt"), /^# /);
});

test("llms.txt's figures, where it has them, say how fresh they are, and are", () => {
  const llms = read("llms.txt");
  const found = /^## Figures\n\nAs of (\d{4}-\d{2}-\d{2}) \(UTC\); rebuilt daily/m.exec(llms);
  // The daily build (REQUIRE_STATS) must have them: the figures are made each night.
  assert.ok(found || !process.env.REQUIRE_STATS, "no figures, where the daily build must have them");
  if (found) {
    assert.ok(Date.now() - Date.parse(`${found[1]}T00:00:00Z`) <= 4 * 86_400_000, `figures as of ${found[1]}`);
    assert.equal(llms.indexOf("## Figures"), llms.lastIndexOf("## Figures"), "once");
  }
});

test("the stats page has its figures, where the daily build must", () => {
  const page = read("stats.html");
  assert.ok(!process.env.REQUIRE_STATS || page.includes('class="tile hero"'), "no figures on /stats");
  assert.match(page, /<script src="\/assets\/stats\.js" defer><\/script>/);
  assert.doesNotMatch(page, /ENOENT|could not be read|answered \d{3}|\/tmp\/|\/home\//, "no build detail on the page");
});
