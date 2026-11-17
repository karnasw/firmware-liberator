// api/local.mjs: the API Worker's own code on this computer, for emulator runs
// that send their readouts (unattended-test.sh --api).
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

const SERVER = new URL("../api/local.mjs", import.meta.url).pathname;

function proofFor(body, bits) {
  const time = Math.floor(Date.now() / 1000);
  const digest = createHash("sha256").update(body).digest("hex");
  for (let nonce = 0; ; nonce += 1) {
    const hash = createHash("sha256").update(`${time}:${digest}:${nonce}`).digest();
    if (hash.readUInt32BE(0) >>> (32 - bits) === 0) return `${time}:${nonce}`;
  }
}

async function started(folder, enough, env = {}) {
  const child = spawn(process.execPath, [SERVER, "127.0.0.1", "0", folder, enough], { env: { ...process.env, ...env } });
  // Port 0 is the system's choice: read it back from what the server says.
  const port = await new Promise((resolve, reject) => {
    child.stdout.on("data", (data) => {
      const found = /:(\d+),/.exec(String(data));
      if (found) resolve(Number(found[1]));
    });
    child.on("exit", (code) => reject(new Error(`exited ${code}`)));
  });
  return { child, port };
}

test("it answers as the Worker does and keeps what it is given in its folder", async () => {
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), "autoflash-api-"));
  const { child, port } = await started(folder, "NSPANEL");
  try {
    const body = Buffer.from(JSON.stringify({ format: 1, model: "NSPanel", stock_model: "E32-MSW-NX", stock_version: "1.5.0",
      stage: "first", firmware: [] }));
    const answer = await fetch(`http://127.0.0.1:${port}/v1/wanted`, { method: "POST", body,
      headers: { "Content-Type": "application/json", "X-Proof": proofFor(body, 20) } });
    assert.deepEqual(await answer.json(), { wanted: false, why: "enough", held: [] });
    assert.match(fs.readFileSync(path.join(folder, "requests.log"), "utf8"), /POST \/v1\/wanted .* -> 200/);
  } finally {
    child.kill();
    fs.rmSync(folder, { recursive: true, force: true });
  }
});

test("it is gone once the script that started it is", async () => {
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), "autoflash-api-"));
  const parent = spawn("sleep", ["30"]);
  const { child } = await started(folder, "", { PARENT: String(parent.pid) });
  try {
    parent.kill();
    const code = await new Promise((resolve) => child.on("exit", resolve));
    assert.equal(code, 0);
  } finally {
    child.kill();
    fs.rmSync(folder, { recursive: true, force: true });
  }
});
