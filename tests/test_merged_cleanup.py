from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from workflowkit.cleanup import (
    CleanupError,
    build_session_closure,
    canonical_bytes,
    execute_inventory,
    output_transition,
    _read_control_file,
    _write_inventory_file,
    history_inventory_from_bytes,
    validate_history_inventory,
    validate_inventory,
)


PROJECT = "project-11111111-1111-4111-8111-111111111111"
TASK = "task-22222222-2222-4222-8222-222222222222"
OTHER_TASK = "task-33333333-3333-4333-8333-333333333333"
WORKFLOW = "44444444-4444-4444-8444-444444444444"
ROOT_SESSION = "55555555-5555-4555-8555-555555555555"
CHILD_SESSION = "66666666-6666-4666-8666-666666666666"
GRANDCHILD_SESSION = "77777777-7777-4777-8777-777777777777"
SUPERVISOR_SESSION = "88888888-8888-4888-8888-888888888888"
OID_A = "a" * 40
OID_B = "b" * 40
OID_C = "c" * 40


def session(
    session_id: str,
    *,
    project_id: str = PROJECT,
    task_id: str | None = None,
    parent: str | None = None,
    previous: str | None = None,
    workspace_id: str | None = None,
    worktree_id: str | None = None,
    roots: list[str] | None = None,
    disposition: str = "remove",
) -> dict:
    return {
        "session_id": session_id,
        "project_id": project_id,
        "task_id": task_id,
        "parent_agent_session_id": parent,
        "previous_session_id": previous,
        "workspace_id": workspace_id,
        "worktree_id": worktree_id,
        "artifact_relpath": f"projects/{project_id}/sessions/{session_id}",
        "device": None,
        "inode": None,
        "supervisor_root_ids": roots or [],
        "disposition": disposition,
    }


def base_inventory(*, sessions=None, supervision=None, members=None, worktrees=None):
    value = {
        "schema": "merged-cleanup-inventory-v1",
        "project_id": PROJECT,
        "task_id": TASK,
        "workflow_id": WORKFLOW,
        "subject": {
            "task_short_id": "KIT-1",
            "workspace_id": "workspace-1",
            "worktree_id": "worktree-1",
            "worktree_path": "/tmp/cleanup-task-worktree",
            "branch": "task/cleanup",
            "head_oid": OID_A,
            "branch_oid": OID_A,
        },
        "pull_request": {
            "repository": "org/repo",
            "number": 4,
            "url": "https://github.com/org/repo/pull/4",
            "base_ref": "main",
            "base_oid": OID_B,
            "head_ref": "source-branch",
            "head_oid": OID_A,
            "state": "MERGED",
            "merged_at": "2026-11-17T10:00:00Z",
            "merge_commit": OID_C,
        },
        "policy": {
            "schema": "merged-cleanup-policy-v1",
            "project_id": PROJECT,
            "workflow_id": WORKFLOW,
            "mode": "after_merge",
        },
        "cleanup_context_sha256": "c" * 64,
        "history_operation": None,
        "policy_provenance": {
            "transition_group_id": "99999999-9999-4999-8999-999999999999",
            "transition_id": "delivered",
            "source_node_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "entry_node_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "edge_key": "finish_delivered",
            "policy_sha256": "",
        },
        "release": {
            "prepared_by_session_id": ROOT_SESSION,
            "owner_session_ids": [ROOT_SESSION],
            "release_sha256": "e" * 64,
        },
        "supervision": supervision or [],
        "sessions": sessions or [session(ROOT_SESSION, task_id=TASK)],
        "member_outcomes": members or [{
            "task_id": TASK, "native_state": "final_disposal",
            "outcome": "merged", "pull_request_url": "https://github.com/org/repo/pull/4",
            "head_oid": OID_A, "merged_at": "2026-11-17T10:00:00Z",
            "merge_commit": OID_C,
        }],
        "worktrees": worktrees or [{
            "project_id": PROJECT, "workspace_id": "workspace-1",
            "worktree_id": "worktree-1", "path": "/tmp/cleanup-task-worktree",
            "branch": "task/cleanup", "head_oid": OID_A, "branch_oid": OID_A,
            "role": "task", "disposition": "remove", "owner_task_id": TASK,
        }],
        "owned_containers": [],
        "protected_roots": [
            {"path": "/tmp/cleanup-primary", "kind": "primary-workspace"},
            {"path": "/tmp/cleanup-r2", "kind": "r2-storage"},
        ],
    }
    value = {
        **value,
        "policy_provenance": {
            **value["policy_provenance"],
            "policy_sha256": hashlib.sha256(
                canonical_bytes(value["policy"], maximum=1000),
            ).hexdigest(),
        },
    }
    return value


class RecordingBackend:
    def __init__(self, *, fail_session=None):
        self.calls = []
        self.fail_session = fail_session

    def begin_inventory(self, _inventory):
        return None

    def record_settlement(self, _stage, _kind, _index, disposition):
        return disposition

    def require_physical_coverage(self):
        pass

    def revalidate(self, inventory):
        self.calls.append(("revalidate", None))
        return ()

    def delete_session(self, row):
        self.calls.append(("delete-session", row["session_id"]))
        if row["session_id"] == self.fail_session:
            raise CleanupError("uncertain native Session delete")
        return "removed"

    def remove_session_container(self, row):
        self.calls.append(("remove-session-container", row["session_id"]))
        return "removed"

    def remove_owned_container(self, row):
        self.calls.append(("remove-owned-container", row["path"]))
        return "removed"

    def delete_worktree(self, row):
        self.calls.append(("delete-worktree", row["worktree_id"]))
        return "removed"

    def verify_postconditions(self, inventory):
        self.calls.append(("postconditions", None))
        return {
            "sessions_absent": sum(
                row["disposition"] == "remove" for row in inventory["sessions"]
            ),
            "session_containers_absent": sum(
                row["disposition"] == "remove" for row in inventory["sessions"]
            ),
            "owned_containers_absent": sum(
                row["disposition"] == "remove"
                for row in inventory["owned_containers"]
            ),
            "worktrees_absent": sum(
                row["disposition"] in {"remove", "absent"}
                for row in inventory["worktrees"]
            ),
            "branches_retained": sum(
                row["disposition"] == "remove" for row in inventory["worktrees"]
            ),
        }


class DescendantRaceBackend(RecordingBackend):
    def __init__(self):
        super().__init__()
        self.child_deleted = False
        self.deleted_sessions = []

    def revalidate(self, inventory):
        super().revalidate(inventory)
        if self.child_deleted:
            raise CleanupError("unexpected descendant appeared after inventory freeze")

    def delete_session(self, row):
        self.deleted_sessions.append(row["session_id"])
        result = super().delete_session(row)
        if row["session_id"] == CHILD_SESSION:
            self.child_deleted = True
        return result


class MergedCleanupPolicyTests(unittest.TestCase):
    def test_executor_persists_each_lifecycle_ack_before_physical_cleanup(self):
        class Receipted(RecordingBackend):
            def begin_inventory(inner, inventory):
                inner.calls.append(("begin", inventory.sha256))

            def record_settlement(inner, stage, kind, index, disposition):
                inner.calls.append((stage, kind, index))
                return disposition

            def require_physical_coverage(inner):
                inner.calls.append(("coverage", None))

        inventory = validate_inventory(base_inventory())
        backend = Receipted()
        execute_inventory(inventory, backend)
        calls = backend.calls
        native = calls.index(("session_native_settled", "session", 0))
        physical = calls.index(("remove-session-container", ROOT_SESSION))
        coverage = calls.index(("coverage", None))
        root = calls.index(("delete-worktree", "worktree-1"))
        self.assertLess(native, physical)
        self.assertLess(calls.index(("container_settled", "session", 0)), coverage)
        self.assertLess(coverage, root)
        self.assertLess(root, calls.index(("worktree_settled", "worktree", 0)))

    def test_failed_ack_persistence_blocks_residuals_and_root(self):
        class Receipted(RecordingBackend):
            def begin_inventory(inner, _inventory):
                pass

            def record_settlement(inner, *_args):
                raise CleanupError("durable ACK missing")

        backend = Receipted()
        with self.assertRaisesRegex(CleanupError, "durable ACK missing"):
            execute_inventory(validate_inventory(base_inventory()), backend)
        self.assertFalse(any(call[0] == "remove-session-container" for call in backend.calls))
        self.assertFalse(any(call[0] == "delete-worktree" for call in backend.calls))

    def test_inventory_binds_closed_policy_and_native_provenance(self):
        value = base_inventory()
        inventory = validate_inventory(value)
        self.assertEqual(inventory.value["policy"], value["policy"])
        self.assertEqual(inventory.value["policy_provenance"], value["policy_provenance"])
        for changed in (
            {**value, "policy": {**value["policy"], "mode": "other"}},
            {**value, "policy": {**value["policy"], "extra": True}},
            {
                **value,
                "policy_provenance": {
                    **value["policy_provenance"], "policy_sha256": "f" * 64,
                },
            },
            {**value, "authority": {"scope_sha256": "d" * 64}},
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(CleanupError):
                    validate_inventory(changed)

    def test_finite_history_inventory_binds_operator_evidence_and_exact_task_set(self):
        operator_session = "99999999-9999-4999-8999-999999999999"
        evidence = {
            "reference": "native approval record",
            "context": "assigned operator verified the historical finite cleanup scope",
        }
        operation = {
            "schema": "merged-cleanup-history-operation-v1",
            "project_id": PROJECT,
            "operator_project_id": PROJECT,
            "operator_task_id": OTHER_TASK,
            "operator_session_id": operator_session,
            "selected_task_ids": [TASK],
            "permission_evidence": evidence,
        }
        value = base_inventory()
        value.update({
            "policy": None,
            "policy_provenance": None,
            "cleanup_context_sha256": hashlib.sha256(canonical_bytes(evidence)).hexdigest(),
            "history_operation": operation,
        })
        value["release"]["prepared_by_session_id"] = operator_session
        value["member_outcomes"][0]["native_state"] = "terminal"
        manifest = validate_history_inventory({
            "schema": "merged-cleanup-history-inventory-v1",
            "project_id": PROJECT, "task_ids": [TASK],
            "operator_project_id": PROJECT,
            "operator_task_id": OTHER_TASK,
            "operator_session_id": operator_session,
            "permission_evidence": evidence,
            "inventories": [value],
        })
        self.assertEqual(manifest.value["task_ids"], [TASK])
        self.assertEqual(
            history_inventory_from_bytes(manifest.raw, manifest.sha256).sha256,
            manifest.sha256,
        )
        with self.assertRaisesRegex(CleanupError, "confirmation"):
            history_inventory_from_bytes(manifest.raw, "f" * 64)
        with self.assertRaises(CleanupError):
            validate_history_inventory({
                **manifest.value, "task_ids": [TASK, OTHER_TASK],
            })

    def test_historical_standalone_cross_project_operator_accepts_absent_worktree(self):
        operator_project = "project-88888888-8888-4888-8888-888888888888"
        operator_session = "99999999-9999-4999-8999-999999999999"
        evidence = {"reference": "original decision", "context": "finite approved selection"}
        value = base_inventory()
        value["policy"] = None
        value["policy_provenance"] = None
        value["cleanup_context_sha256"] = hashlib.sha256(canonical_bytes(evidence)).hexdigest()
        value["history_operation"] = {
            "schema": "merged-cleanup-history-operation-v1",
            "project_id": PROJECT,
            "operator_project_id": operator_project,
            "operator_task_id": None,
            "operator_session_id": operator_session,
            "selected_task_ids": [TASK],
            "permission_evidence": evidence,
        }
        value["release"]["prepared_by_session_id"] = operator_session
        value["release"]["owner_session_ids"] = []
        value["member_outcomes"][0]["native_state"] = "terminal"
        value["subject"].update({
            "worktree_id": "worktree-already-absent",
            "worktree_path": None, "branch": None,
            "head_oid": None, "branch_oid": None,
        })
        value["worktrees"] = [{
            "project_id": PROJECT, "workspace_id": "workspace-1",
            "worktree_id": "worktree-already-absent", "path": None,
            "branch": None, "head_oid": None, "branch_oid": None,
            "role": "task", "disposition": "absent", "owner_task_id": TASK,
        }]
        inventory = validate_inventory(value)
        backend = RecordingBackend()
        result = execute_inventory(inventory, backend)
        self.assertEqual(result["dispositions"]["worktrees"], "a")
        self.assertNotIn(
            ("delete-worktree", "worktree-already-absent"), backend.calls,
        )
        finite = validate_history_inventory({
            "schema": "merged-cleanup-history-inventory-v1",
            "project_id": PROJECT, "operator_project_id": operator_project,
            "task_ids": [TASK], "operator_task_id": None,
            "operator_session_id": operator_session,
            "permission_evidence": evidence, "inventories": [value],
        })
        self.assertEqual(finite.value["operator_project_id"], operator_project)

    def test_closure_follows_inverse_parent_across_projects_not_previous(self):
        rows = [
            session(ROOT_SESSION, task_id=TASK),
            session(CHILD_SESSION, project_id="project-99999999-9999-4999-8999-999999999999",
                    parent=ROOT_SESSION),
            session(GRANDCHILD_SESSION, project_id="project-99999999-9999-4999-8999-999999999999",
                    parent=CHILD_SESSION),
            session(SUPERVISOR_SESSION, previous=ROOT_SESSION),
        ]
        result = build_session_closure(rows, task_id=TASK, project_id=PROJECT)
        self.assertEqual(
            [row["session_id"] for row in result],
            sorted([CHILD_SESSION, GRANDCHILD_SESSION, ROOT_SESSION]),
        )

    def test_closure_rejects_descendant_owned_by_unregistered_task(self):
        rows = [
            session(ROOT_SESSION, task_id=TASK),
            session(CHILD_SESSION, task_id=OTHER_TASK, parent=ROOT_SESSION),
        ]
        with self.assertRaisesRegex(CleanupError, "unregistered Task owner"):
            build_session_closure(rows, task_id=TASK, project_id=PROJECT)

    def test_missing_task_session_ancestry_is_a_completeness_blocker(self):
        rows = [session(ROOT_SESSION, task_id=TASK, parent=SUPERVISOR_SESSION)]
        with self.assertRaisesRegex(CleanupError, "root is missing|ancestry"):
            build_session_closure(rows, task_id=TASK, project_id=PROJECT)

    def test_foreign_reassigned_catalog_paths_do_not_enter_removal_closure(self):
        rows = [session(ROOT_SESSION, task_id=TASK)]
        for index in range(69):
            sid = f"{index + 100:08x}-1111-4111-8111-{index + 100:012x}"
            foreign = session(sid)
            foreign["artifact_relpath"] = (
                f"projects/project-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/sessions/{sid}"
            )
            rows.append(foreign)
        closure = build_session_closure(rows, task_id=TASK, project_id=PROJECT)
        self.assertEqual([row["session_id"] for row in closure], [ROOT_SESSION])
        rows[-1]["parent_agent_session_id"] = ROOT_SESSION
        with self.assertRaisesRegex(CleanupError, "artifact_relpath"):
            build_session_closure(rows, task_id=TASK, project_id=PROJECT)

    def test_shared_supervisor_session_and_worktree_are_held_as_a_whole(self):
        membership = {
            "schema": "merged-cleanup-supervision-v1",
            "project_id": PROJECT,
            "supervisor_session_id": SUPERVISOR_SESSION,
            "task_ids": sorted([TASK, OTHER_TASK]),
            "reused_root_session_ids": [],
        }
        rows = [
            session(ROOT_SESSION, task_id=TASK),
            session(SUPERVISOR_SESSION, workspace_id="workspace-2",
                    worktree_id="worktree-supervisor",
                    roots=[SUPERVISOR_SESSION],
                    disposition="shared-in-use"),
            session(CHILD_SESSION, parent=SUPERVISOR_SESSION,
                    workspace_id="workspace-2", worktree_id="worktree-supervisor",
                    roots=[SUPERVISOR_SESSION], disposition="shared-in-use"),
        ]
        outcomes = [
            base_inventory()["member_outcomes"][0],
            {
                "task_id": OTHER_TASK, "native_state": "active", "outcome": "unknown",
                "pull_request_url": None, "head_oid": None, "merged_at": None,
                "merge_commit": None,
            },
        ]
        worktrees = base_inventory()["worktrees"] + [{
            "project_id": PROJECT, "workspace_id": "workspace-2",
            "worktree_id": "worktree-supervisor", "path": "/tmp/cleanup-supervisor-worktree",
            "branch": "supervisor/group", "head_oid": OID_B, "branch_oid": OID_B,
            "role": "supervisor", "disposition": "shared-in-use",
            "owner_task_id": OTHER_TASK,
        }]
        value = base_inventory(
            sessions=rows, supervision=[membership], members=outcomes,
            worktrees=worktrees,
        )
        validated = validate_inventory(value)
        backend = RecordingBackend()
        result = execute_inventory(validated, backend)
        self.assertIn(("delete-session", ROOT_SESSION), backend.calls)
        self.assertNotIn(("delete-session", SUPERVISOR_SESSION), backend.calls)
        self.assertNotIn(("delete-session", CHILD_SESSION), backend.calls)
        self.assertNotIn(("delete-worktree", "worktree-supervisor"), backend.calls)
        dispositions = result["dispositions"]
        sorted_session_ids = sorted(row["session_id"] for row in rows)
        self.assertEqual(
            dispositions["sessions"][sorted_session_ids.index(SUPERVISOR_SESSION)],
            "s",
        )

    def test_unsettled_group_also_holds_reused_helper_tree(self):
        membership = {
            "schema": "merged-cleanup-supervision-v1",
            "project_id": PROJECT,
            "supervisor_session_id": SUPERVISOR_SESSION,
            "task_ids": sorted([TASK, OTHER_TASK]),
            "reused_root_session_ids": [GRANDCHILD_SESSION],
        }
        rows = [
            session(ROOT_SESSION, task_id=TASK),
            session(SUPERVISOR_SESSION, roots=[SUPERVISOR_SESSION],
                    disposition="shared-in-use"),
            session(GRANDCHILD_SESSION, parent=SUPERVISOR_SESSION,
                    workspace_id="workspace-2", worktree_id="worktree-helper",
                    roots=[GRANDCHILD_SESSION, SUPERVISOR_SESSION],
                    disposition="shared-in-use"),
        ]
        outcomes = [
            base_inventory()["member_outcomes"][0],
            {
                "task_id": OTHER_TASK, "native_state": "active", "outcome": "unknown",
                "pull_request_url": None, "head_oid": None, "merged_at": None,
                "merge_commit": None,
            },
        ]
        worktrees = base_inventory()["worktrees"] + [{
            "project_id": PROJECT, "workspace_id": "workspace-2",
            "worktree_id": "worktree-helper", "path": "/tmp/cleanup-helper-worktree",
            "branch": "helper/group", "head_oid": OID_B, "branch_oid": OID_B,
            "role": "supervisor", "disposition": "shared-in-use",
            "owner_task_id": OTHER_TASK,
        }]
        inventory = validate_inventory(base_inventory(
            sessions=rows, supervision=[membership], members=outcomes,
            worktrees=worktrees,
        ))
        backend = RecordingBackend()
        execute_inventory(inventory, backend)
        self.assertNotIn(("delete-session", GRANDCHILD_SESSION), backend.calls)
        self.assertNotIn(("delete-worktree", "worktree-helper"), backend.calls)

    def test_executor_orders_children_before_parent_and_worktree_last(self):
        child = session(CHILD_SESSION, parent=ROOT_SESSION)
        value = base_inventory(sessions=[session(ROOT_SESSION, task_id=TASK), child])
        value["release"]["owner_session_ids"] = [ROOT_SESSION]
        inventory = validate_inventory(value)
        backend = RecordingBackend()
        result = execute_inventory(inventory, backend)
        effect_calls = [name for name, _ in backend.calls]
        self.assertEqual(effect_calls[0], "revalidate")
        self.assertLess(
            next(i for i, call in enumerate(backend.calls) if call == ("delete-session", CHILD_SESSION)),
            next(i for i, call in enumerate(backend.calls) if call == ("delete-session", ROOT_SESSION)),
        )
        self.assertLess(effect_calls.index("delete-worktree"), effect_calls.index("postconditions"))
        self.assertEqual(result["phase"], "completed")
        output = output_transition(result)
        self.assertIn(b'"transition":"disposal_completed"', output)

    def test_result_roundtrip_keeps_endpoint_and_compact_dispositions(self):
        rows = [session(ROOT_SESSION, task_id=TASK)]
        rows.extend(
            session(
                f"{index:08x}-1111-4111-8111-{index:012x}",
                parent=ROOT_SESSION,
            )
            for index in range(1, 901)
        )
        inventory = validate_inventory(base_inventory(sessions=rows))
        result = execute_inventory(inventory, RecordingBackend())
        output = output_transition(result)
        self.assertLess(len(output), 65_536)
        native_output = __import__("json").loads(output)
        decoded_result = __import__("json").loads(native_output["cleanup_result"])
        self.assertEqual(decoded_result["endpoint"], {
            "repository": "org/repo", "number": 4, "base_ref": "main",
            "head_oid": OID_A,
        })
        self.assertEqual(
            len(decoded_result["dispositions"]["sessions"]), len(rows),
        )
        self.assertEqual(
            decoded_result["inventory"]["sha256"], inventory.sha256,
        )

    def test_result_capacity_is_checked_before_first_effect(self):
        inventory = validate_inventory(base_inventory())
        backend = RecordingBackend()
        with mock.patch("workflowkit.cleanup.MAX_HANDOFF_BYTES", 128):
            with self.assertRaises(CleanupError):
                execute_inventory(inventory, backend)
        self.assertEqual(backend.calls, [])

    def test_native_persistence_contains_only_registered_worktree_exception(self):
        native_root = "/tmp/kent-persistence"
        path = native_root + "/worktrees/appsome/task-1"
        value = base_inventory()
        value["subject"]["worktree_path"] = path
        value["worktrees"][0]["path"] = path
        value["protected_roots"].append({
            "path": native_root, "kind": "native-persistence",
        })
        accepted = validate_inventory(value)
        self.assertEqual(accepted.value["worktrees"][0]["path"], path)

        value["subject"]["worktree_path"] = native_root + "/db/task-1"
        value["worktrees"][0]["path"] = native_root + "/db/task-1"
        with self.assertRaisesRegex(CleanupError, "protected root"):
            validate_inventory(value)

        value["subject"]["worktree_path"] = path
        value["worktrees"][0]["path"] = path
        value["owned_containers"] = [{
            "path": native_root + "/worktrees/appsome/task-1/evidence",
            "kind": "evidence", "owner_task_id": TASK,
            "evidence_reference": "task artifact ownership record",
            "device": 1, "inode": 2, "parent_device": 1,
            "parent_inode": 3, "disposition": "remove",
        }]
        with self.assertRaisesRegex(CleanupError, "protected root"):
            validate_inventory(value)

    def test_exact_owned_session_sibling_preserves_native_storage_protection(self):
        native_root = "/tmp/kent-persistence"
        parent = f"{native_root}/projects/{PROJECT}/sessions"
        value = base_inventory()
        value["protected_roots"].append({
            "path": native_root, "kind": "native-persistence",
        })
        container = {
            "path": parent + "/exact-preserved", "kind": "evidence",
            "owner_task_id": TASK, "evidence_reference": "original exact ownership proof",
            "device": 1, "inode": 2, "parent_device": 1,
            "parent_inode": 3, "disposition": "remove",
        }
        value["owned_containers"] = [container]
        self.assertEqual(
            validate_inventory(value).value["owned_containers"], [container],
        )
        for invalid in (
            parent, parent + "/" + ROOT_SESSION, parent + "/" + CHILD_SESSION,
            f"{native_root}/db/main.sqlite3",
            f"{native_root}/projects/{PROJECT}/sessions/other/child",
        ):
            with self.subTest(path=invalid), self.assertRaises(CleanupError):
                validate_inventory({
                    **value, "owned_containers": [{**container, "path": invalid}],
                })
        with self.assertRaises(CleanupError):
            validate_inventory({
                **value, "owned_containers": [{**container, "owner_task_id": OTHER_TASK}],
            })

    def test_uncertain_session_delete_never_reaches_worktree_or_done(self):
        value = base_inventory(sessions=[
            session(ROOT_SESSION, task_id=TASK),
            session(CHILD_SESSION, parent=ROOT_SESSION),
        ])
        inventory = validate_inventory(value)
        backend = RecordingBackend(fail_session=CHILD_SESSION)
        with self.assertRaisesRegex(CleanupError, "uncertain"):
            execute_inventory(inventory, backend)
        self.assertFalse(any(name == "delete-worktree" for name, _ in backend.calls))
        self.assertFalse(any(name == "postconditions" for name, _ in backend.calls))

    def test_new_descendant_after_child_delete_holds_parent_and_worktree(self):
        inventory = validate_inventory(base_inventory(sessions=[
            session(ROOT_SESSION, task_id=TASK),
            session(CHILD_SESSION, parent=ROOT_SESSION),
        ]))
        backend = DescendantRaceBackend()
        with self.assertRaisesRegex(CleanupError, "unexpected descendant"):
            execute_inventory(inventory, backend)
        self.assertEqual(backend.deleted_sessions, [CHILD_SESSION])
        self.assertNotIn(("delete-session", ROOT_SESSION), backend.calls)
        self.assertNotIn(("delete-worktree", "worktree-1"), backend.calls)

    def test_inventory_rejects_duplicate_json_and_unheld_shared_tree(self):
        with self.assertRaises(CleanupError):
            from workflowkit.cleanup import parse_json
            parse_json(b'{"a":1,"a":2}')
        membership = {
            "schema": "merged-cleanup-supervision-v1",
            "project_id": PROJECT,
            "supervisor_session_id": SUPERVISOR_SESSION,
            "task_ids": sorted([TASK, OTHER_TASK]),
            "reused_root_session_ids": [],
        }
        value = base_inventory(
            sessions=[
                session(ROOT_SESSION, task_id=TASK),
                session(SUPERVISOR_SESSION, roots=[SUPERVISOR_SESSION]),
            ],
            supervision=[membership],
            members=[base_inventory()["member_outcomes"][0], {
                "task_id": OTHER_TASK, "native_state": "active", "outcome": "unknown",
                "pull_request_url": None, "head_oid": None, "merged_at": None,
                "merge_commit": None,
            }],
        )
        with self.assertRaisesRegex(CleanupError, "held whole"):
            validate_inventory(value)

    def test_historical_inventory_file_is_private_external_and_immutable(self):
        inventory = validate_inventory(base_inventory())
        with tempfile.TemporaryDirectory() as directory:
            target = (Path(directory) / "historical-inventory.json").resolve()
            _write_inventory_file(target, inventory)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(_read_control_file(target, 1024 * 1024), inventory.raw)
            with self.assertRaisesRegex(CleanupError, "already exists"):
                _write_inventory_file(target, inventory)

    def test_historical_control_reader_rejects_symlink_and_disposable_overlap(self):
        inventory = validate_inventory(base_inventory())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            data = (root / "request.json").resolve()
            data.write_bytes(b"{}")
            data.chmod(0o600)
            link = (root / "request-link.json").resolve()
            link.symlink_to(data)
            with self.assertRaisesRegex(CleanupError, "symlink"):
                _read_control_file(link, 1024)
            value = base_inventory()
            victim = root / "retiring"
            victim.mkdir()
            value["subject"]["worktree_path"] = str(victim)
            value["worktrees"][0]["path"] = str(victim)
            inventory = validate_inventory(value)
            target = victim / "inventory.json"
            with self.assertRaisesRegex(CleanupError, "overlaps"):
                from workflowkit.cleanup import _assert_control_file_outside_inventory
                _assert_control_file_outside_inventory(target, inventory)


if __name__ == "__main__":
    unittest.main()
