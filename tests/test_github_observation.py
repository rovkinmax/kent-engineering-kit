from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from workflowkit import github_observation as observation

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


class OwnedGroupSettlementTest(unittest.TestCase):
    def process(self):
        process = mock.Mock()
        process.pid = 424242
        process.returncode = 0
        process.poll.return_value = 0
        return process

    def site(self, process, site):
        if site == "probe":
            return observation._signal_owned_group(process, 0)
        observation._stop_process(process)
        return False

    def prefix(self, site):
        return [None] if site == "kill" else []

    def expected_operations(self, site):
        return {"term": [signal.SIGTERM, 0], "kill": [signal.SIGTERM, signal.SIGKILL, 0],
                "probe": [0, 0]}[site]

    def test_confirmed_absence_settles_each_site_without_another_signal(self):
        for site in ("term", "kill", "probe"):
            with self.subTest(site=site):
                process = self.process()
                denied = PermissionError(1, "fixture permission denial")
                with mock.patch.object(observation.os, "killpg",
                    side_effect=self.prefix(site) + [denied, ProcessLookupError()]) as signals:
                    self.assertFalse(self.site(process, site))
                self.assertEqual([call.args[1] for call in signals.call_args_list],
                                 self.expected_operations(site))
                self.assertEqual(process.wait.call_count, int(site == "kill"))
                self.assertTrue(process.poll.called)

    def test_accessible_survivor_preserves_original_denial_at_each_site(self):
        for site in ("term", "kill", "probe"):
            with self.subTest(site=site):
                denied = PermissionError(1, "fixture original permission denial")
                with mock.patch.object(observation.os, "killpg",
                    side_effect=self.prefix(site) + [denied, None]) as signals:
                    with self.assertRaises(PermissionError) as raised:
                        self.site(self.process(), site)
                self.assertIs(raised.exception, denied)
                self.assertEqual([call.args[1] for call in signals.call_args_list],
                                 self.expected_operations(site))

    def test_persistent_denial_has_a_probe_bound_at_each_site(self):
        for site in ("term", "kill", "probe"):
            with self.subTest(site=site):
                denied = PermissionError(1, "fixture original permission denial")
                with mock.patch.object(observation.os, "killpg",
                    side_effect=self.prefix(site) + [denied] * 41) as signals, \
                     mock.patch.object(observation.time, "monotonic", return_value=100.0), \
                     mock.patch.object(observation.time, "sleep") as sleep:
                    with self.assertRaises(PermissionError) as raised:
                        self.site(self.process(), site)
                self.assertIs(raised.exception, denied)
                self.assertEqual(signals.call_count, len(self.prefix(site)) + 41)
                self.assertEqual(sleep.call_count, 40)
                self.assertEqual([call.args[1] for call in signals.call_args_list[1 + len(self.prefix(site)):]],
                                 [0] * 40)

    def test_persistent_denial_has_a_monotonic_deadline_at_each_site(self):
        for site in ("term", "kill", "probe"):
            with self.subTest(site=site):
                denied = PermissionError(1, "fixture original permission denial")
                with mock.patch.object(observation.os, "killpg",
                    side_effect=self.prefix(site) + [denied]), \
                     mock.patch.object(observation.time, "monotonic",
                                       side_effect=[100.0, 100.2]), \
                     mock.patch.object(observation.time, "sleep") as sleep:
                    with self.assertRaises(PermissionError) as raised:
                        self.site(self.process(), site)
                self.assertIs(raised.exception, denied)
                sleep.assert_not_called()

    def test_ordinary_absence_and_other_errors_remain_distinct(self):
        for site in ("term", "kill", "probe"):
            with self.subTest(site=site, outcome="absent"):
                with mock.patch.object(observation.os, "killpg",
                    side_effect=self.prefix(site) + [ProcessLookupError()]) as signals:
                    self.assertFalse(self.site(self.process(), site))
                self.assertEqual(signals.call_count, 1 + len(self.prefix(site)))
            with self.subTest(site=site, outcome="unexpected"):
                unexpected = OSError(5, "fixture IO error")
                with mock.patch.object(observation.os, "killpg",
                    side_effect=self.prefix(site) + [unexpected]):
                    with self.assertRaises(OSError) as raised:
                        self.site(self.process(), site)
                self.assertIs(raised.exception, unexpected)

    def test_unknown_probe_error_does_not_replace_original_denial(self):
        for site in ("term", "kill", "probe"):
            with self.subTest(site=site):
                denied = PermissionError(1, "fixture original permission denial")
                with mock.patch.object(observation.os, "killpg",
                    side_effect=self.prefix(site) + [denied, OSError(5, "probe")]):
                    with self.assertRaises(PermissionError) as raised:
                        self.site(self.process(), site)
                self.assertIs(raised.exception, denied)

    def test_transport_closes_pipes_and_preserves_each_site_exception(self):
        class EmptySelector:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def register(self, *_):
                pass

            def get_map(self):
                return {}

        for site in ("term", "kill", "probe"):
            with self.subTest(site=site):
                process = self.process()
                denied = PermissionError(1, "fixture original permission denial")
                process.wait.side_effect = [None] if site == "probe" else [
                    subprocess.TimeoutExpired("fixture", 1), None,
                ]
                with mock.patch.object(observation.subprocess, "Popen", return_value=process), \
                     mock.patch.object(observation.selectors, "DefaultSelector", EmptySelector), \
                     mock.patch.object(observation.os, "killpg",
                                       side_effect=self.prefix(site) + [denied, None]):
                    with self.assertRaises(PermissionError) as raised:
                        observation.read_bytes("fixture", Path.cwd(), [], timeout=1)
                self.assertIs(raised.exception, denied)
                process.stdout.close.assert_called_once()
                process.stderr.close.assert_called_once()

    def test_settled_cleanup_preserves_timeout_and_observation_hashes(self):
        process = self.process()
        process.wait.side_effect = subprocess.TimeoutExpired("fixture", 1)
        denied = PermissionError(1, "fixture original permission denial")
        with mock.patch.object(observation.subprocess, "Popen", return_value=process), \
             mock.patch.object(observation.selectors, "DefaultSelector") as selector, \
             mock.patch.object(observation.os, "killpg", side_effect=[denied, ProcessLookupError()]):
            selector.return_value.__enter__.return_value.get_map.return_value = {}
            with self.assertRaises(ObservationError) as raised:
                observation.read_bytes("fixture", Path.cwd(), [], timeout=1)
        self.assertEqual(raised.exception.code, "query_timeout")
        self.assertEqual(raised.exception.stdout_sha256,
                         "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
        self.assertEqual(raised.exception.stderr_sha256, raised.exception.stdout_sha256)
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()

    def test_settled_cleanup_preserves_output_limit_and_query_failure(self):
        for failure in ("observation_limit", "query_failed"):
            with self.subTest(failure=failure):
                process = self.process()
                denied = PermissionError(1, "fixture original permission denial")
                output = b"xy" if failure == "observation_limit" else b""
                operations = [denied, ProcessLookupError()]
                if failure == "query_failed":
                    operations.insert(0, None)
                with mock.patch.object(observation.subprocess, "Popen", return_value=process), \
                     mock.patch.object(observation.selectors, "DefaultSelector") as selector, \
                     mock.patch.object(observation.os, "killpg", side_effect=operations) as signals, \
                     mock.patch.object(observation.os, "read", return_value=b"xyz"):
                    active = selector.return_value.__enter__.return_value
                    active.get_map.return_value = failure == "observation_limit"
                    active.select.return_value = [
                        (type("Event", (), {"fileobj": process.stdout, "data": "stdout"})(), 1),
                    ]
                    with self.assertRaises(ObservationError) as raised:
                        observation.read_bytes("fixture", Path.cwd(), [], timeout=1, limit=1)
                self.assertEqual(raised.exception.code, failure)
                self.assertEqual(raised.exception.stdout_sha256, observation.hashlib.sha256(output).hexdigest())
                self.assertEqual(raised.exception.stderr_sha256, observation.hashlib.sha256(b"").hexdigest())
                self.assertNotIn(signal.SIGKILL, [call.args[1] for call in signals.call_args_list])
                process.stdout.close.assert_called_once()
                process.stderr.close.assert_called_once()


class ProviderRetryMetadataTransportTest(unittest.TestCase):
    @staticmethod
    def response(
        status: int,
        *,
        headers: dict[str, str] | None = None,
        body: str = "",
        completed: bool = True,
    ) -> bytes:
        lines = [
            "* Request at 2026-10-22 00:00:00 +0000 UTC",
            "* Request to https://api.github.com/graphql",
            "> Authorization: token secret-marker",
            "> Retry-After: 9999999999",
            "> X-RateLimit-Remaining: 0",
            f"< HTTP/2.0 {status} response",
        ]
        lines.extend(
            f"< {name}: {value}" for name, value in (headers or {}).items()
        )
        lines.extend(["", body])
        if completed:
            lines.append("* Request took 0.001s")
        return ("\n".join(lines) + "\n").encode()

    def test_retry_after_is_extracted_and_raw_trace_is_not_exposed(self):
        trace = self.response(
            429,
            headers={"Retry-After": "120"},
            body='{"message":"SECRET provider response body"}',
        )
        self.assertEqual(observation._provider_retry_metadata(trace), (True, 120))
        error = ObservationError(
            "query_failed",
            stderr=trace,
            rate_limited=True,
            retry_after_seconds=120,
        )
        self.assertEqual(error.retry_delay_seconds(interval=30, failure_count=1), 120)
        safe = repr(error.as_dict())
        self.assertNotIn("secret-marker", safe)
        self.assertNotIn("SECRET provider response body", safe)
        self.assertNotIn("Retry-After", safe)
        self.assertEqual(str(error), "query_failed")

    def test_primary_limit_uses_reset_and_provider_date(self):
        trace = self.response(
            403,
            headers={
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "1060",
                "Date": "Thu, 01 Jan 1970 00:16:40 GMT",
            },
            body='{"message":"API rate limit exceeded"}',
        )
        with mock.patch.object(observation.time, "time", return_value=1000):
            self.assertEqual(observation._provider_retry_metadata(trace), (True, 60))

    def test_secondary_without_timing_uses_fallback_and_invalid_timing_fails_closed(self):
        secondary = self.response(
            403,
            headers={"X-RateLimit-Remaining": "12"},
            body='{"message":"You have exceeded a secondary rate limit."}',
        )
        self.assertEqual(observation._provider_retry_metadata(secondary), (True, None))
        error = ObservationError("query_failed", rate_limited=True)
        self.assertEqual(error.retry_delay_seconds(interval=30, failure_count=1), 60)
        self.assertEqual(error.retry_delay_seconds(interval=30, failure_count=2), 120)
        self.assertEqual(error.retry_delay_seconds(interval=30, failure_count=20), 3600)
        self.assertEqual(
            ObservationError("query_failed").retry_delay_seconds(
                interval=30,
                failure_count=1,
            ),
            30,
        )

        invalid = self.response(
            403,
            headers={
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "not-an-epoch",
                "Date": "invalid date",
            },
            body='{"message":"API rate limit exceeded"}',
        )
        self.assertEqual(observation._provider_retry_metadata(invalid), (None, None))

        invalid_retry_after = self.response(
            429,
            headers={"Retry-After": "invalid"},
            body='{"message":"You have exceeded a secondary rate limit."}',
        )
        self.assertEqual(
            observation._provider_retry_metadata(invalid_retry_after),
            (None, None),
        )

    def test_http_date_retry_after_is_supported(self):
        trace = self.response(
            429,
            headers={"Retry-After": "Thu, 01 Jan 1970 00:17:40 GMT"},
        )
        with mock.patch.object(observation.time, "time", return_value=1000):
            self.assertEqual(observation._provider_retry_metadata(trace), (True, 60))

    def test_permission_403_and_successful_empty_quota_are_not_retries(self):
        permission = self.response(
            403,
            headers={"X-RateLimit-Remaining": "10"},
            body='{"message":"Resource not accessible by integration"}',
        )
        invalid_retry = self.response(
            403,
            headers={
                "Retry-After": "invalid date",
                "X-RateLimit-Remaining": "10",
            },
            body='{"message":"Resource not accessible by integration"}',
        )
        success = self.response(
            200,
            headers={
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "9999999999",
            },
            body='{"data":{"viewer":{"login":"user"}}}',
        )
        self.assertEqual(observation._provider_retry_metadata(permission), (False, None))
        self.assertEqual(observation._provider_retry_metadata(success), (False, None))

    def test_only_final_complete_response_is_considered(self):
        throttled_then_success = self.response(
            429,
            headers={"Retry-After": "120"},
        ) + self.response(200, body='{"data":{}}')
        success_then_throttled = self.response(200, body='{"data":{}}') + self.response(
            429,
            headers={"Retry-After": "120"},
        )
        self.assertEqual(
            observation._provider_retry_metadata(throttled_then_success),
            (False, None),
        )
        self.assertEqual(
            observation._provider_retry_metadata(success_then_throttled),
            (True, 120),
        )
        self.assertEqual(
            observation._provider_retry_metadata(
                self.response(429, headers={"Retry-After": "120"}, completed=False)
            ),
            (None, None),
        )
        self.assertEqual(
            observation._provider_retry_metadata(
                self.response(429, headers={"Retry-After": "120"})
                + b"x" * (16 * 1024)
                + b"\n"
            ),
            (None, None),
        )
        self.assertEqual(
            observation._provider_retry_metadata(b"x" * (16 * 1024) + b"\n"),
            (None, None),
        )
        self.assertEqual(
            observation._provider_retry_metadata(b"provider transport failed\n"),
            (None, None),
        )
        self.assertEqual(
            observation._provider_retry_metadata(
                self.response(429, headers={"Retry-After": "x" * 65})
            ),
            (None, None),
        )

    def test_transport_scopes_debug_to_child_and_keeps_only_typed_error(self):
        trace = self.response(
            429,
            headers={"Retry-After": "120"},
            body='{"message":"SECRET response body"}',
        ).decode()
        command = (
            "import os,sys; "
            "assert os.environ.get('GH_DEBUG') == 'api'; "
            "sys.stderr.write(sys.argv[1]); "
            "sys.stdout.write('secret stdout'); "
            "sys.exit(1)"
        )
        with mock.patch.object(
            observation.time,
            "time_ns",
            return_value=1_000_000_000_000,
        ), self.assertRaises(ObservationError) as raised:
            read_json(
                sys.executable,
                Path.cwd(),
                ["-c", command, trace],
                capture_provider_retry=True,
            )
        error = raised.exception
        self.assertTrue(error.rate_limited)
        self.assertEqual(error.retry_after_seconds, 120)
        self.assertEqual(
            observation.provider_retry_constraint(
                error,
                interval=30,
                failure_count=1,
            ),
            '{"kind":"not_before","not_before_unix_ms":1120000,'
            '"provider_host":"github.com","schema":"github-provider-retry-v1"}',
        )
        self.assertEqual(error._provider_retry_observed_at_ns, 1_000_000_000_000)
        safe = repr(error.as_dict())
        self.assertNotIn("secret stdout", safe)
        self.assertNotIn("SECRET response body", safe)
        self.assertNotIn("secret-marker", safe)
        self.assertNotIn("_provider_retry_observed_at_ns", repr(error.as_dict()))

    def test_captured_failure_without_timing_is_unknown_and_local_errors_are_omitted(self):
        command = "import sys; sys.stderr.write('provider transport failed\\n'); sys.exit(1)"
        with mock.patch.object(
            observation.time,
            "time_ns",
            return_value=5_000_000_000,
        ), self.assertRaises(ObservationError) as raised:
            read_bytes(
                sys.executable,
                Path.cwd(),
                ["-c", command],
                capture_provider_retry=True,
            )
        error = raised.exception
        self.assertEqual(
            observation.provider_retry_constraint(
                error,
                interval=30,
                failure_count=1,
            ),
            '{"kind":"unknown","provider_host":"github.com",'
            '"schema":"github-provider-retry-v1"}',
        )
        self.assertIsNone(
            observation.provider_retry_constraint(
                ObservationError("query_failed"),
                interval=30,
                failure_count=1,
            )
        )

    def test_provider_guard_runs_before_subprocess_creation(self):
        blocked = RuntimeError("provider read was blocked")
        with mock.patch.object(observation.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "provider read was blocked"):
                read_json(
                    sys.executable,
                    Path.cwd(),
                    ["-c", "pass"],
                    provider_guard=lambda: (_ for _ in ()).throw(blocked),
                )
        popen.assert_not_called()

    def test_invalid_or_oversized_transport_never_enables_retry(self):
        trace = self.response(
            429,
            headers={"Retry-After": "120"},
            body='{"message":"API rate limit exceeded"}',
        ).decode()
        command = "import sys; sys.stderr.write(sys.argv[1]); sys.stdout.write('not-json')"
        with self.assertRaises(ObservationError) as raised:
            read_json(
                sys.executable,
                Path.cwd(),
                ["-c", command, trace],
                capture_provider_retry=True,
            )
        self.assertEqual(raised.exception.code, "invalid_observation")
        self.assertFalse(raised.exception.rate_limited)
        self.assertTrue(raised.exception.provider_retry_unknown)

        with self.assertRaises(ObservationError) as raised:
            read_json(
                sys.executable,
                Path.cwd(),
                ["-c", command, trace],
                capture_provider_retry=True,
                limit=80,
            )
        self.assertEqual(raised.exception.code, "observation_limit")
        self.assertFalse(raised.exception.rate_limited)
        self.assertTrue(raised.exception.provider_retry_unknown)


class ProviderRetryDelayTest(unittest.TestCase):
    @staticmethod
    def response(status, *, headers=None, body="", completed=True):
        lines = [
            "* Request at 2026-10-22 00:00:00 +0000 UTC",
            "* Request to https://api.github.com/graphql",
            "> Authorization: token secret-marker",
            f"< HTTP/2.0 {status} response",
        ]
        lines.extend(
            f"< {name}: {value}" for name, value in (headers or {}).items()
        )
        lines.extend(["", body])
        if completed:
            lines.append("* Request took 0.001s")
        return ("\n".join(lines) + "\n").encode()

    def test_retry_after_is_private_and_authoritative(self):
        trace = self.response(
            429,
            headers={"Retry-After": "120"},
            body='{"message":"SECRET provider response body"}',
        )
        self.assertEqual(observation._provider_retry_metadata(trace), (True, 120))
        error = ObservationError(
            "query_failed",
            stderr=trace,
            rate_limited=True,
            retry_after_seconds=120,
        )
        self.assertEqual(error.retry_delay_seconds(interval=30, failure_count=1), 120)
        serialized = repr(error.as_dict())
        self.assertNotIn("secret-marker", serialized)
        self.assertNotIn("SECRET provider response body", serialized)
        self.assertNotIn("Retry-After", serialized)
        self.assertEqual(str(error), "query_failed")

    def test_primary_reset_and_http_date_retry_after(self):
        reset = self.response(
            403,
            headers={
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "1060",
                "Date": "Thu, 01 Jan 1970 00:16:40 GMT",
            },
            body='{"message":"API rate limit exceeded"}',
        )
        date_retry = self.response(
            429,
            headers={"Retry-After": "Thu, 01 Jan 1970 00:17:40 GMT"},
        )
        with mock.patch.object(observation.time, "time", return_value=1000):
            self.assertEqual(observation._provider_retry_metadata(reset), (True, 60))
            self.assertEqual(
                observation._provider_retry_metadata(date_retry),
                (True, 60),
            )

    def test_secondary_fallback_and_invalid_timing_fail_closed(self):
        secondary = self.response(
            403,
            headers={"X-RateLimit-Remaining": "12"},
            body='{"message":"You have exceeded a secondary rate limit."}',
        )
        self.assertEqual(
            observation._provider_retry_metadata(secondary),
            (True, None),
        )
        error = ObservationError("query_failed", rate_limited=True)
        self.assertEqual(error.retry_delay_seconds(interval=30, failure_count=1), 60)
        self.assertEqual(error.retry_delay_seconds(interval=30, failure_count=2), 120)
        self.assertEqual(error.retry_delay_seconds(interval=30, failure_count=20), 3600)
        self.assertEqual(
            ObservationError("query_failed").retry_delay_seconds(
                interval=30,
                failure_count=1,
            ),
            30,
        )
        invalid = self.response(
            403,
            headers={
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "not-an-epoch",
                "Date": "invalid date",
            },
            body='{"message":"API rate limit exceeded"}',
        )
        self.assertEqual(observation._provider_retry_metadata(invalid), (None, None))

    def test_permission_403_and_successful_exhausted_quota_are_not_retries(self):
        permission = self.response(
            403,
            headers={"X-RateLimit-Remaining": "10"},
            body='{"message":"Resource not accessible by integration"}',
        )
        invalid_retry = self.response(
            403,
            headers={
                "Retry-After": "invalid date",
                "X-RateLimit-Remaining": "10",
            },
            body='{"message":"Resource not accessible by integration"}',
        )
        success = self.response(
            200,
            headers={"X-RateLimit-Remaining": "0"},
            body='{"data":{"viewer":{"login":"user"}}}',
        )
        self.assertEqual(
            observation._provider_retry_metadata(permission),
            (False, None),
        )
        self.assertEqual(
            observation._provider_retry_metadata(success),
            (False, None),
        )
        self.assertEqual(
            observation._provider_retry_metadata(invalid_retry),
            (False, None),
        )

    def test_final_request_only_and_incomplete_or_oversized_fail_closed(self):
        throttled_then_success = self.response(
            429,
            headers={"Retry-After": "120"},
        ) + self.response(200, body='{"data":{}}')
        success_then_throttled = self.response(
            200,
            body='{"data":{}}',
        ) + self.response(429, headers={"Retry-After": "120"})
        self.assertEqual(
            observation._provider_retry_metadata(throttled_then_success),
            (False, None),
        )
        self.assertEqual(
            observation._provider_retry_metadata(success_then_throttled),
            (True, 120),
        )
        self.assertEqual(
            observation._provider_retry_metadata(
                self.response(429, headers={"Retry-After": "120"}, completed=False)
            ),
            (None, None),
        )
        self.assertEqual(
            observation._provider_retry_metadata(
                self.response(429, headers={"Retry-After": "120"})
                + b"x" * (16 * 1024)
                + b"\n"
            ),
            (None, None),
        )

    def test_transport_scopes_trace_to_child_and_only_exposes_hashes(self):
        trace = self.response(
            429,
            headers={"Retry-After": "120"},
            body='{"message":"SECRET response body"}',
        ).decode()
        code = (
            "import os,sys; assert os.environ.get('GH_DEBUG') == 'api'; "
            "sys.stderr.write(sys.argv[1]); sys.stdout.write('secret stdout'); "
            "sys.exit(1)"
        )
        with self.assertRaises(ObservationError) as raised:
            read_json(
                sys.executable,
                Path.cwd(),
                ["-c", code, trace],
                capture_provider_retry=True,
            )
        error = raised.exception
        self.assertTrue(error.rate_limited)
        self.assertEqual(error.retry_after_seconds, 120)
        serialized = repr(error.as_dict())
        self.assertNotIn("secret stdout", serialized)
        self.assertNotIn("SECRET response body", serialized)
        self.assertNotIn("secret-marker", serialized)

    def test_invalid_or_limited_transport_cannot_trigger_retry(self):
        trace = self.response(
            429,
            headers={"Retry-After": "120"},
            body='{"message":"API rate limit exceeded"}',
        ).decode()
        code = "import sys; sys.stderr.write(sys.argv[1]); sys.stdout.write('not-json')"
        with self.assertRaises(ObservationError) as raised:
            read_json(
                sys.executable,
                Path.cwd(),
                ["-c", code, trace],
                capture_provider_retry=True,
            )
        self.assertEqual(raised.exception.code, "invalid_observation")
        self.assertFalse(raised.exception.rate_limited)

        with self.assertRaises(ObservationError) as raised:
            read_json(
                sys.executable,
                Path.cwd(),
                ["-c", code, trace],
                capture_provider_retry=True,
                limit=80,
            )
        self.assertEqual(raised.exception.code, "observation_limit")
        self.assertFalse(raised.exception.rate_limited)


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
        self.thread_page_index = 0

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["pr", "view"]:
            self.pr_reads += 1
            return deepcopy(self.first if self.pr_reads == 1 else self.last)
        if args[:2] == ["pr", "checks"]:
            return deepcopy(self.checks)
        if args[:2] == ["api", "graphql"]:
            index = min(self.thread_page_index, len(self.threads) - 1)
            self.thread_page_index += 1
            return deepcopy(self.threads[index])
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

    def test_final_rollup_change_cannot_be_paired_with_stale_checks(self):
        cases = (
            (
                pr(statusCheckRollup=[]),
                pr(statusCheckRollup=[{"name": "required", "conclusion": "FAILURE"}]),
                [],
            ),
            (
                pr(statusCheckRollup=[{"name": "required", "conclusion": "SUCCESS"}]),
                pr(statusCheckRollup=[{"name": "required", "status": "IN_PROGRESS"}]),
                [check()],
            ),
        )
        for first, last, checks in cases:
            with self.subTest(first=first["statusCheckRollup"], last=last["statusCheckRollup"]):
                fake = FakeGitHub(first=first, last=last, checks=checks)
                with self.assertRaises(ObservationError) as raised:
                    self.observe(fake)
                self.assertEqual(raised.exception.code, "source_changed")

    def test_identical_final_rollup_keeps_stable_check_classification(self):
        fake = FakeGitHub(
            first=pr(statusCheckRollup=[{"name": "required", "conclusion": "SUCCESS"}]),
            last=pr(statusCheckRollup=[{"name": "required", "conclusion": "SUCCESS"}]),
        )
        self.assertEqual(self.observe(fake)["checks"][0]["state"], "SUCCESS")

    def test_startup_failure_pending_bucket_is_normalized_to_failure(self):
        fake = FakeGitHub(checks=[check(
            state="STARTUP_FAILURE",
            bucket="pending",
        )])
        result = self.observe(fake)
        self.assertEqual(result["checks"][0]["state"], "STARTUP_FAILURE")
        self.assertEqual(result["checks"][0]["bucket"], "fail")

    def test_outer_thread_pagination_sends_only_the_outer_cursor(self):
        def node(index):
            return {
                "id": f"THREAD-{index}",
                "isResolved": False,
                "isOutdated": False,
                "path": "app.py",
                "subjectType": "LINE",
                "line": index + 1,
                "startLine": None,
                "originalLine": index + 1,
                "originalStartLine": None,
                "comments": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }

        first = [node(index) for index in range(100)]
        second = [node(100)]
        fake = FakeGitHub(threads=[
            thread_page(first, more=True, cursor="OUTER-THREADS-100"),
            thread_page(second),
        ])
        result = self.observe(fake)
        outer_queries = [
            args for args in fake.calls
            if args[:2] == ["api", "graphql"]
            and not any(argument.startswith("threadId=") for argument in args)
        ]
        self.assertEqual(len(result["feedback_items"]), 101)
        self.assertEqual(len(outer_queries), 2)
        self.assertIn("endCursor=null", outer_queries[0])
        self.assertIn("endCursor=OUTER-THREADS-100", outer_queries[1])
        self.assertNotIn("--paginate", outer_queries[0])
        self.assertNotIn("--slurp", outer_queries[0])

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

    def test_malformed_thread_page_info_is_incomplete(self):
        for page_info in (
            None,
            {},
            {"hasNextPage": "yes", "endCursor": "cursor"},
            {"hasNextPage": True, "endCursor": 7},
        ):
            with self.subTest(page_info=page_info):
                page = {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [],
                                    "pageInfo": page_info,
                                }
                            }
                        }
                    }
                }
                with self.assertRaises(ObservationError) as raised:
                    self.observe(FakeGitHub(threads=[page]))
                self.assertEqual(
                    raised.exception.code,
                    "incomplete_observation",
                )

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

    def test_empty_advancing_comment_pages_use_a_query_budget(self):
        thread = {
            "id": "THREAD1",
            "isResolved": False,
            "isOutdated": False,
            "path": "app.py",
            "subjectType": "LINE",
            "line": 1,
            "startLine": None,
            "originalLine": 1,
            "originalStartLine": None,
            "comments": {
                "nodes": [],
                "pageInfo": {"hasNextPage": True, "endCursor": "cursor-0"},
            },
        }
        continuation_queries = 0
        top_page = thread_page([thread])

        def query(args):
            nonlocal continuation_queries
            if args[:2] == ["pr", "view"]:
                return deepcopy(pr())
            if args[:2] == ["pr", "checks"]:
                return [check()]
            if args[:2] == ["api", "graphql"]:
                if any(argument.startswith("threadId=") for argument in args):
                    if continuation_queries >= observation.MAX_FEEDBACK_ITEMS:
                        raise AssertionError("comment pagination exceeded its query budget")
                    continuation_queries += 1
                    cursor = f"cursor-{continuation_queries}"
                    return {
                        "data": {
                            "node": {
                                "comments": {
                                    "nodes": [],
                                    "pageInfo": {
                                        "hasNextPage": True,
                                        "endCursor": cursor,
                                    },
                                }
                            }
                        }
                    }
                return deepcopy(top_page)
            if args[0] == "api":
                return [[]]
            raise AssertionError(args)

        with self.assertRaises(ObservationError) as raised:
            GitHubObserver(Path.cwd(), query=query).observe_pr(URL)
        self.assertEqual(raised.exception.code, "observation_limit")
        self.assertEqual(
            continuation_queries,
            observation.MAX_FEEDBACK_ITEMS - 1,
        )

    def test_repeated_comment_cursor_is_incomplete_not_a_budget_exhaustion(self):
        thread = {
            "id": "THREAD1",
            "isResolved": False,
            "isOutdated": False,
            "path": "app.py",
            "subjectType": "LINE",
            "line": 1,
            "startLine": None,
            "originalLine": 1,
            "originalStartLine": None,
            "comments": {
                "nodes": [],
                "pageInfo": {"hasNextPage": True, "endCursor": "same-cursor"},
            },
        }

        def query(args):
            if args[:2] == ["pr", "view"]:
                return deepcopy(pr())
            if args[:2] == ["pr", "checks"]:
                return [check()]
            if args[:2] == ["api", "graphql"]:
                if any(argument.startswith("threadId=") for argument in args):
                    return {
                        "data": {
                            "node": {
                                "comments": {
                                    "nodes": [],
                                    "pageInfo": {
                                        "hasNextPage": True,
                                        "endCursor": "same-cursor",
                                    },
                                }
                            }
                        }
                    }
                return thread_page([thread])
            if args[0] == "api":
                return [[]]
            raise AssertionError(args)

        with self.assertRaises(ObservationError) as raised:
            GitHubObserver(Path.cwd(), query=query).observe_pr(URL)
        self.assertEqual(raised.exception.code, "incomplete_observation")

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
