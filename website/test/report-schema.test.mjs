// api/report-schema.js: a report as the tool makes it fits; any change the
// schema does not allow is turned away, as tools/repo/report_schema.py does.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { validate } from "../api/report-schema.js";

const read = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), "utf8"));
const schema = read("../api/report-schema.json");
const fixtures = read("./fixtures/reports.json");

// The first of fixtures[base] with one change at a dotted path.
function changed({ set, drop }, base = "valid") {
  const report = structuredClone(fixtures[base][0]);
  const at = (path) => {
    const keys = path.split(".");
    let node = report;
    for (const key of keys.slice(0, -1)) {
      node = node[key];
    }
    return [node, keys.at(-1)];
  };
  for (const [path, value] of Object.entries(set || {})) {
    const [node, key] = at(path);
    node[key] = value;
  }
  if (drop) {
    const [node, key] = at(drop);
    delete node[key];
  }
  return report;
}

test("every valid report fits", () => {
  for (const report of fixtures.valid) {
    assert.deepEqual(validate(schema, report), [], report.id);
  }
});

test("every invalid one is turned away", () => {
  for (const item of fixtures.invalid) {
    assert.notDeepEqual(validate(schema, changed(item)), [], item.why);
  }
});

test("a readout's report is held to the schema's readout, and agrees with the tool's", () => {
  for (const report of fixtures.readoutValid) {
    assert.deepEqual(validate(schema.readout, report), [], report.id);
    assert.notDeepEqual(validate(schema, report), [], "a readout's is no run's");
  }
  for (const item of fixtures.readoutInvalid) {
    assert.notDeepEqual(validate(schema.readout, changed(item, "readoutValid")), [], item.why);
  }
});

test("a value of the wrong kind is turned away, not thrown on", () => {
  for (const value of [null, [], "report", 1, [fixtures.valid[0]]]) {
    assert.notDeepEqual(validate(schema, value), [], JSON.stringify(value));
  }
});
