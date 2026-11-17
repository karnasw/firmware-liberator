import copy
import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from tools.repo import github_settings as gs


class FakeGitHub:
    """GitHub's API for one repository, as tools/repo/github_settings.py sees it."""

    def __init__(self, checks_on_main=(), protect_rules=None, environment=False):
        self.repository = {key: not want for key, want in gs.REPOSITORY_SETTINGS.items()}
        self.repository["security_and_analysis"] = {key: {"status": "disabled"} for key in gs.SECURITY}
        self.topics = []
        self.scanning = {"state": "not-configured", "languages": ["actions", "c-cpp", "python"],
                         "query_suite": "default"}
        self.toggles = {toggle: False for toggle in gs.TOGGLES}
        self.pages = {"cname": "firmwareliberator.com", "build_type": "legacy"}
        # A new environment has no secrets: with `environment`, it is there as written, its secrets set.
        self.environments = {gs.ENVIRONMENT: {"policy": gs.ENVIRONMENT_POLICY, "branches": [
            {"id": 1, "name": "main", "type": "branch"}]}} if environment else {}
        self.secrets = set(gs.ENVIRONMENT_SECRETS) if environment else set()
        self.rulesets = {7: {"id": 7, "name": "Protect Main Branch", "target": "branch",
                             "enforcement": "active", "conditions": gs.MAIN, "bypass_actors": [],
                             "rules": protect_rules or [{"type": "deletion"}],
                             "node_id": "x", "created_at": "2026-10-21"}}
        self.checks_on_main = list(checks_on_main)
        # Actions as a new repository has them: any action by tag, a workflow's token allowed to
        # approve, a fork's workflows run unasked after a first pull request.
        self.actions = {"/actions/permissions": {"enabled": True, "allowed_actions": "all",
                                                 "sha_pinning_required": False},
                        "/actions/permissions/workflow": {"default_workflow_permissions": "read",
                                                          "can_approve_pull_request_reviews": True},
                        "/actions/permissions/fork-pr-contributor-approval": {
                            "approval_policy": "first_time_contributors"}}
        self.repository_secrets = set(gs.REPOSITORY_SECRETS)
        self.sent = []
        self.secrets_set = {}   # what gh was given for each secret it set

    def set_secret(self, name, value):
        self.secrets.add(name)
        self.secrets_set[name] = value

    def __call__(self, path, method="GET", body=None, ok_missing=False):
        if method != "GET":
            self.sent.append((method, path, body))
            if path == "/topics":
                self.topics = list(body["names"])
            if path == "/pages" and method == "DELETE":
                self.pages = None
            if path in self.actions:
                self.actions[path] = copy.deepcopy(body)
            elif path.startswith("/actions/secrets/"):
                self.repository_secrets.discard(path.rsplit("/", 1)[1])
            elif "/secrets/" in path:
                self.secrets.discard(path.rsplit("/", 1)[1])
            elif path.startswith("/environments/"):
                name = path.split("/")[2]
                if path.count("/") == 2 and method == "DELETE":
                    del self.environments[name]
                elif path.count("/") == 2:
                    self.environments[name] = {"policy": copy.deepcopy(body["deployment_branch_policy"]),
                                               "branches": []}
                elif method == "POST":
                    self.environments[name]["branches"].append({"id": len(self.sent), **copy.deepcopy(body)})
                else:
                    gone = int(path.rsplit("/", 1)[1])
                    self.environments[name]["branches"] = [policy for policy in self.environments[name]["branches"]
                                                           if policy["id"] != gone]
            if path.startswith("/rulesets"):
                ruleset = {"id": 8 if method == "POST" else int(path.rsplit("/", 1)[1]), **copy.deepcopy(body)}
                self.rulesets[ruleset["id"]] = ruleset
            return None
        if path == "":
            return self.repository
        if path == "/topics":
            return {"names": list(self.topics)}
        if path == "/code-scanning/default-setup":
            return self.scanning
        if path.lstrip("/") in self.toggles:
            return {"enabled": self.toggles[path.lstrip("/")]}
        if path == "/pages":
            assert ok_missing or self.pages is not None
            return self.pages
        if path in self.actions:
            return copy.deepcopy(self.actions[path])
        if path == "/actions/secrets":
            return {"secrets": [{"name": name} for name in sorted(self.repository_secrets)]}
        if path == "/environments":
            return {"environments": [{"name": name} for name in self.environments]}
        if path == f"/environments/{gs.ENVIRONMENT}/secrets":
            assert gs.ENVIRONMENT in self.environments
            return {"total_count": len(self.secrets), "secrets": [{"name": name} for name in sorted(self.secrets)]}
        if path.startswith("/environments/"):
            environment = self.environments.get(path.split("/")[2])
            if path.endswith("/deployment-branch-policies"):
                return {"branch_policies": copy.deepcopy(environment["branches"])}
            assert ok_missing or environment is not None
            return environment and {"name": path.split("/")[2], "deployment_branch_policy": environment["policy"]}
        if path == "/rulesets":
            return [{"id": r["id"], "name": r["name"]} for r in self.rulesets.values()]
        if path.startswith("/rulesets/"):
            return self.rulesets[int(path.rsplit("/", 1)[1])]
        if path.startswith("/commits?"):
            return [{"sha": "a"}]
        if path == "/commits/a/check-runs?per_page=100":
            return {"check_runs": [{"name": name} for name in self.checks_on_main]}
        raise AssertionError(f"unexpected {method} {path}")


def run(fake, *args, env=None):
    out = io.StringIO()
    with mock.patch.object(gs, "api", fake), mock.patch.object(gs, "set_secret", fake.set_secret), \
            mock.patch.dict("os.environ", env or {}), redirect_stdout(out):
        code = gs.main(list(args))
    return code, out.getvalue()


class GitHubSettingsTests(unittest.TestCase):
    def test_a_ruleset_as_github_returns_it_compares_as_written(self):
        returned = {"id": 1, "name": "Protect Main Branch", "source": "x", "node_id": "y",
                    "_links": {}, **copy.deepcopy(gs.RULESETS["Protect Main Branch"])}
        self.assertEqual(gs.comparable(returned), gs.comparable(gs.RULESETS["Protect Main Branch"]))

    def test_check_only_says_what_differs(self):
        fake = FakeGitHub()
        code, out = run(fake)
        self.assertEqual(code, 1)
        self.assertIn("allow_merge_commit: is True, should be False", out)
        self.assertIn("ruleset 'Protect Main Branch': differs", out)
        self.assertIn("ruleset 'Checks before main': is missing", out)
        self.assertEqual(fake.sent, [])

    def test_only_checks_that_have_run_on_main_are_required(self):
        fake = FakeGitHub(checks_on_main=["unit", "emulator", "scan"], environment=True)
        code, out = run(fake, "apply")
        self.assertEqual(code, 0)
        self.assertIn("set, but for unit (Python 3.12), unit (Python 3.10), lint, not yet run on main", out)
        checks = fake.rulesets[8]["rules"][0]["parameters"]["required_status_checks"]
        self.assertEqual([check["context"] for check in checks], ["emulator", "scan"])
        # Everything else is set, main's protection among it.
        protect = fake.rulesets[7]
        self.assertIn({"type": "non_fast_forward"}, protect["rules"])
        self.assertIn(("PUT", "/immutable-releases", None), fake.sent)
        self.assertIn(("PATCH", "/code-scanning/default-setup", gs.CODE_SCANNING), fake.sent)
        # Asked again: still not as written, until they have run.
        code, out = run(fake)
        self.assertIn("ruleset 'Checks before main': differs", out)

    def test_a_renamed_check_is_no_longer_required_before_its_new_name_has_run(self):
        fake = FakeGitHub(checks_on_main=["unit", "unit (Python 3.10)", "emulator", "scan", "lint"])
        fake.rulesets[8] = {"id": 8, "name": "Checks before main",
                            **gs.checks_ruleset(["unit", "unit (Python 3.10)", "emulator", "scan", "lint"])}
        run(fake, "apply")
        checks = [c["context"] for c in fake.rulesets[8]["rules"][0]["parameters"]["required_status_checks"]]
        self.assertEqual(checks, ["unit (Python 3.10)", "emulator", "scan", "lint"])
        fake.checks_on_main.append("unit (Python 3.12)")
        run(fake, "apply")
        checks = [c["context"] for c in fake.rulesets[8]["rules"][0]["parameters"]["required_status_checks"]]
        self.assertEqual(checks, gs.REQUIRED_CHECKS)

    def test_no_checks_are_required_before_any_has_run_on_main(self):
        code, out = run(FakeGitHub(), "apply")
        self.assertIn("ruleset 'Checks before main': left for now - none has run on main yet", out)

    def test_checks_are_required_once_each_has_run_on_main(self):
        fake = FakeGitHub(checks_on_main=[*gs.REQUIRED_CHECKS, "CodeQL"], environment=True)
        code, out = run(fake, "apply")
        self.assertEqual(code, 0)
        self.assertIn("ruleset 'Checks before main': set", out)
        checks = fake.rulesets[8]["rules"][0]["parameters"]["required_status_checks"]
        self.assertEqual([check["context"] for check in checks], gs.REQUIRED_CHECKS)
        self.assertTrue(all(check["integration_id"] == gs.GITHUB_ACTIONS for check in checks))

    def test_nothing_is_sent_when_all_is_as_written(self):
        fake = FakeGitHub(checks_on_main=gs.REQUIRED_CHECKS)
        run(fake, "apply")
        fake.repository.update(gs.REPOSITORY_SETTINGS)
        fake.repository["security_and_analysis"] = {key: {"status": want} for key, want in gs.SECURITY.items()}
        fake.scanning = dict(gs.CODE_SCANNING)
        fake.toggles = {toggle: True for toggle in gs.TOGGLES}
        fake.secrets = set(gs.ENVIRONMENT_SECRETS)
        fake.sent.clear()
        code, out = run(fake, "apply")
        self.assertEqual((code, fake.sent), (0, []))
        self.assertIn("every setting is as tools/repo/github_settings.py says", out)

    def test_codeql_as_github_names_javascript_is_no_difference(self):
        fake = FakeGitHub()
        fake.scanning = {**gs.CODE_SCANNING, "languages": [
            "actions", "javascript", "javascript-typescript", "python", "typescript"]}
        with mock.patch.object(gs, "api", fake):
            self.assertNotIn("CodeQL", [what for what, _, _ in gs.differences()])

    def test_the_repository_says_what_it_is_and_where_its_website_is(self):
        fake = FakeGitHub()
        fake.repository.update(description="old words", homepage="")
        code, out = run(fake)
        self.assertIn("description: is old words, should be", out)
        self.assertIn(f"homepage: is , should be {gs.WEBSITE}", out)
        run(fake, "apply")
        self.assertIn(("PATCH", "", {"homepage": gs.WEBSITE}), fake.sent)

    def test_the_topics_name_each_maker_chip_family_and_firmware_the_devices_have(self):
        import re
        from tools.autoflash import kinds, routes
        self.assertLessEqual(len(gs.TOPICS), 20, "GitHub takes 20 topics at most")
        wanted = {name for name in routes.FIRMWARES if name != "sonoff"}
        for kind in kinds.EVERY:
            # A topic as GitHub writes one: esp32c3 is esp32-c3.
            wanted |= {kind.maker.lower(), kind.ecosystem, kind.family, re.sub(r"^esp32(?=.)", "esp32-", kind.chip)}
        self.assertLessEqual(wanted, set(gs.TOPICS))

    def test_topics_are_set_whatever_order_github_lists_them_in(self):
        fake = FakeGitHub()
        code, out = run(fake)
        self.assertIn("topics: are [], should be", out)
        self.assertEqual(fake.sent, [])
        run(fake, "apply")
        self.assertEqual(fake.topics, gs.TOPICS)
        fake.topics.reverse()
        fake.sent.clear()
        run(fake, "apply")
        self.assertNotIn("/topics", [path for _, path, _ in fake.sent])

    def test_pages_is_turned_off(self):
        fake = FakeGitHub()
        code, out = run(fake)
        self.assertIn("Pages: is on, should be off", out)
        code, out = run(fake, "apply")
        self.assertIn("Pages: removed", out)
        self.assertIsNone(fake.pages)

    def test_the_website_environment_takes_main_only(self):
        fake = FakeGitHub()
        run(fake, "apply")
        self.assertEqual(fake.environments, {"website": {"policy": gs.ENVIRONMENT_POLICY, "branches": [
            {"id": fake.environments["website"]["branches"][0]["id"], "name": "main", "type": "branch"}]}})
        fake.environments["website"]["branches"].append({"id": 99, "name": "s61", "type": "branch"})
        code, out = run(fake)
        self.assertIn("environment 'website' branch s61: should not be there", out)
        run(fake, "apply")
        self.assertEqual([policy["name"] for policy in fake.environments["website"]["branches"]], ["main"])
        fake.sent.clear()
        run(fake, "apply")
        self.assertEqual([sent for sent in fake.sent if sent[1].startswith("/environments/")], [])

    def test_the_website_is_the_one_environment(self):
        fake = FakeGitHub(environment=True)
        fake.environments["github-pages"] = {"policy": gs.ENVIRONMENT_POLICY, "branches": []}
        code, out = run(fake)
        self.assertIn("environment 'github-pages': should not be there", out)
        run(fake, "apply")
        self.assertEqual(list(fake.environments), [gs.ENVIRONMENT])

    def test_actions_are_pinned_and_a_forks_workflows_wait_for_the_maintainer(self):
        fake = FakeGitHub(checks_on_main=gs.REQUIRED_CHECKS, environment=True)
        code, out = run(fake)
        self.assertIn("Actions permissions: differs in sha_pinning_required (is False, should be True)", out)
        self.assertIn("Actions permissions/workflow: differs in can_approve_pull_request_reviews", out)
        self.assertIn("Actions permissions/fork-pr-contributor-approval: differs in approval_policy", out)
        run(fake, "apply")
        self.assertEqual(fake.actions, gs.ACTIONS)
        fake.sent.clear()
        run(fake, "apply")
        self.assertEqual([sent for sent in fake.sent if sent[1].startswith("/actions")], [])

    def test_the_wiki_is_off(self):
        self.assertIs(gs.REPOSITORY_SETTINGS["has_wiki"], False)
        code, out = run(FakeGitHub())
        self.assertIn("has_wiki: is True, should be False", out)

    def test_a_repository_secret_not_written_here_is_deleted_and_a_missing_one_left(self):
        fake = FakeGitHub(checks_on_main=gs.REQUIRED_CHECKS, environment=True)
        fake.repository_secrets = {"OLD_TOKEN"}
        code, out = run(fake, "apply")
        self.assertEqual(code, 1)
        self.assertIn("repository secret OLD_TOKEN: removed", out)
        self.assertIn("repository secret AUTOFLASH_BACKUP_KEY: left - set it: "
                      "python3 tools/repo/private_backup.py secret", out)
        self.assertEqual(fake.repository_secrets, set())

    def test_the_website_is_deployed_from_the_environment_written_here(self):
        from pathlib import Path
        workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/website.yml").read_text(encoding="utf-8")
        self.assertIn(f"      name: {gs.ENVIRONMENT}\n      url: {gs.WEBSITE}", workflow)

    def test_every_required_check_is_a_job_in_a_workflow(self):
        from pathlib import Path
        import re
        root = Path(__file__).resolve().parents[2] / ".github" / "workflows"
        jobs = set()
        for workflow in root.glob("*.yml"):
            text = workflow.read_text(encoding="utf-8")
            body = text.split("\njobs:\n", 1)[1]
            for job, block in re.findall(r"^  ([\w-]+):\n((?:    .*\n|\n)*)", body, re.MULTILINE):
                named = re.search(r"^    name: (.+)$", block, re.MULTILINE)
                jobs.add(named.group(1).strip() if named else job)
        self.assertLessEqual(set(gs.REQUIRED_CHECKS), jobs)

    def test_a_missing_secret_is_named_and_left_for_its_value(self):
        fake = FakeGitHub(checks_on_main=gs.REQUIRED_CHECKS, environment=True)
        fake.secrets = {"CLOUDFLARE_WORKERS_DEPLOY_TOKEN", "OLD_TOKEN"}
        _, first = run(fake, "apply")
        self.assertIn(f"environment '{gs.ENVIRONMENT}' secret OLD_TOKEN: removed", first)
        code, out = run(fake, "apply")
        self.assertEqual(code, 1)
        # A Cloudflare token is made, and set, by cloudflare_tokens.py.
        self.assertIn("secret CLOUDFLARE_SETTINGS_CHECK_TOKEN: left - made with "
                      "python3 tools/repo/cloudflare_tokens.py settings-check-read-only", out)
        self.assertEqual(fake.secrets, {"CLOUDFLARE_WORKERS_DEPLOY_TOKEN"})

    def test_apply_sets_no_secret_itself(self):
        # Each is a Cloudflare token, made and set by cloudflare_tokens.py; the
        # domain's mail forwarding, private, is kept by Cloudflare alone.
        fake = FakeGitHub(checks_on_main=gs.REQUIRED_CHECKS, environment=True)
        self.assertEqual(run(fake, "apply")[0], 0)
        self.assertEqual(fake.secrets_set, {})
        self.assertTrue(all(name.startswith("CLOUDFLARE_") and name.endswith("_TOKEN") for name in gs.ENVIRONMENT_SECRETS))

    def test_a_secret_goes_to_gh_on_stdin_never_in_its_arguments(self):
        done = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(gs.subprocess, "run", return_value=done) as ran, \
                mock.patch("sys.stdin", io.StringIO("s3cret-value\n")), redirect_stdout(io.StringIO()):
            self.assertEqual(gs.main(["secret", "CLOUDFLARE_SETTINGS_CHECK_TOKEN"]), 0)
        command, = ran.call_args.args
        self.assertEqual(command, ["gh", "secret", "set", "CLOUDFLARE_SETTINGS_CHECK_TOKEN", "--env", gs.ENVIRONMENT,
                                   "-R", gs.REPOSITORY])
        self.assertEqual(ran.call_args.kwargs["input"], "s3cret-value")

    def test_only_a_secret_written_here_is_set(self):
        with mock.patch.object(gs.subprocess, "run") as ran, mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(gs.main(["secret", "SOMETHING_ELSE"]), 2)
        ran.assert_not_called()

    def test_the_workflows_use_the_secrets_written_here(self):
        from pathlib import Path
        import re
        root = Path(__file__).resolve().parents[2] / ".github" / "workflows"
        used = set()
        for name in ("website.yml", "cloudflare-settings.yml"):
            used |= set(re.findall(r"secrets\.(\w+)", (root / name).read_text(encoding="utf-8")))
        self.assertEqual(used, set(gs.ENVIRONMENT_SECRETS))


if __name__ == "__main__":
    unittest.main()
