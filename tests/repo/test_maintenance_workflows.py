"""The workflows that keep things moving, as they are written: GitHub alone runs them."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def workflow(name):
    return (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


class DependabotMergeTests(unittest.TestCase):
    def setUp(self):
        self.text = workflow("dependabot-merge.yml")

    def test_one_behind_main_is_brought_up_to_date_and_checked_again(self):
        self.assertIn("update-branch", self.text)
        self.assertRegex(self.text, r'for workflow in emulator-check\.yml lint\.yml leak-scan\.yml; do\s+'
                                    r'gh workflow run -R "\$GITHUB_REPOSITORY" "\$workflow" --ref "\$branch"')

    def test_a_failure_on_an_old_main_is_not_held_against_it(self):
        failed = self.text[self.text.index("BLOCKED|UNSTABLE)"):]
        self.assertIn("brought_up_to_date", failed[:failed.index(";;")])

    def test_every_check_main_requires_is_run_again(self):
        from tools.repo import github_settings
        # Each required check is a job of one of the workflows started.
        started = "\n".join(workflow(name) for name in ("emulator-check.yml", "lint.yml", "leak-scan.yml"))
        for check in github_settings.REQUIRED_CHECKS:
            job = check.split(" (")[0]
            self.assertTrue(re.search(rf"(?m)^  {re.escape(job)}:$|name: {re.escape(check)}", started), check)


class MonthlyDryRunTests(unittest.TestCase):
    MONTHLY = "github.event.schedule == '0 6 1 * *'"

    def setUp(self):
        self.text = workflow("firmware-update.yml")

    def test_it_is_scheduled_and_is_a_dry_run(self):
        self.assertIn('cron: "0 6 1 * *"', self.text)
        self.assertIn(f"DRY_RUN: ${{{{ inputs.dry_run || {self.MONTHLY} }}}}", self.text)

    def test_it_takes_what_is_pinned_so_every_step_runs(self):
        self.assertIn('pinned_version(u.load_pins(), "esphome")', self.text)
        self.assertIn('pinned_version(u.load_pins(), "openbeken")', self.text)

    def test_it_merges_nothing_and_opens_no_issue(self):
        merge = self.text[self.text.index("\n  merge:"):]
        self.assertIn(self.MONTHLY.replace("==", "!="), merge.split("\n")[2])
        self.assertEqual(self.text.count(self.MONTHLY.replace("==", "!=")), 3)

    def test_it_publishes_nothing(self):
        # The publish job runs only on what the update job says to, which a dry run never does.
        self.assertIn("if: ${{ needs.update.outputs.publish == 'yes' }}", job(self.text, "publish"))
        made = job(self.text, "update")
        self.assertLess(made.index('if [ "$DRY_RUN" = true ]; then'), made.index('echo "publish=yes"'))


def job(text, name):
    """One job of a workflow, as written: from its name to the next job's."""
    start = text.index(f"\n  {name}:\n")
    following = re.search(r"\n(?:  #.*\n)*  [a-z-]+:\n", text[start + 1:])
    return text[start:start + 1 + following.start()] if following else text[start:]


class FirmwareUpdateTests(unittest.TestCase):
    """The weekly update builds with the releases' own tools, so the job that
    does can only read; what it made is pushed by a job that runs none of it,
    and only a pull request it opened is merged, which releases it."""

    def setUp(self):
        self.text = workflow("firmware-update.yml")

    def test_no_token_is_given_to_every_job(self):
        top = self.text[:self.text.index("\njobs:")]
        self.assertNotIn("github.token", top)

    def test_the_build_can_only_read(self):
        update = job(self.text, "update")
        permissions = re.search(r"    permissions:\n((?:      .*\n)+)", update).group(1)
        self.assertNotIn("write", permissions)
        self.assertIn("update_firmware.py esphome", update)
        self.assertIn("git bundle create", update)

    def test_the_publish_job_runs_nothing_of_the_update_and_checks_it_first(self):
        publish = job(self.text, "publish")
        self.assertIn("contents: write", publish)
        for update_code in (".venv", "update_firmware.py esphome", "update_firmware.py openbeken", "unittest",
                            "fault_suite", "pip install", "npm"):
            self.assertNotIn(update_code, publish)
        push = publish.index("git push")
        self.assertLess(publish.index('update_firmware.py publishable "$GITHUB_SHA" FETCH_HEAD'), push)
        self.assertLess(publish.index('leak_scan.py range "$GITHUB_SHA..FETCH_HEAD"'), push)
        # Run from this commit's checkout: the update's commit is fetched, never checked out.
        self.assertNotRegex(publish, r"git (checkout|switch)")

    def test_both_jobs_name_the_same_branch(self):
        made = re.search(r'branch="(firmware/[^"]+)"', job(self.text, "update")).group(1)
        published = re.search(r"BRANCH: (firmware/.+)", job(self.text, "publish")).group(1)
        self.assertEqual(re.sub(r"\$\{(\w+):-same\}", lambda m: m.group(1).lower(), made),
                         re.sub(r"\$\{\{ needs\.due\.outputs\.(\w+) \|\| 'same' \}\}", r"\1", published))

    def test_only_a_pull_request_it_opened_is_merged(self):
        merge = job(self.text, "merge")
        listed = merge[merge.index("gh pr list"):merge.index("; do")]
        self.assertIn("--author app/github-actions", listed)
        self.assertIn("select(.isCrossRepository | not)", listed)
        self.assertIn('select(.headRefName | startswith(\\"firmware/\\"))', listed)
