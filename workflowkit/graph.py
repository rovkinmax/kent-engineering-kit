from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import uuid
from typing import Any

from .model import EdgeSpec, NodeSpec, ParameterSpec, SpecError, WorkflowSpec


@dataclass(frozen=True)
class WorkflowMutationPlan:
    document: dict[str, Any]
    graph_changed: bool
    metadata_changed: bool
    added_nodes: tuple[str, ...]
    removed_nodes: tuple[str, ...]
    added_edges: tuple[str, ...]
    removed_edges: tuple[str, ...]

    @property
    def destructive(self) -> bool:
        return bool(self.removed_nodes or self.removed_edges)

    @property
    def changed(self) -> bool:
        return self.graph_changed or self.metadata_changed

    def summary(self) -> dict[str, Any]:
        return {
            "graph_changed": self.graph_changed,
            "metadata_changed": self.metadata_changed,
            "destructive": self.destructive,
            "added_nodes": list(self.added_nodes),
            "removed_nodes": list(self.removed_nodes),
            "added_edges": list(self.added_edges),
            "removed_edges": list(self.removed_edges),
        }


def plan_workflow_graph(
    spec: WorkflowSpec,
    inspected: dict[str, Any],
    *,
    metadata_changed: bool,
) -> WorkflowMutationPlan:
    workflow_id = inspected.get("workflow_id")
    expected_version = inspected.get("expected_version")
    graph = inspected.get("graph")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise SpecError("workflow graph document has no workflow_id")
    if not isinstance(expected_version, int) or expected_version < 0:
        raise SpecError("workflow graph document has invalid expected_version")
    if not isinstance(graph, dict):
        raise SpecError("workflow graph document has no graph")
    collections: dict[str, list[dict[str, Any]]] = {}
    for key in ("node_groups", "nodes", "transition_groups", "edges"):
        value = graph.get(key)
        if not isinstance(value, list) or not all(
            isinstance(item, dict) for item in value
        ):
            raise SpecError(f"workflow graph document has invalid {key}")
        collections[key] = value
    if collections["node_groups"]:
        raise SpecError(
            "generated workflows do not support non-empty node_groups yet"
        )

    current_nodes = _unique_index(collections["nodes"], "key", "node")
    expected_node_keys = {node.key for node in spec.nodes}
    desired_nodes: list[dict[str, Any]] = []
    for existing in collections["nodes"]:
        key = existing.get("key")
        if key not in expected_node_keys:
            continue
        desired_nodes.append(_project_node(current_nodes[key], _node_spec(spec, key)))
    for node in spec.nodes:
        if node.key not in current_nodes:
            desired_nodes.append(_project_node(None, node))

    node_ids = {node["key"]: node["id"] for node in desired_nodes}
    node_keys_by_id = {node_id: key for key, node_id in node_ids.items()}
    source_keys = {
        node["id"]: node["key"]
        for node in collections["nodes"]
        if isinstance(node.get("id"), str) and isinstance(node.get("key"), str)
    }
    current_groups: dict[tuple[str, str], dict[str, Any]] = {}
    for group in collections["transition_groups"]:
        source = source_keys.get(group.get("source_node_id"))
        transition = group.get("transition_id")
        if source is None or not isinstance(transition, str):
            raise SpecError("workflow graph contains an invalid transition group")
        key = (source, transition)
        if key in current_groups:
            raise SpecError(f"workflow graph repeats transition group {key!r}")
        current_groups[key] = group

    group_specs: dict[tuple[str, str], EdgeSpec] = {}
    group_order: list[tuple[str, str]] = []
    for edge in spec.edges:
        key = (edge.source, edge.transition)
        if key not in group_specs:
            group_specs[key] = edge
            group_order.append(key)

    desired_groups: list[dict[str, Any]] = []
    retained_group_keys: set[tuple[str, str]] = set()
    for existing in collections["transition_groups"]:
        source = source_keys[existing["source_node_id"]]
        key = (source, existing["transition_id"])
        edge = group_specs.get(key)
        if edge is None:
            continue
        desired_groups.append(_project_group(existing, edge, node_ids))
        retained_group_keys.add(key)
    for key in group_order:
        if key not in retained_group_keys:
            desired_groups.append(_project_group(None, group_specs[key], node_ids))
    group_ids = {
        (
            node_keys_by_id[group["source_node_id"]],
            group["transition_id"],
        ): group["id"]
        for group in desired_groups
    }

    current_edges = _unique_index(collections["edges"], "key", "edge")
    expected_edge_keys = {edge.key for edge in spec.edges}
    desired_edges: list[dict[str, Any]] = []
    for existing in collections["edges"]:
        key = existing.get("key")
        if key not in expected_edge_keys:
            continue
        desired_edges.append(
            _project_edge(current_edges[key], _edge_spec(spec, key), node_ids, group_ids)
        )
    for edge in spec.edges:
        if edge.key not in current_edges:
            desired_edges.append(_project_edge(None, edge, node_ids, group_ids))

    desired_graph = deepcopy(graph)
    desired_graph["node_groups"] = []
    desired_graph["nodes"] = desired_nodes
    desired_graph["transition_groups"] = desired_groups
    desired_graph["edges"] = desired_edges
    document = deepcopy(inspected)
    document["workflow_id"] = workflow_id
    document["expected_version"] = expected_version
    document["graph"] = desired_graph

    actual_node_keys = set(current_nodes)
    actual_edge_keys = set(current_edges)
    return WorkflowMutationPlan(
        document=document,
        graph_changed=_canonical_json(desired_graph) != _canonical_json(graph),
        metadata_changed=metadata_changed,
        added_nodes=tuple(node.key for node in spec.nodes if node.key not in actual_node_keys),
        removed_nodes=tuple(
            node["key"]
            for node in collections["nodes"]
            if node.get("key") not in expected_node_keys
        ),
        added_edges=tuple(edge.key for edge in spec.edges if edge.key not in actual_edge_keys),
        removed_edges=tuple(
            edge["key"]
            for edge in collections["edges"]
            if edge.get("key") not in expected_edge_keys
        ),
    )


def graph_matches_spec(spec: WorkflowSpec, inspected: dict[str, Any]) -> bool:
    return not plan_workflow_graph(
        spec,
        inspected,
        metadata_changed=False,
    ).graph_changed


def _project_node(existing: dict[str, Any] | None, spec: NodeSpec) -> dict[str, Any]:
    node = deepcopy(existing) if existing is not None else {"id": str(uuid.uuid4())}
    node.update(
        {
            "key": spec.key,
            "kind": spec.kind,
            "display_name": spec.display_name,
            "group_id": None,
        }
    )
    for key in ("subagent_role", "completion_mode", "script_path"):
        node.pop(key, None)
    if spec.agent is not None:
        node["subagent_role"] = spec.agent
    if spec.completion_mode is not None:
        node["completion_mode"] = spec.completion_mode
    if spec.script_path is not None:
        node["script_path"] = spec.script_path
    return node


def _project_group(
    existing: dict[str, Any] | None,
    edge: EdgeSpec,
    node_ids: dict[str, str],
) -> dict[str, Any]:
    group = deepcopy(existing) if existing is not None else {"id": str(uuid.uuid4())}
    group.update(
        {
            "source_node_id": node_ids[edge.source],
            "transition_id": edge.transition,
            "display_name": _display_name(edge.transition),
            "description": edge.transition_description,
        }
    )
    return group


def _project_edge(
    existing: dict[str, Any] | None,
    spec: EdgeSpec,
    node_ids: dict[str, str],
    group_ids: dict[tuple[str, str], str],
) -> dict[str, Any]:
    edge = deepcopy(existing) if existing is not None else {"id": str(uuid.uuid4())}
    edge.update(
        {
            "transition_group_id": group_ids[(spec.source, spec.transition)],
            "key": spec.key,
            "target_node_id": node_ids[spec.target],
            "assignee_selection": spec.assignee_selection,
            "thinking_selection": spec.thinking_selection,
            "requires_approval": spec.requires_approval,
            "context_mode": spec.context,
            "context_source": _context_source(spec.context_source),
        }
    )
    if spec.parameters:
        edge["parameters"] = [
            {
                "key": parameter.key,
                "description": parameter.description,
                "purpose": parameter.purpose,
            }
            for parameter in spec.parameters
        ]
    else:
        edge.pop("parameters", None)
    if spec.prompt is None:
        edge.pop("prompt_template", None)
    else:
        edge["prompt_template"] = spec.prompt
    return edge


def _context_source(value: str) -> dict[str, str]:
    if value.startswith("node:"):
        return {"kind": "selected_node", "node_key": value.removeprefix("node:")}
    return {"kind": value}


def _display_name(key: str) -> str:
    return " ".join(part.capitalize() for part in key.split("_"))


def _unique_index(
    values: list[dict[str, Any]],
    key_name: str,
    label: str,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for value in values:
        key = value.get(key_name)
        if not isinstance(key, str) or not key:
            raise SpecError(f"workflow graph contains an invalid {label} key")
        if key in result:
            raise SpecError(f"workflow graph repeats {label} key {key!r}")
        result[key] = value
    return result


def _node_spec(spec: WorkflowSpec, key: str) -> NodeSpec:
    return next(node for node in spec.nodes if node.key == key)


def _edge_spec(spec: WorkflowSpec, key: str) -> EdgeSpec:
    return next(edge for edge in spec.edges if edge.key == key)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
