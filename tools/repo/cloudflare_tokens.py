#!/usr/bin/env python3
"""The project's Cloudflare tokens made, or remade with new values, as TOKENS
in tools/repo/cloudflare_settings.py says.

    python3 tools/repo/cloudflare_tokens.py            every one
    python3 tools/repo/cloudflare_tokens.py KEY ...    those named, as TOKENS does

Each is made with what TOKENS allows it, or, there already, given that and
a new value, its old one then refused. The value goes straight where TOKENS
says it is kept, and nowhere else, never to the screen: a secret of the
GitHub environment (with gh, signed in), a Worker's secret, or, for a token
you use from your computer, .env.infra.

A token that can make tokens can make itself any other, so this is run with
one you make by hand, and refuses any other: one named for this, made in
the last ROTATE_MINUTES and expiring within as long, usable from this
computer's address alone (or the network it changes within, as
cloudflare_settings.from_here allows), and allowed nothing but the account's
tokens;
one so named but made otherwise it leaves, saying what to change in the
dashboard.
This says how to make it, then asks for it at a hidden prompt: it is taken
from nowhere else, so it is never written down. What else this needs - the
zone's id, the Workers' tags, the admin Worker's secret - it does with a
helper token it makes for itself (HELPER), held in memory alone, from where
its own token is allowed and for HELPER_SECONDS, and deletes the moment it
is done with it. It deletes its own token once done; should a step fail,
it keeps it, so that it can be run again with the same value until the
token expires, minutes later.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.repo import cloudflare_settings as cs  # noqa: E402
from tools.repo import github_settings  # noqa: E402

ROTATE_KEY = "tokens-rotate"
# A run takes seconds, so the token is made just before it - within
# ROTATE_MINUTES of when Cloudflare says it was issued - and expires as soon.
ROTATE_MINUTES = 10
# What it is allowed, on the account alone: each permission by the API's
# name, with the dashboard's, and why.
ROTATE_PERMISSIONS = {
    "Account API Tokens Write": ("Account - Account API Tokens: Edit",
                                 "the tokens made, remade, the helper, and itself deleted"),
}
# The helper this makes for what the rotate token is not allowed: the zone's
# id, found by its name among the account's zones (before its id is known it
# cannot be given that zone alone); the Workers' tags, which the deploy token
# names them by; and the admin Worker's secret. Its every request must come
# from the address this computer's requests reach Cloudflare from; what it
# does takes seconds.
HELPER_KEY = "tokens-helper"
HELPER_SECONDS = 60
HELPER = {"zones": ["Zone Read"], "account": ["Workers Scripts Write"]}

FAILED = (cs.SettingsError, github_settings.SettingsError)


def signed_in() -> None:
    """gh signed in, before a token is remade that only it can deliver."""
    if subprocess.run(["gh", "auth", "status"], capture_output=True, text=True).returncode:
        raise cs.SettingsError("gh is not signed in (gh auth login): the GitHub secrets could not be set")


def clock() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def when(days: float, now: dt.datetime) -> str:
    return (now + dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


own_address = cs.own_address
TRACE = cs.TRACE


def helper_body(account: str, groups: dict[tuple[str, str], str], network: str,
                now: dt.datetime) -> dict[str, Any]:
    """The helper token HELPER says, for HELPER_SECONDS, from `network` alone."""
    zones = {f"com.cloudflare.api.account.{account}": {"com.cloudflare.api.account.zone.*": "*"}}
    policies = [{"effect": "allow", "resources": zones,
                 "permission_groups": [{"id": groups[(name, "com.cloudflare.api.account.zone")]}
                                       for name in HELPER["zones"]]},
                {"effect": "allow", "resources": {f"com.cloudflare.api.account.{account}": "*"},
                 "permission_groups": [{"id": groups[(name, "com.cloudflare.api.account")]}
                                       for name in HELPER["account"]]}]
    return {"name": cs.TOKEN_PREFIX + HELPER_KEY, "policies": policies,
            "expires_on": when(HELPER_SECONDS / 86400, now),
            "condition": {"request_ip": {"in": [network]}}}


def first_answer(path: str) -> Any:
    """What a new token's first request gets: Cloudflare can take a moment
    to honour a token just made."""
    for wait in (1, 2, 4, 8, 0):
        try:
            return cs.api(path)
        except cs.SettingsError as error:
            if error.status not in (401, 403) or not wait:
                raise
            time.sleep(wait)


def body(key: str, account: str, zone_id: str, tags: dict[str, str], groups: dict[tuple[str, str], str],
         now: dt.datetime, network: str) -> dict[str, Any]:
    """What makes the token TOKENS[key] says: its policies in the API's
    form, each permission by its id for the scope it is given on."""
    spec = cs.TOKENS[key]
    scopes = {"zone": ("com.cloudflare.api.account.zone", {f"com.cloudflare.api.account.zone.{zone_id}": "*"}),
              "account": ("com.cloudflare.api.account", {f"com.cloudflare.api.account.{account}": "*"})}
    if spec.get("workers"):
        missing = sorted(cs.DEPLOY_WORKERS - set(tags))
        if missing:
            raise cs.SettingsError(f"no Worker {', '.join(missing)} yet: a token can be given a Worker only once "
                                   "it is there (website/README.md, Adding a Worker)")
        scopes["workers"] = ("com.cloudflare.edge.worker.script", {f"com.cloudflare.api.account.{account}": {
            f"com.cloudflare.edge.worker.script.{tags[name]}": "*" for name in sorted(cs.DEPLOY_WORKERS)}})
    policies = []
    for scope, (kind, resources) in scopes.items():
        if not spec.get(scope):
            continue
        try:
            ids = [{"id": groups[(name, kind)]} for name in sorted(spec[scope])]
        except KeyError as error:
            raise cs.SettingsError(f"{cs.TOKEN_PREFIX}{key}: Cloudflare has no permission {error.args[0][0]!r} "
                                   f"for {scope}") from None
        policies.append({"effect": "allow", "permission_groups": ids, "resources": resources})
    made = {"name": cs.TOKEN_PREFIX + key, "policies": policies}
    if spec.get("minutes"):
        made["expires_on"] = when(spec["minutes"] / (24 * 60), now)
    if spec.get("from_here"):
        made["condition"] = {"request_ip": {"in": [network]}}
    return made


def write_env(name: str, value: str | None) -> None:
    """`name` set to `value` in .env.infra, or with None taken out, the rest
    as it was; the file readable by its owner alone."""
    try:
        lines = cs.ENV_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    kept = [line for line in lines if not line.startswith(f"{name}=")]
    if value is not None:
        at = next((n for n, line in enumerate(lines) if line.startswith(f"{name}=")), None)
        if at is None:
            kept.append(f"{name}={value}")
        else:
            kept.insert(at, f"{name}={value}")
    elif kept == lines:
        return
    handle, temporary = tempfile.mkstemp(dir=cs.ENV_FILE.parent, prefix=".env.infra.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write("".join(f"{line}\n" for line in kept))
        os.chmod(temporary, 0o600)
        os.replace(temporary, cs.ENV_FILE)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def deliver(key: str, value: str, account: str, helper: str) -> str:
    """The new value put where TOKENS[key] says it is kept: where it went."""
    spec = cs.TOKENS[key]
    if spec["kept"] == "github":
        github_settings.set_secret(spec["variable"], value)
        return f"the GitHub environment '{github_settings.ENVIRONMENT}' secret {spec['variable']}"
    if spec["kept"] == "asked":
        write_env(spec["variable"], None)   # a copy left from when it was kept, now refused
        return f"nowhere: before each run of what uses it, in the dashboard, {cs.ready_by_hand(spec['minutes'])}"
    if spec["kept"] == "worker":
        worker, secret = spec["worker_secret"]
        with cs.using(helper):
            cs.put_secret(account, secret, value, worker)
        return f"{worker}'s secret {secret}"
    write_env(spec["variable"], value)
    return f"tools/repo/.env.infra {spec['variable']}"


def make(key: str, account: str, made: dict[str, Any], tokens: list[dict[str, Any]]) -> str:
    """The token made as `made` says, or the one of its name remade so: its
    new value."""
    path = f"/accounts/{account}/tokens"
    there = [token for token in tokens if token.get("name") == made["name"]]
    if not there:
        return cs.api(path, "POST", made)["value"]
    cs.api(f"{path}/{there[0]['id']}", "PUT", {**made, "status": "active"})
    return cs.api(f"{path}/{there[0]['id']}/value", "PUT", {})


def how_to_make_mine() -> str:
    """How to make the token this runs with, from what is written here."""
    lines = ["Make the token this runs with, in Cloudflare's dashboard (Manage Account, Account API Tokens):",
             f"  name: {cs.TOKEN_PREFIX}{ROTATE_KEY}", f"  TTL: ending within {ROTATE_MINUTES} minutes, a time set",
             *(f"  {shown}  ({why})" for shown, why in ROTATE_PERMISSIONS.values()),
             f"  Client IP Address Filtering: Is in, this computer's address alone - or, where it changes from one "
             "request to the next, as on a mobile connection, the network it changes within, no wider than "
             f"/{cs.WIDEST_FILTER[4]} (IPv6: /{cs.WIDEST_FILTER[6]})",
             f"then, within {ROTATE_MINUTES} minutes of making it, paste its value (shown once, when made) here."]
    return "\n".join(lines)


def wrongly_made(token: dict[str, Any], account: str, now: dt.datetime, address: str) -> str | None:
    """How the rotate token was made other than how_to_make_mine says, if it
    was: lasting too long, usable from elsewhere, or allowed more."""
    def at(text: str | None) -> dt.datetime | None:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")) if text else None
    issued, expires = at(token.get("issued_on")), at(token.get("expires_on"))
    if not issued or issued < now - dt.timedelta(minutes=ROTATE_MINUTES):
        return f"it was made {token.get('issued_on') or 'at no known time'}, not in the last {ROTATE_MINUTES} minutes"
    if not expires or expires > now + dt.timedelta(minutes=ROTATE_MINUTES):
        return f"it expires {token.get('expires_on') or 'never'}, not within {ROTATE_MINUTES} minutes"
    # Usable from one address, this computer's: a copy of it is no use elsewhere.
    if not cs.from_here(token, address):
        allowed = ((token.get("condition") or {}).get("request_ip") or {}).get("in") or []
        return (f"it is usable from {', '.join(allowed) or 'any address'}, not this computer's address "
                f"(which Cloudflare sees as {address}) alone, nor a network around it no wider than "
                f"/{cs.WIDEST_FILTER[ipaddress.ip_network(address, strict=False).version]}")
    given = [(sorted(str(group.get("name")) for group in policy.get("permission_groups") or []),
              policy.get("resources")) for policy in token.get("policies") or [] if policy.get("effect") == "allow"]
    if given != [(sorted(ROTATE_PERMISSIONS), {f"com.cloudflare.api.account.{account}": "*"})]:
        return "it is allowed more than " + ", ".join(shown for shown, _ in ROTATE_PERMISSIONS.values())
    return None


def check_own(account: str, now: dt.datetime, address: str) -> tuple[str, str]:
    """The id of the token this is run with, and the network it is allowed
    from, which the tokens made are given too, once it is the one it should
    be: a SettingsError where it is not, and nothing changed. The rotate
    token made other than it should be is left, to be put right in the
    dashboard rather than made again; but for one made too long ago, which
    no edit makes fresh."""
    own = cs.api(f"/accounts/{account}/tokens/verify")
    token = cs.api(f"/accounts/{account}/tokens/{own['id']}")
    name = cs.TOKEN_PREFIX + ROTATE_KEY
    # Another token, given by mistake, is someone else's to keep.
    if token.get("name") != name:
        raise cs.SettingsError(f"the token given is {token.get('name')!r}: run this with {name} alone")
    wrong = wrongly_made(token, account, now, address)
    if wrong:
        stale = wrong.startswith("it was made")
        raise cs.SettingsError(f"{name} is not made as it should be: {wrong}. "
                               + ("Delete it, and make another as this says.\n" if stale else
                                  "Edit it in the dashboard as this says, then run this again with the same value "
                                  "- or delete it.\n")
                               + how_to_make_mine())
    return own["id"], cs.from_here(token, address)


def drop(account: str, token_id: str, key: str) -> bool:
    """A token of this script's deleted: whether it was."""
    try:
        cs.api(f"/accounts/{account}/tokens/{token_id}", "DELETE")
    except cs.SettingsError as error:
        print(f"cloudflare_tokens: {cs.TOKEN_PREFIX}{key} not deleted ({error}): delete it in the dashboard; it "
              "expires by itself in any case", file=sys.stderr)
        return False
    print(f"  {cs.TOKEN_PREFIX}{key}: deleted")
    return True


# A token's value, as Cloudflare shows it once made; its id, which the
# dashboard also shows, is 32 hex digits.
TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{37,}")


def rotate_token() -> str | None:
    """The token this runs with, asked for: None for none, or for text that
    is no token's value, said without showing it."""
    print(how_to_make_mine(), file=sys.stderr)
    given = cs.ask(f"{cs.TOKEN_PREFIX}{ROTATE_KEY}")
    if given is None:
        if not sys.stdin.isatty():
            print("cloudflare_tokens: run this in a terminal, where it can ask for the token", file=sys.stderr)
        return None
    if not TOKEN_SHAPE.fullmatch(given):
        odd = sorted({"spaces" if c.isspace() else "control characters" if not c.isprintable() else repr(c)
                      for c in given if not (c.isalnum() or c in "_-")})
        print(f"cloudflare_tokens: that is not a token's value: {len(given)} characters"
              + (f", with {', '.join(odd)}" if odd else "")
              + (" - 32 hex digits is a token's id, not its value" if re.fullmatch(r"[0-9a-f]{32}", given) else ""),
              file=sys.stderr)
        return None
    return given


def main(argv: list[str] | None = None, now: dt.datetime | None = None) -> int:
    keys = sys.argv[1:] if argv is None else argv
    if any(key not in cs.TOKENS for key in keys) or "-h" in keys or "--help" in keys:
        print(__doc__.strip() + "\n\nKEY is one of: " + ", ".join(cs.TOKENS), file=sys.stderr)
        return 2
    keys = keys or list(cs.TOKENS)
    mine = rotate_token()
    if not mine:
        return 1
    # Read once the token is given, not before: making it in the dashboard
    # takes minutes, longer than the helper's HELPER_SECONDS.
    now = now or clock()
    with cs.using(mine):
        return run(keys, now)


def run(keys: list[str], now: dt.datetime) -> int:
    """main's work, with the rotate token in use."""
    try:
        try:
            accounts = cs.api("/accounts?per_page=50") or []
        except cs.SettingsError as error:
            if error.status not in (401, 403):
                raise
            # Cloudflare answers alike for a value that is no token's and for a
            # token outside its TTL, as one whose start date was set wrong; only
            # a token used from an address it is not allowed is told apart.
            raise cs.refused_address(error) or cs.SettingsError(
                f"Cloudflare does not take that token ({error}): check its TTL in the "
                "dashboard - start and end dates both today, the end within "
                f"{ROTATE_MINUTES} minutes - and that its value, not its id, was pasted") from None
        if len(accounts) != 1:
            raise cs.SettingsError(f"the token sees {len(accounts)} accounts: give it this project's alone")
        account = accounts[0]["id"]
        try:
            address = own_address()
        except (OSError, KeyError, ValueError) as error:
            raise cs.SettingsError(f"this computer's address, from {TRACE}: {error}") from None
        own, network = check_own(account, now, address)
    except cs.SettingsError as error:
        print(f"cloudflare_tokens: {error}", file=sys.stderr)
        return 1
    left, helper, alive, failed = False, None, False, False
    # Those needing the helper first, so it is deleted as soon as can be.
    keys = sorted(keys, key=lambda key: cs.TOKENS[key]["kept"] != "worker")
    try:
        if any(cs.TOKENS[key]["kept"] == "github" for key in keys):
            signed_in()
        tokens = cs.every_page(f"/accounts/{account}/tokens") or []
        # make() remakes the first of a name, and would leave the rest usable.
        twice = sorted({name for key in keys if sum(token.get("name") == (name := cs.TOKEN_PREFIX + key)
                                                    for token in tokens) > 1})
        if twice:
            raise cs.SettingsError(f"more than one token is named {', '.join(twice)}: delete every one so named "
                                   "in the dashboard, and run this again to make it afresh")
        groups = {(group["name"], scope): group["id"]
                  for group in cs.api(f"/accounts/{account}/tokens/permission_groups")
                  for scope in group.get("scopes") or []}
        helper = cs.api(f"/accounts/{account}/tokens", "POST", helper_body(account, groups, network, now))
        alive = True
        with cs.using(helper["value"]):
            zones = first_answer(f"/zones?name={cs.ZONE}")
            if not zones:
                raise cs.SettingsError(f"the helper token cannot see the zone {cs.ZONE}")
            tags = {script["id"]: script["tag"] for script in cs.api(f"/accounts/{account}/workers/scripts") or []}
        zone_id = zones[0]["id"]
        for key in keys:
            if alive and cs.TOKENS[key]["kept"] != "worker":
                alive = False
                left |= not drop(account, helper["id"], HELPER_KEY)
            value = make(key, account, body(key, account, zone_id, tags, groups, now, network), tokens)
            print(f"  {cs.TOKEN_PREFIX}{key}: made, its value put in {deliver(key, value, account, helper['value'])}")
        for token in tokens:
            name = str(token.get("name"))
            if token.get("id") not in (own, helper["id"]) and name.startswith(cs.TOKEN_PREFIX) \
                    and name.removeprefix(cs.TOKEN_PREFIX) not in cs.TOKENS:
                print(f"  {name}: left - it is not written in TOKENS: delete it in the dashboard once nothing "
                      "uses it")
                left = True
    except FAILED as error:
        print(f"cloudflare_tokens: {error}", file=sys.stderr)
        left = failed = True
    finally:
        if alive:
            left |= not drop(account, helper["id"], HELPER_KEY)
        if not failed:
            left |= not drop(account, own, ROTATE_KEY)
    if failed:
        # It lasts minutes, from this computer's network alone, as check_own held it to.
        print(f"  {cs.TOKEN_PREFIX}{ROTATE_KEY}: kept, to run this again with the same value until it expires, "
              f"within {ROTATE_MINUTES} minutes of being made")
    return int(left)


if __name__ == "__main__":
    sys.exit(main())
