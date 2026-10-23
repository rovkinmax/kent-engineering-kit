from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from test_workflow_runtime import (
    EVIDENCE_FIXTURE_OWNER,
    REPO_ROOT,
    load_template_module,
)


JANITOR_SOURCE = REPO_ROOT / "templates/project/workflow-task-janitor"


class JanitorAuditS3Test(unittest.TestCase):
    def setUp(self) -> None:
        from test_workflow_runtime import WorkflowJanitorTest

        self.fixtures = WorkflowJanitorTest(
            "test_delete_result_native_and_legacy_contracts"
        )
        self.fixtures.setUp()
        self.addCleanup(self.fixtures.doCleanups)

    def janitor(self, name: str):
        return load_template_module(JANITOR_SOURCE, name)

    def test_complete_keeps_the_original_terminal_marker_final(self) -> None:
        root, _, scripts, marker = self.fixtures.make_managed_worktree(with_v2=True)
        janitor = load_template_module(
            scripts / "workflow-task-janitor", "janitor_marker_tail"
        )
        runtime = load_template_module(
            scripts / "workflow_runtime_contracts.py", "janitor_marker_runtime"
        )
        original = f"Opaque\r\nprefix\r\n{marker}\r\n"
        runtime.validate_cleanup_report(original)
        janitor._CLEANUP_REPORT_RUNTIME = runtime

        for transition, blocker in (
            ("task_janitor_done", ""),
            ("task_janitor_blocked", "resource disposition remains unknown"),
        ):
            with self.subTest(transition=transition):
                janitor._ORIGINAL_CLEANUP_REPORT = original
                output = io.StringIO()
                with mock.patch.object(sys, "stdout", output):
                    janitor.complete(
                        transition,
                        f"{original}\nJanitor observed the current result.",
                        blocker,
                    )

                payload = json.loads(output.getvalue())
                report = payload["cleanup_report"]
                self.assertEqual(payload["transition"], transition)
                self.assertEqual(report.count(marker), 1)
                self.assertTrue(report.endswith(f"{marker}\r\n"))
                self.assertIn("Opaque\r\nprefix\r\n", report)
                runtime.validate_cleanup_report(report)
                self.assertEqual(
                    set(payload),
                    {"transition", "cleanup_report"}
                    | ({"blocker_reason"} if blocker else set()),
                )

    def test_complete_leaves_markerless_legacy_reports_unchanged(self) -> None:
        janitor = self.janitor("janitor_marker_legacy")
        janitor._ORIGINAL_CLEANUP_REPORT = "Legacy cleanup report.\r\n"
        output = io.StringIO()
        with mock.patch.object(sys, "stdout", output):
            janitor.complete(
                "task_janitor_done",
                "Legacy cleanup report.\r\nJanitor preserved the worktree.",
            )

        self.assertEqual(
            json.loads(output.getvalue())["cleanup_report"],
            "Legacy cleanup report.\r\nJanitor preserved the worktree.",
        )

    def test_complete_does_not_repair_invalid_or_duplicate_markers(self) -> None:
        _, _, scripts, _ = self.fixtures.make_managed_worktree(with_v2=True)
        janitor = load_template_module(
            scripts / "workflow-task-janitor", "janitor_marker_invalid"
        )
        janitor._CLEANUP_REPORT_RUNTIME = load_template_module(
            scripts / "workflow_runtime_contracts.py",
            "janitor_marker_invalid_runtime",
        )
        cases = (
            "Opaque\nTERMINAL_EVIDENCE_V1 {invalid}",
            "TERMINAL_EVIDENCE_V1 {invalid}\nTERMINAL_EVIDENCE_V1 {invalid}",
            "TERMINAL_EVIDENCE_V1 {invalid}\nopaque suffix",
        )
        for original in cases:
            with self.subTest(original=original):
                janitor._ORIGINAL_CLEANUP_REPORT = original
                output = io.StringIO()
                expected = f"{original}\nJanitor blocked cleanup."
                with mock.patch.object(sys, "stdout", output):
                    janitor.complete("task_janitor_blocked", expected, "blocked")
                payload = json.loads(output.getvalue())
                self.assertEqual(payload["cleanup_report"], expected)

    def test_main_preserves_exact_marker_and_blocks_without_runtime_support(self) -> None:
        _, worktree, scripts, marker = self.fixtures.make_managed_worktree(
            with_v2=True
        )
        nested = worktree / "nested"
        nested.mkdir()
        original = f"Opaque\r\nprefix\r\n{marker}\r\n"

        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable):
                module = load_template_module(
                    scripts / "workflow-task-janitor",
                    f"janitor_main_marker_{unavailable}",
                )
                if unavailable:
                    with mock.patch.object(
                        module,
                        "runtime_contracts",
                        side_effect=RuntimeError("private unsafe path"),
                    ):
                        result = self._run_main_with_report(
                            module, nested, original
                        )
                else:
                    result = self._run_main_with_report(
                        module, nested, original
                    )

                payload = result
                self.assertEqual(payload["cleanup_report"].count(marker), 1)
                self.assertTrue(payload["cleanup_report"].endswith(f"{marker}\r\n"))
                self.assertIn("Opaque\r\nprefix\r\n", payload["cleanup_report"])
                if unavailable:
                    self.assertEqual(payload["transition"], "task_janitor_blocked")
                    self.assertEqual(payload["cleanup_report"], original)
                    self.assertNotIn("private unsafe path", json.dumps(payload))
                else:
                    self.assertEqual(payload["transition"], "task_janitor_done")
                    self.assertTrue(
                        payload["cleanup_report"].endswith(
                            "Janitor preserved non-root workspace: "
                            f"{nested.resolve()}\r\n{marker}\r\n"
                        )
                    )

    def _run_main_with_report(
        self,
        module,
        workspace: Path,
        report: str,
    ) -> dict[str, object]:
        output = io.StringIO()
        payload = json.loads(
            self.fixtures.janitor_input(
                workspace,
                branch_name="TASK-1",
                cleanup_mode="no_pr",
                pr_base_branch="not-applicable",
                cleanup_report=report,
            )
        )
        with (
            mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(sys, "stdout", output),
        ):
            module.main()
        return json.loads(output.getvalue())

    def _invoke_main(
        self,
        module,
        payload: dict[str, object],
        *,
        environment: dict[str, str],
    ) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, environment, clear=False),
            mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(sys, "stdout", output),
        ):
            previous_cwd = Path.cwd()
            try:
                code = module.main()
            finally:
                os.chdir(previous_cwd)
        return code, json.loads(output.getvalue())

    def _provider_context(
        self,
        janitor,
        *,
        workspace: Path,
        observation,
        prepared_guard,
        pending_constraints: list[str] | None = None,
        post_delete: bool = False,
    ) -> dict[str, object]:
        from workflowkit import delivery, runtime

        checkpoint = mock.Mock()
        checkpoint.emit_producer_result.side_effect = (
            lambda _root, _payload, result, _kind, **_kwargs: result
        )
        context = {
            "checkpoint": checkpoint,
            "delivery": delivery,
            "observation": observation,
            "runtime": runtime,
            "workspace": workspace,
            "payload": {},
            "refs": [],
            "identity": {},
            "prepared_guard": prepared_guard,
            "pending_constraints": pending_constraints or [],
            "post_delete": post_delete,
        }
        janitor._PROVIDER_CONTEXT = context
        return context

    def test_provider_guard_uses_current_commentary_footer_and_native_identity(self) -> None:
        from workflowkit import delivery

        root = self.fixtures.create_repository()
        (root / ".kent" / "workflow-profile.toml").write_text(
            '[policies]\ndelivery_reliability = "v1"\n'
        )
        current = (
            "build/kent-workflow/TASK-1/packets/base-"
            + "a" * 64
            + ".json"
        )
        old = (
            "build/kent-workflow/TASK-1/packets/base-"
            + "b" * 64
            + ".json"
        )
        commentary = f"Current carrier\nKENT_PACKET_SOURCE_V1 {current}"
        payload = {
            "commentary": commentary,
            "cleanup_report": f"Old report\nKENT_PACKET_SOURCE_V1 {old}",
        }
        native = {
            "task_id": "native-task",
            "node_id": "janitor-node",
            "transition_branch_key": "cleanup",
        }
        identity = {"invocation_kind": "script", "native": "readback"}
        prepared = mock.Mock()
        checkpoint = mock.Mock()
        checkpoint.delivery_reliability_enabled.return_value = True
        provider_delivery = mock.Mock(wraps=delivery)
        provider_delivery.observe_script_identity.return_value = identity
        provider_delivery.prepare_github_provider_read_guard.return_value = prepared
        observation = mock.Mock()
        runtime = mock.Mock()
        janitor = self.janitor("janitor_current_commentary_guard")

        with (
            mock.patch.object(janitor, "checkpoint_module", return_value=checkpoint),
            mock.patch.object(
                janitor,
                "provider_modules",
                return_value=(provider_delivery, observation, runtime),
            ),
            mock.patch.object(janitor, "_NATIVE_KENT", native),
        ):
            context = janitor.prepare_provider_guard(root, "TASK-1", payload)

        self.assertIsNotNone(context)
        self.assertEqual(context["refs"], [current])
        provider_delivery.producer_locator.assert_called_once_with(commentary)
        provider_delivery.observe_script_identity.assert_called_once_with(
            root,
            "TASK-1",
            native,
            native_read=checkpoint.delivery_native_read,
        )
        provider_delivery.prepare_github_provider_read_guard.assert_called_once_with(
            root,
            [current],
            identity,
        )
        self.assertNotIn(old, context["refs"])

    def test_provider_guard_ignores_older_cleanup_report_footer(self) -> None:
        root = self.fixtures.create_repository()
        (root / ".kent" / "workflow-profile.toml").write_text(
            '[policies]\ndelivery_reliability = "v1"\n'
        )
        janitor = self.janitor("janitor_no_stale_commentary_fallback")
        checkpoint = mock.Mock()
        checkpoint.delivery_reliability_enabled.return_value = True
        with (
            mock.patch.object(janitor, "checkpoint_module", return_value=checkpoint),
            mock.patch.object(janitor, "provider_modules") as provider_modules,
        ):
            with self.assertRaises(janitor.JanitorProviderProvenanceBlocked):
                janitor.prepare_provider_guard(
                    root,
                    "TASK-1",
                    {
                        "cleanup_report": (
                            "Old report\nKENT_PACKET_SOURCE_V1 "
                            "build/kent-workflow/TASK-1/packets/base-"
                            + "a" * 64
                            + ".json"
                        )
                    },
                )
        provider_modules.assert_not_called()

    def test_opted_in_profile_rejected_by_checkpoint_fails_closed(self) -> None:
        root = self.fixtures.create_repository()
        (root / ".kent" / "workflow-profile.toml").write_text(
            '[policies]\ndelivery_reliability = "v1"\n'
        )
        janitor = self.janitor("janitor_profile_adapter_disagreement")
        checkpoint = mock.Mock()
        checkpoint.delivery_reliability_enabled.return_value = False
        payload = {
            "commentary": (
                "Current carrier\nKENT_PACKET_SOURCE_V1 "
                "build/kent-workflow/TASK-1/packets/base-"
                + "a" * 64
                + ".json"
            )
        }

        with (
            mock.patch.object(janitor, "checkpoint_module", return_value=checkpoint),
            mock.patch.object(janitor, "provider_modules") as provider_modules,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "qualified producer adapter rejected opted-in policy",
            ):
                janitor.prepare_provider_guard(root, "TASK-1", payload)

        provider_modules.assert_not_called()

    def test_provider_guard_rejects_malformed_current_footer(self) -> None:
        from workflowkit import delivery

        root = self.fixtures.create_repository()
        (root / ".kent" / "workflow-profile.toml").write_text(
            '[policies]\ndelivery_reliability = "v1"\n'
        )
        janitor = self.janitor("janitor_malformed_current_footer")
        checkpoint = mock.Mock()
        checkpoint.delivery_reliability_enabled.return_value = True
        provider_delivery = mock.Mock(wraps=delivery)
        identity = {
            "task_id": "native-task",
            "node_id": "janitor-node",
            "transition_branch_key": "cleanup",
        }
        with (
            mock.patch.object(janitor, "checkpoint_module", return_value=checkpoint),
            mock.patch.object(
                janitor,
                "provider_modules",
                return_value=(provider_delivery, mock.Mock(), mock.Mock()),
            ),
            mock.patch.object(janitor, "_NATIVE_KENT", identity),
        ):
            with self.assertRaises(janitor.JanitorProviderProvenanceBlocked):
                janitor.prepare_provider_guard(
                    root,
                    "TASK-1",
                    {
                        "commentary": (
                            "Malformed current carrier\n"
                            "KENT_PACKET_SOURCE_V1 not-a-qualified-locator"
                        )
                    },
                )

        provider_delivery.observe_script_identity.assert_not_called()
        provider_delivery.prepare_github_provider_read_guard.assert_not_called()

    def test_opted_in_main_blocks_without_current_footer_before_gh_or_delete(self) -> None:
        import shutil

        root, worktree, scripts, _marker = self.fixtures.make_managed_worktree(
            with_v2=False
        )
        profile = worktree / ".kent" / "workflow-profile.toml"
        profile.write_text('[policies]\ndelivery_reliability = "v1"\n')
        self.fixtures.run_git(worktree, "add", ".kent/workflow-profile.toml")
        self.fixtures.run_git(worktree, "commit", "-q", "-m", "enable reliability")
        shutil.copyfile(
            REPO_ROOT / "templates/project/workflow-checkpoint",
            scripts / "workflow-checkpoint",
        )
        old_locator = (
            "build/kent-workflow/TASK-1/packets/base-"
            + "c" * 64
            + ".json"
        )
        original_report = (
            "Prior cleanup\nKENT_PACKET_SOURCE_V1 " + old_locator
        )
        payload = json.loads(
            self.fixtures.janitor_input(
                worktree,
                branch_name="TASK-1",
                pr_url="https://github.com/example/repo/pull/1",
                cleanup_mode="merged",
                pr_base_branch="main",
                cleanup_report=original_report,
            )
        )
        gh_called = root / "gh-called"
        fake_gh = root / "gh"
        fake_gh.write_text(
            "#!/bin/sh\n"
            f"touch {str(gh_called)!r}\n"
            "exit 1\n"
        )
        fake_gh.chmod(0o755)
        wrapper_called = root / "wrapper-called"
        janitor = load_template_module(
            scripts / "workflow-task-janitor",
            "janitor_missing_current_commentary",
        )
        output = io.StringIO()

        with (
            mock.patch.dict(
                os.environ,
                {
                    **os.environ,
                    "KENT_GH_BIN": str(fake_gh),
                    "KENT_WORKTREE_WRAPPER": str(
                        self.fixtures.completed_wrapper(root)
                    ),
                    "KENT_TEST_WRAPPER_MARKER": str(wrapper_called),
                    "KENT_TEST_PRIMARY": str(root),
                },
            ),
            mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(sys, "stdout", output),
        ):
            self.assertEqual(janitor.main(), 0)

        result = json.loads(output.getvalue())
        self.assertEqual(result["transition"], "task_janitor_blocked")
        self.assertEqual(result["cleanup_report"], original_report)
        self.assertIn("Current Janitor producer provenance is unavailable", result["blocker_reason"])
        self.assertFalse(gh_called.exists())
        self.assertFalse(wrapper_called.exists())
        self.assertTrue(worktree.is_dir())

    def test_delete_boundary_blocks_and_emits_inherited_retry_binding_predelete(self) -> None:
        from workflowkit.runtime import canonical_bytes

        root, worktree, scripts, _marker = self.fixtures.make_managed_worktree(
            with_v2=False
        )
        janitor = load_template_module(
            scripts / "workflow-task-janitor",
            "janitor_delete_boundary_provider_guard",
        )
        payload = json.loads(
            self.fixtures.janitor_input(
                worktree,
                branch_name="TASK-1",
                cleanup_mode="no_pr",
                pr_base_branch="not-applicable",
                cleanup_report="Original cleanup report.",
                commentary=(
                    "Current incoming carrier\nKENT_PACKET_SOURCE_V1 "
                    "build/kent-workflow/TASK-1/packets/base-"
                    + "d" * 64
                    + ".json"
                ),
            )
        )
        constraint = canonical_bytes({
            "schema": "github-provider-retry-v1",
            "provider_host": "github.com",
            "kind": "unknown",
        }).decode("utf-8")
        context = self._provider_context(
            janitor,
            workspace=worktree,
            observation=SimpleNamespace(
                read_json=mock.Mock(),
                provider_retry_constraint=mock.Mock(),
            ),
            prepared_guard=mock.Mock(
                side_effect=janitor.JanitorProviderReadBlocked("unknown")
            ),
            pending_constraints=[constraint],
        )
        context["payload"] = payload
        delete = mock.Mock(side_effect=AssertionError("delete must remain uncalled"))
        output = io.StringIO()

        with (
            mock.patch.object(
                janitor,
                "prepare_provider_guard",
                return_value=context,
            ),
            mock.patch.object(janitor, "admit_origin_route", return_value={}),
            mock.patch.object(janitor, "head_matches_current_remote", return_value=True),
            mock.patch.object(janitor, "admit_local_branch_for_native_delete"),
            mock.patch.object(
                janitor,
                "_admit_managed_runtime_parent",
                return_value=(True, "runtime parent admitted"),
            ),
            mock.patch.object(janitor, "_prepare_v2_managed_runtime_state", return_value=None),
            mock.patch.object(janitor, "cleanup_session_uses_workspace", return_value=False),
            mock.patch.object(
                janitor,
                "capture_kent_worktree_record",
                return_value={
                    "variant": "registered",
                    "root": str(worktree),
                    "worktree_id": "test-worktree-record",
                },
            ),
            mock.patch.object(janitor, "delete_managed_worktree", delete),
            mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(sys, "stdout", output),
        ):
            self.assertEqual(janitor.main(), 0)

        result = json.loads(output.getvalue())
        self.assertEqual(result["transition"], "task_janitor_blocked")
        self.assertEqual(result["cleanup_report"], "Original cleanup report.")
        self.assertIn("managed worktree deletion was not attempted", result["blocker_reason"])
        self.assertTrue(worktree.is_dir())
        delete.assert_not_called()
        context["checkpoint"].emit_producer_result.assert_called_once()
        call = context["checkpoint"].emit_producer_result.call_args
        self.assertEqual(call.args[0], worktree)
        self.assertEqual(call.args[1], payload)
        self.assertEqual(call.args[3], "task-janitor")
        self.assertEqual(call.kwargs["github_retry_constraint"], constraint)

    def test_unknown_inherited_constraint_blocks_before_provider_child(self) -> None:
        from workflowkit.delivery import _PreparedGitHubProviderReadGuard

        primary = self.fixtures.create_repository()
        removed_task_root = primary / ".kent" / "worktrees" / "gone"
        children: list[str] = []

        def read_json(_executable, _workspace, _args, **kwargs):
            kwargs["provider_guard"]()
            children.append("gh")
            return {}

        observation = SimpleNamespace(
            read_json=read_json,
            provider_retry_constraint=lambda *_args, **_kwargs: None,
        )
        janitor = self.janitor("janitor_unknown_provider_guard")
        self._provider_context(
            janitor,
            workspace=removed_task_root,
            observation=observation,
            prepared_guard=_PreparedGitHubProviderReadGuard({
                "schema": "github-provider-retry-v1",
                "provider_host": "github.com",
                "kind": "unknown",
            }),
            post_delete=True,
        )

        with self.assertRaises(janitor.JanitorProviderReadBlocked) as blocked:
            janitor.github_pr(
                primary,
                "https://github.com/example/repo/pull/1",
            )

        self.assertEqual(blocked.exception.state, "unknown")
        self.assertEqual(str(blocked.exception), "github_provider_retry_blocked")
        self.assertEqual(children, [])

    def test_expired_local_deadline_allows_guarded_read_with_fresh_clock(self) -> None:
        from workflowkit.delivery import _PreparedGitHubProviderReadGuard
        from workflowkit.runtime import canonical_bytes

        primary = self.fixtures.create_repository()
        removed_task_root = primary / ".kent" / "worktrees" / "gone"
        (primary / ".kent" / "worktrees").mkdir()
        children: list[str] = []

        def read_json(_executable, _workspace, _args, **kwargs):
            self.assertFalse(removed_task_root.exists())
            kwargs["provider_guard"]()
            children.append("gh")
            return {"state": "MERGED"}

        observation = SimpleNamespace(
            read_json=read_json,
            provider_retry_constraint=lambda *_args, **_kwargs: None,
        )
        janitor = self.janitor("janitor_expired_provider_deadline")
        constraint = canonical_bytes({
            "schema": "github-provider-retry-v1",
            "provider_host": "github.com",
            "kind": "not_before",
            "not_before_unix_ms": 1000,
        }).decode("utf-8")
        self._provider_context(
            janitor,
            workspace=removed_task_root,
            observation=observation,
            prepared_guard=_PreparedGitHubProviderReadGuard(None),
            pending_constraints=[constraint],
            post_delete=True,
        )

        with mock.patch.object(janitor.time, "time_ns", return_value=1_000_000_000) as clock:
            result = janitor.github_pr(
                primary,
                "https://github.com/example/repo/pull/1",
            )

        self.assertEqual(result["state"], "MERGED")
        self.assertEqual(children, ["gh"])
        self.assertEqual(clock.call_count, 2)

    def test_inherited_deadline_is_resampled_after_task_root_removal(self) -> None:
        from workflowkit.delivery import _PreparedGitHubProviderReadGuard

        primary = self.fixtures.create_repository()
        removed_task_root = primary / ".kent" / "worktrees" / "gone"
        children: list[str] = []
        read_calls = 0

        def read_json(_executable, _workspace, _args, **kwargs):
            nonlocal read_calls
            read_calls += 1
            kwargs["provider_guard"]()
            children.append("gh")
            return {"state": "MERGED"}

        observation = SimpleNamespace(
            read_json=read_json,
            provider_retry_constraint=lambda *_args, **_kwargs: None,
        )
        janitor = self.janitor("janitor_inherited_expiry_after_delete")
        prepared = _PreparedGitHubProviderReadGuard({
            "schema": "github-provider-retry-v1",
            "provider_host": "github.com",
            "kind": "not_before",
            "not_before_unix_ms": 1000,
        })
        self._provider_context(
            janitor,
            workspace=removed_task_root,
            observation=observation,
            prepared_guard=prepared,
            post_delete=True,
        )

        with mock.patch.object(
            janitor.time,
            "time_ns",
            side_effect=[999_000_000, 1_000_000_000, 1_000_000_000],
        ) as clock:
            with self.assertRaises(janitor.JanitorProviderReadBlocked) as blocked:
                janitor.github_pr(
                    primary,
                    "https://github.com/example/repo/pull/1",
                )
            result = janitor.github_pr(
                primary,
                "https://github.com/example/repo/pull/1",
            )

        self.assertEqual(blocked.exception.state, "not_before")
        self.assertEqual(read_calls, 2)
        self.assertEqual(children, ["gh"])
        self.assertEqual(result["state"], "MERGED")
        self.assertEqual(clock.call_count, 3)

    def test_unknown_local_retry_constraint_blocks_without_sleep_or_second_child(self) -> None:
        from workflowkit.delivery import _PreparedGitHubProviderReadGuard
        from workflowkit.runtime import canonical_bytes

        primary = self.fixtures.create_repository()
        error = RuntimeError("sensitive provider detail")
        error._provider_retry_captured = True
        children: list[str] = []
        constraint = canonical_bytes({
            "schema": "github-provider-retry-v1",
            "provider_host": "github.com",
            "kind": "unknown",
        }).decode("utf-8")

        def read_json(_executable, _workspace, _args, **kwargs):
            kwargs["provider_guard"]()
            children.append("gh")
            raise error

        observation = SimpleNamespace(
            read_json=read_json,
            provider_retry_constraint=lambda *_args, **_kwargs: constraint,
        )
        janitor = self.janitor("janitor_local_unknown_retry")
        context = self._provider_context(
            janitor,
            workspace=primary,
            observation=observation,
            prepared_guard=_PreparedGitHubProviderReadGuard(None),
        )
        with (
            mock.patch.dict(
                os.environ,
                {"KENT_JANITOR_GH_RETRY_DELAY_SECONDS": "2"},
            ),
            mock.patch.object(janitor.time, "sleep") as sleep,
        ):
            with self.assertRaises(janitor.JanitorProviderReadBlocked) as blocked:
                janitor.github_pr(
                    primary,
                    "https://github.com/example/repo/pull/1",
                )

        self.assertEqual(blocked.exception.state, "unknown")
        self.assertEqual(children, ["gh"])
        self.assertEqual(context["pending_constraints"], [constraint])
        sleep.assert_not_called()

    def test_post_delete_provider_failure_is_not_retried_or_emitted(self) -> None:
        from workflowkit.delivery import _PreparedGitHubProviderReadGuard

        primary = self.fixtures.create_repository()
        removed_task_root = primary / ".kent" / "worktrees" / "gone"
        children: list[str] = []

        def read_json(_executable, _workspace, _args, **kwargs):
            kwargs["provider_guard"]()
            children.append("gh")
            raise RuntimeError("private provider detail")

        observation = SimpleNamespace(
            read_json=read_json,
            provider_retry_constraint=lambda *_args, **_kwargs: None,
        )
        janitor = self.janitor("janitor_post_delete_provider_failure")
        context = self._provider_context(
            janitor,
            workspace=removed_task_root,
            observation=observation,
            prepared_guard=_PreparedGitHubProviderReadGuard(None),
            post_delete=True,
        )
        with (
            mock.patch.object(janitor.time, "sleep") as sleep,
            mock.patch.object(janitor, "complete") as complete,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "GitHub provider read failed after worktree deletion",
            ):
                janitor.github_pr(
                    primary,
                    "https://github.com/example/repo/pull/1",
                )

        self.assertEqual(children, ["gh"])
        sleep.assert_not_called()
        complete.assert_not_called()
        context["checkpoint"].emit_producer_result.assert_not_called()

    def test_origin_admission_allows_multi_fetch_with_one_explicit_push(self) -> None:
        root = self.fixtures.create_repository()
        git = self.fixtures.run_git
        git(root, "remote", "add", "origin", "https://github.com/example/repo.git")
        git(
            root,
            "remote",
            "set-url",
            "--add",
            "origin",
            "https://github.com/mirror/readonly.git",
        )
        git(
            root,
            "config",
            "--add",
            "remote.origin.pushurl",
            "https://github.com/example/repo.git",
        )
        module = self.janitor("janitor_origin_multi_fetch")

        route = module.admit_origin_route(
            root, expected_repository=("example", "repo")
        )

        self.assertEqual(len(route["fetch_urls"]), 2)
        self.assertEqual(
            route["push_urls"], ("https://github.com/example/repo.git",)
        )

    def test_origin_remote_group_helper_and_ambiguous_push_are_rejected(self) -> None:
        cases = ("remote_group", "custom_helper", "multiple_push")
        for case in cases:
            with self.subTest(case=case):
                root = self.fixtures.create_repository()
                git = self.fixtures.run_git
                git(
                    root,
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/example/repo.git",
                )
                module = self.janitor(f"janitor_origin_{case}")
                if case == "remote_group":
                    git(root, "config", "--add", "remotes.origin", "somewhere")
                elif case == "custom_helper":
                    git(root, "config", "remote.origin.vcs", "custom")
                else:
                    git(
                        root,
                        "config",
                        "--add",
                        "remote.origin.pushurl",
                        "https://github.com/example/repo.git",
                    )
                    git(
                        root,
                        "config",
                        "--add",
                        "remote.origin.pushurl",
                        "https://github.com/other/repo.git",
                    )
                with self.assertRaises(RuntimeError):
                    module.admit_origin_route(
                        root, expected_repository=("example", "repo")
                    )

    def test_task_fetch_route_and_returned_pr_identity_are_bound(self) -> None:
        root = self.fixtures.create_repository()
        git = self.fixtures.run_git
        git(root, "remote", "add", "origin", "https://github.com/example/repo.git")
        module = self.janitor("janitor_task_route_identity")
        expected = ("example", "repo")
        task_route = module.admit_task_fetch_route(
            root, expected_repository=expected
        )
        module.require_task_fetch_route(root, task_route)
        primary_route = module.admit_origin_route(
            root, expected_repository=expected
        )

        head = git(root, "rev-parse", "HEAD").stdout.strip()
        pr = {
            "url": "https://github.com/example/repo/pull/7",
            "state": "MERGED",
            "headRefName": "TASK-1",
            "headRefOid": head,
            "baseRefName": "main",
            "isCrossRepository": False,
        }
        with mock.patch.object(module, "github_pr", return_value=pr):
            observed, recoverable = module.read_merged_pr_witness(
                root,
                pr["url"],
                "TASK-1",
                head,
                "main",
                primary_route,
                expected,
                task_workspace=root,
                task_route=task_route,
            )
        self.assertTrue(recoverable)
        self.assertEqual(observed["url"], pr["url"])

        with mock.patch.object(
            module,
            "github_pr",
            return_value={**pr, "url": "https://github.com/other/repo/pull/7"},
        ), mock.patch.object(module, "merged_pr_contains_head") as ancestry:
            with self.assertRaisesRegex(RuntimeError, "different pull request"):
                module.read_merged_pr_witness(
                    root,
                    pr["url"],
                    "TASK-1",
                    head,
                    "main",
                    primary_route,
                    expected,
                    task_workspace=root,
                    task_route=task_route,
                )
            ancestry.assert_not_called()

        git(root, "remote", "set-url", "origin", "https://github.com/foreign/repo.git")
        with self.assertRaisesRegex(RuntimeError, "differs"):
            module.require_task_fetch_route(root, task_route)

    def test_closed_without_merge_is_exact_safe_preservation_mode(self) -> None:
        root = self.fixtures.create_repository()
        module = self.janitor("janitor_closed_without_merge")
        payload = json.loads(
            self.fixtures.janitor_input(
                root,
                cleanup_mode="closed_without_merge",
                pr_base_branch="not-applicable",
            )
        )
        output = io.StringIO()
        with (
            mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(sys, "stdout", output),
            mock.patch.object(module, "run") as command,
        ):
            self.assertEqual(module.main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["transition"], "task_janitor_done")
        self.assertIn("intentionally preserved", result["cleanup_report"])
        self.assertTrue(root.is_dir())
        command.assert_not_called()

        for invalid in ("", "Merged", " merged", "closed_without_merge ", None, "unknown"):
            with self.subTest(invalid=invalid):
                payload = json.loads(
                    self.fixtures.janitor_input(
                        root,
                        cleanup_mode="report_only",
                        pr_base_branch="not-applicable",
                    )
                )
                payload["cleanup_mode"] = invalid
                output = io.StringIO()
                with (
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                    mock.patch.object(sys, "stdout", output),
                    mock.patch.object(module, "run") as command,
                ):
                    with self.assertRaises(ValueError):
                        module.main()
                command.assert_not_called()

    def test_task_branch_identity_requires_successful_exact_attached_readback(self) -> None:
        module = self.janitor("janitor_exact_branch")
        module.require_task_branch_identity(
            "TASK-1",
            subprocess.CompletedProcess(
                ["git", "symbolic-ref"], 0, "TASK-1\n", ""
            ),
        )
        invalid = (
            ("TASK-1", 1, "TASK-1\n"),
            ("TASK-1", 0, ""),
            ("TASK-1", 0, "OTHER\n"),
            ("", 0, "TASK-1\n"),
            ("not-applicable", 0, "TASK-1\n"),
            ("none", 0, "TASK-1\n"),
            ("null", 0, "TASK-1\n"),
        )
        for branch, status, stdout in invalid:
            with self.subTest(branch=branch, status=status, stdout=stdout):
                with self.assertRaises(ValueError):
                    module.require_task_branch_identity(
                        branch,
                        subprocess.CompletedProcess(
                            ["git", "symbolic-ref"], status, stdout, ""
                        ),
                    )

    def test_main_rejects_matching_branch_stdout_when_symbolic_ref_failed(self) -> None:
        _, worktree, scripts, _ = self.fixtures.make_managed_worktree(
            with_v2=False
        )
        module = load_template_module(
            scripts / "workflow-task-janitor", "janitor_failed_symbolic_ref_main"
        )
        actual_run = module.run
        payload = json.loads(
            self.fixtures.janitor_input(
                worktree,
                branch_name="TASK-1",
                cleanup_mode="report_only",
                pr_base_branch="not-applicable",
            )
        )
        output = io.StringIO()

        def failed_symbolic_ref(args, **kwargs):
            if args[:3] == ["git", "symbolic-ref", "--quiet"]:
                return subprocess.CompletedProcess(args, 1, "TASK-1\n", "")
            return actual_run(args, **kwargs)

        with (
            mock.patch.object(module, "run", side_effect=failed_symbolic_ref),
            mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(sys, "stdout", output),
        ):
            self.assertEqual(module.main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["transition"], "task_janitor_blocked")
        self.assertIn("exact attached branch", result["cleanup_report"])
        self.assertTrue(worktree.is_dir())

    def test_direct_branch_admission_rejects_same_oid_and_dangling_symbolic_refs(
        self,
    ) -> None:
        module = self.janitor("janitor_symbolic_branch_admission")
        for case in ("same_oid_foreign_checkout", "dangling"):
            with self.subTest(case=case):
                root = self.fixtures.create_repository()
                head = self.fixtures.run_git(root, "rev-parse", "HEAD").stdout.strip()
                if case == "same_oid_foreign_checkout":
                    self.fixtures.run_git(root, "branch", "foreign")
                    worktree = root / "foreign-worktree"
                    self.fixtures.run_git(
                        root, "worktree", "add", "-q", str(worktree), "foreign"
                    )
                    self.fixtures.run_git(
                        root,
                        "symbolic-ref",
                        "refs/heads/TASK-1",
                        "refs/heads/foreign",
                    )
                else:
                    worktree = root / "task-worktree"
                    self.fixtures.run_git(
                        root,
                        "worktree",
                        "add",
                        "-q",
                        "-b",
                        "TASK-1",
                        str(worktree),
                    )
                    self.fixtures.run_git(
                        root,
                        "symbolic-ref",
                        "refs/heads/TASK-1",
                        "refs/heads/dangling-target",
                    )
                with self.assertRaisesRegex(RuntimeError, "symbolic"):
                    module.admit_local_branch_for_native_delete(
                        root, worktree, "TASK-1", head
                    )

    def test_direct_branch_admission_accepts_direct_ref_and_exact_absence(self) -> None:
        root, worktree, _, _ = self.fixtures.make_managed_worktree(with_v2=False)
        module = self.janitor("janitor_direct_branch_positive")
        head = self.fixtures.run_git(worktree, "rev-parse", "HEAD").stdout.strip()

        module.admit_local_branch_for_native_delete(
            root, worktree, "TASK-1", head
        )
        self.assertEqual(module._direct_local_branch_oid(root, "TASK-1"), head)

        self.fixtures.run_git(root, "worktree", "remove", "--force", str(worktree))
        self.fixtures.run_git(
            root, "update-ref", "-d", "refs/heads/TASK-1", head
        )
        self.assertEqual(module._direct_local_branch_oid(root, "TASK-1"), "")

    def test_post_native_symbolic_task_refs_block_before_followup_effects(self) -> None:
        for case in ("checked_out_referent", "unattached_referent", "dangling"):
            with self.subTest(case=case):
                root, worktree, scripts, marker = self.fixtures.make_managed_worktree(
                    with_v2=True
                )
                root = root.resolve()
                worktree = worktree.resolve()
                head = self.fixtures.run_git(
                    worktree, "rev-parse", "HEAD"
                ).stdout.strip()

                if case == "dangling":
                    symbolic_target = "refs/heads/missing-target"
                else:
                    self.fixtures.run_git(root, "branch", "foreign", head)
                    symbolic_target = "refs/heads/foreign"
                    foreign_worktree = (
                        root / ".kent" / "worktrees" / "TASK-1-foreign"
                    )
                    if case == "checked_out_referent":
                        self.fixtures.run_git(
                            root,
                            "worktree",
                            "add",
                            "-q",
                            str(foreign_worktree),
                            "foreign",
                        )

                wrapper = self.fixtures.completed_wrapper(root)
                wrapper_text = wrapper.read_text()
                deletion = (
                    'git -C "$KENT_TEST_PRIMARY" worktree remove --force "$target"\n'
                )
                self.assertIn(deletion, wrapper_text)
                wrapper.write_text(
                    wrapper_text.replace(
                        deletion,
                        deletion
                        + "git -C \"$KENT_TEST_PRIMARY\" symbolic-ref "
                        + f"refs/heads/TASK-1 {symbolic_target}\n",
                        1,
                    )
                )

                module = load_template_module(
                    scripts / "workflow-task-janitor",
                    f"janitor_post_native_symbolic_{case}",
                )
                wrapper_log = root / "post-native-wrapper.json"
                environment = self.fixtures.managed_pr_environment(
                    root, worktree, wrapper=wrapper
                )
                environment["KENT_TEST_WRAPPER_LOG"] = str(wrapper_log)
                native_done = root / ".kent-test-delete-done"
                actual_run = module.run
                post_native_effects: list[tuple[str, ...]] = []

                def observe_post_native_effects(args, **kwargs):
                    command = tuple(str(value) for value in args)
                    if (
                        native_done.exists()
                        and command[:1] == ("git",)
                        and len(command) > 1
                        and command[1] in {"fetch", "ls-remote", "push", "update-ref"}
                    ):
                        post_native_effects.append(command)
                    return actual_run(args, **kwargs)

                payload = json.loads(
                    self.fixtures.janitor_input(
                        worktree,
                        branch_name="TASK-1",
                        pr_url="https://github.com/example/repo/pull/1",
                        cleanup_mode="merged",
                        pr_base_branch="main",
                        cleanup_report=marker,
                    )
                )
                with (
                    mock.patch.object(
                        module, "run", side_effect=observe_post_native_effects
                    ),
                    mock.patch.object(
                        module,
                        "_admit_managed_runtime_parent",
                        return_value=(True, "fixture admitted"),
                    ),
                    mock.patch.object(
                        module,
                        "_prepare_v2_managed_runtime_state",
                        return_value=None,
                    ),
                ):
                    code, result = self._invoke_main(
                        module, payload, environment=environment
                    )

                self.assertEqual(code, 0)
                self.assertEqual(result["transition"], "task_janitor_blocked")
                self.assertIn("Local branch disposition: unknown.", result["cleanup_report"])
                self.assertEqual(result["cleanup_report"].count(marker), 1)
                self.assertTrue(result["cleanup_report"].endswith(marker))
                self.assertNotIn(
                    "preserved because its OID changed",
                    result["cleanup_report"],
                )
                self.assertTrue(native_done.is_file())
                self.assertFalse(worktree.exists())
                self.assertEqual(len(post_native_effects), 0)
                self.assertEqual(
                    json.loads(wrapper_log.read_text())[0],
                    "delete",
                )
                self.assertEqual(
                    self.fixtures.run_git(
                        root, "symbolic-ref", "refs/heads/TASK-1"
                    ).stdout.strip(),
                    symbolic_target,
                )
                if case != "dangling":
                    self.assertEqual(
                        self.fixtures.run_git(
                            root, "rev-parse", "refs/heads/foreign"
                        ).stdout.strip(),
                        head,
                    )
                    if case == "checked_out_referent":
                        self.assertTrue(foreign_worktree.is_dir())
                        self.assertEqual(
                            self.fixtures.run_git(
                                foreign_worktree, "branch", "--show-current"
                            ).stdout.strip(),
                            "foreign",
                        )
                self.assertEqual(
                    self.fixtures.run_git(
                        Path(
                            self.fixtures.run_git(
                                root,
                                "config",
                                "--get",
                                "kent.test.githubBareRemote",
                            ).stdout.strip()
                        ),
                        "rev-parse",
                        "refs/heads/TASK-1",
                    ).stdout.strip(),
                    head,
                )

    def test_main_blocks_remote_groups_and_custom_transport_before_fetch(self) -> None:
        for case in ("one_member_group", "multiple_member_group", "custom_helper"):
            with self.subTest(case=case):
                root, worktree, scripts, marker = self.fixtures.make_managed_worktree(
                    with_v2=True
                )
                root = root.resolve()
                worktree = worktree.resolve()
                wrapper = self.fixtures.completed_wrapper(root)
                wrapper_log = root / "blocked-wrapper.json"
                environment = self.fixtures.managed_pr_environment(
                    root, worktree, wrapper=wrapper
                )
                environment["KENT_TEST_WRAPPER_LOG"] = str(wrapper_log)
                helper_log = root / "remote-helper-invoked"
                if case.endswith("_group"):
                    remotes = root / "fixture-remotes"
                    remotes.mkdir()
                    first = remotes / "first.git"
                    first.mkdir()
                    self.fixtures.run_git(first, "init", "--bare", "-q")
                    self.fixtures.run_git(root, "remote", "add", "alternate", str(first))
                    self.fixtures.run_git(
                        root, "config", "--add", "remotes.origin", "alternate"
                    )
                    if case == "multiple_member_group":
                        second = remotes / "second.git"
                        second.mkdir()
                        self.fixtures.run_git(second, "init", "--bare", "-q")
                        self.fixtures.run_git(root, "remote", "add", "secondary", str(second))
                        self.fixtures.run_git(
                            root, "config", "--add", "remotes.origin", "secondary"
                        )
                else:
                    helper_directory = root / "fixture-remote-helper"
                    helper_directory.mkdir()
                    helper = helper_directory / "git-remote-audit"
                    helper.write_text(
                        "#!/bin/sh\n"
                        "touch \"$KENT_TEST_REMOTE_HELPER_LOG\"\n"
                        "exit 1\n"
                    )
                    helper.chmod(0o755)
                    self.fixtures.run_git(
                        root, "config", "remote.origin.vcs", "audit"
                    )
                    environment["PATH"] = (
                        str(helper_directory)
                        + os.pathsep
                        + environment.get("PATH", "")
                    )
                    environment["KENT_TEST_REMOTE_HELPER_LOG"] = str(helper_log)

                pr_state = Path(environment["KENT_TEST_PR_STATE"])
                pr = json.loads(pr_state.read_text())
                pr["headRefOid"] = "f" * 40
                pr_state.write_text(json.dumps(pr))

                module = load_template_module(
                    scripts / "workflow-task-janitor",
                    f"janitor_blocked_remote_route_{case}",
                )
                actual_run = module.run
                network_operations: list[tuple[str, ...]] = []

                def observe_network_operations(args, **kwargs):
                    command = tuple(str(value) for value in args)
                    if (
                        command[:1] == ("git",)
                        and len(command) > 1
                        and command[1] in {"fetch", "ls-remote", "push"}
                    ):
                        network_operations.append(command)
                    return actual_run(args, **kwargs)

                payload = json.loads(
                    self.fixtures.janitor_input(
                        worktree,
                        branch_name="TASK-1",
                        pr_url="https://github.com/example/repo/pull/1",
                        cleanup_mode="merged",
                        pr_base_branch="main",
                        cleanup_report=marker,
                    )
                )
                with mock.patch.object(
                    module, "run", side_effect=observe_network_operations
                ):
                    code, result = self._invoke_main(
                        module, payload, environment=environment
                    )

                self.assertEqual(code, 0)
                self.assertEqual(result["transition"], "task_janitor_blocked")
                self.assertTrue(worktree.is_dir())
                self.assertEqual(network_operations, [])
                self.assertFalse(wrapper_log.exists())
                self.assertFalse(helper_log.exists())

    def test_main_admits_multi_fetch_single_push_and_rejects_fallback_fanout(
        self,
    ) -> None:
        for case in ("explicit_single_push", "fallback_multi_push"):
            with self.subTest(case=case):
                root, worktree, scripts, marker = self.fixtures.make_managed_worktree(
                    with_v2=True
                )
                root = root.resolve()
                worktree = worktree.resolve()
                self.fixtures.run_git(
                    root,
                    "remote",
                    "set-url",
                    "--add",
                    "origin",
                    "https://github.com/mirror/readonly.git",
                )
                if case == "explicit_single_push":
                    self.fixtures.run_git(
                        root,
                        "config",
                        "--add",
                        "remote.origin.pushurl",
                        "https://github.com/example/repo.git",
                    )
                    bare_remote = Path(
                        self.fixtures.run_git(
                            root,
                            "config",
                            "--get",
                            "kent.test.githubBareRemote",
                        ).stdout.strip()
                    )
                    self.fixtures.run_git(
                        bare_remote,
                        "update-ref",
                        "-d",
                        "refs/heads/TASK-1",
                    )

                wrapper = self.fixtures.completed_wrapper(root)
                wrapper_log = root / "route-wrapper.json"
                environment = {
                    **os.environ,
                    "KENT_WORKTREE_WRAPPER": str(wrapper),
                    "KENT_TEST_PRIMARY": str(root),
                    "KENT_TEST_WRAPPER_LOG": str(wrapper_log),
                }
                module = load_template_module(
                    scripts / "workflow-task-janitor",
                    f"janitor_multi_fetch_{case}",
                )
                payload = json.loads(
                    self.fixtures.janitor_input(
                        worktree,
                        branch_name="TASK-1",
                        cleanup_mode="no_pr",
                        pr_base_branch="not-applicable",
                        cleanup_report=marker,
                    )
                )
                code, result = self._invoke_main(
                    module, payload, environment=environment
                )

                self.assertEqual(code, 0)
                if case == "explicit_single_push":
                    self.assertEqual(result["transition"], "task_janitor_done")
                    self.assertTrue(worktree.is_dir())
                    self.assertFalse(wrapper_log.exists())
                    self.assertIn("not conclusively recoverable", result["cleanup_report"])
                    route = module.admit_origin_route(root)
                    self.assertEqual(len(route["fetch_urls"]), 2)
                    self.assertEqual(
                        route["push_urls"],
                        ("https://github.com/example/repo.git",),
                    )
                else:
                    self.assertEqual(
                        result["transition"], "task_janitor_blocked"
                    )
                    self.assertTrue(worktree.is_dir())
                    self.assertFalse(wrapper_log.exists())

    def test_local_branch_lost_ack_is_settled_by_one_direct_ref_readback(self) -> None:
        root = self.fixtures.create_repository()
        self.fixtures.run_git(root, "branch", "TASK-1")
        head = self.fixtures.run_git(root, "rev-parse", "HEAD").stdout.strip()
        module = self.janitor("janitor_local_ack_settlement")
        actual_run = module.run
        update_calls: list[tuple[str, ...]] = []
        effect_status: dict[str, str] = {}

        def delete_then_lose_ack(args, **kwargs):
            if args[:3] == ["git", "update-ref", "--no-deref"]:
                update_calls.append(tuple(args))
                actual_run(args, **kwargs)
                return subprocess.CompletedProcess(args, 1, "", "lost acknowledgement")
            return actual_run(args, **kwargs)

        with mock.patch.object(module, "run", side_effect=delete_then_lose_ack):
            deleted, note, blocker = module.settle_local_branch(
                root,
                "TASK-1",
                head,
                "merged",
                _effect_status=effect_status,
            )

        self.assertTrue(deleted)
        self.assertFalse(blocker)
        self.assertIn("acknowledgement was uncertain", note)
        self.assertEqual(effect_status["disposition"], "absent")
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(
            module._direct_local_branch_oid(root, "TASK-1"),
            "",
        )

    def test_local_branch_report_disposition_tracks_observed_ref_state(self) -> None:
        module = self.janitor("janitor_local_disposition")

        present_root = self.fixtures.create_repository()
        self.fixtures.run_git(present_root, "branch", "TASK-1")
        head = self.fixtures.run_git(
            present_root, "rev-parse", "HEAD"
        ).stdout.strip()
        present_status: dict[str, str] = {}
        with mock.patch.object(
            module,
            "_local_branch_worktree",
            return_value=str(present_root / "occupied-worktree"),
        ):
            deleted, _, blocker = module.settle_local_branch(
                present_root,
                "TASK-1",
                head,
                "merged",
                _effect_status=present_status,
            )
        self.assertFalse(deleted)
        self.assertTrue(blocker)
        self.assertEqual(present_status["disposition"], "present")

        changed_root = self.fixtures.create_repository()
        original_head = self.fixtures.run_git(
            changed_root, "rev-parse", "HEAD"
        ).stdout.strip()
        self.fixtures.run_git(changed_root, "branch", "TASK-1", original_head)
        self.fixtures.run_git(
            changed_root, "commit", "--allow-empty", "-q", "-m", "changed tip"
        )
        changed_head = self.fixtures.run_git(
            changed_root, "rev-parse", "HEAD"
        ).stdout.strip()
        self.fixtures.run_git(
            changed_root, "update-ref", "refs/heads/TASK-1", changed_head
        )
        changed_status: dict[str, str] = {}
        deleted, _, blocker = module.settle_local_branch(
            changed_root,
            "TASK-1",
            original_head,
            "merged",
            _effect_status=changed_status,
        )
        self.assertFalse(deleted)
        self.assertTrue(blocker)
        self.assertEqual(changed_status["disposition"], "changed")

        absent_root = self.fixtures.create_repository()
        absent_head = self.fixtures.run_git(
            absent_root, "rev-parse", "HEAD"
        ).stdout.strip()
        absent_status: dict[str, str] = {}
        deleted, _, blocker = module.settle_local_branch(
            absent_root,
            "TASK-1",
            absent_head,
            "merged",
            _effect_status=absent_status,
        )
        self.assertTrue(deleted)
        self.assertFalse(blocker)
        self.assertEqual(absent_status["disposition"], "absent")

        unknown_status: dict[str, str] = {}
        with mock.patch.object(
            module,
            "_direct_local_branch_oid",
            side_effect=RuntimeError("readback unavailable"),
        ):
            deleted, _, blocker = module.settle_local_branch(
                absent_root,
                "TASK-1",
                absent_head,
                "merged",
                _effect_status=unknown_status,
            )
        self.assertFalse(deleted)
        self.assertTrue(blocker)
        self.assertEqual(unknown_status["disposition"], "unknown")

    def test_no_pr_remote_readback_failure_does_not_claim_local_preservation(self) -> None:
        module = self.janitor("janitor_no_pr_truthful_disposition")
        for remote_result in ("unavailable", "absent"):
            with self.subTest(remote_result=remote_result):
                root = self.fixtures.create_repository()
                self.fixtures.run_git(root, "branch", "TASK-1")
                head = self.fixtures.run_git(root, "rev-parse", "HEAD").stdout.strip()
                effect_status: dict[str, str] = {}
                remote_side_effect = (
                    RuntimeError("remote read unavailable")
                    if remote_result == "unavailable"
                    else None
                )
                with mock.patch.object(
                    module,
                    "remote_branch_oid",
                    side_effect=remote_side_effect,
                    return_value="" if remote_result == "absent" else None,
                ):
                    deleted, note, blocker = module.settle_local_branch(
                        root,
                        "TASK-1",
                        head,
                        "no_pr",
                        _effect_status=effect_status,
                    )

                self.assertFalse(deleted)
                self.assertIn("Remote branch", note)
                self.assertNotIn("preserved", blocker.lower())
                self.assertEqual(effect_status["disposition"], "present")
                self.assertEqual(
                    module._direct_local_branch_oid(root, "TASK-1"),
                    head,
                )

    def test_main_reports_observed_local_branch_disposition_not_a_false_claim(self) -> None:
        root, worktree, scripts, _ = self.fixtures.make_managed_worktree(
            with_v2=False
        )
        module = load_template_module(
            scripts / "workflow-task-janitor", "janitor_report_local_disposition"
        )
        output = io.StringIO()
        environment = {
            **os.environ,
            "KENT_WORKTREE_WRAPPER": str(self.fixtures.completed_wrapper(root)),
            "KENT_TEST_PRIMARY": str(root),
        }

        def local_branch_remains(*args, **kwargs):
            kwargs["_effect_status"]["disposition"] = "present"
            return (
                False,
                " Local branch TASK-1 remains at its admitted tip.",
                "Local branch remains after managed worktree deletion.",
            )

        payload = json.loads(
            self.fixtures.janitor_input(
                worktree,
                branch_name="TASK-1",
                cleanup_mode="report_only",
                pr_base_branch="not-applicable",
            )
        )
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(
                module,
                "admit_origin_route",
                return_value={"fetch_urls": ("fixture-fetch",), "push_urls": ("fixture-push",)},
            ),
            mock.patch.object(module, "head_matches_current_remote", return_value=True),
            mock.patch.object(module, "require_origin_route"),
            mock.patch.object(
                module, "_admit_managed_runtime_parent", return_value=(True, "admitted")
            ),
            mock.patch.object(module, "cleanup_session_uses_workspace", return_value=False),
            mock.patch.object(
                module,
                "capture_kent_worktree_record",
                return_value={
                    "variant": "registered",
                    "root": str(worktree),
                    "worktree_id": "test-worktree-record",
                },
            ),
            mock.patch.object(
                module,
                "delete_managed_worktree",
                return_value=(True, "Kent reports completed deletion.", ""),
            ),
            mock.patch.object(
                module, "settle_local_branch", side_effect=local_branch_remains
            ),
            mock.patch.object(
                module,
                "check_terminal_postconditions",
                side_effect=RuntimeError("local branch remains"),
            ),
            mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(sys, "stdout", output),
        ):
            prior_cwd = Path.cwd()
            try:
                self.assertEqual(module.main(), 0)
            finally:
                os.chdir(prior_cwd)

        result = json.loads(output.getvalue())
        self.assertEqual(result["transition"], "task_janitor_blocked")
        self.assertIn("Local branch disposition: present.", result["cleanup_report"])
        self.assertNotIn("Local branch disposition: unknown.", result["cleanup_report"])
        self.assertNotIn("Local branch disposition: absent.", result["cleanup_report"])

    def test_native_kent_readback_failure_does_not_claim_branch_preservation(self) -> None:
        root, worktree, scripts, _ = self.fixtures.make_managed_worktree(
            with_v2=False
        )
        module = load_template_module(
            scripts / "workflow-task-janitor",
            "janitor_kent_readback_unknown_disposition",
        )
        output = io.StringIO()

        def native_delete_then_readback_failure(*args, **kwargs):
            os.chdir(root)
            self.fixtures.run_git(root, "worktree", "remove", "--force", str(worktree))
            self.fixtures.run_git(root, "branch", "-D", "TASK-1")
            kwargs["_effect_status"]["state"] = "attempted"
            return (
                False,
                "Janitor removed managed worktree and Git registration, "
                "but Kent registration readback failed (io_failure).",
                "Kent registration postcondition could not be verified after "
                "worktree deletion; native local-branch cleanup may already "
                "have occurred, and remote cleanup was not attempted.",
            )

        payload = json.loads(
            self.fixtures.janitor_input(
                worktree,
                branch_name="TASK-1",
                cleanup_mode="report_only",
                pr_base_branch="not-applicable",
            )
        )
        with (
            mock.patch.object(
                module,
                "admit_origin_route",
                return_value={
                    "fetch_urls": ("fixture-fetch",),
                    "push_urls": ("fixture-push",),
                },
            ),
            mock.patch.object(module, "head_matches_current_remote", return_value=True),
            mock.patch.object(module, "require_origin_route"),
            mock.patch.object(
                module, "_admit_managed_runtime_parent", return_value=(True, "admitted")
            ),
            mock.patch.object(
                module,
                "cleanup_session_uses_workspace",
                return_value=False,
            ),
            mock.patch.object(
                module,
                "capture_kent_worktree_record",
                return_value={
                    "variant": "registered",
                    "root": str(worktree),
                    "worktree_id": "test-worktree-record",
                },
            ),
            mock.patch.object(
                module,
                "delete_managed_worktree",
                side_effect=native_delete_then_readback_failure,
            ),
            mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(sys, "stdout", output),
        ):
            prior_cwd = Path.cwd()
            try:
                self.assertEqual(module.main(), 0)
            finally:
                os.chdir(prior_cwd)

        result = json.loads(output.getvalue())
        self.assertEqual(result["transition"], "task_janitor_blocked")
        self.assertIn(
            "Local branch disposition: unknown; remote branch disposition: unknown.",
            result["cleanup_report"],
        )
        self.assertNotIn("preserved", result["cleanup_report"].lower())
        self.assertIn("native local-branch cleanup may already have occurred", result["blocker_reason"])
        self.assertFalse(worktree.exists())
        self.assertEqual(module._direct_local_branch_oid(root, "TASK-1"), "")

    def test_transport_failures_never_echo_remote_credentials_or_timeout_commands(self) -> None:
        module = self.janitor("janitor_safe_transport_errors")
        root = self.fixtures.create_repository()
        secret_url = "https://user:private-token@github.com/example/repo.git"
        with mock.patch.object(
            module.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["git", "ls-remote"], 128, "", secret_url
            ),
        ):
            with self.assertRaises(RuntimeError) as raised:
                module.run(
                    ["git", "ls-remote", secret_url],
                    cwd=root,
                )
        self.assertNotIn(secret_url, str(raised.exception))
        timed_out = subprocess.TimeoutExpired(
            ["git", "push", secret_url],
            timeout=1,
        )
        self.assertEqual(module.safe_error_code(timed_out), "command_timeout")
        self.assertNotIn(secret_url, module.safe_error_code(timed_out))

    def test_merged_pr_target_is_required_and_drift_blocks_fresh_witness(self) -> None:
        module = self.janitor("janitor_pr_target_admission")
        module.require_pr_target({"baseRefName": "main"}, "main")
        for invalid in ({}, {"baseRefName": ""}, {"baseRefName": "release"}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(RuntimeError):
                    module.require_pr_target(invalid, "main")

        root = self.fixtures.create_repository()
        self.fixtures.run_git(
            root,
            "remote",
            "add",
            "origin",
            "https://github.com/example/repo.git",
        )
        route = module.admit_origin_route(
            root, expected_repository=("example", "repo")
        )
        original = {
            "url": "https://github.com/example/repo/pull/1",
            "state": "MERGED",
            "headRefName": "TASK-1",
            "headRefOid": "a" * 40,
            "baseRefName": "main",
            "isCrossRepository": False,
        }
        with mock.patch.object(module, "github_pr", return_value={
            **original, "baseRefName": "release",
        }):
            with self.assertRaisesRegex(RuntimeError, "base branch"):
                module.require_same_merged_pr(
                    root,
                    "https://github.com/example/repo/pull/1",
                    "TASK-1",
                    "a" * 40,
                    "main",
                    route,
                    ("example", "repo"),
                    original,
                )

    def test_merged_target_drift_is_rechecked_at_native_delete_boundary(self) -> None:
        root, worktree, scripts, marker = self.fixtures.make_managed_worktree(
            with_v2=True
        )
        root = root.resolve()
        worktree = worktree.resolve()
        self.fixtures.run_git(
            root,
            "remote",
            "set-url",
            "origin",
            "https://github.com/example/repo.git",
        )
        head = self.fixtures.run_git(worktree, "rev-parse", "HEAD").stdout.strip()
        original_pr = {
            "url": "https://github.com/example/repo/pull/1",
            "state": "MERGED",
            "headRefName": "TASK-1",
            "headRefOid": head,
            "baseRefName": "main",
            "isCrossRepository": False,
        }
        module = load_template_module(
            scripts / "workflow-task-janitor", "janitor_target_drift_main"
        )
        output = io.StringIO()
        environment = {
            **os.environ,
            "KENT_WORKTREE_WRAPPER": str(self.fixtures.completed_wrapper(root)),
            "KENT_TEST_PRIMARY": str(root),
        }
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(
                module,
                "github_pr",
                side_effect=(
                    original_pr,
                    original_pr,
                    {**original_pr, "baseRefName": "release"},
                ),
            ) as pr_read,
            mock.patch.object(module, "merged_pr_contains_head", return_value=True),
            mock.patch.object(module, "remote_branch_oid", return_value=head),
            mock.patch.object(module, "cleanup_session_uses_workspace", return_value=False),
            mock.patch.object(
                module,
                "capture_kent_worktree_record",
                return_value={
                    "variant": "registered",
                    "root": str(worktree),
                    "worktree_id": "test-worktree-record",
                },
            ),
            mock.patch.object(
                module, "_admit_managed_runtime_parent",
                return_value=(True, "runtime parent admitted"),
            ),
            mock.patch.object(module, "_prepare_v2_managed_runtime_state", return_value=None),
            mock.patch.object(
                sys,
                "stdin",
                io.StringIO(
                    self.fixtures.janitor_input(
                        worktree,
                        branch_name="TASK-1",
                        pr_url="https://github.com/example/repo/pull/1",
                        cleanup_mode="merged",
                        pr_base_branch="main",
                        cleanup_report=marker,
                    )
                ),
            ),
            mock.patch.object(sys, "stdout", output),
        ):
            prior_cwd = Path.cwd()
            try:
                code = module.main()
            finally:
                os.chdir(prior_cwd)

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["transition"], "task_janitor_blocked")
        self.assertIn("witness failed", payload["cleanup_report"])
        self.assertEqual(pr_read.call_count, 3)
        self.assertTrue(worktree.is_dir())

    def _make_restored_tracked_only_fixture(self, *, with_recovery: bool = False):
        import shutil

        from test_workflow_runtime import EVIDENCE_FIXTURE_OWNER
        from workflowkit.delivery import capture_producer

        root, worktree, scripts, _ = self.fixtures.make_managed_worktree(
            with_v2=False
        )
        root = root.resolve()
        worktree = worktree.resolve()
        shutil.copyfile(
            REPO_ROOT / "templates/project/workflow-checkpoint",
            scripts / "workflow-checkpoint",
        )
        ignore = worktree / ".gitignore"
        ignore.write_text(
            ignore.read_text()
            + "/build/kent-workflow/\n/native-fixture.json\n/fake-native/\n"
        )
        context = worktree / ".kent" / "context"
        context.mkdir(parents=True)
        (context / "implement.md").write_text("retained Task context\n")
        redaction = worktree / "redaction.json"
        redaction.write_bytes(b'{"status":"passed"}\n')
        self.fixtures.run_git(
            worktree, "add", ".gitignore", ".kent/context/implement.md", "redaction.json"
        )
        self.fixtures.run_git(
            worktree,
            "add",
            "-f",
            ".kent/scripts/workflow-task-janitor",
            ".kent/scripts/workflow-evidence-ledger",
            ".kent/scripts/workflow_runtime_contracts.py",
            ".kent/scripts/workflow-checkpoint",
        )
        self.fixtures.run_git(worktree, "commit", "-q", "-m", "terminal sources")

        native_temporary = tempfile.TemporaryDirectory(prefix="janitor-native-")
        self.addCleanup(native_temporary.cleanup)
        native_directory = Path(native_temporary.name)
        home = native_directory / "home"
        tools = home / ".kent" / "bin"
        tools.mkdir(parents=True)
        (tools / "kent-prepare-github-ci").symlink_to(
            REPO_ROOT / "scripts/prepare-github-ci"
        )

        owner = {
            "session_id": EVIDENCE_FIXTURE_OWNER["KENT_SESSION_ID"],
            "step_id": EVIDENCE_FIXTURE_OWNER["KENT_STEP_ID"],
        }
        identity = {
            "task_id": "native-task",
            "task_short_id": "TASK-1",
            "project_id": "native-project",
            "workspace_path": str(worktree),
            **owner,
        }
        locator, _ = capture_producer(
            worktree,
            identity=identity,
            producer_kind="restored-root-fixture",
            inputs={},
            result={"commentary": "Original cleanup report"},
        )
        ledger = load_template_module(
            scripts / "workflow-evidence-ledger", "janitor_restored_fixture_ledger"
        )
        runtime_support = load_template_module(
            scripts / "workflow_runtime_contracts.py",
            "janitor_restored_fixture_runtime",
        )
        lookup = lambda *_: {
            "task_short_id": "TASK-1",
            "workspace": str(worktree),
            "source_workspace": str(root),
            "owner": owner,
            "source_native_identity": {
                "project_id": "native-project",
                "workspace_id": "native-primary",
                "task_id": "native-task",
            },
        }
        with mock.patch.dict(
            os.environ,
            {**EVIDENCE_FIXTURE_OWNER, "HOME": str(home)},
            clear=False,
        ):
            ledger.append_entry(
                worktree,
                worktree / ".kent/runtime/TASK-1/evidence-ledger.jsonl",
                task="TASK-1",
                _payload={
                    "node_key": "cleanup",
                    "evidence_type": "cleanup",
                    "summary": f"Terminal cleanup\nKENT_PACKET_SOURCE_V1 {locator}",
                    "artifacts": ["redaction.json"],
                    "checks": [],
                    "decisions": [],
                    "context": {
                        "manifest_path": ".kent/context/implement.md",
                        "files_read": [],
                    },
                },
            )
            if with_recovery:
                ledger_path = (
                    worktree / ".kent/runtime/TASK-1/evidence-ledger.jsonl"
                )
                damaged = ledger_path.read_bytes() + b'{"incomplete":'
                ledger_path.write_bytes(damaged)
                recovery_owner = {
                    "session_id": "33333333-3333-4333-8333-333333333333",
                    "step_id": "44444444-4444-4444-8444-444444444444",
                }
                recovery_lookup = lambda *_: {
                    **lookup(worktree, "TASK-1"),
                    "owner": recovery_owner,
                }
                with mock.patch.dict(
                    os.environ,
                    {
                        "KENT_SESSION_ID": recovery_owner["session_id"],
                        "KENT_STEP_ID": recovery_owner["step_id"],
                    },
                    clear=False,
                ):
                    request = runtime_support.build_evidence_recovery_request(
                        damaged,
                        task_short_id="TASK-1",
                        owner=recovery_owner,
                    )
                    ledger.recover_evidence(
                        worktree,
                        task="TASK-1",
                        request=request,
                        confirm=runtime_support.canonical_sha256(request),
                        _native_lookup=recovery_lookup,
                    )
                    recovered_event, reused = ledger.append_entry(
                        worktree,
                        ledger_path,
                        task="TASK-1",
                        _payload={
                            "node_key": "cleanup",
                            "evidence_type": "cleanup",
                            "summary": "Recovered final cleanup evidence",
                            "artifacts": [],
                            "checks": [],
                            "decisions": [],
                            "context": {
                                "manifest_path": ".kent/context/implement.md",
                                "files_read": [],
                            },
                        },
                    )
                self.assertFalse(reused)
                self.assertTrue(
                    runtime_support.is_ordinary_evidence_record(recovered_event)
                )
            with mock.patch.object(
                ledger,
                "read_json_input",
                return_value={
                    "schema": "terminal-evidence-seal-request-v1",
                    "operation_report_digests": [],
                    "redaction": {
                        "status": "passed",
                        "report_sha256": hashlib.sha256(
                            redaction.read_bytes()
                        ).hexdigest(),
                    },
                    "retention_class": "cleanup_report_only",
                },
            ):
                sealed = ledger.seal_terminal(worktree, task="TASK-1")
            lookup = lambda *_: {
                "task_short_id": "TASK-1",
                "workspace": str(worktree),
                "source_workspace": str(root),
                "owner": owner,
                "source_native_identity": {
                    "project_id": "native-project",
                    "workspace_id": "native-primary",
                    "task_id": "native-task",
                },
            }
            preview = ledger.retain_evidence(
                worktree, task="TASK-1", _native_lookup=lookup
            )
            retained = ledger.retain_evidence(
                worktree,
                task="TASK-1",
                confirm=preview["confirm"],
                _native_lookup=lookup,
            )

        workflow = "44444444-4444-4444-8444-444444444444"
        detail = {
            "summary": {
                "id": "native-task",
                "project_id": "native-project",
                "short_id": "TASK-1",
                "workflow_id": workflow,
            },
            "project": {},
            "source_workspace": {
                "root_path": str(root),
                "workspace_id": "native-primary",
                "is_primary": True,
            },
            "worktree_path": str(worktree),
            "current_scripts": [
                {
                    "current_node": {
                        "node_id": "janitor",
                        "transition_branch_key": "cleanup",
                    },
                    "path": str(scripts / "workflow-task-janitor"),
                }
            ],
        }
        graph = {
            "workflow_id": workflow,
            "graph": {
                "nodes": [
                    {
                        "id": "janitor",
                        "kind": "script",
                        "script_path": str(scripts / "workflow-task-janitor"),
                    }
                ]
            },
        }
        native_file = native_directory / "task.json"
        graph_file = native_directory / "graph.json"
        native_file.write_text(json.dumps(detail))
        graph_file.write_text(json.dumps(graph))
        executable = native_directory / "kent"
        executable.write_text(
            "#!" + sys.executable + "\n"
            "import os,sys\n"
            "a=sys.argv[1:]\n"
            "if a[:1]==['project']: print('native-project')\n"
            "elif a[:2]==['task','show']: print(open(os.environ['NATIVE_FIXTURE']).read())\n"
            "elif a[:3]==['workflow','graph','inspect']: print(open(os.environ['NATIVE_GRAPH']).read())\n"
            "else: raise SystemExit(2)\n"
        )
        executable.chmod(0o755)
        environment = {
            "HOME": str(home),
            "PATH": str(native_directory) + os.pathsep + os.environ["PATH"],
            "NATIVE_FIXTURE": str(native_file),
            "NATIVE_GRAPH": str(graph_file),
        }
        janitor = load_template_module(
            scripts / "workflow-task-janitor", "janitor_restored_tracked_only"
        )
        native = {
            "task_id": "native-task",
            "node_id": "janitor",
            "transition_branch_key": "cleanup",
        }
        marker = sealed["terminal_marker"]
        bundle = Path(retained["bundle_path"])
        head = self.fixtures.run_git(worktree, "rev-parse", "HEAD").stdout.strip()
        return root, worktree, janitor, native, environment, marker, bundle, head, detail, native_file

    def test_restored_tracked_only_root_uses_current_native_binding_and_archive(self) -> None:
        import shutil

        (
            root,
            worktree,
            janitor,
            native,
            environment,
            marker,
            bundle,
            head,
            detail,
            native_file,
        ) = self._make_restored_tracked_only_fixture()
        shutil.rmtree(worktree / ".kent/runtime")
        janitor._NATIVE_KENT = native
        with mock.patch.dict(os.environ, environment, clear=False):
            admitted = janitor._admit_managed_runtime_parent(
                worktree,
                "TASK-1",
                cleanup_report=marker,
                expected_primary=root,
                expected_branch="TASK-1",
                expected_head=head,
            )
            closure: dict[str, object] = {}
            prepared = janitor._prepare_v2_managed_runtime_state(
                worktree,
                "TASK-1",
                cleanup_report=marker,
                expected_primary=root,
                expected_branch="TASK-1",
                expected_head=head,
                _closure=closure,
            )
        self.assertTrue(admitted[0], admitted)
        self.assertEqual(prepared, (True, "verified current native binding and immutable retained evidence"))
        self.assertTrue(closure["loaded"])
        self.assertTrue(closure["restored_tracked_only"])
        self.assertEqual(
            closure["retained"]["identity"]["workspace"],
            str(worktree),
        )
        self.assertEqual(
            closure["retained"]["identity"]["source_workspace"],
            str(root),
        )
        self.assertFalse((worktree / ".kent/runtime").exists())
        self.assertFalse((worktree / ".kent/runtime/TASK-1/evidence-ledger.jsonl").exists())
        self.assertTrue(bundle.is_dir())

        original_task = detail["summary"]["short_id"]
        detail["summary"]["short_id"] = "FOREIGN-1"
        native_file.write_text(json.dumps(detail))
        with mock.patch.dict(os.environ, environment, clear=False):
            foreign = janitor._admit_managed_runtime_parent(
                worktree,
                "TASK-1",
                cleanup_report=marker,
                expected_primary=root,
                expected_branch="TASK-1",
                expected_head=head,
            )
        self.assertFalse(foreign[0])
        detail["summary"]["short_id"] = original_task
        native_file.write_text(json.dumps(detail))

        original_node = detail["current_scripts"][0]["current_node"]["node_id"]
        detail["current_scripts"][0]["current_node"]["node_id"] = "foreign-node"
        native_file.write_text(json.dumps(detail))
        with mock.patch.dict(os.environ, environment, clear=False):
            foreign_node = janitor._admit_managed_runtime_parent(
                worktree,
                "TASK-1",
                cleanup_report=marker,
                expected_primary=root,
                expected_branch="TASK-1",
                expected_head=head,
            )
        self.assertFalse(foreign_node[0])
        detail["current_scripts"][0]["current_node"]["node_id"] = original_node
        native_file.write_text(json.dumps(detail))

        dirty = worktree / "untracked-restored-file"
        dirty.write_text("unaccounted\n")
        with mock.patch.dict(os.environ, environment, clear=False):
            dirty_result = janitor._admit_managed_runtime_parent(
                worktree,
                "TASK-1",
                cleanup_report=marker,
                expected_primary=root,
                expected_branch="TASK-1",
                expected_head=head,
            )
        self.assertFalse(dirty_result[0])
        dirty.unlink()

        with mock.patch.dict(os.environ, environment, clear=False):
            changed_head = janitor._admit_managed_runtime_parent(
                worktree,
                "TASK-1",
                cleanup_report=marker,
                expected_primary=root,
                expected_branch="TASK-1",
                expected_head="0" * len(head),
            )
            changed_branch = janitor._admit_managed_runtime_parent(
                worktree,
                "TASK-1",
                cleanup_report=marker,
                expected_primary=root,
                expected_branch="FOREIGN-1",
                expected_head=head,
            )
            unavailable_source = janitor._admit_managed_runtime_parent(
                worktree,
                "TASK-1",
                cleanup_report=marker,
                expected_primary=root / "missing-source",
                expected_branch="TASK-1",
                expected_head=head,
            )
        self.assertFalse(changed_head[0])
        self.assertFalse(changed_branch[0])
        self.assertFalse(unavailable_source[0])

        unaccounted_runtime = worktree / ".kent/runtime/TASK-1/unaccounted"
        unaccounted_runtime.parent.mkdir(parents=True)
        unaccounted_runtime.write_bytes(b"new runtime material\n")
        with mock.patch.dict(os.environ, environment, clear=False):
            new_runtime = janitor._admit_managed_runtime_parent(
                worktree,
                "TASK-1",
                cleanup_report=marker,
                expected_primary=root,
                expected_branch="TASK-1",
                expected_head=head,
            )
        self.assertFalse(new_runtime[0])
        shutil.rmtree(worktree / ".kent/runtime")

        first_entry = next(path for path in bundle.iterdir() if path.name == "000")
        foreign_entry = bundle / "unaccounted"
        foreign_entry.write_bytes(b"foreign archive material\n")
        with self.assertRaises((OSError, ValueError)):
            janitor.revalidate_retained_archive(closure)
        foreign_entry.unlink()

        first_entry.chmod(0o400)
        with self.assertRaises((OSError, ValueError)):
            janitor.revalidate_retained_archive(closure)
        first_entry.chmod(0o600)
        saved_entry = first_entry.read_bytes()
        first_entry.unlink()
        with self.assertRaises((OSError, ValueError)):
            janitor.revalidate_retained_archive(closure)
        first_entry.write_bytes(saved_entry)
        first_entry.chmod(0o600)

        first_entry.write_bytes(b"tampered")
        with mock.patch.dict(os.environ, environment, clear=False):
            tampered = janitor._admit_managed_runtime_parent(
                worktree,
                "TASK-1",
                cleanup_report=marker,
                expected_primary=root,
                expected_branch="TASK-1",
                expected_head=head,
            )
        self.assertFalse(tampered[0])
        self.assertFalse((worktree / ".kent/runtime").exists())

    def test_restored_tracked_only_root_rejects_new_ignored_inventory_before_effects(self) -> None:
        import shutil

        (
            root,
            worktree,
            janitor,
            native,
            environment,
            marker,
            _bundle,
            _head,
            _detail,
            _native_file,
        ) = self._make_restored_tracked_only_fixture()
        shutil.rmtree(worktree / ".kent/runtime")
        unknown = worktree / "build/kent-workflow/new-unaccounted-material"
        unknown.parent.mkdir(parents=True, exist_ok=True)
        unknown.write_bytes(b"preserve this unaccounted ignored material\n")
        unknown_helper = (
            worktree / ".kent/scripts/new-unaccounted-helper"
        )
        unknown_helper.write_bytes(b"preserve this unaccounted helper\n")
        ignored = subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "check-ignore",
                "--quiet",
                str(unknown.relative_to(worktree)),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(ignored.returncode, 0)
        ignored_helper = subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "check-ignore",
                "--quiet",
                str(unknown_helper.relative_to(worktree)),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(ignored_helper.returncode, 0)
        janitor._NATIVE_KENT = native
        payload = json.loads(
            self.fixtures.janitor_input(
                worktree,
                branch_name="TASK-1",
                cleanup_mode="no_pr",
                pr_base_branch="not-applicable",
                cleanup_report=marker,
            )
        )
        payload["_kent"] = native
        deletion = mock.Mock(side_effect=AssertionError("native deletion reached"))
        settlement = mock.Mock(side_effect=AssertionError("branch settlement reached"))
        with (
            mock.patch.object(janitor, "admit_origin_route", return_value={}),
            mock.patch.object(janitor, "head_matches_current_remote", return_value=True),
            mock.patch.object(janitor, "admit_local_branch_for_native_delete"),
            mock.patch.object(janitor, "delete_managed_worktree", deletion),
            mock.patch.object(janitor, "settle_local_branch", settlement),
        ):
            code, result = self._invoke_main(
                janitor,
                payload,
                environment=environment,
            )

        self.assertEqual(code, 0)
        self.assertEqual(result["transition"], "task_janitor_blocked")
        self.assertIn("unknown_runtime_entry", result["cleanup_report"])
        self.assertIn(
            "unknown_runtime_entry: unadmitted runtime material",
            result["cleanup_report"],
        )
        self.assertNotIn(unknown.name, json.dumps(result))
        self.assertNotIn(unknown_helper.name, json.dumps(result))
        self.assertEqual(
            unknown.read_bytes(),
            b"preserve this unaccounted ignored material\n",
        )
        self.assertEqual(
            unknown_helper.read_bytes(),
            b"preserve this unaccounted helper\n",
        )
        deletion.assert_not_called()
        settlement.assert_not_called()

    def test_restored_tracked_only_root_rejects_changed_tracked_helper_bytes(self) -> None:
        import shutil

        (
            root,
            worktree,
            janitor,
            native,
            environment,
            marker,
            _bundle,
            head,
            _detail,
            _native_file,
        ) = self._make_restored_tracked_only_fixture()
        shutil.rmtree(worktree / ".kent/runtime")
        helper = worktree / ".kent/scripts/workflow-checkpoint"
        original = helper.read_bytes()
        helper.write_bytes(original + b"\n# modified helper fixture bytes\n")
        janitor._NATIVE_KENT = native
        try:
            with mock.patch.dict(os.environ, environment, clear=False):
                changed = janitor._admit_managed_runtime_parent(
                    worktree,
                    "TASK-1",
                    cleanup_report=marker,
                    expected_primary=root,
                    expected_branch="TASK-1",
                    expected_head=head,
                )
        finally:
            helper.write_bytes(original)
        self.assertFalse(changed[0])
        self.assertIn("restored Task root is not clean", changed[1])
        self.assertNotIn("workflow-checkpoint", changed[1])

    def test_restored_tracked_only_root_ignores_valid_recovery_boundary_rows(self) -> None:
        import shutil

        (
            root,
            worktree,
            janitor,
            native,
            environment,
            marker,
            _bundle,
            head,
            _detail,
            _native_file,
        ) = self._make_restored_tracked_only_fixture(with_recovery=True)
        shutil.rmtree(worktree / ".kent/runtime")
        janitor._NATIVE_KENT = native
        with mock.patch.dict(os.environ, environment, clear=False):
            admitted = janitor._admit_managed_runtime_parent(
                worktree,
                "TASK-1",
                cleanup_report=marker,
                expected_primary=root,
                expected_branch="TASK-1",
                expected_head=head,
            )
        self.assertTrue(admitted[0], admitted)
        self.assertTrue(_bundle.is_dir())
        self.assertFalse((worktree / ".kent/runtime").exists())

    def test_runtime_safety_refusals_use_closed_non_sensitive_codes(self) -> None:
        janitor = self.janitor("janitor_safe_diagnostic_codes")
        with tempfile.TemporaryDirectory(prefix="janitor-inventory-limit-") as name:
            directory = Path(name)
            for index in range(janitor.MAX_RUNTIME_ENTRIES + 1):
                (directory / f"entry-{index:03d}").touch()
            descriptor = os.open(
                directory,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                with self.assertRaises(janitor.JanitorSafetyError) as captured:
                    janitor._bounded_inventory(descriptor)
            finally:
                os.close(descriptor)
        inventory_detail = janitor.safe_error_detail(captured.exception)
        self.assertEqual(captured.exception.code, "inventory_limit")
        self.assertEqual(
            inventory_detail,
            "inventory_limit: runtime evidence inventory exceeds a fixed limit",
        )
        self.assertNotIn("entry-256", inventory_detail)

        with tempfile.TemporaryDirectory(prefix="janitor-unsafe-entry-") as name:
            directory = Path(name)
            (directory / "private-target").write_text("private\n")
            (directory / "sensitive-evidence-name").symlink_to(
                directory / "private-target"
            )
            descriptor = os.open(
                directory,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                with self.assertRaises(janitor.JanitorSafetyError) as captured:
                    janitor._open_evidence_file(
                        descriptor,
                        "sensitive-evidence-name",
                    )
            finally:
                os.close(descriptor)
        unsafe_detail = janitor.safe_error_detail(captured.exception)
        self.assertEqual(
            unsafe_detail,
            "unsafe_runtime_entry: runtime evidence is unsafe",
        )
        self.assertNotIn("sensitive-evidence-name", unsafe_detail)

        root = self.fixtures.create_repository()
        git_command, git_environment = janitor._evidence_git_workspace(root)
        with self.assertRaises(janitor.JanitorSafetyError) as captured:
            janitor._verify_runtime_git_paths(
                root,
                ["tracked.txt"],
                git_command,
                git_environment,
            )
        tracked_detail = janitor.safe_error_detail(captured.exception)
        self.assertEqual(
            tracked_detail,
            "tracked_or_not_ignored: runtime entries are tracked or not ignored "
            "in selected workspace",
        )
        self.assertNotIn("tracked.txt", tracked_detail)
        self.assertEqual(
            janitor.safe_error_detail(OSError("sensitive arbitrary path")),
            "io_failure",
        )

    def test_missing_root_with_valid_archive_remains_blocked_without_reentry(self) -> None:
        import shutil

        root, worktree, _, native, _, marker, bundle, _, _, _ = (
            self._make_restored_tracked_only_fixture()
        )
        scripts = worktree / ".kent" / "scripts"
        stable = tempfile.TemporaryDirectory(prefix="janitor-missing-root-")
        self.addCleanup(stable.cleanup)
        stable_root = Path(stable.name)
        janitor_path = stable_root / "workflow-task-janitor"
        shutil.copyfile(scripts / "workflow-task-janitor", janitor_path)
        shutil.copyfile(
            scripts / "workflow_runtime_contracts.py",
            stable_root / "workflow_runtime_contracts.py",
        )
        module = load_template_module(janitor_path, "janitor_missing_root")
        self.fixtures.run_git(root, "worktree", "remove", "--force", str(worktree))
        self.assertFalse(worktree.exists())
        self.assertTrue(bundle.is_dir())

        payload = json.loads(
            self.fixtures.janitor_input(
                worktree,
                branch_name="TASK-1",
                cleanup_mode="no_pr",
                pr_base_branch="not-applicable",
                cleanup_report=marker,
            )
        )
        payload["_kent"] = native
        output = io.StringIO()
        with (
            mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(sys, "stdout", output),
        ):
            self.assertEqual(module.main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["transition"], "task_janitor_blocked")
        self.assertIn(
            "cannot infer deletion of missing workspace",
            result["cleanup_report"],
        )
        self.assertNotIn("preserved", result["cleanup_report"])
        self.assertTrue(result["cleanup_report"].endswith(marker))
        self.assertTrue(bundle.is_dir())
        self.assertFalse(worktree.exists())

        archived_entry = next(path for path in bundle.iterdir() if path.name == "000")
        archived_entry.write_bytes(b"tampered after Task-root loss")
        output = io.StringIO()
        with (
            mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(sys, "stdout", output),
        ):
            self.assertEqual(module.main(), 0)
        tampered_result = json.loads(output.getvalue())
        self.assertEqual(tampered_result["transition"], "task_janitor_blocked")
        self.assertIn(
            "cannot infer deletion of missing workspace",
            tampered_result["cleanup_report"],
        )
        self.assertNotIn("preserved", tampered_result["cleanup_report"])
        self.assertTrue(tampered_result["cleanup_report"].endswith(marker))
        self.assertFalse(worktree.exists())

        tampered_output = io.StringIO()
        with (
            mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(sys, "stdout", tampered_output),
        ):
            self.assertEqual(module.main(), 0)
        tampered_bundle_result = json.loads(tampered_output.getvalue())
        self.assertEqual(
            tampered_bundle_result["transition"],
            "task_janitor_blocked",
        )
        self.assertIn(
            "cannot infer deletion of missing workspace",
            tampered_bundle_result["cleanup_report"],
        )
        self.assertNotIn("preserved", tampered_bundle_result["cleanup_report"])
        self.assertTrue(tampered_bundle_result["cleanup_report"].endswith(marker))
        self.assertFalse(worktree.exists())
        self.assertTrue(bundle.is_dir())
    def test_lost_native_ack_uses_preloaded_retained_archive_and_readbacks(self) -> None:
        root, worktree, scripts, _ = self.fixtures.make_managed_worktree(
            with_v2=False
        )
        root = root.resolve()
        worktree = worktree.resolve()
        task = "TASK-1"
        owner = {
            "session_id": "11111111-1111-4111-8111-111111111111",
            "step_id": "33333333-3333-4333-8333-333333333333",
        }
        worktree.joinpath(".gitignore").write_text(
            "/.kent/runtime/\n/.kent/scripts/\n/build/kent-workflow/\n"
        )
        profile = worktree / ".kent" / "workflow-profile.toml"
        profile.write_text('[policies]\ndelivery_reliability = "v1"\n')
        context = worktree / ".kent" / "context"
        context.mkdir(parents=True)
        (context / "implement.md").write_text("retention fixture\n")
        report = worktree / "redaction.json"
        report.write_bytes(b'{"status":"passed"}\n')
        self.fixtures.run_git(
            worktree,
            "add",
            ".gitignore",
            ".kent/workflow-profile.toml",
            ".kent/context/implement.md",
            "redaction.json",
        )
        self.fixtures.run_git(worktree, "commit", "-q", "-m", "retention evidence setup")

        ledger = load_template_module(
            scripts / "workflow-evidence-ledger", "janitor_retention_audit_ledger"
        )
        runtime = load_template_module(
            scripts / "workflow_runtime_contracts.py",
            "janitor_retention_audit_runtime",
        )
        identity = {
            "task_short_id": task,
            "workspace": str(worktree.resolve()),
            "source_workspace": str(root.resolve()),
            "owner": owner,
            "source_native_identity": {
                "project_id": "native-project",
                "workspace_id": "native-primary",
                "task_id": "native-task",
            },
        }
        event = {
            "node_key": "cleanup",
            "evidence_type": "cleanup",
            "summary": "retained terminal fixture",
            "artifacts": ["redaction.json"],
            "checks": [],
            "decisions": [],
            "context": {
                "manifest_path": ".kent/context/implement.md",
                "files_read": [],
            },
        }
        seal_request = {
            "schema": runtime.TERMINAL_SEAL_REQUEST_SCHEMA,
            "operation_report_digests": [],
            "redaction": {
                "status": "passed",
                "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
            },
            "retention_class": "cleanup_report_only",
        }
        with mock.patch.dict(
            os.environ,
            {
                **EVIDENCE_FIXTURE_OWNER,
                "KENT_SESSION_ID": owner["session_id"],
                "KENT_STEP_ID": owner["step_id"],
            },
            clear=False,
        ):
            with mock.patch.object(ledger, "read_json_input", return_value=event):
                ledger.append_entry(
                    worktree,
                    worktree / ".kent/runtime/TASK-1/evidence-ledger.jsonl",
                    task=task,
                )
            with mock.patch.object(
                ledger, "read_json_input", return_value=seal_request
            ):
                sealed = ledger.seal_terminal(worktree, task=task)
            lookup = lambda *_: identity
            preview = ledger.retain_evidence(
                worktree, task=task, _native_lookup=lookup
            )
            retained = ledger.retain_evidence(
                worktree,
                task=task,
                confirm=preview["confirm"],
                _native_lookup=lookup,
            )
        bundle = Path(retained["bundle_path"])
        self.assertTrue(bundle.is_dir())
        self.assertNotEqual(bundle.parent.parent.resolve(), worktree.resolve())
        archive_before = {path.name: path.read_bytes() for path in bundle.iterdir()}
        fixture_remote = Path(
            self.fixtures.run_git(
                root, "config", "--get", "kent.test.githubBareRemote"
            ).stdout.strip()
        )
        self.assertTrue(fixture_remote.is_absolute())
        self.assertTrue(fixture_remote.is_dir())
        self.fixtures.run_git(
            root,
            "push",
            "-q",
            str(fixture_remote),
            "refs/heads/TASK-1:refs/heads/TASK-1",
        )

        wrapper = self.fixtures.completed_wrapper(root)
        wrapper_text = wrapper.read_text()
        wrapper_text = wrapper_text.replace(
            'open(os.environ["KENT_TEST_WRAPPER_LOG"], "w").write('
            "json.dumps(sys.argv[1:]))",
            'open(os.environ["KENT_TEST_WRAPPER_LOG"], "a").write('
            'json.dumps(sys.argv[1:]) + "\\n")',
            1,
        )
        wrapper_text = wrapper_text.replace(
            'git -C "$KENT_TEST_PRIMARY" worktree remove --force "$target"\n',
            'git -C "$KENT_TEST_PRIMARY" worktree remove --force "$target"\n'
            'git -C "$KENT_TEST_PRIMARY" branch -D TASK-1 >/dev/null\n',
            1,
        )
        wrapper_text = wrapper_text.replace(
            "printf '%s\\n' ",
            "exit 23\nprintf '%s\\n' ",
            1,
        )
        wrapper.write_text(wrapper_text)
        janitor = load_template_module(
            scripts / "workflow-task-janitor", "janitor_lost_ack_retention"
        )
        ledger_adapter = type("RetainedLedgerAdapter", (), {})()
        ledger_adapter.find_retained_evidence = lambda root_path, **kwargs: (
            ledger.find_retained_evidence(
                root_path,
                task=kwargs["task"],
                marker=kwargs["marker"],
                _native_lookup=lookup,
            )
        )
        ledger_adapter.verify_retained_sources = ledger.verify_retained_sources
        archive_revalidations_after_delete: list[str] = []

        def revalidate_archive(identity, retained_task, receipt, observation):
            if not worktree.exists():
                archive_revalidations_after_delete.append(retained_task)
            return ledger.revalidate_retention_observation(
                identity, retained_task, receipt, observation
            )

        ledger_adapter.revalidate_retention_observation = revalidate_archive
        imports_after_root_removal: list[str] = []
        original_runtime_loader = janitor.runtime_contracts

        def guarded_ledger_loader():
            if not worktree.exists():
                imports_after_root_removal.append("evidence-ledger")
                raise AssertionError("lazy evidence-ledger import after deletion")
            return ledger_adapter

        def guarded_runtime_loader():
            if not worktree.exists():
                imports_after_root_removal.append("runtime")
                raise AssertionError("lazy runtime import after deletion")
            return original_runtime_loader()

        output = io.StringIO()
        wrapper_log = root / "wrapper-effects.jsonl"
        environment = {
            **os.environ,
            "KENT_WORKTREE_WRAPPER": str(wrapper),
            "KENT_TEST_PRIMARY": str(root),
            "KENT_TEST_WRAPPER_LOG": str(wrapper_log),
            "KENT_SESSION_ID": owner["session_id"],
            "KENT_STEP_ID": owner["step_id"],
        }
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(janitor, "evidence_ledger", guarded_ledger_loader),
            mock.patch.object(janitor, "runtime_contracts", guarded_runtime_loader),
            mock.patch.object(janitor, "prepare_provider_guard", return_value=None),
            mock.patch.object(
                sys,
                "stdin",
                io.StringIO(
                    self.fixtures.janitor_input(
                        worktree,
                        branch_name=task,
                        cleanup_mode="no_pr",
                        pr_base_branch="not-applicable",
                        cleanup_report=sealed["terminal_marker"],
                    )
                ),
            ),
            mock.patch.object(sys, "stdout", output),
        ):
            prior_cwd = Path.cwd()
            try:
                code = janitor.main()
            finally:
                os.chdir(prior_cwd)

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["transition"], "task_janitor_done", payload)
        self.assertIn("uncertain acknowledgement", payload["cleanup_report"])
        self.assertIn("Observed local branch disposition: absent", payload["cleanup_report"])
        self.assertFalse(worktree.exists())
        self.assertFalse(imports_after_root_removal)
        self.assertEqual(archive_revalidations_after_delete, [task])
        wrapper_calls = [
            json.loads(line) for line in wrapper_log.read_text().splitlines()
        ]
        self.assertEqual(len(wrapper_calls), 1)
        self.assertEqual(wrapper_calls[0][0], "delete")
        self.assertEqual(
            {path.name: path.read_bytes() for path in bundle.iterdir()},
            archive_before,
        )

    def test_lost_native_ack_subprocess_deletes_root_once_with_retained_closure(self) -> None:
        (
            root,
            worktree,
            janitor,
            native,
            fixture_environment,
            marker,
            bundle,
            head,
            _,
            _,
        ) = self._make_restored_tracked_only_fixture()
        archive_before = {path.name: path.read_bytes() for path in bundle.iterdir()}
        fixture_remote = Path(
            self.fixtures.run_git(
                root, "config", "--get", "kent.test.githubBareRemote"
            ).stdout.strip()
        )
        self.fixtures.run_git(
            root,
            "push",
            "-q",
            str(fixture_remote),
            "refs/heads/TASK-1:refs/heads/TASK-1",
        )

        wrapper = self.fixtures.completed_wrapper(root)
        wrapper_text = wrapper.read_text()
        wrapper_text = wrapper_text.replace(
            'git -C "$KENT_TEST_PRIMARY" worktree remove --force "$target"\n',
            'git -C "$KENT_TEST_PRIMARY" worktree remove --force "$target"\n'
            'git -C "$KENT_TEST_PRIMARY" branch -D TASK-1 >/dev/null\n',
            1,
        )
        wrapper_text = wrapper_text.replace(
            "printf '%s\\n' ",
            "exit 23\nprintf '%s\\n' ",
            1,
        )
        wrapper.write_text(wrapper_text)

        log_root = tempfile.TemporaryDirectory(prefix="janitor-wrapper-effects-")
        self.addCleanup(log_root.cleanup)
        wrapper_log = Path(log_root.name) / "effects.jsonl"
        payload = json.loads(
            self.fixtures.janitor_input(
                worktree,
                branch_name="TASK-1",
                cleanup_mode="no_pr",
                pr_base_branch="not-applicable",
                cleanup_report=marker,
            )
        )
        payload["_kent"] = native
        environment = {
            **os.environ,
            **fixture_environment,
            **EVIDENCE_FIXTURE_OWNER,
            "KENT_WORKTREE_WRAPPER": str(wrapper),
            "KENT_TEST_PRIMARY": str(root),
            "KENT_TEST_WRAPPER_LOG": str(wrapper_log),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        result = subprocess.run(
            [str(worktree / ".kent/scripts/workflow-task-janitor")],
            cwd=worktree,
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=60,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["transition"], "task_janitor_done", output)
        self.assertIn("uncertain acknowledgement", output["cleanup_report"])
        self.assertIn(
            "Observed local branch disposition: absent",
            output["cleanup_report"],
        )
        self.assertIn(
            "published branch retained at the exact admitted tip",
            output["cleanup_report"],
        )
        self.assertEqual(output["cleanup_report"].count(marker), 1)
        runtime = load_template_module(
            REPO_ROOT / "workflowkit/runtime.py",
            "janitor_subprocess_marker_runtime",
        )
        runtime.validate_cleanup_report(output["cleanup_report"])
        self.assertFalse(worktree.exists())
        self.assertEqual(
            janitor._direct_local_branch_oid(root, "TASK-1"),
            "",
            "the wrapper removes the local Task ref before returning its lost ACK",
        )
        wrapper_calls = [
            json.loads(line) for line in wrapper_log.read_text().splitlines()
        ]
        self.assertEqual(len(wrapper_calls), 1)
        self.assertEqual(wrapper_calls[0][0], "delete")
        self.assertEqual(
            {path.name: path.read_bytes() for path in bundle.iterdir()},
            archive_before,
        )


if __name__ == "__main__":
    unittest.main()
