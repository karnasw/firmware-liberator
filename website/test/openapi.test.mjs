// api/openapi.json, the API's description: the Worker answers every request
// as it says, and only so; the tool asks as it says; and no change to it
// breaks a client written against a description already published
// (fixtures/openapi/, one file a version, never changed once written).
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readdirSync, readFileSync } from "node:fs";
import { test } from "node:test";
import worker, * as api from "../api/index.js";
import { d1 } from "../api/local-d1.mjs";
import { work } from "../api/proof-of-work.js";
import * as reports from "../api/reports.js";
import { incompatible, loadSpec, narrowerProblems, resolve, validate } from "../scripts/openapi.mjs";

const spec = loadSpec();
const ROOT = new URL("../../", import.meta.url);
const PUBLISHED = new URL("./fixtures/openapi/", import.meta.url);
const METHODS = ["get", "post", "put", "patch", "delete", "options"];

// Each description published, by its SHA-256 as it was written: one is never
// changed once out, as a client may be written against it. A release that
// changes the API adds the new one, whose hash goes here.
const WRITTEN = {
  "1.0.0.json": "160c33d80d1e2f6e7c59b897e071d0ee442e2dfc5c25b31a3ae1a9aa5953a2b9",
};

// Easier work than the real, so the tests are quick: set on the module, as
// nothing the Worker is given can lower it.
const BITS = 8;
work.bits = BITS;

function proofFor(subject, time = Math.floor(Date.now() / 1000)) {
  for (let nonce = 0; ; nonce += 1) {
    const hash = createHash("sha256").update(`${time}:${subject}:${nonce}`).digest();
    if (hash.readUInt32BE(0) >>> (32 - BITS) === 0) return `${time}:${nonce}`;
  }
}
const sha = (data) => createHash("sha256").update(data).digest("hex");

function tar(files) {
  const blocks = [];
  for (const [name, text] of Object.entries(files)) {
    const data = Buffer.from(text);
    const header = Buffer.alloc(512);
    header.write(name, 0);
    header.write(data.length.toString(8).padStart(11, "0"), 124);
    blocks.push(header, data, Buffer.alloc((512 - (data.length % 512)) % 512));
  }
  return Buffer.concat([...blocks, Buffer.alloc(1024)]);
}

function bucket() {
  const objects = new Map();
  return {
    objects,
    head: async (key) => (objects.has(key) ? { key } : null),
    get: async (key) => (objects.has(key) ? { text: async () => objects.get(key).text } : null),
    put: async (key, body) => objects.set(key, { text: typeof body === "string" ? body : "" }),
    delete: async (key) => { objects.delete(key); },
  };
}

function mailer() {
  const sent = [];
  return { sent, send: async (message) => { sent.push(message); return { messageId: "m" }; } };
}

const env = (changes = {}) => ({ READOUTS: bucket(), DB: d1(), INGEST: "on", ENOUGH: "", SENDER_KEY: "a test key",
  DEVICE_HASH_KEY_PERMANENT: "another test key", EMAIL: mailer(),
  READOUT_LIMIT: { limit: async () => ({ success: true }) }, REPORT_LIMIT: { limit: async () => ({ success: true }) },
  ...changes });

const URL_OF = (path) => `https://api.firmwareliberator.com${path}`;

// A POST of `body`, with its length and, unless given, the work for it.
function post(path, body, type, headers = {}) {
  const bytes = Buffer.from(body);
  return new Request(URL_OF(path), { method: "POST", body: bytes, headers: {
    "Content-Type": type, "Content-Length": String(bytes.byteLength), "CF-Connecting-IP": "192.0.2.9",
    "X-Proof": proofFor(sha(bytes)), ...headers } });
}
const json = (path, value, headers) => post(path, JSON.stringify(value), "application/json", headers);
let nonce = 0;
const fresh = (value) => ({ ...value, nonce: String((nonce += 1)).padStart(16, "0") });

// Every answer seen, checked against the description as it comes: its status
// one listed for its path and method, its type and its body as given.
const seen = new Map();
async function answered(request, bindings) {
  const response = await worker.fetch(request, bindings);
  const path = new URL(request.url).pathname;
  // A method a path does not take is answered as the one it does says.
  const method = spec.paths[path]?.[request.method.toLowerCase()] || response.status !== 405 ? request.method
    : METHODS.find((name) => spec.paths[path]?.[name]?.responses["405"])?.toUpperCase() ?? request.method;
  const where = `${request.method} ${path} ${response.status}`;
  const operation = spec.paths[path]?.[method.toLowerCase()];
  assert.ok(operation, `${request.method} ${path} is not described`);
  const listed = operation.responses[String(response.status)];
  assert.ok(listed, `${where}: an answer not described`);
  const described = listed.$ref ? resolve(spec, listed.$ref) : listed;
  const type = (response.headers.get("Content-Type") ?? "").split(";")[0];
  const text = await response.text();
  // An answer described with no content has none.
  const media = described.content ? described.content[type] : { empty: text === "" };
  assert.ok(media && media.empty !== false, `${where}: ${type}, not ${Object.keys(described.content ?? {}).join(" or ")}`);
  if (type === "application/json") {
    assert.deepEqual(validate(spec, media.schema, JSON.parse(text)), [], `${where}: ${text}`);
  }
  for (const [name, header] of Object.entries(described.headers ?? {})) {
    const value = response.headers.get(name);
    if (value !== null) {
      const { schema } = header.$ref ? resolve(spec, header.$ref) : header;
      assert.deepEqual(validate(spec, schema, value), [], `${where}: ${name} ${value}`);
    }
  }
  const key = `${method} ${path}`;
  seen.set(key, new Set([...(seen.get(key) ?? []), String(response.status)]));
  return { status: response.status, text, headers: response.headers };
}

test("the description is OpenAPI 3.1, the tool's paths are under /v1/, and the pages an email links to are not", () => {
  assert.equal(spec.openapi, "3.1.0");
  assert.match(spec.info.version, /^1\.\d+\.\d+$/, "a /v1/ description is of version 1");
  assert.deepEqual(spec.servers, [{ url: "https://api.firmwareliberator.com" }]);
  const pages = ["/confirm", "/forget"];
  for (const path of Object.keys(spec.paths)) {
    assert.ok(path.startsWith("/v1/") || pages.includes(path), path);
  }
});

test("the Worker routes the paths described, and no others", () => {
  assert.deepEqual(Object.keys(api.ROUTES).sort(), Object.keys(spec.paths).sort());
});

test("each path takes the methods described, and answers any other 405, naming those", async () => {
  for (const [path, item] of Object.entries(spec.paths)) {
    const described = METHODS.filter((method) => item[method]).map((method) => method.toUpperCase());
    for (const method of METHODS.map((name) => name.toUpperCase())) {
      const response = await worker.fetch(new Request(URL_OF(path), { method,
        ...(method === "GET" ? {} : { body: "", headers: { "Content-Length": "0" } }) }), env());
      if (described.includes(method)) {
        assert.notEqual(response.status, 405, `${method} ${path}`);
      } else {
        assert.equal(response.status, 405, `${method} ${path}`);
        assert.deepEqual(response.headers.get("Allow").split(", ").sort(), described.sort(), `${method} ${path}`);
      }
    }
  }
});

test("the limits and names the description gives are the Worker's own", () => {
  const body = (path) => spec.paths[path].post.requestBody;
  const schema = (name) => spec.components.schemas[name];
  assert.equal(body("/v1/wanted")["x-max-bytes"], api.QUESTION_MAX_BYTES);
  assert.equal(body("/v1/notify")["x-max-bytes"], api.QUESTION_MAX_BYTES);
  assert.equal(body("/v1/readout")["x-max-bytes"], api.MAX_BYTES);
  assert.equal(body("/v1/reports")["x-max-bytes"], reports.MAX_BODY);
  assert.deepEqual([body("/v1/reports")["x-proof-bits"], body("/v1/reports")["x-proof-age"]],
    [reports.REPORT_PROOF_BITS, reports.REPORT_PROOF_AGE]);
  const { Proof, LightProof } = spec.components.parameters;
  assert.deepEqual([Proof["x-proof-bits"], Proof["x-proof-age"]], [api.PROOF_BITS, api.PROOF_AGE]);
  assert.deepEqual([LightProof["x-proof-bits"], LightProof["x-proof-age"]], [api.LIGHT_PROOF_BITS, api.LIGHT_PROOF_AGE]);
  assert.equal(schema("Question").properties.firmware.maxItems, api.MAX_FIRMWARE);
  assert.deepEqual(schema("Question").properties.stage.enum, api.STAGES);
  assert.equal(schema("Question").properties.format.const, api.FORMAT);
  assert.equal(schema("SignUp").properties.devices.maxItems, api.MAX_DEVICES);
  assert.equal(body("/v1/reports").content["application/json"].schema.properties.reports.maxItems, reports.MAX_REPORTS);
  assert.equal(schema("Model").pattern, api.MODEL.source);
  assert.equal(schema("StockModel").pattern, api.STOCK_MODEL.source);
  assert.equal(schema("StockVersion").pattern, api.STOCK_VERSION.source);
  // A model is a report's model (report-schema.json), in either case.
  const model = JSON.parse(readFileSync(new URL("../api/report-schema.json", import.meta.url), "utf8")).properties.model;
  assert.equal(api.MODEL.source, model.pattern.replaceAll("A-Z", "A-Za-z"));
});

test("every example the description gives fits its schema", () => {
  for (const [path, item] of Object.entries(spec.paths)) {
    for (const method of METHODS.filter((name) => item[name])) {
      const operation = item[method];
      for (const media of Object.values(operation.requestBody?.content ?? {})) {
        if (media.example !== undefined) {
          assert.deepEqual(validate(spec, media.schema, media.example), [], `${method} ${path}`);
        }
      }
      for (const [status, response] of Object.entries(operation.responses)) {
        for (const media of Object.values(response.content ?? {})) {
          if (media.example !== undefined) {
            assert.deepEqual(validate(spec, media.schema, media.example), [], `${method} ${path} ${status}`);
          }
        }
      }
    }
  }
});

const ASKED = { format: 1, model: "NEWTHING", stock_model: "SN-NEW-01", stock_version: "1.0.0", stage: "first", firmware: [] };

test("/v1/wanted answers as described", async () => {
  assert.equal((await answered(json("/v1/wanted", fresh(ASKED)), env())).status, 200);
  assert.equal((await answered(json("/v1/wanted", fresh(ASKED)), env({ ENOUGH: "NEWTHING" }))).status, 200);
  assert.equal((await answered(json("/v1/wanted", fresh({ ...ASKED, model: "a|b" })), env())).status, 400);
  assert.equal((await answered(json("/v1/wanted", fresh(ASKED), { "X-Proof": "1:1" }), env())).status, 403);
  const once = json("/v1/wanted", fresh(ASKED));
  const again = once.clone();
  const bindings = env();
  await answered(once, bindings);
  assert.equal((await answered(again, bindings)).status, 403, "a proof used already");
  assert.equal((await answered(new Request(URL_OF("/v1/wanted")), env())).status, 405);
  assert.equal((await answered(json("/v1/wanted", fresh(ASKED), { "Content-Length": "5000" }), env())).status, 413);
  assert.equal((await answered(json("/v1/wanted", fresh(ASKED), { "Content-Type": "text/plain" }), env())).status, 415);
  assert.equal((await answered(json("/v1/wanted", fresh(ASKED)), env({ INGEST: "off" }))).status, 503);
});

const MANIFEST = { format: 1, model: "NEWTHING", stock_model: "SN-NEW-01", stock_version: "1.0.0", tool: "2.4.0",
  read: false, offered: [{ file: "offered/user1.bin", version: "1.1.0", sha256: sha("an app") }] };
const readoutOf = (manifest = MANIFEST) => tar({ "manifest.json": JSON.stringify(manifest), "offered/user1.bin": "an app" });
const sendReadout = (body, headers) => post("/v1/readout", body, "application/x-tar", headers);

test("/v1/readout answers as described", async () => {
  const bindings = env();
  assert.equal((await answered(sendReadout(readoutOf()), bindings)).status, 201);
  assert.equal((await answered(sendReadout(readoutOf()), bindings)).status, 200, "the same again");
  assert.equal((await answered(sendReadout(readoutOf()), env({ ENOUGH: "NEWTHING" }))).status, 200);
  assert.equal((await answered(sendReadout(tar({ "manifest.json": "{}" })), env())).status, 400);
  assert.equal((await answered(sendReadout(readoutOf(), { "X-Proof": "1:1" }), env())).status, 403);
  assert.equal((await answered(new Request(URL_OF("/v1/readout")), env())).status, 405);
  const unsaid = new Request(URL_OF("/v1/readout"), { method: "POST", body: readoutOf(),
    headers: { "Content-Type": "application/x-tar" } });
  assert.equal((await answered(unsaid, env())).status, 411);
  assert.equal((await answered(sendReadout(readoutOf(), { "Content-Length": String(api.MAX_BYTES + 1) }), env())).status,
    413);
  assert.equal((await answered(sendReadout(readoutOf(), { "Content-Type": "application/gzip" }), env())).status, 415);
  const limited = env({ READOUT_LIMIT: { limit: async () => ({ success: false }) } });
  assert.equal((await answered(sendReadout(readoutOf()), limited)).status, 429);
  assert.equal((await answered(sendReadout(readoutOf()), env({ SENDER_KEY: undefined }))).status, 503);
});

const SIGN_UP = { format: 1, email: "me@example.com", devices: [{ model: "NEWTHING", stock_model: "SN-NEW-01" }] };

test("/v1/notify, and the pages its emails link to, answer as described", async () => {
  const bindings = env();
  assert.equal((await answered(json("/v1/notify", fresh(SIGN_UP)), bindings)).status, 202);
  const [, token] = /\?t=([0-9a-f]{32})/.exec(bindings.EMAIL.sent[0].text);
  const page = await answered(new Request(URL_OF(`/confirm?t=${token}`)), bindings);
  assert.equal(page.status, 200);
  const form = (path, fields) => new Request(URL_OF(path), { method: "POST", body: new URLSearchParams(fields),
    headers: { "Content-Type": "application/x-www-form-urlencoded" } });
  assert.equal((await answered(form("/confirm", { t: token }), bindings)).status, 200);
  assert.equal((await answered(form("/confirm", { t: token }), bindings)).status, 404, "used up");
  assert.equal((await answered(new Request(URL_OF(`/confirm?t=${token}`)), bindings)).status, 404);
  const kept = await bindings.DB.prepare("SELECT who, token FROM subscribers").first();
  const removal = new URL(`${api.FORGET_URL}?a=${kept.who}&t=${kept.token}`);
  assert.equal((await answered(new Request(URL_OF(`/forget${removal.search}`)), bindings)).status, 200);
  const fields = Object.fromEntries(removal.searchParams);
  assert.equal((await answered(form("/forget", fields), bindings)).status, 200);
  assert.equal((await answered(form("/forget", fields), bindings)).status, 404, "removed already");
  assert.equal((await answered(new Request(URL_OF(`/forget${removal.search}`)), bindings)).status, 404);
  for (const path of ["/confirm", "/forget"]) {
    assert.equal((await answered(new Request(URL_OF(path)), env({ INGEST: "off" }))).status, 503);
    assert.equal((await answered(form(path, {}), env({ INGEST: "off" }))).status, 503);
  }
  assert.equal((await answered(json("/v1/notify", fresh({ ...SIGN_UP, email: "not one" })), env())).status, 400);
  assert.equal((await answered(json("/v1/notify", fresh(SIGN_UP), { "X-Proof": "1:1" }), env())).status, 403);
  assert.equal((await answered(new Request(URL_OF("/v1/notify")), env())).status, 405);
  assert.equal((await answered(json("/v1/notify", fresh(SIGN_UP), { "Content-Length": "5000" }), env())).status, 413);
  assert.equal((await answered(json("/v1/notify", fresh(SIGN_UP), { "Content-Type": "text/plain" }), env())).status,
    415);
  const failing = env({ EMAIL: { send: async () => { throw new Error("E_INTERNAL_SERVER_ERROR"); } } });
  assert.equal((await answered(json("/v1/notify", fresh(SIGN_UP)), failing)).status, 502);
  assert.equal((await answered(json("/v1/notify", fresh(SIGN_UP)), env({ EMAIL: undefined }))).status, 503);
});

test("/v1/notify lets the website's sign-up page post from the browser, and read the answer", async () => {
  const asked = await answered(new Request(URL_OF("/v1/notify"), { method: "OPTIONS",
    headers: { Origin: api.SITE_ORIGIN, "Access-Control-Request-Method": "POST",
      "Access-Control-Request-Headers": "content-type, x-proof" } }), env());
  assert.equal(asked.status, 204);
  assert.equal(asked.headers.get("Access-Control-Allow-Origin"), api.SITE_ORIGIN);
  for (const [value, status] of [[fresh(SIGN_UP), 202], [fresh({ ...SIGN_UP, email: "not one" }), 400]]) {
    const response = await answered(json("/v1/notify", value, { Origin: api.SITE_ORIGIN }), env());
    assert.equal(response.status, status);
    assert.equal(response.headers.get("Access-Control-Allow-Origin"), api.SITE_ORIGIN, String(status));
  }
});

const REPORT = JSON.parse(readFileSync(new URL("./fixtures/reports.json", import.meta.url), "utf8")).valid[0];
function aReport() {
  const id = createHash("md5").update(String(Math.random())).digest("hex");
  return { ...structuredClone(REPORT), id, proof: proofFor(`report-${id}`) };
}

test("/v1/reports answers as described", async () => {
  const bindings = env();
  const sent = aReport();
  assert.equal((await answered(json("/v1/reports", { reports: [sent, { ...sent, id: "x" }] }), bindings)).status, 200);
  assert.equal((await answered(json("/v1/reports", { reports: [sent] }), bindings)).status, 200, "a duplicate");
  assert.equal((await answered(json("/v1/reports", { reports: [] }), env())).status, 400);
  assert.equal((await answered(new Request(URL_OF("/v1/reports")), env())).status, 405);
  const unsaid = new Request(URL_OF("/v1/reports"), { method: "POST", body: JSON.stringify({ reports: [aReport()] }),
    headers: { "Content-Type": "application/json" } });
  assert.equal((await answered(unsaid, env())).status, 411);
  assert.equal((await answered(json("/v1/reports", { reports: [aReport()] },
    { "Content-Length": String(reports.MAX_BODY + 1) }), env())).status, 413);
  assert.equal((await answered(json("/v1/reports", { reports: [aReport()] }, { "Content-Type": "text/plain" }), env()))
    .status, 415);
  const limited = env({ REPORT_LIMIT: { limit: async () => ({ success: false }) } });
  assert.equal((await answered(json("/v1/reports", { reports: [aReport()] }), limited)).status, 429);
  assert.equal((await answered(json("/v1/reports", { reports: [aReport()] }), env({ INGEST: "off" }))).status, 503);
});

test("every answer the description lists is one the Worker gives", () => {
  // Run after the tests above, which saw each answer.
  for (const [path, item] of Object.entries(spec.paths)) {
    for (const method of METHODS.filter((name) => item[name])) {
      const key = `${method.toUpperCase()} ${path}`;
      const missing = Object.keys(item[method].responses).filter((status) => !seen.get(key)?.has(status));
      assert.deepEqual(missing, [], `${key}: described, never seen`);
    }
  }
});

test("the tool asks as described, at the addresses described", () => {
  // Its own code, run: what ask_wanted and notify send, caught on the way out.
  const done = spawnSync(process.env.AUTOFLASH_PYTHON || "python3", ["-c", `
import json, sys
from unittest import mock
from tools.autoflash import hostsys, readout, report_upload
sent = []
class Answer:
    def __init__(self, request):
        sent.append({"url": request.full_url, "method": request.get_method(), "body": json.loads(request.data),
                     "type": request.get_header("Content-type")})
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def read(self): return b'{"wanted": true, "held": []}'
with mock.patch.object(readout, "upload_url", return_value=readout.UPLOAD_URL), \\
        mock.patch.object(readout, "proof", return_value="1:1"), \\
        mock.patch.object(hostsys, "urlopen", side_effect=lambda request, timeout: Answer(request)):
    readout.ask_wanted("NSPanel", "E32-MSW-NX", "1.0.4", "first", ["a" * 64])
    readout.ask_wanted("BASICR4", "", "", "whole", [])
    readout.notify("me@example.com", [("NSPanel", "E32-MSW-NX"), ("BASICR4", "")])
print(json.dumps({"sent": sent, "readout": readout.UPLOAD_URL, "reports": report_upload.UPLOAD_URL}))
`], { cwd: ROOT.pathname, encoding: "utf8" });
  assert.equal(done.status, 0, done.stderr);
  const { sent, readout, reports: reportsUrl } = JSON.parse(done.stdout.trim().split("\n").at(-1));
  const server = spec.servers[0].url;
  for (const url of [readout, reportsUrl]) {
    assert.ok(url.startsWith(server) && spec.paths[url.slice(server.length)]?.post, url);
  }
  assert.equal(sent.length, 3);
  for (const request of sent) {
    assert.ok(request.url.startsWith(server), request.url);
    const operation = spec.paths[request.url.slice(server.length)]?.[request.method.toLowerCase()];
    assert.ok(operation, `${request.method} ${request.url}`);
    const media = operation.requestBody.content[request.type];
    assert.ok(media, request.type);
    assert.deepEqual(validate(spec, media.schema, request.body), [], JSON.stringify(request.body));
  }
});

test("each description published is as it was written", () => {
  const files = readdirSync(PUBLISHED).sort();
  assert.deepEqual(files, Object.keys(WRITTEN).sort(), "a description published is listed in WRITTEN");
  for (const name of files) {
    assert.equal(sha(readFileSync(new URL(name, PUBLISHED))), WRITTEN[name], `${name} changed once published`);
    assert.equal(JSON.parse(readFileSync(new URL(name, PUBLISHED), "utf8")).info.version, name.replace(/\.json$/, ""));
  }
});

test("the description breaks nothing for a client of any published before it", () => {
  for (const name of readdirSync(PUBLISHED)) {
    const published = JSON.parse(readFileSync(new URL(name, PUBLISHED), "utf8"));
    assert.deepEqual(incompatible(published, spec), [], `against ${name}`);
    const [major, minor, patch] = spec.info.version.split(".").map(Number);
    const [was, wasMinor, wasPatch] = published.info.version.split(".").map(Number);
    assert.ok(major * 1e6 + minor * 1e3 + patch >= was * 1e6 + wasMinor * 1e3 + wasPatch, "its version only goes up");
  }
});

// The checker itself: each change a client would trip on is caught, and
// those it would not are let through.
const changed = (change) => {
  const next = structuredClone(spec);
  change(next);
  return incompatible(spec, next);
};
const schemas = (next) => next.components.schemas;

test("a change that would break a client is caught", () => {
  const breaking = {
    "a path gone": (next) => delete next.paths["/v1/wanted"],
    "a method gone": (next) => delete next.paths["/confirm"].get,
    "a field required that was not": (next) => schemas(next).Question.required.push("firmware"),
    "a name held tighter": (next) => { schemas(next).Model.pattern = "^[A-Z]+$"; },
    "fewer files to a question": (next) => { schemas(next).Question.properties.firmware.maxItems = 4; },
    "a stage gone": (next) => { schemas(next).Question.properties.stage.enum = ["first"]; },
    "a smaller body": (next) => { next.paths["/v1/readout"].post.requestBody["x-max-bytes"] = 1024; },
    "harder work": (next) => { next.components.parameters.Proof["x-proof-bits"] = 28; },
    "work good for less time": (next) => { next.components.parameters.LightProof["x-proof-age"] = 30; },
    "harder work for a report": (next) => { next.paths["/v1/reports"].post.requestBody["x-proof-bits"] = 26; },
    "a header required that was not": (next) => next.paths["/v1/reports"].post.parameters = [
      { name: "X-Proof", in: "header", required: true, schema: { type: "string" } }],
    "a body type no longer taken": (next) => {
      next.paths["/v1/readout"].post.requestBody.content = { "application/gzip": { schema: { type: "string" } } };
    },
    "an answer's field gone": (next) => { schemas(next).Wanted.required = ["wanted"]; },
    "a reason not met before": (next) => schemas(next).Wanted.properties.why.enum.push("busy"),
    "a success never given before": (next) => {
      next.paths["/v1/wanted"].post.responses["204"] = { description: "new" };
    },
    "a success gone": (next) => delete next.paths["/v1/readout"].post.responses["201"],
    "an answer in a type never given before": (next) => {
      next.paths["/v1/wanted"].post.responses["200"].content["text/plain"] = { schema: { type: "string" } };
    },
    "an answer's field of another type": (next) => {
      schemas(next).Kept.properties.id = { type: "integer" };
    },
    "a report's field held tighter": (next) => {
      next.paths["/v1/reports"].post.requestBody.content["application/json"].schema.properties.reports.maxItems = 50;
    },
  };
  for (const [what, change] of Object.entries(breaking)) {
    assert.notDeepEqual(changed(change), [], what);
  }
});

test("a change no client would trip on is let through", () => {
  const harmless = {
    "a new path": (next) => { next.paths["/v1/new"] = { post: { responses: { 200: { description: "x" } } } }; },
    "a larger body": (next) => { next.paths["/v1/readout"].post.requestBody["x-max-bytes"] *= 2; },
    "lighter work": (next) => { next.components.parameters.Proof["x-proof-bits"] = 22; },
    "more files to a question": (next) => { schemas(next).Question.properties.firmware.maxItems = 16; },
    "a stage added": (next) => schemas(next).Question.properties.stage.enum.push("partial"),
    "an answer's new field": (next) => { schemas(next).Wanted.properties.note = { type: "string" }; },
    "an error answer added": (next) => {
      next.paths["/v1/wanted"].post.responses["429"] = { description: "x",
        content: { "application/json": { schema: { $ref: "#/components/schemas/Error" } } } };
    },
    "a field no longer required": (next) => { schemas(next).Question.required = ["format", "model"]; },
    "words alone": (next) => { next.info.description = "Said otherwise."; },
  };
  for (const [what, change] of Object.entries(harmless)) {
    assert.deepEqual(changed(change), [], what);
  }
});

test("a schema narrower than another is told from one wider", () => {
  const narrow = { type: "string", enum: ["a"] };
  const wide = { type: "string", maxLength: 4 };
  assert.deepEqual(narrowerProblems(spec, narrow, wide), []);
  assert.notDeepEqual(narrowerProblems(spec, wide, narrow), []);
  assert.deepEqual(narrowerProblems(spec, { type: "integer" }, { type: ["integer", "null"] }), []);
  assert.notDeepEqual(narrowerProblems(spec, { type: ["integer", "null"] }, { type: "integer" }), []);
});
