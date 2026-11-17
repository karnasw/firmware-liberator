// src/stats.mjs: the figures /stats and llms.txt are built from.
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { isFresh, llmsFigures } from "../src/stats.mjs";

const FIGURES = {
  format: 1, through: "2026-11-09", licence: "CC BY 4.0",
  devices: { seen: 526, converted: 390, firstRun: 72.8, eventually: 74.1 },
  runs: { runs: 615, success: 75.4 },
  kinds: [{ device: "sonoff-s60" }, { device: "sonoff-retired" }, { device: "sonoff-pow-elite" }],
};

async function loaded(text) {
  const folder = mkdtempSync(join(tmpdir(), "autoflash-stats-"));
  try {
    writeFileSync(join(folder, "stats.json"), text);
    process.env.STATS_FILE = join(folder, "stats.json");
    // A fresh copy of the module, as each build is: it reads its figures once.
    const module = await import(`../src/stats.mjs?${Math.random()}`);
    return await module.loadStats(new Date("2026-11-10T04:30:00Z"));
  } finally {
    delete process.env.STATS_FILE;
    rmSync(folder, { recursive: true, force: true });
  }
}

test("the figures a file gives are read, and fresh the morning after", async () => {
  assert.deepEqual(await loaded(JSON.stringify(FIGURES)), { figures: FIGURES, fresh: true, why: null });
});

test("anything else gives no figures, and says why", async () => {
  for (const [text, why] of [["not json", /could not be read/], [JSON.stringify({ ...FIGURES, format: 2 }), /format/],
    [JSON.stringify({ ...FIGURES, through: "yesterday" }), /format/]]) {
    const { figures, why: said } = await loaded(text);
    assert.equal(figures, null);
    assert.match(said, why);
  }
});

test("figures are fresh for three days after the night that made them, then not", () => {
  // Through 2026-11-09, made in the small hours of the 10th.
  assert.equal(isFresh(FIGURES, new Date("2026-11-12T23:00:00Z")), true);
  assert.equal(isFresh(FIGURES, new Date("2026-11-13T04:30:00Z")), false, "three nights missed");
});

test("llms.txt's section gives the headline, how fresh it is, and the devices the tool converts", () => {
  const section = llmsFigures({ figures: FIGURES, fresh: true });
  assert.match(section, /^## Figures\n\nAs of 2026-11-09 \(UTC\); rebuilt daily/);
  assert.match(section, /- 390 devices converted, of 526 seen\./);
  assert.match(section, /- Most converted: Sonoff S60, Sonoff POW Elite 16A\./, "by name, one the tool no longer knows left out");
  assert.equal(llmsFigures({ figures: FIGURES, fresh: false }), "", "never a stale figure");
  assert.equal(llmsFigures({ figures: null, fresh: false }), "");
});

test("figures all under five give no section at all", () => {
  const none = { ...FIGURES, devices: { seen: null, converted: null, firstRun: null, eventually: null },
    runs: { runs: null, success: null }, kinds: [] };
  assert.equal(llmsFigures({ figures: none, fresh: true }), "");
});
