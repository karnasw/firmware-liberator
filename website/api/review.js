// Whether a report is held back for the maintainer's review rather than
// counted: one no run of the tool's would write. Held, it is still answered
// as accepted, so its sender learns nothing; it is quarantined, so it is in
// no figure and makes no device's record, and listed in `held_reports` with why, for
// the admin view to release or delete.
// - Words no report would hold (held-words.json: LDNOOBW's list, CC BY 4.0,
//   in each language of the Latin alphabet, as a report's text is printable
//   ASCII), as whole words, read also with lookalike characters as letters,
//   in any field a person could write: the model, the computer's and
//   Python's version, the Sonoff version, a failure's words.
// - A failure's words that read as none of the tool's messages, where they
//   claim a class of their own: the tool classes the very words it sends by
//   the same patterns (report-schema.json's failurePatterns, written from
//   tools/autoflash/failures.py). Words cut short ("...") are not checked.
// - A run the person stopped ("cancelled") that is not as the tool writes
//   one: with words, or without one of its ways of stopping (stoppedBy), or
//   one of those on a run that did not end so.
import words from "./held-words.json" with { type: "json" };
import schema from "./report-schema.json" with { type: "json" };

const STOPPED_BY = new Set(schema.stoppedBy);
const PATTERNS = schema.failurePatterns.map(([name, pattern]) => [name, new RegExp(pattern, "i")]);
const SINGLE = new Set(words.words.filter((word) => !word.includes(" ")));
const PHRASES = words.words.filter((word) => word.includes(" "));
const LOOKALIKE = new RegExp(`[${Object.keys(words.lookalike).map((char) => `\\${char}`).join("")}]`, "g");

// The class the tool gives these words (tools/autoflash/failures.classify).
export const classify = (text) => PATTERNS.find(([, pattern]) => pattern.test(text))?.[0] ?? "other";

// Whether `text` holds a word, or a phrase, of the list.
export function holdsHeldWords(text) {
  const lower = String(text ?? "").toLowerCase();
  for (const variant of [lower, lower.replace(LOOKALIKE, (char) => words.lookalike[char])]) {
    const tokens = variant.split(/[^a-z]+/).filter(Boolean);
    if (tokens.some((token) => SINGLE.has(token))) {
      return true;
    }
    const joined = ` ${tokens.join(" ")} `;
    if (PHRASES.some((phrase) => joined.includes(` ${phrase} `))) {
      return true;
    }
  }
  return false;
}

// Why a report (one that fits the schema) is held for review, or null.
export function heldFor(report) {
  if (report.kind === "readout") {
    // No words of its own but the names the device and computer gave.
    const written = [report.model, report.stock_model, report.stock_version, report.os.version, report.python.version];
    return written.some(holdsHeldWords) ? "words no report would hold" : null;
  }
  const stopped = report.stopped;
  const written = [report.model, report.stock_version, report.os.version, report.python.version, stopped?.text, stopped?.crash_site,
    report.firmware_name];
  if (written.some(holdsHeldWords)) {
    return "words no report would hold";
  }
  if ((report.result === "cancelled") !== STOPPED_BY.has(stopped?.error)
      || (report.result === "cancelled" && (stopped.text || stopped.crash_site))) {
    return "a stopped run not as the tool writes one";
  }
  if (stopped?.text && !stopped.text.endsWith("...") && !["other", "crash"].includes(stopped.error)
      && classify(stopped.text) === "other") {
    return "words that read as none of the tool's messages";
  }
  return null;
}
