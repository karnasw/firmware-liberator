// What the site serves, and where: the repository's Markdown pages, each at
// an address made from its path, and how a link between them is rewritten.
import { execFileSync } from "node:child_process";
import { globSync } from "node:fs";
import { dirname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

export const REPOSITORY = "adamsthws/firmware-liberator";
export const SITE = "https://firmwareliberator.com";
export const ROOT = fileURLToPath(new URL("../../", import.meta.url));

// Every Markdown file git tracks, so a link to one never leaves the site
// and nothing untracked is ever built in. Nothing under .github/: GitHub's
// own templates, and the pages for the maintainer alone.
// Everything else - the tool, a config, LICENSE - is linked to on GitHub.
export const PAGES = execFileSync("git", ["ls-files", "-z", "--", "*.md"], { cwd: ROOT, encoding: "utf-8" })
  .split("\0")
  .filter((path) => path && !path.startsWith(".github/"));

// Pictures the pages show, copied beside them.
export const PICTURES = ["docs/*.svg", "docs/*.png"];
// The pages built from no Markdown file, by their address: checks/built.mjs
// holds them to what it holds every page to.
export const EXTRA_PAGES = ["stats", "notify"];

export const pages = () => new Set(PAGES);

// docs/emulator.md -> docs/emulator, devices/sonoff-s61/README.md ->
// devices/sonoff-s61, README.md -> "".
export function slug(path) {
  const page = path.replace(/\.md$/, "");
  return page === "README" ? "" : page.replace(/\/README$/, "");
}

// A built page's address as readers use it: Astro's "file" format calls the
// front page /index, and the Worker redirects from .html, the form the old
// GitHub Pages site's addresses took.
export const address = (pathname) => pathname.replace(/\.html$/, "").replace(/\/index$/, "/");

// Retired work stays readable but out of search results: a search must not
// land someone on a superseded procedure for a mains-voltage device. They are
// marked noindex and left out of the sitemap, and never blocked in
// robots.txt: a crawler kept out would not see the noindex.
export const retired = (address) => /^\/?archive(\/|$)/.test(address);

// What a search result says of a page: its first paragraph as plain text, cut
// to the length results show. Markdown can't carry a description of its own,
// as front matter would show on GitHub.
export function description(markdown, limit = 160) {
  const paragraph = markdown
    .replace(/^#{1,6} .*$/gm, "")
    .split(/\n\s*\n/)
    .map((block) => block.trim())
    .find((block) => block && !/^(\||```|<|!\[|\[!\[|[-*] |\d+\. )/.test(block));
  if (!paragraph) return undefined;
  const text = paragraph
    .replace(/^>\s?/gm, "")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[*`]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/:$/, ".");
  if (text.length <= limit) return text;
  const cut = text.slice(0, limit);
  const sentence = cut.lastIndexOf(". ");
  return sentence >= limit / 2 ? cut.slice(0, sentence + 1) : `${cut.slice(0, cut.lastIndexOf(" "))}…`;
}

// A link in the page at `from` (a repository path), as the site needs it:
// another page by its address, a picture as it is, anything else on GitHub.
export function rewrite(href, from, onSite) {
  if (/^([a-z][a-z0-9+.-]*:|[#/])/i.test(href)) return href;
  const [target, hash] = href.split(/(?=#)/);
  let path = normalize(join(dirname(from), decodeURIComponent(target))).replace(/\/$/, "");
  if (path === ".") path = "";
  if (path.startsWith("..")) return href;
  const readme = path ? `${path}/README.md` : "README.md";
  if (onSite.has(path)) return `/${slug(path)}${hash ?? ""}`;
  if (onSite.has(readme)) return `/${slug(readme)}${hash ?? ""}`;
  if (globSync(PICTURES, { cwd: ROOT }).includes(path)) return `/${path}`;
  const kind = /\.[^/]+$/.test(path) || path === "LICENSE" ? "blob" : "tree";
  return `https://github.com/${REPOSITORY}/${kind}/main/${path}${hash ?? ""}`;
}
