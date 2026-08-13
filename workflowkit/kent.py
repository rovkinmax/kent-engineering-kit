from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import subprocess
import uuid
from typing import Any

from .graph import WorkflowMutationPlan, graph_matches_spec, plan_workflow_graph
from .model import EdgeSpec, NodeSpec, ParameterSpec, SpecError, WorkflowSpec


class KentCommandError(RuntimeError):
    """Raised when the Kent CLI rejects a generator operation."""


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
    ) -> dict[str, Any]:
        spec.validate()
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
                f"workflow {workflow_before['name']!r} has tasks; its graph is "
                "frozen. Generate a new workflow version or migrate/retire its "
                "tasks first"
            )
        if plan.graph_changed and not created and self.workflow_is_linked(definition):
            raise SpecError(
                f"workflow {workflow_before['name']!r} is linked to a project; "
                "generate a new workflow revision instead of reconciling it "
                "in place"
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

    def preflight_reconcile(
        self,
        spec: WorkflowSpec,
        definition: dict[str, Any],
    ) -> bool:
        self.assert_no_extra_nodes(spec, definition)

        expected_edges = {edge.key for edge in spec.edges}
        actual_edges = {
            edge["key"]
            for edge in (definition.get("edges") or [])
        }
        extra_edges = sorted(actual_edges - expected_edges)
        if extra_edges:
            raise SpecError(
                f"workflow {spec.name!r} contains unexpected edges {extra_edges}; "
                "use another experimental label"
            )

        indexed_edges = edge_index(definition)
        for edge in spec.edges:
            existing = indexed_edges.get(edge.key)
            if existing is None:
                continue
            if existing["source"] != edge.source:
                raise SpecError(
                    f"edge {edge.key!r} changed source from "
                    f"{existing['source']!r} to {edge.source!r}; use another "
                    "experimental label"
                )
            if existing["requires_approval"] and not edge.requires_approval:
                raise SpecError(
                    f"edge {edge.key!r} would remove approval; use another "
                    "experimental label"
                )

        workflow = definition["workflow"]
        metadata_matches = (
            workflow.get("description") == spec.description
            and execution_target_from_policy(
                workflow.get("execution_target_policy") or {}
            )
            == spec.execution_target
        )
        node_index = {
            node["key"]: node
            for node in (definition.get("nodes") or [])
        }
        nodes_match = all(
            node.key in node_index and node_matches(node_index[node.key], node)
            for node in spec.nodes
        )
        edges_match = all(
            edge.key in indexed_edges and edge_matches(indexed_edges[edge.key], edge)
            for edge in spec.edges
        )
        return not (metadata_matches and nodes_match and edges_match)

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

    def ensure_node(
        self,
        workflow: str,
        spec: NodeSpec,
        definition: dict[str, Any],
    ) -> None:
        existing = next(
            (
                node
                for node in (definition.get("nodes") or [])
                if node["key"] == spec.key
            ),
            None,
        )
        if existing is not None and node_matches(existing, spec):
            return

        if existing is None:
            args = [
                "workflow",
                "node",
                "add",
                workflow,
                "--key",
                spec.key,
                "--kind",
                spec.kind,
                "--display-name",
                spec.display_name,
            ]
        else:
            args = [
                "workflow",
                "node",
                "update",
                workflow,
                spec.key,
                "--kind",
                spec.kind,
                "--display-name",
                spec.display_name,
            ]

        if spec.agent:
            args.extend(["--agent", spec.agent])
        if spec.completion_mode:
            args.extend(["--completion-mode", spec.completion_mode])
        if spec.script_path:
            args.extend(["--script-path", spec.script_path])
        args.append("--json")
        self.run_json(args)

    def ensure_edge(
        self,
        workflow: str,
        spec: EdgeSpec,
        definition: dict[str, Any],
    ) -> None:
        index = edge_index(definition)
        existing = index.get(spec.key)
        if existing is not None and edge_matches(existing, spec):
            return

        if existing is None:
            args = [
                "workflow",
                "edge",
                "add",
                workflow,
                "--from",
                spec.source,
                "--transition",
                spec.transition,
                "--edge-key",
                spec.key,
                "--to",
                spec.target,
                "--context",
                spec.context,
            ]
        else:
            if existing["source"] != spec.source:
                raise SpecError(
                    f"edge {spec.key!r} changed source from "
                    f"{existing['source']!r} to {spec.source!r}; use another "
                    "experimental label"
                )
            if existing["requires_approval"] and not spec.requires_approval:
                raise SpecError(
                    f"edge {spec.key!r} would remove approval; use another "
                    "experimental label"
                )
            args = [
                "workflow",
                "edge",
                "update",
                workflow,
                existing["id"],
                "--transition",
                spec.transition,
                "--edge-key",
                spec.key,
                "--to",
                spec.target,
                "--context",
                spec.context,
            ]

        args.extend(
            [
                "--transition-description",
                spec.transition_description,
            ]
        )
        args.extend(["--context-source", spec.context_source])
        if existing is not None:
            args.extend(["--prompt", spec.prompt or ""])
        elif spec.prompt is not None:
            args.extend(["--prompt", spec.prompt])
        if spec.requires_approval:
            args.append("--requires-approval")
        if spec.parameters:
            for parameter in spec.parameters:
                args.extend(
                    [
                        "--param",
                        f"{parameter.key}={parameter.description}",
                    ]
                )
        elif existing is not None and existing["parameters"]:
            args.append("--clear-params")
        args.append("--json")
        self.run_json(args)

    def assert_no_extra_nodes(
        self,
        spec: WorkflowSpec,
        definition: dict[str, Any],
    ) -> None:
        expected = {node.key for node in spec.nodes}
        actual = {
            node["key"]
            for node in (definition.get("nodes") or [])
        }
        extra = sorted(actual - expected)
        if extra:
            raise SpecError(
                f"workflow {spec.name!r} contains unexpected nodes {extra}; "
                "use another experimental label"
            )

    def assert_exact_graph(
        self,
        spec: WorkflowSpec,
        definition: dict[str, Any],
    ) -> None:
        self.assert_no_extra_nodes(spec, definition)
        workflow = definition["workflow"]
        current_target = execution_target_from_policy(
            workflow.get("execution_target_policy") or {}
        )
        if (
            workflow.get("description") != spec.description
            or current_target != spec.execution_target
        ):
            raise SpecError(
                f"workflow {spec.name!r} metadata does not match the specification"
            )

        node_index = {
            node["key"]: node
            for node in (definition.get("nodes") or [])
        }
        missing_nodes = sorted(
            node.key
            for node in spec.nodes
            if node.key not in node_index
        )
        mismatched_nodes = sorted(
            node.key
            for node in spec.nodes
            if node.key in node_index and not node_matches(node_index[node.key], node)
        )

        expected_edges = {edge.key for edge in spec.edges}
        actual_edges = {
            edge["key"]
            for edge in (definition.get("edges") or [])
        }
        extra_edges = sorted(actual_edges - expected_edges)
        missing_edges = sorted(expected_edges - actual_edges)
        indexed_edges = edge_index(definition)
        mismatched_edges = sorted(
            edge.key
            for edge in spec.edges
            if edge.key in indexed_edges
            and not edge_matches(indexed_edges[edge.key], edge)
        )
        if (
            missing_nodes
            or mismatched_nodes
            or extra_edges
            or missing_edges
            or mismatched_edges
        ):
            raise SpecError(
                f"workflow {spec.name!r} semantic mismatch; "
                f"missing_nodes={missing_nodes}, "
                f"mismatched_nodes={mismatched_nodes}, "
                f"extra_edges={extra_edges}, missing_edges={missing_edges}, "
                f"mismatched_edges={mismatched_edges}. "
                "Use another experimental label if reconciliation cannot clear it."
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


def node_matches(existing: dict[str, Any], spec: NodeSpec) -> bool:
    return (
        existing.get("kind") == spec.kind
        and existing.get("display_name") == spec.display_name
        and existing.get("subagent_role") == spec.agent
        and existing.get("completion_mode") == spec.completion_mode
        and existing.get("script_path") == spec.script_path
    )


def edge_index(definition: dict[str, Any]) -> dict[str, dict[str, Any]]:
    nodes = {
        node["id"]: node["key"]
        for node in (definition.get("nodes") or [])
    }
    groups = {
        group["id"]: group
        for group in (definition.get("transition_groups") or [])
    }
    derived_wiring = definition.get("derived_wiring") or {}
    contracts = {
        item["edge_id"]: tuple(
            ParameterSpec(
                field["name"],
                field["description"],
                field.get("purpose", "ordinary"),
            )
            for field in item.get("required_provision_fields", [])
        )
        for item in (derived_wiring.get("edges") or [])
    }
    index: dict[str, dict[str, Any]] = {}
    for edge in (definition.get("edges") or []):
        group = groups[edge["transition_group_id"]]
        index[edge["key"]] = {
            "id": edge["id"],
            "source": nodes[group["source_node_id"]],
            "transition": group["transition_id"],
            "target": nodes[edge["target_node_id"]],
            "context": edge["context_mode"],
            "context_source": context_source_string(edge["context_source"]),
            "requires_approval": edge["requires_approval"],
            "prompt": edge.get("prompt_template"),
            "description": group.get("description") or "",
            "parameters": contracts.get(edge["id"], ()),
            "assignee_selection": edge.get("assignee_selection", "configured"),
            "thinking_selection": edge.get("thinking_selection", "configured"),
        }
    return index


def edge_matches(existing: dict[str, Any], spec: EdgeSpec) -> bool:
    return (
        existing["source"] == spec.source
        and existing["transition"] == spec.transition
        and existing["target"] == spec.target
        and existing["context"] == spec.context
        and existing["context_source"] == spec.context_source
        and existing["requires_approval"] == spec.requires_approval
        and (existing["prompt"] or None) == (spec.prompt or None)
        and existing["description"] == spec.transition_description
        and existing["parameters"] == spec.parameters
        and existing.get("assignee_selection", "configured")
        == spec.assignee_selection
        and existing.get("thinking_selection", "configured")
        == spec.thinking_selection
    )


def metadata_matches(workflow: dict[str, Any], spec: WorkflowSpec) -> bool:
    return (
        workflow.get("description") == spec.description
        and execution_target_from_policy(
            workflow.get("execution_target_policy") or {}
        )
        == spec.execution_target
    )


def context_source_string(raw: dict[str, Any] | None) -> str:
    if not raw:
        return "immediate_source"
    kind = raw.get("kind", "immediate_source")
    if kind in {"node", "selected_node"}:
        return f"node:{raw['node_key']}"
    return kind


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
