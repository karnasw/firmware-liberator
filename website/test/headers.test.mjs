// The headers public/_headers sends with every page, and that its
// Content-Security-Policy lets through what the layout loads, and no more.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { NOTIFY_URL } from "../public/assets/notify.mjs";

const file = readFileSync(new URL("../public/_headers", import.meta.url), "utf8");
const layout = readFileSync(new URL("../src/layouts/Page.astro", import.meta.url), "utf8");

function headers() {
  const lines = file.split("\n").filter((line) => line.trim() && !line.startsWith("#"));
  assert.equal(lines[0], "/*", "the headers are for every path");
  return Object.fromEntries(lines.slice(1).map((line) => {
    const at = line.indexOf(":");
    return [line.slice(0, at).trim().toLowerCase(), line.slice(at + 1).trim()];
  }));
}

function policy() {
  return Object.fromEntries(headers()["content-security-policy"].split(";").map((part) => {
    const [name, ...sources] = part.trim().split(/\s+/);
    return [name, sources];
  }));
}

test("every page is sent the hardening headers", () => {
  const sent = headers();
  assert.match(sent["strict-transport-security"], /^max-age=31536000; includeSubDomains$/);
  assert.equal(sent["x-content-type-options"], "nosniff");
  assert.equal(sent["x-frame-options"], "DENY");
  assert.equal(sent["referrer-policy"], "strict-origin-when-cross-origin");
  assert.equal(sent["cross-origin-opener-policy"], "same-origin");
  assert.match(sent["permissions-policy"], /camera=\(\).*microphone=\(\)/);
});

test("the policy starts from nothing and allows no inline script", () => {
  const csp = policy();
  assert.deepEqual(csp["default-src"], ["'none'"]);
  assert.deepEqual(csp["frame-ancestors"], ["'none'"]);
  assert.deepEqual(csp["base-uri"], ["'none'"]);
  assert.ok(!csp["script-src"].some((source) => source.includes("unsafe")), csp["script-src"]);
});

test("the layout has no inline script, which the policy would block", () => {
  for (const tag of layout.match(/<script\b[^>]*>/g) || []) {
    assert.match(tag, /\bsrc=/, tag);
  }
  assert.doesNotMatch(layout, /\son[a-z]+=/i, "an inline event handler");
});

test("each origin the layout loads from is one the policy allows", () => {
  const csp = policy();
  for (const [, tag, origin] of layout.matchAll(/<(script|img)\b[^>]*?\bsrc=["'](https:\/\/[^/"']+)/g)) {
    const directive = tag === "script" ? "script-src" : "img-src";
    assert.ok(csp[directive].includes(origin), `${origin} in ${directive}`);
  }
  assert.ok(csp["connect-src"].includes("https://cloudflareinsights.com"), "where the beacon reports");
  assert.ok(csp["connect-src"].includes(new URL(NOTIFY_URL).origin), "where the sign-up page sends an address");
});
