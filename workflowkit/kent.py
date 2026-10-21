from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import uuid
from typing import Any

from .graph import WorkflowMutationPlan, graph_matches_spec, plan_workflow_graph
from .model import SpecError, WorkflowSpec


class KentCommandError(RuntimeError):
    """Raised when the Kent CLI rejects a generator operation."""


@dataclass(frozen=True)
class CanonicalWorkflowCompatibilityPlan:
    """Exact-hash authorization to delegate an existing Workflow update."""

    path: Path
    expected_sha256: str
    confirmation_sha256: str


class KentClient:
    def __init__(
        self,
        workspace: Path,
        binary: str = "kent",
        project_workspace: Path | None = None,
    ) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.project_workspace = (
            project_workspace.expanduser().resolve()
            if project_workspace is not None
            else primary_worktree_root(self.workspace)
        )
        self.binary = binary
        self._workflow_selector_cache: dict[str, str] = {}

    def apply(
        self,
        spec: WorkflowSpec,
        *,
        minimum_version: tuple[int, int, int] = (2, 6, 1),
        set_default: bool = False,
        workflow_selector: str | None = None,
        compatibility_plan: CanonicalWorkflowCompatibilityPlan | None = None,
    ) -> dict[str, Any]:
        spec.validate()
        if compatibility_plan is not None:
            if set_default:
                raise SpecError(
                    "a compatibility-plan update cannot create or change a project default"
                )
            workflow_id = canonical_workflow_selector(workflow_selector)
            if workflow_id is None or workflow_selector != workflow_id:
                raise SpecError(
                    "a compatibility-plan update requires an explicit Workflow UUID"
                )
            if compatibility_plan.confirmation_sha256 != compatibility_plan.expected_sha256:
                raise SpecError(
                    "compatibility confirmation must exactly match the expected plan digest"
                )
            from shutil import which
            from .operations import (
                OperationError,
                load_plan,
                reconcile_canonical_workflows,
            )

            binary_path = Path(self.binary).expanduser()
            if not binary_path.is_absolute():
                resolved_binary = which(self.binary)
                if resolved_binary is None:
                    raise SpecError("configured Kent executable cannot be resolved")
                binary_path = Path(resolved_binary)
            expected_binary = binary_path.resolve()
            try:
                plan = load_plan(
                    compatibility_plan.path,
                    schema="canonical-workflow-reconcile-plan-v2",
                    expected_sha256=compatibility_plan.expected_sha256,
                    mutation=True,
                    confirm=compatibility_plan.confirmation_sha256,
                )
            except (OperationError, OSError) as error:
                raise SpecError(f"compatibility plan is invalid: {error}") from error
            value = plan.value
            if (
                Path(value.get("project_root", "")).expanduser().resolve()
                != self.project_workspace
                or Path(value.get("kent", {}).get("path", "")).expanduser().resolve()
                != expected_binary
                or len(value.get("workflows", [])) != 1
                or canonical_workflow_selector(
                    value["workflows"][0].get("workflow_id")
                ) != workflow_id
            ):
                raise SpecError(
                    "compatibility plan project, Kent executable, or Workflow UUID differs"
                )
            try:
                prepared = reconcile_canonical_workflows(
                    plan,
                    mode="prepare",
                    kent=str(expected_binary),
                    expected_workflow_id=workflow_id,
                    expected_spec=spec,
                )
            except (OperationError, OSError) as error:
                raise SpecError(f"compatibility plan could not be prepared: {error}") from error
            if prepared.get("phase") != "prepared":
                raise SpecError(
                    "compatibility plan did not reach its prepared state"
                )
            try:
                return reconcile_canonical_workflows(
                    plan,
                    mode="apply",
                    confirm=compatibility_plan.confirmation_sha256,
                    kent=str(expected_binary),
                    expected_workflow_id=workflow_id,
                    expected_spec=spec,
                )
            except (OperationError, OSError) as error:
                raise SpecError(f"compatibility plan could not be applied: {error}") from error
        self.require_version(*minimum_version)
        self.preflight_scripts(spec)

        definition = self.inspect(workflow_selector or spec.name)
        created = definition is None
        if definition is None:
            if workflow_selector is not None:
                raise SpecError(
                    f"explicit workflow {workflow_selector!r} was not found"
                )
            self.run_json(
                [
                    "workflow",
                    "create",
                    "--description",
                    spec.description,
                    "--json",
                    spec.name,
                ]
            )
            definition = self.require_inspect(spec.name)

        workflow_ref = workflow_selector_from_definition(definition)
        graph_before = self.inspect_graph(workflow_ref)
        workflow_before = definition["workflow"]
        metadata_changed = not metadata_matches(workflow_before, spec)
        plan = plan_workflow_graph(
            spec,
            graph_before,
            metadata_changed=metadata_changed,
        )
        if plan.graph_changed and not created and self.workflow_has_tasks(definition):
            raise SpecError(
                f"workflow {workflow_before['name']!r} has tasks; this client "
                "does not support task-referenced semantic updates. Use a "
                "separately approved lifecycle operation"
            )
        if plan.graph_changed and not created and self.workflow_is_linked(definition):
            raise SpecError(
                f"workflow {workflow_before['name']!r} is linked to a project; "
                "this client does not support linked semantic updates. Use a "
                "separately approved lifecycle operation"
            )

        graph_saved = False
        metadata_saved = False
        try:
            if plan.graph_changed:
                self.apply_graph_plan(plan)
                graph_saved = True
            if plan.metadata_changed:
                self.ensure_workflow_metadata(workflow_ref, spec, definition)
                metadata_saved = True
            definition = self.require_inspect(workflow_ref)
            graph_after = self.inspect_graph(workflow_ref)
            self.assert_exact_graph_document(spec, graph_after)
            if not metadata_matches(definition["workflow"], spec):
                raise SpecError(
                    f"workflow {spec.name!r} metadata does not match the specification"
                )
            self.validate(workflow_ref)
        except Exception as original_error:
            if graph_saved or metadata_saved:
                try:
                    self.rollback_reconcile(
                        workflow_ref,
                        graph_before=graph_before,
                        workflow_before=workflow_before,
                        restore_graph=graph_saved,
                        restore_metadata=metadata_saved,
                    )
                except Exception as rollback_error:
                    raise KentCommandError(
                        f"workflow reconcile failed: {original_error}; "
                        f"rollback also failed: {rollback_error}"
                    ) from original_error
            raise

        self.link(workflow_ref, set_default=set_default)
        self.validate(workflow_ref)
        return self.require_inspect(workflow_ref)

    def inspect_graph(self, workflow: str) -> dict[str, Any]:
        return self.run_json(["workflow", "graph", "inspect", workflow])

    def apply_graph_plan(self, plan: WorkflowMutationPlan) -> dict[str, Any]:
        return self.apply_graph_document(
            plan.document,
            confirm_destructive=plan.destructive,
        )

    def apply_graph_document(
        self,
        document: dict[str, Any],
        *,
        confirm_destructive: bool,
    ) -> dict[str, Any]:
        encoded = json.dumps(document, ensure_ascii=False)
        result = self.run(
            ["workflow", "graph", "apply", "-", "--json"],
            check=False,
            input_text=encoded,
        )
        outcome = decode_json(
            result.stdout,
            f"{self.binary} workflow graph apply",
        )
        kind = outcome.get("outcome")
        if result.returncode == 0 and kind in {"saved", "unchanged"}:
            return outcome
        if kind == "confirmation_required" and confirm_destructive:
            confirmed = self.run(
                ["workflow", "graph", "apply", "-", "--confirm", "--json"],
                check=False,
                input_text=encoded,
            )
            confirmed_outcome = decode_json(
                confirmed.stdout,
                f"{self.binary} workflow graph apply --confirm",
            )
            if confirmed.returncode == 0 and confirmed_outcome.get("outcome") in {
                "saved",
                "unchanged",
            }:
                return confirmed_outcome
            raise KentCommandError(graph_apply_error(confirmed, confirmed_outcome))
        raise KentCommandError(graph_apply_error(result, outcome))

    def rollback_reconcile(
        self,
        workflow_ref: str,
        *,
        graph_before: dict[str, Any],
        workflow_before: dict[str, Any],
        restore_graph: bool,
        restore_metadata: bool,
    ) -> None:
        failures: list[str] = []
        if restore_graph:
            try:
                current = self.inspect_graph(workflow_ref)
                rollback = json.loads(json.dumps(graph_before))
                rollback["expected_version"] = current["expected_version"]
                self.apply_graph_document(rollback, confirm_destructive=True)
            except Exception as error:
                failures.append(f"graph rollback failed: {error}")
        if restore_metadata:
            try:
                self.run_json(
                    [
                        "workflow",
                        "update",
                        workflow_ref,
                        "--description",
                        workflow_before.get("description") or "",
                        "--execution-target",
                        execution_target_from_policy(
                            workflow_before.get("execution_target_policy") or {}
                        ),
                        "--json",
                    ]
                )
            except Exception as error:
                failures.append(f"metadata rollback failed: {error}")
        if failures:
            raise KentCommandError("; ".join(failures))

    def assert_exact_graph_document(
        self,
        spec: WorkflowSpec,
        graph: dict[str, Any],
    ) -> None:
        if not graph_matches_spec(spec, graph):
            raise SpecError(
                f"workflow {spec.name!r} graph does not match the specification"
            )

    def workflow_has_tasks(self, definition: dict[str, Any]) -> bool:
        workflow_id = definition.get("workflow", {}).get("id")
        workflow_ref = canonical_workflow_selector(workflow_id)
        if workflow_ref is None:
            raise KentCommandError(
                f"workflow definition returned invalid id {workflow_id!r}"
            )
        for project_id in self.project_ids():
            linked_workflows = {
                canonical_workflow_selector(record.get("id"))
                for record in self.workflow_records(project_id=project_id)
            }
            if workflow_ref not in linked_workflows:
                continue
            result = self.run(
                [
                    "task",
                    "list",
                    "--project",
                    project_id,
                    "--workflow",
                    workflow_ref,
                    "--limit",
                    "1",
                    "--json",
                ],
                check=False,
            )
            if result.returncode != 0:
                raise SpecError(
                    f"cannot prove workflow "
                    f"{definition['workflow']['name']!r} is taskless in "
                    f"project {project_id!r}: {command_error(result)}"
                )
            payload = decode_json(
                result.stdout,
                (
                    f"task list for workflow "
                    f"{definition['workflow']['name']!r} in {project_id}"
                ),
            )
            if payload.get("tasks"):
                return True
        return False

    def workflow_is_linked(self, definition: dict[str, Any]) -> bool:
        workflow_id = definition.get("workflow", {}).get("id")
        workflow_ref = canonical_workflow_selector(workflow_id)
        if workflow_ref is None:
            raise KentCommandError(
                f"workflow definition returned invalid id {workflow_id!r}"
            )
        for project_id in self.project_ids():
            linked_workflows = {
                canonical_workflow_selector(record.get("id"))
                for record in self.workflow_records(project_id=project_id)
            }
            if workflow_ref in linked_workflows:
                return True
        return False

    def project_ids(self) -> tuple[str, ...]:
        result = self.run(["project", "list"], check=False)
        if result.returncode != 0:
            raise SpecError(
                "cannot enumerate Kent projects before workflow mutation: "
                + command_error(result)
            )
        project_ids = tuple(
            line.split("\t", 1)[0].strip()
            for line in result.stdout.splitlines()
            if line.split("\t", 1)[0].strip().startswith("project-")
        )
        if not project_ids:
            raise SpecError(
                "cannot enumerate Kent projects before workflow mutation: "
                "project list was empty or unrecognized"
            )
        return project_ids

    def inspect(self, workflow: str) -> dict[str, Any] | None:
        workflow_ref = self.resolve_workflow_selector(workflow)
        if workflow_ref is None:
            return None
        result = self.run(
            ["workflow", "inspect", workflow_ref, "--json"],
            check=False,
        )
        if result.returncode == 0:
            return decode_json(result.stdout, f"workflow inspect {workflow!r}")
        if "not found" in result.stderr.lower():
            return None
        raise KentCommandError(command_error(result))

    def resolve_workflow_selector(self, workflow: str) -> str | None:
        cached = self._workflow_selector_cache.get(workflow)
        if cached is not None:
            return cached

        canonical = canonical_workflow_selector(workflow)
        records = self.workflow_records()
        for record in records:
            workflow_id = record.get("id")
            record_canonical = canonical_workflow_selector(workflow_id)
            workflow_name = record.get("name")
            if record_canonical is None or not isinstance(workflow_name, str):
                continue
            self._workflow_selector_cache[workflow_id] = workflow_id
            self._workflow_selector_cache[record_canonical] = workflow_id
            self._workflow_selector_cache[
                f"workflow-{record_canonical}"
            ] = workflow_id

        if canonical is not None:
            matches = [
                record
                for record in records
                if canonical_workflow_selector(record.get("id")) == canonical
            ]
        else:
            matches = [
                record for record in records if record.get("name") == workflow
            ]
        if not matches:
            return None
        if len(matches) > 1:
            raise KentCommandError(
                f"workflow name {workflow!r} is ambiguous; use a bare UUID"
            )
        workflow_id = matches[0].get("id")
        if canonical_workflow_selector(workflow_id) is None:
            raise KentCommandError(
                f"workflow {workflow!r} returned invalid id {workflow_id!r}"
            )
        self._workflow_selector_cache[workflow] = workflow_id
        workflow_name = matches[0].get("name")
        if isinstance(workflow_name, str):
            self._workflow_selector_cache[workflow_name] = workflow_id
        return workflow_id

    def workflow_records(
        self,
        *,
        project_id: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        records: list[dict[str, Any]] = []
        offset = 0
        while True:
            args = [
                "workflow",
                "list",
            ]
            if project_id is not None:
                args.extend(["--project", project_id])
            args.extend(["--limit", "100", "--offset", str(offset), "--json"])
            payload = self.run_json(args)
            page = payload.get("workflows")
            if not isinstance(page, list) or not all(
                isinstance(record, dict) for record in page
            ):
                raise KentCommandError(
                    "workflow list returned an invalid workflows collection"
                )
            records.extend(page)
            next_offset = payload.get("next_offset")
            if next_offset is not None and not isinstance(next_offset, int):
                raise KentCommandError(
                    "workflow list returned an invalid next_offset"
                )
            if next_offset is None:
                return tuple(records)
            offset = next_offset

    def require_inspect(self, workflow: str) -> dict[str, Any]:
        definition = self.inspect(workflow)
        if definition is None:
            raise KentCommandError(f"workflow {workflow!r} disappeared")
        return definition

    def validate(self, workflow: str) -> dict[str, Any]:
        result = self.run_json(
            [
                "workflow",
                "validate",
                workflow,
                "--mode",
                "execution",
                "--json",
            ]
        )
        if result.get("valid") is not True:
            raise KentCommandError(
                "workflow execution validation failed:\n"
                + json.dumps(result, indent=2, ensure_ascii=False)
            )
        return result

    def link(self, workflow: str, *, set_default: bool) -> None:
        args = [
            "workflow",
            "link",
            str(self.project_workspace),
            workflow,
        ]
        if set_default:
            args.append("--default")
        args.append("--json")
        result = self.run(args, check=False)
        if result.returncode == 0:
            return
        if "already linked" in result.stderr.lower():
            if set_default:
                self.run_json(
                    [
                        "workflow",
                        "default",
                        str(self.project_workspace),
                        workflow,
                        "--json",
                    ]
                )
            return
        raise KentCommandError(command_error(result))

    def export_snapshot(
        self,
        definition: dict[str, Any],
        destination: Path,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(definition, indent=2, ensure_ascii=False) + "\n"
        )

    def ensure_workflow_metadata(
        self,
        workflow_ref: str,
        spec: WorkflowSpec,
        definition: dict[str, Any],
    ) -> None:
        workflow = definition["workflow"]
        policy = workflow.get("execution_target_policy") or {}
        current_target = execution_target_from_policy(policy)
        if (
            workflow.get("description") == spec.description
            and current_target == spec.execution_target
        ):
            return
        self.run_json(
            [
                "workflow",
                "update",
                workflow_ref,
                "--description",
                spec.description,
                "--execution-target",
                spec.execution_target,
                "--json",
            ]
        )

    def require_version(self, major: int, minor: int, patch: int) -> None:
        result = self.run(["--version"])
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout)
        if match is None:
            raise KentCommandError(
                f"cannot parse Kent version from {result.stdout!r}"
            )
        actual = tuple(int(part) for part in match.groups())
        required = (major, minor, patch)
        if actual < required:
            raise KentCommandError(
                f"Kent {major}.{minor}.{patch}+ is required; found "
                f"{'.'.join(str(part) for part in actual)}"
            )

    def preflight_scripts(self, spec: WorkflowSpec) -> None:
        for node in spec.nodes:
            if node.kind != "script" or node.script_path is None:
                continue
            path = Path(node.script_path).expanduser()
            if not path.is_absolute():
                path = self.workspace / path
            if not path.is_file():
                raise SpecError(
                    f"script node {node.key!r} path does not exist: {path}"
                )
            if not os.access(path, os.X_OK):
                raise SpecError(
                    f"script node {node.key!r} path is not executable: {path}"
                )

    def run_json(self, args: list[str]) -> dict[str, Any]:
        result = self.run(args)
        return decode_json(result.stdout, " ".join([self.binary, *args]))

    def run(
        self,
        args: list[str],
        *,
        check: bool = True,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        for key in ("KENT_SESSION_ID", "KENT_RUN_ID", "KENT_STEP_ID"):
            environment.pop(key, None)
        result = subprocess.run(
            [self.binary, *args],
            cwd=self.workspace,
            env=environment,
            text=True,
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and result.returncode != 0:
            raise KentCommandError(command_error(result))
        return result


def primary_worktree_root(workspace: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(workspace), "worktree", "list", "--porcelain"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        return workspace
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            return Path(line.removeprefix("worktree ")).expanduser().resolve()
    return workspace


def metadata_matches(workflow: dict[str, Any], spec: WorkflowSpec) -> bool:
    return (
        workflow.get("description") == spec.description
        and execution_target_from_policy(
            workflow.get("execution_target_policy") or {}
        )
        == spec.execution_target
    )


def execution_target_from_policy(raw: dict[str, Any]) -> str:
    mode = raw.get("mode")
    if mode == "ask_on_first_execution":
        return "ask-on-first-execution"
    if mode == "default_branch":
        return "default-branch"
    if mode == "custom_ref":
        return f"ref:{raw.get('custom_ref', '')}"
    if mode in {"none", "head"}:
        return mode
    return ""


def canonical_workflow_selector(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    candidate = raw.removeprefix("workflow-")
    try:
        parsed = uuid.UUID(candidate)
    except (ValueError, AttributeError):
        return None
    if parsed.version != 4 or str(parsed) != candidate.lower():
        return None
    return str(parsed)


def workflow_selector_from_definition(definition: dict[str, Any]) -> str:
    workflow_id = definition.get("workflow", {}).get("id")
    if canonical_workflow_selector(workflow_id) is None:
        raise KentCommandError(
            f"workflow definition returned invalid id {workflow_id!r}"
        )
    return workflow_id


def decode_json(raw: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise KentCommandError(f"{label} returned invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise KentCommandError(f"{label} returned non-object JSON")
    return value


def command_error(result: subprocess.CompletedProcess[str]) -> str:
    command = " ".join(str(part) for part in result.args)
    detail = result.stderr.strip() or result.stdout.strip() or "no output"
    return f"{command} failed with exit {result.returncode}: {detail}"


def graph_apply_error(
    result: subprocess.CompletedProcess[str],
    outcome: dict[str, Any],
) -> str:
    message = outcome.get("message")
    blockers = outcome.get("blockers")
    detail = message or blockers or result.stderr.strip() or "no diagnostic"
    return (
        f"{' '.join(str(part) for part in result.args)} failed with "
        f"outcome {outcome.get('outcome')!r}: {detail}"
    )


def spec_as_json(spec: WorkflowSpec) -> dict[str, Any]:
    return asdict(spec)
