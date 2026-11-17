// The work the API asks of what the tool sends it, so a flood costs its
// sender: "<time>:<nonce>" such that the SHA-256 of "<time>:<subject>:<nonce>"
// starts with `bits` zero bits. The subject says what the work is for, so
// work done for one thing is good for nothing else: a readout's is its body's
// SHA-256 (bodySubject), a report's its random ID (reportSubject). The tool
// does the work in tools/autoflash/proof_of_work.py.

// The work asked, lowered only by the tests, which import the Worker's modules
// and set `bits`: nothing the Worker is given, a variable or a secret, can.
export const work = { bits: null };

export async function sha256(data) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", data));
}

export const hex = (bytes) => [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");

export const bodySubject = async (body) => hex(await sha256(body));

export const reportSubject = (id) => `report-${id}`;

export function zeroBits(bytes) {
  let bits = 0;
  for (const byte of bytes) {
    if (byte === 0) {
      bits += 8;
      continue;
    }
    return bits + Math.clz32(byte) - 24;
  }
  return bits;
}

// Whether `proof` is the work for `subject`, made no more than `age` seconds
// from `now` (seconds).
export async function proven(proof, subject, now, bits, age) {
  const [time, nonce, extra] = String(proof || "").split(":");
  if (extra !== undefined || !/^\d{1,12}$/.test(time || "") || !/^\d{1,20}$/.test(nonce || "")) {
    return false;
  }
  if (Math.abs(now - Number(time)) > age) {
    return false;
  }
  return zeroBits(await sha256(new TextEncoder().encode(`${time}:${subject}:${nonce}`))) >= bits;
}
