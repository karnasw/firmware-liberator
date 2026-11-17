// What api/index.js, the project's API Worker, takes and turns away.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { read, release } from "../api/counters.js";
import worker, * as readout from "../api/index.js";
import { d1 } from "../api/local-d1.mjs";
import { PENDING } from "../api/pending.js";

const NOW = new Date("2026-11-07T12:00:00Z");

function tar(files) {
  const blocks = [];
  for (const [name, text] of Object.entries(files)) {
    const data = Buffer.isBuffer(text) ? text : Buffer.from(text);
    const header = Buffer.alloc(512);
    header.write(name, 0);
    header.write(data.length.toString(8).padStart(11, "0"), 124);
    blocks.push(header, data, Buffer.alloc((512 - (data.length % 512)) % 512));
  }
  return Buffer.concat([...blocks, Buffer.alloc(1024)]);
}

const MANIFEST = { format: 1, model: "NEWTHING", stock_model: "SN-NEW-01", stock_version: "1.0.0", tool: "2.4.0",
  read: true };
// xz files, as the tool's lzma.compress writes them: of 1 MiB of zeros, a
// flash chip's size, and of 3 MiB, no chip's.
const XZ_1MIB = Buffer.from("/Td6WFoAAATm1rRGAgAhARYAAAB0L+Wj7///ANldAABv/f//o7f/Rz5IFXI5YVG4kijmo4YH+e7kHoLTL8U6PAFLsX7J"
  + "iopNL6MN2X+m44wjEVPgWRjFdYrid/i2lH8MasDedElk4ulcU7IE2PdEDKtfDW1G6eXDdoi3llestk3haR1v+0uIEGxCy4g/XACP0E6vJiiUcR89jyTh"
  + "cJ6nI1/sKMuF0ZWYin4qkfIndfcZwAaYTZj92K/VkA/EJVP49ZE2MQWlsO5vwXBNRwzRkRGqrWAdus6xJxhcWYbpZlJYvul2rFnk5VsFCPnH2q38+1Iq"
  + "9+SVJQAAAAAAwva6PqJwa2AAAfUBgIBAAHmXf0exxGf7AgAAAAAEWVo=", "base64");
const XZ_3MIB = Buffer.from("/Td6WFoAAATm1rRGAgAhARYAAAB0L+Wj//8RAWxdAABv/f//o7f/Rz5IFXI5YVG4kijmo4YH+e7kHoLTL8U6PAFLsX7J"
  + "iopNL6MN2X+m44wjEVPgWRjFdYrid/i2lH8MasDedElk4ulcU7IE2PdEDKtfDW1G6eXDdoi3llestk3haR1v+0uIEGxCy4g/XACP0E6vJiiUcR89jyTh"
  + "cJ6nI1/sKMuF0ZWYin4qkfIndfcZwAaYTZj92K/VkA/EJVP49ZE2MQWlsO5vwXBNRwzRkRGqrWAdus6xJxhcWYbpZlJYvul2rFnk5VsFCPnH2q38+1Ir"
  + "dM0eWyBC+d1TPfgpZAk7gMsqbN+1O/DEvS5fqg8+S2ZCkBMO/xCT+HF4WfgLzf+VKEYPqfx83vuaMC5WwI+F84OBwGXEJVP49ZE2MQWlsO5vwXBNRwzR"
  + "kRGqrWAdus6xJxhcWYbpZlJYvul2rFnk5VsFCPnH2q38+1IrdM0eWyBC+d1TPfgpZAk7gMsqbN+1O/DEvEgn5liQAO0AmQDsc1On/b6ufDEan7eNMW5w"
  + "nqcjX+woy4XRlZiKfiqR8id19xnABphNmP3Yr9WQD8QlU/j1kTYxBaWw7m/BcE1HDNGREaqtYB26zrEnGFxZhulmUli+6XasWeTlWwUI+cfarfz7Uit0"
  + "zR5bIEL53VM9+ClkCTuAyyps37U78MS9Ll+qDz5LZkKQEw7/EJP4cXhZ+AvMsOLZAAAAALuBRxe1BKA/AAGnBICAwAEoGV2CscRn+wIAAAAABFla",
"base64");
const readoutOf = (manifest = MANIFEST, extra = {}) =>
  tar({ "manifest.json": JSON.stringify(manifest), "flash.bin.xz": XZ_1MIB, ...extra });

// Offered files, as a readout holds them: its manifest's list, and the files.
function offering(...contents) {
  const offered = contents.map((content, index) => ({ file: `offered/user${index + 1}.bin`, version: "1.1.0",
    sha256: createHash("sha256").update(content).digest("hex") }));
  return { offered, files: Object.fromEntries(offered.map((item, index) => [item.file, contents[index]])) };
}

// The tool's work (proof_of_work.solve, by way of readout.proof), made easy for a test.
// Easier work than a real readout's, so the tests are quick: set on the module
// (readout.work), as nothing the Worker is given can lower it.
const BITS = 8;
readout.work.bits = BITS;

function proofFor(body, bits = BITS, time = NOW.getTime() / 1000) {
  const digest = createHash("sha256").update(body).digest("hex");
  for (let nonce = 0; ; nonce += 1) {
    const hash = createHash("sha256").update(`${time}:${digest}:${nonce}`).digest();
    const zero = hash.readUInt32BE(0) >>> (32 - bits) === 0 && bits <= 32;
    if (zero) return `${time}:${nonce}`;
  }
}

function bucket() {
  const objects = new Map();
  return {
    objects,
    head: async (key) => (objects.has(key) ? { key } : null),
    list: async ({ prefix }) => ({
      objects: [...objects].filter(([key]) => key.startsWith(prefix)).map(([key, item]) => ({ key, size: item.size })),
    }),
    get: async (key) => (objects.has(key) ? { text: async () => objects.get(key).text } : null),
    put: async (key, body, options) => objects.set(key, {
      size: typeof body === "string" ? body.length : body.byteLength, options,
      text: typeof body === "string" ? body : "",
    }),
    delete: async (key) => { objects.delete(key); },
  };
}

// The readouts kept, leaving out the marks of what is held and the day's count.
const tars = (bindings) => [...bindings.READOUTS.objects.keys()].filter((key) => key.endsWith(".tar"));

// What the Worker would email, kept to look at: each message as sent.
function mailer() {
  const sent = [];
  return { sent, send: async (message) => { sent.push(message); return { messageId: `m${sent.length}` }; } };
}

function env({ allowed = true, enough = "" } = {}) {
  return { READOUTS: bucket(), DB: d1(), READOUT_LIMIT: { limit: async () => ({ success: allowed }) },
    ENOUGH: enough, SENDER_KEY: "a-made-up-test-key", INGEST: "on", EMAIL: mailer() };
}

function post(body, headers = {}) {
  return new Request("https://api.firmwareliberator.com/v1/readout", {
    method: "POST",
    body,
    headers: { "Content-Type": "application/x-tar", "Content-Length": String(body.byteLength),
      "CF-Connecting-IP": "192.0.2.9", "X-Proof": proofFor(body), ...headers },
  });
}

const send = (request, bindings) => readout.readout(request, bindings, NOW);

test("a readout with its work done is kept, under a reference it is given back", async () => {
  const bindings = env();
  const response = await send(post(readoutOf()), bindings);
  assert.equal(response.status, 201);
  const { id } = await response.json();
  assert.match(id, /^2026-11-07-[0-9a-f]{12}$/);
  const [key] = tars(bindings);
  const item = bindings.READOUTS.objects.get(key);
  assert.match(key, /^readouts\/2026-11-07\/[0-9a-f]{24}\.tar$/);
  assert.equal(item.options.customMetadata.model, "NEWTHING");
  assert.ok(!JSON.stringify(item.options).includes("192.0.2.9"), "the sender's address is kept");
});

test("the same one again is the one already kept", async () => {
  const bindings = env();
  const body = readoutOf();
  const first = await (await send(post(body), bindings)).json();
  const again = await send(post(body), bindings);
  assert.equal(again.status, 200);
  assert.deepEqual(await again.json(), first);
  assert.equal(tars(bindings).length, 1);
});

test("without its work, old work, or another body's, nothing is kept", async () => {
  const body = readoutOf();
  for (const proof of ["", "1:2:3", `${NOW.getTime() / 1000 - 3600}:0`, proofFor(readoutOf({ ...MANIFEST, x: 1 }))]) {
    const bindings = env();
    assert.equal((await send(post(body, { "X-Proof": proof }), bindings)).status, 403, proof);
    assert.equal(bindings.READOUTS.objects.size, 0);
  }
  const stale = proofFor(body, BITS, NOW.getTime() / 1000 - readout.PROOF_AGE - 60);
  assert.equal((await send(post(body, { "X-Proof": stale }), env())).status, 403);
});

test("one proof keeps one readout: another device's needs work of its own", async () => {
  // So a run reading out many devices cannot spread one proof over them all.
  const bindings = env();
  const first = readoutOf();
  const proof = proofFor(first);
  assert.equal((await send(post(first, { "X-Proof": proof }), bindings)).status, 201);
  const second = readoutOf({ ...MANIFEST, model: "OTHERTHING" });
  assert.equal((await send(post(second, { "X-Proof": proof }), bindings)).status, 403);
  assert.equal((await send(post(second), bindings)).status, 201);
  assert.equal(tars(bindings).length, 2);
});

test("anything but a readout the tool makes is turned away", async () => {
  const cases = [
    [new Request("https://api.firmwareliberator.com/v1/readout"), 405],
    [post(readoutOf(), { "Content-Type": "application/octet-stream" }), 415],
    [post(Buffer.from("not a tar")), 400],
    [post(tar({ "flash.bin.xz": "x" })), 400],
    [post(readoutOf({ format: 2, model: "NEWTHING" })), 400],
    [post(readoutOf(), { "Content-Length": String(readout.MAX_BYTES + 1) }), 413],
  ];
  for (const [request, status] of cases) {
    const bindings = env();
    assert.equal((await send(request, bindings)).status, status);
    assert.equal(bindings.READOUTS.objects.size, 0);
  }
});

test("too many from one sender wait", async () => {
  const bindings = env({ allowed: false });
  assert.equal((await send(post(readoutOf()), bindings)).status, 429);
  assert.equal(bindings.READOUTS.objects.size, 0);
});

// A day's count of readouts kept, as the database holds it (api/counters.js).
const dayCount = (bindings, day, count, bytes) => bindings.DB.db
  .prepare("INSERT INTO counters (name, day, count, bytes) VALUES (?, ?, ?, ?)").run(`readouts:${day}`, day, count, bytes);

test("a day's limit, by count and by size, is never passed", async () => {
  const bindings = env();
  dayCount(bindings, "2026-11-07", readout.DAILY_COUNT, 1);
  assert.equal((await send(post(readoutOf()), bindings)).status, 503);
  const full = env();
  dayCount(full, "2026-11-07", 1, readout.DAILY_BYTES);
  assert.equal((await send(post(readoutOf()), full)).status, 503);
  const yesterday = env();
  dayCount(yesterday, "2026-11-06", readout.DAILY_COUNT, readout.DAILY_BYTES);
  assert.equal((await send(post(readoutOf()), yesterday)).status, 201);
});

test("the day's count is kept in the database as readouts are, not listed", async () => {
  const bindings = { ...env(), READOUTS: { ...bucket(), list: async () => assert.fail("listed the day's readouts") } };
  let bytes = 0;
  for (const version of ["1.0.0", "1.0.1"]) {
    const body = readoutOf({ ...MANIFEST, stock_version: version });
    bytes += body.byteLength;
    assert.equal((await send(post(body), bindings)).status, 201);
  }
  assert.deepEqual(await read(bindings.DB, "readouts:2026-11-07"), { count: 2, bytes });
  assert.ok(![...bindings.READOUTS.objects.keys()].some((key) => key.includes("_readouts")), "no count in the bucket");
});

test("a readout not kept after all gives back its place in the day's count", async () => {
  const bindings = env();
  bindings.READOUTS.put = async () => {
    throw new Error("R2 down");
  };
  await assert.rejects(send(post(readoutOf()), bindings), /R2 down/);
  assert.deepEqual(await read(bindings.DB, "readouts:2026-11-07"), { count: 0, bytes: 0 });
});

test("a readout cut off part way through being kept leaves nothing of it behind", async () => {
  // Its tar and one file's mark written, then the next mark fails.
  const bindings = env();
  const put = bindings.READOUTS.put;
  let puts = 0;
  bindings.READOUTS.put = async (...args) => {
    puts += 1;
    if (puts === 3) {
      throw new Error("R2 down");
    }
    return put(...args);
  };
  const { offered, files } = offering("one app", "another app");
  await assert.rejects(send(post(readoutOf({ ...MANIFEST, offered }, files)), bindings), /R2 down/);
  assert.deepEqual([...bindings.READOUTS.objects.keys()], [], "its tar or a mark is left");
  assert.deepEqual(bindings.DB.db.prepare("SELECT * FROM readout_firmware").all(), []);
  assert.deepEqual(bindings.DB.db.prepare("SELECT * FROM readouts").all(), []);
  assert.deepEqual(await read(bindings.DB, "readouts:2026-11-07"), { count: 0, bytes: 0 });
});

test("until the database is brought up to date, every path asks the tool to wait", async () => {
  const bindings = { ...env(), DB: d1({ migrated: false }) };
  for (const path of ["/v1/readout", "/v1/wanted", "/v1/notify"]) {
    const response = await worker.fetch(new Request(`https://api.firmwareliberator.com${path}`, { method: "POST" }),
      bindings);
    assert.equal(response.status, 503, path);
    assert.equal(response.headers.get("Retry-After"), "300", path);
  }
});

// A readout from `address` (a sender of its own), its manifest's fields as
// given: its bytes its own, as two owners' readouts are (the same bytes twice
// are the same readout sent again).
// The network (Cloudflare's ASN) a request came from, as a Worker is given it.
const onNetwork = (request, asn) => Object.defineProperty(request, "cf", { value: { asn } });
// Each address on a network of its own, unless one is given.
const networks = new Map();
const networkOf = (address) => networks.get(address) ?? networks.set(address, 64496 + networks.size).get(address);
const from = (bindings, address, manifest, asn = networkOf(address)) =>
  send(onNetwork(post(readoutOf({ ...MANIFEST, owner: address, ...manifest }), { "CF-Connecting-IP": address }), asn),
    bindings);
const rows = async (bindings) =>
  (await bindings.DB.prepare("SELECT * FROM readouts ORDER BY received, id").all()).results;

test("three readouts of each model and version are kept, each from a different sender", async () => {
  assert.equal(readout.PER_KEY, 3);
  const bindings = env();
  const first = { read: false };
  for (const address of ["192.0.2.1", "192.0.2.2", "192.0.2.3"]) {
    assert.equal((await from(bindings, address, first)).status, 201, address);
  }
  const fourth = await from(bindings, "192.0.2.4", first);
  assert.deepEqual([fourth.status, await fourth.json()], [200, { enough: true }], "a fourth adds nothing");
  assert.equal((await from(bindings, "192.0.2.4", { ...first, stock_version: "1.1.0" })).status, 201, "another version does");
  assert.equal((await from(bindings, "192.0.2.4", { ...first, stock_model: "SN-NEW-02" })).status, 201,
    "and another firmware model");
});

test("one sender's readouts of one key are one, and take one of its day", async () => {
  // An owner with many of one device, read out at once, sends one of it.
  const bindings = env();
  const statuses = [];
  for (const tool of ["a", "b", "c", "d"]) {
    statuses.push((await from(bindings, "192.0.2.9", { read: false, tool })).status);
  }
  assert.deepEqual(statuses, [201, 200, 200, 200]);
  assert.equal((await from(bindings, "192.0.2.9", { tool: "e" })).status, 200, "nor a whole one of the same key");
  const sender = (await bindings.DB.prepare("SELECT count FROM counters WHERE name LIKE 'sender:%'").first()).count;
  assert.equal(sender, 1, "those not kept take none of its 20 a day");
  assert.equal((await from(bindings, "192.0.2.10", { read: false, tool: "f" })).status, 201, "another sender's is kept");
});

test("a whole readout takes a first stage's place, as it holds all one does", async () => {
  const bindings = env();
  for (const address of ["192.0.2.1", "192.0.2.2"]) {
    assert.equal((await from(bindings, address, {})).status, 201);
  }
  assert.equal((await from(bindings, "192.0.2.3", { read: false })).status, 201, "one place left for a first stage");
  // From a network one of those came from: past three, a place is only for another's.
  assert.equal((await from(bindings, "192.0.2.4", { read: false }, networkOf("192.0.2.3"))).status, 200, "and then none");
  assert.equal((await from(bindings, "192.0.2.4", {})).status, 201, "a whole one has its own third place");
});

const FIRMWARE = { table: "1".repeat(64), apps: [{ project: "esp32_fw", version: "1.0.0", elf_sha256: "2".repeat(64) }] };

test("three that agree are taken as right; until then each waits for review", async () => {
  const bindings = env();
  for (const address of ["192.0.2.1", "192.0.2.2"]) {
    await from(bindings, address, { firmware: FIRMWARE });
  }
  assert.deepEqual((await rows(bindings)).map((row) => row.review), ["pending", "pending"]);
  await from(bindings, "192.0.2.3", { firmware: FIRMWARE, wifi_blanked: 3 });   // its owner's settings differ
  assert.deepEqual((await rows(bindings)).map((row) => row.review), ["quorum", "quorum", "quorum"]);
});

test("one that differs from the others leaves them all for review", async () => {
  const bindings = env();
  const odd = { ...FIRMWARE, apps: [{ ...FIRMWARE.apps[0], elf_sha256: "3".repeat(64) }] };
  await from(bindings, "192.0.2.1", { firmware: FIRMWARE });
  await from(bindings, "192.0.2.2", { firmware: odd });
  await from(bindings, "192.0.2.3", { firmware: FIRMWARE });
  const kept = await rows(bindings);
  assert.deepEqual(kept.map((row) => row.review), ["pending", "pending", "pending"]);
  const prints = kept.map((row) => row.fingerprint);
  const oddPrint = prints.find((print) => prints.filter((other) => other === print).length === 1);
  assert.ok(oddPrint, "the odd one is the one whose fingerprint differs");
  // Rejected, its place is free for another sender's.
  await bindings.DB.prepare("UPDATE readouts SET review = 'rejected' WHERE fingerprint = ?").bind(oddPrint).run();
  assert.equal((await from(bindings, "192.0.2.4", { firmware: FIRMWARE })).status, 201);
  const after = await rows(bindings);
  assert.deepEqual(after.filter((row) => row.fingerprint === oddPrint).map((row) => row.review), ["rejected"]);
  assert.deepEqual(after.filter((row) => row.fingerprint !== oddPrint).map((row) => row.review),
    ["quorum", "quorum", "quorum"]);
});

test("three that agree from one network wait for review; two more networks' that agree settle them", async () => {
  assert.deepEqual([readout.MIN_NETWORKS, readout.MAX_PLACES], [3, 6]);
  const bindings = env();
  for (const address of ["192.0.2.1", "192.0.2.2", "192.0.2.3"]) {
    assert.equal((await from(bindings, address, { firmware: FIRMWARE }, 64500)).status, 201);
  }
  assert.deepEqual((await rows(bindings)).map((row) => row.review), ["pending", "pending", "pending"],
    "one rented server's network, however many address blocks");
  assert.equal((await from(bindings, "192.0.2.4", { firmware: FIRMWARE }, 64500)).status, 200, "no place: that network has");
  assert.equal((await from(bindings, "192.0.2.5", { firmware: FIRMWARE }, 64501)).status, 201, "a place past three");
  assert.deepEqual((await rows(bindings)).map((row) => row.review), Array(4).fill("pending"), "two networks: not enough");
  assert.equal((await from(bindings, "192.0.2.6", { firmware: FIRMWARE }, 64502)).status, 201);
  assert.deepEqual((await rows(bindings)).map((row) => row.review), Array(5).fill("quorum"));
  assert.equal((await from(bindings, "192.0.2.7", { firmware: FIRMWARE }, 64503)).status, 200, "agreed: no more places");
});

test("three made up on one network cannot keep out the owners who outvote them", async () => {
  const bindings = env();
  const made = { ...FIRMWARE, apps: [{ ...FIRMWARE.apps[0], elf_sha256: "9".repeat(64) }] };
  for (const address of ["198.51.100.1", "198.51.100.2", "198.51.100.3"]) {
    assert.equal((await from(bindings, address, { firmware: made }, 64999)).status, 201);
  }
  for (const [address, asn] of [["192.0.2.1", 64496], ["192.0.2.2", 64497], ["192.0.2.3", 64498]]) {
    assert.equal((await from(bindings, address, { firmware: FIRMWARE }, asn)).status, 201, address);
  }
  const kept = await rows(bindings);
  assert.deepEqual(kept.filter((row) => row.review === "quorum").length, 3, "the owners' three agree");
  assert.deepEqual(kept.filter((row) => row.review === "pending").length, 3, "the made-up ones wait, to be deleted");
  assert.equal((await from(bindings, "192.0.2.9", { firmware: FIRMWARE }, 64510)).status, 200);
});

test("past three places, a readout without a network is not taken, nor one from a network held", async () => {
  const bindings = env();
  const odd = (n) => ({ firmware: { ...FIRMWARE, apps: [{ ...FIRMWARE.apps[0], elf_sha256: String(n).repeat(64) }] } });
  for (const [n, address] of ["192.0.2.1", "192.0.2.2", "192.0.2.3"].entries()) {
    await from(bindings, address, odd(n + 3), 64500 + n);
  }
  assert.equal((await from(bindings, "192.0.2.4", odd(7), null)).status, 200, "no network given");
  assert.equal((await from(bindings, "192.0.2.5", odd(7), 64501)).status, 200, "a network one came from");
  assert.equal((await from(bindings, "192.0.2.6", odd(7), 64503)).status, 201);
  const asked = (asn) => readout.wanted(onNetwork(question(fresh(ASKED_WHOLE)), asn), bindings, NOW);
  assert.equal((await (await asked(64504)).json()).wanted, true, "asked first, a new network's would be taken");
  assert.equal((await (await asked(64503)).json()).why, "held");
});

test("three alike from three clouds are kept, but do not agree until an owner's network joins them", async () => {
  const bindings = env();
  for (const [address, asn] of [["198.51.100.1", 16509], ["198.51.100.2", 24940], ["198.51.100.3", 14061]]) {
    assert.equal((await from(bindings, address, { firmware: FIRMWARE }, asn)).status, 201, address);
  }
  assert.deepEqual((await rows(bindings)).map((row) => row.review), Array(3).fill("pending"), "a cloud's is no network");
  for (const [address, asn] of [["192.0.2.1", 64496], ["192.0.2.2", 64497]]) {
    assert.equal((await from(bindings, address, { firmware: FIRMWARE }, asn)).status, 201, address);
  }
  assert.ok((await rows(bindings)).every((row) => row.review === "pending"), "two networks only");
  assert.equal((await from(bindings, "192.0.2.3", { firmware: FIRMWARE }, 64498)).status, 201);
  assert.deepEqual((await rows(bindings)).map((row) => row.review), Array(6).fill("quorum"));
});

// Every readout held made to have come `days` before NOW.
const aged = (bindings, days) => bindings.DB.db.prepare("UPDATE readouts SET received = ?")
  .run(new Date(NOW.getTime() - days * 86_400_000).toISOString());
const MADE_UP = { ...FIRMWARE, apps: [{ ...FIRMWARE.apps[0], elf_sha256: "9".repeat(64) }] };
// Three whole readouts that agree, from three networks.
async function agreed(bindings) {
  for (const address of ["192.0.2.1", "192.0.2.2", "192.0.2.3"]) {
    assert.equal((await from(bindings, address, { firmware: FIRMWARE })).status, 201, address);
  }
  assert.ok((await rows(bindings)).every((row) => row.review === "quorum"));
}

test("once whole readouts agree, one more checks them each CHECK_DAYS, from a new network", async () => {
  assert.equal(readout.CHECK_DAYS, 30);
  const bindings = { ...env(), ALERTS_RECIPIENT_EMAIL: "maintainer@example.com" };
  await agreed(bindings);
  assert.equal((await from(bindings, "192.0.2.4", { firmware: FIRMWARE })).status, 200, "not before CHECK_DAYS");
  aged(bindings, 29);
  assert.equal((await from(bindings, "192.0.2.4", { firmware: FIRMWARE })).status, 200, "nor a day short");
  aged(bindings, 30);
  assert.equal((await from(bindings, "192.0.2.4", { firmware: FIRMWARE }, 16509)).status, 200,
    "nor from a network that counts as none");
  assert.equal((await from(bindings, "192.0.2.5", { firmware: FIRMWARE }, networkOf("192.0.2.1"))).status, 200,
    "nor from one held");
  const asked = (asn) => readout.wanted(onNetwork(question(fresh(ASKED_WHOLE)), asn), bindings, NOW);
  assert.equal((await (await asked(64505)).json()).wanted, true, "asked first, it is wanted");
  assert.equal((await from(bindings, "192.0.2.6", { firmware: FIRMWARE })).status, 201);
  const check = (await rows(bindings)).at(-1);
  assert.deepEqual([check.review, check.checked], ["quorum", 1], "alike: taken as right too");
  assert.equal(bindings.EMAIL.sent.at(-1).subject, "Readouts confirmed: NEWTHING|SN-NEW-01|1.0.0 (whole)");
  assert.equal((await from(bindings, "192.0.2.7", { firmware: FIRMWARE })).status, 200, "the next CHECK_DAYS on");
  assert.equal((await from(bindings, "192.0.2.7", { read: false })).status, 200, "and a first stage is not checked");
});

test("a check alike readouts approved from too few networks is taken as right too", async () => {
  const bindings = env();
  for (const address of ["192.0.2.1", "192.0.2.2", "192.0.2.3"]) {
    await from(bindings, address, { firmware: FIRMWARE }, 64500);
  }
  bindings.DB.db.prepare("UPDATE readouts SET review = 'approved'").run();
  aged(bindings, 30);
  assert.equal((await from(bindings, "192.0.2.4", { firmware: FIRMWARE }, 64501)).status, 201);
  assert.equal((await rows(bindings)).at(-1).review, "quorum", "two networks, but alike those approved");
});

test("checks stop at MAX_PLACES in all", async () => {
  const bindings = env();
  await agreed(bindings);
  for (const address of ["192.0.2.4", "192.0.2.5", "192.0.2.6"]) {
    aged(bindings, 30);
    assert.equal((await from(bindings, address, { firmware: FIRMWARE })).status, 201, address);
  }
  aged(bindings, 30);
  assert.equal((await from(bindings, "192.0.2.7", { firmware: FIRMWARE })).status, 200);
  assert.equal((await rows(bindings)).length, readout.MAX_PLACES);
});

test("a check not alike waits for review, the maintainer is told, and new networks are taken at once", async () => {
  const bindings = { ...env(), ALERTS_RECIPIENT_EMAIL: "maintainer@example.com" };
  bindings.DB.db.prepare("INSERT INTO settings (name, value) VALUES (?, 'off')").run(readout.AGREEMENT_SETTING);
  await agreed(bindings);
  aged(bindings, 30);
  assert.equal((await from(bindings, "192.0.2.4", { firmware: MADE_UP })).status, 201);
  const check = (await rows(bindings)).at(-1);
  assert.deepEqual([check.review, check.checked], ["pending", 1]);
  assert.deepEqual(bindings.EMAIL.sent.map((message) => message.subject),
    ["Readouts disagree: NEWTHING|SN-NEW-01|1.0.0 (whole)"], "whatever the switch says");
  assert.match(bindings.EMAIL.sent[0].text, /readouts\.py review NEWTHING lists it \(DISAGREES\)/);
  // Disputed: two more not alike, from new networks, at once - three from
  // three networks, yet not taken as right while another fingerprint is.
  for (const address of ["192.0.2.5", "192.0.2.6"]) {
    assert.equal((await from(bindings, address, { firmware: MADE_UP })).status, 201, address);
  }
  assert.deepEqual((await rows(bindings)).map((row) => row.review),
    ["quorum", "quorum", "quorum", "pending", "pending", "pending"], "the maintainer settles it");
});

// A first stage offered `version`, its one file held already (by its SHA-256).
const offeredAt = (version, sha) => ({ read: false, offered: [{ file: null, version, sha256: sha, held: true }] });
const OFFER_A = offeredAt("1.2.0", "a".repeat(64));
const OFFER_B = offeredAt("1.3.0", "b".repeat(64));

test("first stages are held and agree each offer apart, as each release eWeLink offers is new", async () => {
  const bindings = { ...env(), ALERTS_RECIPIENT_EMAIL: "maintainer@example.com" };
  for (const address of ["192.0.2.1", "192.0.2.2", "192.0.2.3"]) {
    assert.equal((await from(bindings, address, OFFER_A)).status, 201, address);
  }
  assert.equal((await from(bindings, "192.0.2.4", OFFER_A)).status, 200, "that offer has its three");
  assert.deepEqual(bindings.EMAIL.sent.map((message) => message.subject),
    ["Readouts agree: NEWTHING|SN-NEW-01|1.0.0 (first, offered 1.2.0)"]);
  const asked = (firmware) => readout.wanted(onNetwork(question(fresh({ ...ASKED, firmware })), 64505), bindings, NOW);
  assert.equal((await (await asked(["a".repeat(64)])).json()).why, "held");
  assert.equal((await (await asked(["b".repeat(64)])).json()).wanted, true, "asked first, a new offer is wanted");
  // A new release offered: the same owners may send it too.
  for (const address of ["192.0.2.1", "192.0.2.2", "192.0.2.3"]) {
    assert.equal((await from(bindings, address, OFFER_B)).status, 201, address);
  }
  assert.equal((await from(bindings, "192.0.2.1", OFFER_B)).status, 200, "but each offer once");
  const kept = await rows(bindings);
  assert.deepEqual(kept.map((row) => [row.offered, row.review]).sort(), [...Array(3).fill(["1.2.0", "quorum"]),
    ...Array(3).fill(["1.3.0", "quorum"])], "each agreed on its own");
  assert.equal(bindings.EMAIL.sent.at(-1).subject, "Readouts agree: NEWTHING|SN-NEW-01|1.0.0 (first, offered 1.3.0)");
});

test("a whole readout takes its own offer's first stage place, and one sender sends one whole whatever its offer",
  async () => {
    const bindings = env();
    const whole = (offer) => ({ ...offer, read: true });
    for (const address of ["192.0.2.1", "192.0.2.2", "192.0.2.3"]) {
      assert.equal((await from(bindings, address, whole(OFFER_A))).status, 201, address);
    }
    assert.equal((await from(bindings, "192.0.2.4", OFFER_A, 64499)).status, 200, "offer A's places are full");
    assert.equal((await from(bindings, "192.0.2.4", OFFER_B)).status, 201, "offer B's are not");
    assert.equal((await from(bindings, "192.0.2.1", whole(OFFER_B), 64510)).status, 200, "nor a second whole one");
  });

const offers = (bindings) => bindings.DB.db.prepare("SELECT key, offered FROM agreed_offers").all()
  .map((row) => ({ ...row }));
const whole = (offer) => ({ ...offer, read: true });

test("three whole readouts naming one offer, from three networks, agree on it as well as on their firmware", async () => {
  const bindings = { ...env(), ALERTS_RECIPIENT_EMAIL: "maintainer@example.com" };
  for (const address of ["192.0.2.1", "192.0.2.2", "192.0.2.3"]) {
    await from(bindings, address, whole(OFFER_A));
  }
  assert.deepEqual(offers(bindings), [{ key: "NEWTHING|SN-NEW-01|1.0.0", offered: "1.2.0" }]);
  assert.deepEqual(bindings.EMAIL.sent.map((message) => message.subject).sort(), [
    "Readouts agree: NEWTHING|SN-NEW-01|1.0.0 (first, offered 1.2.0)", "Readouts agree: NEWTHING|SN-NEW-01|1.0.0 (whole)"]);
});

test("first stages and whole readouts agree on their offer together", async () => {
  const bindings = env();
  await from(bindings, "192.0.2.1", whole(OFFER_A));
  await from(bindings, "192.0.2.2", whole(OFFER_A));
  assert.deepEqual(offers(bindings), [], "two name it");
  await from(bindings, "192.0.2.3", OFFER_A);
  assert.equal(offers(bindings).length, 1);
  assert.deepEqual((await rows(bindings)).map((row) => [row.stage, row.review]).sort(),
    [["first", "quorum"], ["whole", "pending"], ["whole", "pending"]], "the whole ones wait on their firmware");
});

test("whole readouts taken as right on their firmware do not close the offer they name", async () => {
  const bindings = env();
  await from(bindings, "192.0.2.1", whole(OFFER_A), 64500);
  await from(bindings, "192.0.2.2", whole(OFFER_A), 64501);
  await from(bindings, "192.0.2.3", whole(OFFER_B), 64502);
  assert.ok((await rows(bindings)).every((row) => row.review === "quorum"));
  assert.equal((await from(bindings, "192.0.2.4", OFFER_A, 64500)).status, 201, "offer A's third place");
  assert.deepEqual(offers(bindings), [], "from two networks");
  assert.equal((await from(bindings, "192.0.2.5", OFFER_A, 64503)).status, 201, "so a place for a new network");
  assert.deepEqual(offers(bindings).map((row) => row.offered), ["1.2.0"]);
});

test("the maintainer is emailed when readouts come to agree, unless the admin view's switch is off", async () => {
  const agree = async (bindings) => {
    for (const address of ["192.0.2.1", "192.0.2.2", "192.0.2.3"]) {
      await from(bindings, address, { firmware: FIRMWARE });
    }
    return bindings.EMAIL.sent;
  };
  const told = await agree({ ...env(), ALERTS_RECIPIENT_EMAIL: "maintainer@example.com" });
  assert.equal(told.length, 1);
  assert.deepEqual([told[0].to, told[0].from], ["maintainer@example.com", readout.NOTIFY_FROM]);
  assert.match(told[0].subject, /^Readouts agree: NEWTHING\|SN-NEW-01\|1\.0\.0 \(whole\)$/);
  const off = { ...env(), ALERTS_RECIPIENT_EMAIL: "maintainer@example.com" };
  off.DB.db.prepare("INSERT INTO settings (name, value) VALUES (?, 'off')").run(readout.AGREEMENT_SETTING);
  assert.deepEqual(await agree(off), []);
  assert.deepEqual(await agree(env()), [], "nowhere to send it");
});

test("three alike from too few networks send no email: the admin view lists them", async () => {
  const bindings = { ...env(), ALERTS_RECIPIENT_EMAIL: "maintainer@example.com" };
  for (const [address, asn] of [["192.0.2.1", 64500], ["192.0.2.2", 64500], ["192.0.2.3", 64501]]) {
    await from(bindings, address, { firmware: FIRMWARE }, asn);
  }
  assert.deepEqual(bindings.EMAIL.sent, []);
  assert.equal((await from(bindings, "192.0.2.4", { firmware: FIRMWARE }, 64502)).status, 201);
  assert.deepEqual(bindings.EMAIL.sent.map((message) => message.subject),
    ["Readouts agree: NEWTHING|SN-NEW-01|1.0.0 (whole)"], "once they agree, from three networks");
});

test("a readout's row names its sender by a keyed hash a key, never its address", async () => {
  const bindings = env();
  await from(bindings, "192.0.2.9", { read: false });
  await from(bindings, "192.0.2.9", { read: false, stock_version: "2.0.0" });
  const kept = await rows(bindings);
  const one = kept.find((row) => row.key.endsWith("|1.0.0"));
  const two = kept.find((row) => row.key.endsWith("|2.0.0"));
  assert.match(one.sender, /^[0-9a-f]{32}$/);
  assert.notEqual(one.sender, two.sender, "nothing ties one sender's readouts of two keys together");
  assert.ok(!JSON.stringify(await rows(bindings)).includes("192.0.2"));
  assert.equal(one.key, "NEWTHING|SN-NEW-01|1.0.0");
  assert.match(one.object, /^readouts\/2026-11-07\/[0-9a-f]{24}\.tar$/);
});

test("an offered file a readout holds is marked, and one it left out is not", async () => {
  const bindings = env();
  const { offered: [one], files } = offering("an app");
  const offered = [one, { file: null, version: "1.1.0", sha256: "b".repeat(64), held: true }];
  assert.equal((await send(post(readoutOf({ ...MANIFEST, offered }, files)), bindings)).status, 201);
  const keys = [...bindings.READOUTS.objects.keys()];
  assert.ok(keys.includes(`readouts/firmware/${one.sha256}`));
  assert.ok(!keys.includes(`readouts/firmware/${"b".repeat(64)}`));
  assert.ok(!keys.some((key) => key.startsWith("readouts/held/")), "what is held is the readouts table's");
});

test("a readout naming an offered file it does not hold, or one that is not what it says, is turned away", async () => {
  // Else one made-up readout could mark a real file held, and every genuine
  // readout after it would leave that file out.
  const real = "f".repeat(64);
  const { offered: [one], files } = offering("an app");
  for (const [offered, extra] of [
    [[{ file: "offered/user1.bin", version: "1.1.0", sha256: real }], {}],
    [[{ file: "offered/user1.bin", version: "1.1.0", sha256: real }], { "offered/user1.bin": "something else" }],
    [[{ ...one, file: "offered/user2.bin" }], files],
    [[{ ...one, file: 7 }], files]]) {
    const bindings = env();
    const response = await send(post(readoutOf({ ...MANIFEST, offered }, extra)), bindings);
    assert.equal(response.status, 400, JSON.stringify(offered));
    assert.deepEqual([...bindings.READOUTS.objects.keys()], []);
    assert.deepEqual(await read(bindings.DB, "readouts:2026-11-07"), { count: 0, bytes: 0 });
  }
});

test("a readout of the whole memory holds a whole flash chip's, or is turned away", async () => {
  assert.equal(readout.xzUnpacked(new Uint8Array(XZ_1MIB)), 1024 * 1024);
  assert.equal(readout.xzUnpacked(new Uint8Array(XZ_3MIB)), 3 * 1024 * 1024);
  for (const [why, files] of [["no memory", { "manifest.json": JSON.stringify(MANIFEST) }],
    ["not xz", { "manifest.json": JSON.stringify(MANIFEST), "flash.bin.xz": "\xfd7zXZ, and nothing" }],
    ["no flash chip's size", { "manifest.json": JSON.stringify(MANIFEST), "flash.bin.xz": XZ_3MIB }],
    ["cut short", { "manifest.json": JSON.stringify(MANIFEST), "flash.bin.xz": XZ_1MIB.subarray(0, 200) }]]) {
    const bindings = env();
    assert.equal((await send(post(tar(files)), bindings)).status, 400, why);
    assert.deepEqual(tars(bindings), [], why);
  }
  const firstStage = { ...MANIFEST, read: false };
  assert.equal((await send(post(tar({ "manifest.json": JSON.stringify(firstStage) })), env())).status, 201,
    "a first stage has no memory to hold");
});

test("the Worker finds the offered files in a readout the tool itself packs", () => {
  const made = spawnSync(process.env.AUTOFLASH_PYTHON || "python3", ["-c", `
import base64, sys
from tools.autoflash import readout
from tools.ewelink import offered_firmware
item = offered_firmware.Offered("a-name-long-enough-that-a-ustar-header-splits-it-into-a-prefix-and-a-name-"
                                "of-its-own-as-python-writes-one/user1.bin", "1.1.0", b"\\xe9offered app")
manifest = {"format": 1, "model": "NEWTHING", "offered": [{"file": "offered/" + item.name, "sha256": item.sha256}]}
sys.stdout.write(base64.b64encode(readout.package(manifest, {"flash.bin": b"flash"}, [item])).decode())
`], { cwd: new URL("../../", import.meta.url).pathname, encoding: "utf8" });
  assert.equal(made.status, 0, made.stderr);
  const body = Buffer.from(made.stdout, "base64");
  const files = readout.filesOf(body.buffer.slice(body.byteOffset, body.byteOffset + body.byteLength));
  const [name] = [...files.keys()].filter((key) => key.startsWith("offered/"));
  assert.match(name, /^offered\/a-name-long-enough-.*\/user1\.bin$/);
  assert.equal(createHash("sha256").update(files.get(name)).digest("hex"),
    createHash("sha256").update(Buffer.from("\xe9offered app", "latin1")).digest("hex"));
  assert.ok(files.has("flash.bin.xz"));
  assert.equal(readout.xzUnpacked(files.get("flash.bin.xz")), 5, "the size read as the tool's xz gives it");
});

test("asked first, a readout that would add nothing is not wanted - nor one its sender sent", async () => {
  const bindings = env();
  const askFrom = (address, asked = ASKED) => ask(asked, bindings, NOW, { "CF-Connecting-IP": address });
  await from(bindings, "192.0.2.1", { read: false });
  assert.equal((await askFrom("192.0.2.1")).why, "held", "its sender has one");
  assert.equal((await askFrom("192.0.2.2")).wanted, true, "another sender's would add one");
  await from(bindings, "192.0.2.2", { read: false });
  await from(bindings, "192.0.2.3", { read: false });
  assert.deepEqual(await askFrom("192.0.2.4"), { wanted: false, why: "held", held: [] });
  assert.equal((await askFrom("192.0.2.4", { ...ASKED, stock_version: "2.0.0" })).wanted, true);
  assert.equal((await askFrom("192.0.2.4", { ...ASKED, stage: "whole" })).wanted, true);
});

test("readouts waiting for review are held to their own room, and given it back once three agree", async () => {
  assert.equal(readout.PENDING_BYTES, 10 * 1024 ** 3);
  const bindings = env();
  bindings.DB.db.prepare("INSERT INTO counters (name, day, count, bytes) VALUES ('readouts:pending', NULL, 0, ?)")
    .run(readout.PENDING_BYTES - 10);
  assert.equal((await from(bindings, "192.0.2.1", { firmware: FIRMWARE })).status, 503, "no room for one more");
  bindings.DB.db.prepare("UPDATE counters SET bytes = 0 WHERE name = 'readouts:pending'").run();
  for (const address of ["192.0.2.1", "192.0.2.2", "192.0.2.3"]) {
    await from(bindings, address, { firmware: FIRMWARE });
  }
  assert.deepEqual(await read(bindings.DB, "readouts:pending"), { count: 0, bytes: 0 }, "agreed: none waiting");
});

test("two IPv6 addresses in one /48 are one sender", async () => {
  // So one rented server's 65,536 /64s cannot be the three senders that agree.
  const bindings = env();
  assert.equal((await from(bindings, "2001:db8:1:2::10", { read: false })).status, 201);
  assert.equal((await from(bindings, "2001:db8:1:ffff::99", { read: false })).status, 200, "the same sender's again");
  assert.equal((await from(bindings, "2001:db8:2::10", { read: false })).status, 201, "the next /48 is another");
});

test("a readout naming its product with anything but a model's name is turned away, and its tool is cleaned", async () => {
  const bindings = env();
  assert.equal((await from(bindings, "192.0.2.1", { model: "NEW\u001b[2JTHING" })).status, 400);
  // "|" joins a key's parts (keyOf): in a name, two devices could share one key.
  assert.equal((await from(bindings, "192.0.2.1", { model: "NEW|THING" })).status, 400);
  assert.equal((await from(bindings, "192.0.2.1", { model: "NEW THING" })).status, 400);
  assert.equal((await from(bindings, "192.0.2.1", { tool: "2.4\u001b[2J" })).status, 201);
  const [key] = tars(bindings);
  assert.equal(bindings.READOUTS.objects.get(key).options.customMetadata.tool, "2.4[2J");
});

test("a device enough readouts of are in is thanked, not kept", async () => {
  const bindings = env({ enough: "OTHER, newthing" });
  const response = await send(post(readoutOf()), bindings);
  assert.deepEqual([response.status, await response.json()], [200, { enough: true }]);
  assert.equal(bindings.READOUTS.objects.size, 0);
});

test("what it may keep costs under a dollar a month, a flood takes months to fill it, and nothing expires", () => {
  const free = 10, perGbMonth = 0.015;   // R2, 2026
  assert.ok((readout.TOTAL_BYTES / 1024 ** 3 - free) * perGbMonth < 1);
  assert.ok(readout.TOTAL_BYTES / readout.DAILY_BYTES > 90, "three months of a day's limit, every day");
  assert.ok(readout.PENDING_BYTES < readout.TOTAL_BYTES / 2, "room left for readouts taken as right");
  const settings = readFileSync(new URL("../../tools/repo/cloudflare_settings.py", import.meta.url), "utf8");
  assert.match(settings, /^READOUTS_LIFECYCLE = \{"rules": \[\]\}$/m);
});

// The bytes of every readout ever kept, as the database holds them.
const storeHolds = (bindings, bytes) => bindings.DB.db
  .prepare("INSERT INTO counters (name, day, count, bytes) VALUES ('readouts:all', NULL, 1, ?)").run(bytes);

test("every readout kept counts towards the store's limit", async () => {
  const bindings = env();
  let bytes = 0;
  for (const version of ["1.0.0", "1.0.1"]) {
    const body = readoutOf({ ...MANIFEST, stock_version: version });
    bytes += body.byteLength;
    await send(post(body), bindings);
  }
  assert.deepEqual(await read(bindings.DB, "readouts:all"), { count: 2, bytes });
});

test("the store's limit is never passed, and the day's place is given back", async () => {
  const bindings = env();
  storeHolds(bindings, readout.TOTAL_BYTES - 1);
  const response = await send(post(readoutOf()), bindings);
  assert.equal(response.status, 503);
  assert.match((await response.json()).error, /store of readouts is full/);
  assert.deepEqual(await read(bindings.DB, "readouts:2026-11-07"), { count: 0, bytes: 0 });
  assert.equal(tars(bindings).length, 0);
});

test("a readout not kept after all gives back its place in the store too", async () => {
  const bindings = env();
  bindings.READOUTS.put = async () => {
    throw new Error("R2 down");
  };
  await assert.rejects(send(post(readoutOf()), bindings), /R2 down/);
  assert.deepEqual(await read(bindings.DB, "readouts:all"), { count: 0, bytes: 0 });
});

test("the full work is asked, whatever the Worker is given", async () => {
  // A variable or secret, as the tests once lowered it by, changes nothing.
  const bindings = { ...env(), TEST_PROOF_BITS: "1", PROOF_BITS: "1", LIGHT_PROOF_BITS: "1" };
  readout.work.bits = null;
  try {
    assert.equal((await send(post(readoutOf()), bindings)).status, 403);
    assert.equal((await readout.wanted(question(), bindings, NOW)).status, 403);
  } finally {
    readout.work.bits = BITS;
  }
});

test("the API and the admin view log and trace every request, for a failure to be looked into, and say so", () => {
  for (const file of ["../api/wrangler.jsonc", "../admin/wrangler.jsonc"]) {
    const config = readFileSync(new URL(file, import.meta.url), "utf8");
    const setting = config.split("\n").find((line) => line.trim().startsWith('"observability"'));
    assert.ok(setting, `${file} says nothing of observability`);
    assert.deepEqual(JSON.parse(setting.trim().replace(/^"observability":\s*/, "").replace(/,$/, "")),
      { enabled: true, head_sampling_rate: 1, logs: { enabled: true, invocation_logs: true },
        traces: { enabled: true, head_sampling_rate: 1 } },
      file);
  }
  const told = readFileSync(new URL("../../docs/what-the-tool-sends.md", import.meta.url), "utf8");
  assert.match(told, /Cloudflare logs\s+each request and keeps a trace of it/);
  assert.match(told, /for seven days/);
});

test("the tool does the same work the Worker asks", () => {
  const tool = readFileSync(new URL("../../tools/autoflash/readout.py", import.meta.url), "utf8");
  assert.match(tool, new RegExp(`^PROOF_BITS = ${readout.PROOF_BITS}\\b`, "m"));
  assert.match(tool, new RegExp(`^LIGHT_PROOF_BITS = ${readout.LIGHT_PROOF_BITS}\\b`, "m"));
  
});

test("its pages cannot be put inside another site's, and no answer is read as other than it is", async () => {
  const bindings = env();
  const page = await readout.default.fetch(new Request("https://api.firmwareliberator.com/confirm?t=not-a-token"), bindings);
  assert.match(page.headers.get("Content-Security-Policy"), /(^|; )frame-ancestors 'none'(;|$)/);
  assert.equal(page.headers.get("X-Content-Type-Options"), "nosniff");
  const json = await worker.fetch(new Request("https://api.firmwareliberator.com/"), bindings);
  assert.equal(json.headers.get("X-Content-Type-Options"), "nosniff");
});

test("every answer names the code deployed, as the release waits to see it", async () => {
  const tree = "0123456789abcdef0123456789abcdef01234567";
  const named = { ...env(), API_TREE: tree };
  const answers = [
    await worker.fetch(new Request("https://api.firmwareliberator.com/"), named),
    await worker.fetch(new Request("https://api.firmwareliberator.com/confirm?t=not-a-token"), named),
    await worker.fetch(new Request("https://api.firmwareliberator.com/v1/notify", { method: "OPTIONS",
      headers: { Origin: readout.SITE_ORIGIN } }), named),
    await worker.fetch(new Request("https://api.firmwareliberator.com/confirm?t=not-a-token"),
      { ...named, INGEST: "off" }),
  ];
  assert.deepEqual(answers.map((answer) => [answer.status, answer.headers.get("API-Tree")]),
    [[404, tree], [404, tree], [204, tree], [503, tree]]);
  // Its other headers kept.
  assert.equal(answers[1].headers.get("X-Content-Type-Options"), "nosniff");
  assert.ok(answers[2].headers.get("Access-Control-Allow-Origin"));
  assert.equal((await worker.fetch(new Request("https://api.firmwareliberator.com/"), env())).headers.get("API-Tree"),
    null);
});

test("the Worker can send email from NOTIFY_FROM alone", () => {
  const config = readFileSync(new URL("../api/wrangler.jsonc", import.meta.url), "utf8");
  const binding = config.split("\n").find((line) => line.includes('"name": "EMAIL"'));
  assert.deepEqual(JSON.parse(binding.trim().replace(/,$/, "")),
    { name: "EMAIL", allowed_sender_addresses: [readout.NOTIFY_FROM] });
});

test("every other address is not found, and the site is not this Worker", async () => {
  const response = await worker.fetch(new Request("https://api.firmwareliberator.com/"), env());
  assert.equal(response.status, 404);
  const site = readFileSync(new URL("../wrangler.jsonc", import.meta.url), "utf8");
  assert.doesNotMatch(site, /readout|api\//i);
});

const ASKED = { format: 1, model: "NEWTHING", stock_model: "SN-NEW-01", stock_version: "1.0.0", stage: "first", firmware: [] };
const ASKED_WHOLE = { ...ASKED, stage: "whole" };

function question(asked = ASKED, headers = {}, body = Buffer.from(JSON.stringify(asked)), path = "/v1/wanted") {
  return new Request(`https://api.firmwareliberator.com${path}`, {
    method: "POST",
    body,
    headers: { "Content-Type": "application/json", "Content-Length": String(body.byteLength),
      "X-Proof": proofFor(body), ...headers },
  });
}

// Each question with a field of its own, as the tool's are, so its proof is
// its own: the Worker takes a proof once.
let asked = 0;
const fresh = (value) => ({ ...value, nonce: `n${(asked += 1)}` });

const ask = async (question_, bindings, now = NOW, headers = {}) => {
  const body = Buffer.from(JSON.stringify(fresh(question_)));
  const asked = question_;
  const proof = proofFor(body, BITS, Math.floor(now.getTime() / 1000));   // made as it is asked
  return (await readout.wanted(question(asked, { "X-Proof": proof, ...headers }, body), bindings, now)).json();
};

test("asked first, the question is counted by its model for the figures", async () => {
  const bindings = env();
  await ask({ ...ASKED, model: "NewThing" }, bindings);
  await ask({ ...ASKED, model: "NEWTHING", stage: "whole" }, bindings);
  assert.deepEqual(bindings.DB.db.prepare("SELECT day, model, asked FROM wanted_days").all().map((row) => ({ ...row })),
    [{ day: "2026-11-07", model: "NEWTHING", asked: 2 }]);
});

test("asked first, each device is counted once a day, and kept as a keyed hash with its first and last day", async () => {
  const bindings = { ...env(), DEVICE_HASH_KEY_PERMANENT: "a device key for the tests" };
  const [one, two] = ["a", "b"].map((digit) => digit.repeat(64));
  const later = new Date("2026-11-09T12:00:00Z");
  await ask({ ...ASKED, device_hash: one }, bindings);
  await ask({ ...ASKED, device_hash: one, stage: "whole" }, bindings);   // the same device again: one
  await ask({ ...ASKED, device_hash: two }, bindings);
  await ask({ ...ASKED }, bindings);   // an earlier tool's, naming none: a question, no device
  await ask({ ...ASKED, device_hash: one }, bindings, later);
  const rows = (sql) => bindings.DB.db.prepare(sql).all().map((row) => ({ ...row }));
  assert.deepEqual(rows("SELECT day, asked, devices FROM wanted_days ORDER BY day"),
    [{ day: "2026-11-07", asked: 4, devices: 2 }, { day: "2026-11-09", asked: 1, devices: 1 }]);
  const kept = rows("SELECT * FROM wanted_devices ORDER BY first_day, asks DESC");
  assert.deepEqual(kept.map(({ model, first_day, last_day, asks }) => [model, first_day, last_day, asks]),
    [["NEWTHING", "2026-11-07", "2026-11-09", 3], ["NEWTHING", "2026-11-07", "2026-11-07", 1]]);
  assert.ok(kept.every((row) => /^[0-9a-f]{64}$/.test(row.device_hash) && ![one, two].includes(row.device_hash)),
    "kept keyed, never as sent");
  assert.equal((await ask({ ...ASKED, device_hash: "not a hash" }, bindings)).error, "that is not a question this tool asks");
});

test("asked first, a device enough readouts of are in is not wanted, any other is", async () => {
  const bindings = env({ enough: "S60TPG, basicr4" });
  for (const [product, wanted] of [["BASICR4", false], ["s60tpg", false], ["NEWTHING", true]]) {
    const answer = await ask({ ...ASKED, model: product }, bindings);
    assert.equal(answer.wanted, wanted, product);
    if (!wanted) assert.equal(answer.why, "enough");
  }
  assert.equal(bindings.READOUTS.objects.size, 0);
});

test("twenty readouts a day are kept from one sender, counted under a keyed hash", async () => {
  assert.equal(readout.SENDER_DAILY, 20);
  const bindings = env();
  const from = (address, tool) => send(post(readoutOf({ ...MANIFEST, tool, stock_version: tool }),
    { "CF-Connecting-IP": address }), bindings);
  for (let n = 0; n < readout.SENDER_DAILY; n += 1) {
    assert.equal((await from("192.0.2.9", `1.0.${n}`)).status, 201, `readout ${n + 1}`);
  }
  const over = await from("192.0.2.9", "1.0.99");
  assert.equal(over.status, 503);
  assert.equal((await over.json()).limit, true);
  assert.equal((await from("192.0.2.10", "1.0.99")).status, 201, "another sender's are their own");
  assert.equal((await ask({ ...ASKED, stock_version: "3.0.0" }, bindings, NOW, { "CF-Connecting-IP": "192.0.2.9" })).why,
    "limit", "asked from the sender at its limit");
  assert.equal((await ask({ ...ASKED, stock_version: "3.0.0" }, bindings, NOW, { "CF-Connecting-IP": "192.0.2.11" })).wanted,
    true, "and from another");
  const names = (await bindings.DB.prepare("SELECT name FROM counters").all()).results.map((row) => row.name);
  assert.ok(names.some((name) => name.startsWith("sender:")));
  assert.ok(!names.some((name) => name.includes("192.0.2")), "never the address itself");
});

test("a readout not kept gives its sender's place back", async () => {
  const bindings = env();
  for (let n = 0; n < readout.SENDER_DAILY + 3; n += 1) {
    await send(post(readoutOf({ ...MANIFEST, tool: `${n}` })), bindings);   // after two, held: not kept
  }
  assert.equal((await send(post(readoutOf({ ...MANIFEST, stock_version: "4.0.0" })), bindings)).status, 201);
});

test("without its SENDER_KEY no readout is taken", async () => {
  const bindings = env();
  delete bindings.SENDER_KEY;
  assert.equal((await send(post(readoutOf()), bindings)).status, 503);
  assert.equal(tars(bindings).length, 0);
});

test("asked first, the files held already are named, wanted or not", async () => {
  const bindings = env();
  const { offered, files } = offering("an app");
  await send(post(readoutOf({ ...MANIFEST, model: "OTHERTHING", offered }, files)), bindings);
  const answer = await ask({ ...ASKED, firmware: [offered[0].sha256, "c".repeat(64)] }, bindings);
  assert.deepEqual(answer, { wanted: true, held: [offered[0].sha256] });
  bindings.READOUTS.objects.set(`readouts/firmware/${"c".repeat(64)}`, { size: 1 });
  assert.deepEqual((await ask({ ...ASKED, firmware: ["c".repeat(64)] }, bindings)).held, ["c".repeat(64)]);
});

test("asked first, a full store says so, whatever the day", async () => {
  const bindings = env();
  storeHolds(bindings, readout.TOTAL_BYTES);
  assert.equal((await ask({ ...ASKED, model: "FULL2" }, bindings)).why, "store-full");
  assert.equal((await ask({ ...ASKED, model: "FULL2" }, bindings, new Date("2026-12-07T12:00:00Z"))).why,
    "store-full");
});

test("asked first, a full day says so, by count or by bytes, and the next day does not", async () => {
  const at = new Date("2026-11-07T12:00:00Z");
  for (const [count, bytes] of [[readout.DAILY_COUNT, 1], [1, readout.DAILY_BYTES]]) {
    const bindings = env();
    dayCount(bindings, "2026-11-07", count, bytes);
    assert.deepEqual(await ask({ ...ASKED, model: "FULL1" }, bindings, at), { wanted: false, why: "full", held: [] });
    assert.equal((await ask({ ...ASKED, model: "FULL1" }, bindings, new Date("2026-11-08T00:00:01Z"))).wanted, true);
  }
});

test("each question needs work of its own, lighter than a readout's and newer", async () => {
  const body = Buffer.from(JSON.stringify(ASKED));
  const other = Buffer.from(JSON.stringify({ ...ASKED, model: "OTHERTHING" }));
  const minuteAgo = proofFor(body, BITS, NOW.getTime() / 1000 - readout.LIGHT_PROOF_AGE - 5);
  for (const proof of ["", "1:2:3", proofFor(other), minuteAgo]) {
    assert.equal((await readout.wanted(question(ASKED, { "X-Proof": proof }, body), env(), NOW)).status, 403, proof);
  }
  assert.ok(readout.LIGHT_PROOF_BITS < readout.PROOF_BITS);
  assert.ok(readout.LIGHT_PROOF_AGE < readout.PROOF_AGE);
});

test("a question is held to no readout limit, so a batch's leave its readouts theirs", async () => {
  const bindings = { ...env(), READOUT_LIMIT: { limit: async () => assert.fail("asked the readout limiter") } };
  assert.equal((await readout.wanted(question(), bindings, NOW)).status, 200);
});

test("anything but the tool's question is turned away", async () => {
  const bindings = env();
  const json = (value) => Buffer.from(JSON.stringify(value));
  const badly = [
    [new Request("https://api.firmwareliberator.com/v1/wanted"), 405],
    [question(ASKED, { "Content-Type": "text/plain" }), 415],
    [question(ASKED, {}, json({ ...ASKED, model: "" })), 400],
    [question(ASKED, {}, json({ ...ASKED, model: "NEW|THING" })), 400],
    [question(ASKED, {}, json({ ...ASKED, stock_model: "SN|NEW" })), 400],
    [question(ASKED, {}, json({ ...ASKED, stock_version: "1.0 beta" })), 400],
    [question(ASKED, {}, json({ ...ASKED, stage: "some" })), 400],
    [question(ASKED, {}, json({ ...ASKED, stock_version: undefined })), 400],
    [question(ASKED, {}, json({ ...ASKED, firmware: ["not a hash"] })), 400],
    [question(ASKED, {}, json({ ...ASKED, firmware: Array(readout.MAX_FIRMWARE + 1).fill("a".repeat(64)) })), 400],
    [question(ASKED, {}, json({ ...ASKED, format: 2 })), 400],
    [question(ASKED, {}, Buffer.from("not json")), 400],
    [question(ASKED, {}, Buffer.alloc(readout.QUESTION_MAX_BYTES + 1, 32)), 413],
  ];
  for (const [request, status] of badly) {
    assert.equal((await readout.wanted(request, bindings, NOW)).status, status);
  }
});

test("the Worker sends each path to its own", async () => {
  const signUp = { format: 1, email: "me@example.com", devices: [{ model: "NEWTHING", stock_model: "SN-NEW-01" }] };
  for (const [path, value, status] of [["/v1/wanted", ASKED, 200], ["/v1/notify", signUp, 202]]) {
    const body = Buffer.from(JSON.stringify(fresh(value)));
    const now = proofFor(body, BITS, Math.floor(Date.now() / 1000));   // fetch() checks it against the clock
    const response = await worker.fetch(question(value, { "X-Proof": now }, body, path), env());
    assert.equal(response.status, status, path);
  }
  for (const path of ["/confirm", "/forget"]) {
    assert.equal((await worker.fetch(new Request(`https://api.firmwareliberator.com${path}`), env())).status, 404,
      `${path}, opened with no link's token`);
  }
});

test("switched off (INGEST), every path asks the tool to wait", async () => {
  for (const path of Object.keys(readout.ROUTES)) {
    for (const ingest of ["off", undefined]) {
      const response = await worker.fetch(new Request(`https://api.firmwareliberator.com${path}`, { method: "POST" }),
        { ...env(), INGEST: ingest });
      assert.equal(response.status, 503, `${path} with INGEST ${ingest}`);
      assert.equal(response.headers.get("Retry-After"), "3600");
    }
  }
});

test("a light proof is taken once: the same question sent again within its minute is not", async () => {
  const bindings = env();
  const body = Buffer.from(JSON.stringify(ASKED));
  const proof = proofFor(body, BITS);
  const send = () => readout.wanted(question(ASKED, { "X-Proof": proof }, body), bindings, NOW);
  assert.equal((await send()).status, 200);
  const again = await send();
  assert.equal(again.status, 403);
  assert.match((await again.json()).error, /used already/);
});

const NOTIFY = { format: 1, email: " Me@Example.com ", devices: [
  { model: "NEWTHING", stock_model: "SN-NEW-01" }, { model: "newthing", stock_model: "SN-NEW-02" },
  { model: "BASICR4", stock_model: "-" }], readouts: ["2026-11-07-0123456789ab"] };
const tell = (value, bindings, now = NOW, headers = {}) => {
  const body = Buffer.from(JSON.stringify(fresh(value)));
  const proof = proofFor(body, BITS, Math.floor(now.getTime() / 1000));   // made as it is asked
  return readout.notify(question(value, { "X-Proof": proof, ...headers }, body, "/v1/notify"), bindings, now);
};
const linkIn = (message) => /https:\/\/api\.firmwareliberator\.com\/confirm\?t=([0-9a-f]{32})/.exec(message.text);
const confirmBy = (token, bindings) => readout.confirm(new Request("https://api.firmwareliberator.com/confirm",
  { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ t: token }).toString() }), bindings, NOW);
// What the database keeps of confirmed addresses, each table's rows.
const kept = (bindings) => Object.fromEntries(["subscribers", "subscriber_devices", "subscriber_readouts"]
  .map((table) => [table, bindings.DB.db.prepare(`SELECT * FROM ${table} ORDER BY who, 2, ${
    table === "subscriber_devices" ? 3 : 2}`).all()]));
const nothingKept = { subscribers: [], subscriber_devices: [], subscriber_readouts: [] };

// An address given, and confirmed through the link emailed to it: on `now`'s
// day, as an address is sent one confirmation a day.
async function signedUp(bindings, value = NOTIFY, now = NOW) {
  assert.equal((await tell(value, bindings, now)).status, 202);
  const [, token] = linkIn(bindings.EMAIL.sent.at(-1));
  assert.equal((await confirmBy(token, bindings)).status, 200);
}

test("an address given is only sent a link to confirm it: nothing is kept until it is followed", async () => {
  const bindings = env();
  const response = await tell(NOTIFY, bindings);
  assert.deepEqual([response.status, await response.json()], [202, { confirming: true }]);
  assert.deepEqual(kept(bindings), nothingKept, "not on the list");
  const [message] = bindings.EMAIL.sent;
  assert.deepEqual([message.to, message.from], ["Me@Example.com", readout.NOTIFY_FROM]);
  assert.match(message.text, /as more\ndevices are supported, and when the support for these changes: NEWTHING, BASICR4/);
  assert.match(message.text, /If it was not you, do nothing/);
  assert.ok(linkIn(message), "it carries its link");
});

test("the link asks first: opening it, as a mail scanner may, confirms nothing", async () => {
  const bindings = env();
  await tell(NOTIFY, bindings);
  const [link] = linkIn(bindings.EMAIL.sent[0]);
  const response = await readout.confirm(new Request(link), bindings, NOW);
  assert.equal(response.status, 200);
  assert.match(await response.text(), /<form method="post" action="\/confirm">/);
  assert.deepEqual(kept(bindings), nothingKept);
});

test("confirmed, the address is kept once, with each of its devices and readouts, and the link is used up", async () => {
  const bindings = env({ enough: "NEWTHING" });   // wanted or not, an address is kept
  await signedUp(bindings);
  const { subscribers, subscriber_devices: devices, subscriber_readouts: readouts } = kept(bindings);
  assert.equal(subscribers.length, 1);
  const [{ who, email, token }] = subscribers;
  assert.deepEqual([email, who], ["Me@Example.com", createHash("sha256").update("me@example.com").digest("hex")]);
  assert.match(token, /^[0-9a-f]{32}$/);
  assert.deepEqual(devices.map((row) => [row.model, row.stock_model]),
    [["BASICR4", "-"], ["NEWTHING", "SN-NEW-01"], ["NEWTHING", "SN-NEW-02"]]);
  assert.deepEqual(readouts.map((row) => row.readout), ["2026-11-07-0123456789ab"]);
  assert.equal(bindings.DB.db.prepare("SELECT count(*) AS n FROM confirmations").get().n, 0);
  const [, link] = linkIn(bindings.EMAIL.sent[0]);
  assert.equal((await confirmBy(link, bindings)).status, 404, "used once");
});

test("a confirmed address's later sign-ups add devices and readouts, never take any away, and email nothing",
  async () => {
    const bindings = env();
    await signedUp(bindings);
    const sent = bindings.EMAIL.sent.length;
    const later = new Date("2026-11-08T12:00:00Z");
    assert.equal((await tell({ ...NOTIFY, devices: [{ model: "NSPANEL", stock_model: "E32-MSW-NX" },
      { model: "BASICR4", stock_model: "-" }], readouts: ["2026-11-08-ba9876543210"] }, bindings, later)).status, 202);
    assert.equal(bindings.EMAIL.sent.length, sent, "no second confirmation");
    const { subscribers, subscriber_devices: devices, subscriber_readouts: readouts } = kept(bindings);
    assert.equal(subscribers.length, 1, "still one address");
    // A device gone from the account was most likely converted, not given away.
    assert.deepEqual(devices.map((row) => [row.model, row.first_seen, row.last_seen]), [
      ["BASICR4", NOW.toISOString(), later.toISOString()], ["NEWTHING", NOW.toISOString(), NOW.toISOString()],
      ["NEWTHING", NOW.toISOString(), NOW.toISOString()], ["NSPANEL", later.toISOString(), later.toISOString()]]);
    assert.deepEqual(readouts.map((row) => row.readout), ["2026-11-07-0123456789ab", "2026-11-08-ba9876543210"]);
  });

test("an update for an address not confirmed - removed, or never confirmed - starts nothing", async () => {
  // The tool sends each later sitting's devices with the address given
  // before: one who left, or never confirmed, is not emailed again.
  const bindings = env();
  const response = await tell({ ...NOTIFY, update: true }, bindings);
  assert.deepEqual([response.status, await response.json()], [202, { confirming: true }], "answered as any other");
  assert.equal(bindings.EMAIL.sent.length, 0);
  assert.equal(bindings.DB.db.prepare("SELECT count(*) AS n FROM confirmations").get().n, 0);
  assert.deepEqual(kept(bindings), nothingKept);
  assert.deepEqual(await read(bindings.DB, "notify:2026-11-07"), { count: 0, bytes: 0 }, "none of the day's emails");
});

test("an update for a confirmed address adds to it, with no email", async () => {
  const bindings = env();
  await signedUp(bindings);
  const sent = bindings.EMAIL.sent.length;
  await tell({ ...NOTIFY, devices: [{ model: "NSPANEL", stock_model: "E32-MSW-NX" }], update: true }, bindings,
    new Date("2026-11-08T12:00:00Z"));
  assert.equal(bindings.EMAIL.sent.length, sent);
  assert.ok(kept(bindings).subscriber_devices.some((row) => row.model === "NSPANEL"));
});

test("an address with no devices seen is taken: for news of any device", async () => {
  const bindings = env();
  await signedUp(bindings, { format: 1, email: "me@example.com", devices: [] });
  assert.match(bindings.EMAIL.sent[0].text, /as more\ndevices are supported\.\n/);
  assert.equal(kept(bindings).subscribers.length, 1);
});

test("one confirmation an address a day, however many ask for it", async () => {
  const bindings = env();
  for (const address of ["192.0.2.1", "192.0.2.2", "192.0.2.3"]) {
    assert.equal((await tell({ ...NOTIFY, email: "Me@Example.com" }, bindings, NOW, { "CF-Connecting-IP": address })).status, 202);
  }
  assert.equal(bindings.EMAIL.sent.length, 1, "a stranger cannot be emailed again and again");
  await tell(NOTIFY, bindings, new Date("2026-11-08T12:00:00Z"));
  assert.equal(bindings.EMAIL.sent.length, 2, "the next day, once more");
});

test("five sign-ups a day from one sender, so nobody can have many strangers emailed", async () => {
  assert.equal(readout.NOTIFY_SENDER_DAILY, 5);
  const bindings = env();
  for (let n = 0; n < readout.NOTIFY_SENDER_DAILY; n += 1) {
    assert.equal((await tell({ ...NOTIFY, email: `owner${n}@example.com` }, bindings)).status, 202);
  }
  assert.equal((await tell({ ...NOTIFY, email: "another@example.com" }, bindings)).status, 503);
  assert.equal(bindings.EMAIL.sent.length, readout.NOTIFY_SENDER_DAILY);
  assert.equal((await tell({ ...NOTIFY, email: "another@example.com" }, bindings, NOW,
    { "CF-Connecting-IP": "2001:db8:9:9::1" })).status, 202, "another sender's are their own");
});

test("without an email to send with, no address is taken", async () => {
  const bindings = env();
  delete bindings.EMAIL;
  assert.equal((await tell(NOTIFY, bindings)).status, 503);
  assert.equal((await bindings.DB.prepare("SELECT count(*) AS n FROM confirmations").first()).n, 0);
});

test("a confirmation that cannot be sent leaves nothing, and may be asked for again", async () => {
  const bindings = env();
  bindings.EMAIL = { send: async () => { throw new Error("E_SENDER_NOT_VERIFIED"); } };
  assert.equal((await tell(NOTIFY, bindings)).status, 502);
  assert.equal((await bindings.DB.prepare("SELECT count(*) AS n FROM confirmations").first()).n, 0);
  bindings.EMAIL = mailer();
  assert.equal((await tell(NOTIFY, bindings)).status, 202);
  assert.equal(bindings.EMAIL.sent.length, 1);
});

const at = (time) => new Date(`2026-11-${time}Z`);

test("an address not confirmed is sent its link once more a day after, and only once", async () => {
  assert.equal(readout.REMIND_AFTER_DAYS, 1);
  const bindings = env();
  await tell(NOTIFY, bindings);   // at 2026-11-07T12:00
  assert.equal(await readout.remind(bindings, at("08T03:17:00")), 0, "not the next night: a day after");
  assert.equal(await readout.remind(bindings, at("08T11:47:00")), 0, "too soon");
  assert.equal(await readout.remind(bindings, at("08T12:47:00")), 1);
  const [first, again] = bindings.EMAIL.sent;
  assert.match(again.subject, /^Reminder/);
  assert.match(again.text, /on 2026-11-07/);
  assert.match(again.text, /This is the only reminder/);
  assert.equal(linkIn(again)[1], linkIn(first)[1], "the same link");
  assert.equal(await readout.remind(bindings, at("08T12:47:00")), 0, "a run twice reminds nobody twice");
  assert.equal(await readout.remind(bindings, at("09T12:47:00")), 0);
  assert.equal(bindings.EMAIL.sent.length, 2);
  assert.equal((await confirmBy(linkIn(again)[1], bindings)).status, 200, "the reminder's link confirms it");
});

test("a reminder goes to none confirmed, and once an address however often it was given", async () => {
  const bindings = env();
  await signedUp(bindings);   // confirmed on 2026-11-07
  await tell(NOTIFY, bindings, at("08T12:00:00"));   // given again, not confirmed
  await tell({ ...NOTIFY, email: "other@example.com" }, bindings, at("08T12:00:00"));
  await tell({ ...NOTIFY, email: "other@example.com" }, bindings, at("09T12:00:00"));
  const before = bindings.EMAIL.sent.length;
  assert.equal(await readout.remind(bindings, at("11T03:47:00")), 1);
  assert.deepEqual(bindings.EMAIL.sent.slice(before).map((message) => message.to), ["other@example.com"]);
});

test("a reminder is counted against the day's emails, and one that fails is tried again", async () => {
  const bindings = env();
  await tell(NOTIFY, bindings);
  const send = bindings.EMAIL.send;
  bindings.EMAIL.send = async () => { throw new Error("E_RATE_LIMIT_EXCEEDED"); };
  assert.equal(await readout.remind(bindings, at("09T03:47:00")), 0);
  assert.deepEqual(await read(bindings.DB, "notify:2026-11-09"), { count: 0, bytes: 0 });
  bindings.EMAIL.send = send;
  await bindings.DB.prepare("INSERT INTO counters (name, day, count, bytes) VALUES (?1, ?2, ?3, 0)")
    .bind("notify:2026-11-10", "2026-11-10", readout.NOTIFY_DAILY).run();
  assert.equal(await readout.remind(bindings, at("10T03:47:00")), 0, "none left today");
  assert.equal(await readout.remind(bindings, at("11T03:47:00")), 1);
});

const suppressed = () => { throw Object.assign(new Error("Recipient is suppressed"), { code: "E_RECIPIENT_SUPPRESSED" }); };

test("an address that bounced for good, or complained, is answered as any other, kept nowhere, not tried again today",
  async () => {
    const bindings = env();
    bindings.EMAIL.send = async () => suppressed();
    const response = await tell(NOTIFY, bindings);
    assert.deepEqual([response.status, await response.json()], [202, { confirming: true }]);
    assert.equal((await bindings.DB.prepare("SELECT count(*) AS n FROM confirmations").first()).n, 0);
    assert.deepEqual(await read(bindings.DB, "notify:2026-11-07"), { count: 0, bytes: 0 }, "none of the day's 500");
    assert.equal((await read(bindings.DB, "mail-failed:E_RECIPIENT_SUPPRESSED:2026-11-07")).count, 1);
    let tried = 0;
    bindings.EMAIL.send = async () => { tried += 1; };
    assert.equal((await tell(NOTIFY, bindings, NOW, { "CF-Connecting-IP": "192.0.2.9" })).status, 202);
    assert.equal(tried, 0);
  });

test("a reminder to an address since suppressed is not tried again", async () => {
  const bindings = env();
  await tell(NOTIFY, bindings);
  let tried = 0;
  bindings.EMAIL.send = async () => { tried += 1; suppressed(); };
  assert.equal(await readout.remind(bindings, at("08T12:47:00")), 0);
  assert.equal(await readout.remind(bindings, at("08T13:47:00")), 0);
  assert.equal(tried, 1);
  assert.deepEqual(await read(bindings.DB, "notify:2026-11-08"), { count: 0, bytes: 0 });
});

test("a confirmation that fails to send gives back its place in the day's 500", async () => {
  const bindings = env();
  bindings.EMAIL = { send: async () => { throw new Error("E_INTERNAL_SERVER_ERROR"); } };
  assert.equal((await tell(NOTIFY, bindings)).status, 502);
  assert.deepEqual(await read(bindings.DB, "notify:2026-11-07"), { count: 0, bytes: 0 });
});

test("what tools/repo/api_health.py live asks after a deploy is answered as it expects, counting nothing", async () => {
  const bindings = env();
  const page = await readout.default.fetch(new Request("https://api.firmwareliberator.com/confirm?t=not-a-token"), bindings);
  assert.equal(page.status, 404);
  assert.match(await page.text(), /Nothing to confirm/);
  const body = Buffer.from(JSON.stringify({ format: 0, health: "check", nonce: "n-health" }));
  const proof = proofFor(body, BITS, Math.floor(Date.now() / 1000));
  const answered = await readout.default.fetch(question(null, { "X-Proof": proof }, body), bindings);
  assert.equal(answered.status, 400);
  assert.match((await answered.json()).error, /not a question this tool makes/);
  assert.equal((await bindings.DB.prepare("SELECT count(*) AS n FROM wanted_days").first()).n, 0);
  const off = await readout.default.fetch(new Request("https://api.firmwareliberator.com/confirm?t=x"),
    { ...bindings, INGEST: "off" });
  assert.equal(off.status, 503, "deployed taking none, as live off expects");
});

test("the confirmation page shows what a sender named as text, never as markup", async () => {
  // No such name gets in (below); were one ever kept, it is still escaped.
  const bindings = env();
  await tell(NOTIFY, bindings);
  const [link, token] = linkIn(bindings.EMAIL.sent[0]);
  bindings.DB.db.prepare("UPDATE confirmations SET devices = ? WHERE token = ?")
    .run(JSON.stringify([{ model: "<IMG SRC=X ONERROR=ALERT(1)>", stock_model: "-" }]), token);
  const html = await (await readout.confirm(new Request(link), bindings, NOW)).text();
  assert.ok(!html.includes("<IMG") && !html.includes("<img"), "escaped");
  assert.match(html, /&lt;IMG SRC=X ONERROR=ALERT\(1\)&gt;/);
});

const forgetLinkOf = (bindings) => {
  const [row] = kept(bindings).subscribers;
  return `${readout.FORGET_URL}?a=${row.who}&t=${row.token}`;
};

test("each address has one removal link, the same every time it is given", async () => {
  const bindings = env();
  await signedUp(bindings);
  const link = forgetLinkOf(bindings);
  assert.match(link, /^https:\/\/api\.firmwareliberator\.com\/forget\?a=[0-9a-f]{64}&t=[0-9a-f]{32}$/);
  // Given again once confirmed: taken with no link to follow.
  assert.equal((await tell({ ...NOTIFY, devices: [{ model: "NSPANEL", stock_model: "E32-MSW-NX" }] }, bindings,
    new Date("2026-11-08T12:00:00Z"))).status, 202);
  assert.equal(forgetLinkOf(bindings), link);
});

const opened = (link, bindings) => readout.forget(new Request(link), bindings);
const confirmed = (link, bindings) => {
  const url = new URL(link);
  return readout.forget(new Request("https://api.firmwareliberator.com/forget", { method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ a: url.searchParams.get("a"), t: url.searchParams.get("t") }).toString() }), bindings);
};

test("the removal link asks first: opening it, as a mail scanner may, removes nothing", async () => {
  const bindings = env();
  await signedUp(bindings);
  const before = kept(bindings);
  const response = await opened(forgetLinkOf(bindings), bindings);
  assert.equal(response.status, 200);
  assert.match(await response.text(), /<form method="post" action="\/forget">/);
  assert.match(response.headers.get("Content-Type"), /^text\/html/);
  assert.deepEqual(kept(bindings), before);
});

test("confirmed, the address goes, with its devices and readouts", async () => {
  const bindings = env();
  await signedUp(bindings);
  // Given again once confirmed: taken with no link to follow.
  assert.equal((await tell({ ...NOTIFY, devices: [{ model: "NSPANEL", stock_model: "E32-MSW-NX" }] }, bindings,
    new Date("2026-11-08T12:00:00Z"))).status, 202);
  const link = forgetLinkOf(bindings);
  assert.equal((await confirmed(link, bindings)).status, 200);
  assert.deepEqual(kept(bindings), nothingKept);
  assert.equal((await opened(link, bindings)).status, 404, "a second time, nothing to remove");
});

test("a mail program's one-click unsubscribe removes it, posting to the link itself", async () => {
  // RFC 8058: List-Unsubscribe-Post: List-Unsubscribe=One-Click, posted to the
  // List-Unsubscribe link with that body alone.
  const bindings = env();
  await signedUp(bindings);
  const link = forgetLinkOf(bindings);
  const response = await readout.forget(new Request(link, { method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: "List-Unsubscribe=One-Click" }), bindings);
  assert.equal(response.status, 200);
  assert.deepEqual(kept(bindings), nothingKept);
  assert.match(await response.text(), new RegExp(`Changed your mind\\? <a href="${readout.SIGNUP_PAGE}">`),
    "the way back in");
  assert.equal(new URL(readout.SIGNUP_PAGE).pathname, "/notify");
});

test("a wrong token, or another address's, removes nothing", async () => {
  const bindings = env();
  await signedUp(bindings);
  const link = forgetLinkOf(bindings);
  const before = kept(bindings);
  for (const wrong of [link.replace(/t=[0-9a-f]{32}/, `t=${"0".repeat(32)}`), link.replace(/t=.*/, "t="),
    link.replace(/a=[0-9a-f]{64}/, `a=${"f".repeat(64)}`), `${readout.FORGET_URL}?a=../x&t=1`]) {
    assert.equal((await confirmed(wrong, bindings)).status, 404, wrong);
  }
  assert.deepEqual(kept(bindings), before);
  assert.equal((await readout.forget(new Request(link, { method: "PUT" }), bindings)).status, 405);
});

test("an address is kept in the database alone, never in the bucket of readouts", async () => {
  const bindings = env();
  await signedUp(bindings);
  for (const [key, item] of bindings.READOUTS.objects) {
    assert.ok(!String(item.text ?? "").includes("xample"), key);
  }
});

test("a day's addresses are limited too", async () => {
  const bindings = env();
  bindings.DB.db.prepare("INSERT INTO counters (name, day, count, bytes) VALUES (?, ?, ?, 0)")
    .run("notify:2026-11-07", "2026-11-07", readout.NOTIFY_DAILY - 1);
  assert.equal((await tell({ ...NOTIFY, email: "a@example.com" }, bindings)).status, 202, "the day's last");
  assert.equal((await tell({ ...NOTIFY, email: "b@example.com" }, bindings)).status, 503);
  assert.deepEqual(await read(bindings.DB, "notify:2026-11-07"), { count: readout.NOTIFY_DAILY, bytes: 0 });
});

test("anything but the tool's request to be told is turned away", async () => {
  assert.equal(readout.MAX_DEVICES, 20);
  for (const value of [{ ...NOTIFY, email: "not an email" }, { ...NOTIFY, email: "a\u001b[31m@example.com" },
    { ...NOTIFY, devices: [{ stock_model: "x" }] }, { ...NOTIFY, devices: undefined }, { ...NOTIFY, update: "yes" },
    { ...NOTIFY, readouts: ["not-a-reference"] }, { ...NOTIFY, readouts: "2026-11-07-0123456789ab" },
    { ...NOTIFY, readouts: Array(readout.MAX_READOUTS + 1).fill("2026-11-07-0123456789ab") },
    { ...NOTIFY, devices: [{ model: "NEW\nTHING", stock_model: "x" }] },
    // Nothing but a model's name: no sentence or link put into the email.
    { ...NOTIFY, devices: [{ model: "URGENT-VERIFY-AT", stock_model: "-" }, { model: "evil.example/login", stock_model: "-" }] },
    { ...NOTIFY, devices: [{ model: "Confirm your account", stock_model: "-" }] },
    { ...NOTIFY, devices: [{ model: "NEWTHING", stock_model: "see https://evil.example" }] },
    { ...NOTIFY, devices: Array(readout.MAX_DEVICES + 1).fill(NOTIFY.devices[0]) }, { ...NOTIFY, format: 2 }]) {
    const bindings = env();
    assert.equal((await tell(value, bindings)).status, 400, JSON.stringify(value).slice(0, 80));
    assert.equal(bindings.EMAIL.sent.length, 0);
  }
});

// Numbers in [0, 1) from `seed`, the same each time (mulberry32): a failure
// names its seed and step, and runs again alike.
function seeded(seed) {
  let state = seed;
  return () => {
    state = (state + 0x6d2b79f5) | 0;
    let mixed = Math.imul(state ^ (state >>> 15), 1 | state);
    mixed = (mixed + Math.imul(mixed ^ (mixed >>> 7), 61 | mixed)) ^ mixed;
    return ((mixed ^ (mixed >>> 14)) >>> 0) / 4294967296;
  };
}

// What must hold of one key's readouts however they came.
function holds(bindings, at) {
  const one = (sql, ...values) => bindings.DB.db.prepare(sql).get(...values);
  const none = (sql, why) => assert.equal(one(`SELECT count(*) AS n FROM (${sql})`).n, 0, `${at}: ${why}`);
  assert.ok(one(`SELECT count(DISTINCT fingerprint) AS n FROM readouts WHERE stage = 'whole'
    AND review IN ('quorum', 'approved')`).n <= 1, `${at}: one whole fingerprint taken as right at most`);
  assert.ok(one("SELECT count(*) AS n FROM readouts WHERE stage = 'whole' AND review != 'rejected'").n
    <= readout.MAX_PLACES, `${at}: MAX_PLACES whole ones at most`);
  none(`SELECT offer FROM readouts WHERE stage = 'first' AND review != 'rejected' GROUP BY offer
    HAVING count(*) > ${readout.MAX_PLACES}`, "MAX_PLACES first stages of an offer at most");
  none("SELECT sender FROM readouts WHERE stage = 'whole' GROUP BY sender HAVING count(*) > 1",
    "one whole readout from a sender");
  none("SELECT sender FROM readouts GROUP BY sender, offer HAVING count(*) > 1", "one readout of an offer from a sender");
  none(`SELECT id FROM readouts r WHERE stage = 'first' AND review = 'quorum'
    AND NOT EXISTS (SELECT 1 FROM agreed_offers a WHERE a.key = r.key AND a.offer = r.offer)`,
  "a first stage agreed only with its offer");
  none(`SELECT id FROM readouts r WHERE review = 'pending' AND stage = 'first'
    AND EXISTS (SELECT 1 FROM agreed_offers a WHERE a.key = r.key AND a.offer = r.offer)`,
  "none waits whose offer is agreed");
  none(`SELECT id FROM readouts r WHERE review = 'pending' AND stage = 'whole' AND EXISTS (SELECT 1 FROM readouts t
    WHERE t.key = r.key AND t.stage = 'whole' AND t.fingerprint = r.fingerprint AND t.review IN ('quorum', 'approved'))`,
  "none waits alike whole ones taken as right");
  none(`SELECT a.offer FROM agreed_offers a JOIN readouts r ON r.key = a.key AND r.offer = a.offer
    LEFT JOIN readout_networks n ON n.id = r.id GROUP BY a.offer
    HAVING count(*) < ${readout.PER_KEY} OR count(DISTINCT n.network) < ${readout.MIN_NETWORKS}`,
  "an offer agreed from PER_KEY readouts and MIN_NETWORKS networks");
  const waiting = one("SELECT count(*) AS count, coalesce(sum(bytes), 0) AS bytes FROM readouts WHERE review = 'pending'");
  const counted = one("SELECT count, bytes FROM counters WHERE name = ?", PENDING) ?? { count: 0, bytes: 0 };
  assert.deepEqual([counted.count, counted.bytes], [waiting.count, waiting.bytes], `${at}: the room for those waiting`);
}

test("whatever readouts come, in whatever order, the rules of places and agreement hold", async () => {
  const offers = [OFFER_A, OFFER_B, offeredAt("1.4.0", "c".repeat(64))];
  const firmware = [FIRMWARE, MADE_UP];
  // Ten networks, Amazon's (counting as none), and none named.
  const networks = [...Array.from({ length: 10 }, (_, at) => 64500 + at), 16509, null];
  for (const seed of [1, 2, 3, 4, 5, 6]) {
    const random = seeded(seed);
    const pick = (list) => list[Math.floor(random() * list.length)];
    const bindings = env();
    for (let step = 0; step < 300; step += 1) {
      const at = `seed ${seed} step ${step}`;
      const roll = random();
      if (roll < 0.75) {
        const offer = pick(offers);
        const manifest = random() < 0.4 ? offer : { ...offer, read: true, firmware: pick(firmware) };
        const sent = await from(bindings, `192.0.2.${1 + Math.floor(random() * 40)}`, manifest, pick(networks));
        assert.ok([200, 201].includes(sent.status), `${at}: ${sent.status}`);
      } else if (roll < 0.93) {
        // Time passes: everything held came that many days earlier.
        bindings.DB.db.prepare("UPDATE readouts SET received = strftime('%Y-%m-%dT%H:%M:%fZ', received, ?)")
          .run(`-${1 + Math.floor(random() * 20)} days`);
      } else {
        // The maintainer rejects one waiting, as readouts.py reject does.
        const waiting = bindings.DB.db.prepare("SELECT id, bytes FROM readouts WHERE review = 'pending'").all();
        if (waiting.length) {
          const row = pick(waiting);
          bindings.DB.db.prepare("UPDATE readouts SET review = 'rejected' WHERE id = ?").run(row.id);
          await release(bindings.DB, PENDING, row.bytes);
        }
      }
      holds(bindings, at);
    }
    const kept = bindings.DB.db.prepare("SELECT review, count(*) AS n FROM readouts GROUP BY review").all();
    assert.ok(kept.some((row) => row.review === "quorum"), `seed ${seed}: some came to agree (${JSON.stringify(kept)})`);
  }
});
