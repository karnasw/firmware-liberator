// api/mail.js: every email the API sends, counted by what came of it.
import assert from "node:assert/strict";
import { test } from "node:test";
import { read } from "../api/counters.js";
import { d1 } from "../api/local-d1.mjs";
import { SUPPRESSED, failedOn, sendMail } from "../api/mail.js";

const DAY = "2026-11-10";
const MESSAGE = { to: "owner@example.com", from: "updates@firmwareliberator.com", subject: "s", text: "t" };
const throwing = (error) => ({ DB: d1(), EMAIL: { send: async () => { throw error; } } });

test("a sent email is counted for its day", async () => {
  const env = { DB: d1(), EMAIL: { send: async () => ({ messageId: "m1" }) } };
  assert.deepEqual(await sendMail(env, MESSAGE, DAY), { sent: true });
  assert.equal((await read(env.DB, `mail-sent:${DAY}`)).count, 1);
  assert.deepEqual(await failedOn(env.DB, DAY), {});
});

test("one that fails is counted by Email Service's code, from its code or its words, and never throws", async () => {
  const env = throwing(Object.assign(new Error("Recipient is suppressed"), { code: SUPPRESSED }));
  const done = await sendMail(env, MESSAGE, DAY);
  assert.deepEqual([done.sent, done.code], [false, SUPPRESSED]);
  env.EMAIL.send = async () => { throw new Error("E_RATE_LIMIT_EXCEEDED: too many"); };
  await sendMail(env, MESSAGE, DAY);
  env.EMAIL.send = async () => { throw new Error("socket closed"); };
  assert.match((await sendMail(env, MESSAGE, DAY)).error, /socket closed/);
  assert.deepEqual(await failedOn(env.DB, DAY), { [SUPPRESSED]: 1, E_RATE_LIMIT_EXCEEDED: 1, OTHER: 1 });
  assert.deepEqual(await failedOn(env.DB, "2026-11-09"), {}, "each day its own");
  assert.equal((await read(env.DB, `mail-sent:${DAY}`)).count, 0);
});
