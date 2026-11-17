#!/usr/bin/env python3
"""The readouts people have sent, in the R2 bucket the API Worker keeps them in.

    python3 tools/repo/readouts.py [prefix]        list them (prefix: readouts/)
    python3 tools/repo/readouts.py notify [PRODUCT]  who asked to be emailed as
                                                    more devices are supported
                                                    (of a product: its owners)
    python3 tools/repo/readouts.py forget ADDRESS  take one off that list (one that
                                                    bounced, say)
    python3 tools/repo/readouts.py review [PRODUCT]  the readouts waiting for review
    python3 tools/repo/readouts.py approve ID        one taken as right
    python3 tools/repo/readouts.py reject ID         one taken as wrong: its place
                                                    goes to another sender's
    python3 tools/repo/readouts.py offers [PRODUCT]  what eWeLink offers each key, as
                                                    readouts agreed on it
    python3 tools/repo/readouts.py rotated TIME      the keys taken as right with
                                                    readouts from both sides of
                                                    SENDER_KEY's rotation at TIME
    python3 tools/repo/readouts.py changes [REF]     the devices whose status changed
                                                    since REF (the latest release's
                                                    tag), and whom to tell of each

Each line is a readout's key, size, when it came in and what the Worker noted
of it: the tool, the model, and whether it holds a whole read.
Under readouts/ only the readouts are listed, not the marks of what is held
beside them; a prefix given lists everything under it.

`review` lists, by product, firmware model and version, each readout the
API has not taken as right: fewer than three of them, from three senders,
agree (.github/readouts.md); first stages each offer apart (the versions
eWeLink offered them), as each is agreed on apart. Each line is its
reference, stage, when it came in, its object and fingerprint; one whose
fingerprint differs from the rest of its key is marked ODD, and one that
came to check those taken as right, and is not alike them, DISAGREES.
`offers` lists, by key, each offer readouts agreed on - first stages or
whole ones, from three networks - with the versions offered and the day it
was. `rotated` lists, the same way, each key taken as right from readouts sent
both before and after TIME (as `cloudflare_settings.py
rotate SENDER_KEY` printed it): two of its three could be one sender's, as a
new key makes every sender look new. The API's database is read with the
maintainer's CLOUDFLARE_READOUTS_REVIEW_TOKEN (D1 is refused to CI's).

`notify` lists each address confirmed, from the API's database: its
devices (eWeLink's product model and the firmware model, as the tool last
saw the owner's account), the readouts it sent, when it was confirmed, and
the link each email to it carries for its owner to remove it. Given a
product, only that product's owners. Any address on Email Service's
suppression list for a hard bounce or a spam complaint is taken off the list
first, and how many said. `forget` takes off one address, as its own link
would: one an email sent by hand bounced from. Everyone listed is emailed
when a new device is supported (.github/readouts.md). `changes` lists each device whose
status in devices/status.json differs from REF's, with its owners: run it
when a release changes one. Never from CI, whose logs are public.
Fetch one with `npx wrangler r2 object get <bucket>/<key> --remote --file
readout.tar`, from website/, with CLOUDFLARE_API_TOKEN, as wrangler names it,
set to the same token. It is CLOUDFLARE_READOUTS_REVIEW_TOKEN, in the
environment or tools/repo/.env.infra, as TOKENS in
tools/repo/cloudflare_settings.py allows it; tools/repo/cloudflare_tokens.py
makes it.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.repo import cloudflare_settings as cs  # noqa: E402


def objects(prefix: str) -> Iterator[dict[str, Any]]:
    """Every object under `prefix`, a page of 1000 at a time."""
    zones = cs.api(f"/zones?name={cs.ZONE}")
    if not zones:
        raise cs.SettingsError(f"the token cannot see the zone {cs.ZONE}")
    path = f"/accounts/{zones[0]['account']['id']}/r2/buckets/{cs.READOUTS_BUCKET}/objects"
    query = {"prefix": prefix, "per_page": "1000"}
    while True:
        answer = cs.api(f"{path}?{urllib.parse.urlencode(query)}", whole=True)
        yield from answer.get("result") or []
        info = answer.get("result_info") or {}
        if not info.get("is_truncated") or not info.get("cursor"):
            return
        query["cursor"] = info["cursor"]


def describe(item: dict[str, Any]) -> str:
    noted = item.get("custom_metadata") or {}
    return "  ".join(shown(part) for part in [item["key"], f"{item.get('size', 0):>10}", item.get("last_modified", ""),
                     *(f"{key}={noted[key]}" for key in ("tool", "model", "read") if key in noted)])


def shown(value: Any) -> str:
    """`value`, from the store, as it is safe to print: anything but
    printable ASCII escaped, so nothing a sender wrote reaches the terminal
    as a control sequence."""
    return "".join(c if " " <= c <= "~" else f"\\x{ord(c):02x}" if ord(c) < 256 else f"\\u{ord(c):04x}"
                   for c in str(value))


def account() -> str:
    """The id of the account the zone is in."""
    zones = cs.api(f"/zones?name={cs.ZONE}")
    if not zones:
        raise cs.SettingsError(f"the token cannot see the zone {cs.ZONE}")
    return str(zones[0]["account"]["id"])


def query(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    """The rows `sql` gives in the API's database."""
    path = f"/accounts/{account()}/d1/database/{cs.bound_database_id()}/query"
    answer = cs.api(path, "POST", {"sql": sql, "params": params or []})
    return list((answer or [{}])[0].get("results") or [])


REVIEW_STATES = {"approve": "approved", "reject": "rejected"}
# The count of the bytes of readouts waiting for review (website/api/pending.js).
PENDING = "readouts:pending"


def review(product: str | None) -> int:
    """The readouts waiting for review, by key: each odd one marked."""
    rows = query("SELECT id, key, stage, fingerprint, object, received, checked, offer, offered FROM readouts"
                 " WHERE review = 'pending'"
                 + (" AND key LIKE ?1" if product else "") + " ORDER BY key, stage, received",
                 [f"{product.upper()}|%"] if product else [])
    keys: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        first = row["stage"] == "first"
        keys.setdefault((row["key"], f"first, offered {row['offered']}" if first else row["stage"],
                         row["offer"] if first else ""), []).append(row)
    for (key, stage, _), held in keys.items():
        print(f"{shown(key)} ({shown(stage)}): {len(held)} waiting")
        prints = [row["fingerprint"] for row in held]
        for row in held:
            odd = len(held) > 1 and prints.count(row["fingerprint"]) == 1 and len(set(prints)) > 1
            print(f"    {shown(row['id'])}  {shown(row['received'])}  {shown(row['object'])}  "
                  f"{shown(row['fingerprint'][:12])}" + ("  ODD" if odd else "")
                  + ("  DISAGREES" if row["checked"] else ""))
    print(f"{len(rows)} waiting for review")
    return 0


def offers(product: str | None) -> int:
    """What eWeLink offers each key, as readouts agreed on it."""
    rows = query("SELECT key, offered, day FROM agreed_offers" + (" WHERE key LIKE ?1" if product else "")
                 + " ORDER BY key, day", [f"{product.upper()}|%"] if product else [])
    for row in rows:
        print(f"{shown(row['key'])}  offered {shown(row['offered'])}  since {shown(row['day'])}")
    print(f"{len(rows)} offers agreed")
    return 0


def rotated(when: str) -> int:
    """The keys taken as right from readouts on both sides of `when`."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z", when):
        print(f"readouts: {when} is not a time as rotate prints it (2026-11-10T12:00:00Z)", file=sys.stderr)
        return 2
    rows = query("SELECT id, key, stage, fingerprint, object, received FROM readouts"
                 " WHERE review IN ('quorum', 'approved') ORDER BY key, stage, received", [])
    keys: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        keys.setdefault((row["key"], row["stage"]), []).append(row)
    both = {found: kept for found, kept in keys.items()
            if min(row["received"] for row in kept) < when <= max(row["received"] for row in kept)}
    for (key, stage), kept in both.items():
        print(f"{shown(key)} ({shown(stage)}): {len(kept)} taken as right, sent both sides of {when}")
        for row in kept:
            side = "before" if row["received"] < when else "after"
            print(f"    {shown(row['id'])}  {shown(row['received'])}  {shown(row['object'])}  {side}")
    print(f"{len(both)} to look at again")
    return 0


def settle(action: str, reference: str) -> int:
    """`approve` or `reject` one readout, by its reference."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}-[0-9a-f]{12}", reference):
        print(f"readouts: {reference} is not a readout's reference (2026-11-09-0123456789ab)", file=sys.stderr)
        return 2
    before = query("SELECT review, bytes FROM readouts WHERE id = ?1", [reference])
    if not before:
        print(f"readouts: no readout {reference}", file=sys.stderr)
        return 1
    done = query("UPDATE readouts SET review = ?1 WHERE id = ?2 RETURNING id, key, review",
                 [REVIEW_STATES[action], reference])
    # Settled: no longer held to the room for readouts waiting for review.
    if before[0]["review"] == "pending":
        query("UPDATE counters SET count = max(count - 1, 0), bytes = max(bytes - ?1, 0) WHERE name = ?2",
              [before[0]["bytes"], PENDING])
    print(f"{shown(done[0]['id'])} ({shown(done[0]['key'])}): {shown(done[0]['review'])}")
    return 0


# The link each email to an address carries for its owner to remove it, as
# website/api/index.js's FORGET_URL (tests/repo/test_readouts.py holds them alike).
FORGET_URL = "https://api.firmwareliberator.com/forget"
# Each confirmed address, with its devices ("MODEL:stock_model", by spaces) and
# readouts, as website/api/migrations/0001_initial.sql keeps them.
SUBSCRIBERS = """SELECT who, email, token, since,
  (SELECT group_concat(model || ':' || stock_model, ' ') FROM subscriber_devices d WHERE d.who = s.who) AS devices,
  (SELECT group_concat(readout, ' ') FROM subscriber_readouts r WHERE r.who = s.who) AS readouts
  FROM subscribers s"""


def subscriber(row: dict[str, Any]) -> str:
    return "  ".join(shown(part) for part in [row.get("email", "?"), row.get("devices") or "-",
                                              row.get("readouts") or "-", row.get("since", ""),
                                              f"{FORGET_URL}?a={row.get('who')}&t={row.get('token')}"])


# Why Email Service puts an address on the account's suppression list for
# good: its mail bounced as no such mailbox, or its owner marked one as spam.
# Either way it is never to be written to again (.github/readouts.md).
GONE = {"hard_bounce", "complaint"}
SUPPRESSIONS_PAGE = 1000


def suppressed() -> set[str]:
    """The addresses on the account's suppression list for a GONE reason,
    in lower case."""
    found: set[str] = set()
    ident = account()
    for page in range(1, 101):
        rows = cs.api(f"/accounts/{ident}/email/sending/suppressions?per_page={SUPPRESSIONS_PAGE}&page={page}") or []
        found |= {str(row.get("email") or "").lower() for row in rows if row.get("reason") in GONE}
        if len(rows) < SUPPRESSIONS_PAGE:
            break
    return found - {""}


def forget(who: str) -> None:
    """Takes an address off the list, as its own /forget link does."""
    for table in ("subscriber_devices", "subscriber_readouts", "subscribers"):
        query(f"DELETE FROM {table} WHERE who = ?1", [who])


def who_of(email: str) -> str:
    """An address's key in the database, as website/api/index.js makes it."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def subscribers(product: str | None = None) -> list[dict[str, Any]]:
    """Each confirmed address, or only `product`'s owners; any that bounced
    for good or drew a complaint taken off the list first."""
    gone = suppressed()
    if product:
        rows = query(f"{SUBSCRIBERS} WHERE who IN (SELECT who FROM subscriber_devices WHERE model = ?1) "
                     "ORDER BY since", [product.upper()])
    else:
        rows = query(f"{SUBSCRIBERS} ORDER BY since")
    kept = []
    for row in rows:
        if str(row.get("email") or "").lower() in gone:
            forget(str(row["who"]))
        else:
            kept.append(row)
    if len(kept) < len(rows):
        print(f"Removed {len(rows) - len(kept)} that bounced for good or marked an email as spam.")
    return kept


def products_of(row: dict[str, Any]) -> list[str]:
    return [device.partition(":")[0] for device in (row.get("devices") or "").split()]


def statuses(ref: str | None = None) -> dict[str, str]:
    """devices/status.json as it is, or as it was at `ref`."""
    if ref is None:
        return json.loads((ROOT / "devices/status.json").read_text(encoding="utf-8"))
    shown = subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:devices/status.json"], capture_output=True,
                           text=True, check=False)
    return json.loads(shown.stdout) if shown.returncode == 0 else {}


def latest_release() -> str:
    found = subprocess.run(["git", "-C", str(ROOT), "describe", "--tags", "--abbrev=0", "--match", "v*"],
                           capture_output=True, text=True, check=False)
    if found.returncode != 0:
        raise cs.SettingsError("no release tag to compare with: give one")
    return found.stdout.strip()


def changes(ref: str | None) -> int:
    """The devices whose status changed since `ref`, each with whom to tell."""
    from tools.autoflash import kinds
    ref = ref or latest_release()
    before, now = statuses(ref), statuses()
    changed = [kind for kind in kinds.EVERY if before.get(kind.key) != now.get(kind.key)]
    if not changed:
        print(f"No device's status changed since {ref}.")
        return 0
    given = subscribers()
    told = 0
    for kind in changed:
        print(f"{kind.name}: {before.get(kind.key, 'not listed')} -> {now.get(kind.key)}")
        theirs = [row for row in given if any(kind.product_of(product) for product in products_of(row))]
        for row in theirs:
            print("    " + subscriber(row))
        told += len(theirs)
        if not theirs:
            print("    no owner on the list")
    print(f"{len(changed)} changed since {ref}, {told} owners to tell; {len(given)} on the list for news of any "
          "device")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cs.TOKEN_VARIABLE = cs.TOKENS["readouts-review"]["variable"]
    if args[:1] in (["review"], ["offers"], ["approve"], ["reject"], ["rotated"]):
        if (args[0] in ("review", "offers") and len(args) > 2) or (
                args[0] not in ("review", "offers") and len(args) != 2):
            print(__doc__.strip(), file=sys.stderr)
            return 2
        try:
            if args[0] == "rotated":
                return rotated(args[1])
            if args[0] == "offers":
                return offers(args[1] if len(args) == 2 else None)
            return review(args[1] if len(args) == 2 else None) if args[0] == "review" else settle(*args)
        except cs.SettingsError as error:
            print(f"readouts: {error}", file=sys.stderr)
            return 1
    if args[:1] == ["changes"]:
        if len(args) > 2:
            print(__doc__.strip(), file=sys.stderr)
            return 2
        try:
            return changes(args[1] if len(args) == 2 else None)
        except cs.SettingsError as error:
            print(f"readouts: {error}", file=sys.stderr)
            return 1
    if args[:1] == ["forget"]:
        if len(args) != 2:
            print(__doc__.strip(), file=sys.stderr)
            return 2
        try:
            known = query("SELECT who FROM subscribers WHERE who = ?1", [who_of(args[1])])
            if known:
                forget(who_of(args[1]))
        except cs.SettingsError as error:
            print(f"readouts: {error}", file=sys.stderr)
            return 1
        print("Removed." if known else "Not on the list.")
        return 0
    if args[:1] == ["notify"]:
        if len(args) > 2:
            print(__doc__.strip(), file=sys.stderr)
            return 2
        try:
            rows = subscribers(args[1] if len(args) == 2 else None)
        except cs.SettingsError as error:
            print(f"readouts: {error}", file=sys.stderr)
            return 1
        for row in rows:
            print(subscriber(row))
        print(f"{len(rows)} addresses")
        return 0
    if len(args) > 1 or args[:1] in (["-h"], ["--help"]):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    prefix = args[0] if args else "readouts/"
    count = 0
    try:
        for item in objects(prefix):
            if not args and not item["key"].endswith(".tar"):
                continue   # the marks of what is held
            print(describe(item))
            count += 1
    except cs.SettingsError as error:
        print(f"readouts: {error}", file=sys.stderr)
        return 1
    print(f"{count} in {cs.READOUTS_BUCKET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
