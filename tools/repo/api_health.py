#!/usr/bin/env python3
"""Whether the project's API is well, as the cloudflare-settings workflow checks it daily.

    python3 tools/repo/api_health.py snapshot    the public figures are fresh, and of today's schema
    python3 tools/repo/api_health.py usage       the account is well within what its plan includes
    python3 tools/repo/api_health.py mail        the API's emails bounce rarely, and draw no complaint
    python3 tools/repo/api_health.py live [on|off]  the API just deployed answers as it should
    python3 tools/repo/api_health.py deployed    the API answering is this commit's

`snapshot` needs no token: it reads the public figures the API Worker writes
each night (website/api/nightly.js), and fails if they are missing, older than
SNAPSHOT_AGE, or written by a database short of the newest migration
(website/api/database.js) - which is how CI, whose token is refused D1,
notices a migration not yet applied.

`usage` reads the account's use from Cloudflare's analytics, with
CLOUDFLARE_SETTINGS_CHECK_TOKEN (CI's, which needs Account Analytics Read):
this month's Workers requests, D1 rows and R2 operations to yesterday, and
what D1 and R2 hold, each against what the account's plan includes a month
(INCLUDED: Workers Paid since 2026-11-10, and R2's own free allowance). It
fails at WARN_AT of any, so the maintainer hears long before a bill: past
what is included, each is billed, not stopped. Any error Cloudflare's
analytics give is said in full.

`mail` reads what came of the emails the API Worker sends (website/api/mail.js
counts only what happens as it sends): from Email Service's analytics, how
many of the last MAIL_DAYS' were delivered and how many bounced; from the
account's suppression list, the addresses added since COMPLAINT_DAYS ago for
a spam complaint or a hard bounce. It fails when the bounces pass
BOUNCE_WARN, Cloudflare's own mark for a sender's good standing (once there
are MIN_BOUNCES, so one mistyped address among few emails does not), or on
any complaint: Email Service pauses an account's sending for poor
reputation. Counts only, as CI's log is public: never an address. With
CLOUDFLARE_SETTINGS_CHECK_TOKEN, CI's, which needs the zone's Analytics Read
and the account's Email Sending Read.

`live` is the website workflow's last step of deploying the API Worker, and
needs no token. It asks the API what only a deployed, migrated Worker with
its database answers rightly: a GET of /confirm with a token that cannot be
one (the firewall's GET path, the Worker, the database's newest migration:
a page saying there is nothing to confirm), and a POST to /v1/wanted with the
light proof of work but of a format no tool sends (the firewall's POST path,
the proof checked and taken once in the database: 400). Nothing is counted
or kept but that proof's mark. Deployed taking no requests (`off`), each is
to answer 503. It tries for LIVE_WAIT, as a new version takes some seconds
to be everywhere.

`deployed` is the release workflow's: a tool is released only once the API
it talks to is the one its commit holds, deployed and answering as `live`
checks, so a tool never goes out ahead of an API it needs. Every answer the
Worker gives names, in its API-Tree header, the git tree of website/api/ the
website workflow deployed it from; the same push deploys it, so this waits
for it up to DEPLOYED_WAIT, and needs no token.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.autoflash import proof_of_work  # noqa: E402
from tools.repo import cloudflare_settings as cs  # noqa: E402

SNAPSHOT_URL = f"https://{cs.STATS_HOST}/v1/stats.json"
# The night's job runs at 03:17 UTC, and the workflow checks at 06:00: a
# night missed is then nearly 27 hours old, so more than 25 is a night
# missed, while a check run by hand at any hour sees at most about 24.
SNAPSHOT_AGE = dt.timedelta(hours=25)
DATABASE_JS = ROOT / "website" / "api" / "database.js"
INDEX_JS = ROOT / "website" / "api" / "index.js"
API_URL = f"https://{cs.API_HOST}"
LIVE_WAIT = 90
LIVE_EVERY = 10
# The website workflow's API deploy, started by the same push as the
# release, done in minutes; this long allows for its runner's wait too.
DEPLOYED_WAIT = 30 * 60
DEPLOYED_EVERY = 30
WARN_AT = 0.5
GRAPHQL = f"{cs.API}/graphql"

# What the account's plan includes, as Cloudflare's pricing pages give it
# (2026): Workers Paid's allowance a month for Workers and D1, and R2's own
# free allowance, which Workers Paid leaves as it is.
INCLUDED = {
    "Workers requests this month": 10_000_000,
    "D1 rows read this month": 25_000_000_000,
    "D1 rows written this month": 50_000_000,
    "D1 storage, bytes": 5 * 1000 ** 3,
    "R2 storage, bytes": 10 * 1000 ** 3,
    "R2 Class A operations this month": 1_000_000,
    "R2 Class B operations this month": 10_000_000,
}
# R2's operations, as its analytics name them, of Class B (reads); every
# other is of Class A, as R2's pricing counts any it does not list as free.
CLASS_B = {"GetObject", "HeadObject", "HeadBucket", "UsageSummary", "GetBucketEncryption", "GetBucketLocation",
           "GetBucketCors", "GetBucketLifecycleConfiguration"}
FREE = {"DeleteObject", "DeleteBucket", "AbortMultipartUpload"}

# Cloudflare's own marks for a sender's good standing are a hard bounce rate
# under 2% and complaints under 0.1%: among so few emails, any complaint is
# past the second.
MAIL_DAYS = 7
COMPLAINT_DAYS = 2
BOUNCE_WARN = 0.02
MIN_BOUNCES = 5
SUPPRESSIONS_PAGE = 1000


class HealthError(RuntimeError):
    pass


def latest_migration() -> str:
    found = re.search(r'^export const LATEST_MIGRATION = "([^"]+)";$', DATABASE_JS.read_text(encoding="utf-8"), re.M)
    if not found:
        raise HealthError(f"{DATABASE_JS.relative_to(ROOT)} names no LATEST_MIGRATION")
    return found.group(1)


def light_proof_bits() -> int:
    found = re.search(r"^export const LIGHT_PROOF_BITS = (\d+);$", INDEX_JS.read_text(encoding="utf-8"), re.M)
    if not found:
        raise HealthError(f"{INDEX_JS.relative_to(ROOT)} names no LIGHT_PROOF_BITS")
    return int(found.group(1))


def ask(path: str, body: bytes | None = None) -> tuple[int, str]:
    """The API's answer to a GET, or with `body` a POST of it: (status, text)."""
    headers = {"User-Agent": "firmware-liberator-health"}
    if body is not None:
        headers |= {"Content-Type": "application/json", "X-Proof": proof_of_work.solve(
            proof_of_work.body_subject(body), light_proof_bits())}
    request = urllib.request.Request(API_URL + path, data=body, method="GET" if body is None else "POST",
                                     headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")
    except OSError as error:
        return 0, str(error)


def live_problems(ingest: str) -> list[str]:
    """What the API answered wrongly, once, as deployed taking requests or not."""
    problems = []
    status, text = ask("/confirm?t=not-a-token")
    if ingest == "off":
        if status != 503:
            problems.append(f"GET /confirm: {status} {text[:200]!r}, not 503 as taking no requests")
    elif status != 404 or "Nothing to confirm" not in text:
        problems.append(f"GET /confirm: {status} {text[:200]!r}, not its page saying there is nothing to confirm")
    body = json.dumps({"format": 0, "health": "check", "nonce": secrets.token_hex(8)}).encode()
    status, text = ask("/v1/wanted", body)
    if ingest == "off":
        if status != 503:
            problems.append(f"POST /v1/wanted: {status} {text[:200]!r}, not 503 as taking no requests")
    elif status != 400 or "not a question this tool makes" not in text:
        problems.append(f"POST /v1/wanted: {status} {text[:200]!r}, not 400 for a question of no tool's format"
                        + (" (run python3 tools/repo/cloudflare_settings.py apply)" if "being updated" in text else ""))
    return problems


def live(ingest: str, wait: float = LIVE_WAIT, every: float = LIVE_EVERY) -> list[str]:
    """live_problems, asked again until none or `wait` seconds have passed."""
    started = time.monotonic()
    while True:
        problems = live_problems(ingest)
        if not problems or time.monotonic() - started + every > wait:
            return problems
        time.sleep(every)


def tree_here() -> str:
    """The git tree of website/api/ in this commit, as the website workflow
    names a deploy of it."""
    return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD:website/api"], capture_output=True, text=True,
                          check=True).stdout.strip()


def tree_deployed() -> str:
    """The tree the API answering was deployed from: its API-Tree header."""
    request = urllib.request.Request(API_URL + "/confirm?t=not-a-token",
                                     headers={"User-Agent": "firmware-liberator-health"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.headers.get("API-Tree") or ""
    except urllib.error.HTTPError as error:
        return error.headers.get("API-Tree") or ""
    except OSError:
        return ""


def deployed_problems(tree: str) -> list[str]:
    """Why the API answering is not `tree`'s, deployed and well, if it is not."""
    found = tree_deployed()
    if found != tree:
        return [f"the API answering was deployed from website/api/ {found or '(not named)'}, not this commit's "
                f"{tree}: see the website workflow's deploy-api job for this commit"]
    return live_problems("on")


def deployed(tree: str, wait: float = DEPLOYED_WAIT, every: float = DEPLOYED_EVERY) -> list[str]:
    """deployed_problems, asked again until none or `wait` seconds have passed."""
    started = time.monotonic()
    while True:
        problems = deployed_problems(tree)
        if not problems or time.monotonic() - started + every > wait:
            return problems
        time.sleep(every)


def fetch_snapshot(url: str = SNAPSHOT_URL) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "firmware-liberator-health"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except (OSError, ValueError) as error:
        raise HealthError(f"{url}: {error}") from None


def snapshot_problems(figures: dict[str, Any], now: dt.datetime) -> list[str]:
    """What is wrong with the public figures: [] when fresh and current."""
    problems = []
    if figures.get("format") != 1:
        problems.append(f"they are of format {figures.get('format')!r}, not 1")
    try:
        made = dt.datetime.strptime(str(figures.get("generated")), "%Y-%m-%dT%H:%MZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        problems.append(f"they say no time they were made ({figures.get('generated')!r})")
    else:
        if now - made > SNAPSHOT_AGE:
            problems.append(f"they were made {made:%Y-%m-%d %H:%M} UTC, over {SNAPSHOT_AGE} ago: "
                            "the nightly job has not run")
    if figures.get("schema") != latest_migration():
        problems.append(f"they were made by a database at {figures.get('schema')!r}, not {latest_migration()!r}: "
                        "run python3 tools/repo/cloudflare_settings.py apply")
    return problems


def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        GRAPHQL, method="POST", data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"Bearer {cs.token()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            answer = json.load(response)
    except urllib.error.HTTPError as error:
        raise HealthError(f"Cloudflare's analytics answered {error.code}: "
                          f"{error.read().decode('utf-8', 'replace')[:500]}") from None
    except (OSError, ValueError) as error:
        raise HealthError(f"Cloudflare's analytics: {error}") from None
    if answer.get("errors"):
        raise HealthError("Cloudflare's analytics: " + "; ".join(str(e.get("message")) for e in answer["errors"]))
    return answer["data"]


USAGE_QUERY = """
query Usage($account: string!, $day: Date!, $firstDay: Date!, $from: Time!, $to: Time!, $month: Time!) {
  viewer { accounts(filter: {accountTag: $account}) {
    workers: workersInvocationsAdaptive(limit: 10000, filter: {date_geq: $firstDay, date_leq: $day}) {
      sum { requests } }
    d1: d1AnalyticsAdaptiveGroups(limit: 10000, filter: {date_geq: $firstDay, date_leq: $day}) {
      sum { rowsRead rowsWritten } }
    d1Storage: d1StorageAdaptiveGroups(limit: 10000, filter: {date: $day}) {
      max { databaseSizeBytes } dimensions { databaseId } }
    r2Storage: r2StorageAdaptiveGroups(limit: 10000, filter: {datetime_geq: $from, datetime_lt: $to}) {
      max { payloadSize metadataSize } dimensions { bucketName } }
    r2Operations: r2OperationsAdaptiveGroups(limit: 10000, filter: {datetime_geq: $month, datetime_lt: $to}) {
      sum { requests } dimensions { actionType } }
  } }
}"""


def held(rows: list[dict[str, Any]] | None, keys: tuple[str, ...], by: str) -> int:
    """What the databases or buckets hold together: each one's most in the
    period (`keys` added up), summed, as the plan's limit is the account's."""
    most: dict[str, int] = {}
    for row in rows or []:
        name = (row.get("dimensions") or {}).get(by, "")
        size = sum(int((row.get("max") or {}).get(key) or 0) for key in keys)
        most[name] = max(most.get(name, 0), size)
    return sum(most.values())


def used(data: dict[str, Any]) -> dict[str, int]:
    """Each of INCLUDED's measures, from the analytics' answer."""
    accounts = data["viewer"]["accounts"]
    if not accounts:
        raise HealthError("the token sees no account: give it this account's Account Analytics: Read")
    account = accounts[0]
    total = lambda rows, part, key: sum(int((row.get(part) or {}).get(key) or 0) for row in rows or [])
    operations = account.get("r2Operations") or []
    by_class = {"A": 0, "B": 0}
    for row in operations:
        action = (row.get("dimensions") or {}).get("actionType", "")
        if action not in FREE:
            by_class["B" if action in CLASS_B else "A"] += int((row.get("sum") or {}).get("requests") or 0)
    return {
        "Workers requests this month": total(account.get("workers"), "sum", "requests"),
        "D1 rows read this month": total(account.get("d1"), "sum", "rowsRead"),
        "D1 rows written this month": total(account.get("d1"), "sum", "rowsWritten"),
        "D1 storage, bytes": held(account.get("d1Storage"), ("databaseSizeBytes",), "databaseId"),
        "R2 storage, bytes": held(account.get("r2Storage"), ("payloadSize", "metadataSize"), "bucketName"),
        "R2 Class A operations this month": by_class["A"],
        "R2 Class B operations this month": by_class["B"],
    }


def usage(now: dt.datetime) -> dict[str, int]:
    zones = cs.api(f"/zones?name={cs.ZONE}")
    if not zones:
        raise HealthError(f"the token cannot see the zone {cs.ZONE}")
    yesterday = (now - dt.timedelta(days=1)).date()
    start = dt.datetime.combine(yesterday, dt.time(), dt.timezone.utc)
    # The month that yesterday is in: on the 1st, the whole of the month before.
    month = start.replace(day=1)
    stamp = lambda moment: moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return used(graphql(USAGE_QUERY, {"account": zones[0]["account"]["id"], "day": yesterday.isoformat(),
                                      "firstDay": month.date().isoformat(),
                                      "from": stamp(start), "to": stamp(start + dt.timedelta(days=1)),
                                      "month": stamp(month)}))


MAIL_QUERY = """
query Mail($zone: string!, $start: Date!, $end: Date!) {
  viewer { zones(filter: {zoneTag: $zone}) {
    emailSendingAdaptiveGroups(limit: 10000, filter: {date_geq: $start, date_leq: $end}) {
      count dimensions { status } }
  } }
}"""


def delivery(data: dict[str, Any]) -> dict[str, int]:
    """The emails' count by status (delivered, deliveryFailed - a bounce - ...)."""
    zones = data["viewer"]["zones"]
    if not zones:
        raise HealthError("the token sees no zone analytics: give it the zone's Analytics: Read")
    counts: dict[str, int] = {}
    for row in zones[0].get("emailSendingAdaptiveGroups") or []:
        status = str((row.get("dimensions") or {}).get("status") or "")
        counts[status] = counts.get(status, 0) + int(row.get("count") or 0)
    return counts


def suppressions(account: str) -> list[dict[str, Any]]:
    """Every entry of the account's suppression list."""
    found: list[dict[str, Any]] = []
    for page in range(1, 101):
        rows = cs.api(f"/accounts/{account}/email/sending/suppressions?per_page={SUPPRESSIONS_PAGE}&page={page}") or []
        found += rows
        if len(rows) < SUPPRESSIONS_PAGE:
            return found
    return found


def mail(now: dt.datetime) -> dict[str, int]:
    zones = cs.api(f"/zones?name={cs.ZONE}")
    if not zones:
        raise HealthError(f"the token cannot see the zone {cs.ZONE}")
    today = now.date()
    counts = delivery(graphql(MAIL_QUERY, {"zone": zones[0]["id"], "end": today.isoformat(),
                                           "start": (today - dt.timedelta(days=MAIL_DAYS)).isoformat()}))
    since = (now - dt.timedelta(days=COMPLAINT_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    added = [row for row in suppressions(zones[0]["account"]["id"]) if str(row.get("created_at") or "") >= since]
    return {"delivered": counts.get("delivered", 0), "bounced": counts.get("deliveryFailed", 0),
            "complaints": sum(row.get("reason") == "complaint" for row in added),
            "hard bounces suppressed": sum(row.get("reason") == "hard_bounce" for row in added)}


def mail_problems(measures: dict[str, int]) -> list[str]:
    problems = []
    tried = measures["delivered"] + measures["bounced"]
    if measures["bounced"] >= MIN_BOUNCES and measures["bounced"] >= BOUNCE_WARN * tried:
        problems.append(f"{measures['bounced']} of {tried} emails in {MAIL_DAYS} days bounced "
                        f"({measures['bounced'] / tried:.1%}), past {BOUNCE_WARN:.0%}: .github/readouts.md says what to do")
    if measures["complaints"]:
        problems.append(f"{measures['complaints']} marked as spam in {COMPLAINT_DAYS} days: "
                        ".github/readouts.md says what to do")
    return problems


def usage_problems(measures: dict[str, int]) -> list[str]:
    return [f"{what}: {measures[what]:,} of {limit:,} ({measures[what] / limit:.0%}), past {WARN_AT:.0%}"
            for what, limit in INCLUDED.items() if measures.get(what, 0) >= WARN_AT * limit]


def main(argv: list[str] | None = None, now: dt.datetime | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cs.TOKEN_VARIABLE = cs.TOKENS["settings-check-read-only"]["variable"]
    if args not in (["snapshot"], ["usage"], ["mail"], ["live"], ["live", "on"], ["live", "off"], ["deployed"]):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        if args == ["snapshot"]:
            problems = snapshot_problems(fetch_snapshot(), now)
            good = f"The public figures at {SNAPSHOT_URL} are fresh, of schema {latest_migration()}."
        elif args[0] == "live":
            ingest = args[1] if len(args) > 1 else "on"
            problems = live(ingest)
            good = (f"{API_URL} answers as deployed, " + ("taking requests." if ingest == "on" else "taking none."))
        elif args == ["deployed"]:
            tree = tree_here()
            problems = deployed(tree)
            good = f"{API_URL} answers as deployed from this commit's website/api/ ({tree})."
        elif args == ["mail"]:
            measures = mail(now)
            print(f"  In {MAIL_DAYS} days: {measures['delivered']:,} emails delivered, {measures['bounced']:,} bounced")
            print(f"  Suppressed in {COMPLAINT_DAYS} days: {measures['hard bounces suppressed']:,} for a hard bounce, "
                  f"{measures['complaints']:,} for a complaint")
            problems = mail_problems(measures)
            good = "The API's emails bounce rarely, and drew no complaint."
        else:
            measures = usage(now)
            for what, limit in INCLUDED.items():
                print(f"  {what}: {measures[what]:,} of {limit:,} ({measures[what] / limit:.1%})")
            problems = usage_problems(measures)
            good = f"Every measure is under {WARN_AT:.0%} of what the plan includes."
    except (HealthError, cs.SettingsError) as error:
        print(f"api_health: {error}", file=sys.stderr)
        return 1
    for problem in problems:
        print(f"  {problem}")
    if problems:
        return 1
    print(good)
    return 0


if __name__ == "__main__":
    sys.exit(main())
