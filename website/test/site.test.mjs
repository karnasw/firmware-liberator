// What src/site.mjs makes of the repository's paths and links.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { EXTRA_PAGES, REPOSITORY, address, description, pages, retired, rewrite, slug } from "../src/site.mjs";

const onSite = pages();
const github = `https://github.com/${REPOSITORY}`;

test("a page's address is its path, a README its directory's", () => {
  assert.equal(slug("README.md"), "");
  assert.equal(slug("docs/emulator.md"), "docs/emulator");
  assert.equal(slug("devices/sonoff-s61/README.md"), "devices/sonoff-s61");
});

test("every Markdown file is a page, wherever it is; nothing else is", () => {
  for (const path of ["README.md", "docs/what-happens.md", "devices/sonoff-s61/README.md", "tools/beken/README.md"]) {
    assert.ok(onSite.has(path), path);
  }
  assert.ok([...onSite].every((path) => path.endsWith(".md") && !path.startsWith(".github/")));
});

test("a link to a page goes to its address, with its anchor", () => {
  assert.equal(rewrite("docs/emulator.md", "README.md", onSite), "/docs/emulator");
  assert.equal(rewrite("emulator.md#stopping", "docs/testing.md", onSite), "/docs/emulator#stopping");
  assert.equal(rewrite("../README.md", "docs/testing.md", onSite), "/");
  assert.equal(rewrite("devices/sonoff-s61/", "README.md", onSite), "/devices/sonoff-s61");
  assert.equal(rewrite("../tools/beken/README.md", "docs/x.md", onSite), "/tools/beken");
});

test("a link to anything else goes to it on GitHub", () => {
  assert.equal(rewrite("../tools/beken/backup.py", "docs/x.md", onSite), `${github}/blob/main/tools/beken/backup.py`);
  assert.equal(rewrite("tools/autoflash/", "README.md", onSite), `${github}/tree/main/tools/autoflash`);
  assert.equal(rewrite("LICENSE", "README.md", onSite), `${github}/blob/main/LICENSE`);
});

test("a built page's address is the one readers use", () => {
  assert.equal(address("/index"), "/");
  assert.equal(address("/index.html"), "/");
  assert.equal(address("/docs/emulator.html"), "/docs/emulator");
  assert.equal(address("/docs/emulator"), "/docs/emulator");
});

test("a page is described by its first paragraph, as plain text", () => {
  assert.equal(description("# T\n\nSee [the **docs**](a.md) and `x`,\nthen go.\n\nLater."), "See the docs and x, then go.");
  assert.equal(description("# T\n\n> **Superseded.** Use the tool."), "Superseded. Use the tool.");
  assert.equal(description("# T\n\n## Status\n\nDone."), "Done.");
  assert.equal(description("# T\n\n[![badge](b.svg)](c)\n\n| a |\n\n- item\n\nProse."), "Prose.");
  assert.equal(description("# T\n\nThree ways:\n\n- one"), "Three ways.");
  assert.equal(description("# T\n\n- only a list"), undefined);
});

test("a long description ends at a sentence, or failing that a word", () => {
  const sentence = `${"a".repeat(100)}. ${"b ".repeat(100)}`;
  assert.equal(description(sentence), `${"a".repeat(100)}.`);
  const words = description("word ".repeat(100));
  assert.ok(words.length <= 161 && words.endsWith("word…"), words);
});

test("only archive/ is kept out of search results", () => {
  for (const address of ["archive", "archive/bridge", "/archive", "/archive/docs/reference/ota-findings"]) {
    assert.ok(retired(address), address);
  }
  for (const address of ["", "/", "docs/emulator", "/devices/sonoff-s61", "/archived", "/docs/archive"]) {
    assert.ok(!retired(address), address);
  }
});

test("llms.txt links only to pages that exist, none of them retired", () => {
  const text = readFileSync(new URL("../public/llms.txt", import.meta.url), "utf-8");
  const addresses = [...text.matchAll(/https:\/\/firmwareliberator\.com\/([^)\s]*)/g)].map((match) => match[1]);
  assert.ok(addresses.length > 0);
  const known = new Set([...[...onSite].map(slug), ...EXTRA_PAGES]);
  for (const address of addresses) {
    assert.ok(known.has(address) && !retired(address), address);
  }
});

test("a picture stays beside the page; addresses and anchors are left alone", () => {
  assert.equal(rewrite("docs/FirmwareRoutes.svg", "README.md", onSite), "/docs/FirmwareRoutes.svg");
  for (const href of ["https://esphome.io/", "#lets-go", "mailto:x@example.com", "/liberate.sh"]) {
    assert.equal(rewrite(href, "README.md", onSite), href);
  }
});
