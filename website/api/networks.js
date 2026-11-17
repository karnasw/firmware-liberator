// Which networks count, as agreements count them: readouts agree only from
// MIN_NETWORKS networks (index.js), and a model's name is shown only once
// given from VOUCH_NETWORKS (nightly.js). A network anyone can rent servers
// or a VPN's exits on counts as none: else made-up readouts from three
// clouds, or from one VPN switched between three countries, would be three
// networks. Such a request is still taken, as one Cloudflare names no network
// for is: an owner behind a VPN, or at work at a host, loses nothing but that
// vote, and readouts alike from too few networks wait for one more or for the
// maintainer. Nothing here stops residential proxies, which really are
// owners' networks: the later checks of agreed readouts (index.js) are what
// would catch what came through them.
//
// Two sources, either enough alone:
// - CLOUD_ASNS, kept here, always applied: the big clouds, the hosts most
//   VPNs' exits are on, and the relays (Cloudflare's WARP and Workers,
//   iCloud Private Relay's Akamai and Fastly).
// - NETWORK_LISTS, X4BNet's lists of datacenter and VPN ASNs, kept up to date
//   by others, fetched each night (refreshNetworks) and kept in the database
//   (network_lists), so a new host or VPN counts as none with no change here.
//   Fetched, never copied into this repository: they carry no licence. Each
//   from GitHub, or failing that from jsDelivr's copy of the same repository
//   (LIST_SOURCES): GitHub may refuse Workers, which share their addresses,
//   past its limit for unauthenticated downloads. A list that comes from
//   neither, or comes far shorter than the one kept, is not taken:
//   the one kept stays, the admin view shows its age, and the nightly alert
//   says so each LIST_STALE_DAYS it goes untaken (alerts.js). One taken far
//   longer than the one kept is told too (LIST_JUMP).
import { ready } from "./database.js";

// Amazon (16509, 14618), Google (15169, 396982), Microsoft (8075),
// DigitalOcean (14061), Hetzner (24940), OVH (16276), Akamai's Linode
// (63949), Vultr (20473), Oracle (31898), Contabo (51167), Alibaba (45102);
// M247 (9009), Datacamp (60068, 212238), Leaseweb (16265, 60781), PacketHub
// (136787); Cloudflare (13335), Akamai (36183), Fastly (54113).
export const CLOUD_ASNS = new Set([16509, 14618, 15169, 396982, 8075, 14061, 24940, 16276, 63949, 20473, 31898,
  51167, 45102, 9009, 60068, 212238, 16265, 60781, 136787, 13335, 36183, 54113]);

// Each list by the name network_lists keeps it under, and its path in
// X4BNet's repository: one "AS<number>" a line, "#" a comment, "-AS<number>"
// one the list's own build leaves out (left out here too).
export const NETWORK_LISTS = { datacenter: "input/datacenter/ASN.txt", vpn: "input/vpn/ASN.txt" };
// Where each is fetched from, in turn until one gives it: by name, the base
// its path is put after. jsDelivr serves a branch as it was up to a day ago.
export const LIST_SOURCES = {
  GitHub: "https://raw.githubusercontent.com/X4BNet/lists_vpn/main/",
  jsDelivr: "https://cdn.jsdelivr.net/gh/X4BNet/lists_vpn@main/",
};
// A list larger than this, or with more ASNs, is not one of these.
export const LIST_MAX_BYTES = 1024 * 1024;
export const LIST_MAX_ASNS = 50_000;
// A list with fewer than this share of the ASNs of the one kept is taken as
// cut short, not as that many hosts gone.
export const LIST_SHRINK = 0.5;
export const LIST_STALE_DAYS = 7;
// A list taken with this share more ASNs than the one kept is told the same
// night (alerts.js), with up to LIST_ADDED_SHOWN of those it added: one
// mistaken entry for a big home provider would cost its customers their vote.
export const LIST_JUMP = 0.5;
export const LIST_ADDED_SHOWN = 20;
// How long a Worker uses the lists it read before reading them again.
const LIST_CACHE_MS = 10 * 60 * 1000;

// The share of a list's lines that may be unreadable, each passed over: so
// one stray line upstream does not stop the list being taken, while a page
// that is no list at all (an error page, say) is still refused.
export const LIST_ODD_SHARE = 0.02;

// The ASNs a list's text names, or null where it is not one.
export function asnsOf(text) {
  const asns = new Set();
  let odd = 0;
  for (const line of text.split("\n")) {
    const body = line.replace(/#.*/, "").trim();
    if (!body || body.startsWith("-")) {
      continue;
    }
    const found = /^AS(\d{1,10})$/i.exec(body);
    if (found) {
      asns.add(Number(found[1]));
    } else {
      odd += 1;
    }
  }
  return asns.size && asns.size <= LIST_MAX_ASNS && odd <= LIST_ODD_SHARE * (asns.size + odd)
    ? [...asns].sort((a, b) => a - b) : null;
}

// The lists' ASNs as the database keeps them, read once each LIST_CACHE_MS.
const cached = new WeakMap();
async function listed(db, now = Date.now()) {
  const last = cached.get(db);
  if (last && now - last.at < LIST_CACHE_MS) {
    return last.asns;
  }
  const { results } = await db.prepare("SELECT asns FROM network_lists").all();
  const asns = new Set(results.flatMap((row) => JSON.parse(row.asns)));
  cached.set(db, { at: now, asns });
  return asns;
}

// The network a request came from (Cloudflare's ASN), as agreements count
// networks: null where Cloudflare gives none, or it is on CLOUD_ASNS or a list.
export async function countedNetwork(env, request) {
  const asn = request.cf?.asn;
  if (!Number.isInteger(asn) || CLOUD_ASNS.has(asn) || (await listed(env.DB)).has(asn)) {
    return null;
  }
  return asn;
}

// One list from one source: its ASNs, or why not, held to the `before` kept.
async function fetchList(url, before, fetcher) {
  try {
    const response = await fetcher(url);
    const text = response.ok ? await response.text() : "";
    const asns = response.ok && text.length <= LIST_MAX_BYTES ? asnsOf(text) : null;
    const problem = !response.ok ? `HTTP ${response.status}` : !asns ? "not a list of ASNs"
      : asns.length < LIST_SHRINK * before ? `${asns.length} ASNs, where ${before} were kept` : null;
    return problem ? { problem } : { asns };
  } catch (error) {
    return { problem: String(error?.message ?? error).slice(0, 200) };
  }
}

// Each list fetched afresh and kept, from the first source that gives it
// whole and not cut short: by name, "taken" or why not (each source's why).
// The nightly cron's (index.js scheduled).
export async function refreshNetworks(env, now = new Date(), fetcher = fetch) {
  if (!(await ready(env.DB))) {
    return {};
  }
  const day = now.toISOString().slice(0, 10);
  const done = {};
  for (const [name, path] of Object.entries(NETWORK_LISTS)) {
    const kept = JSON.parse(await env.DB.prepare("SELECT asns FROM network_lists WHERE name = ?").bind(name)
      .first("asns") ?? "[]");
    const before = kept.length;
    const problems = [];
    let asns = null;
    for (const [source, base] of Object.entries(LIST_SOURCES)) {
      const got = await fetchList(`${base}${path}`, before, fetcher);
      if (got.asns) {
        asns = got.asns;
        break;
      }
      problems.push(`${source}: ${got.problem}`);
    }
    const problem = asns ? null : problems.join("; ");
    // Taken: its ASNs, today, and how many were kept before it and which it
    // added. Not: the ASNs and day kept stay (the first try's day, while none
    // was ever taken), with why.
    const added = asns ? asns.filter((asn) => !kept.includes(asn)).slice(0, LIST_ADDED_SHOWN) : [];
    await (problem
      ? env.DB.prepare(`INSERT INTO network_lists (name, asns, taken, problem) VALUES (?1, '[]', ?2, ?3)
         ON CONFLICT (name) DO UPDATE SET problem = ?3`).bind(name, day, problem)
      : env.DB.prepare(`INSERT INTO network_lists (name, asns, taken, problem, previous, added)
         VALUES (?1, ?2, ?3, NULL, ?4, ?5)
         ON CONFLICT (name) DO UPDATE SET asns = ?2, taken = ?3, problem = NULL, previous = ?4, added = ?5`)
        .bind(name, JSON.stringify(asns), day, before, JSON.stringify(added)))
      .run();
    done[name] = problem ?? "taken";
  }
  cached.delete(env.DB);
  return done;
}
