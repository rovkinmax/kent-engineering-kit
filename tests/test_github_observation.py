from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from workflowkit.github_observation import (
    GitHubObserver,
    ObservationError,
    parse_pr_url,
    read_bytes,
    read_json,
)


URL = "https://github.com/owner/repository/pull/12"
HEAD = "a" * 40
BASE = "b" * 40


def pr(**changes):
    return {
        "url": URL, "state": "OPEN", "headRefOid": HEAD, "baseRefOid": BASE,
        "headRefName": "feature", "baseRefName": "main", "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN", "reviewDecision": "", "mergedAt": None,
        "mergeCommit": None, "statusCheckRollup": [{"name": "test"}], **changes,
    }


def check(**changes):
    return {
        "name": "test", "workflow": "CI", "event": "pull_request", "bucket": "pass",
        "state": "SUCCESS", "link": "https://github.com/owner/repository/actions/runs/1/job/2",
        **changes,
    }


def thread_page(nodes=(), *, more=False, cursor=None):
    return {"data": {"repository": {"pullRequest": {"reviewThreads": {
        "nodes": list(nodes), "pageInfo": {"hasNextPage": more, "endCursor": cursor},
    }}}}}


class FakeGitHub:
    def __init__(self, *, checks=None, first=None, last=None, comments=None, threads=None):
        self.checks = [check()] if checks is None else checks
        self.first = first or pr()
        self.last = last or self.first
        self.comments = [[]] if comments is None else comments
        self.threads = [thread_page()] if threads is None else threads
        self.calls = []
        self.pr_reads = 0

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["pr", "view"]:
            self.pr_reads += 1
            return deepcopy(self.first if self.pr_reads == 1 else self.last)
        if args[:2] == ["pr", "checks"]:
            return deepcopy(self.checks)
        if args[:2] == ["api", "graphql"]:
            return deepcopy(self.threads)
        if any("/issues/" in arg for arg in args):
            return deepcopy(self.comments)
        if args[0] == "api":
            return [[]]
        raise AssertionError(args)


class GitHubObservationTest(unittest.TestCase):
    def assert_pid_gone(self, pid: int) -> None:
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.01)
        self.fail(f"process {pid} remained alive")

    def observe(self, fake):
        return GitHubObserver(Path.cwd(), query=fake).observe_pr(URL)

    def test_external_check_and_event_identity_are_preserved(self):
        fake = FakeGitHub(checks=[
            check(workflow="", name="External security", event=""),
            check(event="push"), check(event="pull_request"),
        ])
        result = self.observe(fake)
        self.assertEqual(result["checks"][0]["workflow_name"], "")
        self.assertEqual(result["checks"][0]["check_name"], "External security")
        self.assertEqual({row["event"] for row in result["checks"]}, {"", "push", "pull_request"})
        self.assertEqual(len(result["checks"]), 3)
        self.assertEqual(fake.pr_reads, 2)

    def test_json_success_does_not_discard_failing_checks(self):
        result = self.observe(FakeGitHub(checks=[check(bucket="fail", state="FAILURE")]))
        self.assertEqual(result["checks"][0]["state"], "FAILURE")

    def test_no_checks_uses_structured_absence_not_error_text(self):
        fake = FakeGitHub(first=pr(statusCheckRollup=[]))
        self.assertEqual(self.observe(fake)["checks"], [])
        self.assertFalse(any(args[:2] == ["pr", "checks"] for args in fake.calls))

    def test_head_or_base_change_rejects_mixed_observation(self):
        for change in ({"headRefOid": "c" * 40}, {"baseRefOid": "d" * 40}):
            with self.subTest(change=change), self.assertRaises(ObservationError) as raised:
                self.observe(FakeGitHub(last=pr(**change)))
            self.assertEqual(raised.exception.code, "source_changed")

    def test_conflict_is_returned_even_with_pending_ci(self):
        result = self.observe(FakeGitHub(
            first=pr(mergeable="CONFLICTING", mergeStateStatus="DIRTY"),
            checks=[check(bucket="pending", state="IN_PROGRESS")],
        ))
        self.assertEqual(result["pr"]["mergeable"], "CONFLICTING")
        self.assertEqual(result["checks"][0]["state"], "IN_PROGRESS")

    def test_merge_during_observation_returns_real_merge_proof(self):
        result = self.observe(FakeGitHub(last=pr(
            state="MERGED", mergedAt="2026-09-22T12:00:00Z", mergeCommit={"oid": "e" * 40},
        )))
        self.assertEqual(result["pr"]["state"], "MERGED")
        self.assertEqual(result["pr"]["mergeCommit"]["oid"], "e" * 40)

    def test_feedback_pages_are_complete_and_bodies_are_not_retained(self):
        def comment(identifier):
            return {
                "id": identifier, "user": {"login": "reviewer"}, "body": "sensitive-example-body",
                "created_at": "2026-09-22T10:00:00Z", "updated_at": "2026-09-22T10:00:00Z",
            }
        result = self.observe(FakeGitHub(comments=[[comment(1)], [comment(2)]]))
        self.assertEqual(len(result["feedback_items"]), 2)
        self.assertNotIn("sensitive-example-body", json.dumps(result))
        self.assertEqual(result["feedback_items"][0]["body_bytes"], 22)

    def test_incomplete_thread_pagination_is_not_success(self):
        with self.assertRaises(ObservationError) as raised:
            self.observe(FakeGitHub(threads=[thread_page(more=True, cursor="unfinished")]))
        self.assertEqual(raised.exception.code, "incomplete_observation")

    def test_graphql_errors_with_partial_data_are_not_success(self):
        page = {**thread_page(), "errors": [{"message": "private diagnostic"}]}
        with self.assertRaises(ObservationError) as raised:
            self.observe(FakeGitHub(threads=[page]))
        self.assertNotIn("private diagnostic", str(raised.exception))

    def test_wrong_pr_identity_and_malformed_rows_are_rejected(self):
        for fake in (
            FakeGitHub(first=pr(url="https://github.com/other/repository/pull/12")),
            FakeGitHub(checks=[None]), FakeGitHub(checks=[check(state=None)]),
            FakeGitHub(checks=[check(workflow=False)]),
            FakeGitHub(comments={"unexpected": []}), FakeGitHub(comments=[]),
        ):
            with self.subTest(fake=fake), self.assertRaises(ObservationError):
                self.observe(fake)

    def test_thread_comment_ids_are_joined_and_nested_pages_are_checked(self):
        thread = {
            "id": "THREAD1", "isResolved": False, "isOutdated": False, "path": "app.py",
            "subjectType": "LINE", "line": 1, "startLine": None, "originalLine": 1,
            "originalStartLine": None,
            "comments": {"nodes": [{"id": "COMMENT1"}],
                         "pageInfo": {"hasNextPage": True, "endCursor": "next"}},
        }
        fake = FakeGitHub(threads=[thread_page([thread])])
        ordinary_query = fake.__call__

        def query(args):
            if "threadId=THREAD1" in args:
                return {"data": {"node": {"comments": {
                    "nodes": [{"id": "COMMENT2"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }}}}
            if any("/pulls/12/comments" in arg for arg in args):
                return [[{
                    "id": index, "node_id": f"COMMENT{index}", "body": "Review",
                    "created_at": "2026-09-22T10:00:00Z", "updated_at": "2026-09-22T10:00:00Z",
                    "commit_id": HEAD, "original_commit_id": HEAD,
                } for index in (1, 2)]]
            return ordinary_query(args)

        result = GitHubObserver(Path.cwd(), query=query).observe_pr(URL)
        comments = [row for row in result["feedback_items"] if row["kind"] == "review_comment"]
        self.assertEqual([row["thread_id"] for row in comments], ["THREAD1", "THREAD1"])
        self.assertEqual(result["feedback_items"][-1]["comment_ids"], ["COMMENT1", "COMMENT2"])

    def test_run_observation_accepts_extra_jobs_but_preserves_failure(self):
        run = {"id": 1, "run_attempt": 2, "head_sha": HEAD, "event": "push",
               "head_branch": "v1.0.0", "name": "Release", "status": "completed",
               "conclusion": "success"}
        jobs = [
            {"id": 10, "name": "Renamed publish", "status": "completed", "conclusion": "success"},
            {"id": 11, "name": "Extra validation", "status": "completed", "conclusion": "failure"},
        ]
        calls = []

        def query(args):
            calls.append(args)
            return [{"total_count": 2, "jobs": jobs}] if "--paginate" in args else deepcopy(run)

        result = GitHubObserver(Path.cwd(), query=query).observe_run("owner/repository", 1, attempt=2)
        self.assertEqual([row["bucket"] for row in result["checks"]], ["pass", "fail"])
        self.assertTrue(any("/attempts/2/jobs" in args[-1] for args in calls))

    def test_incomplete_run_jobs_and_attempt_change_are_rejected(self):
        run = {"id": 1, "run_attempt": 2, "head_sha": HEAD, "event": "push", "head_branch": "v1"}
        for missing in (True, False):
            reads = 0

            def query(args):
                nonlocal reads
                if "--paginate" in args:
                    return [{"total_count": 1 if missing else 0, "jobs": []}]
                reads += 1
                return {**run, "run_attempt": 3 if reads > 1 else 2}

            with self.subTest(missing=missing), self.assertRaises(ObservationError) as raised:
                GitHubObserver(Path.cwd(), query=query).observe_run("owner/repository", 1)
            self.assertEqual(raised.exception.code, "incomplete_observation" if missing else "source_changed")

    def test_url_is_a_canonical_github_pull_request(self):
        self.assertEqual(parse_pr_url(URL), ("owner/repository", 12))
        for url in ("https://example.com/owner/repository/pull/12",
                    URL + "?token=secret", URL + "/files", URL.replace("/12", "/0")):
            with self.subTest(url=url), self.assertRaises(ObservationError):
                parse_pr_url(url)

    def test_transport_does_not_accept_empty_or_malformed_json(self):
        for code in ("pass", "print('not json')"):
            with self.subTest(code=code), self.assertRaises(ObservationError) as raised:
                read_json(sys.executable, Path.cwd(), ["-c", code], timeout=2)
            self.assertEqual(raised.exception.code, "invalid_observation")

    def test_raw_transport_preserves_empty_and_non_utf8_stdout(self):
        self.assertEqual(
            read_bytes(sys.executable, Path.cwd(), ["-c", "pass"]),
            b"",
        )
        self.assertEqual(
            read_bytes(
                sys.executable,
                Path.cwd(),
                [
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'\\x00\\xff')",
                ],
            ),
            b"\x00\xff",
        )

    def test_raw_transport_uses_strict_exit_codes_and_safe_error_hashes(self):
        with self.assertRaises(ObservationError) as raised:
            read_bytes(
                sys.executable,
                Path.cwd(),
                [
                    "-c",
                    (
                        "import sys; sys.stdout.buffer.write(b'out'); "
                        "sys.stderr.buffer.write(b'err'); sys.exit(7)"
                    ),
                ],
            )
        self.assertEqual(raised.exception.code, "query_failed")
        self.assertEqual(raised.exception.exit_code, 7)
        self.assertEqual(
            raised.exception.stdout_sha256,
            "762069bc07a6e1b5df123a5ae7bd91c10daa04694fbaa17fba0cd6a8dcce8f22",
        )
        self.assertEqual(
            raised.exception.stderr_sha256,
            "d9eb253e06987fa74a5d3189f73d9f7a8104cca786fafbb52bc9555972f5477f",
        )
        self.assertEqual(
            read_bytes(
                sys.executable,
                Path.cwd(),
                ["-c", "import sys; sys.exit(1)"],
                accepted_exit_codes=(0, 1, 8),
            ),
            b"",
        )

    def test_json_wrapper_rejects_invalid_utf8_with_safe_hashes(self):
        with self.assertRaises(ObservationError) as raised:
            read_json(
                sys.executable,
                Path.cwd(),
                [
                    "-c",
                    (
                        "import sys; sys.stdout.buffer.write(b'\\xff'); "
                        "sys.stderr.buffer.write(b'diagnostic')"
                    ),
                ],
            )
        self.assertEqual(raised.exception.code, "invalid_observation")
        self.assertEqual(
            raised.exception.stdout_sha256,
            "a8100ae6aa1940d0b663bb31cd466142ebbdbd5187131b92d93818987832eb89",
        )
        self.assertEqual(
            raised.exception.stderr_sha256,
            "5a695eea5b00a31f8aef7dbb89c8f798fab371246ac1549afe84b16420707b99",
        )

    def test_transport_limits_are_distinct_from_format_errors(self):
        with self.assertRaises(ObservationError) as raised:
            read_json(sys.executable, Path.cwd(), ["-c", "print('x' * 4096)"], timeout=2, limit=1024)
        self.assertEqual(raised.exception.code, "observation_limit")
        with self.assertRaises(ObservationError) as raised:
            read_json(sys.executable, Path.cwd(), ["-c", "import time; time.sleep(5)"], timeout=0.05)
        self.assertEqual(raised.exception.code, "query_timeout")

    def test_raw_transport_output_and_time_bounds(self):
        with self.assertRaises(ObservationError) as raised:
            read_bytes(
                sys.executable,
                Path.cwd(),
                ["-c", "print('x' * 4096)"],
                timeout=2,
                limit=1024,
            )
        self.assertEqual(raised.exception.code, "observation_limit")
        with self.assertRaises(ObservationError) as raised:
            read_bytes(
                sys.executable,
                Path.cwd(),
                ["-c", "import time; time.sleep(5)"],
                timeout=0.05,
            )
        self.assertEqual(raised.exception.code, "query_timeout")

    def test_raw_transport_reaps_descendant_holding_pipes_after_parent_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_path = Path(temporary) / "child.pid"
            child = (
                "import pathlib, signal, subprocess, sys, time; "
                "p = subprocess.Popen([sys.executable, '-c', "
                " 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)']); "
                "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); "
                "sys.exit(0)"
            )
            with self.assertRaises(ObservationError) as raised:
                read_bytes(
                    sys.executable,
                    Path.cwd(),
                    ["-c", child, str(pid_path)],
                    timeout=0.05,
                )
            self.assertEqual(raised.exception.code, "query_timeout")
            self.assertTrue(pid_path.exists())
            self.assert_pid_gone(int(pid_path.read_text()))

    def test_check_transport_accepts_status_exit_codes_but_not_other_failures(self):
        command = [
            sys.executable,
            "-c",
            "import json,sys; print(json.dumps([])); sys.exit(1)",
        ]
        self.assertEqual(
            read_json(
                sys.executable,
                Path.cwd(),
                command[1:],
                timeout=2,
                accepted_exit_codes=(0, 1, 8),
            ),
            [],
        )
        for code, output in ((2, "[]"), (1, "not-json")):
            with self.subTest(code=code, output=output), self.assertRaises(
                ObservationError
            ):
                read_json(
                    sys.executable,
                    Path.cwd(),
                    [
                        "-c",
                        (
                            "import sys; print("
                            + repr(output)
                            + "); sys.exit("
                            + str(code)
                            + ")"
                        ),
                    ],
                    timeout=2,
                    accepted_exit_codes=(0, 1, 8),
                )

    def test_bounded_stdin_for_report_archiver(self):
        value = {"operation": "archive_ci_report"}
        raw = read_bytes(
            sys.executable, Path.cwd(),
            ["-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
            timeout=2, input_bytes=json.dumps(value).encode(),
        )
        self.assertEqual(json.loads(raw), value)
        result = read_json(
            sys.executable, Path.cwd(),
            ["-c", "import json,sys; print(json.dumps(json.load(sys.stdin)))"],
            timeout=2, input_bytes=json.dumps(value).encode(),
        )
        self.assertEqual(result, value)
        with self.assertRaises(ObservationError) as raised:
            read_bytes(
                sys.executable,
                Path.cwd(),
                ["-c", "pass"],
                input_bytes=b"x" * (1024 * 1024 + 1),
            )
        self.assertEqual(raised.exception.code, "observation_limit")
        with self.assertRaises(ObservationError) as raised:
            read_json(sys.executable, Path.cwd(), ["-c", "pass"], input_bytes=b"x" * (1024 * 1024 + 1))
        self.assertEqual(raised.exception.code, "observation_limit")


if __name__ == "__main__":
    unittest.main()
