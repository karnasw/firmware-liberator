// The sign-up page's (src/pages/notify.astro) request to the API, made in
// the browser as the tool makes it (tools/autoflash/readout.py's notify): an
// address alone, with the light work /v1/notify asks (website/api/
// proof-of-work.js). test/notify.test.mjs holds these to the Worker's own.
// A file of its own, loaded by the page: the site's policy takes no inline
// script.
export const NOTIFY_URL = "https://api.firmwareliberator.com/v1/notify";
// As website/api/index.js's LIGHT_PROOF_BITS, LIGHT_PROOF_AGE and FORMAT.
export const LIGHT_PROOF_BITS = 20;
export const LIGHT_PROOF_AGE = 60;
export const FORMAT = 1;
// Tries between pauses for the page to draw: a few hundredths of a second on
// a slow phone, so it never looks stuck.
export const TRIES_AT_ONCE = 20000;

// SHA-256's round constants and starting state (FIPS 180-4).
const K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);
const START = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
  0x5be0cd19]);
const rotate = (x, n) => (x >>> n) | (x << (32 - n));

// One 64-byte block of SHA-256 (`block`'s bytes from `at`) on `state`, into
// `out`. In script, not crypto.subtle: a million tries, each awaited there,
// take many times as long. Its buffers are reused, as it runs a million times.
const W = new Uint32Array(64);
function compress(state, block, out, at = 0) {
  const w = W;
  for (let i = 0; i < 16; i += 1) {
    const j = at + i * 4;
    w[i] = (block[j] << 24) | (block[j + 1] << 16) | (block[j + 2] << 8) | block[j + 3];
  }
  for (let i = 16; i < 64; i += 1) {
    const x = w[i - 15];
    const y = w[i - 2];
    w[i] = w[i - 16] + (rotate(x, 7) ^ rotate(x, 18) ^ (x >>> 3)) + w[i - 7] + (rotate(y, 17) ^ rotate(y, 19) ^ (y >>> 10));
  }
  let a = state[0], b = state[1], c = state[2], d = state[3], e = state[4], f = state[5], g = state[6], h = state[7];
  for (let i = 0; i < 64; i += 1) {
    const t1 = (h + (rotate(e, 6) ^ rotate(e, 11) ^ rotate(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + w[i]) | 0;
    const t2 = ((rotate(a, 2) ^ rotate(a, 13) ^ rotate(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) | 0;
    h = g;
    g = f;
    f = e;
    e = (d + t1) | 0;
    d = c;
    c = b;
    b = a;
    a = (t1 + t2) | 0;
  }
  out[0] = state[0] + a;
  out[1] = state[1] + b;
  out[2] = state[2] + c;
  out[3] = state[3] + d;
  out[4] = state[4] + e;
  out[5] = state[5] + f;
  out[6] = state[6] + g;
  out[7] = state[7] + h;
  return out;
}

// The work for `subject`: "<time>:<nonce>" such that the SHA-256 of
// "<time>:<subject>:<nonce>" starts with `bits` (at most 32) zero bits, as
// proof-of-work.js's proven() checks it. The first 64 bytes never change, so
// they are hashed once, and each try hashes one block more. Given `tries`,
// it stops after that many from `from`, and gives null if none was it.
export function solve(subject, bits, time, { from = 0, tries = Infinity } = {}) {
  const prefix = new TextEncoder().encode(`${time}:${subject}:`);
  if (prefix.length < 64 || prefix.length > 96 || bits > 32) {
    throw new RangeError("a subject this page never sends");
  }
  const first = compress(START, prefix, new Uint32Array(8));
  const rest = prefix.subarray(64);
  const block = new Uint8Array(64);
  const out = new Uint32Array(8);
  for (let nonce = from; nonce < from + tries; nonce += 1) {
    const digits = String(nonce);
    block.fill(0);
    block.set(rest);
    for (let i = 0; i < digits.length; i += 1) {
      block[rest.length + i] = digits.charCodeAt(i);
    }
    block[rest.length + digits.length] = 0x80;
    const length = (prefix.length + digits.length) * 8;
    block[62] = length >>> 8;
    block[63] = length & 0xff;
    if (compress(first, block, out)[0] >>> (32 - bits) === 0) {
      return `${time}:${nonce}`;
    }
  }
  return null;
}

// solve(), TRIES_AT_ONCE at a time, letting the page draw between: on an old
// phone the work can take many seconds. Begun again with a later time should
// it near LIGHT_PROOF_AGE, after which the Worker would turn it away.
const drawn = () => new Promise((go) => setTimeout(go));

export async function work(subject, bits, { now = () => Date.now(), pause = drawn } = {}) {
  let time = Math.floor(now() / 1000);
  for (let from = 0; ; from += TRIES_AT_ONCE) {
    if (now() / 1000 - time > LIGHT_PROOF_AGE / 2) {
      time = Math.floor(now() / 1000);
      from = 0;
    }
    const proof = solve(subject, bits, time, { from, tries: TRIES_AT_ONCE });
    if (proof) return proof;
    await pause();
  }
}

const hex = (bytes) => [...new Uint8Array(bytes)].map((b) => b.toString(16).padStart(2, "0")).join("");

// Sends `email` to be emailed as more devices are supported: what to show,
// and whether it was taken. A random field of its own, as the tool's, so two
// sign-ups alike in one second carry different work.
export async function signUp(email, { send = fetch, now = () => Date.now(), pause } = {}) {
  const nonce = hex(crypto.getRandomValues(new Uint8Array(8)));
  const body = JSON.stringify({ format: FORMAT, email: email.trim(), devices: [], nonce });
  const subject = hex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(body)));
  const proof = await work(subject, LIGHT_PROOF_BITS, { now, pause });
  let response;
  try {
    response = await send(NOTIFY_URL, { method: "POST", body,
      headers: { "Content-Type": "application/json", "X-Proof": proof } });
  } catch {
    return { taken: false, said: "That could not be sent: check your connection and try again." };
  }
  if (response.status === 202) {
    return { taken: true, said: "Check your inbox: follow the link in the email to confirm your address." };
  }
  if (response.status === 400) {
    return { taken: false, said: "That does not look like an email address." };
  }
  let error = "";
  try {
    error = String((await response.json()).error || "");
  } catch {
    // no reason given
  }
  return { taken: false, said: error ? `Not taken: ${error}.` : "That could not be sent just now: try again later." };
}

// The page's form, where there is one (not in the tests).
const form = globalThis.document?.getElementById("notify");
if (form) {
  const said = document.getElementById("said");
  const button = form.querySelector("button");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    button.disabled = true;
    form.classList.add("working");
    said.textContent = "Working… this can take a few seconds on an older device.";
    const answer = await signUp(form.elements.namedItem("email").value);
    form.classList.remove("working");
    said.textContent = answer.said;
    button.disabled = answer.taken;
  });
}
