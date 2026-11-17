#!/usr/bin/env python3
"""The repository's settings on GitHub, written down here with why.

    python3 tools/repo/github_settings.py          what differs from them
    python3 tools/repo/github_settings.py apply    set them
    python3 tools/repo/github_settings.py secret NAME    set an environment
                                                         secret, from stdin

Through gh, logged in as an admin of the repository. `apply` changes only
what differs, and of the checks before main (the second ruleset) requires
only those that have run on main, saying which are left: a check required
before its workflow is there - a new job, or one renamed - would hold every
pull request back, the firmware-update workflow's too. Run it again once
they have run. A secret's value cannot be read back, so only its name is
checked, and one not written here is deleted. `apply` leaves a missing one
to `secret` - or a Cloudflare token to tools/repo/cloudflare_tokens.py,
which makes it and sets it.
"""

from __future__ import annotations

import getpass
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.repo import cloudflare_settings  # noqa: E402

REPOSITORY = "adamsthws/firmware-liberator"
GITHUB_ACTIONS = 15368   # the GitHub Actions app: only its runs may pass a required check
ADMIN_ROLE = 5           # the repository's admin role, in a ruleset's bypass list

WEBSITE = "https://firmwareliberator.com/"

# How the repository is found: its description, website and topics are what
# GitHub's search and its pages show, in the words people search with.
# tests/repo/test_github_settings.py fails until the topics name each maker,
# chip, chip family and firmware the devices have.
TOPICS = ["beken", "bk7238", "esp32", "esp32-c3", "esphome", "espressif", "ewelink", "firmware", "flashing-tool",
          "home-assistant", "home-automation", "iot", "openbeken", "ota", "smart-plug", "smart-switch", "sonoff",
          "supla", "tasmota"]

# Pull requests are squashed - one commit each, as main's history is kept -
# and their branches deleted once merged. No wiki: on a public repository any
# GitHub user may edit one by default, pages under the project's address no
# review, leak scan or check sees; the docs live in docs/.
REPOSITORY_SETTINGS = {
    "description": "Convert Sonoff smart devices to Tasmota, ESPHome, OpenBeken or Supla over Wi-Fi, "
                   "with no soldering. Fully automated OTA flasher for Linux, macOS and Windows.",
    "homepage": WEBSITE,
    "allow_squash_merge": True,
    "allow_merge_commit": False,
    "allow_rebase_merge": False,
    "delete_branch_on_merge": True,
    "has_wiki": False,
}

# GitHub Actions, each at repos/REPOSITORY/<path>:
# - an action is used only pinned to a commit, as every workflow pins them
#   (a tag can be moved to other code; a commit cannot);
# - a workflow's token reads unless the workflow asks for more, and can never
#   approve a pull request: no workflow here does, and one that could would
#   pass a review a ruleset asked for;
# - a pull request from anyone outside the repository waits for the maintainer
#   to let its workflows run, not only a first one: a fork's workflows run
#   whatever it changed.
ACTIONS = {
    "/actions/permissions": {"enabled": True, "allowed_actions": "all", "sha_pinning_required": True},
    "/actions/permissions/workflow": {"default_workflow_permissions": "read",
                                      "can_approve_pull_request_reviews": False},
    "/actions/permissions/fork-pr-contributor-approval": {"approval_policy": "all_external_contributors"},
}

# The repository's own secrets, apart from the website environment's, each
# set by the script that says what it is for: any other is a difference, and
# `apply` deletes it. Every workflow's job could read one; the environment's
# are for main's alone.
REPOSITORY_SECRETS = {
    "AUTOFLASH_BACKUP_KEY": "the sealed stock-firmware backup's key, for the emulator: "
                            "python3 tools/repo/private_backup.py secret",
    # The emulator suites' service token, with which the final-firmware check
    # on main sends its runs to the admin view (tools/repo/emulator_stats.py):
    # it can add emulator runs and nothing else. The workflow gives it only to
    # a run on main, never a pull request's.
    "EMULATOR_STATS_CLIENT_ID": "the emulator suites' service token's id: "
                                "python3 tools/repo/cloudflare_settings.py rotate emulator-stats",
    "EMULATOR_STATS_CLIENT_SECRET": "the emulator suites' service token's secret: "
                                    "python3 tools/repo/cloudflare_settings.py rotate emulator-stats",
}

# Secret scanning refuses a push holding a known kind of token; Dependabot
# opens a pull request at once for a package with an advisory (its weekly
# updates wait 7 days, .github/dependabot.yml).
SECURITY = {
    "secret_scanning": "enabled",
    "secret_scanning_push_protection": "enabled",
    "dependabot_security_updates": "enabled",
}

# CodeQL's own setup: the Python, the website's JavaScript and the workflows,
# on each push and pull request to main and weekly. (The C under archive/ is
# history.)
CODE_SCANNING = {"state": "configured", "languages": ["actions", "javascript-typescript", "python"],
                 "query_suite": "default"}
# GitHub answers with the one language above under these names as well.
CODE_SCANNING_ALIASES = {"javascript", "typescript"}

# On/off settings, each at repos/REPOSITORY/<path>: private vulnerability
# reporting, which SECURITY.md asks for; immutable releases - a release's
# tag and files cannot be changed once published, and GitHub signs an
# attestation of them (`gh release verify`).
TOGGLES = ["private-vulnerability-reporting", "immutable-releases"]

# The website, firmwareliberator.com, is a Cloudflare Worker
# (website/README.md) the website workflow deploys from this environment: it
# takes only main, and holds the Cloudflare tokens, each allowed only what its
# workflow does (tools/repo/cloudflare_settings.py lists what each is given).
# GitHub Pages, which served it before, is off, and it is the one environment:
# another (github-pages, which Pages left) is deleted.
ENVIRONMENT = "website"
ENVIRONMENT_POLICY = {"protected_branches": False, "custom_branch_policies": True}
ENVIRONMENT_BRANCHES = ["main"]
ENVIRONMENT_SECRETS = {
    "CLOUDFLARE_WORKERS_DEPLOY_TOKEN": "the website workflow's, deploying the three Workers",
    "CLOUDFLARE_SETTINGS_CHECK_TOKEN": "the cloudflare-settings workflow's, reading the settings it checks",
}

MAIN = {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}}
# The checks main requires: jobs of the unit tests and emulator, leak-scan
# and lint workflows.
REQUIRED_CHECKS = ["unit (Python 3.12)", "unit (Python 3.10)", "emulator", "scan", "lint"]


def checks_ruleset(names: list[str]) -> dict[str, Any]:
    return {
        "target": "branch", "enforcement": "active", "conditions": MAIN,
        "bypass_actors": [{"actor_id": ADMIN_ROLE, "actor_type": "RepositoryRole", "bypass_mode": "always"}],
        "rules": [{"type": "required_status_checks", "parameters": {
            "strict_required_status_checks_policy": False,
            "do_not_enforce_on_create": False,
            "required_status_checks": [{"context": name, "integration_id": GITHUB_ACTIONS} for name in names],
        }}],
    }


RULESETS: dict[str, dict[str, Any]] = {
    # For everyone, admins too: main is never deleted or rewritten. Signed
    # commits are not required: GitHub then lets only a pull request's author
    # squash it, so no one else's could be merged.
    "Protect Main Branch": {
        "target": "branch", "enforcement": "active", "conditions": MAIN, "bypass_actors": [],
        "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}],
    },
    # What reaches main through a pull request has passed the checks. An admin
    # may push to main directly, as releases are made; the release workflow
    # runs the same checks before it releases.
    "Checks before main": checks_ruleset(REQUIRED_CHECKS),
}


class SettingsError(RuntimeError):
    pass


def api(path: str, method: str = "GET", body: Any = None, ok_missing: bool = False) -> Any:
    command = ["gh", "api", "-X", method, "-H", "Accept: application/vnd.github+json",
               f"repos/{REPOSITORY}{path}"]
    if body is not None:
        command += ["--input", "-"]
    result = subprocess.run(command, input=None if body is None else json.dumps(body),
                            capture_output=True, text=True)
    if result.returncode:
        if ok_missing and "(HTTP 404)" in result.stderr:
            return None
        raise SettingsError(f"gh api -X {method} {path}: {result.stderr.strip() or result.stdout.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def comparable(ruleset: dict[str, Any]) -> dict[str, Any]:
    """What a ruleset says, as RULESETS writes it, from what GitHub returns."""
    rules = [{key: value for key, value in rule.items() if key in ("type", "parameters")}
             for rule in ruleset.get("rules", [])]
    return {
        "target": ruleset.get("target"), "enforcement": ruleset.get("enforcement"),
        "conditions": ruleset.get("conditions"), "rules": rules,
        "bypass_actors": [{key: actor[key] for key in ("actor_id", "actor_type", "bypass_mode")}
                          for actor in ruleset.get("bypass_actors") or []],
    }


def checks_run_on_main() -> set[str]:
    """Each check run on main's last 30 commits."""
    names: set[str] = set()
    for commit in api("/commits?sha=main&per_page=30"):
        runs = api(f"/commits/{commit['sha']}/check-runs?per_page=100")
        names.update(run["name"] for run in runs["check_runs"])
    return names


def differences() -> list[tuple[str, str, Any]]:
    """(what, how it differs, the change that makes it right)."""
    found: list[tuple[str, str, Any]] = []
    repository = api("")
    for key, want in REPOSITORY_SETTINGS.items():
        if repository.get(key) != want:
            found.append((key, f"is {repository.get(key)}, should be {want}", ("PATCH", "", {key: want})))
    topics = api("/topics")["names"]
    if sorted(topics) != sorted(TOPICS):
        found.append(("topics", f"are {topics}, should be {TOPICS}", ("PUT", "/topics", {"names": TOPICS})))
    analysis = repository.get("security_and_analysis") or {}
    for key, want in SECURITY.items():
        have = (analysis.get(key) or {}).get("status")
        if have != want:
            found.append((key, f"is {have}, should be {want}",
                          ("PATCH", "", {"security_and_analysis": {key: {"status": want}}})))
    scanning = api("/code-scanning/default-setup")
    have = {key: scanning.get(key) for key in CODE_SCANNING}
    languages = sorted(set(have["languages"] or []) - CODE_SCANNING_ALIASES)
    if have["state"] != CODE_SCANNING["state"] or languages != CODE_SCANNING["languages"] \
            or have["query_suite"] != CODE_SCANNING["query_suite"]:
        found.append(("CodeQL", f"is {have}, should be {CODE_SCANNING}",
                      ("PATCH", "/code-scanning/default-setup", CODE_SCANNING)))
    for toggle in TOGGLES:
        if not (api(f"/{toggle}", ok_missing=True) or {}).get("enabled"):
            found.append((toggle, "is off, should be on", ("PUT", f"/{toggle}", None)))
    if api("/pages", ok_missing=True) is not None:
        found.append(("Pages", "is on, should be off: the website is a Cloudflare Worker",
                      ("DELETE", "/pages", None)))
    for path, want in ACTIONS.items():
        have = api(path)
        off = sorted(key for key, value in want.items() if have.get(key) != value)
        if off:
            found.append((f"Actions {path.removeprefix('/actions/')}",
                          "differs in " + ", ".join(f"{key} (is {have.get(key)}, should be {want[key]})" for key in off),
                          ("PUT", path, want)))
    repository_secrets = {secret["name"] for secret in api("/actions/secrets")["secrets"]}
    for name, how in REPOSITORY_SECRETS.items():
        if name not in repository_secrets:
            found.append((f"repository secret {name}", "is missing", ("LEFT", f"set it: {how.split(': ')[-1]}", None)))
    for name in sorted(repository_secrets - set(REPOSITORY_SECRETS)):
        found.append((f"repository secret {name}", "should not be there", ("DELETE", f"/actions/secrets/{name}", None)))
    for name in sorted({env["name"] for env in api("/environments")["environments"]} - {ENVIRONMENT}):
        found.append((f"environment '{name}'", "should not be there", ("DELETE", f"/environments/{name}", None)))
    environment = api(f"/environments/{ENVIRONMENT}", ok_missing=True)
    have = (environment or {}).get("deployment_branch_policy")
    if have != ENVIRONMENT_POLICY:
        how = "is missing" if environment is None else f"has the branch policy {have}, should name its branches"
        found.append((f"environment '{ENVIRONMENT}'", how,
                      ("PUT", f"/environments/{ENVIRONMENT}", {"deployment_branch_policy": ENVIRONMENT_POLICY})))
    policies = api(f"/environments/{ENVIRONMENT}/deployment-branch-policies")["branch_policies"] \
        if have == ENVIRONMENT_POLICY else []
    for branch in ENVIRONMENT_BRANCHES:
        if not any(policy["name"] == branch and policy.get("type") == "branch" for policy in policies):
            found.append((f"environment '{ENVIRONMENT}' branch {branch}", "is missing",
                          ("POST", f"/environments/{ENVIRONMENT}/deployment-branch-policies",
                           {"name": branch, "type": "branch"})))
    for policy in policies:
        if policy["name"] not in ENVIRONMENT_BRANCHES or policy.get("type") != "branch":
            found.append((f"environment '{ENVIRONMENT}' {policy.get('type')} {policy['name']}", "should not be there",
                          ("DELETE", f"/environments/{ENVIRONMENT}/deployment-branch-policies/{policy['id']}", None)))
    secrets = {secret["name"] for secret in api(f"/environments/{ENVIRONMENT}/secrets")["secrets"]} \
        if environment is not None else set()
    for name in ENVIRONMENT_SECRETS:
        if name not in secrets:
            found.append((f"environment '{ENVIRONMENT}' secret {name}", "is missing", ("SECRET", name, None)))
    for name in sorted(secrets - set(ENVIRONMENT_SECRETS)):
        found.append((f"environment '{ENVIRONMENT}' secret {name}", "should not be there",
                      ("DELETE", f"/environments/{ENVIRONMENT}/secrets/{name}", None)))
    existing = {ruleset["name"]: ruleset["id"] for ruleset in api("/rulesets")}
    for name, want in RULESETS.items():
        body = {"name": name, **want}
        if name not in existing:
            found.append((f"ruleset '{name}'", "is missing", ("POST", "/rulesets", body)))
        elif comparable(api(f"/rulesets/{existing[name]}")) != comparable(want):
            found.append((f"ruleset '{name}'", "differs", ("PUT", f"/rulesets/{existing[name]}", body)))
    return found


def set_secret(name: str, value: str, environment: bool = True) -> None:
    """Through gh, which seals it with the environment's public key - or the
    repository's, for one of REPOSITORY_SECRETS; the value goes on stdin,
    never into a command line."""
    where = ["--env", ENVIRONMENT] if environment else []
    result = subprocess.run(["gh", "secret", "set", name, *where, "-R", REPOSITORY],
                            input=value, capture_output=True, text=True)
    if result.returncode:
        raise SettingsError(f"gh secret set {name}: {result.stderr.strip() or result.stdout.strip()}")


def secret(name: str) -> int:
    if name not in ENVIRONMENT_SECRETS:
        print(f"github_settings: {name} is not one of {', '.join(ENVIRONMENT_SECRETS)}", file=sys.stderr)
        return 2
    value = (getpass.getpass(f"{name}: ") if sys.stdin.isatty() else sys.stdin.read()).strip()
    if not value:
        print(f"github_settings: no value for {name}", file=sys.stderr)
        return 1
    try:
        set_secret(name, value)
    except SettingsError as error:
        print(f"github_settings: {error}", file=sys.stderr)
        return 1
    print(f"  environment '{ENVIRONMENT}' secret {name}: set")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 2 and args[0] == "secret":
        return secret(args[1])
    if args not in ([], ["apply"]):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    left = 0
    try:
        found = differences()
        for what, how, _ in found:
            print(f"  {what}: {how}")
        if not found:
            print(f"{REPOSITORY}: every setting is as tools/repo/github_settings.py says.")
        elif not args:
            print("python3 tools/repo/github_settings.py apply sets them.")
            return 1
        if not args:
            return 0
        for what, _, (method, path, body) in found:
            if method == "SECRET":
                made = [key for key, spec in cloudflare_settings.TOKENS.items() if spec.get("variable") == path]
                print(f"  {what}: left - " + (f"made with python3 tools/repo/cloudflare_tokens.py {made[0]}" if made
                                              else f"its value is set with python3 tools/repo/github_settings.py "
                                                   f"secret {path}"))
                left = 1
                continue
            if method == "LEFT":
                print(f"  {what}: left - {path}")
                left = 1
                continue
            if what == "ruleset 'Checks before main'":
                run = checks_run_on_main()
                ready = [name for name in REQUIRED_CHECKS if name in run]
                if not ready:
                    print(f"  {what}: left for now - none has run on main yet")
                    continue
                if ready != REQUIRED_CHECKS:
                    body = {"name": "Checks before main", **checks_ruleset(ready)}
                    not_run = [name for name in REQUIRED_CHECKS if name not in run]
                    api(path, method, body)
                    print(f"  {what}: set, but for {', '.join(not_run)}, not yet run on main: "
                          "apply again once they have")
                    continue
            api(path, method, body)
            print(f"  {what}: {'removed' if method == 'DELETE' else 'set'}")
    except (SettingsError, cloudflare_settings.SettingsError) as error:
        print(f"github_settings: {error}", file=sys.stderr)
        return 1
    return left


if __name__ == "__main__":
    sys.exit(main())
