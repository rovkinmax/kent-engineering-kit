from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import uuid
from typing import Any

from .model import EdgeSpec, NodeSpec, SpecError, WorkflowSpec


# unicode.White_Space as used by Go strings.TrimSpace; Python's default strip
# additionally removes U+001C..U+001F, which native Kent preserves.
_GO_WHITE_SPACE = (
    "\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)


def native_graph_projection(
    graph: dict[str, Any],
    *,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    """Encode source graph rows as Kent persists them.

    Apply only to a source-built graph before diffing, hashing, or review. This
    is not readback normalization. Native rows omit source ``workflow_id``
    annotations, empty parameter arrays, and explicit selection/context
    defaults. Existing row order, IDs, unknown fields, and non-default values
    are retained.
    """
    projected = deepcopy(graph)
    for collection in ("nodes", "edges", "node_groups", "transition_groups"):
        for row in projected.get(collection, []):
            if not isinstance(row, dict) or "workflow_id" not in row:
                continue
            if not isinstance(workflow_id, str) or row["workflow_id"] != workflow_id:
                raise SpecError("source graph row Workflow identity differs")
            row.pop("workflow_id")
    for edge in projected.get("edges", []):
        if not isinstance(edge, dict):
            continue
        if isinstance(edge.get("prompt_template"), str):
            edge["prompt_template"] = edge["prompt_template"].strip(_GO_WHITE_SPACE)
        if edge.get("parameters") == []:
            edge.pop("parameters")
        for selection in ("assignee_selection", "thinking_selection"):
            if edge.get(selection) == "configured":
                edge.pop(selection)
        if (
            edge.get("context_mode") == "new_session"
            and edge.get("context_source") == {"kind": "immediate_source"}
        ):
            edge.pop("context_mode")
            edge.pop("context_source")
    return projected


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
    native_desired_graph = native_graph_projection(
        desired_graph,
        workflow_id=workflow_id,
    )
    native_source_graph = native_graph_projection(
        graph,
        workflow_id=workflow_id,
    )
    document_graph = deepcopy(native_desired_graph)
    projected_edges = {
        edge.get("id"): edge
        for edge in document_graph.get("edges", [])
        if isinstance(edge, dict)
    }
    for source_edge in desired_graph["edges"]:
        edge = projected_edges.get(source_edge.get("id"))
        if edge is None:
            continue
        for selection in ("assignee_selection", "thinking_selection"):
            if source_edge.get(selection) == "configured":
                edge[selection] = "configured"
        if (
            source_edge.get("context_mode") == "new_session"
            and source_edge.get("context_source") == {"kind": "immediate_source"}
        ):
            edge["context_mode"] = "new_session"
            edge["context_source"] = {"kind": "immediate_source"}
    document = deepcopy(inspected)
    document["workflow_id"] = workflow_id
    document["expected_version"] = expected_version
    document["graph"] = document_graph

    actual_node_keys = set(current_nodes)
    actual_edge_keys = set(current_edges)
    return WorkflowMutationPlan(
        document=document,
        graph_changed=(
            _canonical_json(native_desired_graph)
            != _canonical_json(native_source_graph)
        ),
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
    """Compare workflow semantics across explicit source/native default encodings.

    This is not the exact readback/effect-settlement comparator; callers that
    settle an effect must compare the raw native document without projection.
    """
    workflow_id = inspected.get("workflow_id")
    graph = inspected.get("graph")
    if isinstance(graph, dict):
        inspected = deepcopy(inspected)
        inspected["graph"] = native_graph_projection(
            graph,
            workflow_id=workflow_id if isinstance(workflow_id, str) else None,
        )
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
