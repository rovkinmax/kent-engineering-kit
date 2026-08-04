from __future__ import annotations

from dataclasses import dataclass, field
import re


MODEL_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
NODE_KINDS = {"start", "agent", "script", "join", "terminal"}
CONTEXT_MODES = {
    "new_session",
    "continue_session",
    "compact_and_continue_session",
}


class SpecError(ValueError):
    """Raised when a generated workflow violates the toolkit contract."""


@dataclass(frozen=True, order=True)
class ParameterSpec:
    key: str
    description: str

    def validate(self) -> None:
        validate_model_key(self.key, "parameter")
        if not self.description.strip():
            raise SpecError(f"parameter {self.key!r} has no description")


@dataclass(frozen=True)
class NodeSpec:
    key: str
    kind: str
    display_name: str
    agent: str | None = None
    completion_mode: str | None = None
    script_path: str | None = None

    def validate(self) -> None:
        validate_model_key(self.key, "node")
        if self.kind not in NODE_KINDS:
            raise SpecError(f"node {self.key!r} has unsupported kind {self.kind!r}")
        if not self.display_name.strip():
            raise SpecError(f"node {self.key!r} has no display name")
        if self.kind == "agent":
            if not self.agent:
                raise SpecError(f"agent node {self.key!r} has no role")
            if not self.completion_mode:
                raise SpecError(f"agent node {self.key!r} has no completion mode")
            if self.script_path:
                raise SpecError(f"agent node {self.key!r} has a script path")
        elif self.kind == "script":
            if not self.script_path:
                raise SpecError(f"script node {self.key!r} has no script path")
            if self.agent or self.completion_mode:
                raise SpecError(f"script node {self.key!r} has agent settings")
        elif self.agent or self.completion_mode or self.script_path:
            raise SpecError(f"{self.kind} node {self.key!r} has executable settings")


@dataclass(frozen=True)
class EdgeSpec:
    key: str
    source: str
    transition: str
    target: str
    context: str = "new_session"
    context_source: str = "immediate_source"
    prompt: str | None = None
    transition_description: str = ""
    parameters: tuple[ParameterSpec, ...] = field(default_factory=tuple)
    requires_approval: bool = False

    def validate(self) -> None:
        validate_model_key(self.key, "edge")
        validate_model_key(self.source, "edge source")
        validate_model_key(self.transition, "transition")
        validate_model_key(self.target, "edge target")
        if self.context not in CONTEXT_MODES:
            raise SpecError(
                f"edge {self.key!r} has unsupported context {self.context!r}"
            )
        if self.context == "new_session" and self.context_source != "immediate_source":
            raise SpecError(
                f"edge {self.key!r} uses a context source with new_session"
            )
        if not self.context_source.strip():
            raise SpecError(f"edge {self.key!r} has no context source")
        if not self.transition_description.strip():
            raise SpecError(f"edge {self.key!r} has no transition description")
        parameter_keys: set[str] = set()
        for parameter in self.parameters:
            parameter.validate()
            if parameter.key in parameter_keys:
                raise SpecError(
                    f"edge {self.key!r} repeats parameter {parameter.key!r}"
                )
            parameter_keys.add(parameter.key)


@dataclass(frozen=True)
class WorkflowSpec:
    name: str
    description: str
    execution_target: str
    nodes: tuple[NodeSpec, ...]
    edges: tuple[EdgeSpec, ...]

    def validate(self) -> None:
        if not self.name.strip():
            raise SpecError("workflow has no name")
        if not self.description.strip():
            raise SpecError("workflow has no description")
        validate_execution_target(self.execution_target)

        node_by_key: dict[str, NodeSpec] = {}
        for node in self.nodes:
            node.validate()
            if node.key in node_by_key:
                raise SpecError(f"duplicate node key {node.key!r}")
            node_by_key[node.key] = node

        if sum(node.kind == "start" for node in self.nodes) != 1:
            raise SpecError("workflow must have exactly one start node")
        if not any(node.kind == "terminal" for node in self.nodes):
            raise SpecError("workflow must have at least one terminal node")

        edge_by_key: dict[str, EdgeSpec] = {}
        groups: dict[tuple[str, str], list[EdgeSpec]] = {}
        transition_sources: dict[str, str] = {}
        outgoing: dict[str, list[EdgeSpec]] = {}
        for edge in self.edges:
            edge.validate()
            if edge.key in edge_by_key:
                raise SpecError(f"duplicate edge key {edge.key!r}")
            edge_by_key[edge.key] = edge
            if edge.source not in node_by_key:
                raise SpecError(
                    f"edge {edge.key!r} references unknown source {edge.source!r}"
                )
            if edge.target not in node_by_key:
                raise SpecError(
                    f"edge {edge.key!r} references unknown target {edge.target!r}"
                )
            previous_source = transition_sources.setdefault(
                edge.transition,
                edge.source,
            )
            if previous_source != edge.source:
                raise SpecError(
                    f"transition {edge.transition!r} is reused by source "
                    f"{edge.source!r}; transition keys must be workflow-wide unique"
                )

            target = node_by_key[edge.target]
            if target.kind == "agent" and not (edge.prompt or "").strip():
                raise SpecError(
                    f"edge {edge.key!r} targets an agent without a prompt"
                )
            if target.kind != "agent" and edge.prompt:
                raise SpecError(
                    f"edge {edge.key!r} targets {target.kind} but has a prompt"
                )

            groups.setdefault((edge.source, edge.transition), []).append(edge)
            outgoing.setdefault(edge.source, []).append(edge)

        for group_key, group_edges in groups.items():
            contracts = {
                tuple((parameter.key, parameter.description) for parameter in edge.parameters)
                for edge in group_edges
            }
            if len(contracts) != 1:
                raise SpecError(
                    f"transition group {group_key!r} has conflicting parameters"
                )

            if len(group_edges) <= 1:
                continue
            join_targets: set[str] = set()
            for branch_edge in group_edges:
                branch_outgoing = outgoing.get(branch_edge.target, [])
                if len(branch_outgoing) != 1:
                    raise SpecError(
                        f"fan-out branch {branch_edge.target!r} must have one "
                        "direct edge to Join"
                    )
                join_edge = branch_outgoing[0]
                join_node = node_by_key[join_edge.target]
                if join_node.kind != "join":
                    raise SpecError(
                        f"fan-out branch {branch_edge.target!r} does not target Join"
                    )
                join_targets.add(join_edge.target)
            if len(join_targets) != 1:
                raise SpecError(
                    f"fan-out transition group {group_key!r} uses multiple Joins"
                )


def validate_model_key(key: str, label: str) -> None:
    if not MODEL_KEY_PATTERN.fullmatch(key):
        raise SpecError(f"{label} key {key!r} is not a stable model key")


def validate_execution_target(target: str) -> None:
    if target in {"ask-on-first-execution", "none", "head", "default-branch"}:
        return
    if target.startswith("ref:"):
        revision = target[4:]
        if revision and not any(character.isspace() for character in revision):
            return
    raise SpecError(f"unsupported execution target {target!r}")
