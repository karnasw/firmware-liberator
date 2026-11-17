"""api_health: the public figures fresh, and the account within the free plan."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from tools.repo import api_health as health

NOW = dt.datetime(2026, 11, 10, 9, 0, tzinfo=dt.timezone.utc)


def figures(**changes):
    return {"format": 1, "generated": "2026-11-10T03:17Z", "schema": health.latest_migration(), **changes}


def run(*args, snapshot=None, analytics=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch.object(health, "fetch_snapshot", return_value=snapshot or figures()), \
            mock.patch.object(health.cs, "api", return_value=[{"account": {"id": "a1"}}]), \
            mock.patch.object(health, "graphql", return_value=analytics):
        code = health.main(list(args), now=NOW)
    return code, out.getvalue() + err.getvalue()


def analytics(requests=1000, rows_read=10_000, rows_written=500, d1_bytes=10_000_000, r2_bytes=1_000_000_000,
              class_a=100, class_b=1000):
    return {"viewer": {"accounts": [{
        "workers": [{"sum": {"requests": requests}}],
        "d1": [{"sum": {"rowsRead": rows_read, "rowsWritten": rows_written}}],
        "d1Storage": [{"max": {"databaseSizeBytes": d1_bytes}, "dimensions": {"databaseId": "d"}}],
        "r2Storage": [{"max": {"payloadSize": r2_bytes, "metadataSize": 0}, "dimensions": {"bucketName": "b"}}],
        "r2Operations": [{"sum": {"requests": class_a}, "dimensions": {"actionType": "PutObject"}},
                         {"sum": {"requests": class_b}, "dimensions": {"actionType": "GetObject"}},
                         {"sum": {"requests": 99}, "dimensions": {"actionType": "DeleteObject"}}],
    }]}}


class SnapshotTests(unittest.TestCase):
    def test_fresh_figures_of_todays_schema_pass(self):
        code, out = run("snapshot")
        self.assertEqual(code, 0, out)
        self.assertIn("fresh", out)

    def test_figures_a_night_missed_fail(self):
        self.assertEqual(run("snapshot", snapshot=figures(generated="2026-11-09T09:00Z"))[0], 0, "a day old")
        code, out = run("snapshot", snapshot=figures(generated="2026-11-09T07:59Z"))
        self.assertEqual(code, 1)
        self.assertIn("the nightly job has not run", out)

    def test_a_night_missed_is_seen_at_the_workflows_hour(self):
        # The last figures from the night before, checked at 06:00 after a night missed.
        at_six = dt.datetime(2026, 11, 10, 6, 0, tzinfo=dt.timezone.utc)
        self.assertTrue(health.snapshot_problems(figures(generated="2026-11-09T03:17Z"), at_six))
        self.assertFalse(health.snapshot_problems(figures(generated="2026-11-10T03:17Z"), at_six))
        workflow = (Path(health.ROOT) / ".github/workflows/cloudflare-settings.yml").read_text(encoding="utf-8")
        self.assertIn('- cron: "0 6 * * *"', workflow)
        api = (Path(health.ROOT) / "website/api/wrangler.jsonc").read_text(encoding="utf-8")
        self.assertIn('"crons": ["17 3 * * *", "47 * * * *"]', api)

    def test_figures_from_a_database_short_of_a_migration_say_to_apply_it(self):
        code, out = run("snapshot", snapshot=figures(schema="0001_counters.sql"))
        self.assertEqual(code, 1)
        self.assertIn("run python3 tools/repo/cloudflare_settings.py apply", out)

    def test_figures_not_as_written_fail(self):
        for changed in ({"format": 2}, {"generated": "yesterday"}, {"generated": None}):
            self.assertEqual(run("snapshot", snapshot=figures(**changed))[0], 1, changed)

    def test_figures_not_there_fail_plainly(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out), mock.patch.object(
                health.urllib.request, "urlopen", side_effect=OSError("Name or service not known")):
            self.assertEqual(health.main(["snapshot"], now=NOW), 1)
        self.assertIn(f"{health.SNAPSHOT_URL}: Name or service not known", out.getvalue())

    def test_its_time_is_written_as_the_nightly_job_writes_it(self):
        snapshot = (Path(health.ROOT) / "website/api/snapshot.js").read_text(encoding="utf-8")
        self.assertIn('generated: now.toISOString().slice(0, 16) + "Z"', snapshot)


class UsageTests(unittest.TestCase):
    def test_well_within_each_limit_passes_and_says_each(self):
        code, out = run("usage", analytics=analytics())
        self.assertEqual(code, 0, out)
        for what in health.INCLUDED:
            self.assertIn(what, out)

    def test_half_of_anything_included_fails(self):
        cases = {"requests": 5_000_000, "rows_read": 12_500_000_000, "rows_written": 25_000_000,
                 "d1_bytes": 2_500_000_000, "r2_bytes": 5_000_000_000, "class_a": 500_000, "class_b": 5_000_000}
        for key, value in cases.items():
            code, out = run("usage", analytics=analytics(**{key: value}))
            self.assertEqual(code, 1, key)
            self.assertIn("past 50%", out, key)

    def test_operations_are_counted_by_class_and_deletes_are_free(self):
        measures = health.used(analytics(class_a=7, class_b=11))
        self.assertEqual(measures["R2 Class A operations this month"], 7)
        self.assertEqual(measures["R2 Class B operations this month"], 11)

    def test_storage_is_each_buckets_most_added_together(self):
        rows = [{"max": {"payloadSize": 5, "metadataSize": 1}, "dimensions": {"bucketName": "a"}},
                {"max": {"payloadSize": 9, "metadataSize": 0}, "dimensions": {"bucketName": "a"}},
                {"max": {"payloadSize": 2, "metadataSize": 0}, "dimensions": {"bucketName": "b"}}]
        self.assertEqual(health.held(rows, ("payloadSize", "metadataSize"), "bucketName"), 11)
        self.assertEqual(health.held(None, ("payloadSize",), "bucketName"), 0)

    def test_a_token_that_sees_no_account_says_what_it_needs(self):
        code, out = run("usage", analytics={"viewer": {"accounts": []}})
        self.assertEqual(code, 1)
        self.assertIn("Account Analytics: Read", out)

    def test_an_analytics_error_is_said_in_full(self):
        answer = io.BytesIO(json.dumps({"data": None, "errors": [{"message": "unknown field d1StorageAdaptiveGroups"}]})
                            .encode())
        out = io.StringIO()
        with contextlib.redirect_stderr(out), mock.patch.object(health.cs, "token", return_value="t"), \
                mock.patch.object(health.cs, "api", return_value=[{"account": {"id": "a1"}}]), \
                mock.patch.object(health.urllib.request, "urlopen", return_value=answer):
            self.assertEqual(health.main(["usage"], now=NOW), 1)
        self.assertIn("unknown field d1StorageAdaptiveGroups", out.getvalue())

    def test_it_asks_for_this_month_to_yesterday(self):
        asked = {}
        with mock.patch.object(health.cs, "api", return_value=[{"account": {"id": "a1"}}]), \
                mock.patch.object(health, "graphql", side_effect=lambda query, variables: asked.update(variables)
                                  or analytics()):
            health.usage(NOW)
        self.assertEqual(asked, {"account": "a1", "day": "2026-11-09", "firstDay": "2026-11-01",
                                 "from": "2026-11-09T00:00:00Z", "to": "2026-11-10T00:00:00Z",
                                 "month": "2026-11-01T00:00:00Z"})
        # On the 1st, the month before, whole.
        asked.clear()
        with mock.patch.object(health.cs, "api", return_value=[{"account": {"id": "a1"}}]), \
                mock.patch.object(health, "graphql", side_effect=lambda query, variables: asked.update(variables)
                                  or analytics()):
            health.usage(dt.datetime(2026, 12, 1, 6, 0, tzinfo=dt.timezone.utc))
        self.assertEqual((asked["firstDay"], asked["day"]), ("2026-11-01", "2026-11-30"))


class WorkflowTests(unittest.TestCase):
    def test_both_are_checked_daily_by_the_settings_workflow_and_not_on_a_push(self):
        workflow = (Path(health.ROOT) / ".github/workflows/cloudflare-settings.yml").read_text(encoding="utf-8")
        job = workflow[workflow.index("\n  health:"):]
        self.assertIn("if: github.event_name != 'push'", job)
        self.assertIn("run: python3 tools/repo/api_health.py snapshot", job)
        self.assertIn("run: python3 tools/repo/api_health.py usage", job)
        self.assertIn("run: python3 tools/repo/api_health.py mail", job)
        self.assertIn("CLOUDFLARE_SETTINGS_CHECK_TOKEN: ${{ secrets.CLOUDFLARE_SETTINGS_CHECK_TOKEN }}", job)

    def test_the_read_token_is_given_what_usage_needs(self):
        """As cloudflare_settings.py lists what to give CI's read token: the
        account's analytics for `usage`; the zone's, and the account's
        suppression list, for `mail`."""
        given = health.cs.TOKENS["settings-check-read-only"]
        self.assertIn("api_health.py usage", given["account"]["Account Analytics Read"])
        self.assertIn("api_health.py mail", given["zone"]["Analytics Read"])
        self.assertIn("api_health.py mail", given["account"]["Email Sending Read"])


def delivered(**counts):
    return {"viewer": {"zones": [{"emailSendingAdaptiveGroups": [
        {"count": n, "dimensions": {"status": status}} for status, n in counts.items()]}]}}


def suppressed(*rows):
    return [{"id": f"s{n}", "email": f"owner{n}@example.com", "reason": reason, "created_at": at}
            for n, (reason, at) in enumerate(rows)]


def mail_run(analytics, listed=(), asked=None):
    def api(path, **_):
        if path.startswith("/zones?"):
            return [{"id": "z1", "account": {"id": "a1"}}]
        if asked is not None:
            asked.append(path)
        return list(listed)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch.object(health.cs, "api", side_effect=api), \
            mock.patch.object(health, "graphql", return_value=analytics):
        code = health.main(["mail"], now=NOW)
    return code, out.getvalue() + err.getvalue()


class MailTests(unittest.TestCase):
    def test_few_bounces_and_no_complaint_pass_and_say_the_counts(self):
        code, out = mail_run(delivered(delivered=400, deliveryFailed=7),
                             suppressed(("hard_bounce", "2026-11-09T20:00:00Z")))
        self.assertEqual(code, 0, out)
        self.assertIn("400 emails delivered, 7 bounced", out)
        self.assertIn("1 for a hard bounce, 0 for a complaint", out)

    def test_bounces_past_two_percent_fail(self):
        code, out = mail_run(delivered(delivered=200, deliveryFailed=8))
        self.assertEqual(code, 1)
        self.assertIn("8 of 208 emails in 7 days bounced (3.8%), past 2%", out)

    def test_one_mistyped_address_among_few_emails_does_not_fail(self):
        self.assertEqual(mail_run(delivered(delivered=3, deliveryFailed=4))[0], 0)

    def test_any_recent_complaint_fails_and_one_older_does_not(self):
        code, out = mail_run(delivered(delivered=10), suppressed(("complaint", "2026-11-09T08:00:00Z")))
        self.assertEqual(code, 1)
        self.assertIn("1 marked as spam in 2 days", out)
        self.assertEqual(mail_run(delivered(delivered=10), suppressed(("complaint", "2026-11-01T08:00:00Z")))[0], 0)

    def test_it_never_prints_an_address(self):
        _, out = mail_run(delivered(delivered=10, deliveryFailed=9), suppressed(
            ("complaint", "2026-11-09T08:00:00Z"), ("hard_bounce", "2026-11-09T08:00:00Z")))
        self.assertNotIn("@", out)

    def test_it_reads_every_page_of_the_suppression_list(self):
        asked = []
        full = suppressed(*[("manual", "2026-01-01T00:00:00Z")] * health.SUPPRESSIONS_PAGE)
        pages = iter([full, suppressed(("complaint", "2026-11-09T08:00:00Z"))])
        def api(path, **_):
            if path.startswith("/zones?"):
                return [{"id": "z1", "account": {"id": "a1"}}]
            asked.append(path)
            return next(pages)
        with mock.patch.object(health.cs, "api", side_effect=api), \
                mock.patch.object(health, "graphql", return_value=delivered()):
            self.assertEqual(health.mail(NOW)["complaints"], 1)
        self.assertEqual(asked, [f"/accounts/a1/email/sending/suppressions?per_page={health.SUPPRESSIONS_PAGE}&page=1",
                                 f"/accounts/a1/email/sending/suppressions?per_page={health.SUPPRESSIONS_PAGE}&page=2"])

    def test_it_asks_the_zones_analytics_for_the_last_days(self):
        asked = {}
        with mock.patch.object(health.cs, "api", side_effect=lambda path, **_: (
                [{"id": "z1", "account": {"id": "a1"}}] if path.startswith("/zones?") else [])), \
                mock.patch.object(health, "graphql", side_effect=lambda query, variables: asked.update(variables)
                                  or delivered()):
            health.mail(NOW)
        self.assertEqual(asked, {"zone": "z1", "start": "2026-11-03", "end": "2026-11-10"})

    def test_a_token_without_the_zones_analytics_says_what_it_needs(self):
        code, out = mail_run({"viewer": {"zones": []}})
        self.assertEqual(code, 1)
        self.assertIn("give it the zone's Analytics: Read", out)


class LiveTests(unittest.TestCase):
    """What a just-deployed API answers; the Worker's own answers are in website/test/."""

    def answers(self, confirm, wanted, ingest="on"):
        asked = []
        def ask(path, body=None):
            asked.append((path, body))
            return confirm if path.startswith("/confirm") else wanted
        with mock.patch.object(health, "ask", side_effect=ask):
            return health.live_problems(ingest), asked

    def test_a_migrated_worker_taking_requests_passes(self):
        problems, asked = self.answers((404, "<h1>Nothing to confirm</h1>"),
                                       (400, '{"error":"that is not a question this tool makes"}'))
        self.assertEqual(problems, [])
        body = json.loads(asked[1][1])
        self.assertNotEqual(body["format"], 1, "a format no tool sends, so nothing is counted")

    def test_the_old_worker_or_a_database_short_of_a_migration_fails_and_says_why(self):
        problems, _ = self.answers((404, '{"error":"readouts are sent to /readout, by the tool"}'),
                                   (503, '{"error":"the API is being updated: try again later"}'))
        self.assertEqual(len(problems), 2)
        self.assertIn("cloudflare_settings.py apply", problems[1])

    def test_deployed_taking_no_requests_each_answers_503(self):
        off = (503, '{"error":"not taking requests for now: try again later"}')
        self.assertEqual(self.answers(off, off, "off")[0], [])
        self.assertEqual(len(self.answers((404, "Nothing to confirm"), off, "off")[0]), 1)

    def test_the_post_carries_the_light_proof_the_worker_asks(self):
        sent = {}
        def urlopen(request, timeout):
            sent.update(request.headers)
            sent["body"] = request.data
            raise health.urllib.error.HTTPError(request.full_url, 400, "", {}, io.BytesIO(b"{}"))
        with mock.patch.object(health.urllib.request, "urlopen", side_effect=urlopen), \
                mock.patch.object(health, "light_proof_bits", return_value=4):
            self.assertEqual(health.ask("/v1/wanted", b'{"format": 0}')[0], 400)
        self.assertEqual(sent["Content-type"], "application/json")
        stamp, nonce = sent["X-proof"].split(":")
        subject = health.proof_of_work.body_subject(sent["body"])
        digest = health.proof_of_work.hashlib.sha256(f"{stamp}:{subject}:{nonce}".encode()).digest()
        self.assertEqual(digest[0] >> 4, 0, "four leading zero bits, as asked, for this body")

    def test_it_reads_the_proofs_bits_from_the_worker(self):
        self.assertEqual(health.light_proof_bits(), 20)

    def test_it_tries_again_while_the_new_version_spreads(self):
        results = iter([["old"], ["old"], []])
        with mock.patch.object(health, "live_problems", side_effect=lambda ingest: next(results)), \
                mock.patch.object(health.time, "sleep"):
            self.assertEqual(health.live("on", wait=90, every=10), [])
        with mock.patch.object(health, "live_problems", return_value=["old"]), \
                mock.patch.object(health.time, "sleep"), \
                mock.patch.object(health.time, "monotonic", side_effect=[0, 0, 85]):
            self.assertEqual(health.live("on", wait=90, every=10), ["old"])

    def test_it_is_the_api_deploys_last_step(self):
        workflow = (Path(health.ROOT) / ".github/workflows/website.yml").read_text(encoding="utf-8")
        job = workflow[workflow.index("\n  deploy-api:"):workflow.index("\n  deploy-token:")]
        self.assertLess(job.index("npx wrangler deploy --config api/wrangler.jsonc"),
                        job.index('run: python3 tools/repo/api_health.py live "$INGEST"'))


class DeployedTests(unittest.TestCase):
    """The release waits for the API its commit holds."""

    def test_the_tree_is_this_commits_website_api(self):
        import subprocess
        self.assertEqual(health.tree_here(), subprocess.run(
            ["git", "-C", str(health.ROOT), "rev-parse", "HEAD:website/api"], capture_output=True, text=True,
            check=True).stdout.strip())

    def test_the_tree_is_read_from_any_answer(self):
        def urlopen(request, timeout):
            self.assertEqual(request.full_url, health.API_URL + "/confirm?t=not-a-token")
            raise health.urllib.error.HTTPError(request.full_url, 404, "", {"API-Tree": "a" * 40}, io.BytesIO(b""))
        with mock.patch.object(health.urllib.request, "urlopen", side_effect=urlopen):
            self.assertEqual(health.tree_deployed(), "a" * 40)
        with mock.patch.object(health.urllib.request, "urlopen", side_effect=OSError("offline")):
            self.assertEqual(health.tree_deployed(), "")

    def test_another_tree_is_a_problem_and_this_one_is_held_to_live(self):
        with mock.patch.object(health, "tree_deployed", return_value="b" * 40), \
                mock.patch.object(health, "live_problems") as live:
            problems = health.deployed_problems("a" * 40)
        self.assertEqual(len(problems), 1)
        self.assertIn("b" * 40, problems[0])
        live.assert_not_called()
        with mock.patch.object(health, "tree_deployed", return_value="a" * 40), \
                mock.patch.object(health, "live_problems", return_value=["not migrated"]) as live:
            self.assertEqual(health.deployed_problems("a" * 40), ["not migrated"])
        live.assert_called_once_with("on")

    def test_it_waits_for_the_deploy(self):
        results = iter([["old"], ["old"], []])
        with mock.patch.object(health, "deployed_problems", side_effect=lambda tree: next(results)), \
                mock.patch.object(health.time, "sleep") as slept:
            self.assertEqual(health.deployed("a" * 40), [])
        self.assertEqual(slept.call_count, 2)
        with mock.patch.object(health, "deployed_problems", return_value=["old"]), \
                mock.patch.object(health.time, "sleep"), \
                mock.patch.object(health.time, "monotonic", side_effect=[0, 0, health.DEPLOYED_WAIT]):
            self.assertEqual(health.deployed("a" * 40), ["old"])

    def test_main_says_which_and_fails_on_a_problem(self):
        with mock.patch.object(health, "tree_here", return_value="a" * 40), \
                mock.patch.object(health, "deployed", return_value=[]), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(health.main(["deployed"]), 0)
        self.assertIn("a" * 40, out.getvalue())
        with mock.patch.object(health, "tree_here", return_value="a" * 40), \
                mock.patch.object(health, "deployed", return_value=["old"]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(health.main(["deployed"]), 1)

    def test_the_deploy_names_its_tree_and_the_release_waits_for_it(self):
        root = Path(health.ROOT)
        website = (root / ".github/workflows/website.yml").read_text(encoding="utf-8")
        job = website[website.index("\n  deploy-api:"):website.index("\n  deploy-admin:")]
        self.assertIn('--var "API_TREE:$(git rev-parse HEAD:website/api)"', job)
        release = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        api = release[release.index("\n  api:"):release.index("\n  release:")]
        self.assertIn("run: python3 tools/repo/api_health.py deployed", api)
        self.assertRegex(release[release.index("\n  release:"):], r"\n    needs: \[[^]]*\bapi\]")
        self.assertIn('named.headers.set("API-Tree", env.API_TREE);',
                      (root / "website/api/index.js").read_text(encoding="utf-8"))
