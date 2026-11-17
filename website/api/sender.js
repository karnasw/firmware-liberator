// Who sent a request, as the API's limits count senders: an IPv4 address
// whole, and an IPv6 one by its first 48 bits (its /48). One home is given a
// /64 at least, often a /56 or a /48, and one rented server as easily a /48:
// 65,536 /64s, each of which would otherwise seem another sender. A few homes
// on one provider may share a /48 and be counted as one, which the limits
// allow for. One with many IPv4 addresses still seems many. Never stored as
// it is: index.js and reports.js keep only a keyed hash of it.

// The sender's address from Cloudflare's header, IPv6 cut to its /48.
export function senderAddress(request) {
  return prefixOf(request.headers.get("CF-Connecting-IP") || "unknown");
}

// An address as senders are counted: an IPv6 one's first three groups,
// written out in full (so 2001:db8::1 and 2001:0db8:0:5::2 are one sender);
// anything else as it is.
export function prefixOf(address) {
  const text = String(address).trim().toLowerCase();
  if (!text.includes(":")) {
    return text;
  }
  const [head, tail = ""] = text.split("%")[0].split("::");
  const groups = (part) => (part ? part.split(":") : []);
  let words = groups(head);
  if (text.includes("::")) {
    const after = groups(tail);
    words = [...words, ...Array(Math.max(0, 8 - words.length - after.length)).fill("0"), ...after];
  }
  if (words.length < 3 || words.some((word) => !/^[0-9a-f]{1,4}$/.test(word))) {
    return text;   // not one we can read: counted as it is
  }
  return `${words.slice(0, 3).map((word) => word.padStart(4, "0")).join(":")}::/48`;
}
