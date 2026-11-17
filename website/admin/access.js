// Who is asking: the admin view is behind Cloudflare Access (made by
// tools/repo/cloudflare_settings.py: GitHub sign-in, the maintainer alone),
// which signs each request it lets through (Cf-Access-Jwt-Assertion). This
// checks that signature itself, against Access's published keys, with its
// audience (ACCESS_AUD: this application's), its issuer (the account's team,
// ACCESS_TEAM) and its times - so a request that did not come through Access,
// or a setting gone wrong there, is refused here too.

const keysets = new Map();
const KEYS_KEPT = 3_600_000;

function base64url(text) {
  const plain = text.replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(plain + "=".repeat((4 - (plain.length % 4)) % 4)), (char) => char.charCodeAt(0));
}

const json = (part) => JSON.parse(new TextDecoder().decode(base64url(part)));

// Access's public keys for the team, kept an hour.
async function keysOf(team, fetcher, now) {
  const kept = keysets.get(team);
  if (kept && now - kept.at < KEYS_KEPT) {
    return kept.keys;
  }
  const response = await fetcher(`https://${team}.cloudflareaccess.com/cdn-cgi/access/certs`);
  if (!response.ok) {
    throw new Error(`Access's keys answered ${response.status}`);
  }
  const { keys } = await response.json();
  keysets.set(team, { at: now, keys });
  return keys;
}

// The signed-in user's claims, or null where the request is not Access's:
// for the application whose audience tag is `audience` - the view's
// (ACCESS_AUD) unless another is given, as emulator.js gives its own.
export async function verified(request, env, now = Date.now(), fetcher = fetch, audience = env.ACCESS_AUD) {
  const token = request.headers.get("Cf-Access-Jwt-Assertion");
  if (!token || !env.ACCESS_TEAM || !audience) {
    return null;
  }
  const parts = token.split(".");
  if (parts.length !== 3) {
    return null;
  }
  let header;
  let claims;
  try {
    [header, claims] = [json(parts[0]), json(parts[1])];
  } catch {
    return null;
  }
  if (header.alg !== "RS256" || typeof header.kid !== "string") {
    return null;
  }
  const jwk = (await keysOf(env.ACCESS_TEAM, fetcher, now)).find((key) => key.kid === header.kid);
  if (!jwk) {
    return null;
  }
  const key = await crypto.subtle.importKey("jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false,
    ["verify"]);
  const signed = new TextEncoder().encode(`${parts[0]}.${parts[1]}`);
  if (!(await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, base64url(parts[2]), signed))) {
    return null;
  }
  const audiences = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  const seconds = now / 1000;
  if (!audiences.includes(audience) || claims.iss !== `https://${env.ACCESS_TEAM}.cloudflareaccess.com`
      || !(claims.exp > seconds) || (claims.nbf !== undefined && claims.nbf > seconds + 60)) {
    return null;
  }
  return claims;
}
