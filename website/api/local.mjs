// The API Worker (index.js) on this computer, where both emulators' runs send
// their readouts (tools/repo/emulator-room.sh's local_api_start): the
// Worker's own code, with an R2 bucket that keeps what it is given in a folder,
// its database in that folder too (api.sqlite, local-d1.mjs), and rate
// limiters holding READOUT_LIMIT's and REPORT_LIMIT's rules - two and three a
// minute from one address, in a fixed minute's window. Never deployed.
//   node website/api/local.mjs ADDRESS PORT FOLDER [ENOUGH]
// AUTOFLASH_LOCAL_PROOF_BITS, for a test, lowers the work every path asks
// (proof-of-work.js's work.bits), so a test's tool need not do the full work;
// AUTOFLASH_LOCAL_READOUT_LIMIT, the readouts a minute it takes, so a test can
// reach the limit however slowly its readouts come.
import fs from "node:fs";
import http from "node:http";
import path from "node:path";

// local-d1.mjs's database is node:sqlite, which Node 22.11 has only behind a
// flag and 22.14 misreads (numbered parameters, ?1, out of range): this was
// tried on 22.23, and is refused on anything older rather than failing later.
const [major, minor] = process.versions.node.split(".").map(Number);
if (major < 22 || (major === 22 && minor < 23)) {
  console.error(`website/api/local.mjs needs Node 22.23 or newer, not ${process.versions.node}`);
  process.exit(2);
}

// After the check: an older Node fails as these are loaded.
const { default: worker } = await import("./index.js");
const { d1 } = await import("./local-d1.mjs");
const { work } = await import("./proof-of-work.js");
if (process.env.AUTOFLASH_LOCAL_PROOF_BITS) {
  work.bits = Number(process.env.AUTOFLASH_LOCAL_PROOF_BITS);
}

const [address, port, folder, enough = ""] = process.argv.slice(2);
fs.mkdirSync(folder, { recursive: true });
const file = (key) => path.join(folder, key.replaceAll("/", "__"));

const READOUTS = {
  head: async (key) => (fs.existsSync(file(key)) ? { key } : null),
  get: async (key) => (fs.existsSync(file(key)) ? { text: async () => fs.readFileSync(file(key), "utf8") } : null),
  list: async ({ prefix }) => ({
    objects: fs.readdirSync(folder).map((name) => name.replaceAll("__", "/")).filter((key) => key.startsWith(prefix))
      .map((key) => ({ key, size: fs.statSync(file(key)).size })),
  }),
  delete: async (key) => fs.rmSync(file(key), { force: true }),
  put: async (key, body, options) => {
    fs.writeFileSync(file(key), typeof body === "string" ? body : Buffer.from(body));
    fs.writeFileSync(`${file(key)}.meta.json`, JSON.stringify(options ?? {}));
  },
};
// Cloudflare's rate limiter, as wrangler.jsonc sets each: `most` a minute.
function limiter(most) {
  const windows = new Map();
  return {
    limit: async ({ key }) => {
      const minute = Math.floor(Date.now() / 60_000);
      const seen = windows.get(key);
      const count = seen?.minute === minute ? seen.count + 1 : 1;
      windows.set(key, { minute, count });
      return { success: count <= most };
    },
  };
}
const READOUT_LIMIT = limiter(Number(process.env.AUTOFLASH_LOCAL_READOUT_LIMIT) || 2);
const REPORT_LIMIT = limiter(3);
// Brought up to date at each start, as `apply` brings the real one.
const DB = d1({ file: path.join(folder, "api.sqlite") });
// Every email it would send, written to mail.log instead.
const EMAIL = {
  send: async (message) => {
    fs.appendFileSync(path.join(folder, "mail.log"), `${JSON.stringify(message)}\n`);
    return { messageId: `local-${Date.now()}` };
  },
};
// Made-up keys: this copy counts its senders, and keeps its devices, as the Worker does.
const env = { READOUTS, DB, READOUT_LIMIT, REPORT_LIMIT, EMAIL, INGEST: "on", ENOUGH: enough,
  SENDER_KEY: "a-made-up-key-for-this-computer", DEVICE_HASH_KEY_PERMANENT: "another-made-up-key-for-this-computer" };

// Gone once the script that started it is (PARENT, its process id), however it ended.
if (process.env.PARENT) {
  setInterval(() => {
    try {
      process.kill(Number(process.env.PARENT), 0);
    } catch {
      process.exit(0);
    }
  }, 2000).unref();
}

const server = http.createServer(async (request, response) => {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  const body = Buffer.concat(chunks);
  const headers = new Headers(request.headers);
  headers.set("CF-Connecting-IP", request.socket.remoteAddress || "unknown");
  const answer = await worker.fetch(new Request(`https://api.firmwareliberator.com${request.url}`, {
    method: request.method, headers, body: ["GET", "HEAD"].includes(request.method) ? undefined : body,
  }), env);
  const text = await answer.text();
  fs.appendFileSync(path.join(folder, "requests.log"),
    `${new Date().toISOString()} ${request.method} ${request.url} ${body.length} -> ${answer.status} ${text}\n`);
  response.writeHead(answer.status, Object.fromEntries(answer.headers));
  response.end(text);
});
server.listen(Number(port), address, () =>
  console.log(`API on ${address}:${server.address().port}, kept in ${folder}, ENOUGH=${enough}`));
