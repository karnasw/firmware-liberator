// The tool's own report of a run (tools/autoflash/report_upload.py, with
// report_data.py and proof_of_work.py), sent to the API Worker's own code
// (api/local.mjs) as a run sends it: the report this computer's Python makes,
// with its work done in a process of its own, is taken, and forgotten here.
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

const ROOT = new URL("../../", import.meta.url).pathname;
const SERVER = new URL("../api/local.mjs", import.meta.url).pathname;
const PYTHON = process.env.AUTOFLASH_PYTHON || "python3";
const BITS = 8;

async function started(folder) {
  const child = spawn(process.execPath, [SERVER, "127.0.0.1", "0", folder],
    { env: { ...process.env, AUTOFLASH_LOCAL_PROOF_BITS: String(BITS) } });
  const port = await new Promise((resolve, reject) => {
    child.stdout.on("data", (data) => {
      const found = /:(\d+),/.exec(String(data));
      if (found) resolve(Number(found[1]));
    });
    child.on("exit", (code) => reject(new Error(`exited ${code}`)));
  });
  return { child, port };
}

// A run's end, as report.py's invite_report() has it, in this computer's Python.
const RUN = `
import json, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from tools.autoflash import report_upload as upload
from tools.autoflash.state import RunState

queue, bits = Path(sys.argv[1]), int(sys.argv[2])
state = RunState.open(Path(tempfile.mkdtemp()) / "run")
for key, value in {"product_model": "S60TPF", "stock_version": "1.2.0", "device_kind": "sonoff-s60",
                   "firmware": "tasmota", "device_mac": "12:34:56:ab:cd:ef"}.items():
    state.set_input(key, value)
ctx = SimpleNamespace(state=state, last_failure="", last_failed_step="", args=SimpleNamespace())
with mock.patch.object(upload, "QUEUE", queue), mock.patch.object(upload, "PROOF_BITS", bits):
    ctx.report_id = "0123456789abcdef"
    ctx.report_work = upload.start_work(ctx.report_id)
    upload.send(ctx, "converted", 300, False, {"Relay": "switches (meter confirms)"}).join(120)
print(json.dumps({"kept": sorted(path.name for path in queue.glob("*.json"))}))
`;

function runTool(queue, url) {
  const env = { ...process.env, AUTOFLASH_REPORTS_URL: url };
  delete env.AUTOFLASH_NO_REPORT;
  delete env.AUTOFLASH_EMULATOR_URL;
  const done = spawnSync(PYTHON, ["-c", RUN, queue, String(BITS)], { cwd: ROOT, env, encoding: "utf8", timeout: 180_000 });
  assert.equal(done.status, 0, done.stderr);
  return JSON.parse(done.stdout.trim().split("\n").at(-1));
}

test("a run's report, as the tool makes and sends it, is taken by the Worker and forgotten here", async () => {
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), "autoflash-api-"));
  const queue = fs.mkdtempSync(path.join(os.tmpdir(), "autoflash-reports-"));
  const { child, port } = await started(folder);
  try {
    const { kept } = runTool(queue, `http://127.0.0.1:${port}/v1/reports`);
    assert.deepEqual(kept, [], "nothing left to send");
    const log = fs.readFileSync(path.join(folder, "requests.log"), "utf8");
    assert.match(log, /POST \/v1\/reports \d+ -> 200 \{"accepted":\["0123456789abcdef"\]/);
  } finally {
    child.kill();
    fs.rmSync(folder, { recursive: true, force: true });
    fs.rmSync(queue, { recursive: true, force: true });
  }
});

test("with no API to answer, the report is kept, its work done, to send at the next start", () => {
  const queue = fs.mkdtempSync(path.join(os.tmpdir(), "autoflash-reports-"));
  try {
    const { kept } = runTool(queue, "http://127.0.0.1:9/reports");
    assert.deepEqual(kept, ["0123456789abcdef.json"]);
    const item = JSON.parse(fs.readFileSync(path.join(queue, kept[0]), "utf8"));
    assert.match(item.proof, /^\d+:\d+$/);
    assert.equal(item.report.id, "0123456789abcdef");
  } finally {
    fs.rmSync(queue, { recursive: true, force: true });
  }
});
