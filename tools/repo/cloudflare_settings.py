#!/usr/bin/env python3
"""The website's settings on Cloudflare, written down here with why.

    python3 tools/repo/cloudflare_settings.py          what differs from them
    python3 tools/repo/cloudflare_settings.py apply    set them
    python3 tools/repo/cloudflare_settings.py ci       what differs, and that the
                                                       token reads no more than CI's should
    python3 tools/repo/cloudflare_settings.py deploy-token
                                                       that the token reads no more than
                                                       the deploy token should
    python3 tools/repo/cloudflare_settings.py rotate NAME
                                                       a new value for the API Worker's
                                                       secret NAME, one made here
    python3 tools/repo/cloudflare_settings.py rotate emulator-stats
                                                       a new secret for the emulator
                                                       suites' service token, written here
                                                       and copied to their machines
    python3 tools/repo/cloudflare_settings.py email    the domain's mail forwarded: asks
                                                       to whom, and any addresses besides
                                                       FORWARDED

Each command is run with the token whose job it is, from the environment or
tools/repo/.env.infra (as .env.infra.example beside it shows; not .env, whose
names are the tool's): `ci` CLOUDFLARE_SETTINGS_CHECK_TOKEN, CI's, which reads;
`deploy-token` CLOUDFLARE_WORKERS_DEPLOY_TOKEN, the website workflow's; the rest
CLOUDFLARE_SETTINGS_APPLY_TOKEN, the maintainer's, which edits. TOKENS lists
what each is allowed, and tools/repo/cloudflare_tokens.py makes them; `apply`
checks each. `apply` changes only what differs. The Workers, and the domains pointing at them, are
their wrangler.jsonc's, which the website workflow deploys. website/README.md
says which token is which.

Settings are applied by hand, never by CI. They change rarely, and a token
that only reads, should CI's ever leak, cannot change the DNS or a bucket.

`ci`, as the cloudflare-settings workflow runs it, also tries reads its
token must be refused, failing where any works: other zones and accounts,
Workers, and what CI_REFUSED lists. A token given more than it should be is
found that way, by what it can see; reads alone cannot show what it can
change.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import getpass
import ipaddress
import json
import os
import secrets
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
# The maintainer's tokens and private settings for the project's services,
# apart from .env, which the tool reads and which refuses a name it does not
# know.
ENV_FILE = ROOT / "tools" / "repo" / ".env.infra"

from tools.autoflash import credentials  # noqa: E402
from tools.repo import emulator_stats  # noqa: E402

ZONE = "firmwareliberator.com"
API = "https://api.cloudflare.com/client/v4"

# Email addresses in the site's pages are hidden from scrapers, shown to a
# browser by Cloudflare's script (on by default; kept on here). It covers
# HTML Cloudflare serves, but not what a Worker's own code writes into a
# page, nor an address inside a tag's attribute other than a link's href.
# Plain HTTP is answered with a redirect to HTTPS, and TLS older than 1.2,
# which every browser of the last decade outgrew, is refused.
SETTINGS = {"always_use_https": "on", "min_tls_version": "1.2", "email_obfuscation": "on"}

# The repository's issues on GitHub (github_settings.REPOSITORY), where the
# tool sends someone whose run failed by the short address ISSUES_PATH
# (report.ISSUES_URL): a line on the screen, not a GitHub link that wraps.
ISSUES = "https://github.com/adamsthws/firmware-liberator/issues"
ISSUES_PATH = "/issues"

# www.firmwareliberator.com, which wrangler.jsonc also points at the Worker,
# is sent on to firmwareliberator.com, keeping the path and query, so the
# site has one address; and its ISSUES_PATH to ISSUES, for a while (302) so
# the repository can move. Cloudflare runs these before the Worker.
REDIRECT_PHASE = "http_request_dynamic_redirect"
REDIRECTS = [{
    "ref": "www-to-apex",
    "description": f"www.{ZONE} to {ZONE}",
    "expression": f'(http.host eq "www.{ZONE}")',
    "action": "redirect",
    "action_parameters": {"from_value": {
        "status_code": 301,
        "target_url": {"expression": f'concat("https://{ZONE}", http.request.uri.path)'},
        "preserve_query_string": True,
    }},
    "enabled": True,
}, {
    "ref": "issues-to-github",
    "description": f"{ZONE}{ISSUES_PATH} to the repository's issues on GitHub",
    "expression": f'(http.host eq "{ZONE}" and http.request.uri.path in {{"{ISSUES_PATH}" "{ISSUES_PATH}/"}})',
    "action": "redirect",
    "action_parameters": {"from_value": {
        "status_code": 302,
        "target_url": {"value": ISSUES},
        "preserve_query_string": False,
    }},
    "enabled": True,
}]


# Where the API Worker keeps the readouts the tool sends
# (website/api/index.js), as its wrangler.jsonc binds it. Private: a bucket
# is made with no public access, and nothing here gives it any. Nothing in it
# expires: a readout is kept only if it adds something, and kept for good, the
# Worker's limit on the bytes of them all (TOTAL_BYTES in website/api/index.js)
# keeping its cost under a dollar a month. So it has no lifecycle rule, and one added in
# the dashboard is a difference that `apply` takes away.
READOUTS_BUCKET = "firmware-liberator-readouts"
READOUTS_LIFECYCLE = {"rules": []}


# The API Worker's database (website/api/database.js), as its wrangler.jsonc
# binds it: made here, and brought up to date by running each file in
# MIGRATIONS not yet run, in name order, recorded in d1_migrations as wrangler
# records them. Only with the maintainer's token: D1 permissions reach every
# database in the account, so CI's token is refused them (REFUSED) and `ci`
# leaves the database to the hand check and to the public snapshot's check.
D1_DATABASE = "firmware-liberator-api"
API_WRANGLER = ROOT / "website" / "api" / "wrangler.jsonc"
ADMIN_WRANGLER = ROOT / "website" / "admin" / "wrangler.jsonc"
MIGRATIONS = ROOT / "website" / "api" / "migrations"
D1_PLACEHOLDER = "00000000-0000-0000-0000-000000000000"
D1_MIGRATIONS_TABLE = ("CREATE TABLE IF NOT EXISTS d1_migrations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "name TEXT UNIQUE, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL);")


# Before the API Worker (website/api/index.js) runs at all: more than
# RATE_LIMIT_REQUESTS from one address in 10 seconds is blocked for 10 seconds,
# so a flood never reaches the Worker, where every request, even one it turns
# away, runs its code and is counted. The account is on Workers Paid: what is
# past its allowance a month is billed, not stopped, so a flood costs money;
# on the free plan, past 100,000 a day, the API (any Worker that runs code)
# would fail until the next day, while the website, static files alone,
# which are free and unlimited, would still be served.
# A real readout is one request. The free plan allows one such rule, of 10-second periods, by
# address and data centre.
API_HOST = f"api.{ZONE}"
RATE_LIMIT_PHASE = "http_ratelimit"
RATE_LIMIT_REQUESTS = 5
RATE_LIMITS = [{
    "ref": "api-flood",
    "description": f"{API_HOST}: at most {RATE_LIMIT_REQUESTS} requests in 10 seconds from one address",
    "expression": f'(http.host eq "{API_HOST}")',
    "action": "block",
    "ratelimit": {"characteristics": ["cf.colo.id", "ip.src"], "period": 10,
                  "requests_per_period": RATE_LIMIT_REQUESTS, "mitigation_timeout": 10},
    "enabled": True,
}]
# Before the API Worker too: only the tool's requests, a POST to one of its
# paths, get through; anything else is blocked at Cloudflare's edge, so it
# never counts against the account's Workers requests (the rate rule above
# says why that matters). The free plan allows five such rules.
FIREWALL_PHASE = "http_request_firewall_custom"
# The paths and the methods each takes, as the API's description gives them
# (website/api/openapi.json, which website/test/openapi.test.mjs holds the
# Worker to): a POST from the tool; for /confirm and /forget, opened from the
# link in an email, a GET too; and for /v1/notify the OPTIONS a browser asks
# before the website's sign-up page posts to it.
API_DESCRIPTION = ROOT / "website" / "api" / "openapi.json"
API_METHODS = {path: tuple(method.upper() for method in ("get", "post", "put", "patch", "delete", "options")
                           if method in item)
               for path, item in json.loads(API_DESCRIPTION.read_text(encoding="utf-8"))["paths"].items()}
API_PATHS = tuple(API_METHODS)


def _allowed(methods: tuple[str, ...]) -> str:
    """The firewall expression's term for the paths taking `methods`."""
    paths = " ".join(f'"{path}"' for path, taken in API_METHODS.items() if taken == methods)
    said = " ".join(f'"{method}"' for method in methods)
    return f"(http.request.method in {{{said}}} and http.request.uri.path in {{{paths}}})"


# Crawlers, AI ones included, may read /stats and the figures behind it:
# cached, they cost nothing. Should that change, one more rule here would
# block Cloudflare's verified AI crawlers there -
#   (cf.verified_bot_category eq "AI Crawler" and (http.request.uri.path eq "/stats"
#    or http.host eq "stats.firmwareliberator.com"))
# - once Cloudflare's documentation confirms the free plan allows that field.
FIREWALL_RULES = [{
    "ref": "api-only-the-tool",
    "description": f"{API_HOST}: only " + "; ".join(f"{'/'.join(methods)} to {', '.join(p for p, m in API_METHODS.items() if m == methods)}"
                                                    for methods in dict.fromkeys(API_METHODS.values())),
    "expression": (f'(http.host eq "{API_HOST}" and not ('
                   + " or ".join(_allowed(methods) for methods in dict.fromkeys(API_METHODS.values())) + "))"),
    "action": "block",
    "enabled": True,
}]

# The public figures the API Worker writes each night (website/api/nightly.js)
# are served from a bucket of their own, holding nothing else, at STATS_HOST
# alone: its custom domain, made here, which makes its own DNS record (left
# out of the DNS check, as the Workers' are); R2's own public address for it
# (r2.dev) stays off. Cloudflare does not cache JSON by itself, so a cache
# rule has the figures served from its cache for an hour: a page view or a
# crawl then costs no read of the bucket.
STATS_BUCKET = "firmware-liberator-stats"
STATS_HOST = f"stats.{ZONE}"
CACHE_PHASE = "http_request_cache_settings"
CACHE_RULES = [{
    "ref": "stats-cached",
    "description": f"{STATS_HOST}: served from Cloudflare's cache for an hour",
    "expression": f'(http.host eq "{STATS_HOST}")',
    "action": "set_cache_settings",
    "action_parameters": {"cache": True, "edge_ttl": {"mode": "override_origin", "default": 3600},
                          "browser_ttl": {"mode": "override_origin", "default": 3600}},
    "enabled": True,
}]

# The API Worker's secrets (website/api/wrangler.jsonc says what each is for),
# set by `apply` and checked here by name with the maintainer's token - a
# value is never read back, and CI's token is refused the Worker's settings.
# - "made": a random value `apply` makes when it is missing, and `rotate NAME`
#   replaces: nothing outside the Worker needs to know it. Rotating SENDER_KEY
#   makes every sender look new for a while: .github/readouts.md, Rotating
#   SENDER_KEY, says what that weakens, and when to.
# - "kept": one that must never change, kept by the maintainer in a
#   password manager (KEPT_IN), as no copy can be read back from the Worker:
#   `apply` asks for it at a hidden prompt when the Worker lacks it, and
#   keeps it nowhere; never made or rotated here.
# - "destination": the maintainer's address, the one the domain's mail is
#   forwarded to (below), which the API's nightly job emails when a limit was
#   reached (alerts.js): set from Email Routing's rules, and never printed.
# - "token": a Cloudflare token TOKENS lists, which tools/repo/cloudflare_tokens.py
#   makes and sets on the Worker alone, kept nowhere else.
API_WORKER = "firmware-liberator-api"
API_SECRETS = {"DEVICE_HASH_KEY_PERMANENT": "kept", "SENDER_KEY": "made", "ALERTS_RECIPIENT_EMAIL": "destination"}

# The admin view (website/admin/), behind Cloudflare Access: its Worker's
# one secret, a token that can only read the account's analytics
# (admin-dashboard-read-only in TOKENS); and the Access application in front of it,
# made here (access_differences): GitHub sign-in alone, and the maintainer
# alone let in - by ACCESS_ADMIN_EMAIL, or else the address the domain's mail
# is forwarded to, never written here or printed. Access's GitHub sign-in is a GitHub OAuth app
# of the maintainer's, whose client id and secret `apply` asks for the once it
# makes the sign-in, and keeps nowhere. Not checked by CI: its token has no reach
# into Access, and the policy holds the maintainer's address.
ADMIN_WORKER = "firmware-liberator-admin"
ADMIN_HOST = f"admin.{ZONE}"
ADMIN_SECRETS = {"ANALYTICS_TOKEN": "token"}
# Where the maintainer keeps a "kept" secret, which `apply` reminds them of
# when it asks for one.
KEPT_IN = "your password manager"
# Each Worker's secrets.
WORKER_SECRETS = {API_WORKER: API_SECRETS, ADMIN_WORKER: ADMIN_SECRETS}
ACCESS_APP = {"name": "FirmwareLiberator admin", "domain": ADMIN_HOST, "type": "self_hosted",
              "session_duration": "24h", "auto_redirect_to_identity": True, "app_launcher_visible": False}

# The emulator suites' service token (tools/repo/emulator_stats.py), with which
# they send each run's outcome to the admin view, and nothing else: an
# application of its own covers the suites' paths alone (UPLOAD_APP, more
# specific than ACCESS_APP, so Access takes it there), and its one policy
# lets in this token and no one; the view refuses the token everywhere else
# (website/admin/emulator.js). Made by `apply` (emulator_differences), never
# to expire - it can do nothing but add runs, and a token that lapsed would
# stop every machine's figures at once - and given a new secret by `rotate
# emulator-stats`, should one be seen. Either writes the id and secret to
# emulator_stats.CREDENTIALS - Access shows a secret only as it makes one -
# and copies it to each machine emulator_stats.HOSTS names; the old secret
# stops working at once.
EMULATOR_TOKEN = "emulator-stats"
# Named as the API tokens are (TOKEN_PREFIX), though Access lists it apart from them.
SERVICE_TOKEN_NAME = "firmware-liberator-" + EMULATOR_TOKEN
SERVICE_TOKEN_DURATION = "forever"
UPLOAD_APP = {"name": "FirmwareLiberator emulator runs", "domain": emulator_stats.SCOPE_URL.removeprefix("https://"),
              "type": "self_hosted", "session_duration": "24h", "app_launcher_visible": False}

# The website's visits are counted by Cloudflare Web Analytics, without
# cookies: a site of the account's, for the zone, whose beacon tag every page
# carries (website/src/layouts/Page.astro, the site's token in it - public,
# as every page shows it). Cloudflare injecting the tag itself (auto_install)
# would count each visit twice, so it is off; its one rule is Cloudflare's
# own, every host and path measured.
# A token reading these reads every account setting, so CI's is refused them,
# and only the apply token checks them.
ANALYTICS_PAGE = ROOT / "website" / "src" / "layouts" / "Page.astro"
ANALYTICS_BEACON = re.compile(r"""data-cf-beacon='\{"token": "([0-9a-f]{32})"\}'""")

# Mail to the domain is received by Cloudflare's Email Routing, which made
# and keeps its own DNS records: MX, its DKIM key and SPF. They are checked
# against the list Email Routing gives of what it needs, so its key is not
# copied here. Some of the domain's addresses ("contact", say) are forwarded
# to one destination, the maintainer's, an address the account has
# verified; mail to any other is dropped (the catch-all, as Cloudflare makes
# it, left off). Which, and to whom, is private, so kept in Email Routing's
# rules alone - never in the repository, where scrapers would find it, nor in
# what this prints, as CI's log is public. `email` asks for both and sets
# them; the checks hold the rules to their shape (email_differences), and
# take the destination from them where it is needed (the alerts address, the
# admin view's one sign-in).
EMAIL_CATCH_ALL = {"name": "", "matchers": [{"type": "all"}], "actions": [{"type": "drop"}], "enabled": False}

# The zone's DNS records beyond Email Routing's and the Workers' custom
# domains' (wrangler makes those): any other record is a difference, and
# `apply` deletes it. Each proves something, and is public anyway.
def caa(authority: str) -> dict[str, Any]:
    """A CAA record letting `authority` issue certificates for the domain,
    its subdomains and wildcards; the API takes it as data, and lists it as
    content."""
    return {"type": "CAA", "name": ZONE, "content": f'0 issue "{authority}"',
            "data": {"flags": 0, "tag": "issue", "value": authority}}


DNS_RECORDS = [
    # Only the certificate authorities Cloudflare's Universal SSL uses may
    # issue a certificate for the domain: one from any other, for someone
    # who has fooled it, is refused. Cloudflare adds its own CAs' records
    # beside these, unlisted, should it take on another.
    *(caa(authority) for authority in ("letsencrypt.org", "pki.goog", "ssl.com")),
    # DMARC: mail from the domain that passes neither SPF nor DKIM is put in
    # spam, and receivers' reports go to Cloudflare's DMARC Management
    # (dashboard, Email, DMARC Management).
    {"type": "TXT", "name": f"_dmarc.{ZONE}",
     "content": "v=DMARC1; p=quarantine; rua=mailto:06eff54c46654ef69afd474e0d4cece5@dmarc-reports.cloudflare.net"},
    # Proves to Google Search Console that the domain is this site's.
    {"type": "TXT", "name": ZONE, "content": "google-site-verification=K7JzvlKuELWLFIHNqGRPc_D_ZWduaiq_TIj-jr0yve4"},
    # Verifies the domain for adamsthws' GitHub Pages, so no one else's
    # Pages site can claim it.
    {"type": "TXT", "name": f"_github-pages-challenge-adamsthws.{ZONE}", "content": "ee553f2ec46fa330f7f3221f7399ec"},
]

# The project's Cloudflare tokens, each allowed only what its job needs, and
# made or remade from here by tools/repo/cloudflare_tokens.py, which puts
# each value where it is used, and nowhere else: a GitHub secret of the
# website environment ("github"), a Worker's secret ("worker"), or, for one
# you use from your computer, .env.infra ("env"). Each is named for its job:
# firmware-liberator-<key> in Cloudflare, and `variable` where it is kept
# under a name, which the tool that uses it reads. What each is allowed is in
# the API's names for the permissions, on the zone alone ("zone"), the account
# ("account"), or the Workers by name ("workers"), with what needs it; `apply`
# checks each token against it (token_differences). A check that comes to
# need more is a permission written down here, and the token remade.
TOKEN_PREFIX = "firmware-liberator-"
DEPLOY_WORKERS = {"firmware-liberator", "firmware-liberator-api", "firmware-liberator-admin"}
TOKENS: dict[str, dict[str, Any]] = {
    # The maintainer's, for `apply` and this script's other commands, and
    # kept nowhere: before each run you Roll it in the dashboard and give it a
    # TTL ending within `minutes` (ready_by_hand), and paste the value when
    # this asks (asked_token), as a change is applied as soon as it is made. Usable from
    # the address it was made from alone, or the network that address changes
    # within ("from_here"), and allowed no tokens' making, so it never can make
    # itself more. It edits what the check token
    # reads, and reaches what CI's is refused because each reaches the whole
    # account: its API tokens (reading one means reading every one, other
    # sites' too), every D1 database, the Workers' settings, Access, and the
    # account's settings.
    "settings-apply": {
        "variable": "CLOUDFLARE_SETTINGS_APPLY_TOKEN", "kept": "asked", "minutes": 10, "from_here": True,
        "zone": {
            "Zone Read": "the zone found by its name, and its account",
            "Zone Settings Write": "SETTINGS",
            "DNS Write": "dns_differences, dnssec_differences, and the records onboard_sending makes",
            "Dynamic URL Redirects Write": "REDIRECTS",
            "Zone WAF Write": "RATE_LIMITS and FIREWALL_RULES",
            "Cache Settings Write": "CACHE_RULES",
            "Email Routing Rules Write": "email_differences: the forwarding rules and the catch-all",
            "Workers Routes Write": "unwritten_differences: a Worker route deleted",
            # Lists the zone's rulesets, and deletes one in the transform phases. A
            # ruleset in another phase not kept here needs that phase's Write:
            # apply says so, and makes the rest.
            "Zone Transform Rules Write": "unwritten_differences: the zone's rulesets listed, a transform one deleted",
        },
        "account": {
            "Workers R2 Storage Write": "READOUTS_BUCKET and STATS_BUCKET: made, the figures' domain, no lifecycle rule",
            "D1 Write": "database_differences: the API's database made, and each migration run",
            "Email Routing Addresses Write": "email_differences: the destination address",
            "Email Sending Write": "onboard_sending: the domain onboarded for sending",
            "Account API Tokens Read": "token_differences: each token's permissions",
            "Workers Scripts Write": "token_differences: the Workers by name; secret_differences: the Workers' secrets",
            "Access: Organizations Write": "access_differences: the Zero Trust team",
            "Access: Identity Providers Write": "access_differences: the admin view's GitHub sign-in",
            "Access: Groups Write": "access_differences: as Access asks of the two above",
            "Access: Apps and Policies Write": "access_differences: the admin view's application and its one policy",
            "Access: Service Tokens Write": "emulator_differences: the emulator suites' service token, made and rotated",
            # Both: Web Analytics refuses a token with Write alone, as other
            # account settings do not.
            "Account Settings Read": "analytics_differences: the Web Analytics site",
            "Account Settings Write": "analytics_differences: the Web Analytics site",
        },
    },
    # CI's: the cloudflare-settings workflow runs `ci` and
    # tools/repo/api_health.py with it. It reads and nothing more, and `ci`
    # fails where it can read what CI_REFUSED lists.
    "settings-check-read-only": {
        "variable": "CLOUDFLARE_SETTINGS_CHECK_TOKEN", "kept": "github",
        "zone": {
            "Zone Read": "the zone found by its name, and its account",
            "Zone Settings Read": "SETTINGS",
            "DNS Read": "dns_differences and dnssec_differences",
            "Dynamic URL Redirects Read": "REDIRECTS",
            "Zone WAF Read": "RATE_LIMITS and FIREWALL_RULES",
            "Cache Settings Read": "CACHE_RULES",
            "Email Routing Rules Read": "email_differences: the forwarding rules and the catch-all",
            "Analytics Read": "api_health.py mail: what came of the emails sent",
            "Workers Routes Read": "unwritten_differences: any Worker route",
            # Cloudflare's list of a zone's rulesets takes none of the rule
            # permissions above; this read, of the zone alone, is one it does.
            "Zone Transform Rules Read": "unwritten_differences: the zone's rulesets listed",
        },
        "account": {
            "Workers R2 Storage Read": "READOUTS_BUCKET and STATS_BUCKET",
            "Account Analytics Read": "api_health.py usage: the month's use against what the plan includes",
            "Email Routing Addresses Read": "email_differences: the destination address, verified",
            "Email Sending Read": "api_health.py mail: the suppression list",
        },
    },
    # The website workflow's, deploying the Workers: the zone's Worker routes
    # and DNS records, for their custom domains, and its three Workers, by
    # name - no other Worker in the account.
    "workers-deploy": {
        "variable": "CLOUDFLARE_WORKERS_DEPLOY_TOKEN", "kept": "github",
        "zone": {
            "Zone Read": "wrangler finding the zone",
            "Workers Routes Write": "the Workers' custom domains",
            "DNS Write": "the custom domains' DNS records",
        },
        "workers": {"Individual Workers Editor": "wrangler deploy, of DEPLOY_WORKERS alone"},
    },
    # The admin view's, showing the month's use: its Worker's secret
    # ANALYTICS_TOKEN, and kept nowhere else.
    "admin-dashboard-read-only": {
        "kept": "worker", "worker_secret": (ADMIN_WORKER, "ANALYTICS_TOKEN"),
        "account": {"Account Analytics Read": "the month's use, as api_health.py usage reads it"},
    },
    # The maintainer's, for tools/repo/readouts.py: reviewing readouts in the
    # API's database, and listing the bucket they are kept in.
    "readouts-review": {
        "variable": "CLOUDFLARE_READOUTS_REVIEW_TOKEN", "kept": "env",
        "zone": {"Zone Read": "the zone found by its name, and its account"},
        "account": {
            "D1 Write": "review, approve and reject: the readouts table",
            "Workers R2 Storage Read": "list, changes and notify: the readouts bucket",
            "Email Sending Read": "changes and notify: the suppression list, to remove addresses gone for good",
        },
    },
}
DEPLOY_TOKEN = TOKEN_PREFIX + "workers-deploy"
# The token each command of this script is run with; tools that borrow api()
# set their own.
TOKEN_VARIABLE = TOKENS["settings-apply"]["variable"]

# Each zone ruleset this script keeps, by its phase: the rules as written here.
RULESETS = {REDIRECT_PHASE: ("redirects", REDIRECTS), RATE_LIMIT_PHASE: ("rate limits", RATE_LIMITS),
            FIREWALL_PHASE: ("firewall rules", FIREWALL_RULES), CACHE_PHASE: ("cache rules", CACHE_RULES)}


class SettingsError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status   # the HTTP status Cloudflare answered, if it answered


def variable(name: str) -> str:
    """`name` from the environment, or else .env.infra."""
    value = os.environ.get(name)
    if value:
        return value
    try:
        env_file = credentials.parse(ENV_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        env_file = {}
    value = env_file.get(name)
    if not value:
        raise SettingsError(f"no {name}: in the environment or tools/repo/.env.infra")
    return value


# What a terminal can wrap a paste in (bracketed paste), which a hidden
# prompt keeps.
PASTE_MARKS = ("\x1b[200~", "\x1b[201~")


def ask(what: str) -> str | None:
    """A secret typed or pasted at a hidden prompt, so it is never written
    down: None where there is no terminal to ask at, or nothing was given."""
    if not sys.stdin.isatty():
        return None
    given = getpass.getpass(f"{what}: ")
    for mark in PASTE_MARKS:
        given = given.replace(mark, "")
    return given.strip() or None


TRACE = "https://api.cloudflare.com/cdn-cgi/trace"

# How Cloudflare refuses a token used from an address its Client IP Address
# Filtering does not hold, naming the address it saw.
REFUSED_ADDRESS = re.compile(r"Cannot use the access token from location: (\S+)")


def refused_address(error: SettingsError) -> SettingsError | None:
    """What to put right, where Cloudflare refused a token for the address it
    was used from rather than for its TTL or value: None for any other
    refusal."""
    seen = REFUSED_ADDRESS.search(str(error))
    if not seen:
        return None
    return SettingsError(f"Cloudflare does not take that token from this computer's address, {seen[1]}: in the "
                         f"dashboard, give it {filter_how(seen[1])}, and run this again.", error.status)


# How wide a network a token used by hand may be allowed from, by IP version:
# this computer's address alone where it holds still, or else a network
# around it no wider than this, as a mobile connection hands out one address
# after another from its provider's pool, sometimes between two requests of
# one run. A value seen by someone else is then of use only from there, and
# only for the token's minutes.
WIDEST_FILTER = {4: 24, 6: 64}


def filter_how(address: str) -> str:
    """The Client IP Address Filtering to give a token used from `address`."""
    here = ipaddress.ip_network(address, strict=False)
    return (f"Client IP Address Filtering, Is in, {here.network_address} alone - or, where the address changes "
            "from one request to the next, as on a mobile connection, the network it changes within, no wider "
            f"than /{WIDEST_FILTER[here.version]}")


def from_here(token: dict[str, Any], address: str) -> str | None:
    """The one network a token's Client IP Address Filtering allows it from,
    where that holds `address` and is no wider than WIDEST_FILTER: None
    where it allows more, or elsewhere."""
    allowed = ((token.get("condition") or {}).get("request_ip") or {}).get("in") or []
    if len(allowed) != 1:
        return None
    try:
        network = ipaddress.ip_network(allowed[0], strict=False)
    except ValueError:
        return None
    here = ipaddress.ip_network(address, strict=False)
    if here.version != network.version or not here.subnet_of(network) \
            or network.prefixlen < WIDEST_FILTER[network.version]:
        return None
    return str(network)


def own_address() -> str:
    """The address this computer's requests reach Cloudflare's API from, as
    Cloudflare's trace page there says, as a network of that one address."""
    with urllib.request.urlopen(TRACE, timeout=30) as answer:
        lines = dict(line.split("=", 1) for line in answer.read().decode().splitlines() if "=" in line)
    return str(ipaddress.ip_network(ipaddress.ip_address(lines["ip"])))


def ready_by_hand(minutes: int) -> str:
    """How a token kept nowhere is readied for a run, in the dashboard (Manage
    Account, Account API Tokens). Roll comes first: Cloudflare will not
    change an expired token's TTL, answering as if it did not exist, until
    it is rolled."""
    return (f"Roll it, then give it a TTL ending within {minutes} minutes (an expired token's TTL cannot be "
            "changed until it is rolled), and paste the value Roll showed")


def asked_token(now: dt.datetime | None = None) -> str | None:
    """The settings-apply token, asked for, so it is never written down, and
    taken only as TOKENS has it: rolled for this run, with a TTL ending
    within its minutes, usable from this computer's address alone or the
    network it changes within (from_here). None,
    said why, where it is not; a token outside its TTL Cloudflare refuses
    as it does a value that is no token's."""
    key = "settings-apply"
    spec, name = TOKENS[key], TOKEN_PREFIX + key
    print(f"Give the {name} token: in Cloudflare's dashboard (Manage Account, Account API Tokens), "
          f"{ready_by_hand(spec['minutes'])}.", file=sys.stderr)
    value = ask(name)
    if not value:
        if not sys.stdin.isatty():
            print("cloudflare_settings: run this in a terminal, where it can ask for the token", file=sys.stderr)
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    with using(value):
        try:
            try:
                zones = api(f"/zones?name={ZONE}")
            except SettingsError as error:
                if error.status not in (401, 403):
                    raise
                raise refused_address(error) or SettingsError(
                    f"Cloudflare does not take that token ({error}): check its TTL - start and end "
                    f"today, the end within {spec['minutes']} minutes - and that its value was "
                    "pasted, as Roll shows it") from None
            account = zones[0]["account"]["id"]
            token = api(f"/accounts/{account}/tokens/" + api(f"/accounts/{account}/tokens/verify")["id"])
            expires = token.get("expires_on")
            if token.get("name") != name:
                raise SettingsError(f"the token given is {token.get('name')!r}: give {name}")
            if not expires or dt.datetime.fromisoformat(expires.replace("Z", "+00:00")) \
                    > now + dt.timedelta(minutes=spec["minutes"]):
                raise SettingsError(f"{name} expires {expires or 'never'}: give it a TTL ending within "
                                    f"{spec['minutes']} minutes")
            address = own_address()
            if not from_here(token, address):
                raise SettingsError(f"{name} is usable from more than this computer's address, or not from it: "
                                    f"in the dashboard, give it {filter_how(address)}")
        except (SettingsError, OSError, KeyError, IndexError, ValueError) as error:
            print(f"cloudflare_settings: {error}", file=sys.stderr)
            return None
    return value


def token_key() -> str:
    """The key in TOKENS of the token in use."""
    return next((key for key, spec in TOKENS.items() if spec.get("variable") == TOKEN_VARIABLE), TOKEN_VARIABLE)


# A token used for a moment in place of TOKEN_VARIABLE's, and held in memory
# alone: tools/repo/cloudflare_tokens.py's short-lived helper.
_IN_USE: str | None = None


@contextlib.contextmanager
def using(value: str):
    """api() calls in this block made with the token `value`."""
    global _IN_USE
    before, _IN_USE = _IN_USE, value
    try:
        yield
    finally:
        _IN_USE = before


def token() -> str:
    return _IN_USE or variable(TOKEN_VARIABLE)


# A read whose connection is lost - dropped or timed out, as a mobile one can
# be - is sent again after each of these waits, in seconds. A change is sent
# once: it may have been made before its answer was lost.
READ_RETRY_WAITS = (1, 3)


def api(path: str, method: str = "GET", body: Any = None, ok_missing: bool = False, whole: bool = False,
        ok_forbidden: bool = False) -> Any:
    """The answer's result, or with `whole` all of it, its result_info too;
    None for a 404 with `ok_missing`, or with `ok_forbidden` a refusal: 403,
    or 401 as some products answer a token not allowed them."""
    request = urllib.request.Request(
        API + path, method=method, data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"})
    for wait in (*READ_RETRY_WAITS, None) if method == "GET" else (None,):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                answer = json.load(response)
                return answer if whole else answer["result"]
        except urllib.error.HTTPError as error:
            if (ok_missing and error.code == 404) or (ok_forbidden and error.code in (401, 403)):
                return None
            detail = error.read().decode("utf-8", "replace")
            try:
                detail = "; ".join(e.get("message", "") for e in json.loads(detail)["errors"]) or detail
            except (ValueError, KeyError, TypeError):
                pass
            raise SettingsError(f"{method} {path}: HTTP {error.code}: {detail}", error.code) from None
        except OSError as error:
            if wait is None:
                raise SettingsError(f"{method} {path}: {error}") from None
            time.sleep(wait)


def every_page(path: str, ok_forbidden: bool = False) -> list[Any] | None:
    """All of a list Cloudflare gives a page at a time; None for a refusal
    with `ok_forbidden`."""
    found: list[Any] = []
    page = 1
    while True:
        answer = api(f"{path}?per_page=50&page={page}", whole=True, ok_forbidden=ok_forbidden)
        if answer is None:
            return None
        found += answer.get("result") or []
        if page >= ((answer.get("result_info") or {}).get("total_pages") or 1):
            return found
        page += 1


def comparable(rule: dict[str, Any], written: dict[str, Any] | None = None) -> dict[str, Any]:
    """What a rule says, as `written` (REDIRECTS' first, by default) words it,
    from what Cloudflare returns: only the keys written here."""
    return {key: rule.get(key) for key in (written or REDIRECTS[0])}


Policies = list[tuple[list[str], list[str]]]


def written_policies(spec: dict[str, Any]) -> Policies:
    """What a TOKENS entry allows, as (permissions, what on) pairs in an
    order of their own: the zone by its name, the account as "account",
    Workers by theirs."""
    on = {"zone": [ZONE], "account": ["account"], "workers": sorted(DEPLOY_WORKERS)}
    return sorted((sorted(spec[scope]), on[scope]) for scope in on if spec.get(scope))


def live_policies(token: dict[str, Any], zone_id: str, workers: dict[str, str]) -> Policies:
    """What a token Cloudflare lists allows, as written_policies words it;
    `workers` names each Worker by its resource."""
    zone = f"com.cloudflare.api.account.zone.{zone_id}"
    policies = []
    for policy in token.get("policies") or []:
        on = set()
        for key, value in (policy.get("resources") or {}).items():
            if isinstance(value, dict):   # an account's resources naming its Workers inside it
                on.update(workers.get(inner, inner) for inner in value)
            else:
                on.add(ZONE if key == zone else "account" if key.startswith("com.cloudflare.api.account.")
                       and ".zone." not in key else key)
        denied = "" if policy.get("effect") == "allow" else "denied: "
        policies.append((sorted(denied + str(group.get("name")) for group in policy.get("permission_groups") or []),
                         sorted(on)))
    return sorted(policies)


def say(policies: Policies) -> str:
    return "; ".join(f"{', '.join(names)} on {', '.join(on)}" for names, on in policies) or "nothing"


def token_differences(account: str, zone_id: str) -> list[tuple[str, str, Any]]:
    """Each token in TOKENS there and allowed what it says, and no other of
    the project's; unchecked where this token cannot read the account's
    tokens and Workers. One is made or changed by
    tools/repo/cloudflare_tokens.py, never by `apply`, whose token cannot
    make one."""
    tokens = every_page(f"/accounts/{account}/tokens", ok_forbidden=True)
    scripts = api(f"/accounts/{account}/workers/scripts", ok_forbidden=True)
    if tokens is None or scripts is None:
        print("  API tokens: not checked - this token cannot read the account's API tokens and Workers")
        return []
    workers = {f"com.cloudflare.edge.worker.script.{script.get('tag')}": script.get("id") for script in scripts}
    found: list[tuple[str, str, Any]] = []
    for key, spec in TOKENS.items():
        name, remake = TOKEN_PREFIX + key, f"run python3 tools/repo/cloudflare_tokens.py {key}"
        named = [token for token in tokens if token.get("name") == name]
        have = [token for token in named if token.get("status") == "active"]
        # Only the first of a name would be checked, and remade, the rest left
        # as they are, still usable.
        if len(named) > 1:
            found.append((name, f"is {len(named)} tokens of one name",
                          f"delete every one in the dashboard, then {remake}"))
        elif not have:
            found.append((name, "does not exist", remake))
        elif (policies := live_policies(have[0], zone_id, workers)) != written_policies(spec):
            found.append((name, f"allows {say(policies)}, should allow {say(written_policies(spec))}", remake))
    for token in tokens:
        name = str(token.get("name"))
        if name.startswith(TOKEN_PREFIX) and name[len(TOKEN_PREFIX):] not in TOKENS:
            found.append((name, "is not written here", "delete it in the dashboard once nothing uses it"))
    return found


# What each of CI's tokens must be refused, each a sign of permissions it was
# never meant to have. CI's read token needs none of it to check the
# settings, and each was refused it when written down, so a
# failure there is a change. Both read the zone's DNS records: CI's to check
# them, the deploy token as it edits its Workers' custom domains there. The
# deploy token edits its Workers, so it may read those too; its value lives
# only in GitHub, so its list was first checked by CI itself.
REFUSED = [
    ("the account's API tokens", "/accounts/{account}/tokens"),
    ("your user's details", "/user"),
    ("the account's members", "/accounts/{account}/members"),
    ("the account's audit logs", "/accounts/{account}/audit_logs"),
    ("the account's billing", "/accounts/{account}/billing/profile"),
    ("the account's Pages projects", "/accounts/{account}/pages/projects"),
    ("the zone's SSL certificates", "/zones/{zone}/ssl/certificate_packs"),
    ("the account's KV namespaces", "/accounts/{account}/storage/kv/namespaces"),
    ("the account's D1 databases", "/accounts/{account}/d1/database"),
    ("the account's settings (Web Analytics sites)", "/accounts/{account}/rum/site_info/list"),
]
CI_REFUSED = [*REFUSED,
              ("the API Worker's settings and bindings",
               "/accounts/{account}/workers/scripts/firmware-liberator-api/settings")]
DEPLOY_REFUSED = REFUSED


def token_reach(account: str, zone: str, workers: set[str], refused: list[tuple[str, str]]) -> list[tuple[str, str, Any]]:
    """What the token in use can see that it should not: a zone but this one,
    an account but this one, Workers other than `workers` (the list answers,
    filtered to those it may see), or anything `refused` lists."""
    found: list[tuple[str, str, Any]] = []
    others = sorted(item.get("name") for item in api("/zones?per_page=50") or [] if item.get("name") != ZONE)
    if others:
        found.append(("this token", f"can see other zones ({', '.join(others)}): give it {ZONE} alone", None))
    accounts = api("/accounts?per_page=50") or []
    if len(accounts) != 1:
        found.append(("this token", f"can see {len(accounts)} accounts: give it this one alone", None))
    seen = {script.get("id") for script in api(f"/accounts/{account}/workers/scripts", ok_forbidden=True) or []}
    if seen != workers:
        wanted = ", ".join(sorted(workers)) or "none"
        found.append(("this token", f"can see the Workers {sorted(seen)}: it should see {wanted}", None))
    for what, path in refused:
        if api(path.format(account=account, zone=zone), ok_forbidden=True) is not None:
            found.append(("this token", f"can read {what}: it should be refused", None))
    return found


def ci_token_reach(account: str, zone: str) -> list[tuple[str, str, Any]]:
    """What CI's read token can see that it should not: it needs no Worker."""
    return token_reach(account, zone, set(), CI_REFUSED)


def deploy_token_reach() -> list[tuple[str, str, Any]]:
    """What the deploy token can see that it should not: anything but its two
    Workers, this zone and this account, and what it edits."""
    zones = api(f"/zones?name={ZONE}")
    if not zones:
        raise SettingsError(f"the token cannot see the zone {ZONE}")
    return token_reach(zones[0]["account"]["id"], zones[0]["id"], DEPLOY_WORKERS, DEPLOY_REFUSED)


def text(record: dict[str, Any]) -> str:
    """A record's content as compared: a TXT record's quoted strings joined,
    as Cloudflare lists a long one split and Email Routing whole; a host
    name without its final dot."""
    content = record.get("content") or ""
    if record.get("type") == "TXT" and content.startswith('"'):
        return "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', content))
    return content.rstrip(".") if record.get("type") in ("MX", "CNAME") else content


def key(record: dict[str, Any]) -> tuple[Any, ...]:
    return record.get("type"), record.get("name"), text(record), record.get("priority")


def said(record: dict[str, Any]) -> str:
    return f"{record['type']} {record['name']} {text(record)!r}"


def dnssec_differences(zone: str) -> list[tuple[str, str, Any]]:
    """DNSSEC on: answers signed, so a resolver can tell them from forged
    ones. Cloudflare is the registrar, and puts the DS record in .com itself
    once it is turned on: pending until then."""
    status = api(f"{zone}/dnssec").get("status")
    if status == "active":
        return []
    if status == "pending":
        return [("DNSSEC", "is pending", "wait: Cloudflare, the registrar, adds the DS record to .com itself")]
    return [("DNSSEC", f"is {status}", ("PATCH", f"{zone}/dnssec", {"status": "active"}))]


def dns_differences(zone: str) -> list[tuple[str, str, Any]]:
    """The zone's records against DNS_RECORDS and what Email Routing needs,
    the Workers' custom domains aside."""
    answer = api(f"{zone}/dns_records?per_page=500", whole=True)
    have = [r for r in answer["result"] if not (r.get("meta") or {}).get("origin_worker_id")
            and r.get("name") != STATS_HOST]
    if answer["result_info"]["total_count"] > len(answer["result"]):
        raise SettingsError("the zone has more DNS records than one page lists")
    wanted = DNS_RECORDS + (api(f"{zone}/email/routing/dns") or []) + sending_records(zone)
    found = []
    for record in wanted:
        if not any(key(r) == key(record) for r in have):
            body = {**record, "content": text(record), "ttl": 1, "proxied": False}
            if "data" in record:
                del body["content"]
            found.append((said(record), "is missing", ("POST", f"{zone}/dns_records", body)))
    for record in have:
        if not any(key(r) == key(record) for r in wanted):
            found.append((said(record), "is not written down", ("DELETE", f"{zone}/dns_records/{record['id']}", None)))
    return found


# The domain the API Worker's email is sent from (Cloudflare Email Service,
# its EMAIL binding: the sign-ups' confirmations and the nightly alerts),
# onboarded for sending as the zone itself, so mail comes from
# updates@firmwareliberator.com. Onboarding makes Cloudflare want its own DNS
# records - a bounce subdomain's MX, SPF and DKIM - which the DNS check then
# expects as it does Email Routing's; but for its DMARC record: DNS_RECORDS
# has the zone's one, and a second would void both.
SENDING_DOMAIN = ZONE


def sending_domains(zone: str) -> list[dict[str, Any]]:
    """The zone's domains onboarded for Email Service's sending."""
    return api(f"{zone}/email/sending/subdomains") or []


def sending_records(zone: str, domains: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The DNS records Email Service wants for each domain onboarded, as it
    lists them, but its DMARC one: the zone has its own (DNS_RECORDS)."""
    records = []
    for domain in sending_domains(zone) if domains is None else domains:
        ident = domain.get("id") or domain.get("tag")
        for record in api(f"{zone}/email/sending/subdomains/{ident}/dns") or []:
            if not str(record.get("name", "")).startswith("_dmarc."):
                records.append({field: record[field] for field in ("type", "name", "content", "priority")
                                if record.get(field) is not None})
    return records


def onboard_sending(zone: str) -> list[str]:
    """Onboard SENDING_DOMAIN for sending, and make the DNS records it then
    wants that the zone lacks; what is left to do by hand, if anything."""
    made = api(f"{zone}/email/sending/subdomains", "POST", {"name": SENDING_DOMAIN})
    have = [key(r) for r in api(f"{zone}/dns_records?per_page=500", whole=True)["result"]]
    for record in sending_records(zone, [made]):
        if key(record) not in have:
            api(f"{zone}/dns_records", "POST", {**record, "content": text(record), "ttl": 1, "proxied": False})
    return []


def sending_differences(zone: str) -> list[tuple[str, str, Any]]:
    """SENDING_DOMAIN onboarded for Email Service's sending, and enabled."""
    found = [domain for domain in sending_domains(zone) if domain.get("name") == SENDING_DOMAIN]
    if not found:
        return [(f"Email Sending for {SENDING_DOMAIN}", "is not onboarded", lambda: onboard_sending(zone))]
    if not found[0].get("enabled"):
        return [(f"Email Sending for {SENDING_DOMAIN}", "is onboarded but not enabled",
                 "enable it in the dashboard: Compute, Email Service, Email Sending")]
    return []


FORWARD_NAME = re.compile(r"[a-z0-9][a-z0-9._+-]*")
# The domain's addresses the site publishes (docs/privacy.md's, say), by the
# part before the @: each forwarded to where the others are, which `apply`
# sees to. Any others are set by `email`, and never written here.
FORWARDED = ("contact", "privacy")


def email_rules(destination: str, names: list[str]) -> list[dict[str, Any]]:
    """A rule forwarding each of the domain's addresses `names` names, by the
    part before the @, to `destination`."""
    return [{"name": "", "matchers": [{"type": "literal", "field": "to", "value": f"{name}@{ZONE}"}],
             "actions": [{"type": "forward", "value": [destination]}], "enabled": True} for name in sorted(names)]


def forwarding_rules(zone: str) -> list[dict[str, Any]]:
    """Email Routing's rules, the catch-all aside."""
    return [rule for rule in api(f"{zone}/email/routing/rules?per_page=50") or []
            if rule.get("matchers") != EMAIL_CATCH_ALL["matchers"]]


def forwarded_to(rule: dict[str, Any]) -> str | None:
    """The one address a rule forwards one of the domain's addresses to, if
    it is such a rule, as `email` makes them: else None."""
    matchers, actions = rule.get("matchers") or [], rule.get("actions") or []
    if not rule.get("enabled") or len(matchers) != 1 or len(actions) != 1:
        return None
    to, action = matchers[0], actions[0]
    if (to.get("type"), to.get("field")) != ("literal", "to") or action.get("type") != "forward" \
            or len(action.get("value") or []) != 1:
        return None
    name, _, domain = str(to.get("value", "")).lower().partition("@")
    if domain != ZONE or not FORWARD_NAME.fullmatch(name):
        return None
    return str(action["value"][0]).strip().lower()


def destination(zone: str) -> str | None:
    """The address the domain's mail is forwarded to, as its rules say: None
    while there is none, or more than one."""
    found = {forwarded_to(rule) for rule in forwarding_rules(zone)}
    return found.pop() if len(found) == 1 and None not in found else None


def email_differences(zone: str, account: str) -> list[tuple[str, str, Any]]:
    """Email Routing: on, each rule forwarding one of the domain's addresses
    to one destination, the same for all, which the account has verified,
    each of FORWARDED among them, and its catch-all as written. To whom, and
    any other addresses, `email` sets and asks for; here only their shape is
    held, and no address but FORWARDED's is printed."""
    found: list[tuple[str, str, Any]] = []
    if not api(f"{zone}/email/routing").get("enabled"):
        found.append(("Email Routing", "is off", ("POST", f"{zone}/email/routing/enable", {})))
    rules = forwarding_rules(zone)
    if not rules:
        found.append(("email forwarding", "is not set",
                      "run python3 tools/repo/cloudflare_settings.py email in a terminal, which asks for it"))
    for n, rule in enumerate(rules, 1):
        if forwarded_to(rule) is None:
            found.append((f"email rule {n}", "does not forward one of the domain's addresses to one address",
                          "set the forwarding again: python3 tools/repo/cloudflare_settings.py email"))
    to = {forwarded_to(rule) for rule in rules} - {None}
    if len(to) > 1:
        found.append(("email rules", f"forward to {len(to)} addresses: there should be one",
                      "set the forwarding again: python3 tools/repo/cloudflare_settings.py email"))
    named = {str(rule["matchers"][0].get("value", "")).lower().partition("@")[0] for rule in rules
             if forwarded_to(rule) is not None}
    for name in FORWARDED:
        if rules and name not in named:
            found.append((f"{name}@{ZONE}", "is not forwarded",
                          ("POST", f"{zone}/email/routing/rules", email_rules(next(iter(to)), [name])[0])
                          if len(to) == 1 else "set the forwarding again: python3 tools/repo/cloudflare_settings.py "
                          "email"))
    fields = ("name", "matchers", "actions", "enabled")
    catch_all = api(f"{zone}/email/routing/rules/catch_all")
    if {field: catch_all.get(field) for field in fields} != EMAIL_CATCH_ALL:
        found.append(("email catch-all", f"{'forwards' if catch_all.get('enabled') else 'is off'} as written "
                      "nowhere", ("PUT", f"{zone}/email/routing/rules/catch_all", EMAIL_CATCH_ALL)))
    path = f"/accounts/{account}/email/routing/addresses"
    have = {item.get("email", "").lower(): item for item in api(f"{path}?per_page=50") or []}
    if len(to) == 1:
        want = next(iter(to))
        if want not in have:
            found.append(("the address mail is forwarded to", "is not a destination address of the account",
                          ("POST", path, {"email": want})))
        elif not have[want].get("verified"):
            found.append(("the address mail is forwarded to", "is not verified: follow the link Cloudflare "
                          "mailed it", None))
    others = len(have) - len(to & set(have))
    if others:
        found.append(("destination addresses", f"{others} besides the one mail is forwarded to", None))
    return found


def set_email() -> int:
    """`email`: the domain's mail forwarded - to whom, and which of its
    addresses besides FORWARDED - as asked for at a hidden prompt, and kept by
    Email Routing alone; the API's alerts address set to match."""
    if not sys.stdin.isatty():
        print("cloudflare_settings: run email in a terminal, where it can ask", file=sys.stderr)
        return 2
    to = (ask("the address the domain's mail is forwarded to") or "").lower()
    names = sorted({*FORWARDED, *(name.strip().lower() for name in (ask(
        f"any other addresses of {ZONE} forwarded there besides {', '.join(FORWARDED)}, by the part before the @, "
        "separated by commas (Enter for none)") or "").split(",") if name.strip())})
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to) \
            or not all(FORWARD_NAME.fullmatch(name) for name in names):
        print("cloudflare_settings: that is not an address and a list of names: nothing changed", file=sys.stderr)
        return 1
    try:
        zones = api(f"/zones?name={ZONE}")
        if not zones:
            raise SettingsError(f"the token cannot see the zone {ZONE}")
        zone, account = f"/zones/{zones[0]['id']}", zones[0]["account"]["id"]
        if not api(f"{zone}/email/routing").get("enabled"):
            api(f"{zone}/email/routing/enable", "POST", {})
        path = f"/accounts/{account}/email/routing/addresses"
        if to not in {item.get("email", "").lower() for item in api(f"{path}?per_page=50") or []}:
            api(path, "POST", {"email": to})
            print("  the address mail is forwarded to: added; follow the link Cloudflare mails it")
        for rule in forwarding_rules(zone):
            api(f"{zone}/email/routing/rules/{rule['id']}", "DELETE")
        for rule in email_rules(to, names):
            api(f"{zone}/email/routing/rules", "POST", rule)
        api(f"{zone}/email/routing/rules/catch_all", "PUT", EMAIL_CATCH_ALL)
        put_secret(account, "ALERTS_RECIPIENT_EMAIL", to)
    except SettingsError as error:
        print(f"cloudflare_settings: {error}", file=sys.stderr)
        return 1
    print(f"  email forwarding: set, {len(names)} of the domain's addresses to the one given; the API's alerts "
          "go there too. Run apply to let that address, too, into the admin view.")
    return 0


def jsonc(path: Path) -> Any:
    """A wrangler.jsonc: JSON with whole-line // comments, as these are written."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return json.loads("\n".join(line for line in lines if not line.lstrip().startswith("//")))


# The values of the project's own that live in its files and that Cloudflare
# gives - the database's id, the Access applications' audience tags, the team,
# the account, Web Analytics' site token - none of them a secret (each is
# committed, and no use without a token for the account): `apply` writes each
# into its file itself, the rest of the file as it was, and commits them as
# the run ends (commit_written), so none is left uncommitted - but only a file
# that had no changes before it was written, as another session may be
# editing it in the same checkout: one that had is written and left to you.
# The files written this run, as they are said: what was written in each, and
# whether the file had no changes before.
WRITTEN: dict[str, dict[str, Any]] = {}
LEAK_ALLOW = ROOT / "tools" / "repo" / "leak-scan-allow"


def unchanged(path: Path) -> bool:
    """Whether git holds `path` as it is: no change to it, staged or not."""
    try:
        done = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--", str(path)],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and not done.stdout.strip()


def writing(path: Path, what: str) -> None:
    """`path` about to be written with `what`: noted, and whether it had no
    changes before, the first time it is written this run."""
    said = str(path.relative_to(ROOT))
    if said not in WRITTEN:
        WRITTEN[said] = {"what": [], "clean": unchanged(path)}
    if what not in WRITTEN[said]["what"]:
        WRITTEN[said]["what"].append(what)


def commit_written() -> list[str]:
    """The files written this run committed, those that had no changes before
    alone (`git commit --only`, leaving whatever else is staged), signed and
    through the hooks as any commit is: a line for each file left to commit
    by hand, and why."""
    clean = [said for said, written in WRITTEN.items() if written["clean"]]
    dirty = [said for said, written in WRITTEN.items() if not written["clean"]]
    left = []
    if clean:
        message = ("Cloudflare settings: the values apply was given, written where they are kept\n\n"
                   + "".join(f"- {said}: {', '.join(WRITTEN[said]['what'])}\n" for said in clean))
        try:
            done = subprocess.run(["git", "-C", str(ROOT), "commit", "-q", "--only", "-F", "-", "--", *clean],
                                  input=message, capture_output=True, text=True, timeout=300)
            problem = None if done.returncode == 0 else (done.stderr.strip() or done.stdout.strip()).splitlines()[-1:]
        except (OSError, subprocess.SubprocessError) as error:
            problem = [str(error)]
        if problem is None:
            print(f"  Committed: {', '.join(clean)} - the website workflow deploys them once pushed")
        else:
            left.append(f"Written, not committed ({' '.join(problem) or 'git refused'}): {', '.join(clean)} - "
                        "commit them")
    if dirty:
        left.append(f"Written, not committed, as each had changes already: {', '.join(dirty)} - commit the "
                    "values with them")
    WRITTEN.clear()
    return left


def rewrite(path: Path, pattern: str, value: str, what: str) -> None:
    """The one value `pattern` (a regex whose one group is the value) finds in
    `path` set to `value` - `what` it is - nothing else of the file changed."""
    text = path.read_text(encoding="utf-8")
    found = list(re.finditer(pattern, text))
    if len(found) != 1:
        raise SettingsError(f"{path.relative_to(ROOT)} should hold {pattern} once, to write it")
    start, end = found[0].span(1)
    if text[start:end] != value:
        writing(path, what)
        path.write_text(text[:start] + value + text[end:], encoding="utf-8")


def set_admin_var(name: str, value: str) -> None:
    """One of the admin Worker's vars in its wrangler.jsonc."""
    rewrite(ADMIN_WRANGLER, rf'"{re.escape(name)}":\s*"([^"]*)"', value, name)


def set_database_id(uuid: str) -> None:
    """The API's database id, in both Workers' wrangler.jsonc and in the leak
    scan's allow list (it takes any UUID for a device's): the old id there
    replaced, else the new one added."""
    old = {bound_database_id(config) for config in (API_WRANGLER, ADMIN_WRANGLER)}
    for config in (API_WRANGLER, ADMIN_WRANGLER):
        rewrite(config, r'"database_id":\s*"([^"]*)"', uuid, "the database's id")
    lines = LEAK_ALLOW.read_text(encoding="utf-8").splitlines()
    if uuid not in lines:
        at = next((n for n, line in enumerate(lines) if line in old and line), None)
        if at is None:
            lines.append(uuid)
        else:
            lines[at] = uuid
        writing(LEAK_ALLOW, "the database's id")
        LEAK_ALLOW.write_text("\n".join(lines) + "\n", encoding="utf-8")


def set_beacon_token(token: str) -> None:
    """Web Analytics' site token, in the pages' beacon tag."""
    rewrite(ANALYTICS_PAGE, ANALYTICS_BEACON.pattern, token, "Web Analytics' site token")


# Nothing of the project's is there unseen: a zone ruleset in a phase RULESETS
# does not keep, a Worker route (the Workers answer on custom domains alone), a
# custom domain no wrangler.jsonc names, or a Worker or bucket by the project's
# name nothing here knows. The account holds other projects' too, so only this
# zone's and the project's names are looked at; Cloudflare's own managed
# rulesets (kind "managed") are its, never a difference. A Worker or bucket
# is left to be deleted by hand, as deleting one takes its code or what it
# holds with it; a rule, route or domain `apply` deletes.
PROJECT = "firmware-liberator"
WRANGLER_CONFIGS = (ROOT / "website" / "wrangler.jsonc", API_WRANGLER, ADMIN_WRANGLER)


def custom_domains() -> set[tuple[str, str]]:
    """(hostname, Worker) of each custom domain the wrangler.jsonc files name."""
    return {(route["pattern"], config["name"]) for config in map(jsonc, WRANGLER_CONFIGS)
            for route in config.get("routes") or [] if route.get("custom_domain")}


def unwritten_differences(zone: str, account: str, ci: bool = False) -> list[tuple[str, str, Any]]:
    """What is there of the project's that nothing here says should be."""
    found: list[tuple[str, str, Any]] = []

    def refused(what: str, permission: str) -> None:
        # A check that cannot run is a difference, never a pass: the token is
        # given what TOKENS lists.
        found.append((what, "cannot be checked: this token is refused them",
                      f"give it {permission} (TOKENS), and remake it: python3 tools/repo/cloudflare_tokens.py "
                      f"{token_key()}"))

    rulesets = api(f"{zone}/rulesets", ok_forbidden=True)
    if rulesets is None:
        refused("zone rulesets", "Zone Transform Rules Read, or Write")
    for ruleset in rulesets or []:
        if ruleset.get("kind") == "zone" and ruleset.get("phase") not in RULESETS:
            found.append((f"zone ruleset {ruleset.get('phase')}", "is not written here",
                          ("DELETE", f"{zone}/rulesets/{ruleset['id']}", None)))
    routes = api(f"{zone}/workers/routes", ok_forbidden=True)
    if routes is None:
        refused("Worker routes", "Workers Routes Read, or Write")
    for route in routes or []:
        found.append((f"Worker route {route.get('pattern')}", "is not written here: the Workers answer on their "
                      "custom domains alone", ("DELETE", f"{zone}/workers/routes/{route['id']}", None)))
    buckets = api(f"/accounts/{account}/r2/buckets", ok_forbidden=True)
    if buckets is None:
        refused("R2 buckets", "Workers R2 Storage Read, or Write")
    for name in sorted(bucket.get("name", "") for bucket in (buckets or {}).get("buckets") or []):
        if name.startswith(PROJECT) and name not in (READOUTS_BUCKET, STATS_BUCKET):
            found.append((f"R2 bucket {name}", "is not written here",
                          "empty and delete it in the dashboard, once nothing in it is needed"))
    if ci:
        print("  Workers and their custom domains: not checked - CI's token is refused the Workers")
        return found
    for script in api(f"/accounts/{account}/workers/scripts") or []:
        name = script.get("id") or ""
        if name.startswith(PROJECT) and name not in DEPLOY_WORKERS:
            found.append((f"Worker {name}", "is not written here: no wrangler.jsonc deploys it",
                          f"delete it (npx wrangler delete --name {name}) once it is not needed"))
    wanted = custom_domains()
    for domain in api(f"/accounts/{account}/workers/domains?zone_name={ZONE}") or []:
        if (domain.get("hostname"), domain.get("service")) not in wanted:
            found.append((f"Worker custom domain {domain.get('hostname')}",
                          f"points at {domain.get('service')}, which no wrangler.jsonc says",
                          ("DELETE", f"/accounts/{account}/workers/domains/{domain['id']}", None)))
    return found


def bound_database_id(config: Path = API_WRANGLER) -> str:
    """The database id a Worker's wrangler.jsonc (the API's, or the admin
    view's, which reads the same database) binds as DB."""
    bound = [db for db in jsonc(config).get("d1_databases") or [] if db.get("binding") == "DB"]
    if len(bound) != 1 or bound[0].get("database_name") != D1_DATABASE:
        raise SettingsError(f"{config.relative_to(ROOT)} should bind {D1_DATABASE} as DB, once")
    return str(bound[0].get("database_id") or "")


def migration_files() -> list[str]:
    """The migrations, in the order they run. A name goes into the SQL that
    records it, so only the shape website/test/database.test.mjs holds them to."""
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    for name in names:
        if not re.fullmatch(r"\d{4}_[a-z0-9_]+\.sql", name):
            raise SettingsError(f"{name}: a migration is named NNNN_words.sql")
    return names


def applied_migrations(query: str) -> list[str]:
    """The migrations d1_migrations records, in the order they were run: none
    for a database never migrated, which has no such table."""
    try:
        answer = api(query, "POST", {"sql": "SELECT name FROM d1_migrations ORDER BY id"})
    except SettingsError as error:
        if "no such table" in str(error):
            return []
        raise
    return [row["name"] for row in answer[0]["results"]]


def database_differences(account: str) -> list[tuple[str, str, Any]]:
    """The API's database: there, bound by its id, and every migration run, in order."""
    path = f"/accounts/{account}/d1/database"
    found_db = [db for db in api(f"{path}?name={D1_DATABASE}") or [] if db.get("name") == D1_DATABASE]
    if not found_db:
        return [(f"D1 {D1_DATABASE}", "does not exist", lambda: create_database(account))]
    uuid = found_db[0]["uuid"]
    found: list[tuple[str, str, Any]] = []
    for config in (API_WRANGLER, ADMIN_WRANGLER):
        bound = bound_database_id(config)
        if bound != uuid:
            found.append((str(config.relative_to(ROOT)), f"binds the database {bound}, not {uuid}",
                          lambda: set_database_id(uuid) or []))
    query = f"{path}/{uuid}/query"
    applied, files = applied_migrations(query), migration_files()
    if applied != files[:len(applied)]:
        found.append((f"D1 {D1_DATABASE}", f"has the migrations {applied}, which are not the first of {files}: "
                      "a migration was changed, removed or renamed after it was run", None))
        return found
    for name in files[len(applied):]:
        sql = (MIGRATIONS / name).read_text(encoding="utf-8")
        found.append((f"D1 {D1_DATABASE} migration {name}", "has not been run",
                      ("POST", query, {"sql": f"{D1_MIGRATIONS_TABLE}\n{sql}\n"
                                              f"INSERT INTO d1_migrations (name) VALUES ('{name}');"})))
    return found


def create_database(account: str) -> list[str]:
    """Make the API's database and run every migration in it: what is left
    for the maintainer to do, as lines to show."""
    api(f"/accounts/{account}/d1/database", "POST", {"name": D1_DATABASE})
    left = []
    for what, how, change in database_differences(account):
        if callable(change):
            left += change()
            continue
        if not isinstance(change, tuple):
            left.append(f"{what}: {how}: {change or 'change it in the dashboard'}")
            continue
        method, path, body = change
        api(path, method, body)
    return left


def stats_differences(account: str, zone_id: str) -> list[tuple[str, str, Any]]:
    """The public figures' bucket: there, at STATS_HOST, and nowhere else."""
    buckets = f"/accounts/{account}/r2/buckets"
    if api(f"{buckets}/{STATS_BUCKET}", ok_missing=True) is None:
        return [(STATS_BUCKET, "does not exist", ("POST", buckets, {"name": STATS_BUCKET})),
                (f"{STATS_BUCKET} at {STATS_HOST}", "is not set",
                 ("POST", f"{buckets}/{STATS_BUCKET}/domains/custom",
                  {"domain": STATS_HOST, "zoneId": zone_id, "enabled": True, "minTLS": "1.2"}))]
    found: list[tuple[str, str, Any]] = []
    domains = (api(f"{buckets}/{STATS_BUCKET}/domains/custom") or {}).get("domains") or []
    names = sorted(domain.get("domain") for domain in domains)
    if not any(domain.get("domain") == STATS_HOST and domain.get("enabled") for domain in domains):
        found.append((f"{STATS_BUCKET} at {STATS_HOST}", "is not set",
                      ("POST", f"{buckets}/{STATS_BUCKET}/domains/custom",
                       {"domain": STATS_HOST, "zoneId": zone_id, "enabled": True, "minTLS": "1.2"})))
    elif names != [STATS_HOST]:
        found.append((STATS_BUCKET, f"is served at {names}: it should be at {STATS_HOST} alone", None))
    if (api(f"{buckets}/{STATS_BUCKET}/domains/managed") or {}).get("enabled"):
        found.append((f"{STATS_BUCKET}'s r2.dev address", "is on: it should be off",
                      ("PUT", f"{buckets}/{STATS_BUCKET}/domains/managed", {"enabled": False})))
    return found


def beacon_token() -> str:
    """The Web Analytics site token every page's beacon tag carries."""
    found = ANALYTICS_BEACON.findall(ANALYTICS_PAGE.read_text(encoding="utf-8"))
    if len(found) != 1:
        raise SettingsError(f"{ANALYTICS_PAGE.relative_to(ROOT)} should carry Web Analytics' beacon tag, once")
    return found[0]


def analytics_differences(account: str, zone_id: str) -> list[tuple[str, str, Any]]:
    """Web Analytics: one site for the zone, the one the pages' tag names,
    which Cloudflare does not inject the tag for, measuring every path: its
    rules only the one Cloudflare starts it with, every host and path in."""
    path = f"/accounts/{account}/rum/site_info"
    sites = [site for site in api(f"{path}/list?per_page=100") or []
             if (site.get("ruleset") or {}).get("zone_tag") == zone_id]
    page = str(ANALYTICS_PAGE.relative_to(ROOT))
    if not sites:
        def create() -> list[str]:
            made = api(path, "POST", {"zone_tag": zone_id, "auto_install": False})
            set_beacon_token(made["site_token"])
            return []
        return [("Web Analytics", f"has no site for {ZONE}", create)]
    if len(sites) > 1:
        return [("Web Analytics", f"has {len(sites)} sites for {ZONE}: it should have one", None)]
    # The list leaves out a site's rules; the site itself has them.
    site, found = api(f"{path}/{sites[0]['site_tag']}"), []
    if site.get("site_token") != beacon_token():
        found.append((page, f"carries another site's token: Web Analytics' site for {ZONE} is "
                      f"{site.get('site_token')}", lambda: set_beacon_token(site["site_token"]) or []))
    if site.get("auto_install"):
        found.append(("Web Analytics' automatic setup", "is on: the pages carry the tag, so it should be off",
                      ("PUT", f"{path}/{site['site_tag']}", {"auto_install": False, "zone_tag": zone_id})))
    narrowed = [rule for rule in site.get("rules") or []
                if (rule.get("host"), rule.get("paths"), rule.get("inclusive"), rule.get("is_paused"))
                != ("*", ["*"], True, False)]
    if narrowed:
        found.append(("Web Analytics' rules", f"narrow what is measured ({[(r.get('host'), r.get('paths')) for r in narrowed]}"
                      "): there should be none but every host and path in", None))
    return found


def put_secret(account: str, name: str, value: str, worker: str = API_WORKER) -> None:
    """Set a Worker's secret `name`; its value goes in the request's body
    alone, never a command line or the screen."""
    api(f"/accounts/{account}/workers/scripts/{worker}/secrets", "PUT",
        {"name": name, "text": value, "type": "secret_text"})


def made_secret() -> str:
    """A new value for a "made" secret: 256 random bits."""
    return secrets.token_hex(32)


def secret_differences(account: str, worker: str = API_WORKER, to: str | None = None) -> list[tuple[str, str, Any]]:
    """A Worker's secrets (WORKER_SECRETS): each there, by name, and how
    `apply` sets one that is not: a "made" one made afresh, a "kept" one
    asked for, a "destination" one from `to`, the address the domain's mail
    is forwarded to; a "token" one is
    left to tools/repo/cloudflare_tokens.py."""
    wanted = WORKER_SECRETS[worker]
    path = f"/accounts/{account}/workers/scripts/{worker}/secrets"
    have = api(path, ok_missing=True)
    if have is None:
        return [(f"{worker} secrets", "cannot be checked: the Worker is not there yet",
                 "deploy it, then run this again")]
    names = {secret.get("name") for secret in have}
    found: list[tuple[str, str, Any]] = []
    for name, how in wanted.items():
        if name in names:
            continue
        if how == "made":
            found.append((f"{worker} secret {name}", "is not set",
                          lambda name=name: put_secret(account, name, made_secret(), worker) or []))
            continue
        if how == "token":
            key = next(key for key, spec in TOKENS.items() if spec.get("worker_secret") == (worker, name))
            found.append((f"{worker} secret {name}", "is not set",
                          f"make it: python3 tools/repo/cloudflare_tokens.py {key}"))
            continue
        if how == "destination":
            if not to:
                found.append((f"{worker} secret {name}", "is not set",
                              "set the domain's mail forwarding first: python3 tools/repo/cloudflare_settings.py "
                              "email"))
                continue
            found.append((f"{worker} secret {name}", "is not set",
                          lambda name=name, value=to: put_secret(account, name, value, worker) or []))
            continue
        if not sys.stdin.isatty():
            found.append((f"{worker} secret {name}", "is not set",
                          f"run apply in a terminal, which asks for its value, kept in {KEPT_IN} as "
                          f"{name} (the Worker's wrangler.jsonc says what it is)"))
            continue

        def kept(name: str = name) -> list[str]:
            print(f"  {worker} secret {name}: look for it in {KEPT_IN}, under {name}")
            value = ask(f"{worker} secret {name}")
            if not value:
                return [f"{worker} secret {name}: left - not given: run apply again"]
            put_secret(account, name, value, worker)
            return []
        found.append((f"{worker} secret {name}", "is not set", kept))
    return found


def rotate(name: str) -> int:
    """`rotate NAME`: a "made" secret replaced by a new value; or the emulator
    suites' service token given a new secret (EMULATOR_TOKEN)."""
    if name == EMULATOR_TOKEN:
        try:
            zones = api(f"/zones?name={ZONE}")
            if not zones:
                raise SettingsError(f"the token cannot see the zone {ZONE}")
            token = service_token(zones[0]["account"]["id"])
            if token is None:
                print(f"cloudflare_settings: there is no {SERVICE_TOKEN_NAME} yet: apply makes it", file=sys.stderr)
                return 1
            left = rotated(zones[0]["account"]["id"], token)
        except SettingsError as error:
            print(f"cloudflare_settings: {error}", file=sys.stderr)
            return 1
        print(f"  {SERVICE_TOKEN_NAME}: rotated")
        for line in left:
            print(f"  {line}")
        return 1 if left else 0
    if API_SECRETS.get(name) != "made":
        made = [secret for secret, how in API_SECRETS.items() if how == "made"] + [EMULATOR_TOKEN]
        print(f"cloudflare_settings: only a secret made here is rotated here: {', '.join(made)}"
              + (f" ({name} is kept, and must never change)" if name in API_SECRETS else ""), file=sys.stderr)
        return 2
    try:
        zones = api(f"/zones?name={ZONE}")
        if not zones:
            raise SettingsError(f"the token cannot see the zone {ZONE}")
        put_secret(zones[0]["account"]["id"], name, made_secret())
    except SettingsError as error:
        print(f"cloudflare_settings: {error}", file=sys.stderr)
        return 1
    print(f"  {API_WORKER} secret {name}: rotated")
    if name == "SENDER_KEY":
        at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"  Every sender looks new from {at}. Over the next weeks, look at the keys this weakened:\n"
              f"    python3 tools/repo/readouts.py rotated {at}")
    return 0


def admin_email(to: str | None) -> str:
    """The one address Access lets into the admin view: ACCESS_ADMIN_EMAIL,
    or else `to`, the one the domain's mail is forwarded to."""
    try:
        return variable("ACCESS_ADMIN_EMAIL")
    except SettingsError:
        if not to:
            raise
        return to


def github_idp(account: str) -> dict[str, Any] | None:
    """Access's GitHub sign-in, if it has one."""
    providers = api(f"/accounts/{account}/access/identity_providers") or []
    return next((idp for idp in providers if idp.get("type") == "github"), None)


def admin_app_body(idp: str) -> dict[str, Any]:
    return {**ACCESS_APP, "allowed_idps": [idp]}


def admin_policy_body(idp: str, email: str) -> dict[str, Any]:
    """Allow: the maintainer's address, signed in with GitHub; no one else."""
    return {"name": "The maintainer, by GitHub", "decision": "allow", "precedence": 1,
            "include": [{"email": {"email": email}}], "require": [{"login_method": {"id": idp}}], "exclude": []}


def policy_matches(have: dict[str, Any], want: dict[str, Any]) -> bool:
    return all((have.get(key) or []) == want[key] for key in ("include", "require", "exclude")) \
        and have.get("decision") == want["decision"]


def admin_vars() -> dict[str, Any]:
    """The admin Worker's vars, as its wrangler.jsonc has them."""
    return jsonc(ADMIN_WRANGLER).get("vars") or {}


def access_differences(account: str, to: str | None = None) -> list[tuple[str, str, Any]]:
    """Cloudflare Access in front of the admin view: its organisation (made
    in the dashboard: it names the account's team), the GitHub sign-in, the
    application at ADMIN_HOST and its one policy; and the admin Worker's
    wrangler.jsonc holding the team, the application's audience tag and the
    account, which its own check of each request needs."""
    try:
        organisation = api(f"/accounts/{account}/access/organizations", ok_missing=True)
    except SettingsError as error:
        if error.status not in (401, 403):
            raise
        # A token made before the admin view: the rest is still checked.
        return [("Access", "cannot be checked: this token is not allowed Access",
                 "remake the apply token, which TOKENS allows Access: python3 tools/repo/cloudflare_tokens.py "
                 "settings-apply, then run this again")]
    team_domain = (organisation or {}).get("auth_domain") or ""
    if not team_domain.endswith(".cloudflareaccess.com"):
        return [("Access", "has no Zero Trust organisation",
                 "in the dashboard, Zero Trust: choose a team name and the free plan, then run apply again")]
    team = team_domain.removesuffix(".cloudflareaccess.com")
    found: list[tuple[str, str, Any]] = []
    idp = github_idp(account)
    if idp is None:
        how = ("a GitHub OAuth app (github.com/settings/developers) with the homepage "
               f"https://{ADMIN_HOST} and the callback https://{team_domain}/cdn-cgi/access/callback")
        if not sys.stdin.isatty():
            return [("Access GitHub sign-in", "is not set",
                     f"make {how}, then run apply in a terminal, which asks for its client id and a new secret")]

        def sign_in() -> list[str]:
            print(f"  Access GitHub sign-in: from {how}")
            client = {"client_id": ask("its Client ID"), "client_secret": ask("a new client secret of it")}
            if not all(client.values()):
                return ["Access GitHub sign-in: left - not given: run apply again"]
            api(f"/accounts/{account}/access/identity_providers", "POST",
                {"name": "GitHub", "type": "github", "config": client})
            return []
        found.append(("Access GitHub sign-in", "is not set", sign_in))
    try:
        email = admin_email(to)
    except SettingsError:
        return found + [(f"Access for {ADMIN_HOST}", "cannot be made: no address to let in",
                         "set the domain's mail forwarding (python3 tools/repo/cloudflare_settings.py email), "
                         "or put the address in tools/repo/.env.infra as ACCESS_ADMIN_EMAIL, and run apply again")]
    apps = api(f"/accounts/{account}/access/apps") or []
    app = next((app for app in apps if app.get("domain") == ADMIN_HOST), None)
    if app is None:
        def make() -> list[str]:
            made = github_idp(account)
            if made is None:
                return ["Access GitHub sign-in: still missing, so the application was not made"]
            new = api(f"/accounts/{account}/access/apps", "POST", admin_app_body(made["id"]))
            api(f"/accounts/{account}/access/apps/{new['id']}/policies", "POST",
                admin_policy_body(made["id"], email))
            for name, value in (("ACCESS_TEAM", team), ("ACCESS_AUD", new.get("aud") or ""), ("ACCOUNT_ID", account)):
                set_admin_var(name, value)
            return []
        found.append((f"Access application {ADMIN_HOST}", "does not exist", make))
        return found
    if idp is not None:
        want = admin_app_body(idp["id"])
        off = sorted(key for key, value in want.items() if app.get(key) != value)
        if off:
            found.append((f"Access application {ADMIN_HOST}", f"differs in {', '.join(off)}",
                          ("PUT", f"/accounts/{account}/access/apps/{app['id']}", want)))
        policies = api(f"/accounts/{account}/access/apps/{app['id']}/policies") or []
        policy = admin_policy_body(idp["id"], email)
        path = f"/accounts/{account}/access/apps/{app['id']}/policies"
        if not policies:
            found.append((f"Access policy for {ADMIN_HOST}", "is not set", ("POST", path, policy)))
        elif len(policies) > 1:
            found.append((f"Access policies for {ADMIN_HOST}", f"are {len(policies)}: there should be one, "
                          "the maintainer's alone", None))
        elif not policy_matches(policies[0], policy):
            # Its rules are never printed: they hold the maintainer's address.
            found.append((f"Access policy for {ADMIN_HOST}", "lets in someone other than the maintainer, "
                          "or without GitHub", ("PUT", f"{path}/{policies[0]['id']}", policy)))
    want_vars = {"ACCESS_TEAM": team, "ACCESS_AUD": app.get("aud") or "", "ACCOUNT_ID": account}
    have_vars = admin_vars()
    for name, value in want_vars.items():
        if have_vars.get(name) != value:
            found.append((f"{ADMIN_WRANGLER.relative_to(ROOT)} {name}", f"is {have_vars.get(name)!r}, not {value!r}",
                          lambda name=name, value=value: set_admin_var(name, value) or []))
    return found


def service_token(account: str) -> dict[str, Any] | None:
    """The emulator suites' service token, as Access lists it, if made."""
    tokens = api(f"/accounts/{account}/access/service_tokens") or []
    return next((token for token in tokens if token.get("name") == SERVICE_TOKEN_NAME), None)


def keep_credentials(client_id: str, secret: str) -> list[str]:
    """The service token's id and secret written to emulator_stats.CREDENTIALS,
    set as the repository's secrets for the final-firmware check on main
    (github_settings.REPOSITORY_SECRETS), and copied to each machine
    emulator_stats.HOSTS names: a line for each place that did not take
    them, [] when all did."""
    from tools.repo import github_settings
    path = emulator_stats.write_credentials(client_id, secret)
    print(f"  {path}: written")
    left = []
    try:
        for name, value in ((emulator_stats.CLIENT_ID, client_id), (emulator_stats.CLIENT_SECRET, secret)):
            github_settings.set_secret(name, value, environment=False)
        print(f"  GitHub repository secrets {emulator_stats.CLIENT_ID} and {emulator_stats.CLIENT_SECRET}: set")
    except (OSError, github_settings.SettingsError) as error:
        left.append(f"GitHub repository secrets {emulator_stats.CLIENT_ID} and {emulator_stats.CLIENT_SECRET}: "
                    f"not set ({error}): sign gh in (gh auth login) and rotate emulator-stats again")
    machines = emulator_stats.hosts()
    if not machines:
        print("  Copy it to each machine that runs the emulator suites: python3 tools/repo/emulator_stats.py push "
              f"HOST..., or name them in tools/repo/.env.infra as {emulator_stats.HOSTS} for this to copy it")
        return left
    failed = emulator_stats.push(machines)
    copied = len(machines) - len(failed)
    print(f"  {path.name}: copied to {copied} of {len(machines)} machines")
    return left + [f"{path.name} not copied to {line}: python3 tools/repo/emulator_stats.py push copies it again"
                   for line in failed]


def rotated(account: str, token: dict[str, Any]) -> list[str]:
    """The service token given a new secret, written and copied as
    keep_credentials() does; the old secret stops working at once."""
    made = api(f"/accounts/{account}/access/service_tokens/{token['id']}/rotate", "POST", {})
    return keep_credentials(made.get("client_id") or token["client_id"], made["client_secret"])


def upload_policy_body(token_id: str) -> dict[str, Any]:
    """Service auth: the emulator suites' token; no one else."""
    return {"name": "The emulator suites' service token", "decision": "non_identity", "precedence": 1,
            "include": [{"service_token": {"token_id": token_id}}], "require": [], "exclude": []}


def emulator_differences(account: str) -> list[tuple[str, str, Any]]:
    """The emulator suites' service token (EMULATOR_TOKEN): made, never to
    expire, and the one in emulator_stats.CREDENTIALS here; its application
    over the suites' paths alone (UPLOAD_APP) and its one policy, letting in
    the token; and the admin Worker's wrangler.jsonc holding the
    application's audience tag (ACCESS_UPLOAD_AUD), which its own check of
    each upload needs."""
    name = f"Access service token {SERVICE_TOKEN_NAME}"
    try:
        token = service_token(account)
    except SettingsError as error:
        if error.status not in (401, 403):
            raise
        return [(name, "cannot be checked: this token is not allowed Access service tokens",
                 "remake the apply token, which TOKENS allows them: python3 tools/repo/cloudflare_tokens.py "
                 "settings-apply, then run this again")]
    found: list[tuple[str, str, Any]] = []
    if token is None:
        def make() -> list[str]:
            made = api(f"/accounts/{account}/access/service_tokens", "POST",
                       {"name": SERVICE_TOKEN_NAME, "duration": SERVICE_TOKEN_DURATION})
            return keep_credentials(made["client_id"], made["client_secret"])
        found.append((name, "does not exist", make))
    else:
        if token.get("duration") != SERVICE_TOKEN_DURATION:
            def forever() -> list[str]:
                path = f"/accounts/{account}/access/service_tokens/{token['id']}"
                api(path, "PUT", {"name": SERVICE_TOKEN_NAME, "duration": SERVICE_TOKEN_DURATION})
                api(f"{path}/refresh", "POST", {})   # its end moved by the duration now set
                return []
            found.append((name, f"expires ({str(token.get('expires_at'))[:10]}): it should never", forever))
        kept = emulator_stats.client()
        if kept is None or kept[0] != token.get("client_id"):
            found.append((str(emulator_stats.CREDENTIALS), "missing" if kept is None else "another token's",
                          f"python3 tools/repo/cloudflare_settings.py rotate {EMULATOR_TOKEN} writes it - Access "
                          "shows a secret only as it makes one - and copies it to the suites' machines"))
    apps = api(f"/accounts/{account}/access/apps") or []
    app = next((app for app in apps if app.get("domain") == UPLOAD_APP["domain"]), None)
    where = f"Access application {UPLOAD_APP['domain']}"
    if app is None:
        def make_app() -> list[str]:
            made = service_token(account)
            if made is None:
                return [f"{where}: not made, as the service token is still missing"]
            new = api(f"/accounts/{account}/access/apps", "POST", UPLOAD_APP)
            api(f"/accounts/{account}/access/apps/{new['id']}/policies", "POST", upload_policy_body(made["id"]))
            set_admin_var("ACCESS_UPLOAD_AUD", new.get("aud") or "")
            return []
        found.append((where, "does not exist", make_app))
        return found
    off = sorted(key for key, value in UPLOAD_APP.items() if app.get(key) != value)
    if off:
        found.append((where, f"differs in {', '.join(off)}",
                      ("PUT", f"/accounts/{account}/access/apps/{app['id']}", UPLOAD_APP)))
    if token is not None:
        path = f"/accounts/{account}/access/apps/{app['id']}/policies"
        policies = api(path) or []
        policy = upload_policy_body(token["id"])
        if not policies:
            found.append((f"Access policy for {UPLOAD_APP['domain']}", "is not set", ("POST", path, policy)))
        elif len(policies) > 1:
            found.append((f"Access policies for {UPLOAD_APP['domain']}", f"are {len(policies)}: there should be "
                          "one, the service token's alone", None))
        elif not policy_matches(policies[0], policy):
            found.append((f"Access policy for {UPLOAD_APP['domain']}", "lets in other than the service token",
                          ("PUT", f"{path}/{policies[0]['id']}", policy)))
    have = admin_vars().get("ACCESS_UPLOAD_AUD")
    if have != (app.get("aud") or ""):
        found.append((f"{ADMIN_WRANGLER.relative_to(ROOT)} ACCESS_UPLOAD_AUD", f"is {have!r}, not {app.get('aud')!r}",
                      lambda: set_admin_var("ACCESS_UPLOAD_AUD", app.get("aud") or "") or []))
    return found


def differences(ci: bool = False) -> list[tuple[str, str, Any]]:
    """(what, how it differs, the change that makes it right, as main's apply
    takes it). With `ci`, what CI's token can see that it
    should not too."""
    try:
        zones = api(f"/zones?name={ZONE}")
    except SettingsError as error:
        spec = TOKENS[token_key()] if token_key() in TOKENS else {}
        if error.status not in (401, 403) or not spec.get("minutes"):
            raise
        # Cloudflare answers so for a token past its time, or used from elsewhere.
        raise refused_address(error) or SettingsError(f"{error}: the {token_key()} token lasts {spec['minutes']} minutes, from the address it "
                            f"was made from: in the dashboard, {ready_by_hand(spec['minutes'])}", error.status) from None
    if not zones:
        raise SettingsError(f"the token cannot see the zone {ZONE}")
    zone = f"/zones/{zones[0]['id']}"
    found: list[tuple[str, str, Any]] = []
    for key, want in SETTINGS.items():
        have = api(f"{zone}/settings/{key}")["value"]
        if have != want:
            found.append((key, f"is {have}, should be {want}", ("PATCH", f"{zone}/settings/{key}", {"value": want})))
    for phase, (what, rules) in RULESETS.items():
        entrypoint = api(f"{zone}/rulesets/phases/{phase}/entrypoint", ok_missing=True)
        have = [comparable(rule, rules[0]) for rule in (entrypoint or {}).get("rules") or []]
        if have != rules:
            found.append((what, f"are {[rule['ref'] for rule in have]}, should be {[r['ref'] for r in rules]}",
                          ("PUT", f"{zone}/rulesets/phases/{phase}/entrypoint", {"rules": rules})))
    found += unwritten_differences(zone, zones[0]["account"]["id"], ci)
    account = f"/accounts/{zones[0]['account']['id']}/r2/buckets"
    lifecycle = ("PUT", f"{account}/{READOUTS_BUCKET}/lifecycle", READOUTS_LIFECYCLE)
    if api(f"{account}/{READOUTS_BUCKET}", ok_missing=True) is None:
        found.append((READOUTS_BUCKET, "does not exist", ("POST", account, {"name": READOUTS_BUCKET})))
    else:
        rules = (api(lifecycle[1], ok_missing=True) or {}).get("rules") or []
        if rules:
            found.append((f"{READOUTS_BUCKET} lifecycle", f"has {[rule.get('id') for rule in rules]}, should have "
                          "none: readouts are kept for good", lifecycle))
    found += stats_differences(zones[0]["account"]["id"], zones[0]["id"])
    if ci:
        print(f"  D1 {D1_DATABASE}: not checked - CI's token is refused D1, which reaches every database "
              "in the account; the hand check and the public snapshot's check cover it")
        print(f"  {API_WORKER} and {ADMIN_WORKER} secrets: not checked - CI's token is refused the Workers' settings")
        print(f"  Access for {ADMIN_HOST}: not checked - CI's token has no reach into Access, whose policy "
              "holds the maintainer's address")
        print("  Web Analytics: not checked - CI's token is refused it, which reads every account setting")
    else:
        found += database_differences(zones[0]["account"]["id"])
        to = destination(zone)
        found += secret_differences(zones[0]["account"]["id"], to=to)
        found += secret_differences(zones[0]["account"]["id"], ADMIN_WORKER)
        found += access_differences(zones[0]["account"]["id"], to)
        found += emulator_differences(zones[0]["account"]["id"])
        found += analytics_differences(zones[0]["account"]["id"], zones[0]["id"])
    found += email_differences(zone, zones[0]["account"]["id"])
    found += sending_differences(zone)
    found += dns_differences(zone)
    found += dnssec_differences(zone)
    if not ci:
        found += token_differences(zones[0]["account"]["id"], zones[0]["id"])
    if ci:
        found += ci_token_reach(zones[0]["account"]["id"], zones[0]["id"])
    return found


def main(argv: list[str] | None = None) -> int:
    global TOKEN_VARIABLE
    args = sys.argv[1:] if argv is None else argv
    # Each command is run with the token whose job it is.
    TOKEN_VARIABLE = TOKENS[{"ci": "settings-check-read-only", "deploy-token": "workers-deploy"}
                            .get(args[0] if args else "", "settings-apply")]["variable"]
    if not (len(args) == 2 and args[0] == "rotate") and args not in ([], ["apply"], ["ci"], ["deploy-token"], ["email"]):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    if len(args) == 2 and args[0] == "rotate" and API_SECRETS.get(args[1]) != "made" and args[1] != EMULATOR_TOKEN:
        return rotate(args[1])   # refused before a token is asked for
    if TOKENS[token_key()]["kept"] == "asked":
        value = asked_token()
        if not value:
            return 1
        with using(value):
            return run(args)
    return run(args)


def checked(args: list[str]) -> int:
    """What differs, and with apply each change made: run()'s work."""
    if len(args) == 2 and args[0] == "rotate":
        return rotate(args[1])
    if args == ["email"]:
        return set_email()
    if args == ["deploy-token"]:
        try:
            found = deploy_token_reach()
        except SettingsError as error:
            print(f"cloudflare_settings: {error}", file=sys.stderr)
            return 1
        for what, how, _ in found:
            print(f"  {what}: {how}")
        if found:
            print(f"Remake the deploy token as TOKENS says: python3 tools/repo/cloudflare_tokens.py "
                  f"{DEPLOY_TOKEN.removeprefix(TOKEN_PREFIX)}")
            return 1
        print(f"The deploy token sees {ZONE}, its Workers and no more.")
        return 0
    try:
        found = differences(ci=args == ["ci"])
        if not found:
            print(f"{ZONE}: every setting is as tools/repo/cloudflare_settings.py says.")
            return 0
        for what, how, _ in found:
            print(f"  {what}: {how}")
        if args != ["apply"]:
            print("python3 tools/repo/cloudflare_settings.py apply sets them; a token is made by "
                  "tools/repo/cloudflare_tokens.py.")
            return 1
        left = 0
        # A change is (method, path, body) to send; a function, for one made
        # of several, giving back what it left; a sentence saying what to do
        # by hand; or None, for one made in the dashboard.
        for what, _, change in found:
            if change is None or isinstance(change, str):
                print(f"  {what}: left - {change or 'change it in the dashboard'}")
                left = 1
                continue
            # One this token is refused is left, and the rest still made: a rule
            # in a phase not kept here needs that phase's Edit, which
            # TOKENS cannot know before it is there.
            try:
                if callable(change):
                    lines = change()
                    for line in lines:
                        print(f"  {line}")
                        left = 1
                    if not lines:
                        print(f"  {what}: set")
                else:
                    method, path, body = change
                    api(path, method, body)
                    print(f"  {what}: {'removed' if method == 'DELETE' else 'set'}")
            except SettingsError as error:
                if error.status not in (401, 403):
                    raise
                print(f"  {what}: left - this token is refused it ({error}): give it the permission "
                      f"(TOKENS), remake it (python3 tools/repo/cloudflare_tokens.py {token_key()}), and run "
                      "apply again")
                left = 1
    except SettingsError as error:
        print(f"cloudflare_settings: {error}", file=sys.stderr)
        return 1
    return left


def run(args: list[str]) -> int:
    """main's work, with the token for `args` in use: then whatever it wrote
    into the repository's files committed (commit_written), however it ended."""
    left: list[str] = []
    try:
        code = checked(args)
    finally:
        if WRITTEN:
            left = commit_written()
            for line in left:
                print(f"  {line}")
    return 1 if left else code


if __name__ == "__main__":
    sys.exit(main())
