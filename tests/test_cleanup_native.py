from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

from workflowkit import delivery, operations
from workflowkit.cleanup import CleanupError, canonical_bytes, execute_inventory, validate_inventory
from workflowkit.cleanup_native import (
    NativeCleanupBackend,
    _check_path_components,
    _run_bounded,
    _validate_cleanup_route_graph,
    _validate_delivered_policy_graph,
)


PROJECT = "project-11111111-1111-4111-8111-111111111111"
TASK = "task-22222222-2222-4222-8222-222222222222"
OTHER_TASK = "task-33333333-3333-4333-8333-333333333333"
SESSION = "33333333-3333-4333-8333-333333333333"
WORKFLOW = "44444444-4444-4444-8444-444444444444"
SOURCE_NODE = "99999999-9999-4999-8999-999999999999"
ENTRY_NODE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DONE_NODE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
DELIVERED_GROUP = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
DISPOSAL_GROUP = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


class NativeCleanupTests(unittest.TestCase):
    def test_current_node_snapshot_reads_native_binary_uuids(self):
        self.writer.executescript("""
            ALTER TABLE task_current_nodes RENAME TO old_current_nodes;
            CREATE TABLE task_current_nodes (
                task_id TEXT NOT NULL, node_id BLOB NOT NULL,
                current_input_values_json TEXT NOT NULL,
                session_id TEXT, entered_by_edge_id BLOB
            );
            DROP TABLE old_current_nodes;
        """)
        node_bytes = bytes.fromhex(SOURCE_NODE.replace("-", ""))
        edge_bytes = bytes.fromhex(ENTRY_NODE.replace("-", ""))
        self.writer.execute(
            "INSERT INTO task_current_nodes VALUES (?,?,?,?,?)",
            (TASK, node_bytes, "{}", SESSION, edge_bytes),
        )
        self.writer.commit()
        for selected in ((), (TASK,)):
            with self.subTest(selected=selected):
                row, = self.backend._session_snapshot(selected)["current_nodes"]
                self.assertEqual((row["node_id"], row["entered_by_edge_id"]), (SOURCE_NODE, ENTRY_NODE))
                self.assertEqual((row["task_id"], row["session_id"]), (TASK, SESSION))

        for node, edge in ((node_bytes, None), (node_bytes[:-1], edge_bytes),
                           (node_bytes, edge_bytes + b"\0"), (b"\0" * 16, None),
                           (SOURCE_NODE, edge_bytes)):
            self.writer.execute(
                "UPDATE task_current_nodes SET node_id=?, entered_by_edge_id=?", (node, edge),
            )
            self.writer.commit()
            for selected in ((), (TASK,)):
                with self.subTest(node=node, edge=edge, selected=selected):
                    if node == node_bytes and edge is None:
                        row, = self.backend._session_snapshot(selected)["current_nodes"]
                        self.assertIsNone(row["entered_by_edge_id"])
                    else:
                        with self.assertRaises(CleanupError):
                            self.backend._session_snapshot(selected)

    def test_worktree_ack_v2_binds_exact_row_without_absence_claim(self):
        from test_merged_cleanup import base_inventory
        from workflowkit.cleanup import validate_receipt
        inventory = self.receipt_inventory()
        inventory.value["worktrees"] = base_inventory()["worktrees"]
        identity = {"task_id": TASK, "project_id": PROJECT, "node_id": ENTRY_NODE}
        with mock.patch.object(self.backend, "_script_identity", return_value=identity):
            self.backend.begin_inventory(inventory)
        receipt = self.backend._receipt(
            "worktree_native_ack", "worktree", 0, "acknowledged", {
                "incarnation": {"device": 1, "inode": 2, "ctime_ns": 3},
                "native_outcome": "NOT_REQUESTED",
            })
        producer = receipt["producer"]
        self.assertEqual(validate_receipt(receipt, inventory, producer), receipt)
        entry = receipt["entries"][0]
        for invalid in (
            {**receipt, "schema": "merged-cleanup-receipt-v1"},
            {**receipt, "inventory_sha256": "b" * 64},
            {**receipt, "entries": [{**entry, "disposition": "removed"}]},
            {**receipt, "entries": [{**entry, "proof": {**entry["proof"], "path_absent": True}}]},
            {**receipt, "entries": [{**entry, "proof": {**entry["proof"], "native_outcome": []}}]},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(CleanupError):
                validate_receipt(invalid, inventory, producer)

    def test_projected_inputs_keep_bounded_carriers_not_generic_payload(self):
        values = {**{f"unrelated_{index}": "x" * 50_000 for index in range(4)},
                  "cleanup_context": "\x00" * 65_536,
                  "cleanup_result": {"schema": "appsome-native-cleanup-result-v1", "endpoint": {}}}
        self.writer.execute("UPDATE task_current_nodes SET current_input_values_json=?", (json.dumps(values),))
        self.writer.commit()
        observed = self.backend._session_snapshot((TASK,))["current_nodes"][0]["current_input_values"]
        self.assertEqual(observed, {key: values[key] for key in ("cleanup_context", "cleanup_result")})
        for invalid in ([], {"cleanup_context": 1}, {"cleanup_result": "x" * 65_537}):
            self.writer.execute("UPDATE task_current_nodes SET current_input_values_json=?", (json.dumps(invalid),))
            self.writer.commit()
            with self.assertRaises(CleanupError):
                self.backend._session_snapshot((TASK,))

    def test_complete_scans_reject_row_byte_and_sql_type_overflow(self):
        from workflowkit.cleanup_native import _bounded_rows
        import time
        self.writer.execute("CREATE TABLE bounded_evidence(value)")
        self.writer.executemany("INSERT INTO bounded_evidence VALUES (?)", [("a" * 10,), ("b" * 10,)])
        for kwargs in (
            {"maximum_rows": 1, "budget": [100]},
            {"maximum_rows": 2, "budget": [19]},
            {"maximum_rows": 2, "budget": [100], "text_limit": 9},
            {"maximum_rows": 2, "budget": [100], "sql_types": [{"integer"}]},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(CleanupError):
                _bounded_rows(self.writer, "SELECT value FROM bounded_evidence", (),
                              columns=1, deadline=time.monotonic() + 3, **kwargs)

    def test_positive_session_ack_is_durable_before_failing_snapshot(self):
        inventory = self.receipt_inventory()
        identity = {"task_id": TASK, "project_id": PROJECT, "node_id": ENTRY_NODE}
        state = {"deleted": False, "appended": False, "failed": False, "deletes": 0}
        snapshot = self.backend._snapshot_once
        comments = self.backend._receipt_comments

        def observe(*args):
            if state["deleted"] and not state["failed"]:
                self.assertTrue(state["appended"])
                state["failed"] = True
                raise CleanupError("post-ACK snapshot unavailable")
            return snapshot(*args)

        def read_comments(*args, **kwargs):
            if state["deleted"] and not state["failed"]:
                self.assertIsNotNone(kwargs.get("exact"), "ACK path scanned complete history")
            return comments(*args, **kwargs)

        def qualify(_context):
            self.assertFalse(state["deleted"] and not state["failed"],
                             "producer was requalified inside the ACK fast path")
            return identity

        def runner(args, **kwargs):
            if args[:2] == ["session", "delete"]:
                state["deleted"] = True
                state["deletes"] += 1
                self.writer.execute("DELETE FROM sessions WHERE id=?", (SESSION,))
                self.writer.commit()
                return 0, json.dumps({"status": "ok", "result": {"session_id": SESSION}}).encode(), b""
            self.assertEqual(args[:3], ["task", "comment", "add"])
            state["appended"] = True
            body = args[args.index("--body") + 1]
            self.writer.execute("INSERT INTO task_comments VALUES (?,?,?,?,?,?,?)", (
                "comment-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", TASK, body,
                "agent", args[args.index("--author-id") + 1], 1, 1))
            self.writer.commit()
            return 1, b"", b"lost append response"

        with (
            mock.patch.object(self.backend, "_script_identity", side_effect=qualify),
            mock.patch.object(self.backend, "_snapshot_once", side_effect=observe),
            mock.patch.object(self.backend, "_receipt_comments", side_effect=read_comments),
            mock.patch.object(self.backend, "runner", side_effect=runner),
        ):
            self.backend.begin_inventory(inventory)
            with self.assertRaisesRegex(CleanupError, "post-ACK snapshot"):
                self.backend.delete_session(inventory.value["sessions"][0])
            self.assertEqual(self.backend._receipt_growth_plans[inventory.sha256][2], 1)
            self.backend.begin_inventory(inventory)
            self.assertEqual(self.backend.delete_session(inventory.value["sessions"][0]), "removed")
        self.assertEqual(state["deletes"], 1)

    def test_receipt_growth_and_exec_environment_fail_before_delete(self):
        inventory = self.receipt_inventory()
        identity = {"task_id": TASK, "project_id": PROJECT, "node_id": ENTRY_NODE}
        with mock.patch.object(self.backend, "_script_identity", return_value=identity):
            self.backend.begin_inventory(inventory)
            for limit in ("_MAX_RECEIPT_COMMENTS", "_MAX_RECEIPT_HISTORY_BYTES"):
                with (
                    mock.patch("workflowkit.cleanup_native." + limit, 1),
                    mock.patch.object(self.backend, "_receipt_comments", return_value=[]),
                ):
                    with mock.patch.object(
                        self.backend, "runner", side_effect=AssertionError("Delete before capacity admission"),
                    ) as runner:
                        with self.assertRaisesRegex(CleanupError, "capacity"):
                            self.backend.delete_session(inventory.value["sessions"][0])
                        runner.assert_not_called()
            self.backend._execution_environment = {"TOO_LARGE": "x" * 1_000_000}
            with mock.patch("os.sysconf", return_value=262144):
                with mock.patch.object(
                    self.backend, "runner", side_effect=AssertionError("Delete before argv admission"),
                ) as runner:
                    with self.assertRaisesRegex(CleanupError, "capacity"):
                        self.backend.delete_session(inventory.value["sessions"][0])
                    runner.assert_not_called()

    def receipt_inventory(self):
        original = Path.cwd()
        self.addCleanup(os.chdir, original)
        os.chdir(self.project_root)
        (self.persistence / f"projects/{PROJECT}/sessions").mkdir(parents=True, exist_ok=True)
        row = {
            **self.backend._snapshot_once((TASK,))["sessions"][0],
            "device": None, "inode": None,
            "supervisor_root_ids": [], "disposition": "remove",
        }
        value = {
            "task_id": TASK, "project_id": PROJECT, "workflow_id": WORKFLOW,
            "history_operation": None, "sessions": [row], "owned_containers": [],
            "worktrees": [],
            "policy_provenance": {"entry_node_id": ENTRY_NODE},
        }
        self.backend._script_native_context = {"task_id": TASK, "node_id": ENTRY_NODE}
        return SimpleNamespace(value=value, sha256="a" * 64)

    def test_snapshot_shared_byte_growth_blocks_before_delete(self):
        self._assert_snapshot_growth_blocks_before_delete(byte_limit=True)

    def test_snapshot_shared_row_growth_blocks_before_delete(self):
        self._assert_snapshot_growth_blocks_before_delete(byte_limit=False)

    def test_snapshot_batch_byte_growth_is_aggregated_before_effects(self):
        self._assert_snapshot_growth_blocks_before_delete(byte_limit=True, batch=True)

    def test_snapshot_batch_row_growth_is_aggregated_before_effects(self):
        self._assert_snapshot_growth_blocks_before_delete(byte_limit=False, batch=True)

    def _assert_snapshot_growth_blocks_before_delete(self, *, byte_limit, batch=False):
        from workflowkit import cleanup_native
        inventory = self.receipt_inventory()
        inventory.value["supervision"] = [{"task_ids": [TASK, OTHER_TASK]}]
        for index in range(2):
            self.writer.execute("INSERT INTO task_comments VALUES (?,?,?,?,?,?,?)", (
                f"comment-{index:08x}-aaaa-4aaa-8aaa-aaaaaaaaaaaa", OTHER_TASK,
                "unrelated evidence" * 100, "agent", "other", 1, 1))
        self.writer.commit()
        budgets = []
        original_scan = cleanup_native._bounded_rows

        def scan(*args, **kwargs):
            budgets.append(kwargs["budget"])
            return original_scan(*args, **kwargs)

        with mock.patch.object(cleanup_native, "_bounded_rows", side_effect=scan):
            self.backend._session_snapshot((TASK, OTHER_TASK))
        used = cleanup_native._MAX_SNAPSHOT_BYTES - budgets[-1][0]
        second = SimpleNamespace(
            value={**inventory.value, "task_id": OTHER_TASK, "sessions": [{
                **inventory.value["sessions"][0], "task_id": OTHER_TASK,
                "session_id": ENTRY_NODE,
                "artifact_relpath": f"projects/{PROJECT}/sessions/{ENTRY_NODE}",
            }]}, sha256="b" * 64,
        )

        def identity(_context):
            return {"task_id": self.backend._receipt_inventory.value["task_id"],
                    "project_id": PROJECT, "node_id": ENTRY_NODE}

        growth = []
        with mock.patch.object(self.backend, "_script_identity", side_effect=identity):
            for item in (inventory, second):
                self.backend.begin_inventory(item)
                growth.append(sum(
                    len(canonical_bytes(self.backend._receipt(stage, "session", 0, "removed", proof)))
                    + len(item.value["task_id"]) + len(self.backend._receipt_author_id) + 44 + 5 + 16
                    for stage, proof in (
                        ("session_native_settled", {
                            "session_id": item.value["sessions"][0]["session_id"], "native_status": "ok"}),
                        ("container_settled", {"path_absent": True}),
                    )
                ))
        limit = (
            ("_MAX_SNAPSHOT_BYTES", used + (max(growth) if batch else 50))
            if byte_limit else ("_MAX_RECEIPT_COMMENTS", 4 if batch else 3)
        )
        with (
            mock.patch.object(self.backend, "_script_identity", side_effect=identity),
            mock.patch.object(cleanup_native, limit[0], limit[1]),
            mock.patch.object(self.backend, "runner", return_value=(1, b"", b"")) as runner,
        ):
            self.backend.begin_inventory(inventory)
            # Both existing scopes fit, as does the own-Task receipt history.
            self.backend._session_snapshot((TASK, OTHER_TASK))
            with self.assertRaisesRegex(CleanupError, "snapshot.*capacity"):
                if batch:
                    self.backend.admit_effects()
                    self.backend.begin_inventory(second)
                    self.backend.admit_effects()
                else:
                    self.backend.delete_session(inventory.value["sessions"][0])
            runner.assert_not_called()

    def test_durable_native_ack_restores_settlement_without_repeating_delete(self):
        inventory = self.receipt_inventory()
        identity = {"task_id": TASK, "project_id": PROJECT, "node_id": ENTRY_NODE}
        counter = []

        def append(args, *, cwd, **kwargs):
            self.assertEqual(cwd, self.project_root)
            self.assertEqual(args[:4], ["task", "comment", "add", TASK])
            self.assertEqual(args[args.index("--project") + 1], PROJECT)
            self.assertEqual(args[args.index("--author") + 1], "agent")
            self.assertNotIn("KENT_SESSION_ID", kwargs["environment"])
            counter.append(args)
            body = args[args.index("--body") + 1]
            actor = args[args.index("--author-id") + 1]
            cid = "comment-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            self.writer.execute(
                "INSERT INTO task_comments VALUES (?,?,?,?,?,?,?)",
                (cid, TASK, body, "agent", actor, 1, 1),
            )
            self.writer.commit()
            # Lost response: only the exact durable readback can settle this.
            return 1, b"", b"lost connection after append"

        with mock.patch.object(self.backend, "_script_identity", return_value=identity):
            self.backend.begin_inventory(inventory)
            self.backend._native_ack = {
                SESSION: {"session_id": SESSION, "native_status": "ok"},
            }
            with mock.patch.object(self.backend, "runner", side_effect=append):
                self.backend.record_settlement("session_native_settled", "session", 0, "removed")
            self.writer.execute("DELETE FROM sessions WHERE id=?", (SESSION,))
            self.writer.commit()
            self.backend._snapshots = {}
            self.backend._session_settlements = {}
            self.backend.begin_inventory(inventory)
            with mock.patch.object(self.backend, "runner", side_effect=AssertionError("repeat Delete")):
                self.assertEqual(self.backend.delete_session(inventory.value["sessions"][0]), "removed")
            self.assertEqual(len(counter), 1)
            with self.assertRaisesRegex(CleanupError, "coverage"):
                self.backend.require_physical_coverage()

    def test_suppressed_stdout_retry_reuses_receipts_and_redeletes_recreated_root(self):
        from test_merged_cleanup import base_inventory

        self.receipt_inventory()  # establish a real outside cwd and parent
        victim = self.root / "retiring"
        victim.mkdir()
        session_path = self.persistence / f"projects/{PROJECT}/sessions/{SESSION}"
        session_path.mkdir()
        (session_path / "unknown").write_text("owned artifact", encoding="utf-8")
        info = session_path.stat()
        value = base_inventory()
        value["subject"]["worktree_path"] = str(victim)
        value["worktrees"][0]["path"] = str(victim)
        value["sessions"][0].update({
            "session_id": SESSION,
            "artifact_relpath": f"projects/{PROJECT}/sessions/{SESSION}",
            "workspace_id": "workspace-1", "worktree_id": "worktree-1",
            "device": info.st_dev, "inode": info.st_ino,
        })
        value["release"]["prepared_by_session_id"] = SESSION
        value["release"]["owner_session_ids"] = [SESSION]
        value["protected_roots"] = [{
            "path": str(self.project_root), "kind": "primary-workspace",
        }, {"path": str(self.persistence), "kind": "native-persistence"}]
        graph = self._policy_graph()
        entrypoint = Path(__file__).resolve().parents[1] / "scripts/cleanup-merged-task"
        _, value["policy_provenance"] = _validate_delivered_policy_graph(
            graph, project_id=PROJECT, workflow_id=WORKFLOW, entrypoint=entrypoint,
        )
        inventory = validate_inventory(value)
        self.writer.execute(
            "INSERT INTO worktrees VALUES (?,?,?)", ("worktree-1", "workspace-1", str(victim)),
        )
        self.writer.execute("UPDATE tasks SET managed_worktree_id='worktree-1' WHERE id=?", (TASK,))
        self.writer.execute("UPDATE sessions SET worktree_id='worktree-1' WHERE id=?", (SESSION,))
        self.writer.execute(
            "UPDATE task_current_nodes SET node_id=?,session_id=NULL WHERE task_id=?",
            (ENTRY_NODE, TASK),
        )
        self.writer.commit()
        # Prepare froze worktree-1 above; supported native leave clears the
        # owner's Session binding before the real executor revalidates it.
        self.writer.execute("UPDATE sessions SET worktree_id=NULL WHERE id=?", (SESSION,))
        self.writer.commit()
        native_deletes, root_deletes, receipt_stages = [], [], []
        retain_root = [False]
        detail = {
            "summary": {"id": TASK, "project_id": PROJECT, "workflow_id": WORKFLOW, "short_id": "KIT-1"},
            "status": {"native_state": "active"}, "worktree_path": str(victim),
            "source_workspace": {
                "is_primary": True, "workspace_id": "workspace-1", "root_path": str(self.project_root),
            },
            "current_scripts": [{
                "current_node": {"node_id": ENTRY_NODE, "session_id": None},
                "path": str(entrypoint),
            }],
        }

        def runner(args, *, cwd, **kwargs):
            self.assertEqual(cwd, self.project_root)
            if args[0] == "project":
                return 0, PROJECT.encode(), b""
            if args[:2] == ["task", "show"]:
                return 0, json.dumps(detail).encode(), b""
            if args[:3] == ["workflow", "graph", "inspect"]:
                return 0, json.dumps({"workflow_id": WORKFLOW, "graph": graph}).encode(), b""
            if args[:2] == ["session", "delete"]:
                self.assertIn("--confirm", args)
                native_deletes.append(args[-1])
                self.writer.execute("DELETE FROM sessions WHERE id=?", (SESSION,))
                self.writer.commit()
                return 0, json.dumps({"status": "ok", "result": {"session_id": SESSION}}).encode(), b""
            if args[:2] == ["worktree", "delete"]:
                root_deletes.append(args[-1])
                if not retain_root[0]:
                    shutil.rmtree(victim)
                return 0, b'{"status":"ok","result":{"cleanup":{"kind":"NOT_REQUESTED"}}}', b""
            if args[:3] == ["task", "comment", "add"]:
                body = args[args.index("--body") + 1]
                stage = json.loads(body)["stage"]
                if stage == "worktree_settled":
                    self.assertFalse(victim.exists())
                receipt_stages.append(stage)
                cid = f"comment-{len(receipt_stages):08x}-1111-4111-8111-111111111111"
                self.writer.execute(
                    "INSERT INTO task_comments VALUES (?,?,?,?,?,?,?)",
                    (cid, TASK, body, "agent", args[args.index("--author-id") + 1],
                     len(receipt_stages), len(receipt_stages)),
                )
                self.writer.commit()
                return 0, f"comment_id\t{cid}\ntask_id\t{TASK}\n".encode(), b""
            self.fail(args)

        wrong_head = [False]
        fail_after_worktree_ack = [False]

        def git(*args):
            if args[-3:] == ("worktree", "list", "--porcelain"):
                if not victim.exists() and fail_after_worktree_ack[0]:
                    fail_after_worktree_ack[0] = False
                    raise CleanupError("post-worktree-ACK Git unavailable")
                return (
                    f"worktree {victim}\nHEAD {'a' * 40}\nbranch refs/heads/task/cleanup\n\n"
                    if victim.exists() else ""
                )
            if args[-2:] in (("--verify", "HEAD"), ("--verify", "refs/heads/task/cleanup")):
                if args[-1] == "HEAD" and wrong_head[0]:
                    return "f" * 40
                return "a" * 40
            self.fail(args)

        def backend():
            result = NativeCleanupBackend(
                workspace=victim, kent=sys.executable, context=self.context, runner=runner,
            )
            result.workspace = self.project_root  # same handoff leave as production
            result._script_native_context = {"task_id": TASK, "node_id": ENTRY_NODE}
            result._git = git
            result._provider_pr = lambda _url: value["pull_request"]
            result._memory_call = mock.Mock(side_effect=AssertionError("R2 after inventory load"))
            return result

        with mock.patch.object(sys, "argv", [str(entrypoint)]):
            first_backend = backend()
            first = execute_inventory(inventory, first_backend)
            # Suppress final stdout; same loaded invocation can reconcile too.
            retried = execute_inventory(inventory, first_backend)
            self.assertEqual(retried, first)
            self.assertEqual(native_deletes, [SESSION])
            self.assertEqual(len(root_deletes), 1)
            # Native Resume recreates the same exact managed subject before
            # loading the attachment. An old root receipt cannot delete this
            # new incarnation by inference; the native operation runs again.
            victim.mkdir()
            wrong_head[0] = True
            with self.assertRaisesRegex(CleanupError, "head or local ref changed"):
                execute_inventory(inventory, backend())
            self.assertEqual(len(root_deletes), 1)
            wrong_head[0] = False
            recreated = execute_inventory(inventory, backend())
            self.assertEqual(recreated, first)
            self.assertEqual(native_deletes, [SESSION])
            self.assertEqual(len(root_deletes), 2)
            self.assertEqual(receipt_stages, [
                "session_native_settled", "container_settled",
                "worktree_native_ack", "worktree_settled",
                "worktree_native_ack", "worktree_settled",
            ])
            victim.mkdir()
            with mock.patch("workflowkit.cleanup_native._MAX_RECEIPT_COMMENTS", len(receipt_stages) + 1):
                with self.assertRaisesRegex(CleanupError, "capacity"):
                    execute_inventory(inventory, backend())
            self.assertEqual(len(root_deletes), 2, "new incarnation bypassed fresh receipt admission")
            interrupted = backend()
            fail_after_worktree_ack[0] = True
            with self.assertRaisesRegex(CleanupError, "post-worktree-ACK Git"):
                execute_inventory(inventory, interrupted)
            self.assertEqual(receipt_stages[-1], "worktree_native_ack")
            self.assertEqual(len(root_deletes), 3)
            resumed = execute_inventory(inventory, interrupted)
            self.assertEqual(resumed, first)
            self.assertEqual(len(root_deletes), 3, "ACK retry repeated native Delete")
            self.assertEqual(receipt_stages[-1], "worktree_settled")
            victim.mkdir()
            retain_root[0] = True
            surviving = backend()
            with self.assertRaisesRegex(CleanupError, "directory remains"):
                execute_inventory(inventory, surviving)
            self.assertEqual(receipt_stages[-1], "worktree_native_ack")
            delete_count = len(root_deletes)
            with self.assertRaisesRegex(CleanupError, "root still survives"):
                execute_inventory(inventory, surviving)
            self.assertEqual(len(root_deletes), delete_count)

    def test_receipt_provenance_conflicts_duplicates_and_complete_pagination(self):
        inventory = self.receipt_inventory()
        identity = {"task_id": TASK, "project_id": PROJECT, "node_id": ENTRY_NODE}
        with mock.patch.object(self.backend, "_script_identity", return_value=identity):
            self.backend.begin_inventory(inventory)
            entry = {
                "kind": "session", "index": 0, "subject_sha256": hashlib.sha256(
                    canonical_bytes(inventory.value["sessions"][0]),
                ).hexdigest(), "disposition": "removed",
                "proof": {"session_id": SESSION, "native_status": "ok"},
            }
            receipt = {
                "schema": "merged-cleanup-receipt-v1", "task_id": TASK,
                "inventory_sha256": inventory.sha256,
                "producer": self.backend._receipt_producer,
                "stage": "session_native_settled", "entries": [entry],
            }

            def insert(body, index=1, actor=None, author="agent", updated=None):
                self.writer.execute(
                    "INSERT INTO task_comments VALUES (?,?,?,?,?,?,?)",
                    (f"comment-{index:08x}-1111-4111-8111-111111111111",
                     TASK, body if isinstance(body, str) else json.dumps(body),
                     author, actor or self.backend._receipt_author_id, index,
                     index if updated is None else updated),
                )
                self.writer.commit()

            for invalid, options in (
                ({**receipt, "task_id": OTHER_TASK}, {}),
                ({**receipt, "producer": {**receipt["producer"], "node_id": SOURCE_NODE}}, {}),
                ({**receipt, "entries": [{**entry, "subject_sha256": "f" * 64}]}, {}),
                ({**receipt, "schema": "merged-cleanup-receipt-v3"}, {}),
                (json.dumps(receipt)[:-2], {}),
                (receipt, {"author": "user"}),
                (receipt, {"actor": "another producer"}),
                (receipt, {"updated": 2}),
            ):
                self.writer.execute("DELETE FROM task_comments")
                self.writer.commit()
                insert(invalid, **options)
                with self.subTest(invalid=invalid, options=options), self.assertRaises(CleanupError):
                    self.backend.begin_inventory(inventory)
            self.writer.execute("DELETE FROM task_comments")
            self.writer.commit()
            for index in range(1, 130):
                insert("ordinary historical commentary", index)
            insert(receipt, 130)
            insert({**receipt, "schema": "merged-cleanup-receipt-v2"}, 131)
            insert(receipt, 132)  # mixed-version identical observations are harmless
            self.backend.begin_inventory(inventory)
            self.assertEqual(self.backend._session_settlements, {SESSION: "removed"})
            with mock.patch("workflowkit.cleanup_native._MAX_RECEIPT_COMMENTS", 128):
                with self.assertRaisesRegex(CleanupError, "exceeds"):
                    self.backend.begin_inventory(inventory)
            physical = {
                **receipt, "stage": "container_settled",
                "entries": [{**entry, "proof": {"path_absent": True}}],
            }
            insert(physical, 133)
            insert({
                **physical, "entries": [{**physical["entries"][0], "disposition": "absent"}],
            }, 134)
            with self.assertRaisesRegex(CleanupError, "conflicting"):
                self.backend.begin_inventory(inventory)

    def test_missing_ack_or_unknown_receipt_append_never_authorizes_residual_removal(self):
        inventory = self.receipt_inventory()
        identity = {"task_id": TASK, "project_id": PROJECT, "node_id": ENTRY_NODE}
        with mock.patch.object(self.backend, "_script_identity", return_value=identity):
            self.backend.begin_inventory(inventory)
            self.backend._native_ack = {
                SESSION: {"session_id": SESSION, "native_status": "ok"},
            }
            with mock.patch.object(
                self.backend, "runner", return_value=(1, b"", b"unknown append"),
            ):
                with self.assertRaisesRegex(CleanupError, "write remains unknown"):
                    self.backend.record_settlement("session_native_settled", "session", 0, "removed")
            with self.assertRaisesRegex(CleanupError, "lacks settled lifecycle"):
                self.backend.remove_session_container(inventory.value["sessions"][0])
            self.writer.execute("DELETE FROM sessions WHERE id=?", (SESSION,))
            self.writer.commit()
            self.backend._snapshots = {}
            self.backend.begin_inventory(inventory)
            response = {
                "status": "error", "error": {
                    "code": "session_not_found", "session_id": SESSION, "message": "not found",
                },
            }
            with mock.patch.object(
                self.backend, "runner", return_value=(1, json.dumps(response).encode(), b""),
            ):
                with self.assertRaisesRegex(CleanupError, "absence does not prove"):
                    self.backend.delete_session(inventory.value["sessions"][0])

    def test_history_receipt_uses_genuine_standalone_operator_not_script_identity(self):
        inventory = self.receipt_inventory()
        operator = "66666666-6666-4666-8666-666666666666"
        self.insert_session(operator, task_id=None)
        self.writer.commit()
        inventory.value["history_operation"] = {
            "operator_session_id": operator, "operator_task_id": None,
            "operator_project_id": PROJECT, "project_id": PROJECT,
            "selected_task_ids": [TASK],
        }
        observed = []

        def runner(args, *, cwd, **kwargs):
            if args == ["session-id"]:
                return 0, f"Short ID: 2\nUUID: {operator}\nLog path: /tmp/operator\n".encode(), b""
            self.assertEqual(args[:4], ["task", "comment", "add", TASK])
            self.assertEqual(kwargs["environment"]["KENT_SESSION_ID"], operator)
            body = args[args.index("--body") + 1]
            observed.append(json.loads(body)["producer"])
            cid = "comment-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            self.writer.execute(
                "INSERT INTO task_comments VALUES (?,?,?,?,?,?,?)",
                (cid, TASK, body, "agent", args[args.index("--author-id") + 1], 1, 1),
            )
            self.writer.commit()
            return 0, f"comment_id\t{cid}\ntask_id\t{TASK}\n".encode(), b""

        with (
            mock.patch.dict(os.environ, {"KENT_SESSION_ID": operator}, clear=True),
            mock.patch.object(self.backend, "runner", side_effect=runner),
            mock.patch.object(self.backend, "_script_identity", side_effect=AssertionError("fake Script")),
        ):
            self.backend.begin_inventory(inventory)
            self.backend._native_ack = {SESSION: {"session_id": SESSION, "native_status": "ok"}}
            self.backend.record_settlement("session_native_settled", "session", 0, "removed")
        self.assertEqual(observed, [{"kind": "history", "session_id": operator, "project_id": PROJECT}])

    def test_matching_applied_terminal_result_skips_all_effects_and_receipt_writes(self):
        inventory = self.receipt_inventory()
        result = {
            "schema": "merged-cleanup-result-v1", "phase": "completed",
            "task_id": TASK, "project_id": PROJECT, "workflow_id": WORKFLOW,
            "inventory": {"namespace": "attachments", "sha256": inventory.sha256, "byte_size": 100},
            "endpoint": {"repository": "org/repo", "number": 4, "base_ref": "main", "head_oid": "a" * 40},
            "dispositions": {"sessions": "r", "containers": "", "worktrees": ""},
            "postconditions": {
                "sessions_absent": 1, "session_containers_absent": 1, "owned_containers_absent": 0,
                "worktrees_absent": 0, "branches_retained": 0,
            },
        }
        self.writer.execute(
            "UPDATE task_current_nodes SET node_id=?,session_id=NULL,current_input_values_json=? WHERE task_id=?",
            (DONE_NODE, json.dumps({"cleanup_result": json.dumps(result)}), TASK),
        )
        self.writer.commit()
        with (
            mock.patch.object(self.backend, "_native_task", return_value={"status": {"native_state": "terminal"}}),
            mock.patch.object(self.backend, "_qualify_receipt_producer", side_effect=AssertionError("repeated effects")),
        ):
            self.assertEqual(self.backend.begin_inventory(inventory), result)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project_root = self.root / "project"
        self.project_root.mkdir()
        self.persistence = self.root / ".kent"
        (self.persistence / "db").mkdir(parents=True)
        self.database = self.persistence / "db/main.sqlite3"
        self.writer = sqlite3.connect(self.database)
        self.addCleanup(self.writer.close)
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.executescript("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, workspace_id TEXT,
                worktree_id TEXT, artifact_relpath TEXT NOT NULL,
                continuation_json TEXT NOT NULL, locked_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL, previous_session_id TEXT,
                parent_agent_session_id TEXT, task_id TEXT,
                completed_compaction_count INTEGER, last_sequence INTEGER NOT NULL
            );
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, workflow_id BLOB NOT NULL,
                source_workspace_id TEXT, managed_worktree_id TEXT
            );
            CREATE VIEW task_records AS
                SELECT id, project_id, workflow_id, source_workspace_id, managed_worktree_id
                FROM tasks;
            CREATE TABLE workspaces (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                canonical_root_path TEXT NOT NULL
            );
            CREATE TABLE worktrees (
                id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
                canonical_root_path TEXT NOT NULL
            );
            CREATE TABLE task_comments (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, body TEXT NOT NULL,
                author_kind TEXT NOT NULL, author_id TEXT NOT NULL,
                created_at_unix_ms INTEGER NOT NULL, updated_at_unix_ms INTEGER NOT NULL
            );
            CREATE TABLE task_current_nodes (
                task_id TEXT NOT NULL, node_id TEXT NOT NULL,
                current_input_values_json TEXT NOT NULL,
                prior_node_values_json TEXT NOT NULL,
                transition_branch_key TEXT, session_id TEXT,
                scheduling_state TEXT, entered_by_edge_id TEXT
            );
        """)
        self.writer.execute(
            "INSERT INTO workspaces VALUES (?,?,?)",
            ("workspace-1", PROJECT, str(self.project_root)),
        )
        self.writer.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,NULL)",
            (TASK, PROJECT, bytes.fromhex(WORKFLOW.replace("-", "")), "workspace-1"),
        )
        self.insert_session()
        self.writer.execute(
            "INSERT INTO task_current_nodes "
            "(task_id,node_id,current_input_values_json,prior_node_values_json,"
            "transition_branch_key,session_id,scheduling_state,entered_by_edge_id) "
            "VALUES (?,?,?, ?,NULL,?,NULL,NULL)",
            (TASK, SOURCE_NODE, '{"pr_url":"https://github.com/org/repo/pull/4"}',
             '{"transition_parameters":{}}', SESSION),
        )
        self.writer.commit()
        self.context = {
            "persistence_root": str(self.persistence),
            "environment": {
                "KENT_PERSISTENCE_ROOT": str(self.persistence),
                "KENT_SERVER_HOST": "127.0.0.1",
                "KENT_SERVER_PORT": "53082",
            },
        }
        self.validation = mock.patch.object(
            operations, "validate_native_local_context", side_effect=lambda value: value,
        )
        self.validation.start()
        self.addCleanup(self.validation.stop)
        self.backend = NativeCleanupBackend(
            workspace=self.project_root, kent=sys.executable, context=self.context,
            runner=self.runner,
        )

    def insert_session(
        self,
        session_id=SESSION,
        *,
        task_id=TASK,
        parent=None,
        previous=None,
        artifact=None,
    ):
        artifact = artifact or f"projects/{PROJECT}/sessions/{session_id}"
        self.writer.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, PROJECT, "workspace-1", None, artifact,
             "{}", "{}", "{}", previous, parent, task_id, 0, 1),
        )

    def runner(self, args, *, cwd, timeout=20.0, output_limit=1024 * 1024, **_kwargs):
        self.assertEqual(cwd, self.project_root)
        self.assertGreater(timeout, 0)
        self.assertGreater(output_limit, 0)
        return 0, b"", b""

    def test_snapshot_reads_wal_and_parses_typed_task_values(self):
        self.assertGreater(Path(str(self.database) + "-wal").stat().st_size, 0)
        self.insert_session(
            "66666666-6666-4666-8666-666666666666",
            parent=SESSION,
        )
        result = self.backend._session_snapshot((TASK,))
        self.assertEqual(len(result["sessions"]), 1)
        self.assertEqual(result["sessions"][0]["session_id"], SESSION)
        self.assertEqual(result["current_nodes"][0]["current_input_values"], {})
        self.assertEqual(result["current_nodes"][0]["node_id"], SOURCE_NODE)
        self.writer.rollback()
        self.assertEqual(len(self.backend._session_snapshot((TASK,))["sessions"]), 1)

    def test_foreign_native_paths_remain_protected_after_project_reassignment(self):
        path = self.persistence / f"projects/{PROJECT}/sessions/{SESSION}"
        path.mkdir(parents=True)
        own = {
            "session_id": SESSION, "project_id": PROJECT,
            "artifact_relpath": f"projects/{PROJECT}/sessions/{SESSION}",
            "disposition": "remove",
        }
        inventory = {"sessions": [own], "worktrees": [], "owned_containers": []}
        foreign = {
            "session_id": "66666666-6666-4666-8666-666666666666",
            "project_id": "project-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "artifact_relpath": own["artifact_relpath"],
        }
        with self.assertRaisesRegex(CleanupError, "foreign native Session"):
            self.backend._check_foreign_session_collisions(
                inventory, {"sessions": [own, foreign]},
            )
        foreign_path = path.parent / foreign["session_id"]
        foreign["artifact_relpath"] = f"projects/{PROJECT}/sessions/{foreign['session_id']}"
        # A reassigned row remains a protective exact native path.
        self.backend._check_foreign_session_collisions(
            inventory, {"sessions": [own, foreign]},
        )
        foreign_path.symlink_to(path, target_is_directory=True)
        with self.assertRaisesRegex(CleanupError, "foreign native Session"):
            self.backend._check_foreign_session_collisions(
                inventory, {"sessions": [own, foreign]},
            )

    def test_snapshot_scans_more_than_the_cleanup_closure_limit(self):
        rows = []
        for index in range(4100, 8200):
            session_id = f"{index:08x}-1111-4111-8111-{index:012x}"
            rows.append((
                session_id, PROJECT, None, None,
                f"projects/{PROJECT}/sessions/{session_id}",
                "{}", "{}", "{}", None, None, None, 0, 1,
            ))
        self.writer.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.writer.commit()
        snapshot = self.backend._session_snapshot()
        self.assertEqual(len(snapshot["sessions"]), 4101)

    def test_current_session_identity_uses_kent_uuid_line(self):
        with mock.patch.dict(os.environ, {"KENT_SESSION_ID": SESSION}, clear=True):
            self.backend.runner = lambda *_args, **_kwargs: (
                0, f"Short ID: 1\nUUID: {SESSION}\nLog path: /tmp/x.log\n".encode(), b"",
            )
            observed_id, observed = self.backend._current_agent_session()
        self.assertEqual(observed_id, SESSION)
        self.assertEqual(observed["session_id"], SESSION)

    def test_fresh_script_identity_change_blocks_revalidation(self):
        context = {"task_id": TASK, "node_id": ENTRY_NODE}
        original = {"task_id": TASK, "project_id": PROJECT, "node_id": ENTRY_NODE}
        self.backend._script_native_context = dict(context)
        self.backend._frozen_script_identity = dict(original)
        changed = {**original, "node_id": DONE_NODE}
        with mock.patch.object(
            delivery, "observe_script_identity",
            return_value=(changed, {}, {}),
        ):
            with self.assertRaisesRegex(CleanupError, "Script identity differs"):
                self.backend._script_identity(context)
        with self.assertRaisesRegex(CleanupError, "context changed"):
            self.backend._script_identity({**context, "node_id": DONE_NODE})

    def test_terminal_routes_classify_only_exact_native_edge_group(self):
        edge_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        transition_id = "not_delivered"
        graph = {
            "edges": [{
                "id": edge_id,
                "target_node_id": DONE_NODE, "transition_group_id": DELIVERED_GROUP,
            }],
            "transition_groups": [{
                "id": DELIVERED_GROUP,
                "source_node_id": SOURCE_NODE,
                "transition_id": transition_id,
            }],
        }
        self.backend._graph = {
            "workflow_id": WORKFLOW, "graph": graph,
        }
        self.backend._snapshot_once = lambda *_args: {
            "current_nodes": [{
                "task_id": OTHER_TASK, "node_id": DONE_NODE,
                "entered_by_edge_id": edge_id, "current_input_values": None,
            }],
        }
        detail = {"summary": {"workflow_id": WORKFLOW}}
        self.assertEqual(
            self.backend._terminal_transition(OTHER_TASK, detail),
            "not_delivered",
        )
        # A completed first Task releases shared supervision for the second.
        for transition in ("disposal_completed", "ordinary_delivered"):
            graph["transition_groups"][0]["transition_id"] = transition
            self.assertEqual(
                self.backend._terminal_transition(OTHER_TASK, detail), transition)
        for key, value in (
            ("target_node_id", SOURCE_NODE),
            ("transition_group_id", "missing-group"),
        ):
            with self.subTest(edge_binding=key):
                with mock.patch.dict(graph["edges"][0], {key: value}):
                    self.assertIsNone(self.backend._terminal_transition(OTHER_TASK, detail))
        wrong_workflow = "12345678-1234-4234-8234-123456789abc"
        with mock.patch.object(
            self.backend, "_read",
            return_value=json.dumps({"workflow_id": wrong_workflow, "graph": graph}).encode(),
        ):
            self.backend._graph = {"workflow_id": wrong_workflow, "graph": graph}
            self.assertIsNone(self.backend._terminal_transition(OTHER_TASK, detail))
        self.backend._graph = {"workflow_id": WORKFLOW, "graph": graph}
        self.backend._snapshot_once = lambda *_args: {
            "current_nodes": [{
                "task_id": OTHER_TASK, "node_id": DONE_NODE,
                "entered_by_edge_id": "ffffffff-ffff-4fff-8fff-ffffffffffff",
                "current_input_values": None,
            }],
        }
        self.assertIsNone(self.backend._terminal_transition(OTHER_TASK, detail))

    def test_ordinary_delivered_is_the_native_no_pr_route(self):
        self.backend._terminal_endpoint = lambda *_args: None
        self.backend._terminal_transition = lambda *_args: "ordinary_delivered"
        outcome = self.backend._task_outcome(
            OTHER_TASK, PROJECT, current_task_id=TASK, current_pr={},
            known_detail={"status": {"native_state": "terminal"}},
        )
        self.assertEqual(outcome["outcome"], "no-pr")

    def test_script_identity_keeps_native_worktree_root_after_chdir(self):
        original = self.backend.workspace
        primary = self.root / "primary"
        primary.mkdir()
        context = {"task_id": TASK, "node_id": ENTRY_NODE}
        detail = {
            "summary": {"id": TASK, "project_id": PROJECT, "short_id": "KIT-1",
                        "workflow_id": WORKFLOW},
            "source_workspace": {"is_primary": True, "workspace_id": "workspace-1",
                                 "root_path": str(primary)},
            "worktree_path": str(original),
            "current_scripts": [{
                "current_node": {"node_id": ENTRY_NODE, "session_id": None},
                "path": "cleanup",
            }],
        }
        graph = {"workflow_id": WORKFLOW, "graph": {"nodes": [{
            "id": ENTRY_NODE, "kind": "script", "script_path": "cleanup",
        }]}}
        observations = []

        def runner(args, *, cwd, **_kwargs):
            observations.append(cwd)
            if args[0] == "project":
                return 0, PROJECT.encode(), b""
            if args[:2] == ["task", "show"]:
                return 0, json.dumps(detail).encode(), b""
            if args[:3] == ["workflow", "graph", "inspect"]:
                return 0, json.dumps(graph).encode(), b""
            self.fail(args)

        self.backend.runner = runner
        identity = self.backend._script_identity(context)
        self.backend.workspace = primary
        self.assertEqual(self.backend._script_identity(context), identity)
        self.assertEqual(identity["workspace_path"], str(original))
        self.assertEqual(observations[:4], [original] * 4)
        self.assertEqual(observations[4:], [primary] * 4)
        # Re-read actual native assignment, not the cached observation.
        detail["worktree_path"] = str(primary)
        with self.assertRaisesRegex(ValueError, "workspace/source identity mismatch"):
            self.backend._script_identity(context)

    def test_typed_legacy_terminal_endpoint_without_entered_edge(self):
        variants = [
            {"repository": "org/repo", "pr": 1664, "base": "main",
             "head": "a" * 40, "state": "MERGED", "merge_commit": "c" * 40,
             "merged_at": "2026-11-16T03:32:34Z",
             "observation": "Historical provider proof; not current authority",
             "original_delivery_proof": "d" * 64},
            {"repo": "org/repo", "pr": 1664, "base": "main",
             "published_head": "b" * 40, "later_authorized_successor": "a" * 40,
             "ci_run": 43557261600, "attempt": 2, "conclusion": "success",
             "acceptance_ref": "d" * 64, "merge_release": False},
            {"repo": "org/repo", "pr": 1664, "base_ref": "main",
             "head": "a" * 40, "head_ref": "feature/MBL-784", "state": "OPEN",
             "ci": "success; six required checks SUCCESS", "ci_run": 43605215364,
             "download_consumed": 1, "download_remaining": 0,
             "supervisor_personal_actual_new_package": "Historical acceptance locator"},
        ]
        detail = {
            "summary": {"id": OTHER_TASK, "project_id": PROJECT, "workflow_id": WORKFLOW},
            "status": {"native_state": "terminal"},
        }
        self.backend._terminal_transition = lambda *_args: None
        provider = {
            "repository": "org/repo", "number": 1664, "base_ref": "main",
            "url": "https://github.com/org/repo/pull/1664",
            "head_oid": "a" * 40, "merged_at": "2026-11-17T10:00:00Z",
            "merge_commit": "c" * 40,
        }
        self.backend._git_ancestor = lambda old, new: old == "b" * 40 and new == "a" * 40
        for endpoint in variants:
            with self.subTest(endpoint=endpoint):
                self.backend._snapshot_once = lambda *_args: {
                    "current_nodes": [{
                        "task_id": OTHER_TASK, "node_id": DONE_NODE,
                        "entered_by_edge_id": None,
                        "current_input_values": {"cleanup_result": json.dumps({
                            "schema": "appsome-native-cleanup-result-v1",
                            "task_id": OTHER_TASK,
                            "endpoint": endpoint,
                        })},
                    }],
                }
                with mock.patch.object(self.backend, "_provider_pr", return_value=provider) as read:
                    result = self.backend._task_outcome(
                        OTHER_TASK, PROJECT, current_task_id=TASK,
                        current_pr={}, known_detail=detail,
                    )
                self.assertEqual(result["outcome"], "merged")
                read.assert_called_once_with(provider["url"])
                with mock.patch.object(
                    self.backend, "_provider_pr", side_effect=CleanupError("not merged"),
                ):
                    with self.assertRaisesRegex(CleanupError, "not merged"):
                        self.backend._task_outcome(
                            OTHER_TASK, PROJECT, current_task_id=TASK,
                            current_pr={}, known_detail=detail,
                        )
                with (
                    mock.patch.object(
                        self.backend, "_provider_pr",
                        return_value={**provider, "base_ref": "wrong-base"},
                    ),
                    self.assertRaisesRegex(CleanupError, "contradicts"),
                ):
                    self.backend._task_outcome(
                        OTHER_TASK, PROJECT, current_task_id=TASK,
                        current_pr={}, known_detail=detail,
                    )
                with (
                    mock.patch.object(
                        self.backend, "_provider_pr",
                        return_value={**provider, "head_oid": "f" * 40},
                    ),
                    self.assertRaisesRegex(CleanupError, "head differs"),
                ):
                    self.backend._task_outcome(
                        OTHER_TASK, PROJECT, current_task_id=TASK,
                        current_pr={}, known_detail=detail,
                    )

    def test_legacy_endpoint_accepts_only_declared_typed_variants(self):
        endpoint = {
            "repository": "org/repo", "pr": 4, "base": "main",
            "head": "a" * 40, "state": "MERGED",
        }
        self.assertIsNone(self.backend._legacy_terminal_endpoint("PR4 merged"))
        self.assertIsNone(self.backend._legacy_terminal_endpoint({
            **endpoint, "arbitrary_new_authority": True,
        }))
        for invalid in (
            {**endpoint, "pr": True},
            {**endpoint, "head": "merged in prose"},
            {**endpoint, "repository": "https://github.com/org/repo"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(CleanupError):
                self.backend._legacy_terminal_endpoint(invalid)

    def test_terminal_non_delivery_requires_native_typed_route(self):
        self.backend._terminal_endpoint = lambda *_args: None
        self.backend._terminal_transition = lambda *_args: "not_delivered"
        outcome = self.backend._task_outcome(
            OTHER_TASK, PROJECT, current_task_id=TASK,
            current_pr={}, known_detail={"status": {"native_state": "terminal"}},
        )
        self.assertEqual(outcome["native_state"], "terminal")
        self.assertEqual(outcome["outcome"], "non-delivery")

    def test_memory_adapter_real_subprocess_accepts_large_attachment_read(self):
        payload_size = 5 * 1024 * 1024
        adapter = self.root / "memory-adapter"
        adapter.write_text(
            "#!/usr/bin/env python3\n"
            "import hashlib,json,sys\n"
            f"content=b'x'*{payload_size}\n"
            "request=json.loads(sys.stdin.buffer.readline())\n"
            "header={'schema':'r2-artifact-response-v1','operation':'artifact-read',"
            "'object_sha256':hashlib.sha256(content).hexdigest(),"
            "'byte_size':len(content)}\n"
            "sys.stdout.buffer.write(json.dumps(header,separators=(',',':')).encode()+b'\\n'+content)\n",
            encoding="utf-8",
        )
        adapter.chmod(0o755)
        self.backend.memory_adapter = adapter
        expected = hashlib.sha256(b"x" * payload_size).hexdigest()
        response, content = self.backend._memory_call("artifact-read", digest=expected)
        self.assertEqual(response["byte_size"], payload_size)
        self.assertEqual(len(content), payload_size)

    def test_snapshot_does_not_parse_unselected_task_payloads(self):
        self.writer.execute(
            "INSERT INTO task_current_nodes "
            "(task_id,node_id,current_input_values_json,prior_node_values_json,"
            "transition_branch_key,session_id,scheduling_state,entered_by_edge_id) "
            "VALUES (?,?,?, ?,NULL,NULL,NULL,NULL)",
            (OTHER_TASK, "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", "not-json", "{}"),
        )
        self.writer.commit()
        result = self.backend._session_snapshot((TASK,))
        other = next(row for row in result["current_nodes"] if row["task_id"] == OTHER_TASK)
        self.assertIsNone(other["current_input_values"])

    def test_snapshot_does_not_include_previous_session_as_parent(self):
        previous = "66666666-6666-4666-8666-666666666666"
        self.insert_session(previous, task_id=None)
        self.writer.execute(
            "UPDATE sessions SET previous_session_id=? WHERE id=?",
            (previous, SESSION),
        )
        self.writer.commit()
        result = self.backend._session_snapshot((TASK,))
        self.assertEqual(
            result["sessions"][0]["previous_session_id"],
            previous,
        )
        closure_rows = [
            {
                "session_id": row["session_id"], "project_id": row["project_id"],
                "task_id": row["task_id"], "parent_agent_session_id": row["parent_agent_session_id"],
                "previous_session_id": row["previous_session_id"],
                "workspace_id": row["workspace_id"], "worktree_id": row["worktree_id"],
                "artifact_relpath": row["artifact_relpath"], "device": None, "inode": None,
                "supervisor_root_ids": [], "disposition": "remove",
            }
            for row in result["sessions"]
        ]
        from workflowkit.cleanup import build_session_closure
        closure = build_session_closure(closure_rows, task_id=TASK, project_id=PROJECT)
        self.assertEqual([row["session_id"] for row in closure], [SESSION])

    def test_native_worktree_snapshot_revalidates_database_binding(self):
        self.writer.execute(
            "INSERT INTO worktrees VALUES (?,?,?)",
            ("worktree-1", "workspace-1", str(self.root / "task-worktree")),
        )
        self.writer.commit()
        with mock.patch.object(
            operations, "validate_native_local_context", side_effect=lambda value: value,
        ) as validate_context:
            worktrees, workspaces = self.backend._native_paths(
                ["worktree-1"], project_id=PROJECT,
            )
        self.assertEqual(validate_context.call_count, 2)
        self.assertEqual(worktrees, [{
            "worktree_id": "worktree-1", "workspace_id": "workspace-1",
            "path": str(self.root / "task-worktree"),
        }])
        self.assertEqual(workspaces, [{
            "workspace_id": "workspace-1", "project_id": PROJECT,
            "path": str(self.project_root),
        }])

    def test_native_registered_worktree_under_disposable_subtree_is_exactly_admitted(self):
        native_worktree = self.persistence / "worktrees/appsome/task-1"
        self.writer.execute(
            "INSERT INTO worktrees VALUES (?,?,?)",
            ("worktree-1", "workspace-1", str(native_worktree)),
        )
        self.writer.commit()
        worktrees, _workspaces = self.backend._native_paths(
            ["worktree-1"], project_id=PROJECT,
        )
        self.assertEqual(worktrees, [{
            "worktree_id": "worktree-1", "workspace_id": "workspace-1",
            "path": str(native_worktree),
        }])

    def test_terminal_native_endpoint_requires_fresh_merged_provider_facts(self):
        endpoint = {
            "repository": "org/repo", "number": 4, "base_ref": "main",
            "head_oid": "a" * 40,
        }
        result = {
            "schema": "merged-cleanup-result-v1", "phase": "completed",
            "project_id": PROJECT, "task_id": OTHER_TASK, "workflow_id": WORKFLOW,
            "inventory": {"namespace": "attachments", "sha256": "d" * 64, "byte_size": 100},
            "endpoint": endpoint,
            "dispositions": {"sessions": "r", "containers": "", "worktrees": "r"},
            "postconditions": {
                "sessions_absent": 1, "session_containers_absent": 1,
                "owned_containers_absent": 0, "worktrees_absent": 1,
                "branches_retained": 1,
            },
        }
        self.writer.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,NULL)",
            (OTHER_TASK, PROJECT, bytes.fromhex(WORKFLOW.replace("-", "")), "workspace-1"),
        )
        self.writer.execute(
            "INSERT INTO task_current_nodes(task_id,node_id,current_input_values_json,"
            "prior_node_values_json,session_id,entered_by_edge_id) VALUES (?,?,?,'{}',NULL,NULL)",
            (OTHER_TASK, DONE_NODE, json.dumps({"cleanup_result": json.dumps(result)})),
        )
        self.writer.commit()
        self.backend._graph = {"workflow_id": WORKFLOW, "graph": self._policy_graph()}
        self.assertIsNone(self.backend._terminal_transition(
            OTHER_TASK, {"summary": {"workflow_id": WORKFLOW}},
        ))
        provider_facts = {
            "repository": "org/repo", "number": 4,
            "url": "https://github.com/org/repo/pull/4",
            "base_ref": "main", "base_oid": "f" * 40,
            "head_ref": "task/app-158", "head_oid": "a" * 40,
            "state": "MERGED", "merged_at": "2026-11-17T10:00:00Z",
            "merge_commit": "c" * 40,
        }
        self.backend._provider_pr = lambda _url: dict(provider_facts)
        outcome = self.backend._task_outcome(
            OTHER_TASK, PROJECT, current_task_id=TASK,
            current_pr={"url": "https://github.com/org/repo/pull/5"},
            known_detail={
                "summary": {
                    "id": OTHER_TASK, "project_id": PROJECT, "workflow_id": WORKFLOW,
                },
                "status": {"native_state": "terminal"},
            },
        )
        self.assertEqual(outcome["outcome"], "merged")
        self.assertEqual(outcome["head_oid"], "a" * 40)
        # A new base-branch commit alone is an observation drift, not a new PR.
        self.backend._provider_pr = lambda _url: {
            **provider_facts, "head_oid": "b" * 40,
        }
        with self.assertRaisesRegex(CleanupError, "delivered head differs"):
            self.backend._task_outcome(
                OTHER_TASK, PROJECT, current_task_id=TASK,
                current_pr={"url": "https://github.com/org/repo/pull/5"},
                known_detail={
                    "summary": {
                        "id": OTHER_TASK, "project_id": PROJECT, "workflow_id": WORKFLOW,
                    },
                    "status": {"native_state": "terminal"},
                },
            )
        self.backend._provider_pr = lambda _url: dict(provider_facts)

        def outcome(state="terminal"):
            return self.backend._task_outcome(
                OTHER_TASK, PROJECT, current_task_id=TASK,
                current_pr={},
                known_detail={
                    "summary": {
                        "id": OTHER_TASK, "project_id": PROJECT, "workflow_id": WORKFLOW,
                    },
                    "status": {"native_state": state},
                },
            )

        def store(payload, *, node=DONE_NODE, owner=None):
            self.writer.execute(
                "UPDATE task_current_nodes SET current_input_values_json=?,node_id=?,session_id=? WHERE task_id=?",
                (json.dumps(payload), node, owner, OTHER_TASK),
            )
            self.writer.commit()
            self.backend._snapshots = {}

        for key, foreign in (("task_id", TASK), ("project_id", PROJECT.replace("1111", "7777")),
                             ("workflow_id", SOURCE_NODE)):
            store({"cleanup_result": json.dumps({**result, key: foreign})})
            with self.subTest(foreign=key), self.assertRaisesRegex(CleanupError, "another native Task"):
                outcome()
        store({"cleanup_result": json.dumps({**result, "phase": "prepared"})})
        with self.assertRaises(CleanupError):
            outcome()
        for payload in ({}, {"cleanup_result": json.dumps({"endpoint": endpoint})},
                        {"cleanup_result": json.dumps({**result, "schema": "unknown-result-v1"})}):
            store(payload)
            with mock.patch.object(self.backend, "_provider_pr", side_effect=AssertionError("untyped PR")):
                self.assertEqual(outcome()["outcome"], "unknown")
        store({"cleanup_result": json.dumps(result)})
        self.assertEqual(outcome("active")["outcome"], "unknown")
        for node, owner in ((SOURCE_NODE, None), (DONE_NODE, SESSION)):
            store({"cleanup_result": json.dumps(result)}, node=node, owner=owner)
            self.assertEqual(outcome()["outcome"], "unknown")
        store({"cleanup_result": json.dumps(result)})
        graph_nodes = self.backend._graph["graph"]["nodes"]
        terminal = next(row for row in graph_nodes if row["id"] == DONE_NODE)
        with mock.patch.dict(terminal, {"kind": "script"}):
            self.assertEqual(outcome()["outcome"], "unknown")
        with mock.patch.object(
            self.backend, "_provider_pr", return_value={**provider_facts, "base_ref": "wrong"},
        ), self.assertRaisesRegex(CleanupError, "contradicts"):
            outcome()
        with mock.patch.object(
            self.backend, "_provider_pr", side_effect=CleanupError("not merged"),
        ), self.assertRaisesRegex(CleanupError, "not merged"):
            outcome()
        self.assertEqual(outcome()["outcome"], "merged")

    def test_provider_base_oid_drift_does_not_change_frozen_pr_identity(self):
        frozen = {
            "repository": "org/repo", "number": 4, "url": "https://github.com/org/repo/pull/4",
            "base_ref": "main", "base_oid": "a" * 40, "head_ref": "task/x",
            "head_oid": "b" * 40, "state": "MERGED",
            "merged_at": "2026-11-17T10:00:00Z", "merge_commit": "c" * 40,
        }
        fresh = {**frozen, "base_oid": "d" * 40}
        self.assertTrue(self.backend._same_provider_pr_identity(frozen, fresh))
        self.assertFalse(self.backend._same_provider_pr_identity(
            frozen, {**fresh, "head_oid": "e" * 40},
        ))

    def test_fresh_closure_finds_descendant_of_already_removed_session(self):
        late_session = "66666666-6666-4666-8666-666666666666"
        inventory = {
            "task_id": TASK,
            "supervision": [],
            "sessions": [{"session_id": SESSION}],
        }
        snapshot = {
            "sessions": [{
                "session_id": late_session,
                "parent_agent_session_id": SESSION,
            }],
            "current_nodes": [],
        }
        selected = NativeCleanupBackend._selected_cleanup_session_ids(
            snapshot, inventory,
        )
        self.assertEqual(selected, {late_session})
        self.assertTrue(selected - {row["session_id"] for row in inventory["sessions"]})

    def test_native_delete_uses_confirm_and_requires_structured_success(self):
        session_dir = self.persistence / f"projects/{PROJECT}/sessions/{SESSION}"
        session_dir.mkdir(parents=True)
        info = session_dir.stat()
        row = {
            "session_id": SESSION, "project_id": PROJECT, "task_id": TASK,
            "parent_agent_session_id": None, "previous_session_id": None,
            "artifact_relpath": f"projects/{PROJECT}/sessions/{SESSION}",
            "device": info.st_dev, "inode": info.st_ino,
            "supervisor_root_ids": [], "disposition": "remove",
        }

        def delete(args, *, cwd, **kwargs):
            self.assertEqual(args, ["session", "delete", "--confirm", "--json", SESSION])
            self.writer.execute("DELETE FROM sessions WHERE id=?", (SESSION,))
            self.writer.commit()
            return 0, json.dumps({
                "status": "ok", "result": {"session_id": SESSION},
            }).encode(), b""

        self.backend.runner = delete
        self.assertEqual(self.backend.delete_session(row), "removed")
        self.assertEqual(self.backend.remove_session_container(row), "removed")
        self.assertFalse(session_dir.exists())
        self.assertTrue(session_dir.parent.exists())

    def test_live_session_refusal_and_unknown_response_do_not_remove_artifacts(self):
        session_dir = self.persistence / f"projects/{PROJECT}/sessions/{SESSION}"
        session_dir.mkdir(parents=True)
        info = session_dir.stat()
        row = {
            "session_id": SESSION, "project_id": PROJECT, "task_id": TASK,
            "parent_agent_session_id": None, "previous_session_id": None,
            "artifact_relpath": f"projects/{PROJECT}/sessions/{SESSION}",
            "device": info.st_dev, "inode": info.st_ino,
            "supervisor_root_ids": [], "disposition": "remove",
        }
        self.backend.runner = lambda *_args, **_kwargs: (
            1, json.dumps({
                "status": "error",
                "error": {"code": "session_in_use", "session_id": SESSION,
                          "message": "in use"},
            }).encode(), b"",
        )
        with self.assertRaisesRegex(CleanupError, "live Session"):
            self.backend.delete_session(row)
        self.assertTrue(session_dir.exists())

    def test_session_not_found_does_not_authorize_residual_removal(self):
        session_dir = self.persistence / f"projects/{PROJECT}/sessions/{SESSION}"
        session_dir.mkdir(parents=True)
        info = session_dir.stat()
        row = {
            "session_id": SESSION, "project_id": PROJECT, "task_id": TASK,
            "parent_agent_session_id": None, "previous_session_id": None,
            "artifact_relpath": f"projects/{PROJECT}/sessions/{SESSION}",
            "device": info.st_dev, "inode": info.st_ino,
            "supervisor_root_ids": [], "disposition": "remove",
        }
        self.backend.runner = lambda *_args, **_kwargs: (
            1, json.dumps({
                "status": "error",
                "error": {"code": "session_not_found", "session_id": SESSION},
            }).encode(), b"",
        )
        with self.assertRaisesRegex(CleanupError, "does not prove"):
            self.backend.delete_session(row)
        with self.assertRaisesRegex(CleanupError, "lacks settled"):
            self.backend.remove_session_container(row)
        self.assertTrue(session_dir.exists())

    def test_native_worktree_delete_accepts_native_cleanup_shape_and_retained_record(self):
        primary = self.root / "primary"
        primary.mkdir()
        row = {
            "project_id": PROJECT, "workspace_id": "workspace-1",
            "worktree_id": "worktree-1", "path": str(self.root / "task-worktree"),
            "branch": "task/cleanup", "branch_oid": "a" * 40,
            "head_oid": "a" * 40, "role": "task", "disposition": "remove",
        }
        self.backend._detail = {
            "source_workspace": {"root_path": str(primary)},
        }
        self.backend.runner = lambda *_args, **_kwargs: (
            0, b'{"status":"ok","result":{"cleanup":{"kind":"NOT_REQUESTED"}}}', b"",
        )
        self.backend._primary_path = lambda: primary
        self.backend._verify_git_worktree = lambda *_args, **_kwargs: None
        self.backend._git = lambda *_args, **_kwargs: "a" * 40
        self.backend._native_paths = lambda *_args, **_kwargs: ([{
            "worktree_id": "worktree-1", "workspace_id": "workspace-1",
            "path": row["path"],
        }], [])
        self.assertEqual(self.backend.delete_worktree(row), "removed")
        self.assertIn("worktree-1", self.backend._worktree_settlements)

    def test_owned_historical_file_and_directory_removal_use_frozen_parent_identity(self):
        resource_root = self.root / "resources"
        resource_root.mkdir()
        file_path = resource_root / "archive.json"
        file_path.write_text("archive", encoding="utf-8")
        directory_path = resource_root / "evidence"
        directory_path.mkdir()
        (directory_path / "report").write_text("evidence", encoding="utf-8")
        parent_info = resource_root.stat()
        rows = []
        for path, kind in ((file_path, "archive"), (directory_path, "evidence")):
            info = path.lstat()
            rows.append({
                "path": str(path), "kind": kind, "owner_task_id": TASK,
                "evidence_reference": "original ownership map locator",
                "device": info.st_dev, "inode": info.st_ino,
                "parent_device": parent_info.st_dev, "parent_inode": parent_info.st_ino,
                "disposition": "remove",
            })
        self.backend._protected_roots = [self.project_root, self.persistence]
        for row in rows:
            self.assertEqual(self.backend.remove_owned_container(row), "removed")
            self.assertFalse(Path(row["path"]).exists())

    def test_path_component_symlinks_are_rejected(self):
        target = self.root / "target"
        target.mkdir()
        link = self.root / "linked"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(CleanupError, "symlink"):
            _check_path_components(link / "child", allow_missing_leaf=True)

    def test_cleanup_route_graph_must_end_directly_at_done(self):
        entrypoint = Path(__file__).resolve().parents[1] / "scripts/cleanup-merged-task"
        graph = {
            "workflow_id": WORKFLOW,
            "nodes": [
                {
                    "id": "cleanup",
                    "kind": "script",
                    "script_path": str(entrypoint),
                },
                {"id": "done", "kind": "terminal", "key": "done"},
            ],
            "edges": [{
                "transition_group_id": "group-1",
                "key": "finish_disposal",
                "target_node_id": "done",
            }],
            "transition_groups": [{
                "id": "group-1",
                "source_node_id": "cleanup",
                "transition_id": "disposal_completed",
            }],
        }
        self.assertEqual(
            _validate_cleanup_route_graph(graph, WORKFLOW, entrypoint),
            entrypoint.resolve(),
        )
        with self.assertRaisesRegex(CleanupError, "graph is incomplete"):
            _validate_cleanup_route_graph(
                {**graph, "workflow_id": "12345678-1234-4234-8234-123456789abc"},
                WORKFLOW, entrypoint)
        with self.assertRaisesRegex(CleanupError, "successor edge"):
            _validate_cleanup_route_graph({
                **graph,
                "edges": [*graph["edges"], {
                    "transition_group_id": "group-1",
                    "key": "unexpected",
                    "target_node_id": "done",
                }],
            }, WORKFLOW, entrypoint)
        with self.assertRaisesRegex(CleanupError, "directly to Done"):
            _validate_cleanup_route_graph({
                **graph,
                "nodes": [
                    graph["nodes"][0],
                    {"id": "middle", "kind": "agent", "key": "unexpected"},
                ],
                "edges": [{
                    **graph["edges"][0],
                    "target_node_id": "middle",
                }],
            }, WORKFLOW, entrypoint)
        with self.assertRaisesRegex(CleanupError, "native node identity"):
            _validate_cleanup_route_graph({
                **graph,
                "nodes": [
                    {**graph["nodes"][0], "id": None},
                    graph["nodes"][1],
                ],
            }, WORKFLOW, entrypoint)

    def _policy_graph(self, description=None):
        entrypoint = Path(__file__).resolve().parents[1] / "scripts/cleanup-merged-task"
        compact = (
            '{"schema":"merged-cleanup-policy-v1","project_id":"' + PROJECT
            + '","workflow_id":"' + WORKFLOW + '","mode":"after_merge"}'
        )
        return {
            "workflow_id": WORKFLOW,
            "nodes": [
                {"id": SOURCE_NODE, "kind": "agent"},
                {"id": ENTRY_NODE, "kind": "script", "script_path": str(entrypoint)},
                {"id": DONE_NODE, "kind": "terminal", "key": "done"},
            ],
            "edges": [
                {
                    "transition_group_id": DELIVERED_GROUP,
                    "key": "finish_delivered", "target_node_id": ENTRY_NODE,
                },
                {
                    "transition_group_id": DISPOSAL_GROUP,
                    "key": "finish_disposal", "target_node_id": DONE_NODE,
                },
            ],
            "transition_groups": [
                {
                    "id": DELIVERED_GROUP,
                    "source_node_id": SOURCE_NODE, "transition_id": "delivered",
                    "description": compact if description is None else description,
                },
                {
                    "id": DISPOSAL_GROUP,
                    "source_node_id": ENTRY_NODE, "transition_id": "disposal_completed",
                },
            ],
        }

    def test_native_policy_uses_typed_values_not_description_formatting(self):
        spaced = (
            '{ "mode" : "after_merge", "workflow_id" : "' + WORKFLOW
            + '", "schema" : "merged-cleanup-policy-v1", "project_id" : "' + PROJECT + '" }'
        )
        policy, provenance = _validate_delivered_policy_graph(
            self._policy_graph(spaced), project_id=PROJECT,
            workflow_id=WORKFLOW,
            entrypoint=Path(__file__).resolve().parents[1] / "scripts/cleanup-merged-task",
        )
        self.assertEqual(policy["mode"], "after_merge")
        self.assertEqual(provenance["transition_group_id"], DELIVERED_GROUP)

    def test_native_policy_rejects_closed_schema_scope_and_size_errors(self):
        wrong_project = "project-88888888-8888-4888-8888-888888888888"
        invalid = [
            '{"schema":"merged-cleanup-policy-v1","project_id":"' + PROJECT
            + '","workflow_id":"' + WORKFLOW + '","mode":"after_merge","extra":true}',
            '{"schema":"merged-cleanup-policy-v1","project_id":"' + PROJECT
            + '","workflow_id":"' + WORKFLOW + '","mode":"not_after_merge"}',
            '{"schema":"merged-cleanup-policy-v1","project_id":"' + wrong_project
            + '","workflow_id":"' + WORKFLOW + '","mode":"after_merge"}',
            '{"schema":"merged-cleanup-policy-v1","project_id":"' + PROJECT
            + '","workflow_id":"12345678-1234-4234-8234-123456789abc","mode":"after_merge"}',
            "é" * 1001,
        ]
        entrypoint = Path(__file__).resolve().parents[1] / "scripts/cleanup-merged-task"
        for description in invalid:
            with self.subTest(description=description[:60]):
                with self.assertRaises(CleanupError):
                    _validate_delivered_policy_graph(
                        self._policy_graph(description), project_id=PROJECT,
                        workflow_id=WORKFLOW, entrypoint=entrypoint,
                    )

    def test_native_delivered_policy_route_must_enter_final_disposal_script(self):
        graph = self._policy_graph()
        graph["edges"][0]["target_node_id"] = DONE_NODE
        with self.assertRaisesRegex(CleanupError, "delivered policy group"):
            _validate_delivered_policy_graph(
                graph, project_id=PROJECT, workflow_id=WORKFLOW,
                entrypoint=Path(__file__).resolve().parents[1] / "scripts/cleanup-merged-task",
            )

    def test_history_operator_can_be_standalone_and_cross_project(self):
        operator_project = "project-88888888-8888-4888-8888-888888888888"
        operation = {
            "project_id": PROJECT,
            "operator_project_id": operator_project,
            "operator_task_id": None,
            "operator_session_id": SESSION,
        }
        self.backend._current_agent_session = lambda: (
            SESSION,
            {"session_id": SESSION, "task_id": None, "project_id": operator_project},
        )
        self.assertEqual(
            self.backend._verify_history_operator(operation),
            {
                "project_id": operator_project,
                "task_id": None,
                "session_id": SESSION,
            },
        )
        self.backend._current_agent_session = lambda: (
            SESSION,
            {"session_id": SESSION, "task_id": None, "project_id": PROJECT},
        )
        with self.assertRaisesRegex(CleanupError, "identity changed"):
            self.backend._verify_history_operator(operation)

    def test_cli_help_documents_closed_requests_and_digest_confirmation(self):
        entrypoint = Path(__file__).resolve().parents[1] / "scripts/cleanup-merged-task"
        cases = (
            (["--help"], "prepare and register-supervisor"),
            (["prepare", "--help"], "expected_base_ref"),
            (["register-supervisor", "--help"], "reused_root_session_ids"),
            (["history-inspect", "--help"], "permission_evidence"),
            (["history-apply", "--help"], "grants no authority"),
        )
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [sys.executable, str(entrypoint), *arguments],
                    capture_output=True, text=True, timeout=10, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)

    def test_bounded_command_has_no_shell_and_enforces_output_and_timeout(self):
        code, output, error = _run_bounded(
            [sys.executable, "-c", "import sys;sys.stderr.write('x'*100000);print('ACK')"],
            cwd=self.root, environment=dict(os.environ), output_limit=128,
        )
        self.assertEqual((code, output), (0, b"ACK\n"))
        self.assertIn(b"discarded", error)
        self.assertLessEqual(len(error), 65_536)
        code, out, err = _run_bounded(
            [sys.executable, "-c", "print('ready')"],
            cwd=self.project_root, environment={},
        )
        self.assertEqual((code, out, err), (0, b"ready\n", b""))
        with self.assertRaisesRegex(CleanupError, "byte limit"):
            _run_bounded(
                [sys.executable, "-c", "print('x' * 1000)"],
                cwd=self.project_root, environment={}, output_limit=32,
            )
        with self.assertRaisesRegex(CleanupError, "timed out"):
            _run_bounded(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                cwd=self.project_root, environment={}, timeout=0.1,
            )
        payload = b"x" * 1_000_000
        code, out, err = _run_bounded(
            [sys.executable, "-c",
             "import sys; data=sys.stdin.buffer.read(); print(len(data))"],
            cwd=self.project_root, environment={}, input_bytes=payload,
        )
        self.assertEqual((code, out, err), (0, b"1000000\n", b""))
        with self.assertRaisesRegex(CleanupError, "timed out"):
            _run_bounded(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                cwd=self.project_root, environment={}, timeout=0.1,
                input_bytes=payload,
            )

    def test_prepare_attachment_handoff_and_executor_use_native_cleanup_context(self):
        task_root = self.root / "task-worktree"
        task_root.mkdir()
        (self.persistence / f"projects/{PROJECT}/sessions").mkdir(parents=True)
        r2_root = self.root / "r2"
        r2_root.mkdir()
        adapter = self.project_root / ".kent/scripts/workflow-project-memory"
        adapter.parent.mkdir(parents=True)
        adapter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        adapter.chmod(0o755)
        profile = self.project_root / ".kent/workflow-profile.toml"
        profile.write_text(
            f'[project_memory]\nroot = "{r2_root}"\n'
            '[commands]\nproject_memory = ".kent/scripts/workflow-project-memory"\n',
            encoding="utf-8",
        )
        self.writer.execute(
            "INSERT INTO worktrees VALUES (?,?,?)",
            ("worktree-1", "workspace-1", str(task_root)),
        )
        self.writer.execute(
            "UPDATE tasks SET managed_worktree_id=? WHERE id=?",
            ("worktree-1", TASK),
        )
        self.writer.execute(
            "UPDATE sessions SET worktree_id=? WHERE id=?",
            ("worktree-1", SESSION),
        )
        self.writer.execute(
            "UPDATE task_current_nodes SET current_input_values_json=? WHERE task_id=?",
            (json.dumps({"cleanup_context": "opaque genuine native cleanup context"}), TASK),
        )
        for index in range(69):
            sid = f"{index + 100:08x}-1111-4111-8111-{index + 100:012x}"
            self.insert_session(
                sid, task_id=None,
                artifact=f"projects/project-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/sessions/{sid}",
            )
        self.writer.commit()

        entrypoint = Path(__file__).resolve().parents[1] / "scripts/cleanup-merged-task"
        graph_response = {"workflow_id": WORKFLOW, "graph": self._policy_graph()}
        task_detail = {
            "summary": {
                "id": TASK, "project_id": PROJECT, "workflow_id": WORKFLOW,
                "short_id": "KIT-1",
            },
            "status": {"native_state": "active"},
            "worktree_path": str(task_root),
            "source_workspace": {"root_path": str(self.project_root)},
        }
        head_oid, base_oid = "a" * 40, "b" * 40
        provider_pr = {
            "repository": "org/repo", "number": 4,
            "url": "https://github.com/org/repo/pull/4",
            "base_ref": "release/2026.11", "base_oid": base_oid,
            "head_ref": "task/merged", "head_oid": head_oid,
            "state": "MERGED", "merged_at": "2026-11-17T10:00:00Z",
            "merge_commit": "c" * 40,
        }
        backend = NativeCleanupBackend(
            workspace=task_root, kent=sys.executable, context=self.context,
            runner=self.runner,
        )

        def runner(args, *, cwd, **_kwargs):
            self.assertIn(cwd, {task_root, self.project_root})
            if args == ["session-id"]:
                return 0, (
                    f"Short ID: 1\nUUID: {SESSION}\nLog path: /tmp/session.log\n"
                ).encode(), b""
            self.fail(f"unexpected native command in fixture: {args}")

        def git(*args):
            if args[-3:] == ("symbolic-ref", "--short", "HEAD"):
                return "task/merged"
            if args[-3:] == ("worktree", "list", "--porcelain"):
                return f"worktree {task_root}\nHEAD {head_oid}\nbranch refs/heads/task/merged\n\n"
            if args[-3:] == ("rev-parse", "--verify", "HEAD"):
                return head_oid
            if args[-2:] == ("--verify", "refs/heads/task/merged"):
                return head_oid
            self.fail(f"unexpected Git observation in fixture: {args}")

        artifacts = {}

        def memory_call(operation, *, native_context=None, body=None, digest=None):
            expected_digest = hashlib.sha256(body).hexdigest() if body is not None else digest
            if operation == "artifact-write":
                artifacts[expected_digest] = body
                content = b""
            else:
                content = artifacts[expected_digest]
            return ({
                "schema": "r2-artifact-response-v1",
                "operation": operation,
                "object_sha256": expected_digest,
                "byte_size": len(body) if body is not None else len(content),
            }, content)

        identity = {"task_id": TASK, "project_id": PROJECT, "node_id": ENTRY_NODE}
        script_detail = {
            **task_detail,
            "source_workspace": {
                "is_primary": True, "workspace_id": "workspace-1",
                "root_path": str(self.project_root),
            },
        }
        native_context = {"task_id": TASK, "node_id": ENTRY_NODE}
        original_cwd = Path.cwd()
        self.addCleanup(os.chdir, original_cwd)
        request = {
            "schema": "merged-cleanup-prepare-request-v1",
            "pull_request_url": provider_pr["url"],
            "expected_base_ref": "release/2026.11",
            "release": {
                "prepared_by_session_id": SESSION,
                "owner_session_ids": [SESSION],
            },
        }

        class Executor:
            def __init__(inner_self):
                inner_self.calls = []

            def begin_inventory(inner_self, _inventory):
                return None

            def admit_effects(inner_self):
                pass

            def record_settlement(inner_self, _stage, _kind, _index, disposition):
                return disposition

            def require_physical_coverage(inner_self):
                pass

            def revalidate(inner_self, _inventory):
                inner_self.calls.append("revalidate")

            def delete_session(inner_self, row):
                inner_self.calls.append(("delete-session", row["session_id"]))
                return "removed"

            def remove_session_container(inner_self, row):
                inner_self.calls.append(("remove-session-container", row["session_id"]))
                return "removed"

            def remove_owned_container(inner_self, row):
                inner_self.calls.append(("remove-container", row["path"]))
                return "removed"

            def delete_worktree(inner_self, row):
                inner_self.calls.append(("delete-worktree", row["worktree_id"]))
                return "removed"

            def verify_postconditions(inner_self, inventory):
                inner_self.calls.append("postconditions")
                worktrees = inventory["worktrees"]
                return {
                    "sessions_absent": len(inventory["sessions"]),
                    "session_containers_absent": len(inventory["sessions"]),
                    "owned_containers_absent": 0,
                    "worktrees_absent": len(worktrees),
                    "branches_retained": len(worktrees),
                }

        with (
            mock.patch.dict(os.environ, {"KENT_SESSION_ID": SESSION}, clear=True),
            mock.patch.object(sys, "argv", [str(entrypoint)]),
            mock.patch.object(backend, "runner", side_effect=runner),
            mock.patch.object(backend, "_native_task", return_value=task_detail),
            mock.patch.object(backend, "_read", return_value=json.dumps(graph_response).encode()),
            mock.patch.object(backend, "_provider_pr", return_value=provider_pr),
            mock.patch.object(backend, "_github_repository", return_value="org/repo"),
            mock.patch.object(backend, "_git", side_effect=git),
            mock.patch.object(backend, "_memory_call", side_effect=memory_call),
        ):
            qualified_session_id, qualified_row = backend._current_agent_session()
            for invalid_task in (None, "task-99999999-9999-4999-8999-999999999999"):
                with self.subTest(native_task_binding=invalid_task):
                    with mock.patch.object(
                        backend, "_current_agent_session",
                        return_value=(qualified_session_id, {**qualified_row, "task_id": invalid_task}),
                    ):
                        with self.assertRaises(CleanupError):
                            backend.prepare(request)
            snapshot = backend._snapshot_once((TASK,))
            for key in ("session_id", "node_id"):
                with self.subTest(current_node_binding=key):
                    invalid_snapshot = {
                        **snapshot,
                        "current_nodes": [
                            {**row, key: "12345678-1234-4234-8234-123456789abc"}
                            if row["task_id"] == TASK else row
                            for row in snapshot["current_nodes"]
                        ],
                    }
                    with mock.patch.object(backend, "_snapshot_once", return_value=invalid_snapshot):
                        with self.assertRaisesRegex(CleanupError, "current Agent"):
                            backend.prepare(request)
            self.assertEqual(artifacts, {})
            handoff = backend.prepare(request)
            stored = next(iter(artifacts.values()))
            stored_value = json.loads(stored)
            expected_context_sha256 = hashlib.sha256(
                b"opaque genuine native cleanup context",
            ).hexdigest()
            self.assertEqual(
                stored_value["cleanup_context_sha256"], expected_context_sha256,
            )
            self.assertEqual(stored_value["pull_request"]["base_oid"], base_oid)
            self.assertNotIn("expected_base_oid", request)
            sibling = self.root / "exact-owned-evidence.json"
            sibling.write_text("task-owned evidence", encoding="utf-8")
            proof = {
                "path": str(sibling), "kind": "evidence",
                "owner_task_id": TASK, "evidence_reference": "native-assignment:exact-artifact",
            }
            with (
                mock.patch.object(
                    backend, "_all_native_workspace_roots",
                    return_value=[self.project_root, task_root],
                ),
                mock.patch.object(backend, "_profile_memory_roots", return_value=[r2_root]),
            ):
                owned_handoff = backend.prepare({**request, "owned_containers": [proof]})
                owned = json.loads(artifacts[owned_handoff["inventory"]["sha256"]])
                self.assertEqual(len(owned["owned_containers"]), 1)
                resource = owned["owned_containers"][0]
                self.assertEqual(resource["inode"], sibling.stat().st_ino)
                self.assertEqual(resource["owner_task_id"], TASK)
                self.assertEqual(resource["evidence_reference"], proof["evidence_reference"])
                session_parent = self.persistence / f"projects/{PROJECT}/sessions"
                session_sibling = session_parent / "exact-preserved-artifact"
                session_sibling.mkdir()
                session_proof = {**proof, "path": str(session_sibling), "kind": "archive"}
                both_handoff = backend.prepare({
                    **request, "owned_containers": [proof, session_proof],
                })
                both = json.loads(artifacts[both_handoff["inventory"]["sha256"]])
                self.assertEqual(
                    [row["path"] for row in both["owned_containers"]],
                    [str(sibling), str(session_sibling)],
                )
                # Fresh native registration overrides an old artifact claim.
                with mock.patch.object(
                    backend, "_all_native_workspace_roots",
                    return_value=[self.project_root, task_root, session_sibling],
                ):
                    with self.assertRaisesRegex(CleanupError, "collides"):
                        backend._check_owned_container_scope(both)
                for invalid in (
                    {**proof, "owner_task_id": OTHER_TASK},
                    {**proof, "path": str(task_root)},
                    {**proof, "path": str(self.project_root)},
                    {**proof, "path": str(session_parent)},
                    {**proof, "path": str(r2_root)},
                    {**proof, "path": str(self.database)},
                    {**proof, "evidence_reference": ""},
                ):
                    with self.subTest(invalid=invalid), self.assertRaises(CleanupError):
                        backend.prepare({**request, "owned_containers": [invalid]})
            with self.assertRaisesRegex(CleanupError, "fields are invalid"):
                backend.prepare({**request, "expected_base_oid": base_oid})
            advanced_base = {**provider_pr, "base_oid": "d" * 40}
            with mock.patch.object(backend, "_provider_pr", return_value=advanced_base):
                advanced_handoff = backend.prepare(request)
            self.assertNotEqual(
                advanced_handoff["inventory"]["sha256"],
                handoff["inventory"]["sha256"],
            )
            with (
                mock.patch.object(
                    backend, "_provider_pr",
                    return_value={**provider_pr, "base_ref": "different-branch"},
                ),
                self.assertRaisesRegex(CleanupError, "base branch"),
            ):
                backend.prepare(request)
            with (
                mock.patch.object(
                    delivery, "observe_script_identity",
                    return_value=(identity, script_detail, graph_response),
                ),
                mock.patch.object(
                    operations, "native_local_context", return_value=self.context,
                ),
            ):
                immutable = backend.load_handoff(handoff, native_context)
            self.assertEqual(immutable.raw, stored)
            self.assertEqual(
                immutable.value["cleanup_context_sha256"], expected_context_sha256,
            )
            # Supported native leave releases the current owner AND clears its
            # managed worktree, without changing workspace or frozen inventory.
            self.assertEqual(immutable.value["sessions"][0]["worktree_id"], "worktree-1")
            self.writer.execute("UPDATE sessions SET worktree_id=NULL WHERE id=?", (SESSION,))
            self.writer.execute(
                "UPDATE task_current_nodes SET node_id=?,session_id=NULL WHERE task_id=?",
                (ENTRY_NODE, TASK),
            )
            self.writer.commit()
            with mock.patch.object(
                delivery, "observe_script_identity",
                return_value=(identity, script_detail, graph_response),
            ):
                fresh = backend.revalidate(immutable.value)
                self.assertIsNone(next(row for row in fresh if row["session_id"] == SESSION)["worktree_id"])
                native = next(row for row in fresh if row["session_id"] == SESSION)
                for column, changed in (
                    ("worktree_id", "foreign-worktree"),
                    ("workspace_id", "foreign-workspace"),
                    ("task_id", OTHER_TASK),
                    ("project_id", PROJECT.replace("1111", "7777")),
                    ("artifact_relpath", f"projects/{PROJECT}/sessions/{ENTRY_NODE}"),
                    ("parent_agent_session_id", ENTRY_NODE),
                    ("previous_session_id", ENTRY_NODE),
                ):
                    with self.subTest(native_drift=column):
                        self.writer.execute(f"UPDATE sessions SET {column}=? WHERE id=?", (changed, SESSION))
                        self.writer.commit()
                        with self.assertRaises(CleanupError):
                            backend.revalidate(immutable.value)
                        self.writer.execute(
                            f"UPDATE sessions SET {column}=? WHERE id=?", (native[column], SESSION),
                        )
                        self.writer.commit()
                for gate in ("unlisted-owner", "shared-session", "shared-root", "foreign-root-workspace"):
                    altered = json.loads(stored)
                    if gate == "unlisted-owner":
                        altered["release"]["owner_session_ids"] = []
                    elif gate == "shared-session":
                        altered["sessions"][0]["disposition"] = "shared-in-use"
                    elif gate == "shared-root":
                        altered["worktrees"][0]["disposition"] = "shared-in-use"
                    else:
                        altered["worktrees"][0]["workspace_id"] = "foreign-workspace"
                    with self.subTest(release_gate=gate), self.assertRaises(CleanupError):
                        backend.revalidate(altered)
                self.writer.execute(
                    "UPDATE task_current_nodes SET session_id=? WHERE task_id=?", (SESSION, TASK),
                )
                self.writer.commit()
                with self.assertRaisesRegex(CleanupError, "still owns"):
                    backend.revalidate(immutable.value)
                self.writer.execute("UPDATE task_current_nodes SET session_id=NULL WHERE task_id=?", (TASK,))
                self.writer.execute("UPDATE sessions SET worktree_id='worktree-1' WHERE id=?", (SESSION,))
                self.writer.commit()
                backend.revalidate(immutable.value)  # unchanged bindings remain valid too
                self.writer.execute("UPDATE sessions SET worktree_id=NULL WHERE id=?", (SESSION,))
                self.writer.commit()
            self.assertEqual(immutable.raw, stored)
            executor = Executor()
            result = execute_inventory(immutable, executor)
            self.assertEqual(result["phase"], "completed")
            self.assertIn(("delete-session", SESSION), executor.calls)
            self.assertIn(("delete-worktree", "worktree-1"), executor.calls)
            self.assertEqual(
                result["inventory"]["sha256"], hashlib.sha256(stored).hexdigest(),
            )

    def test_history_inspection_accepts_standalone_operator_and_missing_worktree(self):
        victim_project = "project-88888888-8888-4888-8888-888888888888"
        victim_workspace = "workspace-app"
        victim_primary = self.root / "appsome"
        victim_primary.mkdir()
        victim_r2 = self.root / "appsome-r2"
        victim_r2.mkdir()
        (victim_primary / ".kent").mkdir()
        (victim_primary / ".kent/workflow-profile.toml").write_text(
            f'[project_memory]\nroot = "{victim_r2}"\n',
            encoding="utf-8",
        )
        (self.persistence / f"projects/{PROJECT}/sessions").mkdir(parents=True)
        (self.persistence / f"projects/{victim_project}/sessions").mkdir(parents=True)
        victim_session = "66666666-6666-4666-8666-666666666666"
        self.writer.execute(
            "UPDATE sessions SET task_id=NULL,worktree_id=NULL WHERE id=?",
            (SESSION,),
        )
        self.writer.execute("DELETE FROM task_current_nodes WHERE task_id=?", (TASK,))
        self.writer.execute(
            "INSERT INTO workspaces VALUES (?,?,?)",
            (victim_workspace, victim_project, str(victim_primary)),
        )
        self.writer.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,NULL)",
            (
                OTHER_TASK, victim_project,
                bytes.fromhex(WORKFLOW.replace("-", "")), victim_workspace,
            ),
        )
        self.writer.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                victim_session, victim_project, victim_workspace, "worktree-app-gone",
                f"projects/{victim_project}/sessions/{victim_session}",
                "{}", "{}", "{}", None, None, OTHER_TASK, 0, 1,
            ),
        )
        cleanup_result = {
            "schema": "appsome-native-cleanup-result-v1",
            "project_id": victim_project, "task_id": OTHER_TASK,
            "endpoint": {
                "repository": "org/repo", "pr": 4, "base": "main",
                "head": "a" * 40, "state": "MERGED",
                "merge_commit": "c" * 40, "merged_at": "2026-11-16T03:32:34Z",
                "observation": "Historical provider proof; not current authority",
                "original_delivery_proof": "d" * 64,
            },
        }
        self.writer.execute(
            "INSERT INTO task_current_nodes "
            "(task_id,node_id,current_input_values_json,prior_node_values_json,"
            "transition_branch_key,session_id,scheduling_state,entered_by_edge_id) "
            "VALUES (?,?,?, ?,NULL,NULL,NULL,NULL)",
            (
                OTHER_TASK, DONE_NODE,
                json.dumps({"cleanup_result": json.dumps(cleanup_result)}),
                "{}",
            ),
        )
        self.writer.commit()
        backend = NativeCleanupBackend(
            workspace=self.project_root, kent=sys.executable, context=self.context,
            runner=self.runner,
        )
        victim_detail = {
            "summary": {
                "id": OTHER_TASK, "project_id": victim_project,
                "workflow_id": WORKFLOW, "short_id": "APP-158",
            },
            "status": {"native_state": "terminal"},
            "worktree_path": None,
        }
        graph = {
            "workflow_id": WORKFLOW,
            "nodes": [{"id": DONE_NODE, "kind": "terminal", "key": "done"}],
        }
        provider_pr = {
            "repository": "org/repo", "number": 4,
            "url": "https://github.com/org/repo/pull/4",
            "base_ref": "main", "base_oid": "b" * 40,
            "head_ref": "task/app-158", "head_oid": "a" * 40,
            "state": "MERGED", "merged_at": "2026-11-17T10:00:00Z",
            "merge_commit": "c" * 40,
        }

        def runner(args, *, cwd, **_kwargs):
            self.assertEqual(cwd, self.project_root)
            if args == ["session-id"]:
                return 0, (
                    f"Short ID: 1\nUUID: {SESSION}\nLog path: /tmp/session.log\n"
                ).encode(), b""
            self.fail(f"unexpected native command in history fixture: {args}")

        request = {
            "schema": "merged-cleanup-history-request-v1",
            "project_id": victim_project,
            "task_ids": [OTHER_TASK],
            "permission_evidence": {
                "reference": "native original permission",
                "context": "assigned operator verified exact finite historical set",
            },
        }
        resource_root = self.persistence / f"projects/{victim_project}/sessions"
        archive_file = resource_root / "task-archive.json"
        archive_file.write_text("preserved task archive", encoding="utf-8")
        evidence_directory = resource_root / "task-evidence"
        evidence_directory.mkdir()
        (evidence_directory / "report.txt").write_text("preserved report", encoding="utf-8")
        request["owned_containers"] = [
            {
                "path": str(archive_file), "kind": "archive",
                "owner_task_id": OTHER_TASK,
                "evidence_reference": "original finite ownership map: archive-1",
            },
            {
                "path": str(evidence_directory), "kind": "evidence",
                "owner_task_id": OTHER_TASK,
                "evidence_reference": "original finite ownership map: evidence-1",
            },
        ]
        with self.assertRaisesRegex(CleanupError, "owner"):
            backend.inspect_history({
                **request,
                "owned_containers": [{
                    **request["owned_containers"][0], "owner_task_id": TASK,
                }],
            })
        with (
            mock.patch.dict(os.environ, {"KENT_SESSION_ID": SESSION}, clear=True),
            mock.patch.object(backend, "runner", side_effect=runner),
            mock.patch.object(backend, "_native_task", return_value=victim_detail),
            mock.patch.object(
                backend, "_read",
                return_value=json.dumps({
                    "workflow_id": WORKFLOW, "graph": graph,
                }).encode(),
            ),
            mock.patch.object(backend, "_provider_pr", return_value=provider_pr),
            mock.patch.object(backend, "_github_repository", return_value="org/repo"),
        ):
            finite = backend.inspect_history(request)
            legacy_request = {
                key: value for key, value in request.items()
                if key != "owned_containers"
            }
            legacy_finite = backend.inspect_history(legacy_request)
            fresh_sessions = backend.revalidate(finite.inventories[0].value)
            self.assertIn(victim_session, [row["session_id"] for row in fresh_sessions])
            for resource in finite.inventories[0].value["owned_containers"]:
                self.assertEqual(backend.remove_owned_container(resource), "removed")
                self.assertFalse(Path(resource["path"]).exists())
            self.assertTrue(resource_root.is_dir())
            missing_path = self.root / "missing-registered-victim"
            self.writer.execute(
                "INSERT INTO worktrees VALUES (?,?,?)",
                ("worktree-app-gone", victim_workspace, str(missing_path)),
            )
            self.writer.execute(
                "UPDATE tasks SET managed_worktree_id=? WHERE id=?",
                ("worktree-app-gone", OTHER_TASK),
            )
            self.writer.commit()
            victim_detail["worktree_path"] = str(missing_path)
            backend._snapshots = {}
            with mock.patch.object(backend, "_git", return_value=""):
                registered_missing = backend.inspect_history(legacy_request)
                frozen_missing = registered_missing.inventories[0].value
                self.assertEqual(frozen_missing["worktrees"][0]["disposition"], "absent")
                self.assertEqual(frozen_missing["worktrees"][0]["path"], str(missing_path))
                backend.revalidate(frozen_missing)
                with (
                    mock.patch.object(
                        backend, "_git",
                        return_value=f"worktree {missing_path}\nHEAD {'a' * 40}\n\n",
                    ),
                    self.assertRaisesRegex(CleanupError, "Git"),
                ):
                    backend.revalidate(frozen_missing)
                # Direct Task Agents have already gone. The genuine registered
                # standalone supervisor remains, not an invented replacement.
                membership = {
                    "schema": "merged-cleanup-supervision-v1", "project_id": victim_project,
                    "supervisor_session_id": victim_session, "task_ids": [OTHER_TASK],
                    "reused_root_session_ids": [],
                }
                self.writer.execute("UPDATE sessions SET task_id=NULL WHERE id=?", (victim_session,))
                self.writer.execute("INSERT INTO task_comments VALUES (?,?,?,?,?,?,?)", (
                    "comment-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", OTHER_TASK,
                    json.dumps(membership, sort_keys=True, separators=(",", ":")), "agent", "fixture", 1, 1,
                ))
                self.writer.commit()
                backend._snapshots = {}
                residual = backend.inspect_history(legacy_request).inventories[0]
                self.assertIsNone(residual.value["sessions"][0]["task_id"])
                self.assertEqual(residual.value["sessions"][0]["supervisor_root_ids"], [victim_session])
                self.assertEqual(residual.value["worktrees"][0]["disposition"], "absent")
                backend.revalidate(residual.value)
                self.writer.execute(
                    "UPDATE task_current_nodes SET session_id=? WHERE task_id=?", (victim_session, OTHER_TASK),
                )
                self.writer.commit()
                with self.assertRaisesRegex(CleanupError, "current native owner"):
                    backend.revalidate(residual.value)
                self.writer.execute("UPDATE task_current_nodes SET session_id=NULL WHERE task_id=?", (OTHER_TASK,))
                self.writer.execute("UPDATE sessions SET worktree_id=NULL WHERE id=?", (victim_session,))
                self.writer.execute("UPDATE tasks SET managed_worktree_id=NULL WHERE id=?", (OTHER_TASK,))
                self.writer.commit()
                victim_detail["worktree_path"] = None
                backend._snapshots = {}
                rootless = backend.inspect_history(legacy_request).inventories[0]
                self.assertIsNone(rootless.value["subject"]["worktree_id"])
                self.assertEqual(rootless.value["worktrees"], [])
                backend.revalidate(rootless.value)
        self.assertEqual(finite.value["project_id"], victim_project)
        self.assertEqual(finite.value["operator_project_id"], PROJECT)
        self.assertIsNone(finite.value["operator_task_id"])
        inventory = finite.inventories[0].value
        self.assertEqual(inventory["task_id"], OTHER_TASK)
        self.assertEqual(inventory["pull_request"]["head_oid"], "a" * 40)
        self.assertEqual([row["worktree_id"] for row in inventory["worktrees"]],
                         ["worktree-app-gone"])
        self.assertEqual(inventory["worktrees"][0]["disposition"], "absent")
        self.assertEqual(
            [(row["kind"], row["owner_task_id"]) for row in inventory["owned_containers"]],
            [("archive", OTHER_TASK), ("evidence", OTHER_TASK)],
        )
        self.assertEqual(
            [row["evidence_reference"] for row in inventory["owned_containers"]],
            [
                "original finite ownership map: archive-1",
                "original finite ownership map: evidence-1",
            ],
        )
        self.assertEqual(
            legacy_finite.inventories[0].value["owned_containers"], [],
        )
        self.assertEqual(
            [row["session_id"] for row in inventory["sessions"]],
            [victim_session],
        )


if __name__ == "__main__":
    unittest.main()
