// api.firmwareliberator.com: the project's API (tools/autoflash/readout.py,
// .github/readouts.md). A Worker of its own, apart from the website's, so
// nothing sent here can touch the site. Six paths, as openapi.json describes
// them: four a POST from the tool, under /v1/ - /v1/reports (reports.js, which
// says how it is guarded), and three for readouts and sign-ups - and /confirm
// and /forget, which a person opens from an email.
// Every one answers 503 while INGEST is not "on". A sender is counted by its
// address, an IPv6 one by its /48 (sender.js).
// - /v1/wanted: whether a readout of a device would add anything, asked before
//   it is made. Its product model, firmware model and version, the stage it
//   would be (a first stage, or a whole memory), and the firmware eWeLink
//   offers it, by SHA-256: the answer says whether it is wanted, and which of
//   those files are held already, so they are left out.
// - /v1/readout: a readout, made with its owner's consent. Kept only if it adds
//   something: fewer than PER_KEY readouts of that model and version at that
//   stage are held (a whole one taking a first stage's place too), or it
//   checks those taken as right (CHECK_DAYS), and its sender has had fewer
//   than SENDER_DAILY kept today.
// - /v1/notify: an owner's email address, to be emailed as more devices are
//   supported, with their devices' models and the readouts they sent, so
//   their own devices' news can be sent them too. The website's sign-up page
//   (SITE_ORIGIN) posts an address alone to it from the browser, its script
//   doing the same work. Either way the address is sent a link to confirm
//   it, and nothing is kept until it is.
// - /confirm: that link; /forget: the link to stop, one an address. A GET of
//   either shows a page asking first; only the POST it sends acts, so a mail
//   scanner following one does nothing.
// - Readouts not taken as right are held to PENDING_BYTES, and deleted with
//   unconfirmed addresses by the nightly job after their days (pending.js).
//
// The tool is open source, so it can hold no secret: what keeps this from being
// flooded is that each request costs its sender work, and that the bucket has
// hard limits it is never let past.
// - A proof of work: SHA-256 of "<time>:<the body's SHA-256>:<nonce>" starting
//   with PROOF_BITS zero bits, about 17 million tries (10 to 30 seconds in the
//   tool's Python, once a readout), made within PROOF_AGE of now and good for
//   that body alone. Hardware made for it does that in milliseconds, so the
//   daily limits below, not this, are what bound the bucket. /wanted and
//   /notify ask lighter work (LIGHT_PROOF_BITS, a second or two) good for a
//   minute (LIGHT_PROOF_AGE), and taken once: each request is worked for
//   afresh. A device's names are taken only as a model's name is written
//   (MODEL, STOCK_MODEL, STOCK_VERSION), other text only as printable ASCII.
// - It must be a readout: a tar of at most MAX_BYTES, its first file a
//   manifest.json of the format the tool writes.
// - At most two readouts a minute from one address (READOUT_LIMIT, held by the
//   limiter, never stored), and DAILY_COUNT readouts and DAILY_BYTES a day from
//   everyone, counted in the database (counters.js), where two at once can
//   never both pass the limit. /wanted reads that count too, and says so once
//   the day is full. NOTIFY_DAILY addresses a day may be given. /wanted and
//   /notify are not held to READOUT_LIMIT, which a batch's questions would
//   use up before its readouts: the hostname's rate-limiting rule
//   (tools/repo/cloudflare_settings.py) holds them, as it does every path.
// - Until the database has every migration this code needs (database.js),
//   each path answers 503, so the tool tries again later.
// - Kept for good, with the marks of what is held (readouts/held/,
//   readouts/firmware/): a readout is kept only if it adds something, so the
//   store grows only with firmware not seen before, and an old one may prove
//   useful long after. TOTAL_BYTES of them at most, counted in the database
//   as the day's are: room enough that a flood of made-up ones, at the day's
//   limit, takes months to fill it, at well under a dollar a month (R2
//   includes 10 GB, then $0.015 a GB-month, 2026). Past it, none is taken
//   until some are deleted by hand (.github/readouts.md).
// - A device enough readouts of are in, by eWeLink's product model in ENOUGH
//   (a comma-separated variable in wrangler.jsonc), is not wanted at all.
// - The same readout sent again the same day is the one already stored.
// - A confirmed address (subscribers, in the database) has no expiry: it is
//   kept until its owner removes it through /forget. No address or other
//   detail of a sender is kept but the one they gave: a sender's day count is named by a keyed hash of its
//   address (SENDER_KEY, a Worker secret), deleted with the day's counts.
//   Cloudflare's own log of each request (wrangler.jsonc), kept seven days,
//   holds its address.

export const MAX_BYTES = 24 * 1024 * 1024;
export const PROOF_BITS = 24;
export const PROOF_AGE = 15 * 60;
export const DAILY_COUNT = 100;
export const DAILY_BYTES = 250 * 1024 * 1024;
export const TOTAL_BYTES = 50 * 1024 * 1024 * 1024;
export const FORMAT = 1;
export const LIGHT_PROOF_BITS = 20;
export const LIGHT_PROOF_AGE = 60;
export const QUESTION_MAX_BYTES = 4096;
// Confirmations and reminders sent a day at most: what bounds the cost of
// email. Email Service includes 3,000 a month on Workers Paid, then $0.35 a
// 1,000 (2026), so a month of full days, some 15,000, is about $4.
export const NOTIFY_DAILY = 500;
// The most offered files a /wanted question names, and devices and readouts
// a /notify: an eWeLink account's devices, and a sitting's readouts, rarely
// more.
export const MAX_FIRMWARE = 8;
export const MAX_DEVICES = 20;
export const MAX_READOUTS = 20;
// Sign-ups to be told of a change one sender may ask for a day, and one
// address may be sent a confirmation for: so nobody can have many strangers
// emailed, or one stranger many times.
export const NOTIFY_SENDER_DAILY = 5;
// Where the confirmation an address is sent comes from: on the domain the
// Email Service sends for (SENDING_DOMAIN in tools/repo/cloudflare_settings.py,
// which onboards it). Until it is onboarded, a confirmation fails to send and
// /notify answers 502, keeping nothing.
export const NOTIFY_FROM = "updates@firmwareliberator.com";
export const CONFIRM_URL = "https://api.firmwareliberator.com/confirm";
// An address not confirmed this many days after its link was sent is sent it
// once more (remind, run hourly): a confirmation is easily missed, and once is
// all, so a stranger's address given is written to twice at most.
export const REMIND_AFTER_DAYS = 1;
// Reminders sent a run at most; any more go the next hour.
export const REMIND_EACH_RUN = 100;
// wrangler.jsonc's cron for the nightly job; its other, hourly, sends the
// reminders alone.
export const NIGHTLY_CRON = "17 3 * * *";
// The most of the store that readouts not yet taken as right may take: so
// made-up ones, kept until the nightly job deletes them (pending.js), can
// never fill it. Enough for a rare device's readouts to wait the months it
// may take others to send theirs.
export const PENDING_BYTES = 10 * 1024 * 1024 * 1024;
export const STAGES = ["first", "whole"];
// Readouts kept of each product, firmware model and version, each from a
// different sender: three, so one made up stands out against the two that
// agree, and none is taken as right until that many do (readouts table).
export const PER_KEY = 3;
// And from three networks (Cloudflare's ASN for the request, a host's or a
// VPN's counting as none: networks.js), so that no one, nor two, rented
// servers' networks, however many address blocks they have, are enough for
// readouts to agree. A key's places past PER_KEY, up to MAX_PLACES, go only to a readout from a network none of those held came
// from, until some agree: three made up on one network cannot keep out the
// owners who would outvote them, and wait for review until they are deleted
// (pending.js). Three alike from fewer networks wait for one more, or for
// the maintainer to approve them (the admin view, or readouts.py approve).
export const MIN_NETWORKS = 3;
export const MAX_PLACES = 2 * PER_KEY;
// Once a key's whole readouts agree, or are approved, one more is still
// wanted each CHECK_DAYS, from a network none of those held came from, up to
// MAX_PLACES in all: so one wrongly taken as right - made up from owners'
// networks, or approved by mistake - is found by a later owner's. One alike
// is taken as right too. One not alike waits for review, its row marked
// (checked), the maintainer is emailed (tellDisputed), and until it is
// settled readouts from new networks are taken without waiting. No second
// fingerprint is taken as right while one is: the maintainer settles it.
// Whole readouts only: a first stage holds what eWeLink offers the device,
// which changes with each release while the version it runs stays the same.
export const CHECK_DAYS = 30;
// The maintainer is emailed when readouts come to agree, unless the admin
// view's switch, this setting, is "off" (settings table).
export const AGREEMENT_SETTING = "email_on_agreement";
// Readouts kept a day from one sender's address (SENDER_DAILY), counted under
// a keyed hash of it (SENDER_KEY, a Worker secret), never the address itself.
export const SENDER_DAILY = 20;

import { answer } from "./answer.js";
import { read, release, reserve } from "./counters.js";
import { ready } from "./database.js";
import { refreshDownloads } from "./downloads.js";
import { PENDING, SIGNUP_DAYS } from "./pending.js";
import { SUPPRESSED, sendMail } from "./mail.js";
import { countedNetwork, refreshNetworks } from "./networks.js";
import { senderAddress } from "./sender.js";
import { bodySubject, hex, proven, sha256, work } from "./proof-of-work.js";
import { nightly } from "./nightly.js";
import { countWanted, REPORTS_DAILY, reports } from "./reports.js";

export { work };

// Each path, and only these, as openapi.json describes them: the tool's under
// /v1/, kept working for every version of the tool released against them, and
// the two pages an email links to. tools/repo/cloudflare_settings.py's firewall
// rule, made from openapi.json, lets through these and their methods alone.
export const ROUTES = { "/v1/readout": readout, "/v1/wanted": wanted, "/v1/notify": notify, "/v1/reports": reports,
  "/confirm": confirm, "/forget": forget };

export default {
  // Every answer names the code it came from (API-Tree): the git tree of
  // website/api/ the website workflow deployed, which the release workflow
  // waits to see before it releases a tool that talks to it
  // (tools/repo/api_health.py deployed).
  async fetch(request, env) {
    const answered = await handle(request, env);
    if (!env.API_TREE) {
      return answered;
    }
    const named = new Response(answered.body, answered);
    named.headers.set("API-Tree", env.API_TREE);
    return named;
  },
  // The lists of hosts' and VPNs' networks fetched (networks.js), the tool's
  // downloads read (downloads.js), the nightly job (nightly.js), then the
  // reminders (remind), or, at the hourly cron, the reminders alone: at the
  // times wrangler.jsonc's crons give.
  async scheduled(controller, env, context) {
    // The limits the maintainer is told were reached (alerts.js).
    const limits = { DAILY_COUNT, DAILY_BYTES, TOTAL_BYTES, NOTIFY_DAILY, REPORTS_DAILY, from: NOTIFY_FROM };
    const now = new Date(controller.scheduledTime);
    if (controller.cron !== NIGHTLY_CRON) {
      context.waitUntil(remind(env, now).then((reminded) => ({ reminded })));
      return;
    }
    context.waitUntil(refreshNetworks(env, now).then(async (networks) => ({ networks,
      downloads: await refreshDownloads(env, now), ...(await nightly(env, now, limits)), reminded: await remind(env, now) })));
  },
};

async function handle(request, env) {
  const route = ROUTES[new URL(request.url).pathname];
  if (!route) {
    return answer(404, { error: "no such path: the tool's are under /v1/" });
  }
  // The one switch for every path: "on" takes requests; anything else, as
  // the website workflow can deploy with, answers 503 for the tool to try
  // again later.
  if (env.INGEST !== "on") {
    return answer(503, { error: "not taking requests for now: try again later" }, { "Retry-After": "3600" });
  }
  if (!(await ready(env.DB))) {
    return answer(503, { error: "the API is being updated: try again later" }, { "Retry-After": "300" });
  }
  return route(request, env);
}

// The manifest a readout starts with, or null: the first file of its tar,
// which is not compressed (the files after it are, each on its own).
export function manifestOf(body) {
  try {
    const head = new Uint8Array(body, 0, Math.min(body.byteLength, 512 + 65536));
    const text = (start, end) => new TextDecoder().decode(head.slice(start, end));
    const name = text(0, 100).replace(/\0.*$/s, "");
    const size = parseInt(text(124, 136), 8);
    if (name !== "manifest.json" || !(size > 0 && size <= 65536) || head.length < 512 + size) {
      return null;
    }
    const manifest = JSON.parse(text(512, 512 + size));
    if (manifest?.format !== FORMAT || typeof manifest.model !== "string") {
      return null;
    }
    return manifest;
  } catch {
    return null;
  }
}

// The files of a readout's tar, by name, each its bytes: null if it is not
// a tar that holds together.
export function filesOf(body) {
  const bytes = new Uint8Array(body);
  const decoder = new TextDecoder();
  const field = (at, start, end) => decoder.decode(bytes.subarray(at + start, at + end)).replace(/\0.*$/s, "");
  const files = new Map();
  for (let at = 0; at + 512 <= bytes.length;) {
    const name = field(at, 0, 100);
    if (!name) {
      break;   // the empty blocks that end it
    }
    const size = /^[0-7]+$/.test(field(at, 124, 136).trim()) ? parseInt(field(at, 124, 136).trim(), 8) : NaN;
    if (!Number.isSafeInteger(size) || at + 512 + size > bytes.length) {
      return null;
    }
    const prefix = field(at, 257, 263) === "ustar" ? field(at, 345, 500) : "";
    if (["", "0"].includes(field(at, 156, 157))) {   // a file, not a directory or a link
      files.set(prefix ? `${prefix}/${name}` : name, bytes.subarray(at + 512, at + 512 + size));
    }
    at += 512 + Math.ceil(size / 512) * 512;
  }
  return files;
}

// The SHA-256s of the offered files a readout holds (`files`, filesOf), each
// checked against the one its manifest gives it: null if one it names is not
// in it, or is not what it says. Only these are marked held (keep), so no
// readout can have the tool leave out of later ones a file the store does not
// have.
async function offeredIn(files, manifest) {
  const listed = (Array.isArray(manifest.offered) ? manifest.offered : [])
    .filter((item) => item?.file !== null && item?.file !== undefined).slice(0, MAX_FIRMWARE);
  if (!listed.length) {
    return [];
  }
  const found = [];
  for (const item of listed) {
    const data = typeof item.file === "string" ? files?.get(item.file) : undefined;
    if (!data || typeof item.sha256 !== "string" || hex(await sha256(data)) !== item.sha256) {
      return null;
    }
    found.push(item.sha256);
  }
  return found;
}

// The bytes an xz file unpacks to, read from its index without unpacking it:
// null where it is not one xz stream, as the tool's lzma.compress writes.
export function xzUnpacked(bytes) {
  const MAGIC = [0xfd, 0x37, 0x7a, 0x58, 0x5a, 0x00];
  if (bytes.length < 32 || MAGIC.some((byte, at) => bytes[at] !== byte)
      || bytes[bytes.length - 2] !== 0x59 || bytes[bytes.length - 1] !== 0x5a) {
    return null;
  }
  const footer = bytes.length - 12;
  const backward = new DataView(bytes.buffer, bytes.byteOffset + footer + 4, 4).getUint32(0, true);
  let at = footer - (backward + 1) * 4;
  if (at < 12 || bytes[at] !== 0x00) {
    return null;
  }
  at += 1;
  const number = () => {
    let value = 0;
    for (let shift = 0; shift < 63 && at < footer; shift += 7) {
      const byte = bytes[at++];
      value += (byte & 0x7f) * 2 ** shift;
      if (!(byte & 0x80)) {
        return value;
      }
    }
    return NaN;
  };
  const records = number();
  if (!Number.isSafeInteger(records) || records < 1 || records > 65536) {
    return null;
  }
  let total = 0;
  for (let record = 0; record < records; record += 1) {
    number();   // its compressed size
    total += number();
  }
  return Number.isSafeInteger(total) && at <= footer ? total : null;
}

// Whether a readout of the whole memory holds one, as the tool reads it: its
// flash.bin.xz unpacking to a flash chip's size, a power of two from 512 KiB
// to 32 MiB. Checked without unpacking it, from the xz file's own index.
export function wholeFlash(files) {
  const flash = files?.get("flash.bin.xz");
  const size = flash ? xzUnpacked(flash) : null;
  return size !== null && size >= 512 * 1024 && size <= 32 * 1024 * 1024 && (size & (size - 1)) === 0;
}

// The product models enough readouts of are in (ENOUGH), upper-case.
function enoughOf(env) {
  return String(env.ENOUGH || "").split(",").map((model) => model.trim().toUpperCase()).filter(Boolean);
}

// Text from a sender: printable ASCII alone, so nothing kept can carry a
// terminal's escape sequences to whoever reads it (tools/repo/readouts.py).
const PRINTABLE = /^[\x20-\x7e]+$/;
const text = (value, most = 64) => typeof value === "string" && value.length > 0 && value.length <= most
  && PRINTABLE.test(value);

// The names a device goes by, as the tool sends them: its eWeLink product
// model, written as a report's model is (report-schema.json) in either case;
// its firmware model; and the version it runs - those two "-" for none. No
// space, "|", ":" or "/" in any, so none can carry a sentence or a link into
// an email or the figures, and keyOf's "|" is never part of one. openapi.json
// gives the same (test/openapi.test.mjs holds them alike).
export const MODEL = /^[A-Za-z0-9][A-Za-z0-9-]{0,23}$/;
export const STOCK_MODEL = /^(?:-|[A-Za-z0-9][A-Za-z0-9._-]{0,63})$/;
export const STOCK_VERSION = /^(?:-|[A-Za-z0-9][A-Za-z0-9._-]{0,31})$/;
// The device, as a report names it (report-schema.json's device_hash).
export const DEVICE_HASH = /^[0-9a-f]{64}$/;
const fits = (value, pattern) => typeof value === "string" && pattern.test(value);

// Where an offered file a readout holds is marked.
const firmwareKey = (sha) => `readouts/firmware/${sha}`;

const isHeld = async (env, key) => (await env.READOUTS.head(key)) !== null;

// A readout's key: its product, firmware model and version.
const keyOf = (product, model, version) => `${product.toUpperCase()}|${model}|${version}`;

// The readouts table's rows that hold a place of key ?1 at stage ?2, offered
// ?o: all but those rejected. First stages are held each offer apart, as
// each new release eWeLink offers is a first stage of its own to agree on;
// a whole one takes its offer's first stage's place too, as it holds all one
// does.
const HOLDING = (o) => `review != 'rejected' AND (stage = 'whole' OR ?2 = 'first') AND (?2 = 'whole' OR offer = ?${o})`;

// Whether key ?1 at stage ?2 is taken as right: of a whole one, some of its
// whole readouts; of a first stage, its offer ?o (agreed_offers, OFFER_AGREES),
// or first stages of it approved. A whole readout taken as right on its
// firmware says nothing yet of what it was offered.
const AGREED = (o) => `(CASE WHEN ?2 = 'whole'
  THEN EXISTS (SELECT 1 FROM readouts WHERE key = ?1 AND stage = 'whole' AND review IN ('quorum', 'approved'))
  ELSE EXISTS (SELECT 1 FROM agreed_offers WHERE key = ?1 AND offer = ?${o})
    OR EXISTS (SELECT 1 FROM readouts WHERE key = ?1 AND stage = 'first' AND offer = ?${o}
      AND review IN ('quorum', 'approved')) END)`;
// Whether a whole readout of key ?1 checking those taken as right disagrees
// with them, unsettled.
const DISPUTED = "EXISTS (SELECT 1 FROM readouts WHERE key = ?1 AND stage = 'whole' AND checked = 1 AND review = 'pending')";

// Whether a place of key ?1 at stage ?2, offered ?o, is left for a readout
// from the network ?n, it being ?c (CHECK_DAYS ago): fewer than PER_KEY are
// held; or fewer than MAX_PLACES are, none came from that network, and none
// agree - or, of a whole one, the newest held came CHECK_DAYS ago or a check
// disagrees (CHECK_DAYS).
const PLACE_LEFT = (n, c, o) => `((SELECT count(*) FROM readouts WHERE key = ?1 AND ${HOLDING(o)}) < ${PER_KEY}
  OR (?${n} IS NOT NULL AND (SELECT count(*) FROM readouts WHERE key = ?1 AND ${HOLDING(o)}) < ${MAX_PLACES}
    AND NOT EXISTS (SELECT 1 FROM readouts JOIN readout_networks ON readout_networks.id = readouts.id
      WHERE readouts.key = ?1 AND ${HOLDING(o)} AND readout_networks.network = ?${n})
    AND (NOT ${AGREED(o)} OR (?2 = 'whole' AND (${DISPUTED}
      OR (SELECT max(received) FROM readouts WHERE key = ?1 AND ${HOLDING(o)}) <= ?${c})))))`;
const checkSince = (now) => new Date(now.getTime() - CHECK_DAYS * 86_400_000).toISOString();

// Whether `sender` (?3) has a readout of key ?1 a readout at stage ?2,
// offered ?o, would repeat: one of that offer, or a whole one if it is whole.
// So a sender with many of one device sends one, three agreeing come from
// three senders, and an owner may send a first stage again for a new offer.
const THEIRS = (o) => `EXISTS (SELECT 1 FROM readouts WHERE key = ?1 AND sender = ?3
  AND (offer = ?${o} OR (?2 = 'whole' AND stage = 'whole')))`;

// Whether a readout at `stage`, offered `offer`, would add anything for
// `sender`, from `network`, at `now`: a place is left for it, and that sender
// has sent none it would repeat.
async function adds(env, key, stage, sender, network, now, offer) {
  const row = await env.DB.prepare(`SELECT ${PLACE_LEFT(4, 5, 6)} AS room, ${THEIRS(6)} AS theirs`)
    .bind(key, stage, sender ?? "", network, checkSince(now), offer).first();
  return Boolean(row.room) && !row.theirs;
}

// A keyed hash (SENDER_KEY) of `what`, 32 hex: null where the Worker has none.
const senderKeys = new Map();
async function keyedHash(env, what) {
  if (!env.SENDER_KEY) {
    return null;
  }
  if (!senderKeys.has(env.SENDER_KEY)) {
    senderKeys.set(env.SENDER_KEY, await crypto.subtle.importKey("raw", new TextEncoder().encode(env.SENDER_KEY),
      { name: "HMAC", hash: "SHA-256" }, false, ["sign"]));
  }
  const mac = await crypto.subtle.sign("HMAC", senderKeys.get(env.SENDER_KEY), new TextEncoder().encode(what));
  return hex(new Uint8Array(mac)).slice(0, 32);
}

// A sender's address, as senders are counted: IPv6 by its /48 (sender.js).
const addressOf = senderAddress;
// The sender of a readout of `key`, as its row names it: one hash a key, so
// nothing ties one sender's readouts of different keys together.
const senderOf = (env, request, key) => keyedHash(env, `${key}|${addressOf(request)}`);
// The network a readout of `key` came from, as readout_networks names it: a
// keyed hash of its ASN and the key, or null where it counts as none (networks.js).
export async function networkOf(env, request, key) {
  const asn = await countedNetwork(env, request);
  return asn === null ? null : keyedHash(env, `${key}|network|${asn}`);
}

// JSON with its keys in order, so the same fields always hash alike.
const canonical = (value) => Array.isArray(value) ? `[${value.map(canonical).join(",")}]`
  : value && typeof value === "object"
    ? `{${Object.keys(value).sort().map((name) => `${JSON.stringify(name)}:${canonical(value[name])}`).join(",")}}`
    : JSON.stringify(value ?? null);

const hashed = async (value) => hex(await sha256(new TextEncoder().encode(canonical(value))));

// The offer a readout holds, hashed: the SHA-256s of the files eWeLink
// offers the device, as /wanted is asked with them (its "firmware") and a
// readout's manifest lists them. The same for every device of a key until
// Sonoff releases another version, so first stages are held and agree each
// offer apart (HOLDING).
export const offerOf = (shas) => hashed({ offered: shas.map(String).filter((sha) => /^[0-9a-f]{64}$/.test(sha)).sort() });
const offeredShas = (manifest) => (Array.isArray(manifest.offered) ? manifest.offered : [])
  .map((item) => item?.sha256 ?? "");

// The versions a readout says it is offered, for people to read: "-" for none.
const OFFERED_MOST = 4;
const offeredVersions = (manifest) => [...new Set((Array.isArray(manifest.offered) ? manifest.offered : [])
  .map((item) => item?.version).filter((version) => fits(version, STOCK_VERSION) && version !== "-"))]
  .sort().slice(0, OFFERED_MOST).join(",") || "-";

// What every genuine readout of a key at its stage shares, hashed: for a
// whole one, its firmware as the tool read it before anything was taken out
// (manifest.firmware: the partition table, each app's build); for a first
// stage, its offer. Not the owner's settings, which differ.
const fingerprintOf = (manifest, stage) => (stage === "whole"
  ? hashed({ firmware: manifest.firmware ?? null, layout: manifest.layout ?? null }) : offerOf(offeredShas(manifest)));

// An offer of key ?1 (?2, versions ?3) taken as right on day ?4: once PER_KEY
// readouts of it, first stages or whole ones, from MIN_NETWORKS networks,
// name it - a whole readout holds what its first stage would. Its row if it
// is newly.
const OFFER_AGREES = `INSERT INTO agreed_offers (key, offer, offered, day) SELECT ?1, ?2, ?3, ?4
  WHERE (SELECT count(*) FROM readouts WHERE key = ?1 AND offer = ?2 AND review != 'rejected') >= ${PER_KEY}
  AND (SELECT count(DISTINCT readout_networks.network) FROM readouts
    JOIN readout_networks ON readout_networks.id = readouts.id
    WHERE readouts.key = ?1 AND readouts.offer = ?2 AND readouts.review != 'rejected') >= ${MIN_NETWORKS}
  ON CONFLICT DO NOTHING RETURNING key`;

// A readout's row, if a place is left for it and its sender has none it
// would repeat (THEIRS): in one statement, so two sent at once cannot both take the last place;
// marked checked if it is a whole one coming after some agreed; then its
// network. Then its offer taken as right if enough name it (OFFER_AGREES),
// and every pending first stage of an offer taken as right marked agreed
// (quorum). And of a whole one, every pending whole row of its key marked
// agreed once PER_KEY of them, from MIN_NETWORKS networks, hold the same
// fingerprint, or that fingerprint is taken as right already, and no other is.
// The maintainer is told of each. A check that agrees with none taken as
// right is a dispute.
async function hold(env, row, now) {
  const taken = await env.DB.prepare(
    `INSERT INTO readouts (id, key, stage, sender, fingerprint, bytes, object, received, offer, offered, checked)
     SELECT ?4, ?1, ?2, ?3, ?5, ?6, ?7, ?8, ?11, ?12, ?2 = 'whole' AND ${AGREED(11)}
     WHERE ${PLACE_LEFT(9, 10, 11)} AND NOT ${THEIRS(11)}
     ON CONFLICT DO NOTHING RETURNING id, checked`,
  ).bind(row.key, row.stage, row.sender, row.id, row.fingerprint, row.bytes, row.object, row.received, row.network,
    checkSince(now), row.offer, row.offered).first();
  if (!taken) {
    return false;
  }
  if (row.network) {
    await env.DB.prepare("INSERT OR IGNORE INTO readout_networks (id, network) VALUES (?1, ?2)")
      .bind(row.id, row.network).run();
  }
  const offerAgreed = await env.DB.prepare(OFFER_AGREES)
    .bind(row.key, row.offer, row.offered, row.received.slice(0, 10)).first();
  const firsts = (await env.DB.prepare(
    `UPDATE readouts SET review = 'quorum' WHERE key = ?1 AND stage = 'first' AND offer = ?2 AND review = 'pending'
     AND (EXISTS (SELECT 1 FROM agreed_offers WHERE key = ?1 AND offer = ?2)
       OR EXISTS (SELECT 1 FROM readouts WHERE key = ?1 AND stage = 'first' AND offer = ?2 AND review = 'approved'))
     RETURNING bytes`,
  ).bind(row.key, row.offer).all()).results;
  const wholes = row.stage !== "whole" ? [] : (await env.DB.prepare(
    `UPDATE readouts SET review = 'quorum' WHERE key = ?1 AND stage = 'whole' AND review = 'pending'
     AND fingerprint = ?2
     AND NOT EXISTS (SELECT 1 FROM readouts WHERE key = ?1 AND stage = 'whole' AND fingerprint != ?2
       AND review IN ('quorum', 'approved'))
     AND (EXISTS (SELECT 1 FROM readouts WHERE key = ?1 AND stage = 'whole' AND fingerprint = ?2
       AND review IN ('quorum', 'approved'))
     OR ((SELECT count(*) FROM readouts WHERE key = ?1 AND stage = 'whole'
       AND fingerprint = ?2 AND review != 'rejected') >= ?3
     AND (SELECT count(DISTINCT readout_networks.network) FROM readouts
       JOIN readout_networks ON readout_networks.id = readouts.id WHERE readouts.key = ?1 AND readouts.stage = 'whole'
       AND readouts.fingerprint = ?2 AND readouts.review != 'rejected') >= ?4)) RETURNING bytes`,
  ).bind(row.key, row.fingerprint, PER_KEY, MIN_NETWORKS).all()).results;
  // Taken as right: no longer held to the pending readouts' room.
  const agreed = [...firsts, ...wholes];
  const bytes = agreed.reduce((total, each) => total + each.bytes, 0);
  if (bytes) {
    await release(env.DB, PENDING, bytes, agreed.length);
  }
  // An offer of nothing (a device up to date) is agreed on, and not told.
  if (offerAgreed && row.offerFiles) {
    await tellAgreed(env, { ...row, stage: "first" }, firsts.length);
  }
  if (wholes.length) {
    await tellAgreed(env, row, wholes.length, Boolean(taken.checked));
  } else if (taken.checked) {
    await tellDisputed(env, row);
  }
  return true;
}

// The maintainer's email that readouts of a key have come to agree: unless
// the admin view's switch is off, or there is nowhere to send it. Those alike
// from too few networks send none: the admin view lists them (Nearly agreed).
// `checked`: a later readout's, agreeing with those taken as right already.
async function tellAgreed(env, row, newly, checked = false) {
  if (!env.EMAIL || !env.ALERTS_RECIPIENT_EMAIL) {
    return;
  }
  const setting = await env.DB.prepare("SELECT value FROM settings WHERE name = ?").bind(AGREEMENT_SETTING).first("value");
  if (setting === "off") {
    return;
  }
  await sendMail(env, {
    to: env.ALERTS_RECIPIENT_EMAIL,
    from: NOTIFY_FROM,
    subject: `Readouts ${checked ? "confirmed" : "agree"}: ${row.key} (${stageOf(row)})`,
    text: [checked
      ? `A later readout of ${row.key}, at the ${row.stage} stage, is alike those taken as right, so it is taken `
        + "as right too."
      : row.stage === "first"
        ? `Readouts of ${row.key} now agree on what eWeLink offers it (${row.offered}): ${PER_KEY} or more, first `
          + `stages or whole ones, from ${MIN_NETWORKS} or more networks, name the same files, so the offer is taken `
          + `as right (${newly} first stages newly).`
        : `Readouts of ${row.key}, at the ${stageOf(row)} stage, now agree: ${PER_KEY} or more alike, from `
          + `${MIN_NETWORKS} or more networks, so they are taken as right (${newly} newly).`, "",
      "python3 tools/repo/readouts.py review lists them. The admin view's switch turns these emails off."].join("\n"),
  }, row.received.slice(0, 10));
}

// A readout's stage as an email names it: a first stage with what it is offered.
const stageOf = (row) => (row.stage === "first" ? `first, offered ${row.offered}` : row.stage);

// The maintainer's email that a later readout disagrees with those of its key
// taken as right: whatever the switch, as one of them may be wrong.
async function tellDisputed(env, row) {
  if (!env.EMAIL || !env.ALERTS_RECIPIENT_EMAIL) {
    return;
  }
  await sendMail(env, {
    to: env.ALERTS_RECIPIENT_EMAIL,
    from: NOTIFY_FROM,
    subject: `Readouts disagree: ${row.key} (${row.stage})`,
    text: [`A later readout of ${row.key}, at the ${row.stage} stage (${row.id}), is not alike those taken as `
      + "right. One of them may be wrong. It waits for review, and until it is settled readouts of the key from "
      + "other networks are taken without waiting.", "",
      `python3 tools/repo/readouts.py review ${row.key.split("|")[0]} lists it (DISAGREES); the admin view's `
      + "Disputed list has the key. Approve or reject each with readouts.py."].join("\n"),
  }, row.received.slice(0, 10));
}

// The day's count of one sender's readouts, under a keyed hash of their
// address and the day: null where the Worker has no SENDER_KEY.
async function senderOn(env, request, day) {
  const hash = await keyedHash(env, `${day}|${addressOf(request)}`);
  return hash ? `sender:${hash}:${day}` : null;
}

// The names of the counts (counters.js): the day's readouts kept, and
// addresses given, and the bytes of every readout ever kept.
const readoutsOn = (day) => `readouts:${day}`;
const notifyOn = (day) => `notify:${day}`;
const READOUTS_EVER = "readouts:all";

// Why no readout can be kept now, or null: "store-full", the store's limit
// reached, which waiting a day will not change, or "full", the day's.
const fullness = async (env, day) => {
  const [today, ever] = await Promise.all([read(env.DB, readoutsOn(day)), read(env.DB, READOUTS_EVER)]);
  if (ever.bytes >= TOTAL_BYTES) {
    return "store-full";
  }
  return today.count >= DAILY_COUNT || today.bytes >= DAILY_BYTES ? "full" : null;
};

// A question of the tool's, its light work done: the parsed JSON, or a Response.
async function question(request, env, now, what) {
  if (request.method !== "POST") {
    return answer(405, { error: "asked by the tool, with POST" }, { Allow: "POST" });
  }
  if (request.headers.get("Content-Type") !== "application/json") {
    return answer(415, { error: `${what} is application/json` });
  }
  const declared = Number(request.headers.get("Content-Length"));
  if (!Number.isInteger(declared) || declared <= 0 || declared > QUESTION_MAX_BYTES) {
    return answer(413, { error: `${what} says its length, at most ${QUESTION_MAX_BYTES} bytes` });
  }
  const body = await request.arrayBuffer();
  if (body.byteLength !== declared) {
    return answer(400, { error: `that is not a whole ${what}` });
  }
  const bits = work.bits ?? LIGHT_PROOF_BITS;
  const subject = await bodySubject(body);
  const proof = String(request.headers.get("X-Proof") ?? "");
  if (!(await proven(proof, subject, now.getTime() / 1000, bits, LIGHT_PROOF_AGE))) {
    return answer(403, { error: `the ${what}'s proof of work does not check out` });
  }
  // Each proof once: within its minute, one sent again is not taken.
  const day = now.toISOString().slice(0, 10);
  const used = hex(await sha256(new TextEncoder().encode(`${proof}|${subject}`))).slice(0, 32);
  if (!(await reserve(env.DB, `proof:${used}`, day, { most: 1 }))) {
    return answer(403, { error: `the ${what}'s proof of work was used already` });
  }
  try {
    const asked = JSON.parse(new TextDecoder().decode(body));
    if (asked?.format === FORMAT) {
      return asked;
    }
  } catch {
    // below
  }
  return answer(400, { error: `that is not a ${what} this tool makes` });
}

// Whether a readout of a device would add anything, asked before it is made:
// {"format": 1, "model", "stock_model", "stock_version", "stage": "first"|"whole",
//  "firmware": [<SHA-256 of each file eWeLink offers it>], "device_hash"}. Answers
// {"wanted": bool, "why": "enough"|"held"|"full"|"store-full"|"limit" when not,
// "held": [<those of the files kept already>]}.
export async function wanted(request, env, now = new Date()) {
  const asked = await question(request, env, now, "question");
  if (asked instanceof Response) {
    return asked;
  }
  const firmware = asked.firmware ?? [];
  if (!fits(asked.model, MODEL) || !fits(asked.stock_model, STOCK_MODEL) || !fits(asked.stock_version, STOCK_VERSION)
      || !STAGES.includes(asked.stage)
      || !(asked.device_hash == null || (typeof asked.device_hash === "string" && DEVICE_HASH.test(asked.device_hash)))
      || !Array.isArray(firmware) || firmware.length > MAX_FIRMWARE
      || !firmware.every((sha) => typeof sha === "string" && /^[0-9a-f]{64}$/.test(sha))) {
    return answer(400, { error: "that is not a question this tool asks" });
  }
  await countWanted(env, request, asked.model, now.toISOString().slice(0, 10), asked.device_hash ?? null);
  const held = [];
  for (const sha of firmware) {
    if (await isHeld(env, firmwareKey(sha))) {
      held.push(sha);
    }
  }
  const no = (why) => answer(200, { wanted: false, why, held });
  if (enoughOf(env).includes(asked.model.toUpperCase())) {
    return no("enough");
  }
  const key = keyOf(asked.model, asked.stock_model, asked.stock_version);
  if (!(await adds(env, key, asked.stage, await senderOf(env, request, key), await networkOf(env, request, key), now,
    await offerOf(firmware)))) {
    return no("held");
  }
  const day = now.toISOString().slice(0, 10);
  const full = await fullness(env, day);
  if (full) {
    return no(full);
  }
  const sender = await senderOn(env, request, day);
  if (sender && (await read(env.DB, sender)).count >= SENDER_DAILY) {
    return no("limit");
  }
  return answer(200, { wanted: true, held });
}

export async function readout(request, env, now = new Date()) {
  if (request.method !== "POST") {
    return answer(405, { error: "readouts are sent here by the tool, with POST" }, { Allow: "POST" });
  }
  if (request.headers.get("Content-Type") !== "application/x-tar") {
    return answer(415, { error: "a readout is application/x-tar" });
  }
  const declared = Number(request.headers.get("Content-Length"));
  if (!Number.isInteger(declared) || declared <= 0) {
    return answer(411, { error: "a readout says its length" });
  }
  if (declared > MAX_BYTES) {
    return answer(413, { error: `a readout is at most ${MAX_BYTES} bytes` });
  }
  const { success } = await env.READOUT_LIMIT.limit({ key: addressOf(request) });
  if (!success) {
    return answer(429, { error: "too many readouts at once: try again in a minute" });
  }
  const body = await request.arrayBuffer();
  if (body.byteLength !== declared) {
    return answer(400, { error: "that is not a whole readout" });
  }
  const digest = await bodySubject(body);
  const bits = work.bits ?? PROOF_BITS;
  if (!(await proven(request.headers.get("X-Proof"), digest, now.getTime() / 1000, bits, PROOF_AGE))) {
    return answer(403, { error: "the readout's proof of work does not check out" });
  }
  const manifest = manifestOf(body);
  if (!manifest || !fits(manifest.model, MODEL)) {
    return answer(400, { error: "that is not a readout this tool makes" });
  }
  const files = filesOf(body);
  const offered = await offeredIn(files, manifest);
  if (!offered) {
    return answer(400, { error: "a file the readout names is not in it, or is not the file it says" });
  }
  if (manifest.read === true && !wholeFlash(files)) {
    return answer(400, { error: "a readout of the whole memory holds no whole flash chip's memory" });
  }
  const day = now.toISOString().slice(0, 10);
  // Its reference, which its sender is shown, and where it is kept.
  const id = `${day}-${digest.slice(0, 12)}`;
  const key = `readouts/${day}/${digest.slice(0, 24)}.tar`;
  if (await env.READOUTS.head(key)) {
    return answer(200, { id });
  }
  if (enoughOf(env).includes(manifest.model.toUpperCase())) {
    return answer(200, { enough: true });
  }
  const stage = manifest.read === true ? "whole" : "first";
  const model = fits(manifest.stock_model, STOCK_MODEL) ? manifest.stock_model : "-";
  const version = fits(manifest.stock_version, STOCK_VERSION) ? manifest.stock_version : "-";
  const sender = await senderOn(env, request, day);
  if (!sender) {
    return answer(503, { error: "the API is being set up: try again later" }, { "Retry-After": "3600" });
  }
  // Each place taken in turn, and given back in turn if a later one is not.
  const taken = [];
  const giveBack = async () => {
    for (const [name, bytes] of taken.reverse()) {
      await release(env.DB, name, bytes);
    }
  };
  const take = async (name, period, limits, bytes = 0) => {
    const got = await reserve(env.DB, name, period, { ...limits, bytes });
    if (got) {
      taken.push([name, bytes]);
    }
    return got;
  };
  // A readout of a key its sender has sent already, or with no place left,
  // is thanked and not kept - before it takes any of its sender's day.
  const readoutKey = keyOf(manifest.model, model, version);
  const from = await senderOf(env, request, readoutKey);
  const network = await networkOf(env, request, readoutKey);
  const offer = await offerOf(offeredShas(manifest));
  if (!(await adds(env, readoutKey, stage, from, network, now, offer))) {
    return answer(200, { enough: true });
  }
  if (!(await take(sender, day, { most: SENDER_DAILY }))) {
    await giveBack();
    return answer(503, { error: `${SENDER_DAILY} readouts a day from one address: try again tomorrow`, limit: true },
      { "Retry-After": "86400" });
  }
  if (!(await take(readoutsOn(day), day, { most: DAILY_COUNT, mostBytes: DAILY_BYTES }, body.byteLength))) {
    await giveBack();
    return answer(503, { error: "enough readouts for today: try again tomorrow" }, { "Retry-After": "86400" });
  }
  if (!(await take(READOUTS_EVER, null, { mostBytes: TOTAL_BYTES }, body.byteLength))) {
    await giveBack();
    return answer(503, { error: "the store of readouts is full: try again another time" },
      { "Retry-After": "604800" });
  }
  // Pending until three agree or it is approved, and held to their room.
  if (!(await take(PENDING, null, { mostBytes: PENDING_BYTES }, body.byteLength))) {
    await giveBack();
    return answer(503, { error: "too many readouts are waiting for review: try again another time" },
      { "Retry-After": "604800" });
  }
  const row = { key: readoutKey, stage, sender: from, network, id, fingerprint: await fingerprintOf(manifest, stage),
    bytes: body.byteLength, object: key, received: now.toISOString(), offer, offered: offeredVersions(manifest),
    offerFiles: offeredShas(manifest).filter((sha) => /^[0-9a-f]{64}$/.test(String(sha))).length };
  if (!(await hold(env, row, now))) {
    await giveBack();   // its place taken meanwhile, by another sent at once
    return answer(200, { enough: true });
  }
  try {
    await keep(env, key, body, manifest, id, offered);
  } catch (error) {
    // Nothing of it left behind: a mark left would say a file is held that
    // is not, and nothing would ever delete it.
    const { results: marks } = await env.DB.prepare("SELECT sha FROM readout_firmware WHERE readout = ?").bind(id).all();
    for (const mark of marks) {
      await env.READOUTS.delete(firmwareKey(mark.sha));
    }
    await env.READOUTS.delete(key);
    await env.DB.prepare("DELETE FROM readout_firmware WHERE readout = ?").bind(id).run();
    await env.DB.prepare("DELETE FROM readout_networks WHERE id = ?").bind(id).run();
    await env.DB.prepare("DELETE FROM readouts WHERE id = ?").bind(id).run();
    await giveBack();
    throw error;
  }
  return answer(201, { id });
}

// A readout, with the marks of the offered files it holds (offeredIn).
async function keep(env, key, body, manifest, id, offered) {
  await env.READOUTS.put(key, body, {
    httpMetadata: { contentType: "application/x-tar" },
    customMetadata: {
      tool: String(manifest.tool || "").replace(/[^\x20-\x7e]/g, "").slice(0, 16),
      model: manifest.model.slice(0, 40),
      read: String(manifest.read === true),
    },
  });
  // Each file it holds marked, once - the first readout to hold one keeps the
  // mark - and listed by the readout, so deleting it deletes its marks too:
  // listed first, so a mark is never written that its readout does not list.
  for (const sha of offered) {
    if (!(await isHeld(env, firmwareKey(sha)))) {
      await env.DB.prepare("INSERT OR IGNORE INTO readout_firmware (readout, sha) VALUES (?1, ?2)").bind(id, sha).run();
      await env.READOUTS.put(firmwareKey(sha), id, { customMetadata: { readout: key } });
    }
  }
}

const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const isAddress = (email) => text(email, 254) && EMAIL.test(email);
const WHO = /^[0-9a-f]{64}$/;
const TOKEN = /^[0-9a-f]{32}$/;
// A readout's reference, as readout() makes it.
const READOUT_ID = /^\d{4}-\d{2}-\d{2}-[0-9a-f]{12}$/;
export const FORGET_URL = "https://api.firmwareliberator.com/forget";
// The website, whose sign-up page (/notify) posts to /v1/notify from the
// browser: the one origin let read its answers (CORS).
export const SITE_ORIGIN = "https://firmwareliberator.com";
const CORS = { "Access-Control-Allow-Origin": SITE_ORIGIN };
// The website's sign-up page, for one who removed their address and wants
// it back.
export const SIGNUP_PAGE = `${SITE_ORIGIN}/notify`;
// A sign-up: {"format": 1, "email", "devices": [{"model", "stock_model"}],
// "readouts": [<reference>], "update": true|false} - from the tool, the
// devices in the owner's eWeLink account, supported or not, [] where none was
// seen, and the readouts the owner sent that sitting; from the website's page,
// the address alone. "update": the tool's sending of a later sitting's, with
// the address given before, which only adds to a confirmed address.
// Not kept as given: signUp says what is. Its answers carry CORS, so the
// page can read them; the browser asks first, with OPTIONS (preflight).
export async function notify(request, env, now = new Date()) {
  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: { ...CORS, "Access-Control-Allow-Methods": "POST",
      "Access-Control-Allow-Headers": "Content-Type, X-Proof", "Access-Control-Max-Age": "86400" } });
  }
  const answered = request.method === "POST" ? await signUpAsked(request, env, now)
    : answer(405, { error: "asked with POST" }, { Allow: "POST, OPTIONS" });
  for (const [name, value] of Object.entries(CORS)) {
    answered.headers.set(name, value);
  }
  return answered;
}

async function signUpAsked(request, env, now) {
  const asked = await question(request, env, now, "request");
  if (asked instanceof Response) {
    return asked;
  }
  const { devices, readouts = [], update = false } = asked;
  const email = typeof asked.email === "string" ? asked.email.trim() : "";
  if (!isAddress(email) || typeof update !== "boolean" || !Array.isArray(devices) || devices.length > MAX_DEVICES
      || !devices.every((device) => fits(device?.model, MODEL) && fits(device?.stock_model ?? "-", STOCK_MODEL))
      || !Array.isArray(readouts) || readouts.length > MAX_READOUTS
      || !readouts.every((id) => typeof id === "string" && READOUT_ID.test(id))) {
    return answer(400, { error: "that is not a request this tool makes" });
  }
  const named = [...new Map(devices.map((device) => {
    const each = { model: device.model.toUpperCase(), stock_model: device.stock_model ?? "-" };
    return [`${each.model}|${each.stock_model}`, each];
  })).values()];
  const taken = await signUp(env, request, now, email, named, [...new Set(readouts)], update);
  return taken.error ? answer(taken.status, { error: taken.error }, taken.headers)
    : answer(202, { confirming: true });
}

// An address given, by the tool or the sign-up page: sent a link to confirm
// it (/confirm), and only once its owner follows it is it kept (subscribers),
// with the devices and readouts it named, until removed (/forget, by the link
// every email to it carries). Asked and never confirmed, it is deleted after
// SIGNUP_DAYS (pending.js). One confirmed already is sent nothing, and only
// takes what it named; with `update`, one not confirmed is sent nothing either. Answered alike whether or not a confirmation was sent,
// so nobody learns whether an address was given before. {status} when taken,
// or {status, error, headers}.
async function signUp(env, request, now, email, devices, readouts, update = false) {
  const later = (status, error, after) => ({ status, error, headers: after ? { "Retry-After": after } : {} });
  if (!env.EMAIL) {
    return later(503, "not taking addresses yet: try again another time", "86400");
  }
  const day = now.toISOString().slice(0, 10);
  const sender = await keyedHash(env, `notify|${day}|${addressOf(request)}`);
  if (!sender) {
    return later(503, "the API is being set up: try again later", "3600");
  }
  if (!(await reserve(env.DB, `notify-sender:${sender}:${day}`, day, { most: NOTIFY_SENDER_DAILY }))) {
    return later(503, `${NOTIFY_SENDER_DAILY} a day from one address: try again tomorrow`, "86400");
  }
  const who = hex(await sha256(new TextEncoder().encode(email.toLowerCase())));
  // An address confirmed already takes the devices and readouts named, with
  // no email: the tool sends them each sitting once its owner has signed up.
  if (await env.DB.prepare("SELECT 1 AS n FROM subscribers WHERE who = ?").bind(who).first("n")) {
    await keepAddress(env, { who, email, devices, readouts }, now);
    return { status: 202 };
  }
  // An update for an address not confirmed - removed by its owner, or never
  // confirmed, so deleted - starts nothing: no email, nothing kept. Else each
  // later sitting would email it again, one who left included.
  if (update) {
    return { status: 202 };
  }
  // One confirmation an address a day, however many ask for it.
  if (!(await reserve(env.DB, `notify-to:${who}:${day}`, day, { most: 1 }))) {
    return { status: 202 };
  }
  if (!(await reserve(env.DB, notifyOn(day), day, { most: NOTIFY_DAILY }))) {
    return later(503, "enough for today: try again tomorrow", "86400");
  }
  const token = hex(crypto.getRandomValues(new Uint8Array(16)));
  await env.DB.prepare(`INSERT INTO confirmations (token, who, email, devices, readouts, day, sent)
    VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)`)
    .bind(token, who, email, JSON.stringify(devices), JSON.stringify(readouts), day, now.toISOString()).run();
  const mailed = await sendMail(env, confirmation(email, devices, token), day);
  if (!mailed.sent) {
    await env.DB.prepare("DELETE FROM confirmations WHERE token = ?").bind(token).run();
    await release(env.DB, notifyOn(day));
    // An address that bounced for good, or marked one of ours as spam, is
    // answered as any other, so nobody learns that of it; it is not tried
    // again today.
    if (mailed.code === SUPPRESSED) {
      return { status: 202 };
    }
    await release(env.DB, `notify-to:${who}:${day}`);
    return later(502, "the confirmation could not be sent: try again later");
  }
  return { status: 202 };
}

const modelsOf = (devices) => [...new Set(devices.map((device) => device.model))];

// The email asking an address's owner to confirm it, or, given the day it was
// asked for, asking once more.
function confirmation(email, devices, token, given = null) {
  const reminder = given !== null;
  const products = modelsOf(devices);
  return {
    to: email,
    from: NOTIFY_FROM,
    subject: `${reminder ? "Reminder - confirm" : "Confirm"}: email me as more devices are supported`,
    text: [
      `Someone - we hope you - asked Firmware Liberator${reminder ? ` on ${given}` : ""} to email this address as more`,
      products.length ? `devices are supported, and when the support for these changes: ${products.join(", ")}.`
        : "devices are supported.",
      "",
      `To be emailed, confirm it here: ${CONFIRM_URL}?t=${token}`,
      "",
      reminder
        ? `This is the only reminder. If it was not you, do nothing: unless it is confirmed, the address is deleted`
        : `If it was not you, do nothing: unless it is confirmed, the address is deleted in ${SIGNUP_DAYS} days`,
      reminder ? `${SIGNUP_DAYS} days after it was given, and never written to again.` : "and never written to again.",
    ].join("\n"),
  };
}

// The hourly cron's part (scheduled): each address sent its link
// REMIND_AFTER_DAYS or more ago, still not confirmed, is sent it once more - once an
// address, however many times it was given, and none already confirmed
// through another link. Counted against NOTIFY_DAILY like any confirmation.
// How many it sent.
export async function remind(env, now = new Date()) {
  if (!env.EMAIL || !(await ready(env.DB))) {
    return 0;
  }
  const today = now.toISOString().slice(0, 10);
  const before = new Date(now.getTime() - REMIND_AFTER_DAYS * 24 * 60 * 60 * 1000).toISOString();
  const { results } = await env.DB.prepare(`SELECT confirmations.* FROM confirmations
    LEFT JOIN counters ON counters.name = 'notify-remind:' || confirmations.who AND counters.count > 0
    WHERE confirmations.sent <= ?1 AND counters.name IS NULL
      AND confirmations.who NOT IN (SELECT who FROM subscribers)
    ORDER BY confirmations.sent LIMIT ?2`)
    .bind(before, REMIND_EACH_RUN).all();
  let sent = 0;
  for (const signup of results) {
    // The counter outlives the sign-up (pruning in nightly.js, SIGNUP_DAYS in
    // pending.js), so a run twice reminds nobody twice.
    if (!(await reserve(env.DB, `notify-remind:${signup.who}`, today, { most: 1 }))) {
      continue;
    }
    if (!(await reserve(env.DB, notifyOn(today), today, { most: NOTIFY_DAILY }))) {
      await release(env.DB, `notify-remind:${signup.who}`);
      break;
    }
    const mailed = await sendMail(env, confirmation(signup.email, JSON.parse(signup.devices), signup.token, signup.day),
      today);
    if (mailed.sent) {
      sent += 1;
      continue;
    }
    await release(env.DB, notifyOn(today));
    // Suppressed, it is not to be tried again; any other failure, next hour.
    if (mailed.code !== SUPPRESSED) {
      await release(env.DB, `notify-remind:${signup.who}`);
    }
  }
  return sent;
}

// The link in a confirmation (?t=<token>): a GET shows what it confirms and
// a button, and only the POST that button sends keeps the address, so a mail
// scanner following the link confirms nothing.
export async function confirm(request, env, now = new Date()) {
  let token;
  if (request.method === "GET") {
    token = new URL(request.url).searchParams.get("t") ?? "";
  } else if (request.method === "POST") {
    token = new URLSearchParams(await request.text()).get("t") ?? "";
  } else {
    return answer(405, { error: "opened from the link in an email" }, { Allow: "GET, POST" });
  }
  const signup = TOKEN.test(token)
    ? await env.DB.prepare("SELECT * FROM confirmations WHERE token = ?").bind(token).first() : null;
  if (!signup) {
    return page(404, "Nothing to confirm",
      `<p>This link is not one we know, or it has been used, or more than ${SIGNUP_DAYS} days have passed.</p>`);
  }
  const products = modelsOf(JSON.parse(signup.devices));
  if (request.method === "GET") {
    return page(200, "Be emailed as more devices are supported?",
      `<p>Firmware Liberator will email you as more devices are supported${products.length
        ? `, and when the support for these changes: ${escape(products.join(", "))}` : ""}.
Every such email has a link to stop them.</p>
<form method="post" action="/confirm"><input type="hidden" name="t" value="${token}">
<button type="submit">Yes, email me</button></form>`);
  }
  await keepAddress(env, { ...signup, devices: JSON.parse(signup.devices), readouts: JSON.parse(signup.readouts) },
    now, env.DB.prepare("DELETE FROM confirmations WHERE token = ?").bind(token));
  return page(200, "Confirmed", "<p>You will be emailed as more devices are supported. Thank you for helping.</p>");
}

// A confirmed address, kept once, with its one removal token, kept as it was
// first made so every email sent it carries the same link. Its devices and
// readouts are added to those kept, never taken away: a device gone from the
// owner's eWeLink account was most likely converted, not given away. Each
// device keeps when it was first and last named. `also`: a statement run in
// the same batch (the confirmation used up).
async function keepAddress(env, { who, email, devices, readouts }, now, also = null) {
  const at = now.toISOString();
  await env.DB.batch([
    env.DB.prepare(`INSERT INTO subscribers (who, email, token, since, updated) VALUES (?1, ?2, ?3, ?4, ?4)
      ON CONFLICT (who) DO UPDATE SET email = excluded.email, updated = excluded.updated`)
      .bind(who, email, hex(crypto.getRandomValues(new Uint8Array(16))), at),
    ...devices.map((device) => env.DB.prepare(`INSERT INTO subscriber_devices (who, model, stock_model, first_seen,
      last_seen) VALUES (?1, ?2, ?3, ?4, ?4) ON CONFLICT (who, model, stock_model) DO UPDATE SET last_seen = excluded.last_seen`)
      .bind(who, device.model, device.stock_model, at)),
    ...readouts.map((id) => env.DB.prepare("INSERT OR IGNORE INTO subscriber_readouts (who, readout) VALUES (?1, ?2)")
      .bind(who, id)),
    ...(also ? [also] : []),
  ]);
}

// Text put in a page's HTML, from wherever it came.
const escape = (value) => String(value).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function page(status, title, body) {
  return new Response(`<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>${escape(title)}</title><style>body{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem}
button{font:inherit;padding:.5rem 1rem}</style></head><body><h1>${escape(title)}</h1>${body}</body></html>`,
  { status, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
    // Never inside another site's page, which could dress its button up as
    // something else.
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer" } });
}

// Whether `token` is the one `kept` holds, compared in fixed time.
function sameToken(kept, token) {
  if (!TOKEN.test(kept) || !TOKEN.test(token)) {
    return false;
  }
  let differ = 0;
  for (let i = 0; i < kept.length; i += 1) {
    differ |= kept.charCodeAt(i) ^ token.charCodeAt(i);
  }
  return differ === 0;
}

// The link in each email to an address kept (?a=<its SHA-256>&t=<token>): a
// GET shows what it would do and a button, and only the POST that button
// sends removes the address - a mail scanner following the link removes
// nothing. Its devices and readouts go with it. A mail program's one-click
// unsubscribe (RFC 8058: the List-Unsubscribe and List-Unsubscribe-Post
// headers each update email carries, .github/readouts.md) POSTs to the link
// itself, "List-Unsubscribe=One-Click" its body: the link's own a and t.
export async function forget(request, env) {
  let who, token;
  const url = new URL(request.url);
  if (request.method === "GET") {
    [who, token] = [url.searchParams.get("a") ?? "", url.searchParams.get("t") ?? ""];
  } else if (request.method === "POST") {
    const form = new URLSearchParams(await request.text());
    [who, token] = [form.get("a") ?? url.searchParams.get("a") ?? "", form.get("t") ?? url.searchParams.get("t") ?? ""];
  } else {
    return answer(405, { error: "opened from the link in an email" }, { Allow: "GET, POST" });
  }
  const kept = WHO.test(who) ? await env.DB.prepare("SELECT token FROM subscribers WHERE who = ?").bind(who)
    .first("token") : null;
  if (!sameToken(String(kept ?? ""), token)) {
    return page(404, "Nothing to remove",
      "<p>This link is not one we know, or the address it was for has been removed already.</p>");
  }
  if (request.method === "GET") {
    return page(200, "Stop these emails?",
      `<p>Your email address will be removed from Firmware Liberator's list. You will not be emailed again.</p>
<form method="post" action="/forget"><input type="hidden" name="a" value="${who}">
<input type="hidden" name="t" value="${token}"><button type="submit">Remove my address</button></form>`);
  }
  await env.DB.batch(["subscriber_devices", "subscriber_readouts", "subscribers"].map((table) =>
    env.DB.prepare(`DELETE FROM ${table} WHERE who = ?`).bind(who)));
  return page(200, "Removed", `<p>Your email address is off the list. Thank you for helping.</p>
<p>Changed your mind? <a href="${SIGNUP_PAGE}">Sign up again</a>.</p>`);
}
