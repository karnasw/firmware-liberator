// api/review.js: which reports are held back for the maintainer's review.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { classify, heldFor, holdsHeldWords } from "../api/review.js";

const read = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), "utf8"));
const WORDS = read("../api/held-words.json");
const SCHEMA = read("../api/report-schema.json");
const FIXTURES = read("./fixtures/reports.json");

test("every word of the list is found, as itself and among others", () => {
  for (const word of WORDS.words) {
    assert.ok(holdsHeldWords(word), word);
    assert.ok(holdsHeldWords(`the device ${word.toUpperCase()} said`), word);
  }
});

test("lookalike characters are read as the letters they stand for", () => {
  const word = WORDS.words.find((each) => /^[a-z]+$/.test(each) && /[oiea]/.test(each));
  const disguised = word.replace("o", "0").replace("i", "1").replace("e", "3").replace("a", "@");
  assert.notEqual(disguised, word);
  assert.ok(holdsHeldWords(disguised), disguised);
});

test("a word is a whole word: one inside another is not it", () => {
  const word = WORDS.words.find((each) => /^[a-z]{4,}$/.test(each));
  assert.ok(!holdsHeldWords(`x${word}x`), "inside a longer word");
});

test("nothing a real report holds is held", () => {
  for (const report of FIXTURES.valid) {
    assert.equal(heldFor(report), null, report.id);
  }
  for (const device of SCHEMA.properties.device.enum.filter(Boolean)) {
    assert.ok(!holdsHeldWords(device), device);
  }
  for (const model of ["S60TPG", "S60TPF", "S61STPF", "POWR316D", "POWCT", "THR320D", "DUALR3", "BASIC-1GS", "BASICR4",
    "MINIR4", "M5-1C", "M5-2C", "M5-3C", "NSPANEL", "1.2.0", "3.12.4", "11 (build 10.0.22631)"]) {
    assert.ok(!holdsHeldWords(model), model);
  }
});

test("words that read as none of the tool's messages are held, where they claim a class", () => {
  const report = structuredClone(FIXTURES.valid[1]);
  report.stopped = { step: "D4", error: "upload-cut", text: "buy cheap watches at example dot com" };
  assert.equal(heldFor(report), "words that read as none of the tool's messages");
  report.stopped.text = "the delivery stopped part way: the device fetched 10 of 20 bytes";
  assert.equal(heldFor(report), null, "the tool's own words");
  for (const stopped of [{ step: "D4", error: "other", text: "anything at all" },
    { step: "D4", error: "crash", text: "anything at all" },
    { step: "D4", error: "upload-cut", text: "cut short, and so not checked..." }]) {
    assert.equal(heldFor({ ...report, stopped }), null, stopped.error);
  }
});

test("a word of the list held, wherever a person could write one", () => {
  const word = WORDS.words.find((each) => /^[a-z]+$/.test(each));
  const report = structuredClone(FIXTURES.valid[0]);
  for (const changed of [{ model: word.toUpperCase() }, { os: { ...report.os, version: `Linux ${word}` } },
    { stopped: { step: "D4", error: "other", text: `oh ${word}`, crash_site: null } }]) {
    assert.equal(heldFor({ ...report, ...changed }), "words no report would hold", JSON.stringify(changed));
  }
});

test("its classes are the tool's, for its own words", () => {
  assert.equal(classify("the device at <address> did not answer within 30s"), "device-no-answer");
  assert.equal(classify("not confirmed - nothing has been written to the device"), "declined");
  assert.equal(classify("words of nobody's"), "other");
});

test("the list says where it is from, under which licence", () => {
  assert.match(WORDS.source, /^https:\/\/github\.com\/LDNOOBW\/.+\/tree\/[0-9a-f]{40}$/);
  assert.equal(WORDS.licence, "CC BY 4.0");
  const notices = readFileSync(new URL("../../NOTICES.md", import.meta.url), "utf8");
  assert.ok(notices.includes(WORDS.source), "credited in NOTICES.md");
});

test("a run the person stopped is taken only as the tool writes one", () => {
  const stopped = (result, error, text = "") => ({ ...structuredClone(FIXTURES.valid[0]), result,
    stopped: { step: "D1", error, text } });
  for (const how of SCHEMA.stoppedBy) {
    assert.equal(heldFor(stopped("cancelled", how)), null, how);
  }
  const held = "a stopped run not as the tool writes one";
  assert.equal(heldFor(stopped("cancelled", "quit", "I was worried")), held, "with words");
  assert.equal(heldFor(stopped("cancelled", "device-lost")), held, "a failure's class");
  assert.equal(heldFor(stopped("failed", "quit")), held, "a way of stopping on a failure");
  const placed = { ...stopped("cancelled", "quit"), stopped: { step: "D1", error: "quit", text: "",
    crash_site: "KeyError at tools/autoflash/report.py:1" } };
  assert.equal(heldFor(placed), held, "a place in the code: nothing went wrong");
});

test("a readout's report the tool writes is never held; one with words no report would hold is", () => {
  for (const report of FIXTURES.readoutValid) {
    assert.equal(heldFor(report), null, report.id);
  }
  const word = WORDS.words.find((each) => /^[a-z]{4,}$/.test(each)).toUpperCase();
  for (const field of ["model", "stock_model", "stock_version"]) {
    assert.equal(heldFor({ ...FIXTURES.readoutValid[0], [field]: word }), "words no report would hold", field);
  }
});
